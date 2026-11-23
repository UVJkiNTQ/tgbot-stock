"""Reusable single-connection manager for the local SQLite database.

Reusing one ``aiosqlite.Connection`` avoids spawning and tearing down a
background worker thread on every query and keeps SQLite's page cache bounded.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

import config

_conn: aiosqlite.Connection | None = None
_conn_path: str | None = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    global _lock, _lock_loop
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


async def close_db() -> None:
    """Close the cached SQLite connection, if open."""
    global _conn, _conn_path
    conn = _conn
    _conn = None
    _conn_path = None
    if conn is not None:
        await conn.close()


@asynccontextmanager
async def get_db(
    *, row_factory: Any = None
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield the shared SQLite connection serialized by an asyncio lock."""
    global _conn, _conn_path
    loop = asyncio.get_running_loop()
    lock = _get_lock(loop)
    async with lock:
        db_path = config.DB_PATH
        if _conn is not None and _conn_path != db_path:
            old_conn = _conn
            _conn = None
            _conn_path = None
            await old_conn.close()

        if _conn is None:
            raw_conn = aiosqlite.connect(db_path)
            thread = getattr(raw_conn, "_thread", None)
            if thread is not None and not thread.is_alive():
                thread.daemon = True
            conn = await raw_conn
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute("PRAGMA cache_size = -1024")
            _conn = conn
            _conn_path = db_path

        _conn.row_factory = row_factory
        try:
            yield _conn
        except Exception:
            if _conn.in_transaction:
                await _conn.rollback()
            raise
        finally:
            if _conn is not None and _conn.in_transaction:
                await _conn.rollback()
            if _conn is not None:
                _conn.row_factory = None
