"""Async session factory bound to runtime settings.

The engine is lazy (no connection at import time). ``get_session`` is the
FastAPI dependency endpoints use to obtain a session; it commits at the
request edge on a clean return, and rolls back on close if the endpoint
raised — so a failed request leaves no partial writes.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.database_url)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
        await session.commit()
