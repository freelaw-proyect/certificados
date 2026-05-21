"""
Cookies de sesión vía Chrome headless: ejecuta el JS del WAF (F5/TSPD) que httpx no puede.

Requiere Google Chrome (o Chromium) instalado en la máquina donde corre el servicio.
"""

from __future__ import annotations

import os
import re
import sys
import time
from html import unescape
from typing import Any
from urllib.parse import urlencode

BASE = "https://www.registrocivil.cl"
OFICINA = f"{BASE}/OficinaInternet/web"
REF_SERVICIOS_LINEA = f"{BASE}/principal/servicios-en-linea"

# Mantener alineado con `UA` en main.py (misma cadena que httpx).
UA_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def _cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name is not None and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _is_rc_waf_or_non_jsf_shell_html(src: str) -> bool:
    """
    True si la respuesta no es el carrito JSF (WAF TSPD: captcha, bobcmn, página mínima sin <form>).
    """
    low = src.lower()
    if "javax.faces.viewstate" in low:
        return False
    if "<form" in low:
        return False
    return (
        "código de la imagen" in low
        or "codigo de la imagen" in low
        or "resolver el desafío" in low
        or "resolver el desafio" in low
        or "cuál es el código" in low
        or "cual es el codigo" in low
        or "bobcmn" in low
        or "/tspd/" in low
        or "support id is" in low
        or "please enable javascript" in low
        or "failureconfig" in low
        or "código de soporte" in low
        or "codigo de soporte" in low
        or "desafío a continuación" in low
        or "desafio a continuacion" in low
    )


def _is_agregar_iframe_shell_html(src: str) -> bool:
    """
    Respuesta de agregarACarro pensada para iframe dentro de carro.srcei.

    El JS llama a getOrdenDeCompra() y manipula window.parent; abierta sola en Chrome
    la pestaña suele verse en blanco aunque el Network muestre 200 y OrdenDeCompra.srcei.
    """
    if not src or len(src) < 400:
        return False
    low = src.lower()
    if "javax.faces.viewstate" in low:
        return True
    markers = (
        "getordendecompra",
        "agregaracarro.js",
        "initcerraragregarcarro",
        "cu_idiframe4",
        "divagregaracarro",
    )
    return sum(1 for m in markers if m in low) >= 2


def _is_recaptcha_html(src: str) -> bool:
    low = (src or "").lower()
    return (
        "recaptcha" in low
        or "g-recaptcha" in low
        or "grecaptcha" in low
        or "google.com/recaptcha" in low
    )


def _session_ready_to_continue(src: str) -> bool:
    """Carrito JSF o agregar listos: no hace falta captcha aunque antes hubiera WAF."""
    if not src:
        return False
    low = src.lower()
    if "javax.faces.viewstate" in low:
        return True
    if _is_agregar_iframe_shell_html(src):
        return True
    if "<form" in low and (
        "carro de certificados" in low
        or "su carro está vacío" in low
        or "su carro esta vacio" in low
        or "más solicitados" in low
        or "mas solicitados" in low
    ):
        return True
    return False


def _challenge_requires_user_in_browser(src: str) -> bool:
    """True mientras el usuario debe interactuar en Chrome (reCAPTCHA, imagen, WAF)."""
    if _session_ready_to_continue(src):
        return False
    if _is_recaptcha_html(src) or _is_captcha_interstitial_html(src):
        return True
    if _is_rc_waf_or_non_jsf_shell_html(src):
        return True
    return False


def _rc_error_element_visible(page_html: str, element_id: str) -> bool:
    """True si un div de error del RC (p. ej. idErrorMsnjAlMenosUnCert) está visible."""
    import re as _re

    pat = _re.compile(
        rf'<div[^>]*\bid\s*=\s*["\']{_re.escape(element_id)}["\'][^>]*>(.*?)</div>',
        _re.I | _re.DOTALL,
    )
    m = pat.search(page_html)
    if not m:
        return False
    open_tag = page_html[max(0, m.start() - 120) : m.start() + 80].lower()
    if "display:none" in open_tag or "display: none" in open_tag:
        return False
    return bool(_re.sub(r"<[^>]+>", "", m.group(1)).strip())


def _cart_has_certificate_items(html: str) -> bool:
    """True si el HTML del carrito muestra al menos un certificado agregado."""
    low = html.lower()
    return "carro_certificado_" in low or "carro_nombrecertificado_" in low


def extract_rc_div_id_error_message(page_html: str) -> str | None:
    """Texto en <div id=\"idError\">…</div> (fallos de entrega/pagado en el RC)."""
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


