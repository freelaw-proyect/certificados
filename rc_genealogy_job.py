"""
Orquesta: parseo nacimiento → solicitudes RC por persona → PDFs del inbox → ZIP + árbol.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rc_arbol_familiar import render_family_tree_png
from rc_birth_cert import parse_birth_certificate_pdf, pdf_matches_run, run_digits
from rc_genealogy import (
    build_family_tree,
    load_birth_certs_from_dir,
    nodes_missing_parent_birth_cert,
    persons_in_fetch_order,
    tree_to_dict,
)


def _stderr(msg: str) -> None:
    print(f"[genealogia] {msg}", file=sys.stderr, flush=True)


def _collect_pdfs_for_run(inbox: Path, run: str) -> list[Path]:
    if not inbox.is_dir():
        return []
    found: list[Path] = []
    for pdf in inbox.rglob("*.pdf"):
        if pdf_matches_run(pdf.name, run):
            found.append(pdf.resolve())
    return sorted(set(found), key=lambda p: p.stat().st_mtime)


def _assign_certificates_to_nodes(nodes: dict, inbox: Path) -> None:
    from rc_genealogy import PersonNode

    for p in nodes.values():
        if not isinstance(p, PersonNode):
            continue
        pdfs = _collect_pdfs_for_run(inbox, p.run)
        p.certificados = pdfs
        for pdf in pdfs:
            if "NAC" in pdf.name.upper() or "nacimiento" in pdf.name.lower():
                p.birth_cert_path = pdf
                break


def _wait_for_new_pdfs(
    inbox: Path,
    run: str,
    *,
    before_count: int,
    timeout_sec: float,
    poll_sec: float,
) -> list[Path]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        pdfs = _collect_pdfs_for_run(inbox, run)
        if len(pdfs) > before_count:
            return pdfs
        time.sleep(poll_sec)
    return _collect_pdfs_for_run(inbox, run)


def run_genealogy_job(
    seed_pdf: Path,
    *,
    inbox_dir: Path,
    output_dir: Path,
    http_entrega_fn: Callable[..., dict[str, Any]],
    cookie: str,
    email: str,
    numero_solicitante: str,
    filtro: str,
    run_consulta_seed: str,
    run_solicitante_seed: str,
    max_generation: int = 2,
    poll_after_request_sec: float = 90.0,
    poll_interval_sec: float = 5.0,
    skip_rc_requests: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    ``http_entrega_fn`` = función tipo ``_http_entrega_phase`` del main (inyectada para evitar ciclos).
    """
    log = on_progress or (lambda m: _stderr(m))

    seed_pdf = seed_pdf.resolve()
    if not seed_pdf.is_file():
        raise FileNotFoundError(f"No existe PDF semilla: {seed_pdf}")

    log(f"Parseando certificado semilla: {seed_pdf.name}")
    seed_cert = parse_birth_certificate_pdf(seed_pdf)
    if not seed_cert.run_inscrito:
        raise ValueError("El PDF no contiene RUN del inscrito (¿es certificado de nacimiento RC?)")

    inbox_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = output_dir / f"arbol_{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    # Copiar semilla al inbox si no está ahí
    if not str(seed_pdf).startswith(str(inbox_dir.resolve())):
        dest_seed = inbox_dir / seed_pdf.name
        if not dest_seed.is_file():
            shutil.copy2(seed_pdf, dest_seed)

    extra_bc = load_birth_certs_from_dir(inbox_dir)
    nodes = build_family_tree(seed_cert, extra_birth_certs=extra_bc, max_generation=max_generation)

    # Fase 1: solicitar pack RC por cada persona (orden generacional)
    solicitudes: list[dict[str, Any]] = []
    if not skip_rc_requests:
        for person in persons_in_fetch_order(nodes):
            before = len(_collect_pdfs_for_run(inbox_dir, person.run))
            log(f"Solicitando pack RC para {person.nombre or person.run} ({person.relacion})…")
            try:
                result = http_entrega_fn(
                    cookie,
                    run=person.run,
                    run_raw=person.run,
                    run_normalizado_aviso=None,
                    email=email,
                    numero=numero_solicitante,
                    run_consulta=run_consulta_seed,
                    run_solicitante=run_solicitante_seed,
                    filtro=filtro,
                    selenium_used=False,
                    cookie_origin="env",
                    selenium_fallback_to_env_cookie=False,
                    selenium_error=None,
                )
                person.solicitud_rc_ok = bool(result.get("ok"))
                person.solicitud_rc_detalle = str(
                    result.get("meta", {}).get("hint")
                    or result.get("meta", {})
                    or result.get("steps", "")
                )[:500]
            except Exception as e:
                person.solicitud_rc_ok = False
                person.solicitud_rc_detalle = f"{type(e).__name__}: {e}"
            solicitudes.append(
                {
                    "run": person.run,
                    "relacion": person.relacion,
                    "ok": person.solicitud_rc_ok,
                    "detalle": person.solicitud_rc_detalle,
                }
            )
            if poll_after_request_sec > 0:
                log(f"Esperando PDFs en inbox ({poll_after_request_sec:.0f}s)…")
                _wait_for_new_pdfs(
                    inbox_dir,
                    person.run,
                    before_count=before,
                    timeout_sec=poll_after_request_sec,
                    poll_sec=poll_interval_sec,
                )

    _assign_certificates_to_nodes(nodes, inbox_dir)

    # Fase 2: abuelos desde nacimiento de padres
    for parent in nodes_missing_parent_birth_cert(nodes):
        nac = _collect_pdfs_for_run(inbox_dir, parent.run)
        nac_only = [p for p in nac if "NAC" in p.name.upper()]
        if nac_only:
            try:
                bc = parse_birth_certificate_pdf(nac_only[0])
                parent.birth_cert_path = nac_only[0]
                extra_bc[run_digits(parent.run)] = bc
            except Exception:
                pass

    nodes = build_family_tree(seed_cert, extra_birth_certs=extra_bc, max_generation=max_generation)
    _assign_certificates_to_nodes(nodes, inbox_dir)

    # Abuelos sin nodo aún: segunda ronda RC solo para generación 2 faltante
    if not skip_rc_requests and max_generation >= 2:
        for person in persons_in_fetch_order(nodes):
            if person.generation != 2:
                continue
            if person.certificados:
                continue
            before = len(_collect_pdfs_for_run(inbox_dir, person.run))
            log(f"Solicitando pack para abuelo/a: {person.nombre or person.run}…")
            try:
                result = http_entrega_fn(
                    cookie,
                    run=person.run,
                    run_raw=person.run,
                    run_normalizado_aviso=None,
                    email=email,
                    numero=numero_solicitante,
                    run_consulta=run_consulta_seed,
                    run_solicitante=run_solicitante_seed,
                    filtro=filtro,
                    selenium_used=False,
                    cookie_origin="env",
                    selenium_fallback_to_env_cookie=False,
                    selenium_error=None,
                )
                person.solicitud_rc_ok = bool(result.get("ok"))
            except Exception as e:
                person.solicitud_rc_ok = False
                person.solicitud_rc_detalle = str(e)
            _wait_for_new_pdfs(
                inbox_dir,
                person.run,
                before_count=before,
                timeout_sec=poll_after_request_sec,
                poll_sec=poll_interval_sec,
            )
        _assign_certificates_to_nodes(nodes, inbox_dir)

    # Árbol PNG
    arbol_png = work / "arbol_familiar.png"
    render_family_tree_png(nodes, output_path=arbol_png, consultado_run=seed_cert.run_inscrito)

    # Carpetas por persona
    for person in persons_in_fetch_order(nodes):
        folder = work / person.folder_name()
        folder.mkdir(parents=True, exist_ok=True)
        if not person.certificados:
            (folder / "_sin_certificados.txt").write_text(
                f"No se encontraron PDF en inbox para RUN {person.run}\n",
                encoding="utf-8",
            )
            continue
        for i, src in enumerate(person.certificados):
            tipo = _guess_tipo_from_filename(src.name)
            dest = folder / f"{tipo}_{i + 1}_{src.name}"
            if not dest.is_file():
                shutil.copy2(src, dest)

    # ZIP final
    zip_path = output_dir / f"certificados_familia_{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(arbol_png, arcname="arbol_familiar.png")
        for person in persons_in_fetch_order(nodes):
            folder = work / person.folder_name()
            if not folder.is_dir():
                continue
            for f in folder.iterdir():
                if f.is_file():
                    zf.write(f, arcname=f"{person.folder_name()}/{f.name}")

    manifest = {
        "ok": True,
        "consultado_run": seed_cert.run_inscrito,
        "consultado_nombre": seed_cert.nombre_inscrito,
        "zip_path": str(zip_path),
        "arbol_png": str(arbol_png),
        "work_dir": str(work),
        "personas": tree_to_dict(nodes),
        "solicitudes_rc": solicitudes,
    }
    (work / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"ZIP listo: {zip_path}")
    return manifest


def _guess_tipo_from_filename(name: str) -> str:
    u = name.upper()
    if "NAC" in u:
        return "nacimiento"
    if "DEF" in u or "MUERTE" in u:
        return "defuncion"
    if "MAT" in u:
        return "matrimonio"
    if "UNION" in u or "AUC" in u:
        return "union_civil"
    return "certificado"
