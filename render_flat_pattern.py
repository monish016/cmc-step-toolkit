"""
render_flat_pattern.py
=======================
Draw the computed flat pattern (bend lines + hole/slot positions) from
geometry_extract.json produced by step_quote_extract.py.

Usage:
    python3 render_flat_pattern.py [geometry_extract.json] [--out flat_pattern.png]
"""
import argparse
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches


def render(json_path="geometry_extract.json", out_path="flat_pattern.png"):
    with open(json_path) as f:
        g = json.load(f)

    W = g["flat_width_in"]
    seg = g["layout_segments"]
    features = g["features"]
    lengths = [f["length_in"] for f in features if f.get("length_in") is not None]
    LEN = max(lengths) + 1.0 if lengths else 10.0
    # if envelope length is available and bigger, prefer it (covers featureless margins)
    env_len_in = g["envelope"]["bbox_mm"]["xlen"] / 25.4
    LEN = max(LEN, env_len_in)

    fig, ax = plt.subplots(figsize=(16, 4.2))

    # outline: simple bounding rectangle (true taper/edge shape is NOT
    # reconstructed generically -- see instructions)
    ax.plot([0, LEN, LEN, 0, 0], [0, 0, W, W, 0], color="black", lw=1.4)

    bend_colors = {}
    bend_n = 0
    for s in seg:
        if s["kind"] == "bend":
            bend_n += 1
            mid = (s["start_in"] + s["end_in"]) / 2
            ax.axhline(mid, color="tab:blue", lw=0.9, linestyle="--")
            ax.text(LEN + 0.15, mid, f"BEND {bend_n} \u2014 {s['angle_deg']:.0f}\u00b0",
                    va="center", fontsize=8, color="tab:blue")

    marker_map = {"square_or_rect": "s", "round": "o", "slot": None}
    color_map = {"square_or_rect": "tab:orange", "round": "tab:green", "slot": "tab:purple"}
    seen = set()
    for feat in features:
        kind = feat["type"]
        Lx, Ty = feat.get("length_in"), feat.get("transverse_in")
        if Lx is None or Ty is None:
            continue
        if kind == "slot":
            w = feat.get("width_in", 0.3)
            rect = patches.FancyBboxPatch((Lx-0.25, Ty-w/2), 0.5, w,
                                           boxstyle="round,pad=0,rounding_size=0.12",
                                           linewidth=1.1, edgecolor=color_map[kind], facecolor="none")
            ax.add_patch(rect)
            label = "slot" if "slot" not in seen else None
            if label:
                ax.scatter([], [], marker="s", facecolors="none", edgecolors=color_map[kind], label=label)
            seen.add("slot")
        else:
            label = kind if kind not in seen else None
            ax.scatter([Lx], [Ty], marker=marker_map.get(kind, "x"), s=90,
                       facecolors="none", edgecolors=color_map.get(kind, "grey"),
                       linewidths=1.4, label=label, zorder=5)
            seen.add(kind)

    ax.set_xlim(-0.5, LEN + 3.2)
    ax.set_ylim(-0.6, W + 0.6)
    ax.set_aspect("equal")
    ax.set_xlabel(f"Length (in) \u2014 bounding length {LEN:.2f} in")
    ax.set_ylabel("Developed width (in)")
    ax.set_title(
        f"Computed Flat Pattern \u2014 width={W:.2f} in | thickness={g['thickness_in']} in | "
        f"bend radius={g['bend_radius_in']} in | K={g['k_factor_assumed']}", fontsize=10)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.18), ncol=4, fontsize=8, frameon=False)
    ax.text(LEN*0.02, W+0.1,
            "NOTE: true part outline (taper/notches) not reconstructed automatically \u2014 rectangle shown is the bounding envelope only.",
            fontsize=7, color="grey", style="italic")

    plt.tight_layout()
    plt.savefig(out_path, dpi=220, facecolor="white")
    print("saved", out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path", nargs="?", default="geometry_extract.json")
    ap.add_argument("--out", default="flat_pattern.png")
    args = ap.parse_args()
    render(args.json_path, args.out)
