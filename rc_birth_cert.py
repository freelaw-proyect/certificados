"""
Extracción de datos desde certificados de nacimiento del Registro Civil (PDF).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BirthCertificateData:
    source_path: str = ""
    folio: str = ""
    nombre_inscrito: str = ""
    run_inscrito: str = ""
    nombre_padre: str = ""
    run_padre: str = ""
    nombre_madre: str = ""
    run_madre: str = ""
    raw_text: str = ""


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("Instala pypdf: pip install pypdf") from e
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _clean_run(s: str) -> str:
    t = (s or "").strip().upper()
    m = re.search(r"(\d{1,2}(?:\.\d{3}){1,2}-[0-9K])", t)
    return m.group(1) if m else t


def parse_birth_certificate_text(text: str, *, source_path: str = "") -> BirthCertificateData:
    t = text.replace("\r", "\n")
    data = BirthCertificateData(source_path=source_path, raw_text=t)

    m = re.search(r"FOLIO\s*:\s*(\d+)", t, re.I)
    if m:
        data.folio = m.group(1).strip()

    m = re.search(r"Nombre inscrito\s*:\s*(.+?)(?:\n|R\.U\.N)", t, re.I | re.S)
    if m:
        data.nombre_inscrito = _clean_name(m.group(1))

    m = re.search(r"R\.U\.N\.?\s*:\s*([\d\.]+-[0-9K])", t, re.I)
    if m:
        data.run_inscrito = _clean_run(m.group(1))

    m = re.search(r"Nombre del Padre\s*:\s*(.+?)(?:\n|R\.U\.N)", t, re.I | re.S)
    if m:
        data.nombre_padre = _clean_name(m.group(1))

    m = re.search(r"R\.U\.N\. del Padre\s*:\s*([\d\.]+-[0-9K])", t, re.I)
    if m:
        data.run_padre = _clean_run(m.group(1))

    m = re.search(r"Nombre de la Madre\s*:\s*(.+?)(?:\n|R\.U\.N)", t, re.I | re.S)
    if m:
        data.nombre_madre = _clean_name(m.group(1))

    m = re.search(r"R\.U\.N\. de la Madre\s*:\s*([\d\.]+-[0-9K])", t, re.I)
    if m:
        data.run_madre = _clean_run(m.group(1))

    return data


def parse_birth_certificate_pdf(path: Path) -> BirthCertificateData:
    text = _extract_pdf_text(path)
    return parse_birth_certificate_text(text, source_path=str(path))


def run_digits(run: str) -> str:
    return re.sub(r"[^0-9K]", "", (run or "").upper())


def run_body_for_filename(run: str) -> str:
    """Cuerpo numérico sin DV (el RC suele usarlo al final del nombre del PDF)."""
    d = run_digits(run)
    if len(d) >= 2:
        return d[:-1]
    return d


def pdf_matches_run(filename: str, run: str) -> bool:
    """Ej. NAC_AF_500697505248_17402744.pdf → RUN 17.402.744-7."""
    body = run_body_for_filename(run)
    full = run_digits(run)
    if not body and not full:
        return False
    stem = Path(filename).stem.upper()
    if full and (stem.endswith(full) or f"_{full}" in stem):
        return True
    return bool(body and (stem.endswith(body) or f"_{body}" in stem))
