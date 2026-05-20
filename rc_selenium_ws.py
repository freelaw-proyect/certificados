"""
Selenium con relevo de captcha por colas (hilo) ↔ WebSocket (async).
Chrome puede ir headless: la imagen del desafío se envía al cliente por WS.
"""

from __future__ import annotations

import base64
import queue
import sys
import time
from typing import Any
from urllib.parse import urlencode

from rc_selenium import (
    OFICINA,
    _cart_has_certificate_items,
    _challenge_requires_user_in_browser,
    _cookies_to_header,
    _is_agregar_iframe_shell_html,
    _is_captcha_interstitial_html,
    _is_recaptcha_html,
    _is_rc_waf_or_non_jsf_shell_html,
    _maybe_recover_short_html,
    _session_ready_to_continue,
    _wait_document_complete,
    _wait_for_ready,
    extract_rc_div_id_error_message,
    _rc_error_element_visible,
)


def _stderr(msg: str) -> None:
    print(f"[registrocivil-ws] {msg}", file=sys.stderr, flush=True)


def _page_has_jsf_cart(src: str) -> bool:
    low = src.lower()
    return "javax.faces.viewstate" in low


def _img_src_attr(im: Any) -> str:
    try:
        return (im.get_attribute("src") or "") + " " + (im.get_attribute("id") or "") + " " + (im.get_attribute("class") or "")
    except Exception:
        return ""


def _captcha_img_score(im: Any) -> float | None:
    """
    Puntuación del candidato a imagen de código (no max área: el logo del RC suele ser más grande).
    Devuelve None si se descarta (logo/banner).
    """
    try:
        if not im.is_displayed():
            return None
        meta = _img_src_attr(im).lower()
        if any(
            x in meta
            for x in (
                "logo",
                "escudo",
                "banner",
                "header",
                "principal/",
                "gobierno",
                "identificacion",
                "servicios-en-linea",
                "servicios_en",
                "srcei/img/logo",
                "marca",
                "subsecretaria",
            )
        ):
            return None
        sz = im.size
        w, h = int(sz.get("width", 0)), int(sz.get("height", 0))
        if w < 55 or h < 22 or w > 400 or h > 200:
            return None
        ar = w / max(h, 1)
        if ar > 3.0:
            return None
        if w > 260 and h < 55:
            return None

        score = 5.0
        if any(x in meta for x in ("captcha", "kaptcha", "verify", "challenge", "codeimage", "jcaptcha", "image.jsp", "image?")):
            score += 120.0
        if "registrocivil" in meta and ("random" in meta or "id=" in meta or "cid=" in meta):
            score += 35.0
        if 95 <= w <= 320 and 38 <= h <= 120:
            score += 55.0
        if 38 <= h <= 95 and ar <= 2.8:
            score += 20.0
        score -= (w * h) / 80000.0
        return score
    except Exception:
        return None


