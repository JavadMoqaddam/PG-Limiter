"""
PG-Limiter Database Package
SQLite database for storing users, limits, ISP cache, and violation history.
"""

from db.database import (
    init_db,
    get_db,
    get_db_session,
    close_db,
    AsyncSessionLocal,
    engine,
    DB_AVAILABLE,
)

from db.models import (
    Base,
    User,
    UserLimit,
    ExceptUser,
    DisabledUser,
    SubnetISP,
    ViolationHistory,
    Config,
    IPHistory,
)

from db.crud import (
    UserCRUD,
    UserLimitCRUD,
    ExceptUserCRUD,
    DisabledUserCRUD,
    SubnetISPCRUD,
    ViolationHistoryCRUD,
    ConfigCRUD,
    IPHistoryCRUD,
)

__all__ = [
    # Database
    "init_db",
    "get_db",
    "get_db_session",
    "close_db",
    "AsyncSessionLocal",
    "engine",
    "DB_AVAILABLE",
    # Models
    "Base",
    "User",
    "UserLimit",
    "ExceptUser",
    "DisabledUser",
    "SubnetISP",
    "ViolationHistory",
    "Config",
    "IPHistory",
    # CRUD
    "UserCRUD",
    "UserLimitCRUD",
    "ExceptUserCRUD",
    "DisabledUserCRUD",
    "SubnetISPCRUD",
    "ViolationHistoryCRUD",
    "ConfigCRUD",
    "IPHistoryCRUD",
]
