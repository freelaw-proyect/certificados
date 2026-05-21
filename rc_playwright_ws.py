"""
Flujo WebSocket con Playwright (Chromium). Suele funcionar mejor en headless que Selenium.

reCAPTCHA sigue requiriendo intervención humana: con headless=true se envía manual_challenge
y hay que resolver en la ventana del navegador (o usar REGISTROCIVIL_WS_BROWSER_HEADLESS=false).
"""

from __future__ import annotations

import os
import queue
import sys
import time
from typing import Any
from urllib.parse import urlencode

from rc_selenium import (
    OFICINA,
    UA_CHROME,
    _cart_has_certificate_items,
    _challenge_requires_user_in_browser,
    _cookies_to_header,
    _is_agregar_iframe_shell_html,
    _is_captcha_interstitial_html,
    _is_recaptcha_html,
    _is_rc_waf_or_non_jsf_shell_html,
    _maybe_recover_short_html,
    _session_ready_to_continue,
    extract_rc_div_id_error_message,
    _rc_error_element_visible,
)


def _stderr(msg: str) -> None:
    print(f"[registrocivil-pw] {msg}", file=sys.stderr, flush=True)


def _remote_deploy() -> bool:
    """Render y otros PaaS: el Chromium no es visible para el usuario en el navegador."""
    return bool(
        (os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID") or "").strip()
        or (os.environ.get("REGISTROCIVIL_REMOTE_DEPLOY") or "").strip().lower()
        in ("true", "1", "yes")
    )


def _page_status_hint(src: str) -> str:
    if not src:
        return "HTML vacío"
    if _session_ready_to_continue(src):
        return "carrito listo"
    if _is_recaptcha_html(src):
        return "reCAPTCHA"
    if _is_captcha_interstitial_html(src):
        return "captcha imagen"
    if _is_rc_waf_or_non_jsf_shell_html(src):
        return "WAF/desafío"
    return f"esperando ({len(src)} bytes)"


def _content(page: Any) -> str:
    try:
        return page.content() or ""
    except Exception:
        return ""


def _pw_captcha_visual(page: Any) -> dict[str, str]:
    src = _content(page)
    if _session_ready_to_continue(src) and not _is_captcha_interstitial_html(src):
        return {}
    if not (
        _is_captcha_interstitial_html(src)
        or _is_recaptcha_html(src)
        or (_is_rc_waf_or_non_jsf_shell_html(src) and not _session_ready_to_continue(src))
    ):
        return {}
    try:
        loc = page.locator('img[alt="Red dot"]')
        if loc.count() and loc.first.is_visible():
            src_attr = loc.first.get_attribute("src") or ""
            if src_attr.startswith("data:image"):
                return {"image_data_url": src_attr, "mime": "image/png"}
    except Exception:
        pass
    try:
        shot = page.locator('img[alt="Red dot"]').first.screenshot()
        if shot:
            import base64

            return {
                "image_base64": base64.b64encode(shot).decode("ascii"),
                "mime": "image/png",
            }
    except Exception:
        pass
    return {}


def _pw_submit_captcha(page: Any, text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    selectors = (
        "#ans",
        "input[name='answer']",
        "input[placeholder*='código' i]",
        "input[placeholder*='codigo' i]",
    )
    inp = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                inp = loc
                break
        except Exception:
            continue
    if inp is None:
        return False
    try:
        inp.fill(t)
    except Exception:
        return False
    for sel in ("#jar", "button:has-text('Enviar')", "input[type='submit']", "button[type='submit']"):
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    try:
        inp.press("Enter")
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def _wait_challenge_pw(
    page: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    from_client: "queue.Queue[tuple[str, Any]]",
    *,
    phase: str,
    deadline: float,
    answer_timeout_sec: float,
    headless: bool,
) -> bool:
    end = time.monotonic() + answer_timeout_sec

    def ready() -> bool:
        return _session_ready_to_continue(_content(page))

    if ready():
        to_client.put(("log", f"{phase}: carrito listo; continuando…"))
        return True

    vis: dict[str, str] = {}
    while time.monotonic() < end:
        if ready():
            return True
        vis = _pw_captcha_visual(page)
        if vis.get("image_data_url") or vis.get("image_base64"):
            break
        if _is_recaptcha_html(_content(page)):
            if headless or _remote_deploy():
                to_client.put(
                    (
                        "error",
                        f"{phase}: reCAPTCHA en el Registro Civil. "
                        "En Render el navegador corre en el servidor (no hay ventana que puedas usar). "
                        "Pega REGISTROCIVIL_COOKIE en variables de entorno (resuélvelo en tu PC en "
                        "registrocivil.cl) o ejecuta la app en local http://127.0.0.1:8765.",
                    )
                )
                return False
            to_client.put(
                (
                    "manual_challenge",
                    {
                        "phase": phase,
                        "kind": "recaptcha",
                        "message": f"{phase}: resuelve el reCAPTCHA en la ventana de Chromium (solo en local).",
                    },
                )
            )
            last_ping = 0.0
            while time.monotonic() < deadline:
                if not _challenge_requires_user_in_browser(_content(page)):
                    to_client.put(("log", f"{phase}: reCAPTCHA superado."))
                    return True
                now = time.monotonic()
                if now - last_ping >= 20.0:
                    to_client.put(
                        (
                            "log",
                            f"{phase}: sigue reCAPTCHA — marca la casilla en la ventana de Chromium…",
                        )
                    )
                    last_ping = now
                time.sleep(0.45)
            to_client.put(("error", f"{phase}: timeout esperando reCAPTCHA."))
            return False
        time.sleep(0.45)

    if not vis.get("image_data_url") and not vis.get("image_base64"):
        while time.monotonic() < deadline:
            if not _challenge_requires_user_in_browser(_content(page)):
                return True
            time.sleep(0.45)
        return False

    to_client.put(("captcha", {"phase": phase, **vis}))
    while time.monotonic() < end:
        if ready():
            return True
        try:
            msg = from_client.get(timeout=0.5)
        except queue.Empty:
            continue
        if isinstance(msg, tuple) and msg[0] == "captcha_skip":
            if ready():
                return True
            continue
        if not isinstance(msg, tuple) or msg[0] != "captcha_answer":
            continue
        ans = (msg[1] or "").strip()
        if not ans:
            continue
        if not _pw_submit_captcha(page, ans):
            to_client.put(("error", f"No se pudo enviar el código ({phase})"))
            return False
        page.wait_for_load_state("domcontentloaded", timeout=int(min(45000, answer_timeout_sec * 1000)))
        page.wait_for_timeout(1200)
        return True
    to_client.put(("error", f"Timeout captcha ({phase})"))
    return False


def _poll_until_ready_pw(
    page: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    from_client: "queue.Queue[tuple[str, Any]]",
    *,
    deadline: float,
    phase: str,
    answer_timeout_sec: float,
    headless: bool,
) -> bool:
    last_ping = 0.0
    while time.monotonic() < deadline:
        src = _content(page)
        now = time.monotonic()
        if now - last_ping >= 12.0:
            to_client.put(("log", f"{phase}: {_page_status_hint(src)}…"))
            last_ping = now
        if _session_ready_to_continue(src):
            if phase == "agregarACarro" and _is_agregar_iframe_shell_html(src):
                to_client.put(("log", f"{phase}: iframe agregar listo ({len(src)} bytes)."))
                return True
            return True
        if _challenge_requires_user_in_browser(src) or _is_captcha_interstitial_html(src):
            if not _wait_challenge_pw(
                page,
                to_client,
                from_client,
                phase=phase,
                deadline=deadline,
                answer_timeout_sec=answer_timeout_sec,
                headless=headless,
            ):
                return False
            continue
        if phase == "agregarACarro" and _is_agregar_iframe_shell_html(src):
            return True
        time.sleep(0.35)
    return _session_ready_to_continue(_content(page))


def _agregar_via_iframe_pw(page: Any, url_agregar: str, wait_sec: float) -> bool:
    page.locator("#cu_idIframe4").wait_for(state="attached", timeout=int(min(wait_sec, 45) * 1000))
    page.evaluate(
        "(url) => { const f = document.getElementById('cu_idIframe4'); if (f) f.src = url; }",
        url_agregar,
    )
    page.wait_for_timeout(2500)
    deadline = time.monotonic() + min(wait_sec, 90.0)
    while time.monotonic() < deadline:
        if _cart_has_certificate_items(_content(page)):
            return True
        time.sleep(0.6)
    return _cart_has_certificate_items(_content(page))


def _complete_entrega_pw(
    page: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    *,
    email: str,
    email_confirm: str,
    telefono: str,
    run: str,
    numero: str,
    wait_sec: float,
) -> dict[str, Any]:
    to_client.put(("log", "Rellenando carrito y pulsando Continuar (Playwright)…"))
    try:
        page.locator("#preloader").wait_for(state="hidden", timeout=int(min(wait_sec, 45) * 1000))
    except Exception:
        pass
    try:
        page.evaluate("if (typeof getOrdenDeCompra === 'function') getOrdenDeCompra();")
        page.wait_for_timeout(3000)
    except Exception:
        pass
    email_c = email_confirm or email
    page.evaluate(
        """(args) => {
            const [email, emailC, run, numero, tel] = args;
            function jqSet(id, v) {
              if (window.jQuery) window.jQuery('#' + id).val(v).trigger('change');
            }
            jqSet('carro_solicitanteInputEmail', email);
            jqSet('carro_solicitanteInputEmailConfirm', emailC);
            jqSet('carro_solicitanteInputRunSolicitante', run);
            jqSet('carro_solicitanteInputNDocSolicitante', numero);
            jqSet('carro_solicitanteInputTelefono', tel);
            const f = document.getElementById('idContinuarEntregaDocumentos');
            if (f) {
              function set(n,v){ const e = f.elements[n]; if (e) e.value = v || ''; }
              set('carro_email', email); set('carro_emailConfirm', emailC);
              set('runSol', run); set('numeroDocumentoSol', numero); set('carro_telefono', tel);
            }
        }""",
        [email, email_c, run, numero, telefono],
    )
    page.wait_for_timeout(800)
    try:
        page.locator("#carro_btnContinuar").click()
    except Exception:
        try:
            page.evaluate("document.getElementById('idContinuarEntregaDocumentos')?.submit();")
        except Exception:
            return {"ok": False, "message": "No se pudo pulsar Continuar", "url": page.url}

    page.wait_for_timeout(3000)
    deadline = time.monotonic() + max(wait_sec, 120.0)
    last_err: str | None = None
    while time.monotonic() < deadline:
        url = (page.url or "").lower()
        if "entregadocumentosfreepagado" in url or "freepagado" in url:
            to_client.put(("log", "RC: confirmación alcanzada."))
            return {"ok": True, "message": "Confirmación RC", "url": page.url}
        if "entregadocumentos" in url and "carro.srcei" not in url:
            return {"ok": True, "message": "Confirmación RC (entrega)", "url": page.url}
        src = _content(page)
        last_err = extract_rc_div_id_error_message(src)
        if last_err and _rc_error_element_visible(src, "idErrorMsnjAlMenosUnCert"):
            return {"ok": False, "message": last_err, "url": page.url}
        page.wait_for_timeout(600)
    return {
        "ok": False,
        "message": last_err or "Timeout tras Continuar",
        "url": page.url,
    }


def fetch_cookie_header_via_chrome_interactive(
    *,
    run: str,
    filtro: str,
    id_certificado: str,
    pack_specs: list[tuple[str, str]] | None = None,
    headless: bool = True,
    timeout: int,
    manual_captcha_timeout_sec: int | None,
    chrome_binary: str | None,
    to_client: "queue.Queue[tuple[str, Any]]",
    from_client: "queue.Queue[tuple[str, Any]]",
    email: str = "",
    email_confirm: str = "",
    telefono: str = "",
    numero_documento: str = "",
    complete_entrega_in_browser: bool = True,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Instala Playwright: pip install playwright && playwright install chromium") from e

    del chrome_binary
    url_carro = f"{OFICINA}/carro.srcei"
    wait_jsf = float(
        max(timeout, manual_captcha_timeout_sec or 0) if manual_captcha_timeout_sec else timeout
    )
    answer_timeout = min(600.0, max(120.0, wait_jsf))
    pack_list = list(pack_specs or [])
    if not pack_list and id_certificado:
        pack_list = [("certificado", id_certificado)]

    if not headless and not (os.environ.get("DISPLAY") or "").strip():
        headless = True
        to_client.put(
            (
                "log",
                "Sin $DISPLAY: forzando Chromium headless (obligatorio en Render/Docker).",
            )
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--lang=es-CL"],
        )
        context = browser.new_context(
            user_agent=UA_CHROME,
            locale="es-CL",
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(int((wait_jsf + 60) * 1000))
        page = context.new_page()
        try:
            if not headless:
                to_client.put(
                    (
                        "log",
                        "Navegador Playwright abierto: resuelve reCAPTCHA/desafío si aparece.",
                    )
                )
            else:
                to_client.put(("log", "Playwright headless: captcha por imagen en esta página si aplica."))

            to_client.put(("log", "Abriendo carro.srcei (en Render puede tardar 30–90 s)…"))
            try:
                page.goto(
                    url_carro,
                    wait_until="domcontentloaded",
                    timeout=int(min(120, wait_jsf) * 1000),
                )
            except Exception as e:
                to_client.put(("error", f"carro.srcei no cargó: {type(e).__name__}: {e}"))
                return {"cookies": "", "entrega_ok": False, "entrega_detail": str(e)}
            page.wait_for_timeout(1500)
            to_client.put(("log", f"carro cargado ({len(_content(page))} bytes HTML)."))
            if not _poll_until_ready_pw(
                page,
                to_client,
                from_client,
                deadline=time.monotonic() + min(wait_jsf, 120.0),
                phase="carro_inicio",
                answer_timeout_sec=answer_timeout,
                headless=headless,
            ):
                return {"cookies": "", "entrega_ok": False, "entrega_detail": "Captcha/sesión RC"}

            agregar_results: list[dict[str, Any]] = []
            for tipo, cid in pack_list:
                params = {"filtro": filtro, "idCertificado": cid, "run": run}
                url_agregar = f"{OFICINA}/agregarACarro.srcei?{urlencode(params)}"
                to_client.put(("log", f"Agregando {tipo} ({cid})…"))
                ok = _agregar_via_iframe_pw(page, url_agregar, wait_jsf)
                agregar_results.append({"tipo": tipo, "id_certificado": cid, "ok_agregar": ok})
                if ok:
                    to_client.put(("log", f"{tipo}: en carrito."))
                else:
                    to_client.put(("log", f"{tipo}: no agregado (puede no aplicar al RUN)."))

            page.goto(url_carro, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            _poll_until_ready_pw(
                page,
                to_client,
                from_client,
                deadline=time.monotonic() + wait_jsf,
                phase="carro",
                answer_timeout_sec=answer_timeout,
                headless=headless,
            )
            _maybe_recover_short_html_pw(page, wait_jsf)

            final = _content(page)
            if _is_rc_waf_or_non_jsf_shell_html(final) and not _cart_has_certificate_items(final):
                return {"cookies": "", "entrega_ok": False, "entrega_detail": "Sesión RC inválida"}

            entrega_result: dict[str, Any] = {"ok": False, "message": "Entrega no ejecutada", "url": ""}
            if complete_entrega_in_browser and _cart_has_certificate_items(final):
                entrega_result = _complete_entrega_pw(
                    page,
                    to_client,
                    email=email,
                    email_confirm=email_confirm or email,
                    telefono=telefono,
                    run=run,
                    numero=numero_documento,
                    wait_sec=min(wait_jsf, 180.0),
                )

            cookies = _cookies_to_header(context.cookies())
            return {
                "cookies": cookies,
                "entrega_ok": bool(entrega_result.get("ok")),
                "entrega_detail": str(entrega_result.get("message") or ""),
                "entrega_url": str(entrega_result.get("url") or ""),
                "pack_tres_certificados": len(pack_list) > 1,
                "agregar_results": agregar_results,
            }
        finally:
            context.close()
            browser.close()


def _maybe_recover_short_html_pw(page: Any, wait_jsf: float) -> None:
    src = _content(page)
    low = src.lower()
    if len(src) >= 4000 or "javax.faces.viewstate" in low:
        return
    if _is_rc_waf_or_non_jsf_shell_html(src):
        return
    _stderr(f"HTML corto ({len(src)} b); refresh carro…")
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(2000)