def _captcha_visual_payload(driver: Any) -> dict[str, str]:
    """
    Imagen del desafío RC: el sitio usa ``<img alt="Red dot" src="data:image/...;base64,...">``.
    Si hay data-URL en src, se reenvía tal cual (mime correcto). Si no, screenshot PNG del nodo.
    No usa captura de pantalla completa (evita caja vacía cuando el carrito ya cargó).
    """
    from selenium.webdriver.common.by import By

    src0 = driver.page_source or ""
    if _session_ready_to_continue(src0) and not _is_captcha_interstitial_html(src0):
        return {}
    if not (
        _is_captcha_interstitial_html(src0)
        or _is_recaptcha_html(src0)
        or (_is_rc_waf_or_non_jsf_shell_html(src0) and not _session_ready_to_continue(src0))
    ):
        return {}

    def _from_red_dot_images() -> dict[str, str]:
        try:
            for im in driver.find_elements(By.TAG_NAME, "img"):
                try:
                    alt = (im.get_attribute("alt") or "").strip().lower()
                    if alt != "red dot" or not im.is_displayed():
                        continue
                    src_attr = (im.get_attribute("src") or "").strip()
                    if src_attr.startswith("data:image/") and "base64," in src_attr:
                        return {"image_data_url": src_attr}
                    png = im.screenshot_as_png
                    return {"image_base64": base64.standard_b64encode(png).decode("ascii")}
                except Exception:
                    continue
        except Exception:
            pass
        return {}

    def _from_xpath_and_score() -> dict[str, str]:
        try:
            preferred: list[Any] = []
            for xp in (
                "//*[contains(.,'código de la imagen') or contains(.,'codigo de la imagen')]"
                "//ancestor::table[1]//img",
                "//*[contains(.,'resolver el desafío') or contains(.,'resolver el desafio')]"
                "//following::img[position()<=8]",
                "//*[contains(.,'Cuál es el código') or contains(.,'cual es el codigo')]"
                "//following::img[position()<=6]",
                "//label[contains(.,'código') or contains(.,'codigo')]//img",
                "//label[contains(.,'código') or contains(.,'codigo')]/following::img[1]",
            ):
                try:
                    for el in driver.find_elements(By.XPATH, xp):
                        if el not in preferred:
                            preferred.append(el)
                except Exception:
                    continue

            candidates: list[Any] = []
            seen: set[int] = set()
            for el in preferred:
                try:
                    iid = id(el)
                    if iid in seen:
                        continue
                    seen.add(iid)
                    candidates.append(el)
                except Exception:
                    continue
            for im in driver.find_elements(By.TAG_NAME, "img"):
                try:
                    iid = id(im)
                    if iid in seen:
                        continue
                    seen.add(iid)
                    candidates.append(im)
                except Exception:
                    continue

            best_el = None
            best_score = -1.0
            for im in candidates:
                sc = _captcha_img_score(im)
                if sc is not None and sc > best_score:
                    best_score = sc
                    best_el = im

            if best_el is not None and best_score > 0:
                try:
                    src_attr = (best_el.get_attribute("src") or "").strip()
                    if src_attr.startswith("data:image/") and "base64," in src_attr:
                        return {"image_data_url": src_attr}
                    png = best_el.screenshot_as_png
                    return {"image_base64": base64.standard_b64encode(png).decode("ascii")}
                except Exception:
                    return {}
        except Exception as e:
            _stderr(f"captcha img heurística: {e}")
        return {}

    out = _from_red_dot_images()
    if out:
        return out
    return _from_xpath_and_score()


def _submit_captcha_answer(driver: Any, text: str) -> bool:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    t = (text or "").strip()
    if not t:
        return False
    inp = None
    try:
        for xp in (
            "//input[@id='ans']",
            "//input[@name='answer']",
            "//input[contains(@placeholder,'código') or contains(@placeholder,'codigo')]",
            "//input[contains(translate(@name,'CAPTCHA','captcha'),'captcha')]",
            "//input[contains(translate(@id,'CAPTCHA','captcha'),'captcha')]",
            "//*[contains(.,'código de la imagen') or contains(.,'codigo de la imagen')]"
            "//following::input[@type='text'][1]",
        ):
            try:
                el = driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    inp = el
                    break
            except Exception:
                continue
        if inp is None:
            for el in driver.find_elements(By.XPATH, "//input"):
                try:
                    typ = (el.get_attribute("type") or "text").lower()
                    if typ not in ("text", "tel", None, ""):
                        continue
                    if el.is_displayed():
                        inp = el
                        break
                except Exception:
                    continue
    except Exception:
        pass
    if inp is None:
        return False
    try:
        inp.clear()
        inp.send_keys(t)
    except Exception:
        return False
    for xp in (
        "//button[@id='jar']",
        "//button[@type='button' and contains(translate(.,'SUBMIT','submit'),'submit')]",
        "//input[@type='submit']",
        "//button[@type='submit']",
        "//input[contains(translate(@value,'ENVIAR','enviar'),'enviar')]",
        "//button[contains(.,'Enviar') or contains(.,'enviar')]",
    ):
        try:
            btn = driver.find_element(By.XPATH, xp)
            if btn.is_displayed():
                btn.click()
                time.sleep(1.5)
                return True
        except Exception:
            continue
    try:
        inp.send_keys(Keys.RETURN)
        time.sleep(1.5)
        return True
    except Exception:
        return False


