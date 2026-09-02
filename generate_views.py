"""
generate_views.py
==================
Render isometric + orthographic views from a STEP solid, for visual
cross-checking against the geometry_extract.json output.

Usage:
    python3 generate_views.py <path_to_step_file> [--outdir views]
"""
import argparse
import os
import cadquery as cq
from cadquery import importers, exporters
import cairosvg
from PIL import Image, ImageChops


def autocrop(path, pad=15, bg=(255, 255, 255)):
    im = Image.open(path).convert("RGB")
    bgimg = Image.new("RGB", im.size, bg)
    diff = ImageChops.difference(im, bgimg)
    bbox = diff.getbbox()
    if bbox:
        l, t, r, b = bbox
        l, t = max(0, l - pad), max(0, t - pad)
        r, b = min(im.width, r + pad), min(im.height, b + pad)
        im = im.crop((l, t, r, b))
    im.save(path)


def render_views(step_path, outdir="views"):
    os.makedirs(outdir, exist_ok=True)
    shape = importers.importStep(step_path)

    views = {
        "iso": {"projectionDir": (1, -1, 1), "width": 900, "height": 700},
        "front": {"projectionDir": (0, -1, 0), "width": 900, "height": 400},
        "top": {"projectionDir": (0, 0, 1), "width": 900, "height": 400},
        "right": {"projectionDir": (1, 0, 0), "width": 500, "height": 500},
    }

    out_files = {}
    for name, opts in views.items():
        svg_path = os.path.join(outdir, f"view_{name}.svg")
        png_path = os.path.join(outdir, f"view_{name}.png")
        exporters.export(
            shape, svg_path, exportType="SVG",
            opt={"projectionDir": opts["projectionDir"], "width": opts["width"],
                 "height": opts["height"], "showAxes": False, "strokeWidth": 0.3},
        )
        cairosvg.svg2png(url=svg_path, write_to=png_path, scale=2, background_color="white")
        autocrop(png_path)
        out_files[name] = png_path
        print("wrote", png_path)
    return out_files


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step_file")
    ap.add_argument("--outdir", default="views")
    args = ap.parse_args()
    render_views(args.step_file, args.outdir)
