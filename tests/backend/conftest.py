"""Shared fixtures for backend tests.

Provides an in-memory SQLite engine and a session-scoped database session
that rolls back after each test to ensure test isolation.
"""
import pytest
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import sessionmaker, Session
from typing import Generator

from app.backend.database.connection import Base


@pytest.fixture(autouse=True)
def reset_service_caches() -> Generator[None, None, None]:
    """Empty the module-level service caches around every test.

    These caches are keyed by ticker, so two tests that both stub a ticker named
    "TEST" would otherwise share one entry and the second would assert against
    the first one's fixture data.
    """
    def _clear() -> None:
        from app.backend.services import pricing_service
        pricing_service._period_cache.clear()
        pricing_service._snapshot_cache.clear()
        pricing_service._inflight_locks.clear()
        pricing_service._yfinance_cooldown_until = 0.0

    _clear()
    yield
    _clear()


@pytest.fixture(scope="session")
def db_engine() -> Generator[Engine, None, None]:
    """Create an in-memory SQLite engine with all tables created."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """Yield a database session that rolls back after each test."""
    connection = db_engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection)
    session = session_factory()

    yield session

    session.close()
    transaction.rollback()
    connection.close()
