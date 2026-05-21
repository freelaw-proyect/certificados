#!/usr/bin/env python3
"""
Obtiene la cookie del Registro Civil en tu Mac (Chrome visible) y la sube al API.

Uso típico con Render:
  python scripts/registrocivil_cookie_local.py \\
    --api-url https://certificados-r07l.onrender.com \\
    --token TU_REGISTROCIVIL_SESSION_TOKEN

Luego en la UI de Render (o local) «Iniciar solicitud» usará la sesión guardada sin Chromium.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import httpx
except ImportError:
    print("Instala dependencias: pip install httpx", file=sys.stderr)
    sys.exit(1)


def _normalize_run(run: str) -> str:
    from main import _normalize_run_for_rc

    r, _ = _normalize_run_for_rc(run.strip())
    return r


def _fetch_cookie_chrome(*, run: str, filtro: str, id_cert: str, timeout: int) -> str:
    from rc_selenium import fetch_cookie_header_via_chrome

    print(
        "[local] Abriendo Chrome: resuelve captcha/reCAPTCHA en la ventana que aparece…",
        file=sys.stderr,
        flush=True,
    )
    return fetch_cookie_header_via_chrome(
        run=run,
        filtro=filtro,
        id_certificado=id_cert,
        headless=False,
        timeout=timeout,
        manual_captcha_timeout_sec=max(600, timeout),
    ).strip()


def _upload_session(
    *,
    api_url: str,
    cookie: str,
    token: str,
    ttl_sec: int,
    run: str,
) -> dict:
    base = api_url.rstrip("/")
    headers: dict[str, str] = {}
    if token:
        headers["X-RegistroCivil-Session-Token"] = token
    payload = {
        "cookie": cookie,
        "ttl_sec": ttl_sec,
        "source": "registrocivil_cookie_local",
        "run": run,
    }
    with httpx.Client(timeout=60.0) as client:
        r = client.post(f"{base}/registrocivil/session", json=payload, headers=headers)
    r.raise_for_status()
    return r.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Cookie RC local → API (Render o local)")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("REGISTROCIVIL_API_URL", "http://127.0.0.1:8765"),
        help="Base del servicio certificados (sin barra final)",
    )
    parser.add_argument(
        "--token",
        default=(
            os.environ.get("REGISTROCIVIL_SESSION_TOKEN")
            or os.environ.get("REGISTROCIVIL_WS_TOKEN")
            or ""
        ),
        help="X-RegistroCivil-Session-Token (mismo que en Render si REGISTROCIVIL_SESSION_TOKEN)",
    )
    parser.add_argument("--run", default=os.environ.get("REGISTROCIVIL_DEFAULT_RUN", ""))
    parser.add_argument("--filtro", default=os.environ.get("REGISTROCIVIL_DEFAULT_FILTRO", "99"))
    parser.add_argument(
        "--id-certificado",
        default=os.environ.get("REGISTROCIVIL_DEFAULT_ID_CERTIFICADO", "133_1_2"),
    )
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--ttl-sec", type=int, default=7200, help="TTL en el servidor (default 2 h)")
    parser.add_argument(
        "--cookie-only",
        action="store_true",
        help="Solo imprime la cookie (no hace POST); útil para pegar en .env",
    )
    args = parser.parse_args()

    run = _normalize_run(args.run)
    if not run:
        print("Indica --run o REGISTROCIVIL_DEFAULT_RUN", file=sys.stderr)
        return 2

    cookie = _fetch_cookie_chrome(
        run=run,
        filtro=args.filtro.strip(),
        id_cert=args.id_certificado.strip(),
        timeout=args.timeout,
    )
    if not cookie:
        print("[local] No se obtuvo cookie (captcha no resuelto o carrito no cargó).", file=sys.stderr)
        return 1

    if args.cookie_only:
        print(cookie)
        return 0

    try:
        status = _upload_session(
            api_url=args.api_url,
            cookie=cookie,
            token=args.token.strip(),
            ttl_sec=args.ttl_sec,
            run=run,
        )
    except httpx.HTTPStatusError as e:
        print(f"[local] POST /registrocivil/session falló: {e.response.status_code} {e.response.text}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[local] Error subiendo sesión: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[local] Sesión activa en {args.api_url}")
    print(f"        expira en {status.get('expires_in_sec')} s · cookies: {', '.join(status.get('cookie_names') or [])}")
    print("        Ya puedes usar la UI o POST /registrocivil/entrega-certificado en ese servidor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
