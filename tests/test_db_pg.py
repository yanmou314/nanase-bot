import asyncio
import json

import pytest

from helpers import load_module

db_pg = load_module("plugins/chat_stats/db_pg.py", "plugin_db_pg")


def test_load_dsn_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(db_pg, "CFG_FILE", str(tmp_path / "none.json"))
    with pytest.raises(RuntimeError):
        db_pg.load_dsn()


def test_load_dsn_rejects_non_postgres(monkeypatch, tmp_path):
    f = tmp_path / "db.json"
    f.write_text(json.dumps({"dsn": "mysql://x/y"}), encoding="utf-8")
    monkeypatch.setattr(db_pg, "CFG_FILE", str(f))
    with pytest.raises(RuntimeError):
        db_pg.load_dsn()


def test_load_dsn_ok(monkeypatch, tmp_path):
    f = tmp_path / "db.json"
    f.write_text(json.dumps({"dsn": "postgresql://u:p@127.0.0.1:5432/db"}), encoding="utf-8")
    monkeypatch.setattr(db_pg, "CFG_FILE", str(f))
    assert db_pg.load_dsn().startswith("postgresql://")


def _run_writer(db_pg, monkeypatch, items, fail_first=0):
    """驱动 _write_loop 消费 items，返回写入成功的批次列表。"""
    written = []
    state = {"fails": fail_first}

    async def fake_write_batch(batch):
        if state["fails"] > 0:
            state["fails"] -= 1
            raise RuntimeError("db down")
        written.append(list(batch))

    monkeypatch.setattr(db_pg, "_write_batch", fake_write_batch)
    monkeypatch.setattr(db_pg, "_FLUSH_INTERVAL", 0.02)

    async def main():
        q = asyncio.Queue()
        db_pg._write_queue = q
        task = asyncio.create_task(db_pg._write_loop())
        try:
            for item in items:
                q.put_nowait(item)
            # 失败路径先 task_done 再重新入队，join() 会在重入队前放行，
            # 因此按"最终写入条数"轮询等待，而不是只等 join()。
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while sum(len(b) for b in written) < len(items):
                if loop.time() > deadline:
                    raise TimeoutError("write loop did not flush all items")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            db_pg._write_queue = None
        return written

    return asyncio.run(main())


def test_write_loop_batches_all_items(monkeypatch):
    items = [(1, 100, "text", f"m{i}", "2026-08-16", 14) for i in range(7)]
    written = _run_writer(db_pg, monkeypatch, items)
    flat = [m for batch in written for m in batch]
    assert flat == items


def test_write_loop_requeues_after_failure(monkeypatch):
    items = [(1, 100, "text", f"m{i}", "2026-08-16", 14) for i in range(3)]
    written = _run_writer(db_pg, monkeypatch, items, fail_first=1)
    flat = [m for batch in written for m in batch]
    assert flat == items  # 失败批次重试后全部写入，且不丢不重


def test_write_loop_requeue_does_not_deadlock_when_full(monkeypatch):
    """失败批次重入队时队列已满：必须丢弃而不是 await put() 把写循环挂死。"""
    written = []
    state = {"fails": 1}

    async def main():
        q = asyncio.Queue(maxsize=3)

        async def fake_write_batch(batch):
            if state["fails"] > 0:
                # 模拟写库期间并发生产者把队列填满，重入队时必然撞上 QueueFull
                for i in range(3):
                    q.put_nowait((1, 100, "text", f"extra{i}", "2026-08-16", 14))
                state["fails"] -= 1
                raise RuntimeError("db down")
            written.append(list(batch))

        monkeypatch.setattr(db_pg, "_write_batch", fake_write_batch)
        monkeypatch.setattr(db_pg, "_FLUSH_INTERVAL", 0.02)
        db_pg._write_queue = q
        q.put_nowait((1, 100, "text", "m0", "2026-08-16", 14))
        task = asyncio.create_task(db_pg._write_loop())
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            while sum(len(b) for b in written) < 3:
                if loop.time() > deadline:
                    raise TimeoutError("write loop deadlocked on full queue")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            db_pg._write_queue = None

    asyncio.run(main())
    flat = [m for batch in written for m in batch]
    assert len(flat) == 3  # 队列满时丢弃了 m0，但循环存活并写完后续消息
    assert "m0" not in [m[3] for m in flat]
