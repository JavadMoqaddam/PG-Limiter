"""
REST API Server for Limiter
Run with: python api_server.py
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional

import secrets

from fastapi import FastAPI, HTTPException, Depends, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from utils.atomic_io import atomic_write_json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config files
CONFIG_FILE = "config.json"
BACKUP_FILE = "backup.json"

_config_lock = asyncio.Lock()
_backup_lock = asyncio.Lock()

# Security
security = HTTPBasic()

# Failed-auth throttle: too many wrong credentials from one client within the window
# are refused with 429 instead of letting an unthrottled 0.0.0.0 bind be brute-forced.
_AUTH_MAX_FAILURES = 5
_AUTH_WINDOW_SECONDS = 60.0
_failed_auth_attempts: Dict[str, List[float]] = {}


def _record_auth_failure(client: str) -> int:
    """Record a failed auth for a client and return its live failure count in the window."""
    now = time.time()
    recent = [t for t in _failed_auth_attempts.get(client, []) if now - t < _AUTH_WINDOW_SECONDS]
    recent.append(now)
    _failed_auth_attempts[client] = recent
    return len(recent)

# ═══════════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════════

class UserLimit(BaseModel):
    username: str
    limit: int

class UpdateLimit(BaseModel):
    limit: int

class ExceptUser(BaseModel):
    username: str

class DisabledUser(BaseModel):
    username: str
    disabled_at: float
    elapsed_seconds: int

class StatusResponse(BaseModel):
    general_limit: int
    check_interval: int
    reenable_time: int
    special_limits_count: int
    except_users_count: int
    disabled_users_count: int

class ConfigResponse(BaseModel):
    limits: Dict
    timing: Dict
    users: Dict
    settings: Dict

# ═══════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════

def load_config() -> dict:
    """Load config from file or environment defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    
    from utils.read_config import get_config_sync
    return get_config_sync()

def save_config(config: dict):
    """Save config to file"""
    atomic_write_json(CONFIG_FILE, config)

def load_backup() -> dict:
    """Load backup file"""
    if not os.path.exists(BACKUP_FILE):
        return {"special": {}, "except_users": []}
    with open(BACKUP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_backup(backup: dict):
    """Save backup file"""
    atomic_write_json(BACKUP_FILE, backup)

async def load_disabled_users() -> dict:
    """
    ``{username: disabled_at}`` straight from the limiter's database.

    This used to parse the JSON registry, and its writer wrote back only
    ``{"disabled_users": ...}`` - dropping the ``enable_at`` map, which turned
    every other user's permanent or timed ban into a default-window one. The
    ``users`` table is the single store now, so that class of accident is gone.
    """
    from utils import handel_dis_users as dis_registry

    return await dis_registry.disabled_at_map()


async def get_panel_data():
    """Build panel connection data from the canonical configuration."""
    from utils.read_config import read_config
    from utils.types import PanelType

    config = await read_config()
    panel_config = config.get("panel", {})
    if not panel_config.get("domain"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Panel not configured",
        )
    return PanelType(
        panel_username=panel_config.get("username", ""),
        panel_password=panel_config.get("password", ""),
        panel_domain=panel_config["domain"],
    )


def verify_credentials(request: Request = None, credentials: HTTPBasicCredentials = Depends(security)):
    """
    HTTP Basic check that fails closed.

    An unset password used to authenticate *everyone*. The "no password configured"
    branch only logged a warning and carried on, so ``compare_digest(b"", b"")``
    returned True and the default username ``admin`` with an empty password opened
    every route - including the ones that enable users and rewrite config - on a
    server whose default bind address is 0.0.0.0 with no rate limiting. Note that
    ``os.getenv(...) or ...`` also treats an empty environment variable as unset,
    so exporting ``API_PASSWORD=`` reached the same place.

    A missing password now disables the API instead of opening it.
    """
    config = load_config()
    api_config = config.get("api", {})

    correct_username = os.getenv("API_USERNAME") or api_config.get("username", "admin")
    correct_password = os.getenv("API_PASSWORD") or api_config.get("password", "")

    if not correct_password:
        logger.error(
            "⛔ API request refused: no API password is configured. Set API_PASSWORD in "
            "the environment or api.password in the config. Refusing is deliberate - an "
            "empty password authenticates every caller."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API password is not configured; the API is disabled",
        )

    is_correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        correct_username.encode("utf8")
    )
    is_correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        correct_password.encode("utf8")
    )

    if not (is_correct_username and is_correct_password):
        client = request.client.host if request and request.client else "unknown"
        if _record_auth_failure(client) > _AUTH_MAX_FAILURES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed authentication attempts; try again later",
                headers={"Retry-After": str(int(_AUTH_WINDOW_SECONDS))},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    # A clean login clears the client's failure streak.
    _failed_auth_attempts.pop(request.client.host if request and request.client else "unknown", None)
    return credentials.username


