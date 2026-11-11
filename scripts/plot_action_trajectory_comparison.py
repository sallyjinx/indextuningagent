#!/usr/bin/env python3
"""Plot formal Rule/Agent action trajectories from an experiment JSONL log."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


CONDITION_LABELS = {
    "zh": {"rule": "规则基准（Rule）", "autonomous": "自主调优（Agent）"},
    "en": {"rule": "Rule baseline", "autonomous": "Autonomous Agent"},
}
COLORS = {
    "rule": "#d97706",
    "autonomous": "#0f8f87",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True, help="formal events.jsonl")
    parser.add_argument("--output", type=Path, required=True, help="output path without extension")
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--no-title", action="store_true", help="omit the figure-level title")
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=("png", "pdf", "svg"),
        default=("png", "pdf"),
        help="output formats",
    )
    return parser.parse_args()


def load_slots(path: Path) -> list[dict]:
    slots: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("event") == "slot":
                slots.append(event)
    if not slots:
        raise ValueError(f"no slot events found in {path}")
    return slots


def slot_number(row: dict) -> int:
    return (int(row["phase"]) - 1) * 4 + int(row["slot"])


def action_label(row: dict) -> tuple[str, str] | None:
    action = row["execution"]["action"]["action"]
    index = row["execution"]["action"].get("index")
    if action == "CREATE":
        return f"+{index}", "#16803a"
    if action == "DROP":
        return f"−{index}", "#c2413a"
    return None


def main() -> None:
    args = parse_args()
    rows = load_slots(args.log)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    import os

    os.environ.setdefault("MPLCONFIGDIR", str(args.output.parent / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    font_family = ["DejaVu Sans"] if args.language == "en" else ["Droid Sans Fallback", "DejaVu Sans"]
    plt.rcParams.update(
        {
            "font.family": font_family,
            "axes.unicode_minus": False,
            "font.size": 11 if args.language == "en" else 10,
        }
    )

    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["repeat"]), row["condition"])].append(row)
    for values in grouped.values():
        values.sort(key=slot_number)

    no_tuning: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if row["condition"] == "no_tuning":
            no_tuning[slot_number(row)].append(float(row["after"]["mean_ms"]))
    no_tuning_mean = [sum(no_tuning[x]) / len(no_tuning[x]) for x in range(1, 13)]

    all_values = [
        float(row[side]["mean_ms"])
        for row in rows
        for side in ("before", "after")
    ]
    y_max = max(all_values) + 0.45

    compact_english = args.language == "en"
    figure, axes = plt.subplots(
        3,
        2,
        figsize=(15.2, 8.8 if compact_english else 11),
        sharex=True,
        sharey=True,
    )
    conditions = ("rule", "autonomous")
    xs = list(range(1, 13))
    tick_labels = [f"{phase}{slot}" for phase in "ABC" for slot in range(1, 5)]
    phase_spans = ((0.5, 4.5, "A：客户主导"), (4.5, 8.5, "B：商品主导"), (8.5, 12.5, "C：日期主导"))

    for repeat in range(1, 4):
        for column, condition in enumerate(conditions):
            axis = axes[repeat - 1, column]
            color = COLORS[condition]
            data = grouped[(repeat, condition)]
            before = [float(row["before"]["mean_ms"]) for row in data]
            after = [float(row["after"]["mean_ms"]) for row in data]

            for start, end, _ in phase_spans:
                shade = "#f4f7fb" if int(start) % 2 == 0 else "#fffaf0"
                axis.axvspan(start, end, color=shade, alpha=0.65, zorder=0)
            axis.axvline(4.5, color="#7a7a7a", linestyle="--", linewidth=0.9)
            axis.axvline(8.5, color="#7a7a7a", linestyle="--", linewidth=0.9)
            axis.plot(xs, no_tuning_mean, color="#8b95a1", linestyle="--", linewidth=1.2, alpha=0.85)
            axis.plot(xs, after, color=color, linewidth=2.0, marker="s", markersize=5.2, zorder=3)
            axis.scatter(
                xs,
                before,
                facecolors="white",
                edgecolors=color,
                linewidths=1.5,
                s=34,
                zorder=4,
            )
            for x, y_before, y_after, row in zip(xs, before, after, data):
                axis.plot([x, x], [y_before, y_after], color=color, linewidth=1.0, alpha=0.55, zorder=2)
                label = action_label(row)
                if label:
                    text, label_color = label
                    offset = 10 if max(y_before, y_after) < y_max - 0.55 else -16
                    axis.annotate(
                        text,
                        xy=(x, max(y_before, y_after)),
                        xytext=(0, offset),
                        textcoords="offset points",
                        ha="center",
                        va="bottom" if offset > 0 else "top",
                        color=label_color,
                        fontsize=9.5 if compact_english else 8.2,
                        fontweight="bold",
                        bbox={"boxstyle": "round,pad=0.16", "fc": "white", "ec": label_color, "alpha": 0.88, "lw": 0.7},
                        zorder=5,
                    )

            final_indexes = data[-1].get("actual_indexes", [])
            final_text = "+".join(final_indexes) if final_indexes else "无候选索引"
            axis.text(
                0.985,
                0.955,
                (f"Final indexes: {final_text}" if compact_english else f"C 阶段末：{final_text}"),
                transform=axis.transAxes,
                ha="right",
                va="top",
                fontsize=10 if compact_english else 8.5,
                color="#374151",
                bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#c9cdd3", "alpha": 0.9},
            )
            axis.set_xlim(0.5, 12.5)
            axis.set_ylim(0, y_max)
            axis.grid(axis="y", color="#cfd4db", linewidth=0.7, alpha=0.65)
            panel_title = (
                f"Repeat {repeat} · {CONDITION_LABELS['en'][condition]}"
                if compact_english
                else f"重复 {repeat} · {CONDITION_LABELS['zh'][condition]}"
            )
            axis.set_title(
                panel_title,
                loc="left",
                fontsize=14 if compact_english else 11,
                fontweight="bold",
                pad=6,
            )
            if column == 0:
                axis.set_ylabel("Mean query time (ms/query)" if compact_english else "平均查询耗时（ms/查询）")
            if repeat == 3:
                axis.set_xticks(xs, tick_labels)
                axis.set_xlabel("Action slot" if compact_english else "动作槽位")

    legend = [
        Line2D([0], [0], marker="o", color="#4b5563", markerfacecolor="white", linewidth=0, label="Before" if compact_english else "Before（动作前）"),
        Line2D([0], [0], marker="s", color="#4b5563", linewidth=1.8, label="After" if compact_english else "After（动作后）"),
        Line2D([0], [0], color="#8b95a1", linestyle="--", linewidth=1.4, label="No tuning: mean After" if compact_english else "无调优 After（三次平均）"),
        Line2D([0], [0], marker="$+$", color="#16803a", linewidth=0, markersize=10, label="CREATE index" if compact_english else "CREATE 索引"),
        Line2D([0], [0], marker="$-$", color="#c2413a", linewidth=0, markersize=10, label="DROP index" if compact_english else "DROP 索引"),
    ]
    figure.legend(
        handles=legend,
        loc="lower center",
        ncol=5,
        frameon=False,
        fontsize=12.5 if compact_english else 10,
        handlelength=2.2,
        columnspacing=1.8,
        bbox_to_anchor=(0.5, 0.003),
    )
    if not args.no_title:
        figure.suptitle(
            "Formal Action Trajectories: Rule Baseline vs Autonomous Agent"
            if compact_english
            else "正式实验动作轨迹对比：规则基准 vs 自主调优 Agent",
            fontsize=16,
            fontweight="bold",
            y=0.995,
        )
    top = 0.995 if args.no_title else 0.965
    if compact_english:
        figure.tight_layout(rect=(0.025, 0.055, 0.995, top), h_pad=0.55, w_pad=0.55)
    else:
        figure.text(
            0.5,
            0.055,
            "A/B/C 分别为客户、商品、日期主导阶段；空心圆与实心方块之间的竖线表示同一槽位动作前后的变化；+I? 为创建，−I? 为删除。",
            ha="center",
            fontsize=9.5,
            color="#4b5563",
        )
        figure.tight_layout(rect=(0.03, 0.085, 0.99, top), h_pad=2.0, w_pad=1.2)

    for extension in args.formats:
        output = args.output.with_suffix(f".{extension}")
        figure.savefig(output, dpi=220 if extension == "png" else None, bbox_inches="tight")
    plt.close(figure)


if __name__ == "__main__":
    main()
