"""使用实际正式报表绘制质量、精度压缩和冷启动/复用曲线。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def plot(root: Path):
    root = root.resolve()
    os.environ.setdefault("MPLCONFIGDIR", str(root / "plot-cache"))
    sys.path.insert(0, str(ROOT / "data/evals/plot-libs"))
    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    from matplotlib import pyplot as plt
    from matplotlib.ticker import PercentFormatter

    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report["complete_coverage"]:
        raise ValueError("完整矩阵尚未完成，不能绘制最终曲线")
    figures = []
    colors = {"P0M0": "#546e7a", "P0M1": "#267a9c", "P1M0": "#d58b24", "P1M1": "#a83a3a"}
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})

    def save(fig, name):
        fig.tight_layout()
        for extension in ("svg", "png"):
            path = root / f"{name}.{extension}"
            fig.savefig(path, dpi=160, bbox_inches="tight")
            figures.append(path.name)
        plt.close(fig)

    def number(value):
        return math.nan if value is None else value

    fig, ax = plt.subplots(figsize=(8, 4.5))
    names = list(colors)
    values = [report["groups"][name]["accuracy"] for name in names]
    bars = ax.bar(names, values, color=[colors[name] for name in names], width=.58)
    for name, bar, value in zip(names, bars, values):
        group = report["groups"][name]
        ax.text(bar.get_x() + bar.get_width()/2, value + .012,
                f"{value:.1%}\n{group['correct']}/{group['answers']}", ha="center", va="bottom")
    ax.axhline(.9, color="#333333", linestyle="--", label="90% quality floor")
    ax.set_ylim(0, 1.12); ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set_ylabel("Operational + semantic answer accuracy")
    ax.set_title("Formal ablation: all 4,320 answers, failures included")
    ax.grid(axis="y", alpha=.2); ax.legend(loc="lower left")
    save(fig, "quality-ablation")

    pure = report["perception"]["perception"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.4))
    for index, memory in enumerate(("M0", "M1")):
        item = pure[memory]; value = item["quality_difference"]; ci = item["clustered_95_ci"]
        error = [[value-ci[0]], [ci[1]-value]] if ci else None
        axes[0].errorbar(index, value, yerr=error, fmt="o", capsize=5, color=colors[f"P1{memory}"], markersize=7)
    axes[0].axhline(-.02, color="#333333", linestyle="--", label="Noninferiority margin -2 pp")
    axes[0].set_xticks([0, 1], ["Memory off", "Memory on"])
    axes[0].yaxis.set_major_formatter(PercentFormatter(1)); axes[0].set_ylabel("Perception on - off accuracy")
    axes[0].set_title("60 perception scenes: clustered 95% CI"); axes[0].legend(fontsize=8)
    x = np.arange(2)
    main = [number(pure[m]["high_noise"]["main_input_saving"]) for m in ("M0", "M1")]
    product = [number(pure[m]["all"]["product_total_token_saving"]) for m in ("M0", "M1")]
    axes[1].bar(x-.16, main, width=.3, label="Main input: high-noise scenes", color="#267a9c")
    axes[1].bar(x+.16, product, width=.3, label="Product input + output: all scenes", color="#d58b24")
    axes[1].axhline(0, color="#333333", linewidth=.7)
    axes[1].axhline(.3, color="#777777", linestyle="--", label="Main-input target 30%")
    axes[1].set_xticks(x, ["Memory off", "Memory on"])
    for index, values in enumerate(zip(main, product)):
        if all(math.isnan(value) for value in values):
            axes[1].text(index, .08, "Unknown usage", ha="center", color="#666666")
    axes[1].yaxis.set_major_formatter(PercentFormatter(1)); axes[1].set_ylabel("Token reduction against perception off")
    axes[1].set_title("Savings require acceptable answer quality"); axes[1].legend(fontsize=8, loc="best")
    for ax in axes: ax.grid(axis="y", alpha=.2)
    save(fig, "perception-quality-cost")

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5), sharex=True)
    for ax, memory in zip(axes, ("M0", "M1")):
        layer = pure[memory]["by_length"]; lengths = [40, 80, 120]
        for state in ("off", "on"):
            condition = f"P{int(state=='on')}{memory}"
            main_values = [number(layer[str(n)][f"main_{state}"]) / layer[str(n)]["pairs"] for n in lengths]
            product_values = [number(layer[str(n)][f"product_total_tokens_{state}"]) / layer[str(n)]["pairs"] for n in lengths]
            ax.plot(lengths, main_values, marker="o", color=colors[condition], label=f"{condition} main input")
            ax.plot(lengths, product_values, marker="s", linestyle="--", color=colors[condition], label=f"{condition} product I+O")
        ax.set_title(f"Cold start / {memory}"); ax.set_xlabel("Dialogue turns"); ax.set_xticks(lengths)
        ax.grid(alpha=.2); ax.legend(fontsize=8)
    axes[0].set_ylabel("Mean tokens per answer, auxiliary work included")
    save(fig, "cold-cost-by-length")

    fig, axes = plt.subplots(2, 1, figsize=(8, 7), sharex=True)
    incomplete = report.get("cost_integrity", {}).get("reuse_cost_uncertain_conditions", [])
    for condition in ("P0M1", "P1M1"):
        curves = [c for c in report["reuse_cost_curve"] if c["condition"] == condition]
        x = [c["through_question"] for c in curves]
        axes[0].plot(x, [c["accuracy"] for c in curves], marker="o", color=colors[condition], label=condition)
        axes[1].plot(x, [number(c["main_input_tokens"])/1e6 for c in curves], marker="o", color=colors[condition], label=f"{condition} actual main input")
        known = [number(c.get("known_product_input_tokens", c["product_input_tokens"]))/1e6 for c in curves]
        label = f"{condition} recorded product input" + (" (incomplete)" if condition in incomplete else "")
        axes[1].plot(x, known, marker="s", linestyle="--", color=colors[condition], label=label)
    axes[0].axhline(.9, color="#777777", linestyle="--", label="90% quality floor")
    axes[0].set_ylim(0, 1.04); axes[0].yaxis.set_major_formatter(PercentFormatter(1))
    axes[0].set_ylabel("Cumulative accuracy"); axes[0].set_title("Reuse: 24 scenes x 3 repeats, six consecutive questions")
    axes[1].set_ylabel("Cumulative input tokens (millions)"); axes[1].set_xlabel("Through question")
    axes[1].set_xticks([1,3,6])
    if incomplete: axes[1].set_title("Dashed product costs exclude unknown/unattributed calls", fontsize=10)
    for ax in axes: ax.grid(alpha=.2); ax.legend(fontsize=8)
    save(fig, "reuse-cost-curve")

    provenance = dict(code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      report_hash=hashlib.sha256(report_path.read_bytes()).hexdigest(),
                      matplotlib=matplotlib.__version__, numpy=np.__version__, figures=figures,
                      method="只绘制实际报表字段；缺失 usage 不补零，记录量与完整量分开标注。")
    (root / "plots-manifest.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(provenance, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    plot(parser.parse_args().root)