def _challenge_kind(src: str) -> str:
    if _is_recaptcha_html(src):
        return "recaptcha"
    if _is_captcha_interstitial_html(src):
        return "image"
    return "waf"


def _wait_captcha_solved_with_relay(
    driver: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    from_client: "queue.Queue[tuple[str, Any]]",
    *,
    phase: str,
    answer_timeout_sec: float,
) -> bool:
    """Muestra la imagen del desafío en la web y espera el código; si el carrito ya cargó, sigue solo."""
    end = time.monotonic() + answer_timeout_sec

    def _ready() -> bool:
        return _session_ready_to_continue(driver.page_source or "")

    if _ready():
        to_client.put(("log", f"{phase}: carrito RC ya visible en Chrome; continuando automáticamente…"))
        return True

    vis: dict[str, str] = {}
    while time.monotonic() < end:
        if _ready():
            to_client.put(("log", f"{phase}: carrito listo; continuando sin código de imagen…"))
            return True
        vis = _captcha_visual_payload(driver)
        if vis.get("image_data_url") or vis.get("image_base64"):
            break
        time.sleep(0.45)

    if not vis.get("image_data_url") and not vis.get("image_base64"):
        if _is_recaptcha_html(driver.page_source or ""):
            return _wait_challenge_solved_in_chrome(driver, to_client, phase=phase, deadline=end)
        to_client.put(("log", f"{phase}: sin imagen de desafío; esperando carrito en Chrome…"))
        return _wait_challenge_solved_in_chrome(driver, to_client, phase=phase, deadline=end)

    to_client.put(("captcha", {"phase": phase, **vis}))
    while time.monotonic() < end:
        if _ready():
            to_client.put(("log", f"{phase}: carrito listo en Chrome; continuando automáticamente…"))
            return True
        try:
            msg = from_client.get(timeout=0.5)
        except queue.Empty:
            continue
        if isinstance(msg, tuple) and msg[0] == "captcha_skip":
            if _ready():
                to_client.put(("log", f"{phase}: continuando por confirmación del usuario…"))
                return True
            continue
        if not isinstance(msg, tuple) or msg[0] != "captcha_answer":
            to_client.put(("error", f"Mensaje inesperado durante captcha ({phase})"))
            return False
        ans = (msg[1] or "").strip()
        if not ans:
            continue
        to_client.put(("log", f"{phase}: enviando código al Registro Civil…"))
        if not _submit_captcha_answer(driver, ans):
            to_client.put(("error", f"No se pudo ingresar el código ({phase})"))
            return False
        to_client.put(("log", f"{phase}: código enviado; esperando respuesta del RC…"))
        _wait_document_complete(driver, min(45.0, answer_timeout_sec))
        time.sleep(1.2)
        return True
    to_client.put(("error", f"Timeout esperando el código del desafío ({phase})"))
    return False


def _resolve_challenge(
    driver: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    from_client: "queue.Queue[tuple[str, Any]]",
    *,
    phase: str,
    deadline: float,
    answer_timeout_sec: float,
) -> bool:
    """Imagen+código en la web; reCAPTCHA solo en Chrome si no hay imagen."""
    src = driver.page_source or ""
    if _session_ready_to_continue(src):
        return True
    if _is_recaptcha_html(src) and not _is_captcha_interstitial_html(src):
        return _wait_challenge_solved_in_chrome(driver, to_client, phase=phase, deadline=deadline)
    if _is_captcha_interstitial_html(src) or _is_rc_waf_or_non_jsf_shell_html(src):
        return _wait_captcha_solved_with_relay(
            driver, to_client, from_client, phase=phase, answer_timeout_sec=answer_timeout_sec
        )
    if _challenge_requires_user_in_browser(src):
        return _wait_captcha_solved_with_relay(
            driver, to_client, from_client, phase=phase, answer_timeout_sec=answer_timeout_sec
        )
    return True


