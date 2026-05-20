"""
Árbol genealógico: consultado → padres → abuelos (desde certificados de nacimiento).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rc_birth_cert import BirthCertificateData, parse_birth_certificate_pdf, run_digits


@dataclass
class PersonNode:
    run: str
    nombre: str
    relacion: str  # etiqueta respecto al consultado
    generation: int  # 0=consultado, 1=padres, 2=abuelos
    padre_run: str = ""
    madre_run: str = ""
    certificados: list[Path] = field(default_factory=list)
    solicitud_rc_ok: bool | None = None
    solicitud_rc_detalle: str = ""
    birth_cert_path: Path | None = None

    def folder_name(self) -> str:
        base = _safe_folder_part(self.nombre or self.run)
        rel = _safe_folder_part(self.relacion)
        return f"{base} ({rel})"


def _safe_folder_part(s: str) -> str:
    t = s.strip()
    for ch in '\\/:*?"<>|':
        t = t.replace(ch, "_")
    return t[:120] if t else "sin_nombre"


REL_PADRE = "padre"
REL_MADRE = "madre"
REL_ABUELO_PATERNO = "abuelo paterno"
REL_ABUELA_PATERNA = "abuela paterna"
REL_ABUELO_MATERNO = "abuelo materno"
REL_ABUELA_MATERNA = "abuela materna"
REL_CONSULTADO = "consultado"


def build_family_tree(
    seed_cert: BirthCertificateData,
    *,
    extra_birth_certs: dict[str, BirthCertificateData] | None = None,
    max_generation: int = 2,
) -> dict[str, PersonNode]:
    """
    Construye nodos por RUN. ``extra_birth_certs``: RUN → datos parseados de otros PDF.
    """
    extra = extra_birth_certs or {}
    nodes: dict[str, PersonNode] = {}

    def _key(run: str) -> str:
        return run_digits(run)

    def _ensure(run: str, nombre: str, relacion: str, generation: int) -> PersonNode | None:
        if not run:
            return None
        k = _key(run)
        if k in nodes:
            if nombre and not nodes[k].nombre:
                nodes[k].nombre = nombre
            return nodes[k]
        nodes[k] = PersonNode(
            run=run,
            nombre=nombre,
            relacion=relacion,
            generation=generation,
        )
        return nodes[k]

    seed_run = seed_cert.run_inscrito
    _ensure(seed_run, seed_cert.nombre_inscrito, REL_CONSULTADO, 0)
    nodes[_key(seed_run)].birth_cert_path = Path(seed_cert.source_path) if seed_cert.source_path else None

    padre = _ensure(seed_cert.run_padre, seed_cert.nombre_padre, REL_PADRE, 1)
    madre = _ensure(seed_cert.run_madre, seed_cert.nombre_madre, REL_MADRE, 1)
    if padre and madre:
        nodes[_key(seed_run)].padre_run = seed_cert.run_padre
        nodes[_key(seed_run)].madre_run = seed_cert.run_madre

    if max_generation < 2:
        return nodes

    if padre and seed_cert.run_padre:
        bc = extra.get(_key(seed_cert.run_padre))
        if bc:
            nodes[_key(seed_cert.run_padre)].birth_cert_path = (
                Path(bc.source_path) if bc.source_path else None
            )
            _ensure(bc.run_padre, bc.nombre_padre, REL_ABUELO_PATERNO, 2)
            _ensure(bc.run_madre, bc.nombre_madre, REL_ABUELA_PATERNA, 2)
            if padre:
                padre.padre_run = bc.run_padre
                padre.madre_run = bc.run_madre

    if madre and seed_cert.run_madre:
        bc = extra.get(_key(seed_cert.run_madre))
        if bc:
            nodes[_key(seed_cert.run_madre)].birth_cert_path = (
                Path(bc.source_path) if bc.source_path else None
            )
            _ensure(bc.run_padre, bc.nombre_padre, REL_ABUELO_MATERNO, 2)
            _ensure(bc.run_madre, bc.nombre_madre, REL_ABUELA_MATERNA, 2)
            if madre:
                madre.padre_run = bc.run_padre
                madre.madre_run = bc.run_madre

    return nodes


def persons_in_fetch_order(nodes: dict[str, PersonNode]) -> list[PersonNode]:
    """Consultado, padres, abuelos (sin duplicar)."""
    order = sorted(nodes.values(), key=lambda p: (p.generation, p.relacion))
    seen: set[str] = set()
    out: list[PersonNode] = []
    for p in order:
        k = run_digits(p.run)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def nodes_missing_parent_birth_cert(nodes: dict[str, PersonNode]) -> list[PersonNode]:
    """Padres sin cert. de nacimiento local (para pedir al RC antes de abuelos)."""
    missing: list[PersonNode] = []
    for p in nodes.values():
        if p.generation != 1:
            continue
        if not p.birth_cert_path or not p.birth_cert_path.is_file():
            missing.append(p)
    return missing


def tree_to_dict(nodes: dict[str, PersonNode]) -> list[dict[str, Any]]:
    return [
        {
            "run": p.run,
            "nombre": p.nombre,
            "relacion": p.relacion,
            "generation": p.generation,
            "padre_run": p.padre_run,
            "madre_run": p.madre_run,
            "certificados": [str(x) for x in p.certificados],
            "solicitud_rc_ok": p.solicitud_rc_ok,
        }
        for p in sorted(nodes.values(), key=lambda x: (x.generation, x.relacion))
    ]


def load_birth_certs_from_dir(directory: Path) -> dict[str, BirthCertificateData]:
    out: dict[str, BirthCertificateData] = {}
    if not directory.is_dir():
        return out
    for pdf in directory.rglob("*.pdf"):
        try:
            bc = parse_birth_certificate_pdf(pdf)
        except Exception:
            continue
        if bc.run_inscrito:
            out[run_digits(bc.run_inscrito)] = bc
    return out
