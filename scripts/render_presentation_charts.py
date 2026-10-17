"""Render presentation charts from the published G2 medians.

From the repository root:
    uv run --no-project --with matplotlib python scripts/render_presentation_charts.py

This optional documentation tool does not change Project KB dependencies.
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
POLICIES = ("PYCHARM", "PKB_MCP", "HYBRID_MCP")
LABELS = ("PyCharm", "Project KB MCP", "Оба инструмента")
COLORS = ("#64748b", "#2563eb", "#0f766e")
TASKS = ("AUDIT_1V2", "AUDIT_4V2")


def render(metric: str, filename: str, title: str, unit: str) -> None:
    with (ASSETS / "g2-metrics.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    fig, axes = plt.subplots(1, 2, figsize=(12.8, 5.4))
    fig.patch.set_facecolor("#ffffff")
    for ax, task in zip(axes, TASKS, strict=True):
        selected = {row["policy"]: row for row in rows if row["task"] == task}
        values = [float(selected[policy][metric]) for policy in POLICIES]
        ax.bar(range(3), values, color=COLORS, width=0.62, zorder=3)
        ax.set_xticks(range(3), LABELS, fontsize=10)
        ax.set_title(task, loc="left", fontsize=13, fontweight="bold", pad=16)
        ax.set_ylabel(unit, fontsize=10, color="#334155")
        ax.set_ylim(0, max(values) * 1.28)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color("#cbd5e1")
        ax.tick_params(axis="both", length=0, labelcolor="#334155")
        if metric == "median_input_tokens":
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda value, _: f"{int(value / 1000):,}".replace(",", " ") + " тыс.")
            )
        for index, value in enumerate(values):
            label = (
                f"{int(value):,}".replace(",", " ")
                if metric == "median_input_tokens"
                else f"{value:.1f}".replace(".", ",")
            )
            ax.text(
                index,
                value + max(values) * 0.035,
                label,
                ha="center",
                va="bottom",
                fontsize=11,
                fontweight="bold",
                color="#0f172a",
            )
    fig.suptitle(title, fontsize=17, fontweight="bold", x=0.07, ha="left", color="#0f172a")
    fig.text(
        0.07,
        0.045,
        "G2 · медианы 3 повторов на режим и задачу · 18 запусков всего\n"
        "Сравниваются режимы доступных инструментов; использование MCP было необязательным.",
        fontsize=9,
        color="#475569",
        linespacing=1.5,
    )
    fig.subplots_adjust(left=0.075, right=0.98, bottom=0.22, top=0.78, wspace=0.30)
    fig.savefig(ASSETS / filename, dpi=180, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    ASSETS.mkdir(parents=True, exist_ok=True)
    render("median_seconds", "g2-time.png", "Время выполнения двух задач", "Медиана, секунды")
    render(
        "median_input_tokens",
        "g2-input-tokens.png",
        "Входные токены двух задач",
        "Медиана, входные токены",
    )