def _wait_challenge_solved_in_chrome(
    driver: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    *,
    phase: str,
    deadline: float,
) -> bool:
    """
    El usuario resuelve el desafío solo en la ventana de Chrome (reCAPTCHA, imagen o WAF).
    Esta página web solo muestra instrucciones; no pide códigos.
    """
    notified = False
    last_progress = 0.0
    while time.monotonic() < deadline:
        src = driver.page_source or ""
        if not _challenge_requires_user_in_browser(src):
            if notified:
                to_client.put(("log", f"{phase}: desafío superado; continuando automáticamente…"))
            return True
        if _is_agregar_iframe_shell_html(src) or (
            _page_has_jsf_cart(src) and not _challenge_requires_user_in_browser(src)
        ):
            return True
        if not notified:
            kind = _challenge_kind(src)
            if kind == "recaptcha":
                msg = (
                    f"{phase}: resuelve el reCAPTCHA en la ventana de Chrome que se abrió. "
                    "No escribas nada en esta página: al terminar, el certificado se pedirá solo "
                    "y llegará al correo configurado en .env."
                )
            elif kind == "image":
                msg = (
                    f"{phase}: resuelve el código de la imagen en la ventana de Chrome "
                    "(no en esta página). Luego todo es automático."
                )
            else:
                msg = (
                    f"{phase}: completa el desafío de seguridad en la ventana de Chrome. "
                    "El resto del flujo es automático."
                )
            to_client.put(("manual_challenge", {"phase": phase, "kind": kind, "message": msg}))
            notified = True
        now = time.monotonic()
        if now - last_progress >= 20.0:
            last_progress = now
            to_client.put(
                (
                    "log",
                    f"{phase}: esperando que termines en Chrome… ({len(src)} bytes, {driver.current_url!r})",
                )
            )
        time.sleep(0.45)
    to_client.put(("error", f"Timeout: no se superó el desafío en Chrome ({phase})"))
    return False


def _poll_until_jsf_or_captcha(
    driver: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    from_client: "queue.Queue[tuple[str, Any]]",
    *,
    deadline: float,
    phase: str,
    answer_timeout_sec: float,
) -> bool:
    """True si hay ViewState (carrito) o agregar iframe listo. False si error en cola o tiempo agotado."""
    last_progress_log = 0.0
    while time.monotonic() < deadline:
        src = driver.page_source or ""
        if _session_ready_to_continue(src):
            if phase == "agregarACarro" and _is_agregar_iframe_shell_html(src):
                to_client.put(
                    (
                        "log",
                        f"{phase}: RC respondió (HTML agregar/iframe, {len(src)} bytes). "
                        "Pantalla en blanco en Chrome es normal; sigue a carro…",
                    )
                )
                return True
            if _page_has_jsf_cart(src) or "<form" in src.lower():
                return True
        if _challenge_requires_user_in_browser(src) or _is_captcha_interstitial_html(src):
            if not _resolve_challenge(
                driver,
                to_client,
                from_client,
                phase=phase,
                deadline=deadline,
                answer_timeout_sec=answer_timeout_sec,
            ):
                return False
            time.sleep(0.8)
            continue
        if phase == "agregarACarro" and _is_agregar_iframe_shell_html(src):
            to_client.put(
                (
                    "log",
                    f"{phase}: RC respondió (HTML agregar/iframe, {len(src)} bytes). "
                    "Pantalla en blanco en Chrome es normal; sigue a carro…",
                )
            )
            return True
        if _page_has_jsf_cart(src):
            return True
        now = time.monotonic()
        if now - last_progress_log >= 15.0:
            last_progress_log = now
            to_client.put(
                ("log", f"{phase}: esperando… ({len(src)} bytes HTML, url={driver.current_url!r})")
            )
        time.sleep(0.35)
    src_end = driver.page_source or ""
    if phase == "agregarACarro" and _is_agregar_iframe_shell_html(src_end):
        return True
    return _page_has_jsf_cart(src_end)