# ═══════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("🚀 Limiter API Server starting...")
    # Own the database lifecycle: a bare `python api_server.py` used to serve requests
    # against an uninitialised database and never close it. Management writes go
    # through SQLite services, so the schema must be ready before the first request.
    from db.database import init_db, close_db
    await init_db()
    try:
        yield
    finally:
        await close_db()
        logger.info("🛑 Limiter API Server shutting down...")


def _docs_enabled() -> bool:
    """
    Whether to publish /docs, /redoc and /openapi.json.

    FastAPI serves all three without authentication, so on a 0.0.0.0 bind they
    handed an unauthenticated caller the full route list and every request schema.
    Off unless the operator asks for them.
    """
    return os.getenv("API_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}


def _cors_origins() -> list[str]:
    """
    Explicit cross-origin allow-list, empty by default.

    This was ``allow_origins=["*"]`` together with ``allow_credentials=True``.
    Starlette cannot answer a credentialed request with a literal ``*``, so it
    echoes back whichever Origin asked - which is every origin, with credentials.
    Same-origin callers and non-browser clients (curl, scripts, the CLI) are
    unaffected by an empty list.
    """
    raw = os.getenv("API_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _resolve_bind_host() -> str:
    """The address to bind, defaulting to loopback.

    The old default was 0.0.0.0, which exposed the management API to the network the
    moment it started. Remote exposure is now opt-in: set API_HOST (or api.host).
    """
    env_host = os.getenv("API_HOST")
    if env_host:
        return env_host
    try:
        return load_config().get("api", {}).get("host") or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


_DOCS_ON = _docs_enabled()

app = FastAPI(
    title="Limiter API",
    description="REST API for IP Connection Limiter Management",
    version="1.5.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)

_CORS_ORIGINS = _cors_origins()
if _CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ═══════════════════════════════════════════════════════════════
# Routes - Status
# ═══════════════════════════════════════════════════════════════

@app.get("/", tags=["Status"])
async def root():
    """API root - health check"""
    return {"status": "ok", "message": "Limiter API is running"}


@app.get("/status", response_model=StatusResponse, tags=["Status"])
async def get_status(username: str = Depends(verify_credentials)):
    """Get current limiter status"""
    async with _config_lock:
        config = await asyncio.to_thread(load_config)
    disabled = await load_disabled_users()
    
    limits = config.get("limits", {})
    timing = config.get("timing", {})
    users = config.get("users", {})
    
    return StatusResponse(
        general_limit=limits.get("general", 2),
        check_interval=timing.get("check_interval", 120),
        reenable_time=timing.get("time_to_active_users", 300),
        special_limits_count=len(limits.get("special", {})),
        except_users_count=len(users.get("except", [])),
        disabled_users_count=len(disabled)
    )


# ═══════════════════════════════════════════════════════════════
# Routes - User Limits
# ═══════════════════════════════════════════════════════════════

@app.get("/users/limits", tags=["User Limits"])
async def list_user_limits(username: str = Depends(verify_credentials)):
    """List all users with special limits"""
    async with _config_lock:
        config = await asyncio.to_thread(load_config)
    async with _backup_lock:
        backup = await asyncio.to_thread(load_backup)
    
    special_limits = {}
    if "limits" in config and "special" in config["limits"]:
        special_limits.update(config["limits"]["special"])
    if "special" in backup:
        special_limits.update(backup["special"])
    
    return {
        "success": True,
        "data": [
            {"username": k, "limit": v}
            for k, v in sorted(special_limits.items())
        ]
    }


@app.get("/users/limits/{user}", tags=["User Limits"])
async def get_user_limit(user: str, username: str = Depends(verify_credentials)):
    """Get a specific user's limit"""
    async with _config_lock:
        config = await asyncio.to_thread(load_config)
    async with _backup_lock:
        backup = await asyncio.to_thread(load_backup)
    
    limit = None
    if "limits" in config and "special" in config["limits"]:
        limit = config["limits"]["special"].get(user)
    if limit is None and "special" in backup:
        limit = backup["special"].get(user)
    
    general = config.get("limits", {}).get("general", 2)
    
    return {
        "success": True,
        "data": {
            "username": user,
            "limit": limit if limit is not None else general,
            "is_special": limit is not None,
            "general_limit": general
        }
    }


@app.post("/users/limits", tags=["User Limits"])
async def add_user_limit(user_limit: UserLimit, username: str = Depends(verify_credentials)):
    """Add or update a user's special limit"""
    async with _config_lock:
        async with _backup_lock:
            config = await asyncio.to_thread(load_config)
            backup = await asyncio.to_thread(load_backup)
            
            if "limits" not in config:
                config["limits"] = {}
            if "special" not in config["limits"]:
                config["limits"]["special"] = {}
            if "special" not in backup:
                backup["special"] = {}
            
            config["limits"]["special"][user_limit.username] = user_limit.limit
            backup["special"][user_limit.username] = user_limit.limit
            
            await asyncio.to_thread(save_config, config)
            await asyncio.to_thread(save_backup, backup)
    
    return {"success": True, "message": f"Limit for {user_limit.username} set to {user_limit.limit}"}


@app.put("/users/limits/{user}", tags=["User Limits"])
async def update_user_limit(user: str, update: UpdateLimit, username: str = Depends(verify_credentials)):
    """Update a user's special limit"""
    async with _config_lock:
        async with _backup_lock:
            config = await asyncio.to_thread(load_config)
            backup = await asyncio.to_thread(load_backup)
            
            if "limits" not in config:
                config["limits"] = {}
            if "special" not in config["limits"]:
                config["limits"]["special"] = {}
            if "special" not in backup:
                backup["special"] = {}
            
            config["limits"]["special"][user] = update.limit
            backup["special"][user] = update.limit
            
            await asyncio.to_thread(save_config, config)
            await asyncio.to_thread(save_backup, backup)
    
    return {"success": True, "message": f"Limit for {user} updated to {update.limit}"}


@app.delete("/users/limits/{user}", tags=["User Limits"])
async def delete_user_limit(user: str, username: str = Depends(verify_credentials)):
    """Delete a user's special limit"""
    removed = False
    
    async with _config_lock:
        async with _backup_lock:
            config = await asyncio.to_thread(load_config)
            backup = await asyncio.to_thread(load_backup)
            
            if "limits" in config and "special" in config["limits"]:
                if user in config["limits"]["special"]:
                    del config["limits"]["special"][user]
                    await asyncio.to_thread(save_config, config)
                    removed = True
            
            if "special" in backup and user in backup["special"]:
                del backup["special"][user]
                await asyncio.to_thread(save_backup, backup)
                removed = True
    
    if not removed:
        raise HTTPException(status_code=404, detail=f"User {user} not found in special limits")
    
    return {"success": True, "message": f"Special limit for {user} removed"}


# ═══════════════════════════════════════════════════════════════
# Routes - Except Users
# ═══════════════════════════════════════════════════════════════

@app.get("/users/except", tags=["Except Users"])
async def list_except_users(username: str = Depends(verify_credentials)):
    """List whitelisted users from the canonical store enforcement reads."""
    from utils.read_config import read_config

    config = await read_config()
    except_users = config.get("except_users", []) or []

    return {
        "success": True,
        "data": sorted(set(except_users))
    }


@app.post("/users/except", tags=["Except Users"])
async def add_except_user(user: ExceptUser, username: str = Depends(verify_credentials)):
    """Whitelist a user through the canonical SQLite store enforcement reads."""
    from db.database import get_db
    from db.crud import UserCRUD

    async with get_db() as db:
        await UserCRUD.set_excepted(db, user.username, excepted=True, excepted_by="api")
        await db.commit()

    return {"success": True, "message": f"User {user.username} added to except list"}


@app.delete("/users/except/{user}", tags=["Except Users"])
async def delete_except_user(user: str, username: str = Depends(verify_credentials)):
    """Remove a user from the whitelist in the canonical SQLite store."""
    from utils.read_config import read_config
    from db.database import get_db
    from db.crud import UserCRUD

    config = await read_config()
    if user not in (config.get("except_users", []) or []):
        raise HTTPException(status_code=404, detail=f"User {user} not found in except list")

    async with get_db() as db:
        await UserCRUD.set_excepted(db, user, excepted=False, excepted_by="api")
        await db.commit()

    return {"success": True, "message": f"User {user} removed from except list"}


# ═══════════════════════════════════════════════════════════════
# Routes - Disabled Users
# ═══════════════════════════════════════════════════════════════

@app.get("/users/disabled", tags=["Disabled Users"])
async def list_disabled_users_route(username: str = Depends(verify_credentials)):
    """List all currently disabled users"""
    disabled = await load_disabled_users()
    current_time = time.time()
    
    data = []
    for user, disabled_time in disabled.items():
        elapsed = int(current_time - disabled_time)
        data.append({
            "username": user,
            "disabled_at": disabled_time,
            "disabled_at_formatted": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(disabled_time)),
            "elapsed_seconds": elapsed
        })
    
    return {
        "success": True,
        "data": sorted(data, key=lambda x: x["disabled_at"], reverse=True)
    }


@app.delete("/users/disabled/{user}", tags=["Disabled Users"])
async def enable_disabled_user(user: str, username: str = Depends(verify_credentials)):
    """Enable one user on the panel before clearing their recovery record."""
    from utils import handel_dis_users as dis_registry
    from utils.panel_api import enable_selected_users

    if not await dis_registry.is_disabled(user):
        raise HTTPException(status_code=404, detail=f"User {user} is not in disabled list")

    result = await enable_selected_users(await get_panel_data(), {user})
    if user in result.get("failed", []):
        raise HTTPException(status_code=502, detail="Panel enable failed; record preserved")
    if user not in result.get("enabled", []) and user not in result.get("not_found", []):
        raise HTTPException(status_code=502, detail="Panel returned no result; record preserved")
    if not await dis_registry.enable(user):
        raise HTTPException(status_code=500, detail=f"Panel updated; could not clear the disable record for {user}")

    return {"success": True, "message": f"User {user} enabled and removed from disabled list"}


@app.delete("/users/disabled", tags=["Disabled Users"])
async def enable_all_disabled_users(username: str = Depends(verify_credentials)):
    """Enable every tracked user, retaining records for panel failures."""
    from utils import handel_dis_users as dis_registry
    from utils.panel_api import enable_selected_users

    disabled_users = await dis_registry.disabled_usernames()
    if not disabled_users:
        return {"success": True, "message": "Cleared 0 users from disabled list"}

    result = await enable_selected_users(await get_panel_data(), disabled_users)
    confirmed = set(result.get("enabled", [])) | set(result.get("not_found", []))
    cleanup_failed = []
    for user in confirmed:
        if not await dis_registry.enable(user):
            cleanup_failed.append(user)

    panel_failed = sorted(set(result.get("failed", [])) | (disabled_users - confirmed - set(result.get("failed", []))))
    if cleanup_failed:
        raise HTTPException(
            status_code=500,
            detail=f"Panel updated but registry cleanup failed for: {', '.join(sorted(cleanup_failed))}",
        )
    if panel_failed:
        raise HTTPException(
            status_code=502,
            detail=f"Panel enable failed; records preserved for: {', '.join(panel_failed)}",
        )

    return {"success": True, "message": f"Cleared {len(confirmed)} users from disabled list"}


# ═══════════════════════════════════════════════════════════════
# Routes - Configuration
# ═══════════════════════════════════════════════════════════════

@app.get("/config", tags=["Configuration"])
async def get_config(username: str = Depends(verify_credentials)):
    """Get current configuration (sensitive data masked)"""
    async with _config_lock:
        config = await asyncio.to_thread(load_config)
    
    # Mask sensitive data
    if "panel" in config:
        if "password" in config["panel"]:
            config["panel"]["password"] = "***"
    if "telegram" in config:
        if "bot_token" in config["telegram"]:
            config["telegram"]["bot_token"] = "***"
    if "api" in config:
        if "password" in config["api"]:
            config["api"]["password"] = "***"
    
    return {"success": True, "data": config}


@app.put("/config/limits/general", tags=["Configuration"])
async def set_general_limit(limit: int = Query(..., ge=1), username: str = Depends(verify_credentials)):
    """Set the general IP limit through the canonical store enforcement reads."""
    from utils.read_config import save_config_value

    if not await save_config_value("general_limit", limit):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to persist the general limit",
        )
    return {"success": True, "message": f"General limit set to {limit}"}


@app.put("/config/timing/check_interval", tags=["Configuration"])
async def set_check_interval(interval: int = Query(..., ge=30), username: str = Depends(verify_credentials)):
    """Set the check interval in seconds"""
    async with _config_lock:
        config = await asyncio.to_thread(load_config)
        
        if "timing" not in config:
            config["timing"] = {}
        
        config["timing"]["check_interval"] = interval
        await asyncio.to_thread(save_config, config)
    
    return {"success": True, "message": f"Check interval set to {interval} seconds"}


@app.put("/config/timing/reenable_time", tags=["Configuration"])
async def set_reenable_time(seconds: int = Query(..., ge=60), username: str = Depends(verify_credentials)):
    """Set the time to automatically re-enable disabled users"""
    async with _config_lock:
        config = await asyncio.to_thread(load_config)
        
        if "timing" not in config:
            config["timing"] = {}
        
        config["timing"]["time_to_active_users"] = seconds
        await asyncio.to_thread(save_config, config)
    
    return {"success": True, "message": f"Re-enable time set to {seconds} seconds"}


# ═══════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════

class CleanupResult(BaseModel):
    special_limits_removed: List[str]
    except_users_removed: List[str]
    disabled_users_removed: List[str]
    user_groups_backup_removed: List[str]
    total_removed: int


@app.post("/cleanup", response_model=CleanupResult, tags=["Maintenance"])
async def cleanup_deleted_users(username: str = Depends(verify_credentials)):
    """
    Clean up users from limiter config that no longer exist in the panel.
    Removes deleted users from: special limits, except_users, disabled_users, and user groups backup.
    """
    from utils.read_config import read_config
    from utils.types import PanelType
    from utils.panel_api import cleanup_deleted_users as do_cleanup
    
    try:
        config = await read_config()
        panel_config = config.get("panel", {})
        
        if not panel_config.get("domain"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Panel not configured"
            )
        
        panel_data = PanelType(
            panel_username=panel_config.get("username", ""),
            panel_password=panel_config.get("password", ""),
            panel_domain=panel_config.get("domain", "")
        )
        
        result = await do_cleanup(panel_data)
        
        total_removed = (
            len(result["special_limits_removed"]) +
            len(result["except_users_removed"]) +
            len(result["disabled_users_removed"]) +
            len(result["user_groups_backup_removed"])
        )
        
        return CleanupResult(
            special_limits_removed=result["special_limits_removed"],
            except_users_removed=result["except_users_removed"],
            disabled_users_removed=result["disabled_users_removed"],
            user_groups_backup_removed=result["user_groups_backup_removed"],
            total_removed=total_removed
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cleanup failed: {str(e)}"
        )


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    
    # Load config for API settings
    host = _resolve_bind_host()
    try:
        api_config = load_config().get("api", {})
        port = int(os.getenv("API_PORT", api_config.get("port", 8080)))
    except Exception:
        port = 8080
    
    docs_line = f"  Docs: http://{host}:{port}/docs" if _DOCS_ON else "  Docs: disabled (API_DOCS=true)"
    print(f"""
╔═══════════════════════════════════════════╗
║         🛡️  LIMITER API  🛡️               ║
║     REST API for IP Connection Limiter    ║
╠═══════════════════════════════════════════╣
║{docs_line:<43}║
╚═══════════════════════════════════════════╝
    """)

    if host == "0.0.0.0":  # nosec B104 - operator's choice, but say so out loud
        logger.warning(
            "⚠️ The API is listening on 0.0.0.0, so it is reachable from outside this "
            "host. There is no rate limiting; put it behind a firewall or set API_HOST."
        )

    uvicorn.run(app, host=host, port=port)
