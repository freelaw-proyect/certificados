"""
Imagen PNG del árbol familiar (nombres y RUT).
"""

from __future__ import annotations

from pathlib import Path

from rc_birth_cert import run_digits
from rc_genealogy import PersonNode


def render_family_tree_png(
    nodes: dict[str, PersonNode],
    *,
    output_path: Path,
    consultado_run: str,
) -> Path:
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch
    except ImportError as e:
        raise RuntimeError("Instala matplotlib: pip install matplotlib") from e

    ck = run_digits(consultado_run)
    consultado = nodes.get(ck)
    if not consultado:
        consultado = next((p for p in nodes.values() if p.generation == 0), None)
    if not consultado:
        raise ValueError("No hay nodo consultado en el árbol")

    padre = nodes.get(run_digits(consultado.padre_run)) if consultado.padre_run else None
    madre = nodes.get(run_digits(consultado.madre_run)) if consultado.madre_run else None

    abuelos: list[PersonNode] = []
    for p in nodes.values():
        if p.generation == 2:
            abuelos.append(p)

    fig, ax = plt.subplots(figsize=(14, 9))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title("Árbol familiar — certificados Registro Civil", fontsize=14, pad=12)

    def box(x: float, y: float, person: PersonNode, color: str) -> None:
        label = f"{person.nombre or '—'}\nRUT {person.run}\n({person.relacion})"
        w, h = 3.2, 1.1
        rect = FancyBboxPatch(
            (x - w / 2, y - h / 2),
            w,
            h,
            boxstyle="round,pad=0.05",
            linewidth=1.2,
            edgecolor="#333",
            facecolor=color,
        )
        ax.add_patch(rect)
        ax.text(x, y, label, ha="center", va="center", fontsize=8, wrap=True)

    def line(x1: float, y1: float, x2: float, y2: float) -> None:
        ax.plot([x1, x2], [y1, y2], color="#555", linewidth=1.2, zorder=0)

    # Consultado
    box(7, 2.5, consultado, "#d4e8ff")
    if padre:
        box(4, 5.5, padre, "#ffe8cc")
        line(4, 4.9, 6.2, 3.1)
    if madre:
        box(10, 5.5, madre, "#ffe8cc")
        line(10, 4.9, 7.8, 3.1)

    ab_p = [p for p in abuelos if "paterno" in p.relacion or "paterna" in p.relacion]
    ab_m = [p for p in abuelos if "materno" in p.relacion or "materna" in p.relacion]
    xs_p = [2.5, 5.5] if len(ab_p) >= 2 else ([4] if ab_p else [])
    xs_m = [8.5, 11.5] if len(ab_m) >= 2 else ([10] if ab_m else [])
    for i, ab in enumerate(ab_p[:2]):
        x = xs_p[i] if i < len(xs_p) else 4
        box(x, 8.2, ab, "#e8ffe8")
        if padre:
            line(x, 7.6, 4, 6.1)
    for i, ab in enumerate(ab_m[:2]):
        x = xs_m[i] if i < len(xs_m) else 10
        box(x, 8.2, ab, "#e8ffe8")
        if madre:
            line(x, 7.6, 10, 6.1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=120, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return output_path