def _agregar_certificado_via_iframe(driver: Any, url_agregar: str, wait_sec: float) -> bool:
    """Carga agregarACarro en #cu_idIframe4 (como el navegador), no como pestaña suelta."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver.switch_to.default_content()
    WebDriverWait(driver, min(wait_sec, 45.0)).until(
        EC.presence_of_element_located((By.ID, "cu_idIframe4"))
    )
    driver.execute_script(
        "var f = document.getElementById('cu_idIframe4'); if (f) f.src = arguments[0];",
        url_agregar,
    )
    time.sleep(2.5)
    try:
        iframe = driver.find_element(By.ID, "cu_idIframe4")
        driver.switch_to.frame(iframe)
        deadline = time.monotonic() + min(wait_sec, 90.0)
        while time.monotonic() < deadline:
            if _is_agregar_iframe_shell_html(driver.page_source or ""):
                break
            time.sleep(0.5)
    except Exception:
        pass
    finally:
        driver.switch_to.default_content()

    time.sleep(3.0)
    deadline2 = time.monotonic() + min(wait_sec, 90.0)
    while time.monotonic() < deadline2:
        if _cart_has_certificate_items(driver.page_source or ""):
            return True
        time.sleep(0.6)
    return _cart_has_certificate_items(driver.page_source or "")


def _wait_carro_ui_ready(driver: Any, timeout: float) -> None:
    """Espera preloader y botón Continuar (el RC bloquea clics mientras carga)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, "#preloader"))
        )
    except Exception:
        pass
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.ID, "carro_btnContinuar")))


def _fill_entrega_fields_in_browser(
    driver: Any,
    *,
    email: str,
    email_confirm: str,
    telefono: str,
    run: str,
    numero: str,
) -> None:
    """Rellena inputs visibles, divs ocultos (#correo, #runSol) y el form POST de entrega."""
    from selenium.webdriver.common.by import By

    email_c = email_confirm or email
    try:
        driver.execute_script(
            """
            var email = arguments[0], emailC = arguments[1], run = arguments[2],
                numero = arguments[3], tel = arguments[4];
            function jqSet(id, v) {
              if (window.jQuery) {
                var $el = window.jQuery('#' + id);
                if ($el.length) { $el.val(v).trigger('input').trigger('change').trigger('blur'); }
              }
            }
            jqSet('carro_solicitanteInputEmail', email);
            jqSet('carro_solicitanteInputEmailConfirm', emailC);
            jqSet('carro_solicitanteInputRunSolicitante', run);
            jqSet('carro_solicitanteInputNDocSolicitante', numero);
            jqSet('carro_solicitanteInputTelefono', tel);
            var correo = document.getElementById('correo');
            var conf = document.getElementById('confirmacion');
            var runSol = document.getElementById('runSol');
            var ndoc = document.getElementById('numeroDoc');
            if (correo) correo.textContent = email;
            if (conf) conf.textContent = emailC;
            if (runSol) runSol.textContent = run;
            if (ndoc) ndoc.textContent = numero;
            var f = document.getElementById('idContinuarEntregaDocumentos');
            if (!f) return;
            function set(n,v){ var e = f.elements[n]; if (e) e.value = v || ''; }
            set('carro_email', email);
            set('carro_emailConfirm', emailC);
            set('runSol', run);
            set('numeroDocumentoSol', numero);
            set('carro_telefono', tel);
            set('carro_solicitanteInputMailName', email);
            set('carro_solicitanteInputMailConfirmName', emailC);
            set('carro_solicitanteInputRunSolicitante', run);
            set('carro_solicitanteInputNDocSolicitante', numero);
            """,
            email,
            email_c,
            run,
            numero,
            telefono,
        )
    except Exception:
        pass

    def _set(by: str, selector: str, value: str) -> None:
        if not value:
            return
        try:
            el = driver.find_element(by, selector)
            el.clear()
            el.send_keys(value)
        except Exception:
            pass

    _set(By.ID, "carro_solicitanteInputEmail", email)
    _set(By.ID, "carro_solicitanteInputEmailConfirm", email_c)
    _set(By.ID, "carro_solicitanteInputRunSolicitante", run)
    _set(By.ID, "carro_solicitanteInputNDocSolicitante", numero)
    _set(By.ID, "carro_solicitanteInputTelefono", telefono)


