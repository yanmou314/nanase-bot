"""chat_stats 存储层：PostgreSQL（psycopg3 + 连接池）。"""
import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_driver
from psycopg_pool import AsyncConnectionPool

CFG_FILE = os.path.join(os.path.dirname(__file__), "db.json")
_pool: AsyncConnectionPool | None = None
_pool_lock = asyncio.Lock()
_writer_task: asyncio.Task | None = None
_writer_lock = asyncio.Lock()
_write_queue: asyncio.Queue | None = None
_closed = False  # 关停后拒绝新建队列/连接池，避免迟到的写入把它们重建出来
_BATCH_SIZE = 100
_FLUSH_INTERVAL = 0.5
_QUEUE_MAX_SIZE = 5000
_logger = logging.getLogger(__name__)
_SH = ZoneInfo("Asia/Shanghai")


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
    if _closed:
        raise RuntimeError("数据库连接池已关闭（进程正在退出）")
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                # 先在局部变量上完成 open + 建表，全部成功后才发布到 _pool；
                # 中途任何失败都关闭半初始化的池并抛出，让下次调用能重试，
                # 避免残留坏池导致后续查询每次都挂到 PoolTimeout。
                pool = AsyncConnectionPool(load_dsn(), min_size=1, max_size=5, open=False)
                try:
                    await pool.open()
                    async with pool.connection() as conn:
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
                        await conn.execute(
                            """CREATE TABLE IF NOT EXISTS command_usages (
                                id BIGSERIAL PRIMARY KEY,
                                group_id BIGINT NOT NULL,
                                user_id BIGINT NOT NULL,
                                day DATE NOT NULL,
                                hour INT NOT NULL,
                                command TEXT NOT NULL
                            )"""
                        )
                        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cmd_day ON command_usages(day)")
                        await conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_cmd_gday ON command_usages(group_id, day)"
                        )
                        await conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_cmd_uday ON command_usages(user_id, day)"
                        )
                        await conn.commit()
                except Exception:
                    try:
                        await pool.close()  # best-effort 清理，失败只记日志
                    except Exception:
                        _logger.exception("关闭初始化失败的连接池时出错（忽略）")
                    raise
                _pool = pool
    return _pool


@get_driver().on_shutdown
async def close_pool() -> None:
    global _pool, _writer_task, _write_queue, _closed
    _closed = True
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
    """执行查询并一次性返回全部行；异常直接向上抛，由调用方决定如何提示/记录。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def iter_rows(sql: str, params: tuple = ()) -> AsyncIterator[tuple]:
    """流式查询：服务端命名游标逐行产出，避免大结果集一次性载入内存。

    命名游标必须在事务内运行（池连接默认非 autocommit，execute 即开事务）；
    正常耗尽后由池的 connection() 上下文统一 commit、异常时 rollback，
    连接总能干净归还。调用方应完整消费，或用 contextlib.aclosing 包住以防提前退出。
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(name=f"iter_{uuid.uuid4().hex[:8]}") as cur:
            await cur.execute(sql, params)
            async for row in cur:
                yield row


async def wait_writes_drained(timeout: float = 30.0) -> None:
    """等待消息写队列清空（带超时兜底）；供日报任务在统计前等 0.5s flush 窗口的消息落库。"""
    queue = _write_queue
    if queue is None:
        return
    try:
        await asyncio.wait_for(queue.join(), timeout)
    except asyncio.TimeoutError:
        _logger.warning("等待消息写队列清空超时（%.0f 秒），继续执行", timeout)


async def _ensure_writer() -> asyncio.Queue:
    global _writer_task, _write_queue
    if _write_queue is None:
        async with _writer_lock:
            if _write_queue is None:
                _write_queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
                _writer_task = asyncio.create_task(_write_loop())
    return _write_queue


async def _write_batch(batch: list[tuple[int, int, str, str, str, int]]) -> None:
    pool = await get_pool()
    # day/hour 已在消息接收时（write 调用方）按上海时区算好，重试积压不会把消息记错日期桶
    params = [
        (group_id, user_id, msg_type, day, hour, text[:200] or "")
        for group_id, user_id, msg_type, text, day, hour in batch
    ]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO messages(group_id, user_id, msg_type, day, hour, text) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                params,
            )
        await conn.commit()


async def _write_loop() -> None:
    while True:
        try:
            queue = _write_queue
            if queue is None:
                await asyncio.sleep(1)
                continue
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
                # get() 已取出的任务必须先结算，再重新入队；否则 unfinished_tasks 会不断累加，
                # 数据库短暂异常后 shutdown 时 queue.join() 可能永远无法完成。
                for _ in batch:
                    queue.task_done()
                # 写循环是队列唯一的消费者：await put() 在队列满时会把自己永久挂死，
                # 只能用非阻塞回插，插不回去的只能丢弃。
                for item in batch:
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        _logger.warning("消息队列已满，重试消息被丢弃 (group=%s user=%s)", item[0], item[1])
                await asyncio.sleep(1)
            else:
                for _ in batch:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("消息写入循环异常，1 秒后重启")
            await asyncio.sleep(1)


async def write(group_id: int, user_id: int, msg_type: str, text: str) -> None:
    if _closed:
        return
    queue = await _ensure_writer()
    now = datetime.now(_SH)
    item = (group_id, user_id, msg_type, text, now.date().isoformat(), now.hour)
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        _logger.warning("消息队列已满，丢弃该条消息 (group=%s user=%s)", group_id, user_id)


async def write_command(group_id: int, user_id: int, command: str) -> None:
    """记录一个已经通过 NoneBot 命令匹配并执行的群指令。"""
    if _closed:
        return
    pool = await get_pool()
    now = datetime.now(_SH)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO command_usages(group_id, user_id, day, hour, command) "
            "VALUES(%s, %s, %s, %s, %s)",
            (group_id, user_id, now.date().isoformat(), now.hour, command[:100]),
        )
        await conn.commit()
