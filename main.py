"""
Servicio mínimo que replica la secuencia HTTP del flujo de entrega de certificados
(Registro Civil Chile): carrito → orden de compra → entrega → página post-pago.

Las cookies de sesión deben venir del navegador (cabecera o env); sin ellas el sitio
no mantiene sesión como en los curls manuales.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from rc_selenium import _is_rc_waf_or_non_jsf_shell_html


class Settings(BaseSettings):
    """Toda la configuración va en env / .env con prefijo REGISTROCIVIL_."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_prefix="REGISTROCIVIL_")

    cookie: str = ""
    # Opcional: ruta a archivo con una línea Cookie (si REGISTROCIVIL_COOKIE está vacío).
    cookie_file: str = ""
    default_run: str = ""
    default_email: str = ""
    default_numero_documento_sol: str = ""
    default_carro_run_consulta: str = ""
    default_carro_run_solicitante: str = ""
    default_carro_run_nacionalidad: str = "53"
    default_id_certificado: str = "133_1_2"
    default_filtro: str = "99"
    default_solicitar_apostilla: str = "false"
    default_solicitar_valija: str = "false"
    # standard = nacimiento/defunción (POST extra RUN consulta/solicitante). matrimonio = solo campos del formulario plano.
    # Las URLs de entrega/freePagado son siempre las planas /web/entregadocumentos*.srcei (el subpath entregadocumentos/… devuelve 404).
    entrega_variant: str = "standard"
    entrega_email_confirm: str = ""
    entrega_telefono: str = ""
    entrega_email2: str = ""
    entrega_email_confirm2: str = ""
    default_name_check_apostilla: str = "false"
    default_name_pais_apostilla: str = "-1"

    # selenium | playwright (Playwright suele ir mejor en headless con el WAF del RC)
    browser_backend: str = "playwright"
    use_selenium: bool = False
    selenium_headless: bool = True
    selenium_timeout_sec: int = 60
    # Con SELENIUM_HEADLESS=false: segundos máx. esperando a que resuelvas el captcha y cargue el carrito JSF.
    selenium_manual_captcha_timeout_sec: int = 420
    # Si true y Selenium falla, se usa REGISTROCIVIL_COOKIE del .env (riesgo de sesión caduca).
    selenium_fallback_env_cookie: bool = False
    chrome_binary: str = ""
    # true = pack en un carrito + una entrega al correo (fallos parciales no rompen el flujo).
    default_pack_tres_certificados: bool = True
    default_pack_id_nacimiento: str = "133_1_2"
    default_pack_id_defuncion: str = "3_3_1"
    default_pack_id_matrimonio: str = "2_2_2"
    # Vacío = no se intenta. Obtén el valor en carro.srcei → Network → agregarACarro.srcei?idCertificado=…
    default_pack_id_union_civil: str = "9_11_1"
    default_pack_union_civil_entrega_variant: str = "matrimonio"
    # WebSocket /: token opcional (?token=) para no exponer el flujo en red abierta.
    ws_token: str = ""
    # POST /registrocivil/session (script local → Render). Si vacío, se usa ws_token.
    session_token: str = ""
    session_ttl_sec: int = 7200
    # Persistencia opcional (sobrevive reinicios del mismo contenedor; Render efímero).
    session_file: str = "session.rc.json"
    # En el flujo WS: false = Chrome visible (mejor para depurar y para capturar el img del desafío).
    ws_selenium_headless: bool = False
    # Headless del navegador en WebSocket (Playwright: true recomendado; Selenium: a menudo falla el WAF)
    ws_browser_headless: bool = False
    # WebSocket: tras captcha, rellenar carrito y pulsar Continuar en Chrome (no solo httpx).
    ws_complete_entrega_in_browser: bool = True
    # Si true: al arrancar FastAPI se vacía bug/ (incl. bug/requests/). Con uvicorn --reload se vacía
    # en cada recarga. Para conservar trazas entre recargas: REGISTROCIVIL_BUG_CLEAR_ON_START=false.
    bug_clear_on_start: bool = True
    # Ruta absoluta o relativa donde guardar bug/ (vacío = carpeta ``bug`` junto a main.py).
    bug_dir: str = ""

    # Inbox email2json.com: dirección *@email2json.com → POST a /webhooks/email2json
    # Pon esa misma dirección en REGISTROCIVIL_DEFAULT_EMAIL para que el RC envíe ahí.
    email2json_webhook_token: str = ""
    email2json_inbox_dir: str = "incoming"
    email2json_save_raw: bool = True
    # Opcional: reenviar el JSON parseado a otro backend (freelaw-backend, etc.)
    email2json_forward_url: str = ""

    # Árbol familiar: nacimiento semilla → padres → abuelos → ZIP + PNG
    genealogy_output_dir: str = "salida"
    genealogy_max_generation: int = 2
    genealogy_poll_after_request_sec: float = 120.0
    genealogy_poll_interval_sec: float = 5.0


settings = Settings()


def _resolve_ws_headless(requested: bool) -> tuple[bool, str | None]:
    """
    En Render/Docker no hay X11 ($DISPLAY): headless=false rompe Playwright.
    En Mac/local con DISPLAY se respeta ws_browser_headless=false.
    """
    if requested:
        return True, None
    if (os.environ.get("DISPLAY") or "").strip() or (os.environ.get("WAYLAND_DISPLAY") or "").strip():
        return False, None
    return True, (
        "Sin pantalla ($DISPLAY): Playwright en headless. "
        "Para reCAPTCHA en deploy: REGISTROCIVIL_WS_BROWSER_HEADLESS=false y "
        "REGISTROCIVIL_USE_XVFB=true (Docker arranca Xvfb), o usa REGISTROCIVIL_COOKIE."
    )


class EntregaCertificadoIn(BaseModel):
    """Cuerpo opcional: cookie de sesión RC (alternativa a cabecera o .env)."""

    cookie: str | None = None


class RegistroCivilSessionIn(BaseModel):
    """Cookie obtenida en local (Chrome) para que el API en Render la use sin navegador."""

    cookie: str
    ttl_sec: int | None = None
    source: str | None = None
    run: str | None = None


def _session_persist_path() -> Path | None:
    raw = (settings.session_file or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else Path(__file__).resolve().parent / p


def _session_api_token() -> str:
    return (settings.session_token or settings.ws_token or "").strip()


def _verify_session_api_token(
    x_registrocivil_session_token: str | None = None,
    authorization: str | None = None,
) -> None:
    expected = _session_api_token()
    if not expected:
        return
    got = (x_registrocivil_session_token or "").strip()
    if not got and authorization:
        low = authorization.strip().lower()
        if low.startswith("bearer "):
            got = authorization.strip()[7:].strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="Token de sesión inválido")


def _load_cookie_from_env_or_file() -> str:
    raw = (settings.cookie or "").strip()
    if raw:
        return raw
    path_raw = (settings.cookie_file or "").strip()
    if not path_raw:
        return ""
    path = Path(path_raw).expanduser()
    if not path.is_file():
        return ""
    line = path.read_text(encoding="utf-8", errors="replace").strip()
    if line.lower().startswith("cookie:"):
        line = line.split(":", 1)[1].strip()
    return line


def _load_cookie_from_session_store() -> str:
    from rc_session_store import get_active_cookie

    return get_active_cookie()


def _resolve_cookie_from_request(
    x_registrocivil_cookie: str | None,
    body_cookie: str | None,
    *,
    extra_cookie: str | None = None,
) -> tuple[str, str]:
    """
    Orden: cabecera → body → extra (p. ej. WS start) → .env → sesión POST /registrocivil/session.
    Devuelve (cookie, origin).
    """
    had_header = bool((x_registrocivil_cookie or "").strip())
    if (x_registrocivil_cookie or "").strip():
        return (x_registrocivil_cookie or "").strip(), "header"
    if (body_cookie or "").strip():
        return (body_cookie or "").strip(), "body"
    if (extra_cookie or "").strip():
        return (extra_cookie or "").strip(), "ws_start"
    env = _load_cookie_from_env_or_file()
    if env:
        return env, "env"
    stored = _load_cookie_from_session_store()
    if stored:
        return stored, "session_store"
    return "", "none"


def _resolve_registrocivil_cookie(
    x_registrocivil_cookie: str | None,
    body_cookie: str | None,
) -> tuple[str, bool, bool, str | None, str]:
    """
    Orden: cabecera → body → env → sesión en memoria → Selenium (si USE_SELENIUM=true).

    Devuelve (cookie, had_header, selenium_used, selenium_error, cookie_origin).
    """
    had_header = bool((x_registrocivil_cookie or "").strip())
    cookie, origin = _resolve_cookie_from_request(x_registrocivil_cookie, body_cookie)
    selenium_used = False
    selenium_error: str | None = None

    if cookie:
        return cookie, had_header, selenium_used, selenium_error, origin

    if not settings.use_selenium:
        return "", had_header, False, None, "none"

    try:
        from rc_selenium import fetch_cookie_header_via_chrome

        run, _ = _normalize_run_for_rc(settings.default_run.strip())
        id_boot = (
            settings.default_pack_id_nacimiento.strip()
            if settings.default_pack_tres_certificados
            else settings.default_id_certificado.strip()
        )
        sc = fetch_cookie_header_via_chrome(
            run=run,
            filtro=settings.default_filtro,
            id_certificado=id_boot,
            headless=settings.selenium_headless,
            timeout=settings.selenium_timeout_sec,
            chrome_binary=settings.chrome_binary.strip() or None,
            manual_captcha_timeout_sec=(
                settings.selenium_manual_captcha_timeout_sec
                if not settings.selenium_headless
                else None
            ),
        )
        if sc.strip():
            return sc.strip(), had_header, True, None, "selenium"
        selenium_error = (
            "Chrome no llegó a un carrito JSF válido (captcha, TSPD o timeout). "
            "Pega REGISTROCIVIL_COOKIE tras resolver el desafío en el navegador."
        )
    except Exception as e:
        msg = str(e).strip() or repr(e)
        selenium_error = f"{type(e).__name__}: {msg}"

    if settings.selenium_fallback_env_cookie:
        fallback = _load_cookie_from_env_or_file()
        if fallback:
            return fallback, had_header, False, selenium_error, "env_fallback"

    return "", had_header, False, selenium_error, "none"


BUG_DIR_FALLBACK = Path(__file__).resolve().parent / "bug"


