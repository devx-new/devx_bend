import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Coroutine, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

T = TypeVar("T")


def run_in_celery(coro: Coroutine[Any, Any, T]) -> T:
    """
    Execute an async coroutine inside a Celery worker process.

    Plain asyncio.run() creates and then closes the event loop immediately
    after the coroutine finishes. asyncpg tries to cancel its pool connections
    after the loop closes, producing a flood of 'Event loop is closed' errors.

    This helper disposes the engine pool *before* closing the loop so asyncpg
    can shut down gracefully.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(engine.dispose())
        except Exception:
            pass
        # Cancel and drain any lingering tasks (e.g. httpx AsyncClient cleanup)
        # before closing the loop to avoid 'Event loop is closed' RuntimeErrors.
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
