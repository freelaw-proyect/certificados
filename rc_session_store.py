"""
Almacén de cookie RC en memoria (TTL) con persistencia opcional en disco.

Pensado para: script local obtiene sesión en Chrome → POST /registrocivil/session →
Render/API usa la cookie sin abrir Chromium.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "cookie": "",
    "saved_at": 0.0,
    "expires_at": 0.0,
    "source": "",
    "run": "",
}


def _cookie_names_preview(cookie_header: str, limit: int = 12) -> list[str]:
    names: list[str] = []
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if "=" in part:
            names.append(part.split("=", 1)[0].strip())
    return names[:limit]


def configure_persist_path(path: Path | None) -> None:
    with _lock:
        _state["_persist_path"] = path


def _persist_path() -> Path | None:
    return _state.get("_persist_path")


def _write_disk() -> None:
    path = _persist_path()
    if not path:
        return
    payload = {
        "cookie": _state.get("cookie") or "",
        "saved_at": _state.get("saved_at") or 0.0,
        "expires_at": _state.get("expires_at") or 0.0,
        "source": _state.get("source") or "",
        "run": _state.get("run") or "",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_from_disk(path: Path) -> bool:
    """Carga sesión si el archivo existe y no expiró."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    cookie = (data.get("cookie") or "").strip()
    expires_at = float(data.get("expires_at") or 0.0)
    if not cookie or expires_at <= time.time():
        return False
    with _lock:
        _state["cookie"] = cookie
        _state["saved_at"] = float(data.get("saved_at") or time.time())
        _state["expires_at"] = expires_at
        _state["source"] = (data.get("source") or "disk").strip()
        _state["run"] = (data.get("run") or "").strip()
        _state["_persist_path"] = path
    return True


def set_session(
    cookie: str,
    *,
    ttl_sec: float,
    source: str = "",
    run: str = "",
) -> dict[str, Any]:
    raw = (cookie or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw or len(raw) < 20:
        raise ValueError("Cookie vacía o demasiado corta")
    now = time.time()
    ttl = max(300.0, float(ttl_sec))
    with _lock:
        _state["cookie"] = raw
        _state["saved_at"] = now
        _state["expires_at"] = now + ttl
        _state["source"] = (source or "api").strip()
        _state["run"] = (run or "").strip()
    _write_disk()
    return session_status()


def clear_session() -> None:
    with _lock:
        _state["cookie"] = ""
        _state["saved_at"] = 0.0
        _state["expires_at"] = 0.0
        _state["source"] = ""
        _state["run"] = ""
    path = _persist_path()
    if path and path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def get_active_cookie() -> str:
    with _lock:
        cookie = (_state.get("cookie") or "").strip()
        expires_at = float(_state.get("expires_at") or 0.0)
    if not cookie:
        return ""
    if expires_at and expires_at <= time.time():
        clear_session()
        return ""
    return cookie


def session_status() -> dict[str, Any]:
    with _lock:
        cookie = (_state.get("cookie") or "").strip()
        saved_at = float(_state.get("saved_at") or 0.0)
        expires_at = float(_state.get("expires_at") or 0.0)
        source = (_state.get("source") or "").strip()
        run = (_state.get("run") or "").strip()
    now = time.time()
    active = bool(cookie and (not expires_at or expires_at > now))
    return {
        "active": active,
        "source": source if active else "",
        "run": run if active else "",
        "saved_at": saved_at if active else None,
        "expires_at": expires_at if active else None,
        "expires_in_sec": max(0, int(expires_at - now)) if active and expires_at else 0,
        "cookie_names": _cookie_names_preview(cookie) if active else [],
        "cookie_length": len(cookie) if active else 0,
    }