def _complete_entrega_in_browser(
    driver: Any,
    to_client: "queue.Queue[tuple[str, Any]]",
    *,
    email: str,
    email_confirm: str,
    telefono: str,
    run: str,
    numero: str,
    wait_sec: float,
) -> dict[str, Any]:
    """
    Tras agregar al carro: rellena correo/RUN/documento y pulsa Continuar como el usuario.
    """
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    to_client.put(("log", "Rellenando correo/RUN en el carrito y pulsando Continuar en Chrome…"))
    try:
        _wait_carro_ui_ready(driver, min(45.0, wait_sec))
    except TimeoutException:
        return {"ok": False, "message": "El carrito RC no terminó de cargar (preloader/Continuar)", "url": driver.current_url}

    try:
        driver.execute_script(
            "if (typeof getOrdenDeCompra === 'function') { getOrdenDeCompra(); }"
        )
        time.sleep(3.0)
    except Exception:
        pass
    _fill_entrega_fields_in_browser(
        driver,
        email=email,
        email_confirm=email_confirm,
        telefono=telefono,
        run=run,
        numero=numero,
    )
    time.sleep(1.0)
    clicked = False
    try:
        driver.execute_script(
            """
            var btn = document.getElementById('carro_btnContinuar');
            if (window.jQuery && btn) { window.jQuery(btn).trigger('click'); return true; }
            if (btn) { btn.click(); return true; }
            return false;
            """
        )
        clicked = True
    except Exception:
        pass
    if not clicked:
        try:
            btn = WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.ID, "carro_btnContinuar")))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            btn.click()
            clicked = True
        except TimeoutException:
            try:
                driver.execute_script(
                    "var f=document.getElementById('idContinuarEntregaDocumentos'); if(f) f.submit();"
                )
                clicked = True
            except Exception:
                return {
                    "ok": False,
                    "message": "No se pudo pulsar Continuar ni enviar el formulario de entrega",
                    "url": driver.current_url,
                }

    time.sleep(3.0)
    deadline = time.monotonic() + max(wait_sec, 120.0)
    last_err: str | None = None
    while time.monotonic() < deadline:
        url = (driver.current_url or "").lower()
        if (
            "entregadocumentosfreepagado" in url
            or "freepagado" in url
            or ("entregadocumentos" in url and "carro.srcei" not in url)
        ):
            to_client.put(("log", "RC: página de confirmación alcanzada."))
            return {"ok": True, "message": "Confirmación RC (entrega)", "url": driver.current_url}
        src = driver.page_source or ""
        low = src.lower()
        last_err = extract_rc_div_id_error_message(src)
        if last_err and "iderror" in low:
            chunk = low.split("iderror", 1)[1][:200]
            if "display:none" not in chunk and "display: none" not in chunk:
                to_client.put(("log", f"RC (idError): {last_err}"))
                return {"ok": False, "message": last_err, "url": driver.current_url}
        if _rc_error_element_visible(src, "idErrorMsnjAlMenosUnCert"):
            return {"ok": False, "message": "Debe ingresar al menos un certificado", "url": driver.current_url}
        time.sleep(0.6)

    return {
        "ok": False,
        "message": last_err or "Timeout esperando confirmación del RC tras Continuar",
        "url": driver.current_url,
    }