def bug_dir() -> Path:
    raw = (settings.bug_dir or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return BUG_DIR_FALLBACK


_bug_dir_logged = False

BASE = "https://www.registrocivil.cl"
OFICINA = f"{BASE}/OficinaInternet/web"
# Referer inicial del carrito (mismo origen que en tráfico real del portal).
REF_SERVICIOS_LINEA = f"{BASE}/principal/servicios-en-linea"
# El RC expone agregar al carro en /web/agregarACarro.srcei (no bajo /web/carrito/…; esa ruta devuelve 404).
URL_AGREGAR_ACARRO = f"{OFICINA}/agregarACarro.srcei"


def _numero_documento_para_rc(run: str, numero: str) -> str:
    """
    N° documento del solicitante (serie de la cédula), NO el RUN.

    En el curl exitoso del RC: ``numeroDocumentoSol=529745644`` y ``runSol=17.402.744-7``.
    """
    n = (numero or "").strip()
    if not n:
        return ""
    if re.match(r"^\d{1,2}\.\d{3}\.\d{3}-[0-9K]$", n.upper()):
        return ""
    if n.replace(".", "").upper() == run.strip().replace(".", "").upper():
        return ""
    return n


def _aviso_numero_documento_invalido(run: str, numero: str) -> str | None:
    n = (numero or "").strip()
    if not n:
        return (
            "REGISTROCIVIL_DEFAULT_NUMERO_DOCUMENTO_SOL vacío: debe ser el N° de documento "
            "(serie de la cédula, ej. 529745644), no el RUN."
        )
    if re.match(r"^\d{1,2}\.\d{3}\.\d{3}-[0-9K]$", n.upper()):
        return (
            "REGISTROCIVIL_DEFAULT_NUMERO_DOCUMENTO_SOL parece un RUN con puntos; "
            "el RC espera el N° de documento (solo dígitos de la cédula, ej. 529745644)."
        )
    if n.replace(".", "").upper() == run.strip().replace(".", "").upper():
        return (
            "NUMERO_DOCUMENTO_SOL no puede ser el mismo valor que el RUN; "
            "usa el número impreso en la cédula (ej. 529745644)."
        )
    return None


def _normalize_run_for_rc(run: str) -> tuple[str, str | None]:
    """
    Devuelve (run para parámetro `run` en URLs, aviso si el formato es sospechoso).
    Si ya viene como XX.XXX.XXX-D, no se toca.
    Con o sin guion: si el cuerpo numérico tiene 7 u 8 dígitos, inserta puntos.
    """
    raw = run.strip().upper()
    if re.match(r"^\d{1,2}\.\d{3}\.\d{3}-[0-9K]$", raw):
        return raw, None
    t = raw.replace(".", "")
    if "-" in t:
        body_part, dv = t.rsplit("-", 1)
        body = re.sub(r"[^0-9]", "", body_part)
        dv = re.sub(r"[^0-9K]", "", dv.upper())
    else:
        clean = re.sub(r"[^0-9K]", "", t)
        if len(clean) < 2:
            return run.strip(), None
        body, dv = clean[:-1], clean[-1].upper()
        dv = re.sub(r"[^0-9K]", "", dv)
    if not body or len(dv) != 1:
        return run.strip(), None
    if len(body) > 8:
        return run.strip(), (
            "El RUN tiene más de 8 dígitos antes del dígito verificador (revisa typos: "
            "ej. 17.402.744-7). En bug/*_02_GET_agregarACarro.html el RC suele decir que los datos "
            "no cumplen requisitos."
        )
    if len(body) < 7:
        return run.strip(), "El RUN tiene menos de 7 dígitos en el cuerpo; revisa el valor en .env."
    parts: list[str] = []
    i = len(body)
    while i > 0:
        j = max(0, i - 3)
        parts.insert(0, body[j:i])
        i = j
    return ".".join(parts) + "-" + dv, None


def _extract_agregar_carro_mensaje(html: str) -> str | None:
    """Texto de error visible en GET agregarACarro (requisitos / datos)."""
    if not html.strip():
        return None
    m = re.search(
        r'class="errorMessage"[^>]*>\s*<li>\s*<span>([^<]+)</span>',
        html,
        re.I | re.S,
    )
    if m:
        t = re.sub(r"\s+", " ", m.group(1)).strip()
        return t or None
    m2 = re.search(
        r"agr_AdvErrorStruts[^>]*>.*?<span>([^<]{5,})</span>",
        html,
        re.I | re.S,
    )
    if m2:
        t = re.sub(r"\s+", " ", m2.group(1)).strip()
        return t or None
    return None


def _extract_rc_div_id_error_message(page_html: str) -> str | None:
    """Texto en <div id=\"idError\">…</div> (el RC lo rellena en fallos de entrega/pagado)."""
    m = re.search(
        r'<div[^>]*\bid\s*=\s*["\']idError["\'][^>]*>(.*?)</div>',
        page_html,
        re.I | re.DOTALL,
    )
    if not m:
        return None
    t = re.sub(r"<[^>]+>", "", m.group(1))
    t = unescape(re.sub(r"\s+", " ", t).strip())
    return t or None


def _numero_documento_parece_telefono_movil_cl(num: str) -> bool:
    """Heurística: N° documento no debe ser un celular chileno (+569… / 569… / 09…)."""
    s = re.sub(r"\s+", "", num.strip())
    if re.match(r"^\+?569\d{8}$", s):
        return True
    if re.match(r"^09\d{8}$", s):
        return True
    return False


def _defuncion_cert_hint_for_id(id_certificado: str) -> str:
    """Aviso cuando el id corresponde a certificado de defunción (validación en el RC, no en este servicio)."""
    if id_certificado.strip() == "3_3_1":
        return (
            "Defunción (idCertificado 3_3_1): el Registro Civil responde con error si ese RUN no tiene "
            "defunción inscrita; no indica fallo de cookies ni de este flujo HTTP."
        )
    return ""


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def _cookies_from_header(cookie_header: str) -> dict[str, str]:
    """Parse cabecera Cookie a dict para httpx.Client (fusiona Set-Cookie en siguientes requests)."""
    out: dict[str, str] = {}
    for raw in cookie_header.split(";"):
        raw = raw.strip()
        if not raw or "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def _browser_headers(
    referer: str | None = None,
    *,
    ajax: bool = False,
    navigation: bool = False,
    navigation_iframe: bool = False,
    form_submission: bool = False,
    iframe_form_post: bool = False,
) -> dict[str, str]:
    """Sin Cookie: el cliente httpx lleva el jar (incluye cookies nuevas del servidor)."""
    h: dict[str, str] = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
        "Origin": BASE,
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    if referer:
        h["Referer"] = referer
    if iframe_form_post:
        # POST entregadocumentos desde iframe del carrito (curl exitoso del navegador).
        h["Sec-Fetch-Dest"] = "iframe"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "same-origin"
        h["Sec-Fetch-User"] = "?1"
        h["Upgrade-Insecure-Requests"] = "1"
        h["Cache-Control"] = "max-age=0"
    elif form_submission:
        # POST de <form> clásico (JSF navegación): sin XHR; algunos endpoints devuelven 404 si ven AJAX.
        h["Sec-Fetch-Dest"] = "document"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "same-origin"
    elif ajax:
        h["Accept"] = "*/*"
        h["X-Requested-With"] = "XMLHttpRequest"
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Site"] = "same-origin"
    elif navigation_iframe:
        # GET agregarACarro embebido como iframe bajo carro.srcei (curl del navegador).
        h["Sec-Fetch-Dest"] = "iframe"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "same-origin"
        h["Sec-Fetch-User"] = "?1"
        h["Upgrade-Insecure-Requests"] = "1"
    elif navigation:
        h["Sec-Fetch-Dest"] = "document"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "same-origin"
        h["Upgrade-Insecure-Requests"] = "1"
    return h


def _step_summary(method: str, url: str, status: int, length: int) -> dict[str, Any]:
    return {"method": method, "url": url, "status_code": status, "body_length": length}


def _decode_html(resp: httpx.Response) -> str:
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("latin-1")


def _clear_bug_dir() -> None:
    """Vaciar `bug/` al arrancar: HTML sueltos y trazas en `bug/requests/`."""
    root = bug_dir()
    root.mkdir(parents=True, exist_ok=True)
    for p in root.iterdir():
        if p.is_file():
            p.unlink()
        elif p.is_dir() and p.name == "requests":
            shutil.rmtree(p, ignore_errors=True)


def _bug_trace_headers_txt(h: httpx.Headers) -> str:
    try:
        lines = [f"{k}: {v}" for k, v in h.multi_items()]
    except Exception:
        try:
            lines = [f"{k}: {v}" for k, v in h.items()]
        except Exception:
            return f"(no se pudieron serializar cabeceras: {h!r})\n"
    return "\n".join(lines) + ("\n" if lines else "")


def _bug_response_set_cookie_block(resp: httpx.Response) -> str:
    """Lista legible de ``Set-Cookie`` (como en Network), una cookie por bloque."""
    try:
        vals = resp.headers.get_list("set-cookie")
    except Exception:
        vals = []
    if not vals:
        one = resp.headers.get("set-cookie")
        return (one + "\n") if one else "(ninguna cabecera Set-Cookie)\n"
    return "\n\n".join(vals) + "\n"


def _bug_response_kind(resp: httpx.Response) -> str:
    """``html`` | ``json`` | ``text`` para nombrar cuerpos como en DevTools."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content or b""
    lead = raw.lstrip()[:1]
    if "html" in ct:
        return "html"
    if "json" in ct or lead == b"{" or lead == b"[":
        return "json"
    return "text"


def _bug_response_body_text(resp: httpx.Response, kind: str) -> str:
    if kind == "html":
        return _decode_html(resp)
    try:
        return (resp.content or b"").decode("utf-8")
    except UnicodeDecodeError:
        return (resp.content or b"").decode("latin-1", errors="replace")


def _bug_response_body_pretty_json(raw_txt: str) -> str | None:
    try:
        obj = json.loads(raw_txt)
    except json.JSONDecodeError:
        return None
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _bug_top_level_suffix(kind: str) -> str:
    if kind == "html":
        return ".html"
    if kind == "json":
        return ".json"
    return ".txt"


def _save_bug_request_trace(safe_step: str, resp: httpx.Response, root: Path) -> None:
    """
    Por cada paso HTTP guarda en ``<root>/requests/<safe_step>/`` cabeceras y cuerpos
    (petición + respuesta) para análisis tipo Network (incl. OrdenDeCompra y agregarACarro).
    """
    req = resp.request
    out = root / "requests" / safe_step
    out.mkdir(parents=True, exist_ok=True)

    http_ver = getattr(resp, "http_version", None) or "?"
    meta = "\n".join(
        [
            f"request_method: {req.method}",
            f"request_url: {req.url}",
            f"response_status: {resp.status_code}",
            f"response_url: {resp.url}",
            f"response_http_version: {http_ver}",
            "",
        ]
    )
    (out / "meta.txt").write_text(meta, encoding="utf-8")

    (out / "request_headers.txt").write_text(
        _bug_trace_headers_txt(req.headers), encoding="utf-8", errors="replace"
    )
    try:
        req_body = req.content if req.content is not None else b""
    except Exception:
        req_body = b""
    if not req_body:
        req_payload = "(vacío: típico en GET)\n"
    else:
        try:
            req_payload = req_body.decode("utf-8")
        except UnicodeDecodeError:
            req_payload = req_body.decode("latin-1", errors="replace")
    (out / "request_body.txt").write_text(req_payload, encoding="utf-8", errors="replace")

    (out / "response_headers.txt").write_text(
        _bug_trace_headers_txt(resp.headers), encoding="utf-8", errors="replace"
    )
    (out / "response_set_cookie.txt").write_text(
        _bug_response_set_cookie_block(resp), encoding="utf-8", errors="replace"
    )

    kind = _bug_response_kind(resp)
    body_txt = _bug_response_body_text(resp, kind)
    if kind == "html":
        (out / "response_body.html").write_text(body_txt, encoding="utf-8", errors="replace")
    elif kind == "json":
        pretty = _bug_response_body_pretty_json(body_txt)
        (out / "response_body.json").write_text(
            pretty if pretty is not None else body_txt,
            encoding="utf-8",
            errors="replace",
        )
        if pretty is not None and pretty.rstrip() != body_txt.rstrip():
            (out / "response_body_raw.txt").write_text(body_txt, encoding="utf-8", errors="replace")
    else:
        (out / "response_body.txt").write_text(body_txt, encoding="utf-8", errors="replace")


def _save_bug_response(step_filename: str, resp: httpx.Response) -> None:
    """
    Guarda la respuesta en ``<bug_dir>/<paso>.{html|json|txt}`` y la traza en
    ``<bug_dir>/requests/<paso>/``. Los fallos aquí no deben tumbar la entrega RC.
    """
    global _bug_dir_logged
    root = bug_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not _bug_dir_logged:
            print(
                f"[certificados] trazas HTTP RC → {root.resolve()}",
                file=sys.stderr,
                flush=True,
            )
            _bug_dir_logged = True
    except Exception as e:
        print(
            f"[certificados] bug: no se pudo crear {root}: {e}\n{traceback.format_exc()}",
            file=sys.stderr,
            flush=True,
        )
        return

    safe = re.sub(r"[^\w.\-]+", "_", step_filename).strip("_") or "response"
    try:
        kind = _bug_response_kind(resp)
        body_txt = _bug_response_body_text(resp, kind)
        suf = _bug_top_level_suffix(kind)
        if kind == "json":
            pretty = _bug_response_body_pretty_json(body_txt)
            top_body = pretty if pretty is not None else body_txt
        else:
            top_body = body_txt
        path = root / f"{safe}{suf}"
        path.write_text(top_body, encoding="utf-8", errors="replace")
    except Exception as e:
        print(
            f"[certificados] bug: no se pudo escribir archivo resumen ({step_filename}): {e}\n"
            f"{traceback.format_exc()}",
            file=sys.stderr,
            flush=True,
        )

    try:
        _save_bug_request_trace(safe, resp, root)
    except Exception as e:
        print(
            f"[certificados] bug: no se pudo escribir requests/{safe} ({step_filename}): {e}\n"
            f"{traceback.format_exc()}",
            file=sys.stderr,
            flush=True,
        )


def _extract_first_form_from_position(html: str, start: int) -> str | None:
    open_end = html.find(">", start)
    if open_end == -1:
        return None
    open_end += 1
    end = html.find("</form>", open_end)
    if end == -1:
        return None
    return html[start : end + len("</form>")]


class _FormFieldsParser(HTMLParser):
    """Inputs hidden y submit del formulario (JSF / PrimeFaces)."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: v or "" for k, v in attrs}
        if tag == "input":
            t = ad.get("type", "").lower()
            name = ad.get("name")
            if not name:
                return
            if t == "hidden":
                self.fields[name] = ad.get("value") or ""
            elif t in ("submit", "image", "button"):
                self.fields[name] = ad.get("value") or ""
        elif tag == "button":
            name = ad.get("name")
            if name:
                self.fields[name] = ad.get("value") or ""


def _parse_form_fields(fragment: str) -> dict[str, str]:
    p = _FormFieldsParser()
    p.feed(fragment)
    return dict(p.fields)