def _is_captcha_interstitial_html(src: str) -> bool:
    """Solo pantalla explícita de captcha (compat)."""
    low = src.lower()
    if "javax.faces.viewstate" in low or "<form" in low:
        return False
    return (
        "código de la imagen" in low
        or "codigo de la imagen" in low
        or "resolver el desafío" in low
        or "resolver el desafio" in low
        or "cuál es el código" in low
        or "cual es el codigo" in low
    )


def _is_waf_error_page_html(src: str) -> bool:
    """
    Pantalla de fallo F5/TSPD (p. ej. «Oops… something went wrong», support id).
    No es el captcha con código de imagen; no debe mostrarse como desafío al usuario.
    """
    if not src:
        return False
    low = src.lower()
    if _session_ready_to_continue(src) or _is_captcha_interstitial_html(src):
        return False
    if "oops" in low and ("something went wrong" in low or "went wrong" in low):
        return True
    if "support id is" in low and "código de la imagen" not in low and "codigo de la imagen" not in low:
        return True
    if "failureconfig" in low and "#ans" not in low and "name='answer'" not in low:
        return True
    return False


def load_registrocivil_cookie_from_env() -> str:
    """Cookie de sesión desde REGISTROCIVIL_COOKIE o REGISTROCIVIL_COOKIE_FILE."""
    raw = (os.environ.get("REGISTROCIVIL_COOKIE") or "").strip()
    if raw:
        return raw
    path_raw = (os.environ.get("REGISTROCIVIL_COOKIE_FILE") or "").strip()
    if not path_raw:
        return ""
    path = path_raw if os.path.isabs(path_raw) else os.path.join(os.getcwd(), path_raw)
    try:
        line = open(path, encoding="utf-8", errors="replace").readline().strip()
    except OSError:
        return ""
    if line.lower().startswith("cookie:"):
        line = line.split(":", 1)[1].strip()
    return line


def cookie_header_to_playwright_cookies(
    cookie_header: str,
    *,
    domain: str = ".registrocivil.cl",
) -> list[dict[str, Any]]:
    """Convierte cabecera Cookie a lista para ``context.add_cookies``."""
    out: list[dict[str, Any]] = []
    for raw in (cookie_header or "").split(";"):
        part = raw.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
            }
        )
    return out


def _page_ready(driver: Any) -> bool:
    """True cuando parece haber pasado el challenge TSPD y hay app JSF o formulario."""
    src = driver.page_source
    low = src.lower()
    if _is_rc_waf_or_non_jsf_shell_html(src):
        return False
    if "javax.faces.viewstate" in low:
        return True
    head = src[:8000]
    if "bobcmn" in head or "/TSPD/" in head:
        return False
    if len(src) < 6500:
        return False
    return "<form" in src.lower()


def _wait_document_complete(driver: Any, max_sec: float) -> None:
    """Espera document.readyState == complete (SPAs del RC a veces tardan tras el captcha)."""
    deadline = time.monotonic() + max_sec
    while time.monotonic() < deadline:
        try:
            if driver.execute_script("return document.readyState") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.15)


def _stderr_state(driver: Any, label: str) -> None:
    try:
        u = driver.current_url
        n = len(driver.page_source or "")
        print(f"[registrocivil] {label}: url={u!r} html_len={n}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[registrocivil] {label}: (no se pudo leer estado: {e})", file=sys.stderr, flush=True)


def _maybe_recover_short_html(driver: Any, wait_jsf: float, label: str) -> None:
    """
    Tras **carro.srcei** (no tras agregarACarro): si el DOM es casi vacío y no es desafío WAF,
    un refresh puede recuperar JSF. En agregarACarro un refresh suele **reiniciar el captcha**.
    """
    src = driver.page_source or ""
    low = src.lower()
    if len(src) >= 4000 or "javax.faces.viewstate" in low:
        return
    if _is_rc_waf_or_non_jsf_shell_html(src):
        return
    print(
        f"[registrocivil] HTML muy corto ({len(src)} b) tras {label}; reintentando con refresh…",
        file=sys.stderr,
        flush=True,
    )
    try:
        driver.refresh()
    except Exception:
        pass
    _wait_document_complete(driver, min(45.0, float(wait_jsf)))
    time.sleep(2.0)
    _wait_for_ready(driver, int(min(float(wait_jsf), 180.0)))


def _wait_for_ready(driver: Any, timeout: int) -> bool:
    """
    Espera hasta `timeout` s a que la página sea usable.
    Si no llega a tiempo, devuelve False (el caller decide si puede seguir).
    """
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        WebDriverWait(driver, timeout).until(lambda d: _page_ready(d))
        return True
    except TimeoutException:
        return False


