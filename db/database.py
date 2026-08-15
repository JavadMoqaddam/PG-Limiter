"""
Database Connection and Session Management
Uses async SQLAlchemy with aiosqlite for SQLite.
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db.models import Base
from utils.logs import get_logger

db_logger = get_logger("database")

# Database URL - defaults to SQLite in data directory
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/pg_limiter.db"
)


def _get_db_path() -> str:
    """Get the SQLite database file path."""
    db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    if db_path.startswith("./"):
        db_path = db_path[2:]
    return db_path


# For SQLite, use standard async engine without StaticPool to allow concurrent connections with WAL mode
if DATABASE_URL.startswith("sqlite"):
    db_logger.debug(f"📦 Using SQLite database: {DATABASE_URL}")
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 60.0},
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=60000;")
        cursor.execute("PRAGMA cache_size=-64000;")
        cursor.close()
else:
    db_logger.debug(f"📦 Using external database: {DATABASE_URL}")
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

# Session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


_DB_INITIALIZED = False


async def init_db():
    """
    Initialize the database - creates all tables and schema based on SQLAlchemy models.
    Should be called once at application startup.
    """
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return

    # Ensure data directory exists
    db_path = _get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        db_logger.info(f"📁 Created database directory: {db_dir}")
    
    db_logger.debug("🔄 Initializing database schema...")
    
    # Create all tables and indexes via SQLAlchemy declarative metadata
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    _DB_INITIALIZED = True
    db_logger.info(f"✅ Database initialized: {DATABASE_URL}")


async def close_db():
    """Close database connections."""
    db_logger.debug("🔄 Closing database connections...")
    await engine.dispose()
    db_logger.info("✅ Database connections closed")


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for database sessions.
    
    Usage:
        async with get_db() as db:
            result = await db.execute(select(User))
            users = result.scalars().all()
    """
    db_logger.debug("📂 Opening database session")
    session = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
        db_logger.debug("✅ Database session committed")
    except Exception as e:
        await session.rollback()
        db_logger.error(f"❌ Database error (rolled back): {e}")
        raise
    finally:
        await session.close()
        db_logger.debug("📁 Database session closed")


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async generator for FastAPI dependency injection.
    
    Usage:
        @app.get("/users")
        async def get_users(db: AsyncSession = Depends(get_db_session)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            raise
