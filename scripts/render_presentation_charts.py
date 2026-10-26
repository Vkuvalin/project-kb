"""Render presentation charts from saved historical pairs and G2 medians.

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
LABELS = ("PyCharm", "Project KB MCP", "Совместно")
COLORS = ("#64748b", "#2563eb", "#0f766e")
TASKS = ("AUDIT_1V2", "AUDIT_4V2")
TASK_LABELS = ("Карточка класса\nAUDIT_1V2", "Поиск по репозиторию\nAUDIT_4V2")


def render(metric: str, filename: str, title: str, unit: str) -> None:
    with (ASSETS / "g2-metrics.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 5.8))
    fig.patch.set_facecolor("#ffffff")
    for ax, task, task_label in zip(axes, TASKS, TASK_LABELS, strict=True):
        selected = {row["policy"]: row for row in rows if row["task"] == task}
        values = [float(selected[policy][metric]) for policy in POLICIES]
        ax.bar(range(3), values, color=COLORS, width=0.62, zorder=3)
        ax.set_xticks(range(3), LABELS, fontsize=12)
        ax.set_title(task_label, loc="left", fontsize=15, fontweight="bold", pad=16)
        ax.set_ylabel(unit, fontsize=12, color="#334155")
        ax.set_ylim(0, max(values) * 1.28)
        ax.grid(axis="y", color="#e2e8f0", linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color("#cbd5e1")
        ax.tick_params(axis="both", length=0, labelcolor="#334155", labelsize=12)
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
                fontsize=14,
                fontweight="bold",
                color="#0f172a",
            )
    fig.suptitle(title, fontsize=18, fontweight="bold", x=0.07, ha="left", color="#0f172a")
    fig.text(
        0.07,
        0.045,
        "G2 · медианы 3 повторов на режим и задачу · 18 запусков всего",
        fontsize=10,
        color="#475569",
        linespacing=1.5,
    )
    fig.subplots_adjust(left=0.085, right=0.98, bottom=0.24, top=0.72, wspace=0.36)
    fig.savefig(ASSETS / filename, dpi=180, facecolor="white", bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)


def render_early() -> None:
    """Plot four historical pairs without pooling them with later generations."""
    with (ASSETS / "early-structural-metrics.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    tasks = ("A1", "A1R", "A2", "A4")
    labels = (
        "Один символ · A1",
        "Повторение · A1R",
        "Три символа · A2",
        "Поиск по репозиторию · A4",
    )
    values = {(row["task_id"], row["condition"]): float(row["elapsed_seconds"]) for row in rows}
    maximum = max(values.values())
    fig, ax = plt.subplots(figsize=(11.8, 5.7))
    for offset, condition, label, color in (
        (-0.18, "NATIVE", "Нативные инструменты", "#64748b"),
        (0.18, "PKB", "Project KB", "#2563eb"),
    ):
        measured = [values[(task, condition)] for task in tasks]
        positions = [index + offset for index in range(len(tasks))]
        ax.barh(positions, measured, height=0.3, color=color, label=label, zorder=3)
        for position, value in zip(positions, measured, strict=True):
            ax.text(
                value + maximum * 0.02,
                position,
                f"{value:.0f}",
                va="center",
                fontsize=12,
                color="#0f172a",
            )
    for index, task in enumerate(tasks):
        baseline, candidate = values[(task, "NATIVE")], values[(task, "PKB")]
        reduction = (baseline - candidate) / baseline * 100
        ax.text(
            maximum * 1.26,
            index,
            f"−{reduction:.1f}%".replace(".", ","),
            va="center",
            fontsize=13,
            fontweight="bold",
            color="#0f766e",
        )
    ax.set_yticks(range(len(tasks)), labels, fontsize=13)
    ax.invert_yaxis()
    ax.set_xlim(0, maximum * 1.5)
    ax.set_xlabel("Полное время работы агента, секунды", fontsize=12)
    ax.grid(axis="x", color="#e2e8f0", zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#cbd5e1")
    ax.tick_params(axis="both", length=0, labelsize=12, labelcolor="#334155")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.06), ncols=2, frameon=False, fontsize=12)
    fig.suptitle(
        "Ранние структурные аудиты",
        x=0.06,
        y=0.99,
        ha="left",
        fontsize=19,
        fontweight="bold",
        color="#0f172a",
    )
    fig.text(
        0.06,
        0.04,
        "Суммарно: 934 → 626 с · −33,0% времени\n"
        "Оценки 10/10 у обеих сторон по сохранённым историческим отчётам.",
        fontsize=11,
        color="#475569",
        linespacing=1.5,
    )
    fig.subplots_adjust(left=0.32, right=0.98, bottom=0.24, top=0.79)
    fig.savefig(
        ASSETS / "early-structural-time.png",
        dpi=180,
        facecolor="white",
        bbox_inches="tight",
        pad_inches=0.2,
    )
    plt.close(fig)


if __name__ == "__main__":
    ASSETS.mkdir(parents=True, exist_ok=True)
    render_early()
    render("median_seconds", "g2-time.png", "Время выполнения двух задач", "Медиана, секунды")
    render(
        "median_input_tokens",
        "g2-input-tokens.png",
        "Входные токены двух задач",
        "Медиана, входные токены",
    )