def _regex_collect_hidden_inputs(html: str) -> dict[str, str]:
    """Extrae input hidden: orden de atributos libre, type con o sin comillas."""
    fields: dict[str, str] = {}
    for m in re.finditer(r"<input\b([^>]*)/?>", html, re.I):
        tag = m.group(1)
        tm = re.search(r"\btype\s*=\s*(?:[\"']([^\"']+)[\"']|(\S+))", tag, re.I)
        if not tm:
            continue
        tval = (tm.group(1) or tm.group(2) or "").strip().lower()
        if tval != "hidden":
            continue
        nm = re.search(r"\bname\s*=\s*(?:[\"']([^\"']+)[\"']|(\S+))", tag, re.I)
        if not nm:
            continue
        name = (nm.group(1) or nm.group(2) or "").strip()
        if not name:
            continue
        vm = re.search(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        val = vm.group(1) if vm else ""
        fields[name] = val
    return fields


def _hidden_fields_from_fragment(frag: str) -> dict[str, str]:
    """Parser + regex; el parser gana en claves repetidas."""
    rx = _regex_collect_hidden_inputs(frag)
    pr = _parse_form_fields(frag)
    return {**rx, **pr}


def _inject_viewstate_from_html(full_html: str, fields: dict[str, str]) -> dict[str, str]:
    if any("viewstate" in k.lower() for k in fields):
        return fields
    out = dict(fields)
    for pat in (
        r'name\s*=\s*["\']javax\.faces\.ViewState["\']\s+value\s*=\s*["\']([^"\']*)["\']',
        r'value\s*=\s*["\']([^"\']*)["\']\s+name\s*=\s*["\']javax\.faces\.ViewState["\']',
        r'<textarea[^>]+name\s*=\s*["\']javax\.faces\.ViewState["\'][^>]*>([^<]*)</textarea>',
    ):
        m = re.search(pat, full_html, re.I | re.DOTALL)
        if m:
            out["javax.faces.ViewState"] = (m.group(1) or "").strip()
            break
    return out


def _merge_orden_submit_from_orden_form(html: str, page_url: str, body: dict[str, str]) -> dict[str, str]:
    """
    El regex de hiddens no incluye botones submit/image; JSF suele exigir el par name=value
    del comando usado. Añade claves que salgan del parser del <form> de OrdenDeCompra.
    """
    form_html, _ = _find_form_by_action_pattern(html, r"OrdenDeCompra")
    if not form_html:
        form_html = _extract_form_html_ci(html, "OrdenDeCompra.srcei") or _extract_form_html_ci(
            html, "OrdenDeCompra"
        )
    if not form_html:
        return body
    if _is_entrega_only_action(page_url, _form_open_tag_action(form_html)):
        return body
    parsed = _parse_form_fields(form_html)
    out = dict(body)
    for k, v in parsed.items():
        if k not in out:
            out[k] = v
    return out


def _supplement_orden_fields_from_largest_cart_form(
    html: str,
    page_url: str,
    fields: dict[str, str],
    *,
    min_keys: int = 4,
) -> dict[str, str]:
    """
    Si el POST de orden quedó casi vacío, mezcla los hidden del <form> no-entrega
    con más campos (suele ser el carrito real donde el parser falló en el action).
    """
    if len(fields) >= min_keys:
        return _inject_viewstate_from_html(html, fields)
    best: dict[str, str] = {}
    best_n = 0
    for m in re.finditer(r"<form\b", html, re.I):
        frag = _extract_first_form_from_position(html, m.start())
        if not frag:
            continue
        if _is_entrega_only_action(page_url, _form_open_tag_action(frag)):
            continue
        if _form_is_entrega_delivery_fragment(frag):
            continue
        cand = _regex_collect_hidden_inputs(frag)
        if "paises" in cand:
            cand = {k: v for k, v in cand.items() if k != "paises"}
        if len(cand) > best_n:
            best_n = len(cand)
            best = cand
    # Un <form> pequeño (p. ej. 1 hidden) puede ganar el bucle anterior; el regex sobre
    # toda la página suele ver más campos (p. ej. 12) y es imprescindible para el POST.
    whole = _regex_collect_hidden_inputs(html)
    if "paises" in whole:
        whole = {k: v for k, v in whole.items() if k != "paises"}
    if len(whole) > best_n:
        best = whole
        best_n = len(whole)

    merged = dict(best)
    merged.update(fields)
    return _inject_viewstate_from_html(html, merged)


def _parse_form_action_attr(form_html: str) -> str | None:
    m = re.search(r"<form\b[^>]*\baction\s*=\s*(['\"])(.*?)\1", form_html, re.I | re.DOTALL)
    return m.group(2).strip() if m else None


def _extract_form_html_ci(html: str, needle: str) -> str | None:
    """Localiza `needle` sin distinguir mayúsculas; devuelve el <form>...</form> que lo contiene."""
    hl = html.lower()
    i = hl.find(needle.lower())
    if i == -1:
        return None
    start = html.rfind("<form", 0, i)
    if start == -1:
        return None
    end = html.find("</form>", i)
    if end == -1:
        return None
    return html[start : end + len("</form>")]


def _find_form_by_action_pattern(html: str, action_regex: str) -> tuple[str | None, str | None]:
    """Primer <form> cuyo atributo action coincide con action_regex (sin distinguir mayúsculas)."""
    rx = re.compile(action_regex, re.I)
    for m in re.finditer(r"<form\b", html, re.I):
        start = m.start()
        tag_end = html.find(">", start)
        if tag_end == -1:
            continue
        tag = html[start : tag_end + 1]
        am = re.search(r"\baction\s*=\s*(['\"])(.*?)\1", tag, re.I | re.DOTALL)
        action = am.group(2).strip() if am else ""
        if not action or not rx.search(action):
            continue
        frag = _extract_first_form_from_position(html, start)
        if frag:
            return frag, action
    return None, None


def _best_jsf_form(html: str) -> str | None:
    """El <form> con más inputs hidden; se favorece el que contiene ViewState."""
    best_frag: str | None = None
    best_score = 0
    for m in re.finditer(r"<form\b", html, re.I):
        frag = _extract_first_form_from_position(html, m.start())
        if not frag:
            continue
        fields = _parse_form_fields(frag)
        n = len(fields)
        if n == 0:
            continue
        vs_bonus = 80 if "ViewState" in frag else 0
        score = n + vs_bonus
        if score > best_score:
            best_score = score
            best_frag = frag
    return best_frag


def _form_open_tag_action(form_html: str) -> str:
    m = re.search(r"<form\b[^>]*\baction\s*=\s*(['\"])(.*?)\1", form_html, re.I | re.DOTALL)
    return (m.group(2).strip() if m else "") or ""


def _cart_has_certificate_items(html: str) -> bool:
    """True si el HTML del carrito muestra al menos un certificado agregado."""
    low = html.lower()
    return "carro_certificado_" in low or "carro_nombrecertificado_" in low


def _form_is_entrega_delivery_fragment(form_html: str) -> bool:
    """Formulario POST a entregadocumentos.srcei (no OrdenDeCompra ni el listado de países)."""
    low = form_html.lower()
    if "idcontinuarentregadocumentos" in low:
        return True
    act = _form_open_tag_action(form_html).lower()
    return "entregadocumentos" in act and "ordendecompra" not in act


def _html_rc_error_messages(html: str) -> list[str]:
    """Mensajes visibles de error del RC en la página."""
    msgs: list[str] = []
    for m in re.finditer(
        r'<div[^>]*\bid\s*=\s*["\']idError["\'][^>]*>(.*?)</div>',
        html,
        re.I | re.DOTALL,
    ):
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = unescape(re.sub(r"\s+", " ", t).strip())
        if t:
            msgs.append(t)
    low = html.lower()
    if "debe ingresar al menos un certificado" in low:
        msgs.append("Debe ingresar al menos un certificado")
    return msgs


def _is_entrega_only_action(page_url: str, raw_action: str) -> bool:
    """True si el action resuelve a entrega y no al paso Orden de compra del carrito."""
    if not raw_action or raw_action.strip() in ("#",):
        return False
    resolved = _resolve_form_post_url(page_url, raw_action, "").lower()
    act = raw_action.lower()
    if "ordendecompra" in resolved or "ordendecompra" in act:
        return False
    if "entregadocumentos" in resolved or "entregadocumentos" in act:
        return True
    return False


def _best_jsf_form_for_orden(html: str, page_url: str, fallback_orden: str) -> str | None:
    """
    Elige formulario para POST OrdenDeCompra: ignora solo-entrega; exige action con
    OrdenDeCompra o, si no hay, al menos varios hidden (evita formularios de 1 campo).
    """
    best_frag: str | None = None
    best_score = -1
    for m in re.finditer(r"<form\b", html, re.I):
        frag = _extract_first_form_from_position(html, m.start())
        if not frag:
            continue
        raw_action = _form_open_tag_action(frag)
        if _is_entrega_only_action(page_url, raw_action):
            continue
        resolved = _resolve_form_post_url(page_url, raw_action or None, fallback_orden).lower()
        ra = raw_action.lower()
        explicit_orden = "ordendecompra" in ra or "ordendecompra" in resolved

        fields = _hidden_fields_from_fragment(frag)
        n = len(fields)
        if n == 0:
            continue
        if not explicit_orden and n < 6:
            continue

        vs_bonus = 120 if any("viewstate" in k.lower() for k in fields) else 0
        orden_bonus = 400 if explicit_orden else 0
        score = n + vs_bonus + orden_bonus
        if score > best_score:
            best_score = score
            best_frag = frag

    if best_frag is None:
        for m in re.finditer(r"<form\b", html, re.I):
            frag = _extract_first_form_from_position(html, m.start())
            if not frag:
                continue
            raw_action = _form_open_tag_action(frag)
            if _is_entrega_only_action(page_url, raw_action):
                continue
            fields = _hidden_fields_from_fragment(frag)
            n = len(fields)
            if n == 0:
                continue
            vs_bonus = 80 if any("viewstate" in k.lower() for k in fields) else 0
            score = n + vs_bonus
            if score > best_score:
                best_score = score
                best_frag = frag

    return best_frag


def _resolve_form_post_url(page_url: str, action: str | None, fallback_absolute: str) -> str:
    if not action:
        return fallback_absolute
    a = action.strip()
    if not a or a == "#":
        return fallback_absolute
    if a.lower().startswith("http"):
        return a
    return urljoin(page_url, a)


def _pick_orden_from_carro_html(html_carro: str, page_url: str, fallback_orden: str) -> tuple[dict[str, str], str]:
    """
    Campos POST y URL absoluta para el paso orden de compra.
    Orden: action OrdenDeCompra → needle en HTML → mejor form JSF del carrito (filtrado).
    """
    form_html, action = _find_form_by_action_pattern(html_carro, r"OrdenDeCompra")
    if form_html:
        fields = _inject_viewstate_from_html(
            html_carro, _hidden_fields_from_fragment(form_html)
        )
        return fields, _resolve_form_post_url(page_url, action, fallback_orden)

    form_html = _extract_form_html_ci(html_carro, "OrdenDeCompra.srcei") or _extract_form_html_ci(
        html_carro, "OrdenDeCompra"
    )
    if form_html:
        fields = _inject_viewstate_from_html(html_carro, _hidden_fields_from_fragment(form_html))
        act = _parse_form_action_attr(form_html)
        return fields, _resolve_form_post_url(page_url, act, fallback_orden)

    form_html = _best_jsf_form_for_orden(html_carro, page_url, fallback_orden)
    if form_html:
        fields = _inject_viewstate_from_html(html_carro, _hidden_fields_from_fragment(form_html))
        act = _parse_form_action_attr(form_html)
        url = _resolve_form_post_url(page_url, act, fallback_orden)
        if "ordendecompra" not in url.lower():
            url = fallback_orden
        return fields, url

    return _inject_viewstate_from_html(html_carro, {}), fallback_orden


def _orden_response_is_json_lista_certificado(html: str) -> bool:
    """OrdenDeCompra a veces responde JSON (AJAX) en lugar de HTML con <form> de entrega."""
    s = html.lstrip()
    return s.startswith("{") and '"listaCertificadoString"' in s


def _pick_entrega_from_html(html: str, page_url: str, fallback_entrega: str) -> tuple[dict[str, str], str]:
    frag = _extract_form_html_ci(html, "idContinuarEntregaDocumentos") or _extract_form_html_ci(
        html, "entregadocumentos.srcei"
    )
    if frag:
        act = _parse_form_action_attr(frag)
        return _parse_form_fields(frag), _resolve_form_post_url(page_url, act, fallback_entrega)

    form_html, action = _find_form_by_action_pattern(html, r"entregadocumentos\.srcei")
    if form_html:
        return _parse_form_fields(form_html), _resolve_form_post_url(page_url, action, fallback_entrega)

    form_html = _extract_form_html_ci(html, "entregadocumentos.srcei") or _extract_form_html_ci(
        html, "entregadocumentos"
    )
    if form_html:
        fields = _parse_form_fields(form_html)
        act = _parse_form_action_attr(form_html)
        return fields, _resolve_form_post_url(page_url, act, fallback_entrega)

    form_html = _best_jsf_form(html)
    if form_html:
        fields = _parse_form_fields(form_html)
        act = _parse_form_action_attr(form_html)
        return fields, _resolve_form_post_url(page_url, act, fallback_entrega)

    return {}, fallback_entrega


def _build_entrega_delivery_body(
    *,
    ev_matrimonio: bool,
    email: str,
    numero: str,
    run: str,
    run_consulta: str,
    run_solicitante: str,
) -> dict[str, str]:
    """
    Campos del POST real a entregadocumentos.srcei (form ``idContinuarEntregaDocumentos``).

    Curl exitoso del RC (9 campos, sin carro_runConsulta ni carro_solicitanteInput*):
    carro_email, carro_emailConfirm, carro_telefono, carro_email2, carro_emailConfirm2,
    runSol, numeroDocumentoSol, nameCheckApostilla, namePaisApostilla.
    """
    del ev_matrimonio, run_consulta, run_solicitante  # matrimonio/ley 19628 van en UI del carrito, no en este POST
    email_c = settings.entrega_email_confirm.strip() or email
    email2 = settings.entrega_email2.strip()
    email_c2 = settings.entrega_email_confirm2.strip() or email2
    ndoc = _numero_documento_para_rc(run, numero)
    return {
        "carro_email": email,
        "carro_emailConfirm": email_c,
        "carro_telefono": settings.entrega_telefono.strip(),
        "carro_email2": email2,
        "carro_emailConfirm2": email_c2,
        "runSol": run.strip(),
        "numeroDocumentoSol": ndoc,
        "nameCheckApostilla": settings.default_name_check_apostilla.strip() or "false",
        "namePaisApostilla": settings.default_name_pais_apostilla.strip() or "-1",
    }


def _page_signals(html: str) -> dict[str, Any]:
    """Indicios sobre el HTML recibido (sin volcar el documento completo)."""
    low = html.lower()
    return {
        "forms": len(re.findall(r"<form\b", html, re.I)),
        "hidden_inputs_guess": len(
            re.findall(r'<input[^>]+type\s*=\s*["\']?hidden\b', html, re.I)
        ),
        "has_viewstate_text": "viewstate" in low or "javax.faces" in low,
        "likely_challenge_page": any(
            p in low
            for p in (
                "desafío",
                "desafio",
                "captcha",
                "código de soporte",
                "codigo de soporte",
                "resolver el desafío",
                "resolver el desafio",
                "audio is not supported",
                "cuál es el código",
                "cual es el codigo",
            )
        ),
        "title": (m.group(1).strip()[:200] if (m := re.search(r"<title[^>]*>([^<]*)</title>", html, re.I | re.S)) else ""),
    }


def _pack_certificado_specs() -> list[tuple[str, str, str]]:
    """
    (tipo, id_certificado, entrega_variant) del pack por defecto.

    Los ids no vienen de documentación pública del RC: se leen del sitio al agregar al carro.
    Ver ``_como_obtener_id_certificado_rc()`` en el docstring del POST /registrocivil/entrega-certificado.
    """
    specs: list[tuple[str, str, str]] = [
        ("nacimiento", settings.default_pack_id_nacimiento.strip(), "standard"),
        ("defuncion", settings.default_pack_id_defuncion.strip(), "standard"),
        ("matrimonio", settings.default_pack_id_matrimonio.strip(), "matrimonio"),
    ]
    auc_id = settings.default_pack_id_union_civil.strip()
    if auc_id:
        ev = settings.default_pack_union_civil_entrega_variant.strip().lower() or "matrimonio"
        specs.append(("union_civil", auc_id, ev))
    return specs


def _pack_ui_label() -> str:
    tipos = [t for t, _, _ in _pack_certificado_specs()]
    return "pack: " + " + ".join(tipos)


def _pack_result_rows(
    agregar_results: list[dict[str, Any]],
    *,
    entrega_ok: bool,
) -> list[dict[str, Any]]:
    """Una fila por tipo; ``ok`` = agregado al carrito y entrega global exitosa."""
    rows: list[dict[str, Any]] = []
    for ar in agregar_results:
        ok_ag = bool(ar.get("ok_agregar"))
        rows.append(
            {
                "tipo": ar.get("tipo"),
                "id_certificado": ar.get("id_certificado"),
                "ok": ok_ag and entrega_ok,
                "ok_agregar": ok_ag,
                "entrega_ok": entrega_ok,
                "rc_mensaje_agregar": ar.get("rc_mensaje"),
                "error": ar.get("error"),
            }
        )
    return rows


def _run_pack_certificate_flow(
    client: httpx.Client,
    *,
    run: str,
    email: str,
    numero: str,
    filtro: str,
    run_consulta: str,
    run_solicitante: str,
    selenium_used: bool,
    bug_prefix: str = "pack",
) -> dict[str, Any]:
    """
    Agrega nacimiento, defunción y matrimonio al mismo carrito; un solo POST de entrega.
    Si defunción o matrimonio no aplican, se registra el fallo y se sigue con el resto.
    """
    del selenium_used

    def _bug(step: str) -> str:
        return f"{bug_prefix}_{step}" if bug_prefix else step

    url_carro = f"{OFICINA}/carro.srcei"
    steps: list[dict[str, Any]] = []
    agregar_results: list[dict[str, Any]] = []

    r0 = client.get(
        url_carro,
        headers=_browser_headers(referer=REF_SERVICIOS_LINEA, navigation=True),
    )
    _save_bug_response(_bug("00_GET_carro"), r0)
    steps.append(_step_summary("GET", str(r0.request.url), r0.status_code, len(r0.content)))
    referer_carro = str(r0.url)
    html_carro = _decode_html(r0)
    html_agregar_last = ""

    if _is_rc_waf_or_non_jsf_shell_html(html_carro):
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r0.status_code,
            "pack_tres_certificados": True,
            "results": [],
            "agregar_results": [],
            "meta": {
                "hint": "Carrito RC bloqueado (WAF/captcha).",
                "rc_pantalla_waf_sin_carrito_jsf": True,
            },
        }

    for tipo, cid, ev in _pack_certificado_specs():
        row: dict[str, Any] = {
            "tipo": tipo,
            "id_certificado": cid,
            "entrega_variant": ev,
            "ok_agregar": False,
            "rc_mensaje": None,
        }
        try:
            r1 = client.get(
                URL_AGREGAR_ACARRO,
                params={"filtro": filtro, "idCertificado": cid, "run": run},
                headers=_browser_headers(referer=referer_carro, navigation_iframe=True),
            )
            _save_bug_response(_bug(f"agregar_{tipo}"), r1)
            steps.append(_step_summary("GET", str(r1.request.url), r1.status_code, len(r1.content)))
            html_ag = _decode_html(r1)
            html_agregar_last = html_ag
            rc_msg = _extract_agregar_carro_mensaje(html_ag)
            row["rc_mensaje"] = rc_msg
            row["ok_agregar"] = rc_msg is None

            r_carro = client.get(
                url_carro,
                headers=_browser_headers(referer=str(r1.url), navigation=True),
            )
            _save_bug_response(_bug(f"carro_tras_{tipo}"), r_carro)
            steps.append(
                _step_summary("GET", str(r_carro.request.url), r_carro.status_code, len(r_carro.content))
            )
            html_carro = _decode_html(r_carro)
            referer_carro = str(r_carro.url)
            if row["ok_agregar"] and not _cart_has_certificate_items(html_carro):
                row["ok_agregar"] = False
                row["rc_mensaje"] = row["rc_mensaje"] or "No apareció en el carrito tras agregar"
        except Exception as e:
            row["ok_agregar"] = False
            row["error"] = f"{type(e).__name__}: {e}"
        agregar_results.append(row)

    tiene_items = _cart_has_certificate_items(html_carro)
    ok_any_agregar = any(r.get("ok_agregar") for r in agregar_results)

    if not tiene_items or not ok_any_agregar:
        hint = "Ningún certificado quedó en el carrito (defunción/matrimonio suelen fallar si no aplican)."
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r0.status_code,
            "pack_tres_certificados": True,
            "pack_ok_completo": False,
            "pack_ok_nacimiento": False,
            "results": _pack_result_rows(agregar_results, entrega_ok=False),
            "agregar_results": agregar_results,
            "meta": {
                "hint": hint,
                "carro_tiene_items": tiene_items,
                "agregar_exitosos": [r["tipo"] for r in agregar_results if r.get("ok_agregar")],
            },
        }

    url_orden_default = f"{OFICINA}/OrdenDeCompra.srcei"
    orden_body, url_orden, orden_html_source = _pick_orden_first_hit(
        [(html_carro, referer_carro, "carro"), (html_agregar_last, referer_carro, "agregar")],
        url_orden_default,
    )
    orden_body = _supplement_orden_fields_from_largest_cart_form(
        html_carro, referer_carro, orden_body, min_keys=4
    )
    orden_body = _merge_orden_submit_from_orden_form(html_carro, referer_carro, orden_body)
    if "paises" in orden_body:
        orden_body = {k: v for k, v in orden_body.items() if k != "paises"}

    r2 = client.get(url_orden, headers=_browser_headers(referer=referer_carro, ajax=True))
    _save_bug_response(_bug("OrdenDeCompra"), r2)
    steps.append(_step_summary("GET", url_orden, r2.status_code, len(r2.content)))
    html_post_orden = _decode_html(r2)
    if _orden_response_is_json_lista_certificado(html_post_orden):
        r_carro2 = client.get(
            url_carro,
            headers=_browser_headers(referer=referer_carro, navigation=True),
        )
        _save_bug_response(_bug("carro_post_orden"), r_carro2)
        steps.append(
            _step_summary("GET", str(r_carro2.request.url), r_carro2.status_code, len(r_carro2.content))
        )
        html_carro = _decode_html(r_carro2)
        referer_carro = str(r_carro2.url)

    url_entrega_default = f"{OFICINA}/entregadocumentos.srcei"
    url_pagado = f"{OFICINA}/entregadocumentosfreePagado.srcei"
    entrega_hidden, url_entrega = _pick_entrega_from_html(
        html_carro, referer_carro, url_entrega_default
    )
    entrega_body = _build_entrega_delivery_body(
        ev_matrimonio=False,
        email=email,
        numero=numero,
        run=run,
        run_consulta=run_consulta,
        run_solicitante=run_solicitante,
    )
    ndoc_warn = _aviso_numero_documento_invalido(run, numero)
    if ndoc_warn:
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r2.status_code,
            "pack_tres_certificados": True,
            "results": _pack_result_rows(agregar_results, entrega_ok=False),
            "agregar_results": agregar_results,
            "meta": {"hint": ndoc_warn, "carro_tiene_items": True},
        }

    r3 = client.post(
        url_entrega,
        data=entrega_body,
        follow_redirects=False,
        headers={
            **_browser_headers(referer=referer_carro, iframe_form_post=True),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    _save_bug_response(_bug("POST_entregadocumentos"), r3)
    steps.append(_step_summary("POST", url_entrega, r3.status_code, len(r3.content)))
    entrega_redirect_carro = r3.status_code in (301, 302, 303, 307, 308) and "carro.srcei" in (
        (r3.headers.get("location") or "").lower()
    )

    r4 = client.get(
        url_pagado,
        headers=_browser_headers(referer=referer_carro, navigation_iframe=True),
    )
    _save_bug_response(_bug("GET_freePagado"), r4)
    steps.append(_step_summary("GET", url_pagado, r4.status_code, len(r4.content)))

    html_entrega = _decode_html(r3)
    html_pagado = _decode_html(r4)
    rc_err_entrega = _extract_rc_div_id_error_message(html_entrega)
    rc_err_pagado = _extract_rc_div_id_error_message(html_pagado)
    entrega_ok = (
        all(s["status_code"] < 400 for s in steps)
        and not entrega_redirect_carro
        and not rc_err_entrega
        and not rc_err_pagado
    )

    hint = ""
    if entrega_redirect_carro:
        hint = "POST entregadocumentos redirigió al carrito."
    if rc_err_entrega:
        hint = f"{hint} RC (entrega): {rc_err_entrega}".strip()
    if rc_err_pagado:
        hint = f"{hint} RC (pagado): {rc_err_pagado}".strip()
    fallidos = [r["tipo"] for r in agregar_results if not r.get("ok_agregar")]
    if fallidos:
        hint = f"{hint} No agregados al carrito: {', '.join(fallidos)}.".strip()

    ok_any = entrega_ok and ok_any_agregar
    ok_all_agregar = all(r.get("ok_agregar") for r in agregar_results)
    nac_ok = any(r.get("tipo") == "nacimiento" and r.get("ok_agregar") for r in agregar_results)

    return {
        "ok": ok_any,
        "steps": steps,
        "last_status_code": r4.status_code,
        "pack_tres_certificados": True,
        "pack_ok_completo": ok_all_agregar and entrega_ok,
        "pack_ok_nacimiento": nac_ok and entrega_ok,
        "entrega_ok": entrega_ok,
        "results": _pack_result_rows(agregar_results, entrega_ok=entrega_ok),
        "agregar_results": agregar_results,
        "meta": {
            "hint": hint,
            "carro_tiene_items": tiene_items,
            "entrega_email_enviado": email,
            "rc_mensaje_id_error_entrega": rc_err_entrega,
            "rc_mensaje_id_error_pagado": rc_err_pagado,
            "agregar_exitosos": [r["tipo"] for r in agregar_results if r.get("ok_agregar")],
            "agregar_fallidos": fallidos,
            "orden_html_source": orden_html_source,
            "entrega_total_fields": len(entrega_body),
        },
    }


def _pick_orden_first_hit(
    pairs: list[tuple[str, str, str]],
    fallback_orden: str,
) -> tuple[dict[str, str], str, str]:
    """
    Prueba varios HTML hasta obtener campos POST (carrito tras agregar, respuesta agregar, etc.).
    Devuelve (fields, post_url, source_label).
    """
    for html, page_url, label in pairs:
        fields, url = _pick_orden_from_carro_html(html, page_url, fallback_orden)
        if fields:
            return fields, url, label
    if pairs:
        fields, url = _pick_orden_from_carro_html(pairs[0][0], pairs[0][1], fallback_orden)
        return fields, url, "none"
    return {}, fallback_orden, "none"


def _run_one_certificate_flow(
    client: httpx.Client,
    *,
    run: str,
    email: str,
    numero: str,
    filtro: str,
    id_certificado: str,
    entrega_variant: str,
    run_consulta: str,
    run_solicitante: str,
    selenium_used: bool,
    skip_http_repeat_agregar: bool,
    bug_prefix: str,
) -> dict[str, Any]:
    """Una pasada completa: agregar+carro → orden → entrega → freePagado."""
    ev_mat = entrega_variant.strip().lower() == "matrimonio"

    def _bug(step: str) -> str:
        return f"{bug_prefix}_{step}" if bug_prefix else step

    params_agregar = {"filtro": filtro, "idCertificado": id_certificado, "run": run}
    url_agregar = URL_AGREGAR_ACARRO
    url_carro = f"{OFICINA}/carro.srcei"
    steps: list[dict[str, Any]] = []

    if skip_http_repeat_agregar:
        r_carro = client.get(
            url_carro,
            headers=_browser_headers(referer=REF_SERVICIOS_LINEA, navigation_iframe=True),
        )
        _save_bug_response(_bug("01_GET_carro"), r_carro)
        steps.append(_step_summary("GET", str(r_carro.request.url), r_carro.status_code, len(r_carro.content)))
        referer_carro = str(r_carro.url)
        referer_agregar = ref_carro
        html_agregar = ""
    else:
        r0 = client.get(
            url_carro,
            headers=_browser_headers(referer=REF_SERVICIOS_LINEA, navigation=True),
        )
        _save_bug_response(_bug("01_GET_carro"), r0)
        steps.append(_step_summary("GET", str(r0.request.url), r0.status_code, len(r0.content)))
        url_carro_para_agregar = str(r0.url)

        r1 = client.get(
            url_agregar,
            params=params_agregar,
            headers=_browser_headers(referer=url_carro_para_agregar, navigation_iframe=True),
        )
        _save_bug_response(_bug("02_GET_agregarACarro"), r1)
        steps.append(_step_summary("GET", str(r1.request.url), r1.status_code, len(r1.content)))
        referer_agregar = str(r1.url)

        r_carro = client.get(
            url_carro,
            headers=_browser_headers(referer=referer_agregar, navigation=True),
        )
        _save_bug_response(_bug("03_GET_carro"), r_carro)
        steps.append(_step_summary("GET", str(r_carro.request.url), r_carro.status_code, len(r_carro.content)))
        referer_carro = str(r_carro.url)
        html_agregar = _decode_html(r1)

    html_carro = _decode_html(r_carro)
    rc_agregar = _extract_agregar_carro_mensaje(html_agregar) if html_agregar.strip() else None
    # El XHR del navegador en agregarACarro usa /web/OrdenDeCompra.srcei (no /carrito/…).
    url_orden_default = f"{OFICINA}/OrdenDeCompra.srcei"
    if _is_rc_waf_or_non_jsf_shell_html(html_carro):
        hint = (
            "El GET carro no es el carrito JSF (WAF TSPD: captcha, bloqueo o HTML sin formularios). "
            "Aumenta REGISTROCIVIL_SELENIUM_TIMEOUT_SEC, prueba REGISTROCIVIL_SELENIUM_HEADLESS=false, "
            "o envía X-RegistroCivil-Cookie copiada **después** del desafío en el navegador. "
            "Si ves selenium_error y sigue fallando, la cookie de .env puede estar caduca: reemplázala o quita REGISTROCIVIL_COOKIE para forzar solo Selenium."
        )
        meta = {
            "id_certificado": id_certificado.strip(),
            "filtro": filtro.strip(),
            "orden_post_url": url_orden_default,
            "orden_fields": 0,
            "orden_hidden_regex_fullpage": 0,
            "orden_html_source": "waf_sin_jsf_carrito",
            "orden_has_viewstate": False,
            "entrega_post_url": f"{OFICINA}/entregadocumentos.srcei",
            "entrega_variant": entrega_variant.strip().lower() or "standard",
            "entrega_hidden_fields": 0,
            "entrega_total_fields": 0,
            "page_signals_carro": _page_signals(html_carro),
            "page_signals_agregar": _page_signals(html_agregar) if html_agregar.strip() else {},
            "run_en_solicitud": run.strip(),
            "rc_mensaje_agregar": rc_agregar,
            "rc_mensaje_id_error_entrega": None,
            "rc_mensaje_id_error_pagado": None,
            "rc_pantalla_waf_sin_carrito_jsf": True,
            "hint": hint,
        }
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r_carro.status_code,
            "meta": meta,
        }

    orden_body, url_orden, orden_html_source = _pick_orden_first_hit(
        [
            (html_carro, referer_carro, "carro"),
            (html_agregar, referer_agregar, "agregar"),
        ],
        url_orden_default,
    )
    orden_body = _supplement_orden_fields_from_largest_cart_form(
        html_carro, referer_carro, orden_body, min_keys=4
    )
    orden_body = _merge_orden_submit_from_orden_form(html_carro, referer_carro, orden_body)
    if "paises" in orden_body:
        orden_body = {k: v for k, v in orden_body.items() if k != "paises"}

    tiene_items = _cart_has_certificate_items(html_carro)
    orden_method = "GET"
    if tiene_items:
        # Mismo paso que getOrdenDeCompra() en el navegador (XHR), sin el formulario gigante de países.
        r2 = client.get(
            url_orden,
            headers=_browser_headers(referer=referer_carro, ajax=True),
        )
    else:
        orden_method = "POST"
        r2 = client.post(
            url_orden,
            data=orden_body,
            headers={
                **_browser_headers(referer=referer_carro, form_submission=True),
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
    _save_bug_response(
        _bug("02_POST_OrdenDeCompra" if skip_http_repeat_agregar else "04_POST_OrdenDeCompra"),
        r2,
    )
    steps.append(_step_summary(orden_method, url_orden, r2.status_code, len(r2.content)))
    referer_orden_resp = str(r2.url)

    html_post_orden = _decode_html(r2)
    if tiene_items and _orden_response_is_json_lista_certificado(html_post_orden):
        r_carro2 = client.get(
            url_carro,
            headers=_browser_headers(referer=referer_carro, navigation=True),
        )
        _save_bug_response(_bug("03b_GET_carro_post_orden"), r_carro2)
        steps.append(
            _step_summary("GET", str(r_carro2.request.url), r_carro2.status_code, len(r_carro2.content))
        )
        html_carro = _decode_html(r_carro2)
        referer_carro = str(r_carro2.url)
    url_entrega_default = f"{OFICINA}/entregadocumentos.srcei"
    url_pagado = f"{OFICINA}/entregadocumentosfreePagado.srcei"
    if _orden_response_is_json_lista_certificado(html_post_orden) or tiene_items:
        entrega_hidden, url_entrega = _pick_entrega_from_html(
            html_carro, referer_carro, url_entrega_default
        )
    else:
        entrega_hidden, url_entrega = _pick_entrega_from_html(
            html_post_orden, referer_orden_resp, url_entrega_default
        )

    delivery = _build_entrega_delivery_body(
        ev_matrimonio=ev_mat,
        email=email,
        numero=numero,
        run=run,
        run_consulta=run_consulta,
        run_solicitante=run_solicitante,
    )
    # El navegador solo envía los 9 campos del form oculto (curl exitoso); no mezclar extras del HTML.
    entrega_body = delivery
    ndoc_warn = _aviso_numero_documento_invalido(run, numero)
    if ndoc_warn:
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r2.status_code,
            "meta": {
                "id_certificado": id_certificado.strip(),
                "hint": ndoc_warn,
                "carro_tiene_items": tiene_items,
            },
        }
    if not (entrega_body.get("carro_email") or "").strip():
        meta = {
            "id_certificado": id_certificado.strip(),
            "hint": "El POST de entrega iría sin correo: revisa REGISTROCIVIL_DEFAULT_EMAIL en .env.",
            "entrega_total_fields": len(entrega_body),
            "carro_tiene_items": tiene_items,
        }
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r2.status_code,
            "meta": meta,
        }
    if not tiene_items:
        hint_empty = "El carrito no tiene certificados tras agregarACarro; el RC no enviará correo."
        return {
            "ok": False,
            "steps": steps,
            "last_status_code": r2.status_code,
            "meta": {
                "id_certificado": id_certificado.strip(),
                "hint": hint_empty,
                "rc_mensaje_agregar": rc_agregar,
                "carro_tiene_items": False,
            },
        }

    r3 = client.post(
        url_entrega,
        data=entrega_body,
        follow_redirects=False,
        headers={
            **_browser_headers(referer=referer_carro, iframe_form_post=True),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    _save_bug_response(
        _bug("03_POST_entregadocumentos" if skip_http_repeat_agregar else "05_POST_entregadocumentos"),
        r3,
    )
    steps.append(_step_summary("POST", url_entrega, r3.status_code, len(r3.content)))
    entrega_redirect_carro = r3.status_code in (301, 302, 303, 307, 308) and "carro.srcei" in (
        (r3.headers.get("location") or "").lower()
    )
    referer_entrega = str(r3.url) if r3.status_code == 200 else url_entrega

    r4 = client.get(
        url_pagado,
        headers=_browser_headers(referer=referer_carro, navigation_iframe=True),
    )
    _save_bug_response(
        _bug("04_GET_entregadocumentosfreePagado" if skip_http_repeat_agregar else "06_GET_entregadocumentosfreePagado"),
        r4,
    )
    steps.append(_step_summary("GET", url_pagado, r4.status_code, len(r4.content)))

    html_entrega = _decode_html(r3)
    html_pagado = _decode_html(r4)
    rc_err_entrega = _extract_rc_div_id_error_message(html_entrega)
    rc_err_pagado = _extract_rc_div_id_error_message(html_pagado)

    ok_http = all(s["status_code"] < 400 for s in steps)
    ok_agregar = rc_agregar is None
    ok = ok_http and ok_agregar and tiene_items and not entrega_redirect_carro
    if rc_err_entrega:
        ok = False
    if rc_err_pagado:
        ok = False
    rc_msgs = _html_rc_error_messages(html_entrega) + _html_rc_error_messages(html_pagado)
    if any("debe ingresar al menos un certificado" in m.lower() for m in rc_msgs):
        ok = False

    hint = ""
    if rc_agregar:
        hint = f"agregarACarro: {rc_agregar}. El certificado no quedó en el carrito; el resto de pasos no lo emite."
    if entrega_redirect_carro:
        hint = (
            f"{hint} POST entregadocumentos redirigió al carrito (sesión/validación rechazada)."
        ).strip()
    if rc_err_entrega:
        hint = f"{hint} RC (post entrega): {rc_err_entrega}".strip()
    if rc_err_pagado:
        hint = f"{hint} RC (página final): {rc_err_pagado}".strip()
    elif rc_msgs:
        hint = f"{hint} RC: {' | '.join(dict.fromkeys(rc_msgs))}".strip()
    if len(orden_body) == 0:
        hint = (
            "Sin formularios JSF en las respuestas: suele ser pantalla de desafío/captcha, "
            "cookies caducas o sesión distinta al navegador. Copia la cookie en la misma pestaña "
            "donde ves el carrito con ítems, o automatiza con navegador real (p. ej. Playwright)."
        )
    elif not any("ViewState" in k for k in orden_body) and "javax.faces" not in html_carro.lower():
        jsf = (
            "El GET carro no incluye javax.faces en el HTML: la sesión httpx no coincide con un carrito JSF "
            "válido. Sube REGISTROCIVIL_SELENIUM_TIMEOUT_SEC, prueba SELENIUM_HEADLESS=false, o envía "
            "X-RegistroCivil-Cookie copiada de la misma pestaña donde el carrito carga con ítems."
        )
        hint = f"{hint} {jsf}".strip() if hint else jsf
    def_hint = _defuncion_cert_hint_for_id(id_certificado)
    if def_hint:
        hint = f"{hint} {def_hint}".strip() if hint else def_hint
    if ok and _numero_documento_parece_telefono_movil_cl(numero):
        ok = False
        doc_hint = (
            "REGISTROCIVIL_DEFAULT_NUMERO_DOCUMENTO_SOL parece un celular (+569… / 09…): en el RC debe ser "
            "el N° de documento del solicitante (cédula/pasaporte), no el teléfono. Corrige .env y vuelve a ejecutar."
        )
        hint = f"{hint} {doc_hint}".strip() if hint else doc_hint

    meta = {
        "id_certificado": id_certificado.strip(),
        "filtro": filtro.strip(),
        "orden_post_url": url_orden,
        "orden_fields": len(orden_body),
        "orden_hidden_regex_fullpage": len(_regex_collect_hidden_inputs(html_carro)),
        "orden_html_source": orden_html_source,
        "orden_has_viewstate": any("ViewState" in k for k in orden_body),
        "entrega_post_url": url_entrega,
        "entrega_variant": entrega_variant.strip().lower() or "standard",
        "entrega_hidden_fields": len(entrega_hidden),
        "entrega_total_fields": len(entrega_body),
        "page_signals_carro": _page_signals(html_carro),
        "page_signals_agregar": _page_signals(html_agregar),
        "run_en_solicitud": run.strip(),
        "rc_mensaje_agregar": rc_agregar,
        "rc_mensaje_id_error_entrega": rc_err_entrega,
        "rc_mensaje_id_error_pagado": rc_err_pagado,
        "carro_tiene_items": tiene_items,
        "entrega_email_enviado": (entrega_body.get("carro_email") or "").strip(),
        "hint": hint,
    }
    return {
        "ok": ok,
        "steps": steps,
        "last_status_code": r4.status_code,
        "meta": meta,
    }


def _http_entrega_phase(
    cookie: str,
    *,
    run: str,
    run_raw: str,
    run_normalizado_aviso: str | None,
    email: str,
    numero: str,
    run_consulta: str,
    run_solicitante: str,
    filtro: str,
    selenium_used: bool,
    cookie_origin: str,
    selenium_fallback_to_env_cookie: bool,
    selenium_error: str | None,
    skip_http_repeat_agregar: bool = False,
    entrega_browser_detail: str | None = None,
) -> dict[str, Any]:
    """Flujo httpx (pack o certificado único) una vez resueltas las cookies."""
    initial = _cookies_from_header(cookie)
    with httpx.Client(
        cookies=initial,
        follow_redirects=True,
        timeout=60.0,
        headers={"User-Agent": UA},
    ) as client:
        if settings.default_pack_tres_certificados:
            pack = _run_pack_certificate_flow(
                client,
                run=run,
                email=email,
                numero=numero,
                filtro=filtro,
                run_consulta=run_consulta,
                run_solicitante=run_solicitante,
                selenium_used=selenium_used,
                bug_prefix="pack",
            )
            return {
                "ok": pack["ok"],
                "pack_ok_completo": pack.get("pack_ok_completo", False),
                "pack_ok_nacimiento": pack.get("pack_ok_nacimiento", False),
                "run": run,
                "run_env": run_raw,
                "run_normalizado_aviso": run_normalizado_aviso,
                "email_destino": email,
                "pack_tres_certificados": True,
                "cookie_origin": cookie_origin,
                "selenium_fallback_to_env_cookie": selenium_fallback_to_env_cookie,
                "selenium_used": selenium_used,
                "selenium_error": selenium_error,
                "http_skipped_repeat_agregar": False,
                "last_status_code": pack.get("last_status_code", 0),
                "steps": pack.get("steps", []),
                "results": pack.get("results", []),
                "agregar_results": pack.get("agregar_results", []),
                "meta": pack.get("meta", {}),
            }

        one = _run_one_certificate_flow(
            client,
            run=run,
            email=email,
            numero=numero,
            filtro=filtro,
            id_certificado=settings.default_id_certificado.strip(),
            entrega_variant=settings.entrega_variant,
            run_consulta=run_consulta,
            run_solicitante=run_solicitante,
            selenium_used=selenium_used,
            skip_http_repeat_agregar=skip_http_repeat_agregar,
            bug_prefix="",
        )
        meta = {
            **one["meta"],
            "cookie_origin": cookie_origin,
            "selenium_fallback_to_env_cookie": selenium_fallback_to_env_cookie,
            "selenium_used": selenium_used,
            "http_skipped_repeat_agregar": skip_http_repeat_agregar,
            "entrega_browser_detail": entrega_browser_detail,
            "selenium_error": selenium_error,
            "pack_tres_certificados": False,
            "run_env": run_raw,
            "run_normalizado_aviso": run_normalizado_aviso,
        }
        return {
            "ok": one["ok"],
            "steps": one["steps"],
            "last_status_code": one["last_status_code"],
            "run": run,
            "email_destino": email,
            "id_certificado": settings.default_id_certificado.strip(),
            "meta": meta,
        }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if settings.bug_clear_on_start:
        _clear_bug_dir()
    else:
        bug_dir().mkdir(parents=True, exist_ok=True)
    from rc_session_store import configure_persist_path, load_from_disk

    path = _session_persist_path()
    if path:
        configure_persist_path(path)
        if load_from_disk(path):
            print(f"[registrocivil] Sesión RC cargada desde {path}", file=sys.stderr, flush=True)
    yield


app = FastAPI(title="certificados", version="0.1.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/registrocivil/session")
def registrocivil_session_status() -> dict[str, Any]:
    """Estado de la sesión subida por el script local (sin exponer la cookie completa)."""
    from rc_session_store import session_status

    st = session_status()
    st["token_required"] = bool(_session_api_token())
    return st


@app.post("/registrocivil/session")
def registrocivil_session_register(
    payload: RegistroCivilSessionIn,
    x_registrocivil_session_token: str | None = Header(
        default=None, alias="X-RegistroCivil-Session-Token"
    ),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Registra cookie RC obtenida en tu Mac (``scripts/registrocivil_cookie_local.py``).

    El servidor en Render la usa para httpx sin abrir Chromium hasta que expire (TTL).
    """
    _verify_session_api_token(x_registrocivil_session_token, authorization)
    from rc_session_store import set_session

    ttl = float(payload.ttl_sec if payload.ttl_sec is not None else settings.session_ttl_sec)
    try:
        return set_session(
            payload.cookie,
            ttl_sec=ttl,
            source=(payload.source or "api").strip(),
            run=(payload.run or "").strip(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/registrocivil/session")
def registrocivil_session_clear(
    x_registrocivil_session_token: str | None = Header(
        default=None, alias="X-RegistroCivil-Session-Token"
    ),
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _verify_session_api_token(x_registrocivil_session_token, authorization)
    from rc_session_store import clear_session

    clear_session()
    return {"ok": True, "message": "Sesión RC eliminada"}


@app.get("/api/captcha/{token}")
def captcha_image(token: str) -> Response:
    """Imagen del desafío RC (evita mandar base64 gigante por WebSocket)."""
    from rc_captcha_delivery import get_captcha_bytes

    got = get_captcha_bytes(token.strip())
    if not got:
        raise HTTPException(status_code=404, detail="Captcha no encontrado o expirado")
    data, mime = got
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "no-store"},
    )


def _email2json_inbox_path() -> Path:
    raw = (settings.email2json_inbox_dir or "incoming").strip() or "incoming"
    p = Path(raw)
    return p if p.is_absolute() else Path(__file__).resolve().parent / p


def _email2json_webhook_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    tok = (settings.email2json_webhook_token or "").strip()
    q = f"?token={tok}" if tok else ""
    return f"{base}/webhooks/email2json{q}"


@app.get("/webhooks/email2json/setup")
def email2json_setup(request: Request) -> dict[str, Any]:
    """
    Instrucciones para configurar https://www.email2json.com/
    (correo *@email2json.com → webhook POST JSON a este servicio).
    """
    inbox_email = settings.default_email.strip()
    return {
        "email2json": "https://www.email2json.com/",
        "pasos": [
            "En email2json.com crea un webhook apuntando a webhook_url (URL pública; en local usa ngrok/cloudflared).",
            "Copia la dirección *@email2json.com que te asignen.",
            "Pon esa dirección en REGISTROCIVIL_DEFAULT_EMAIL (.env) — el Registro Civil enviará el certificado ahí.",
            f"Opcional: REGISTROCIVIL_EMAIL2JSON_WEBHOOK_TOKEN y el mismo valor en ?token= del webhook.",
        ],
        "webhook_url": _email2json_webhook_url(request),
        "registrocivil_default_email": inbox_email or "(define REGISTROCIVIL_DEFAULT_EMAIL)",
        "inbox_guardado_en": str(_email2json_inbox_path()),
        "nota": "Los primeros 50 correos en email2json son gratis; luego plan de pago en su sitio.",
    }


@app.post("/webhooks/email2json")
async def webhook_email2json(
    request: Request,
    token: str | None = Query(default=None),
) -> JSONResponse:
    """
    Recibe el POST de email2json.com cuando llega un correo al *@email2json.com configurado.
    Guarda JSON/adjuntos en incoming/ y responde 200 para que no reintenten.
    """
    from rc_email2json import load_request_payload, parse_email2json_payload, persist_incoming

    expected = (settings.email2json_webhook_token or "").strip()
    if expected and (token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="token inválido")

    payload = await load_request_payload(request)
    if not payload:
        return JSONResponse({"ok": True, "skipped": "body vacío"})

    parsed = parse_email2json_payload(payload if isinstance(payload, dict) else {"_raw": payload})
    parsed = persist_incoming(
        inbox_dir=_email2json_inbox_path(),
        parsed=parsed,
        payload=payload if isinstance(payload, dict) else {"payload": payload},
        save_raw=settings.email2json_save_raw,
    )

    forward_url = (settings.email2json_forward_url or "").strip()
    forward_status: int | None = None
    forward_error: str | None = None
    if forward_url:
        out = {
            "source": "certificados-email2json",
            "message_id": parsed.message_id,
            "subject": parsed.subject,
            "from": parsed.from_addr,
            "to": parsed.to_addrs,
            "looks_registro_civil": parsed.looks_registro_civil,
            "attachments_saved": parsed.attachments_saved,
            "raw_saved": parsed.raw_saved,
            "body_preview": (parsed.body_text or "")[:4000],
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(forward_url, json=out)
                forward_status = r.status_code
        except Exception as e:
            forward_error = f"{type(e).__name__}: {e}"

    print(
        f"[email2json] subject={parsed.subject!r} rc={parsed.looks_registro_civil} "
        f"files={len(parsed.attachments_saved)}",
        file=sys.stderr,
        flush=True,
    )

    return JSONResponse(
        {
            "ok": True,
            "message_id": parsed.message_id,
            "subject": parsed.subject,
            "from": parsed.from_addr,
            "to": parsed.to_addrs,
            "looks_registro_civil": parsed.looks_registro_civil,
            "attachments_saved": parsed.attachments_saved,
            "raw_saved": parsed.raw_saved,
            "forward_url": forward_url or None,
            "forward_status": forward_status,
            "forward_error": forward_error,
        }
    )


def _genealogy_output_path() -> Path:
    raw = (settings.genealogy_output_dir or "salida").strip() or "salida"
    p = Path(raw)
    return p if p.is_absolute() else Path(__file__).resolve().parent / p


@app.post("/registrocivil/arbol-certificados")
async def arbol_certificados(
    certificado_nacimiento: UploadFile | None = File(
        default=None,
        description="PDF certificado de nacimiento del consultado (semilla del árbol)",
    ),
    skip_rc: bool = Query(
        default=False,
        description="Solo organiza PDFs ya en incoming/ y genera ZIP (sin pedir al RC)",
    ),
    x_registrocivil_cookie: str | None = Header(default=None, alias="X-RegistroCivil-Cookie"),
) -> Any:
    """
    1. Extrae del certificado de nacimiento los RUT de padre y madre.
    2. Solicita el pack de certificados en el RC para consultado, padres y abuelos.
    3. Espera PDFs en ``incoming/`` (email2json) y arma un ZIP con subcarpetas por persona
       y ``arbol_familiar.png`` en la raíz.
    """
    import tempfile

    from rc_genealogy_job import run_genealogy_job

    if not certificado_nacimiento or not certificado_nacimiento.filename:
        raise HTTPException(status_code=400, detail="Sube certificado_nacimiento (PDF)")

    cookie, _, _, selenium_error, _ = _resolve_registrocivil_cookie(x_registrocivil_cookie, None)
    if not skip_rc and not cookie:
        msg = "Sin cookie RC: REGISTROCIVIL_COOKIE o cabecera X-RegistroCivil-Cookie"
        if selenium_error:
            msg = f"{selenium_error}. {msg}"
        raise HTTPException(status_code=400, detail=msg)

    run_seed, _ = _normalize_run_for_rc(settings.default_run.strip())
    email = settings.default_email.strip()
    numero = settings.default_numero_documento_sol.strip()
    if not skip_rc and not (email and numero):
        raise HTTPException(
            status_code=400,
            detail="Define REGISTROCIVIL_DEFAULT_EMAIL y REGISTROCIVIL_DEFAULT_NUMERO_DOCUMENTO_SOL",
        )
    aviso = _aviso_numero_documento_invalido(run_seed, numero)
    if not skip_rc and aviso:
        raise HTTPException(status_code=400, detail=aviso)

    suffix = Path(certificado_nacimiento.filename or "nac.pdf").suffix or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await certificado_nacimiento.read())
        seed_path = Path(tmp.name)

    try:
        manifest = run_genealogy_job(
            seed_path,
            inbox_dir=_email2json_inbox_path(),
            output_dir=_genealogy_output_path(),
            http_entrega_fn=_http_entrega_phase,
            cookie=cookie or "",
            email=email,
            numero_solicitante=numero,
            filtro=settings.default_filtro.strip(),
            run_consulta_seed=settings.default_carro_run_consulta.strip() or run_seed,
            run_solicitante_seed=settings.default_carro_run_solicitante.strip() or run_seed,
            max_generation=max(0, min(2, int(settings.genealogy_max_generation))),
            poll_after_request_sec=float(settings.genealogy_poll_after_request_sec),
            poll_interval_sec=float(settings.genealogy_poll_interval_sec),
            skip_rc_requests=skip_rc,
        )
    finally:
        try:
            seed_path.unlink(missing_ok=True)
        except OSError:
            pass

    zip_path = Path(manifest["zip_path"])
    if not zip_path.is_file():
        raise HTTPException(status_code=500, detail="No se generó el ZIP")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        headers={"X-Genealogy-Personas": str(len(manifest.get("personas", [])))},
    )


@app.post("/registrocivil/entrega-certificado")
def entrega_certificado(
    payload: EntregaCertificadoIn | None = None,
    x_registrocivil_cookie: str | None = Header(
        default=None,
        alias="X-RegistroCivil-Cookie",
        description="Cookie completa copiada del navegador (alternativa a REGISTROCIVIL_COOKIE)",
    ),
) -> dict[str, Any]:
    """
    Ejecuta la misma secuencia que los curls manuales (parámetros solo por env REGISTROCIVIL_DEFAULT_*).

    1. GET agregarACarro.srcei
    2. GET carro.srcei (carrito; Referer correcto y cookies del servidor)
    3. POST OrdenDeCompra.srcei (campos ocultos JSF del carrito, p. ej. ViewState)
    4. POST entrega a `/web/entregadocumentos.srcei` (tras Orden JSON se reutiliza el <form> del último GET carro)
    5. GET `/web/entregadocumentosfreePagado.srcei`

    Certificado de matrimonio: `REGISTROCIVIL_ENTREGA_VARIANT=matrimonio` (sin campos RUN consulta/solicitante en el POST)
    y típicamente `REGISTROCIVIL_DEFAULT_ID_CERTIFICADO=2_2_2`.

    Certificado de defunción: mismo flujo que nacimiento (`ENTREGA_VARIANT=standard` o omitido),
    `REGISTROCIVIL_DEFAULT_ID_CERTIFICADO=3_3_1` y `run` / `filtro=99` como en el sitio; si la persona
    no tiene defunción inscrita, el RC devuelve error (esperado).

    Pack (por defecto activo): `REGISTROCIVIL_DEFAULT_PACK_TRES_CERTIFICADOS=true` agrega al mismo carro
    nacimiento, defunción, matrimonio y (si está configurado) acuerdo de unión civil
    (`REGISTROCIVIL_DEFAULT_PACK_ID_UNION_CIVIL`). Los ids `REGISTROCIVIL_DEFAULT_PACK_ID_*` se copian del
    parámetro `idCertificado` en la URL `agregarACarro.srcei` al pulsar agregar en
    https://www.registrocivil.cl/OficinaInternet/web/carro.srcei (DevTools → Network). Si defunción,
    matrimonio o AUC no aplican al RUN, el resto se intenta igual; `ok` es true si **al menos uno** quedó
    en carrito y la entrega al correo fue bien. `pack_ok_completo` = todos los del pack agregados + entrega.
    `false` = un solo certificado con `DEFAULT_ID_CERTIFICADO`.

    Cookies (orden): cabecera `X-RegistroCivil-Cookie` → body `{"cookie":"..."}` →
    `REGISTROCIVIL_COOKIE` o `REGISTROCIVIL_COOKIE_FILE` → Selenium si `USE_SELENIUM=true`.

    Con cookie válida ejecuta carro → agregar → carro → OrdenDeCompra → entregadocumentos
    (correo en `REGISTROCIVIL_DEFAULT_EMAIL`) → entregadocumentosfreePagado.
    """
    run_raw = settings.default_run.strip()
    run, run_normalizado_aviso = _normalize_run_for_rc(run_raw)
    email = settings.default_email.strip()
    numero = settings.default_numero_documento_sol.strip()
    if not (run and email and numero):
        raise HTTPException(
            status_code=400,
            detail="Define REGISTROCIVIL_DEFAULT_RUN, REGISTROCIVIL_DEFAULT_EMAIL y "
            "REGISTROCIVIL_DEFAULT_NUMERO_DOCUMENTO_SOL en .env",
        )

    body_cookie = (payload.cookie if payload else None) or None
    cookie, had_x_registrocivil_cookie, selenium_used, selenium_error, cookie_origin = (
        _resolve_registrocivil_cookie(x_registrocivil_cookie, body_cookie)
    )
    selenium_fallback_to_env_cookie = bool(
        settings.use_selenium
        and not had_x_registrocivil_cookie
        and not selenium_used
        and settings.selenium_fallback_env_cookie
        and bool(_load_cookie_from_env_or_file())
        and bool(cookie)
        and cookie_origin == "env_fallback"
    )

    if not cookie:
        msg = (
            "Sin cookie: pega REGISTROCIVIL_COOKIE en .env (tras el captcha en registrocivil.cl), "
            "envía X-RegistroCivil-Cookie, body {\"cookie\":\"...\"}, o activa USE_SELENIUM=true."
        )
        if selenium_error:
            msg = f"Selenium falló ({selenium_error}). " + msg
        raise HTTPException(status_code=502 if selenium_error else 400, detail=msg)

    run_consulta = settings.default_carro_run_consulta.strip() or run
    run_solicitante = settings.default_carro_run_solicitante.strip() or run
    filtro = settings.default_filtro.strip()

    return _http_entrega_phase(
        cookie,
        run=run,
        run_raw=run_raw,
        run_normalizado_aviso=run_normalizado_aviso,
        email=email,
        numero=numero,
        run_consulta=run_consulta,
        run_solicitante=run_solicitante,
        filtro=filtro,
        selenium_used=selenium_used,
        cookie_origin=cookie_origin,
        selenium_fallback_to_env_cookie=selenium_fallback_to_env_cookie,
        selenium_error=selenium_error,
    )


def _root_html() -> str:
    run_default = settings.default_run.strip()
    email = settings.default_email.strip() or "(define REGISTROCIVIL_DEFAULT_EMAIL)"
    api_public = (
        (os.environ.get("REGISTROCIVIL_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "")
        .strip()
        .rstrip("/")
        or "https://TU-SERVICIO.onrender.com"
    )
    if settings.default_pack_tres_certificados:
        cert = _pack_ui_label()
    else:
        cert = settings.default_id_certificado.strip()
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Certificados — Registro Civil</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 52rem; margin: 1.5rem auto; padding: 0 1rem; }}
    button {{ padding: 0.65rem 1.25rem; font-size: 1rem; cursor: pointer; }}
    button:disabled {{ opacity: 0.5; cursor: wait; }}
    #alertBox {{
      display: none; margin: 1rem 0; padding: 1rem 1.1rem;
      background: #fff8e6; border: 1px solid #e6c200; border-radius: 8px;
    }}
    #alertBox strong {{ display: block; margin-bottom: 0.35rem; }}
    label {{ display: block; font-weight: 600; margin-bottom: 0.35rem; }}
    #runRut {{ width: 100%; max-width: 22rem; padding: 0.55rem; font-size: 1.1rem; margin-bottom: 1rem; }}
    #captchaBox {{ display: none; margin: 1rem 0; padding: 1rem; border: 1px solid #ccc; border-radius: 8px; background: #fafafa; }}
    #captchaBox img {{ max-width: 100%; min-height: 4rem; height: auto; border: 1px solid #888; display: block; margin-bottom: 0.75rem; background: #eee; }}
    #code {{ width: 100%; max-width: 22rem; padding: 0.55rem; font-size: 1.1rem; margin: 0.5rem 0; }}
    #log {{ white-space: pre-wrap; background: #111; color: #cfc; padding: 0.75rem; border-radius: 6px; min-height: 5rem; font-size: 0.85rem; }}
    .err {{ color: #f88; }}
    .ok {{ color: #6f6; }}
    .meta {{ color: #999; font-size: 0.9rem; margin-bottom: 1rem; }}
  </style>
</head>
<body>
  <h1>Certificado Registro Civil</h1>
  <p class="meta">Pack: <code>{cert}</code> · correo destino <code>{email}</code></p>
  <p>
    <label for="runRut">RUT de la persona (certificados a solicitar)</label>
    <input type="text" id="runRut" name="run" placeholder="17.402.744-7" value="{run_default}" autocomplete="off"/>
  </p>
  <p>
    Pulsa iniciar: si aparece la <strong>imagen del desafío</strong> abajo, escribe el código y envía.
    Si es <strong>reCAPTCHA</strong>, complétalo en Chromium (en servidor va en headless). Tipos del pack que no apliquen al RUN no detienen el resto.
  </p>
  <p class="meta">
    <strong>Render / sin captcha en servidor:</strong> en tu Mac ejecuta
    <code>python scripts/registrocivil_cookie_local.py --api-url {api_public}</code>
    (Chrome local → sube sesión). Luego inicia aquí otra vez.
    Estado: <a href="/registrocivil/session">GET /registrocivil/session</a>
  </p>
  <p><button type="button" id="go">Iniciar solicitud</button></p>
  <div id="captchaBox">
    <strong>Código de la imagen</strong>
    <img id="capImg" alt="Desafío Registro Civil"/>
    <input type="text" id="code" placeholder="Escribe el código" autocomplete="off"/>
    <button type="button" id="sendCode">Enviar código</button>
    <button type="button" id="skipCaptcha" style="margin-left:0.5rem">Carrito ya cargó — continuar</button>
  </div>
  <div id="alertBox" role="status">
    <strong>Acción en Chromium (Playwright)</strong>
    <span id="alertText"></span>
  </div>
  <h2>Progreso</h2>
  <div id="log"></div>
  <script>
    const logEl = document.getElementById("log");
    const capBox = document.getElementById("captchaBox");
    const capImg = document.getElementById("capImg");
    const codeEl = document.getElementById("code");
    const sendBtn = document.getElementById("sendCode");
    const skipBtn = document.getElementById("skipCaptcha");
    const alertBox = document.getElementById("alertBox");
    const alertText = document.getElementById("alertText");
    const goBtn = document.getElementById("go");
    const runRutEl = document.getElementById("runRut");
    function log(msg, cls) {{
      const span = document.createElement("div");
      if (cls) span.className = cls;
      span.textContent = msg;
      logEl.appendChild(span);
      logEl.scrollTop = logEl.scrollHeight;
    }}
    let ws = null;

    function connectWs() {{
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const qs = new URLSearchParams(location.search);
      const tok = qs.get("token");
      let url = proto + "//" + location.host + "/ws/entrega-certificado";
      if (tok) url += "?token=" + encodeURIComponent(tok);
      ws = new WebSocket(url);

      ws.onopen = () => {{
        const run = (runRutEl.value || "").trim();
        if (!run) {{
          log("Ingresa el RUT de la persona.", "err");
          ws.close();
          return;
        }}
        ws.send(JSON.stringify({{ type: "start", run }}));
        log("RUN " + run + " — conectado, abriendo navegador…");
        goBtn.disabled = true;
        runRutEl.disabled = true;
      }};

      ws.onmessage = (ev) => {{
        let msg;
        try {{ msg = JSON.parse(ev.data); }} catch (e) {{ log("JSON inválido", "err"); return; }}
        if (msg.type === "log") {{
          log(msg.message || "");
          return;
        }}
        if (msg.type === "captcha") {{
          capBox.style.display = "block";
          alertBox.style.display = "none";
          let src = "";
          if (msg.image_url) {{
            src = msg.image_url.startsWith("http") ? msg.image_url : (location.origin + msg.image_url);
            src += (src.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
          }} else if (msg.image_data_url) {{
            src = msg.image_data_url;
          }} else if (msg.image_base64) {{
            src = "data:image/png;base64," + msg.image_base64;
          }}
          capImg.onerror = () => log("No se pudo mostrar la imagen del desafío.", "err");
          capImg.onload = () => log("Imagen del desafío visible. Escribe el código y pulsa «Enviar código».", "ok");
          capImg.src = src || "";
          codeEl.value = "";
          codeEl.focus();
          if (!src) log("El servidor no envió URL de imagen.", "err");
          return;
        }}
        if (msg.type === "manual_challenge") {{
          alertBox.style.display = "block";
          capBox.style.display = "none";
          alertText.textContent = msg.message || "Resuelve el reCAPTCHA en la ventana de Chromium.";
          log(msg.message || "", "ok");
          return;
        }}
        if (msg.type === "done") {{
          alertBox.style.display = "none";
          capBox.style.display = "none";
          const r = msg.result || {{}};
          if (r.ok) {{
            const okTipos = (r.results || []).filter(x => x.ok_agregar || x.ok).map(x => x.tipo).filter(Boolean);
            const extra = okTipos.length ? " Tipos en carrito: " + okTipos.join(", ") + "." : "";
            log("Solicitud enviada. Revisa el correo: " + (r.email_destino || "{email}") + extra, "ok");
            if ((r.agregar_results || r.meta?.agregar_fallidos || []).length) {{
              const fall = r.meta?.agregar_fallidos || (r.agregar_results || []).filter(x => !x.ok_agregar).map(x => x.tipo);
              if (fall.length) log("No agregados (esperado si no aplican): " + fall.join(", "));
            }}
          }} else {{
            log("Terminó con errores. Detalle:", "err");
            log(JSON.stringify(r, null, 2), "err");
          }}
          ws.close();
          return;
        }}
        if (msg.type === "error") {{
          alertBox.style.display = "none";
          capBox.style.display = "none";
          log("Error: " + (msg.message || ""), "err");
          ws.close();
        }}
      }};

      ws.onerror = () => log("Error de conexión.", "err");
      ws.onclose = () => {{
        log("Conexión cerrada.");
        goBtn.disabled = false;
        runRutEl.disabled = false;
        ws = null;
      }};
    }}

    goBtn.onclick = () => {{
      if (ws && ws.readyState <= 1) return;
      if (!(runRutEl.value || "").trim()) {{
        log("Ingresa el RUT antes de iniciar.", "err");
        runRutEl.focus();
        return;
      }}
      logEl.textContent = "";
      alertBox.style.display = "none";
      capBox.style.display = "none";
      connectWs();
    }};

    sendBtn.onclick = () => {{
      if (!ws || ws.readyState !== 1) return;
      const t = (codeEl.value || "").trim();
      if (!t) return;
      ws.send(JSON.stringify({{ type: "captcha_answer", text: t }}));
      capBox.style.display = "none";
      log("Código enviado; el sistema continúa automáticamente…");
    }};

    skipBtn.onclick = () => {{
      if (!ws || ws.readyState !== 1) return;
      ws.send(JSON.stringify({{ type: "captcha_skip" }}));
      capBox.style.display = "none";
      log("Continuando: si Chromium ya muestra el carrito, el flujo sigue solo…");
    }};

    (function () {{
      const q = new URLSearchParams(location.search).get("run");
      if (q && !runRutEl.value) runRutEl.value = q;
    }})();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root_ui() -> str:
    return _root_html()


@app.websocket("/ws/entrega-certificado")
async def ws_entrega_certificado(websocket: WebSocket) -> None:
    tok_expected = (settings.ws_token or "").strip()
    if tok_expected:
        qtok = (websocket.query_params.get("token") or "").strip()
        if qtok != tok_expected:
            await websocket.close(code=1008)
            return
    await websocket.accept()

    try:
        raw_start = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
        start_obj = json.loads(raw_start)
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "error", "message": "Timeout: envía {type:'start', run:'...'} al conectar."})
        await websocket.close()
        return
    except json.JSONDecodeError:
        await websocket.send_json({"type": "error", "message": "Primer mensaje inválido; se esperaba JSON con type=start y run."})
        await websocket.close()
        return

    if (start_obj.get("type") or "").strip() != "start":
        await websocket.send_json({"type": "error", "message": "Primer mensaje debe ser type=start con el RUT."})
        await websocket.close()
        return

    run_raw = (start_obj.get("run") or start_obj.get("rut") or "").strip() or settings.default_run.strip()
    run, run_normalizado_aviso = _normalize_run_for_rc(run_raw)
    email = settings.default_email.strip()
    numero = settings.default_numero_documento_sol.strip()
    if not run:
        await websocket.send_json({"type": "error", "message": "Ingresa el RUT de la persona en el formulario."})
        await websocket.close()
        return
    if not (email and numero):
        await websocket.send_json(
            {
                "type": "error",
                "message": "Define REGISTROCIVIL_DEFAULT_EMAIL y REGISTROCIVIL_DEFAULT_NUMERO_DOCUMENTO_SOL en .env",
            }
        )
        await websocket.close()
        return
    if run_normalizado_aviso:
        await websocket.send_json({"type": "log", "message": run_normalizado_aviso})
    aviso_ndoc = _aviso_numero_documento_invalido(run, numero)
    if aviso_ndoc:
        await websocket.send_json({"type": "error", "message": aviso_ndoc})
        await websocket.close()
        return

    start_cookie = (start_obj.get("cookie") or "").strip()
    static_cookie, cookie_origin = _resolve_cookie_from_request(None, None, extra_cookie=start_cookie)
    if static_cookie:
        origin_msg = {
            "env": "REGISTROCIVIL_COOKIE",
            "session_store": "sesión subida (POST /registrocivil/session)",
            "ws_start": "cookie en mensaje start",
        }.get(cookie_origin, cookie_origin)
        await websocket.send_json(
            {
                "type": "log",
                "message": f"{origin_msg}: flujo HTTP (sin abrir Chromium).",
            }
        )
        run_consulta = settings.default_carro_run_consulta.strip() or run
        run_solicitante = settings.default_carro_run_solicitante.strip() or run
        filtro = settings.default_filtro.strip()
        try:
            payload = await asyncio.to_thread(
                _http_entrega_phase,
                static_cookie,
                run=run,
                run_raw=run_raw,
                run_normalizado_aviso=run_normalizado_aviso,
                email=email,
                numero=numero,
                run_consulta=run_consulta,
                run_solicitante=run_solicitante,
                filtro=filtro,
                selenium_used=False,
                cookie_origin=cookie_origin,
                selenium_fallback_to_env_cookie=False,
                selenium_error=None,
                skip_http_repeat_agregar=False,
            )
            await websocket.send_json({"type": "done", "result": payload})
        except Exception as e:
            await websocket.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
        try:
            await websocket.close()
        except Exception:
            pass
        return

    if (os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID") or "").strip():
        await websocket.send_json(
            {
                "type": "log",
                "message": (
                    "Sin cookie en el servidor: en tu Mac ejecuta "
                    "python scripts/registrocivil_cookie_local.py --api-url "
                    + str(websocket.url).replace("wss:", "https:").replace("ws:", "http:").split("/ws/")[0]
                    + " (abre Chrome, resuelve captcha, sube sesión). Luego pulsa Iniciar de nuevo."
                ),
            }
        )

    to_client: queue.Queue[tuple[str, Any]] = queue.Queue()
    from_client: queue.Queue[tuple[str, Any]] = queue.Queue()
    pack_specs: list[tuple[str, str]] | None = None
    if settings.default_pack_tres_certificados:
        pack_specs = [(t, c) for t, c, _ in _pack_certificado_specs()]
    id_boot = (
        "pack"
        if pack_specs
        else settings.default_id_certificado.strip()
    )

    def _browser_worker() -> None:
        backend = settings.browser_backend.strip().lower() or "playwright"
        headless = settings.ws_browser_headless
        if backend == "selenium":
            headless = settings.ws_selenium_headless
        headless, headless_note = _resolve_ws_headless(headless)
        if headless_note:
            to_client.put(("log", headless_note))
        try:
            if backend == "playwright":
                from rc_playwright_ws import fetch_cookie_header_via_chrome_interactive as fetch_session
            else:
                from rc_selenium_ws import fetch_cookie_header_via_chrome_interactive as fetch_session

            session = fetch_session(
                run=run,
                filtro=settings.default_filtro.strip(),
                id_certificado="" if pack_specs else settings.default_id_certificado.strip(),
                pack_specs=pack_specs,
                headless=headless,
                timeout=settings.selenium_timeout_sec,
                manual_captcha_timeout_sec=settings.selenium_manual_captcha_timeout_sec,
                chrome_binary=settings.chrome_binary.strip() or None,
                to_client=to_client,
                from_client=from_client,
                email=email,
                email_confirm=(settings.entrega_email_confirm.strip() or email),
                telefono=settings.entrega_telefono.strip(),
                numero_documento=numero,
                complete_entrega_in_browser=settings.ws_complete_entrega_in_browser,
            )
            to_client.put(("session_result", session))
        except Exception as e:
            to_client.put(("worker_exception", f"{type(e).__name__}: {e}"))

    th = threading.Thread(target=_browser_worker, daemon=True)
    th.start()

    session_out: list[dict[str, Any]] = []

    def _drain_queue_item() -> tuple[str, Any] | None:
        try:
            return to_client.get(timeout=0.35)
        except queue.Empty:
            return None

    try:
        while True:
            item = await asyncio.to_thread(_drain_queue_item)
            if item is None:
                await asyncio.sleep(0.02)
                continue
            kind, data = item
            if kind == "log":
                await websocket.send_json({"type": "log", "message": str(data)})
            elif kind == "captcha":
                from rc_captcha_delivery import register_captcha_for_browser

                payload = data if isinstance(data, dict) else {}
                ws_payload = register_captcha_for_browser(payload)
                if not ws_payload:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Imagen del captcha vacía; reintenta o usa REGISTROCIVIL_COOKIE.",
                        }
                    )
                    await websocket.close()
                    return
                await websocket.send_json(ws_payload)
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=float(min(600, max(120, settings.selenium_manual_captcha_timeout_sec + 60))),
                )
                try:
                    obj = json.loads(raw)
                    if (obj.get("type") or "").strip() == "captcha_skip":
                        await asyncio.to_thread(from_client.put, ("captcha_skip", None))
                    else:
                        txt = (obj.get("text") or obj.get("answer") or "").strip()
                        await asyncio.to_thread(from_client.put, ("captcha_answer", txt))
                except json.JSONDecodeError:
                    await asyncio.to_thread(from_client.put, ("captcha_answer", raw.strip()))
            elif kind == "manual_challenge":
                payload = data if isinstance(data, dict) else {"message": str(data)}
                await websocket.send_json({"type": "manual_challenge", **payload})
            elif kind == "error":
                await websocket.send_json({"type": "error", "message": str(data)})
                await websocket.close()
                return
            elif kind == "worker_exception":
                await websocket.send_json({"type": "error", "message": str(data)})
                await websocket.close()
                return
            elif kind == "session_result":
                session_out.append(data if isinstance(data, dict) else {"cookies": str(data or ""), "entrega_ok": False})
                break
    except WebSocketDisconnect:
        return
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "error", "message": "Timeout esperando mensaje en WebSocket"})
        await websocket.close()
        return

    th.join(timeout=8)

    session = session_out[0] if session_out else {}
    cookie_header = (session.get("cookies") or "").strip()
    entrega_ok_browser = bool(session.get("entrega_ok"))

    if entrega_ok_browser:
        agregar_results = session.get("agregar_results") or []
        done_payload: dict[str, Any] = {
            "ok": True,
            "run": run,
            "email_destino": email,
            "id_certificado": id_boot,
            "via": "chrome_entrega",
            "entrega_url": session.get("entrega_url") or "",
            "entrega_detail": session.get("entrega_detail") or "",
            "pack_tres_certificados": bool(session.get("pack_tres_certificados")),
            "meta": {
                "cookie_origin": "selenium_ws",
                "selenium_used": True,
                "httpx_skipped": True,
                "entrega_email_enviado": email,
                "agregar_results": agregar_results,
                "agregar_exitosos": [r["tipo"] for r in agregar_results if r.get("ok_agregar")],
            },
        }
        if agregar_results:
            done_payload["results"] = [
                {
                    "tipo": r.get("tipo"),
                    "ok": bool(r.get("ok_agregar")),
                    "ok_agregar": bool(r.get("ok_agregar")),
                }
                for r in agregar_results
            ]
        await websocket.send_json({"type": "done", "result": done_payload})
        try:
            await websocket.close()
        except Exception:
            pass
        return

    if not cookie_header:
        detail = (session.get("entrega_detail") or "").strip()
        msg = detail or "No se obtuvieron cookies válidas del RC"
        await websocket.send_json({"type": "error", "message": msg})
        await websocket.close()
        return

    chrome_detail = (session.get("entrega_detail") or "").strip()
    if chrome_detail:
        await websocket.send_json({"type": "log", "message": f"Chrome (entrega): {chrome_detail}"})
    await websocket.send_json(
        {
            "type": "log",
            "message": f"Entrega en Chrome no confirmada; reintentando por HTTP (sin volver a agregar) → {email}…",
        }
    )
    run_consulta = settings.default_carro_run_consulta.strip() or run
    run_solicitante = settings.default_carro_run_solicitante.strip() or run
    filtro = settings.default_filtro.strip()
    await websocket.send_json({"type": "log", "message": f"Solicitud para RUN {run} → {email}"})
    try:
        payload = await asyncio.to_thread(
            _http_entrega_phase,
            cookie_header,
            run=run,
            run_raw=run_raw,
            run_normalizado_aviso=run_normalizado_aviso,
            email=email,
            numero=numero,
            run_consulta=run_consulta,
            run_solicitante=run_solicitante,
            filtro=filtro,
            selenium_used=True,
            cookie_origin="selenium_ws",
            selenium_fallback_to_env_cookie=False,
            selenium_error=None,
            skip_http_repeat_agregar=True,
            entrega_browser_detail=chrome_detail or None,
        )
        await websocket.send_json({"type": "done", "result": payload})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# Permite `python main.py` sin uvicorn en PATH
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
