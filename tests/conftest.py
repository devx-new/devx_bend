"""
Test configuration and shared fixtures.

Creates the SQLite in-memory tables before any test runs so that
endpoints that hit the DB don't fail with "no such table".
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import models  # noqa: F401 — ensures all models are imported for create_all
from app.database import Base, get_db
from app.main import app

# Use a fresh in-memory SQLite database for every test session
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def event_loop_policy():
    """Use the default asyncio policy — required by pytest-asyncio strict mode."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """Create all SQLAlchemy tables once per test session."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(autouse=True)
async def override_db():
    """Override the get_db dependency to use the test in-memory DB."""
    async def _get_test_db():
        async with _session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)
