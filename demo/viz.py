"""Optional topology rendering (networkx + matplotlib, lazy import).

Draws the zone with the fault and the RCA focus highlighted:
  - links coloured by utilisation, dashed red when down;
  - the diagnosed root-cause element outlined in red;
  - customer prefixes annotated at their edge router, red when affected.
Kept deliberately simple; the demo runs fine without matplotlib.
"""

from __future__ import annotations

from demo.heartbeat.incident import Incident
from demo.rca.agent import RootCauseReport
from demo.twin.sim_twin import ATTACHMENTS, CONGESTION_PCT, SimTwin

_POS = {
    "edge1": (0.0, 2.0), "core1": (1.0, 2.0), "core2": (2.2, 2.0), "edge2": (3.2, 2.0),
    "edge3": (0.0, 0.0), "core3": (1.0, 0.0), "core4": (2.2, 0.0), "edge4": (3.2, 0.0),
}


def render_topology(
    twin: SimTwin, incident: Incident, report: RootCauseReport, path: str
) -> None:
    import matplotlib

    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 6))
    cause_where = str(report.cause.get("where", ""))
    affected = {c["prefix"] for c in report.affected_clients}

    for link in twin.links.values():
        (x1, y1), (x2, y2) = _POS[link.a], _POS[link.b]
        util = link.utilisation_pct
        if not link.oper_up:
            colour, style, width = "#c0392b", "--", 2.0
            label = "DOWN"
        else:
            if link.saturated or util >= CONGESTION_PCT:
                colour = "#e67e22"
            elif util >= 60:
                colour = "#f1c40f"
            else:
                colour = "#7f8c8d"
            style, width = "-", 1.0 + util / 25.0
            label = f"{util:.0f}%"
        ax.plot([x1, x2], [y1, y2], style, color=colour, linewidth=width, zorder=1)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        boxed = link.id == cause_where
        ax.annotate(
            f"{link.id}\n{label}",
            (mx, my),
            fontsize=7,
            ha="center",
            color="#c0392b" if boxed else "#2c3e50",
            bbox=(
                dict(boxstyle="round", fc="#fdecea", ec="#c0392b")
                if boxed
                else dict(boxstyle="round", fc="white", ec="#bdc3c7", alpha=0.8)
            ),
            zorder=3,
        )

    for node, (x, y) in _POS.items():
        is_cause_node = node == cause_where
        ax.scatter(
            [x],
            [y],
            s=900,
            c="#2980b9" if node.startswith("core") else "#27ae60",
            edgecolors="#c0392b" if is_cause_node else "white",
            linewidths=3 if is_cause_node else 1.5,
            zorder=4,
        )
        ax.annotate(
            node, (x, y), ha="center", va="center", color="white", fontsize=8, zorder=5
        )

    for prefix, edge in ATTACHMENTS.items():
        x, y = _POS[edge]
        dy = 0.42 if y > 1 else -0.42
        hit = prefix in affected
        ax.annotate(
            prefix + (" !" if hit else ""),
            (x, y + dy),
            ha="center",
            fontsize=8,
            color="#c0392b" if hit else "#2c3e50",
            fontweight="bold" if hit else "normal",
            zorder=5,
        )

    ax.set_title(
        f"{incident.id} — cause racine: {report.cause.get('detail', '?')} "
        f"(confiance {report.confidence})",
        fontsize=10,
    )
    ax.set_xlim(-0.6, 3.8)
    ax.set_ylim(-0.9, 2.9)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