def fetch_cookie_header_via_chrome(
    *,
    run: str,
    filtro: str,
    id_certificado: str,
    headless: bool = True,
    timeout: int = 60,
    chrome_binary: str | None = None,
    manual_captcha_timeout_sec: int | None = None,
) -> str:
    """
    Abre agregarACarro + carro en Chrome, espera lo posible al WAF y devuelve cabecera Cookie.

    Con ``headless=false``, suele aparecer el captcha del RC: hay que resolverlo en esa ventana;
    ``manual_captcha_timeout_seg`` alarga la espera (p. ej. 600) frente al ``timeout`` genérico.

    Si tras el timeout el carrito **no** muestra JSF (p. ej. sigue la pantalla de captcha TSPD),
    devuelve cadena vacía para que el API no siga con httpx usando cookies inútiles (orden_fields=0,
    entrega 404).
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as e:
        raise RuntimeError("Instala selenium: pip install selenium") from e

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-agent={UA_CHROME}")
    opts.add_argument("--lang=es-CL")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Evita que Chrome quede colgado esperando analytics/imágenes; reduce pantallas en blanco eternas.
    try:
        opts.page_load_strategy = "eager"
    except Exception:
        pass

    if chrome_binary:
        opts.binary_location = chrome_binary
    elif os.environ.get("REGISTROCIVIL_CHROME_BINARY"):
        opts.binary_location = os.environ["REGISTROCIVIL_CHROME_BINARY"]

    params = {"filtro": filtro, "idCertificado": id_certificado, "run": run}
    url_agregar = f"{OFICINA}/agregarACarro.srcei?{urlencode(params)}"
    url_carro = f"{OFICINA}/carro.srcei"

    wait_jsf = float(
        max(timeout, manual_captcha_timeout_sec or 0)
        if manual_captcha_timeout_sec
        else timeout
    )

    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(int(wait_jsf) + 45)
        if not headless:
            print(
                "[registrocivil] Chrome visible: si aparece «resolver el desafío», "
                "ingresa el código de la imagen en esta ventana (no en otra pestaña). "
                f"Esperando hasta {int(wait_jsf)} s a que cargue el carrito…",
                file=sys.stderr,
                flush=True,
            )
            print(
                "[registrocivil] No cierres la petición HTTP al API: usa timeout ≥ "
                f"{int(wait_jsf) + 90} s en curl/cliente o la respuesta se corta con Chrome aún abierto.",
                file=sys.stderr,
                flush=True,
            )

        # Misma secuencia que main.py (httpx): carro → agregar (iframe) → carro.
        try:
            driver.get(url_carro)
        except Exception as e:
            print(f"[registrocivil] GET carro (inicio): {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        _wait_document_complete(driver, min(45.0, float(wait_jsf)))
        time.sleep(1.0)

        try:
            driver.get(url_agregar)
        except Exception as e:
            print(f"[registrocivil] GET agregarACarro: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        _wait_document_complete(driver, min(60.0, float(wait_jsf)))
        time.sleep(1.5)
        src_ag = driver.page_source or ""
        if _is_agregar_iframe_shell_html(src_ag) and not headless:
            print(
                "[registrocivil] agregarACarro respondió (vista iframe; pantalla en blanco es normal). "
                "Siguiente paso: carro.srcei con las mismas cookies.",
                file=sys.stderr,
                flush=True,
            )
        elif not _page_ready(driver) and not headless:
            _wait_for_ready(driver, int(min(wait_jsf, 180.0)))
        if not headless:
            _stderr_state(driver, "tras agregarACarro")

        try:
            driver.get(url_carro)
        except Exception as e:
            # page_load_timeout deja a veces la pestaña en blanco hasta window.stop()
            print(f"[registrocivil] GET carro.srcei: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        _wait_document_complete(driver, min(60.0, float(wait_jsf)))
        time.sleep(2.0)
        try:
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.support.ui import WebDriverWait

            def _carro_jsf_ready(d: Any) -> bool:
                src = d.page_source
                low = src.lower()
                if _is_rc_waf_or_non_jsf_shell_html(src):
                    return False
                return "javax.faces.viewstate" in low

            WebDriverWait(driver, wait_jsf).until(_carro_jsf_ready)
        except TimeoutException:
            pass

        if not headless:
            _stderr_state(driver, "tras esperar carro.srcei")

        _maybe_recover_short_html(driver, wait_jsf, "carro.srcei")
        try:
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.support.ui import WebDriverWait

            def _carro_jsf_ready2(d: Any) -> bool:
                src = d.page_source
                low = src.lower()
                if _is_rc_waf_or_non_jsf_shell_html(src):
                    return False
                return "javax.faces.viewstate" in low

            WebDriverWait(driver, min(wait_jsf, 120.0)).until(_carro_jsf_ready2)
        except TimeoutException:
            pass

        final = driver.page_source
        low = final.lower()
        if _is_rc_waf_or_non_jsf_shell_html(final):
            return ""
        if "javax.faces.viewstate" not in low and "<form" not in low:
            return ""

        return _cookies_to_header(driver.get_cookies())
    finally:
        driver.quit()