def fetch_cookie_header_via_chrome_interactive(
    *,
    run: str,
    filtro: str,
    id_certificado: str,
    pack_specs: list[tuple[str, str]] | None = None,
    headless: bool,
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
    """
    Captcha en Chrome; si ``complete_entrega_in_browser``, completa Continuar en el mismo navegador.
    Devuelve cookies y resultado de entrega en browser (evita POST httpx que el RC suele rechazar).
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as e:
        raise RuntimeError("Instala selenium: pip install selenium") from e

    from rc_selenium import UA_CHROME

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(f"--user-agent={UA_CHROME}")
    opts.add_argument("--lang=es-CL")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    try:
        opts.page_load_strategy = "eager"
    except Exception:
        pass
    if chrome_binary:
        opts.binary_location = chrome_binary

    url_carro = f"{OFICINA}/carro.srcei"
    pack_list = pack_specs or []
    if not pack_list and id_certificado:
        pack_list = [("certificado", id_certificado)]

    wait_jsf = float(
        max(timeout, manual_captcha_timeout_sec or 0) if manual_captcha_timeout_sec else timeout
    )
    answer_timeout = min(600.0, max(120.0, wait_jsf))

    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(int(wait_jsf) + 60)
        if not headless:
            to_client.put(
                (
                    "log",
                    "Se abrió Chrome: resuelve el reCAPTCHA/desafío ahí. Esta página solo muestra el progreso.",
                )
            )

        to_client.put(("log", "Abriendo carro.srcei (sesión inicial, como en el navegador)…"))
        try:
            driver.get(url_carro)
        except Exception as e:
            _stderr(f"GET carro inicio: {e}")
        _wait_document_complete(driver, min(45.0, wait_jsf))
        time.sleep(1.0)
        deadline0 = time.monotonic() + min(wait_jsf, 120.0)
        _poll_until_jsf_or_captcha(
            driver, to_client, from_client, deadline=deadline0, phase="carro_inicio", answer_timeout_sec=answer_timeout
        )

        agregar_results: list[dict[str, Any]] = []
        for tipo, cid in pack_list:
            params = {"filtro": filtro, "idCertificado": cid, "run": run}
            url_agregar = f"{OFICINA}/agregarACarro.srcei?{urlencode(params)}"
            to_client.put(("log", f"Agregando {tipo} ({cid}) en iframe del carrito…"))
            ok = _agregar_certificado_via_iframe(driver, url_agregar, wait_jsf)
            agregar_results.append(
                {"tipo": tipo, "id_certificado": cid, "ok_agregar": ok}
            )
            if ok:
                to_client.put(("log", f"{tipo}: agregado al carrito."))
            else:
                to_client.put(
                    (
                        "log",
                        f"{tipo}: no se agregó (puede no existir para este RUN); se continúa con el resto.",
                    )
                )
        try:
            driver.get(url_carro)
        except Exception as e:
            _stderr(f"GET carro tras pack agregar: {e}")
        _wait_document_complete(driver, min(45.0, wait_jsf))
        time.sleep(1.5)

        deadline2 = time.monotonic() + wait_jsf
        if not _poll_until_jsf_or_captcha(
            driver, to_client, from_client, deadline=deadline2, phase="carro", answer_timeout_sec=answer_timeout
        ):
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.support.ui import WebDriverWait

            def _carro_jsf_ready(d: Any) -> bool:
                s = d.page_source or ""
                lo = s.lower()
                if _is_rc_waf_or_non_jsf_shell_html(s):
                    return False
                return "javax.faces.viewstate" in lo

            try:
                WebDriverWait(driver, min(wait_jsf, 90.0)).until(_carro_jsf_ready)
            except TimeoutException:
                pass

        _maybe_recover_short_html(driver, wait_jsf, "carro.srcei")

        final = driver.page_source or ""
        low = final.lower()
        if _is_rc_waf_or_non_jsf_shell_html(final) and not _page_has_jsf_cart(final):
            return {"cookies": "", "entrega_ok": False, "entrega_detail": "Sesión RC inválida tras captcha"}
        if "javax.faces.viewstate" not in low and "<form" not in low and not _cart_has_certificate_items(final):
            return {"cookies": "", "entrega_ok": False, "entrega_detail": "Carrito RC sin formulario"}

        entrega_result: dict[str, Any] = {"ok": False, "message": "Entrega en navegador no ejecutada", "url": ""}
        if complete_entrega_in_browser and _cart_has_certificate_items(final):
            entrega_result = _complete_entrega_in_browser(
                driver,
                to_client,
                email=email,
                email_confirm=email_confirm or email,
                telefono=telefono,
                run=run,
                numero=numero_documento,
                wait_sec=min(wait_jsf, 180.0),
            )

        return {
            "cookies": _cookies_to_header(driver.get_cookies()),
            "entrega_ok": bool(entrega_result.get("ok")),
            "entrega_detail": str(entrega_result.get("message") or ""),
            "entrega_url": str(entrega_result.get("url") or ""),
            "pack_tres_certificados": len(pack_list) > 1,
            "agregar_results": agregar_results,
        }
    finally:
        try:
            driver.quit()
        except Exception:
            pass
