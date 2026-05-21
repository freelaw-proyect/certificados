"""Entrega de imágenes de captcha vía HTTP (evita WebSocket con base64 enorme o vacío)."""

from __future__ import annotations

import base64
import re
import secrets
from typing import Any

_captcha_store: dict[str, tuple[bytes, str]] = {}


def _bytes_from_captcha_payload(payload: dict[str, Any]) -> tuple[bytes, str] | None:
    mime = (payload.get("mime") or "image/png").strip() or "image/png"
    data_url = (payload.get("image_data_url") or "").strip()
    if data_url.startswith("data:image/") and "base64," in data_url:
        try:
            head, b64 = data_url.split("base64,", 1)
            if "/" in head:
                mime = head.split(";", 1)[0].replace("data:", "").strip() or mime
            raw = base64.b64decode(re.sub(r"\s+", "", b64), validate=False)
            if raw:
                return raw, mime
        except Exception:
            pass
    b64 = (payload.get("image_base64") or "").strip()
    if b64:
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", b64), validate=False)
            if raw:
                return raw, mime
        except Exception:
            pass
    return None


def payload_has_image_data(payload: dict[str, Any]) -> bool:
    got = _bytes_from_captcha_payload(payload)
    return got is not None and len(got[0]) >= 800


def register_captcha_for_browser(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    Guarda bytes en memoria y devuelve mensaje WS con ``image_url`` (GET), no base64 inline.
    """
    got = _bytes_from_captcha_payload(payload)
    if not got:
        return None
    data, mime = got
    if len(data) < 800:
        return None
    token = secrets.token_urlsafe(16)
    _captcha_store[token] = (data, mime)
    out: dict[str, Any] = {"type": "captcha", "phase": payload.get("phase") or ""}
    out["image_url"] = f"/api/captcha/{token}"
    if payload.get("capture"):
        out["capture"] = payload["capture"]
    return out


def pop_captcha_bytes(token: str) -> tuple[bytes, str] | None:
    return _captcha_store.pop(token, None)


def get_captcha_bytes(token: str) -> tuple[bytes, str] | None:
    return _captcha_store.get(token)
