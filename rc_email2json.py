"""
Recepción de correos vía webhook de https://www.email2json.com/

email2json hace POST con JSON en el body (ver gist de ejemplo en su sitio).
"""

from __future__ import annotations

import base64
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RC_FROM_HINTS = (
    "registrocivil",
    "srcei",
    "gob.cl",
    "certificado",
    "registro civil",
)


@dataclass
class ParsedIncomingEmail:
    message_id: str = ""
    subject: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    date: str = ""
    body_text: str = ""
    looks_registro_civil: bool = False
    attachments_saved: list[str] = field(default_factory=list)
    raw_saved: str = ""


def _as_list_str(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, list):
        out: list[str] = []
        for x in val:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                for k in ("email", "address", "value"):
                    if isinstance(x.get(k), str) and x[k].strip():
                        out.append(x[k].strip())
                        break
        return out
    return []


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _extract_body_text(data: dict[str, Any]) -> str:
    for key in ("body", "text", "textBody", "plain", "content"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    ch = data.get("commonHeaders")
    if isinstance(ch, dict):
        for key in ("body", "text"):
            v = ch.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _extract_common_headers(data: dict[str, Any]) -> dict[str, Any]:
    ch = data.get("commonHeaders")
    if isinstance(ch, dict):
        return ch
    headers = data.get("headers")
    if not isinstance(headers, list):
        return {}
    out: dict[str, Any] = {}
    for h in headers:
        if not isinstance(h, dict):
            continue
        name = (h.get("name") or "").strip().lower()
        value = h.get("value")
        if name == "subject" and isinstance(value, str):
            out["subject"] = value
        elif name == "from" and isinstance(value, str):
            out.setdefault("from", []).append(value)
        elif name == "to" and isinstance(value, str):
            out.setdefault("to", []).append(value)
        elif name == "date" and isinstance(value, str):
            out["date"] = value
        elif name == "message-id" and isinstance(value, str):
            out["messageId"] = value
    return out


def _looks_registro_civil(subject: str, from_addr: str, body: str) -> bool:
    blob = f"{subject} {from_addr} {body[:4000]}".lower()
    return any(h in blob for h in RC_FROM_HINTS)


def _iter_attachment_candidates(data: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        fn = data.get("filename") or data.get("fileName") or data.get("name")
        content = (
            data.get("content")
            or data.get("data")
            or data.get("base64")
            or data.get("body")
        )
        if isinstance(fn, str) and content is not None:
            found.append({"filename": fn, "content": content, "contentType": data.get("contentType")})
        for k, v in data.items():
            if k in ("attachments", "attachment", "files", "parts", "mimeParts"):
                if isinstance(v, list):
                    for item in v:
                        found.extend(_iter_attachment_candidates(item, depth + 1))
            elif isinstance(v, (dict, list)):
                found.extend(_iter_attachment_candidates(v, depth + 1))
    elif isinstance(data, list):
        for item in data:
            found.extend(_iter_attachment_candidates(item, depth + 1))
    return found


def _decode_attachment_content(raw: Any) -> bytes | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = re.sub(r"\s+", "", s)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


def _safe_filename(name: str, fallback: str) -> str:
    base = re.sub(r"[^\w.\-]+", "_", (name or "").strip()) or fallback
    if not base.lower().endswith((".pdf", ".json", ".txt", ".html", ".zip")):
        if "." not in base:
            base += ".bin"
    return base[:180]


def parse_email2json_payload(data: dict[str, Any]) -> ParsedIncomingEmail:
    ch = _extract_common_headers(data)
    subject = str(ch.get("subject") or data.get("subject") or "").strip()
    from_addr = ""
    from_list = _as_list_str(ch.get("from")) or _as_list_str(data.get("from"))
    if from_list:
        from_addr = from_list[0]
    to_addrs = _as_list_str(ch.get("to")) or _as_list_str(data.get("destination")) or _as_list_str(
        data.get("to")
    )
    body_text = _extract_body_text(data)
    message_id = str(
        ch.get("messageId") or data.get("messageId") or data.get("message_id") or ""
    ).strip()
    date = str(ch.get("date") or data.get("timestamp") or data.get("date") or "").strip()
    return ParsedIncomingEmail(
        message_id=message_id,
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs,
        date=date,
        body_text=body_text,
        looks_registro_civil=_looks_registro_civil(subject, from_addr, body_text),
    )


async def load_request_payload(request: Any) -> dict[str, Any]:
    """Lee JSON del body; tolera application/json y form con campo json."""
    raw = await request.body()
    if not raw:
        return {}
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype or raw[:1] in (b"{", b"["):
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(parsed, dict):
                return parsed
            return {"_raw": parsed}
    except json.JSONDecodeError:
        pass
    try:
        from urllib.parse import parse_qs

        qs = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        for key in ("payload", "json", "data", "email"):
            if key in qs and qs[key]:
                inner = json.loads(qs[key][0])
                if isinstance(inner, dict):
                    return inner
    except Exception:
        pass
    return {"_raw_text": raw.decode("utf-8", errors="replace")[:200_000]}


def persist_incoming(
    *,
    inbox_dir: Path,
    parsed: ParsedIncomingEmail,
    payload: dict[str, Any],
    save_raw: bool,
) -> ParsedIncomingEmail:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^\w\-]+", "_", (parsed.message_id or parsed.subject or "mail")[:60]).strip("_")
    base = f"{stamp}_{slug or 'mail'}"

    if save_raw:
        raw_path = inbox_dir / f"{base}.json"
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        parsed.raw_saved = str(raw_path)

    summary_path = inbox_dir / f"{base}.meta.json"
    summary_path.write_text(
        json.dumps(
            {
                "message_id": parsed.message_id,
                "subject": parsed.subject,
                "from": parsed.from_addr,
                "to": parsed.to_addrs,
                "date": parsed.date,
                "looks_registro_civil": parsed.looks_registro_civil,
                "body_preview": (parsed.body_text or "")[:2000],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    att_dir = inbox_dir / base
    att_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for att in _iter_attachment_candidates(payload):
        fn = _safe_filename(str(att.get("filename") or ""), f"adjunto_{n}")
        blob = _decode_attachment_content(att.get("content"))
        if not blob:
            continue
        dest = att_dir / fn
        dest.write_bytes(blob)
        parsed.attachments_saved.append(str(dest))
        n += 1

    # PDF embebido en body (algunos forwards MIME)
    if parsed.body_text:
        for m in re.finditer(
            r"Content-Disposition:\s*attachment;\s*filename=\"?([^\"\n;]+)\"?",
            parsed.body_text,
            re.I,
        ):
            fn = _safe_filename(m.group(1), f"body_{n}.pdf")
            if not fn.lower().endswith(".pdf"):
                continue
            chunk = parsed.body_text[m.end() : m.end() + 500_000]
            b64_m = re.search(
                r"Content-Transfer-Encoding:\s*base64\s*([\s\S]{80,}?)(?:\n--|\Z)",
                chunk,
                re.I,
            )
            if not b64_m:
                continue
            blob = _decode_attachment_content(b64_m.group(1))
            if blob and blob[:4] == b"%PDF":
                dest = att_dir / fn
                dest.write_bytes(blob)
                parsed.attachments_saved.append(str(dest))
                n += 1

    return parsed


def _stderr(msg: str) -> None:
    print(f"[email2json] {msg}", file=sys.stderr, flush=True)
