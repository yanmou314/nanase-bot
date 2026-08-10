"""chat_stats 存储层：PostgreSQL（psycopg3 + 连接池）。"""
import json
import os

from psycopg_pool import AsyncConnectionPool

CFG_FILE = os.path.join(os.path.dirname(__file__), "db.json")
_pool: AsyncConnectionPool | None = None


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
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_gday ON messages(group_id, day)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_guday ON messages(group_id, user_id, day)")
            await conn.commit()
    return _pool


async def exec(sql: str, params: tuple = ()) -> list:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            try:
                return await cur.fetchall()
            except Exception:
                return []


async def write(group_id: int, user_id: int, msg_type: str, text: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO messages(group_id, user_id, msg_type, day, hour, text) VALUES(%s,%s,%s,CURRENT_DATE,EXTRACT(HOUR FROM NOW())::int,%s)",
            (group_id, user_id, msg_type, text[:200] or ""),
        )
        await conn.commit()
