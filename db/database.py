"""
Database Connection and Session Management
Uses async SQLAlchemy with aiosqlite for SQLite.
"""

import asyncio
import os
import sqlite3
import warnings
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


def _ensure_db_columns():
    """
    Ensure all required tables and columns exist in the database.
    Runs inside a thread executor during init_db().
    """
    db_path = _get_db_path()
    
    # Ensure data directory exists
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    try:
        # 1. Create tables if they do not exist
        from sqlalchemy import create_engine
        from db.models import Base
        sync_engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        # 2. Add any missing columns to existing SQLite tables
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        if not cursor.fetchone():
            conn.close()
            return
        
        cursor.execute("PRAGMA table_info(users)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        
        columns_to_add = [
            ("is_excepted", "BOOLEAN DEFAULT 0"),
            ("exception_reason", "TEXT"),
            ("excepted_by", "VARCHAR(255)"),
            ("excepted_at", "DATETIME"),
            ("special_limit", "INTEGER"),
            ("special_limit_updated_at", "DATETIME"),
            ("is_disabled_by_limiter", "BOOLEAN DEFAULT 0"),
            ("disabled_at", "FLOAT"),
            ("enable_at", "FLOAT"),
            ("original_groups", "JSON"),
            ("disable_reason", "TEXT"),
            ("punishment_step", "INTEGER DEFAULT 0"),
            ("is_monitored", "BOOLEAN DEFAULT 1"),
            ("effective_ip_limit", "INTEGER"),
        ]
        
        added = []
        for col_name, col_type in columns_to_add:
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                    added.append(col_name)
                except sqlite3.OperationalError:
                    pass
        
        if added:
            db_logger.info(f"📌 Added missing columns to users table: {', '.join(added)}")
        
        indexes_to_create = [
            ("ix_users_owner_id", "CREATE INDEX IF NOT EXISTS ix_users_owner_id ON users (owner_id)"),
            ("ix_users_owner_username", "CREATE INDEX IF NOT EXISTS ix_users_owner_username ON users (owner_username)"),
            ("ix_users_status", "CREATE INDEX IF NOT EXISTS ix_users_status ON users (status)"),
            ("ix_users_is_excepted", "CREATE INDEX IF NOT EXISTS ix_users_is_excepted ON users (is_excepted)"),
            ("ix_users_special_limit", "CREATE INDEX IF NOT EXISTS ix_users_special_limit ON users (special_limit)"),
            ("ix_users_is_disabled_by_limiter", "CREATE INDEX IF NOT EXISTS ix_users_is_disabled_by_limiter ON users (is_disabled_by_limiter)"),
            ("ix_users_status_disabled", "CREATE INDEX IF NOT EXISTS ix_users_status_disabled ON users (status, is_disabled_by_limiter)"),
        ]
        for idx_name, stmt in indexes_to_create:
            try:
                cursor.execute(stmt)
            except Exception:
                pass
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        db_logger.warning(f"Column check failed: {e}")


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
    Initialize the database - run schema checks and table creation.
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
    
    db_logger.debug("🔄 Running database schema checks...")
    
    # Run column checks and table creation in background thread
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _ensure_db_columns)
    
    _DB_INITIALIZED = True
    db_logger.info(f"✅ Database initialized: {DATABASE_URL}")


def _run_migrations_sync():
    """
    Run Alembic migrations synchronously.
    Columns are already ensured at module load time by _ensure_db_columns().
    Note: We suppress coroutine warnings since migrations are also handled by start.sh
    """
    from alembic.config import Config
    from alembic import command
    
    db_path = _get_db_path()
    alembic_cfg = Config("alembic.ini")
    
    # Suppress coroutine warnings during migration (handled by start.sh)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="coroutine.*was never awaited")
        try:
            if not os.path.exists(db_path):
                # Fresh database - create with migrations
                db_logger.info("🔄 Creating new database with migrations...")
                command.upgrade(alembic_cfg, "head")
            else:
                # Existing database - try to upgrade, handle errors gracefully
                try:
                    command.upgrade(alembic_cfg, "head")
                except Exception as e:
                    error_msg = str(e).lower()
                    if "already exists" in error_msg or "duplicate" in error_msg:
                        # Tables/columns already exist, stamp as current
                        try:
                            command.stamp(alembic_cfg, "head")
                        except Exception:
                            pass
                    else:
                        db_logger.debug(f"Migration note: {e}")
        except Exception as e:
            db_logger.debug(f"Migration handling: {e}")


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
