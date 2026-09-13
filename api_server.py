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

from fastapi import FastAPI, HTTPException, Depends, Query, status
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


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """HTTP Basic check that fails closed."""
    config = load_config()
    api_config = config.get("api", {})

    correct_username = os.getenv("API_USERNAME") or api_config.get("username", "admin")
    correct_password = os.getenv("API_PASSWORD") or api_config.get("password", "")

    if not correct_password:
        logger.error("API request refused: no API password is configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API password is not configured; the API is disabled",
        )

    is_correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"), correct_username.encode("utf8")
    )
    is_correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"), correct_password.encode("utf8")
    )

    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Limiter API Server starting")
    yield
    logger.info("Limiter API Server shutting down")


def _docs_enabled() -> bool:
    return os.getenv("API_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}


def _cors_origins() -> list[str]:
    raw = os.getenv("API_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


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


@app.get("/", tags=["Status"])
async def root():
    return {"status": "ok", "message": "Limiter API is running"}


@app.get("/status", response_model=StatusResponse, tags=["Status"])
async def get_status(username: str = Depends(verify_credentials)):
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
        disabled_users_count=len(disabled),
    )


@app.get("/users/disabled", tags=["Disabled Users"])
async def list_disabled_users_route(username: str = Depends(verify_credentials)):
    disabled = await load_disabled_users()
    current_time = time.time()
    data = []
    for user, disabled_time in disabled.items():
        elapsed = int(current_time - disabled_time)
        data.append({
            "username": user,
            "disabled_at": disabled_time,
            "disabled_at_formatted": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(disabled_time)),
            "elapsed_seconds": elapsed,
        })
    return {"success": True, "data": sorted(data, key=lambda x: x["disabled_at"], reverse=True)}


@app.delete("/users/disabled/{user}", tags=["Disabled Users"])
async def enable_disabled_user(user: str, username: str = Depends(verify_credentials)):
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


if __name__ == "__main__":
    import uvicorn
    try:
        config = load_config()
        api_config = config.get("api", {})
        host = os.getenv("API_HOST", api_config.get("host", "0.0.0.0"))
        port = int(os.getenv("API_PORT", api_config.get("port", 8080)))
    except Exception:
        host = "0.0.0.0"
        port = 8080
    uvicorn.run(app, host=host, port=port)
