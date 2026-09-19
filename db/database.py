import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db.models import Base
from utils.logs import get_logger

db_logger = get_logger("database")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/pg_limiter.db"
)

# Flag indicating database availability
DB_AVAILABLE: bool = True


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
        connect_args={"check_same_thread": False, "timeout": 30.0},
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA cache_size=-64000;")
        cursor.close()
else:
    db_logger.debug(f"📦 Using external database: {DATABASE_URL}")
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

# Session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

_PENDING_USER_INVALIDATIONS = "pending_user_cache_invalidations"
_USER_INVALIDATION_OUTCOMES = "user_cache_invalidation_outcomes"


def _current_session_transaction(session):
    return session.get_nested_transaction() or session.get_transaction()


@event.listens_for(AsyncSession.sync_session_class, "after_commit")
def _record_user_invalidation_commit(session) -> None:
    """Record which transaction level committed; resolution follows on end."""
    transaction = _current_session_transaction(session)
    if transaction is not None:
        session.info.setdefault(_USER_INVALIDATION_OUTCOMES, {})[transaction] = True


@event.listens_for(AsyncSession.sync_session_class, "after_rollback")
def _record_user_invalidation_rollback(session) -> None:
    """Record which transaction level rolled back; resolution follows on end."""
    transaction = _current_session_transaction(session)
    if transaction is not None:
        session.info.setdefault(_USER_INVALIDATION_OUTCOMES, {})[transaction] = False


@event.listens_for(AsyncSession.sync_session_class, "after_transaction_end")
def _resolve_user_cache_invalidations(session, transaction) -> None:
    """Merge committed savepoint effects or publish a committed root's effects."""
    pending_by_transaction = session.info.get(_PENDING_USER_INVALIDATIONS, {})
    outcomes = session.info.get(_USER_INVALIDATION_OUTCOMES, {})
    outcome = outcomes.pop(transaction, None)
    usernames = pending_by_transaction.pop(transaction, set())
    parent = transaction.parent

    if parent is not None and outcome is True and usernames:
        pending_by_transaction.setdefault(parent, set()).update(usernames)

    if parent is not None:
        return

    session.info.pop(_PENDING_USER_INVALIDATIONS, None)
    session.info.pop(_USER_INVALIDATION_OUTCOMES, None)
    if outcome is not True or not usernames:
        return

    from utils.user_sync import invalidate_user_metadata_cache

    for username in usernames:
        invalidate_user_metadata_cache(username)


_init_db_lock = asyncio.Lock()
_DB_INITIALIZED = False


def _sync_deprecated_legacy_tables_sync(sync_conn):
    """
    Ensure data consistency by migrating legacy records from deprecated tables
    (user_limits, except_users, disabled_users) into the consolidated `users` table
    if any exist.
    """
    from sqlalchemy import text, inspect
    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    
    if "users" not in existing_tables:
        return
        
    # 1. Migrate user_limits -> users.special_limit
    if "user_limits" in existing_tables:
        try:
            sync_conn.execute(text("""
                INSERT INTO users (username, special_limit, is_excepted, is_disabled_by_limiter, status)
                SELECT username, "limit", 0, 0, 'active' FROM user_limits WHERE username IS NOT NULL
                ON CONFLICT(username) DO UPDATE SET
                    special_limit = excluded.special_limit
                WHERE users.special_limit IS NULL;
            """))
        except Exception as e:
            db_logger.debug(f"Legacy user_limits sync note: {e}")

    # 2. Migrate except_users -> users.is_excepted
    if "except_users" in existing_tables:
        try:
            sync_conn.execute(text("""
                INSERT INTO users (username, is_excepted, exception_reason, excepted_by, is_disabled_by_limiter, status)
                SELECT username, 1, reason, created_by, 0, 'active' FROM except_users WHERE username IS NOT NULL
                ON CONFLICT(username) DO UPDATE SET
                    is_excepted = 1,
                    exception_reason = COALESCE(users.exception_reason, excluded.exception_reason),
                    excepted_by = COALESCE(users.excepted_by, excluded.excepted_by)
                WHERE users.is_excepted = 0 OR users.is_excepted IS NULL;
            """))
        except Exception as e:
            db_logger.debug(f"Legacy except_users sync note: {e}")

    # 3. Migrate disabled_users -> users.is_disabled_by_limiter
    if "disabled_users" in existing_tables:
        try:
            sync_conn.execute(text("""
                INSERT INTO users (username, is_disabled_by_limiter, disabled_at, enable_at, original_groups, disable_reason, punishment_step, status)
                SELECT username, 1, disabled_at, enable_at, original_groups, reason, punishment_step, 'active' FROM disabled_users WHERE username IS NOT NULL
                ON CONFLICT(username) DO UPDATE SET
                    is_disabled_by_limiter = 1,
                    disabled_at = COALESCE(users.disabled_at, excluded.disabled_at),
                    enable_at = COALESCE(users.enable_at, excluded.enable_at),
                    original_groups = COALESCE(users.original_groups, excluded.original_groups),
                    disable_reason = COALESCE(users.disable_reason, excluded.disable_reason),
                    punishment_step = COALESCE(users.punishment_step, excluded.punishment_step)
                WHERE users.is_disabled_by_limiter = 0 OR users.is_disabled_by_limiter IS NULL;
            """))
        except Exception as e:
            db_logger.debug(f"Legacy disabled_users sync note: {e}")


async def init_db():
    """
    Initialize the database - creates all tables and schema based on SQLAlchemy models.
    Thread-safe and async-safe: uses double-checked lock to prevent concurrent initialization.
    """
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return

    async with _init_db_lock:
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
            await conn.run_sync(_sync_deprecated_legacy_tables_sync)
        
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
    except BaseException as e:
        # BaseException, not Exception: a cancelled task must still roll back rather
        # than leave an open transaction. Shield the rollback so a second cancellation
        # cannot interrupt it mid-flight.
        try:
            await asyncio.shield(session.rollback())
        except Exception as rollback_error:
            db_logger.error(f"❌ Rollback failed during cleanup: {rollback_error}")
        if isinstance(e, asyncio.CancelledError):
            db_logger.debug("↩️ Database session cancelled (rolled back)")
        else:
            db_logger.error(f"❌ Database error (rolled back): {e}")
        raise
    finally:
        # Shielded so cancellation cannot leak a connection by skipping close.
        try:
            await asyncio.shield(session.close())
        except Exception as close_error:
            db_logger.debug(f"Session close note: {close_error}")
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
