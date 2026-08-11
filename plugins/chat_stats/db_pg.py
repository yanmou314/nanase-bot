"""chat_stats 存储层：PostgreSQL（psycopg3 + 连接池）。"""
import asyncio
import json
import logging
import os

from nonebot import get_driver
from psycopg_pool import AsyncConnectionPool

CFG_FILE = os.path.join(os.path.dirname(__file__), "db.json")
_pool: AsyncConnectionPool | None = None
_pool_lock = asyncio.Lock()
_writer_task: asyncio.Task | None = None
_writer_lock = asyncio.Lock()
_write_queue: asyncio.Queue | None = None
_BATCH_SIZE = 100
_FLUSH_INTERVAL = 0.5
_QUEUE_MAX_SIZE = 5000
_logger = logging.getLogger(__name__)


def load_dsn() -> str:
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        dsn = cfg["dsn"]
        if "postgresql" not in dsn:
            raise ValueError("invalid dsn")
        return dsn
    except Exception:
        raise RuntimeError("数据库配置缺失：请创建 plugins/chat_stats/db.json（含 dsn 字段）")


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = AsyncConnectionPool(load_dsn(), min_size=1, max_size=5, open=False)
                await _pool.open()
                async with _pool.connection() as conn:
                    await conn.execute(
                        """CREATE TABLE IF NOT EXISTS messages (
                            id BIGSERIAL PRIMARY KEY,
                            group_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            msg_type TEXT NOT NULL DEFAULT 'text',
                            day DATE NOT NULL,
                            hour INT NOT NULL,
                            text TEXT NOT NULL DEFAULT ''
                        )"""
                    )
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_day ON messages(day)")
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_gday ON messages(group_id, day)")
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_guday ON messages(group_id, user_id, day)")
                    await conn.commit()
    return _pool


@get_driver().on_shutdown
async def close_pool() -> None:
    global _pool, _writer_task, _write_queue
    if _writer_task is not None and _write_queue is not None:
        try:
            await asyncio.wait_for(_write_queue.join(), timeout=10)
        except asyncio.TimeoutError:
            _logger.warning("消息队列在 10 秒内未刷完，关闭时保留未提交消息")
        finally:
            _writer_task.cancel()
            try:
                await _writer_task
            except asyncio.CancelledError:
                pass
        _writer_task = None
        _write_queue = None
    if _pool is not None:
        await _pool.close()
        _pool = None


async def exec(sql: str, params: tuple = ()) -> list:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            try:
                return await cur.fetchall()
            except Exception:
                return []


async def _ensure_writer() -> asyncio.Queue:
    global _writer_task, _write_queue
    if _write_queue is None:
        async with _writer_lock:
            if _write_queue is None:
                _write_queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
                _writer_task = asyncio.create_task(_write_loop())
    return _write_queue


async def _write_batch(batch: list[tuple[int, int, str, str]]) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO messages(group_id, user_id, msg_type, day, hour, text) "
                "VALUES(%s,%s,%s,CURRENT_DATE,EXTRACT(HOUR FROM NOW())::int,%s)",
                [(group_id, user_id, msg_type, text[:200] or "") for group_id, user_id, msg_type, text in batch],
            )
        await conn.commit()


async def _write_loop() -> None:
    queue = _write_queue
    if queue is None:
        return
    while True:
        first = await queue.get()
        batch = [first]
        deadline = asyncio.get_running_loop().time() + _FLUSH_INTERVAL
        while len(batch) < _BATCH_SIZE:
            timeout = deadline - asyncio.get_running_loop().time()
            if timeout <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout))
            except asyncio.TimeoutError:
                break
        try:
            await _write_batch(batch)
        except Exception:
            _logger.exception("批量写入消息失败，将在稍后重试")
            for _ in batch:
                queue.task_done()
            for item in batch:
                await queue.put(item)
            await asyncio.sleep(1)
        else:
            for _ in batch:
                queue.task_done()


async def write(group_id: int, user_id: int, msg_type: str, text: str) -> None:
    queue = await _ensure_writer()
    await queue.put((group_id, user_id, msg_type, text))
