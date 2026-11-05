"""Independent Mirza Bot route settings and pure secret-free admission snapshots."""

from contextlib import asynccontextmanager
from copy import deepcopy
import html
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit
import uuid

from db.crud.mirza_routes import MirzaRouteError, MirzaRoutesCRUD


def _admin_name(value: str) -> str:
    if not isinstance(value, str) or any(unicodedata.category(char).startswith("C") for char in value):
        raise MirzaRouteError("Admin username must not contain control characters")
    value = value.strip()
    if not value or len(value) > 255:
        raise MirzaRouteError("Admin username must contain 1–255 characters")
    return value


def normalize_base_url(value: str) -> str:
    """Require an HTTPS API base, never infer one from a users endpoint."""
    if not isinstance(value, str):
        raise MirzaRouteError("Invalid HTTPS API base URL")
    value = value.strip()
    if any(char.isspace() or ord(char) < 32 for char in value) or any(char in value for char in "\\?#"):
        raise MirzaRouteError("Invalid HTTPS API base URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            if len(ascii_host) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                raise ValueError
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/users"):
            raise ValueError
        return urlunsplit(("https", parsed.netloc, path or "/api", "", ""))
    except (ValueError, UnicodeError):
        raise MirzaRouteError("Invalid HTTPS API base URL") from None


def parse_recipient(note: str | None) -> str | None:
    """Keep a positive ASCII ID as text; never search other note fields."""
    if not isinstance(note, str):
        return None
    fields = note.split("|")
    if len(fields) != 3:
        return None
    recipient = fields[0].strip()
    return recipient if re.fullmatch(r"[0-9]+", recipient) and any(c != "0" for c in recipient) else None


def render_message(template: str, username: str, limit: int | None = None) -> str:
    """Expand exact config usernames safely inside operator-authored HTML."""
    if not isinstance(template, str) or not isinstance(username, str):
        raise MirzaRouteError("Invalid notification message")
    if "[Limit]" in template:
        if type(limit) is not int or limit < 1:
            raise MirzaRouteError("Limit token requires a positive admitted connection limit")
    tokens = {"[Username]": html.escape(username, quote=True), "[Limit]": str(limit)}
    rendered = re.sub(r"\[Username\]|\[Limit\]", lambda match: tokens[match[0]], template)
    try:
        if not rendered.strip() or len(rendered.encode("utf-16-le")) // 2 > 4096:
            raise ValueError
    except (ValueError, UnicodeError):
        raise MirzaRouteError("Expanded message must contain 1–4096 UTF-16 units") from None
    return rendered


def _validate_route(values: dict) -> dict:
    values = deepcopy(values)
    values["admin_username"] = _admin_name(values["admin_username"])
    if values["base_url"]:
        values["base_url"] = normalize_base_url(values["base_url"])
    elif not isinstance(values["base_url"], str):
        raise MirzaRouteError("Invalid HTTPS API base URL")
    key = values["api_key"]
    if not isinstance(key, str) or len(key) > 512 or any(not 33 <= ord(c) <= 126 for c in key):
        raise MirzaRouteError("API key must be at most 512 printable non-whitespace ASCII characters")
    text = values["message_text"]
    try:
        if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ValueError
    except (ValueError, UnicodeError):
        raise MirzaRouteError("Message must be at most 4096 UTF-16 units") from None
    if (
        type(values["enabled"]) is not bool
        or not isinstance(values["trigger_mode"], str)
        or values["trigger_mode"] not in {"last", "selected"}
    ):
        raise MirzaRouteError("Invalid enabled flag or trigger mode")
    steps = values["selected_steps"]
    if not isinstance(steps, list) or any(type(step) is not int or not 0 <= step <= 9 for step in steps):
        raise MirzaRouteError("Selected steps must be zero-based integers from 0 to 9")
    values["selected_steps"] = sorted(set(steps))
    if values["enabled"] and (
        not values["base_url"]
        or not key
        or not text.strip()
        or (values["trigger_mode"] == "selected" and not values["selected_steps"])
    ):
        raise MirzaRouteError("Complete the URL, key, message and step selection before enabling")
    return values


@asynccontextmanager
async def _transaction():
    """Hide SQL exception parameters, including keys, from caller error logs."""
    from db.database import AsyncSessionLocal, DB_AVAILABLE

    try:
        if not DB_AVAILABLE:
            raise RuntimeError
        async with AsyncSessionLocal() as db:
            yield db
            await db.commit()
    except MirzaRouteError:
        raise
    except Exception:
        raise RuntimeError("Mirza Bot settings database operation failed") from None


async def list_routes() -> list[dict]:
    async with _transaction() as db:
        return [MirzaRoutesCRUD.public(row) for row in await MirzaRoutesCRUD.list(db)]


async def get_route(route_id: str) -> dict | None:
    async with _transaction() as db:
        row = await MirzaRoutesCRUD.get(db, route_id)
        return MirzaRoutesCRUD.public(row) if row is not None else None


async def create_route(admin_username: str) -> dict:
    admin_username = _admin_name(admin_username)
    async with _transaction() as db:
        return MirzaRoutesCRUD.public(await MirzaRoutesCRUD.create(db, admin_username))


async def update_route(route_id: str, changes: dict, *, expected_revision: int | None = None) -> dict:
    allowed = {"admin_username", "base_url", "api_key", "message_text", "enabled", "trigger_mode", "selected_steps"}
    if not isinstance(changes, dict) or not set(changes).issubset(allowed):
        raise MirzaRouteError("Unknown route setting")
    async with _transaction() as db:
        row = await MirzaRoutesCRUD.get(db, route_id)
        if row is None:
            raise MirzaRouteError("Route no longer exists")
        values = {key: getattr(row, key) for key in allowed}
        values.update(deepcopy(changes))
        values = _validate_route(values)
        revision = row.revision if expected_revision is None else expected_revision
        return MirzaRoutesCRUD.public(await MirzaRoutesCRUD.update(db, route_id, values, expected_revision=revision))


async def delete_route(route_id: str, *, expected_revision: int | None = None) -> None:
    async with _transaction() as db:
        await MirzaRoutesCRUD.delete(db, route_id, expected_revision=expected_revision)


async def capture_rules() -> list[dict]:
    async with _transaction() as db:
        rules = []
        for row in await MirzaRoutesCRUD.list(db):
            values = {
                key: getattr(row, key)
                for key in (
                    "admin_username",
                    "base_url",
                    "api_key",
                    "message_text",
                    "enabled",
                    "trigger_mode",
                    "selected_steps",
                )
            }
            try:
                _validate_route(values)
            except MirzaRouteError:
                continue
            if row.enabled:
                rules.append(
                    {
                        "route_id": row.id,
                        "revision": row.revision,
                        "admin_username": row.admin_username,
                        "trigger_mode": row.trigger_mode,
                        "selected_steps": deepcopy(row.selected_steps),
                    }
                )
        return rules


def notification_context(
    rules: list[dict], step_index: int, step_count: int, step_type: str, effective_limit: int | None = None
) -> dict | None:
    if effective_limit is not None and (type(effective_limit) is not int or effective_limit < 1):
        raise MirzaRouteError("Invalid effective connection limit")
    if (
        type(step_index) is not int
        or type(step_count) is not int
        or not 0 <= step_index < step_count <= 10
        or not isinstance(step_type, str)
        or step_type not in {"warning", "disable", "revoke"}
    ):
        return None
    candidates = [
        {key: rule[key] for key in ("route_id", "revision", "admin_username")}
        for rule in rules
        if (rule["trigger_mode"] == "last" and step_index == step_count - 1)
        or (rule["trigger_mode"] == "selected" and step_index in rule["selected_steps"])
    ]
    return (
        deepcopy(
            {
                "candidates": candidates,
                "step_index": step_index,
                "step_count": step_count,
                "step_type": step_type,
                "effective_limit": effective_limit,
            }
        )
        if candidates
        else None
    )


def validate_notification_context(context: dict | None) -> dict | None:
    """Reject malformed or secret-bearing journal context; accept legacy null."""
    if context is None:
        return None
    required = {"candidates", "step_index", "step_count", "step_type"}
    if (
        not isinstance(context, dict)
        or not required.issubset(context)
        or not set(context).issubset(required | {"effective_limit"})
    ):
        raise ValueError("Invalid notification context")
    context = deepcopy(context)
    context.setdefault("effective_limit", None)
    limit = context["effective_limit"]
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Invalid notification context")
    index, count = context["step_index"], context["step_count"]
    if (
        type(index) is not int
        or type(count) is not int
        or not 0 <= index < count <= 10
        or not isinstance(context["step_type"], str)
        or context["step_type"] not in {"warning", "disable", "revoke"}
        or not isinstance(context["candidates"], list)
    ):
        raise ValueError("Invalid notification context")
    seen = set()
    for candidate in context["candidates"]:
        if not isinstance(candidate, dict) or set(candidate) != {"route_id", "revision", "admin_username"}:
            raise ValueError("Invalid notification context")
        try:
            if not isinstance(candidate["route_id"], str):
                raise ValueError
            uuid.UUID(candidate["route_id"])
            if (
                type(candidate["revision"]) is not int
                or candidate["revision"] < 1
                or _admin_name(candidate["admin_username"]) != candidate["admin_username"]
                or candidate["route_id"] in seen
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid notification context") from None
        seen.add(candidate["route_id"])
    return deepcopy(context)
