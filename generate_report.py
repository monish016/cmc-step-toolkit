"""
generate_report.py
====================
Build a quoting-reference PDF from geometry_extract.json + the rendered
views/flat_pattern images. Fully data-driven -- no part-specific values.

Handles both solid-body and surface-only models gracefully.

Usage:
    python3 generate_report.py geometry_extract.json --views views/ \
        --flatpattern flat_pattern.png --out report.pdf
"""
import argparse
import json
import os
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from PIL import Image


def make_report(json_path, views_dir, flat_pattern_path, out_path):
    with open(json_path) as f:
        g = json.load(f)

    PAGE_W, PAGE_H = 17*inch, 11*inch
    c = canvas.Canvas(out_path, pagesize=(PAGE_W, PAGE_H))
    margin = 0.4*inch
    is_surface = g.get("is_surface_model", False)

    def frame():
        c.setLineWidth(1.2)
        c.rect(margin, margin, PAGE_W-2*margin, PAGE_H-2*margin)

    def header(title, subtitle):
        c.setFont("Helvetica-Bold", 15)
        c.drawString(margin+0.2*inch, PAGE_H-margin-0.32*inch, title)
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(colors.HexColor("#1a5f1a"))
        c.drawString(margin+0.2*inch, PAGE_H-margin-0.53*inch, subtitle)
        c.setFillColor(colors.black)
        c.setLineWidth(0.75)
        c.line(margin+0.1*inch, PAGE_H-margin-0.63*inch, PAGE_W-margin-0.1*inch, PAGE_H-margin-0.63*inch)

    def box(x, y, w, h, label=None):
        c.setLineWidth(0.6)
        c.rect(x, y, w, h)
        if label:
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x+4, y+h-13, label)

    def place_image(path, x, y, w, h, pad_top=16, pad=6):
        if not os.path.isfile(path):
            # Draw placeholder text if image missing
            c.setFont("Helvetica-Oblique", 10)
            c.setFillColor(colors.HexColor("#999999"))
            c.drawCentredString(x + w/2, y + h/2, "(image not available)")
            c.setFillColor(colors.black)
            return
        im = Image.open(path)
        iw, ih = im.size
        avail_w, avail_h = w-2*pad, h-pad_top-pad
        scale = min(avail_w/iw, avail_h/ih)
        dw, dh = iw*scale, ih*scale
        c.drawImage(path, x+(w-dw)/2, y+pad, width=dw, height=dh, preserveAspectRatio=True, mask='auto')

    def table(x, y, w, rows, col_fracs, row_h=16, font=8):
        n = len(rows)
        if n == 0:
            return y
        total_h = row_h*n
        xs = [x]
        cx = x
        for fr in col_fracs:
            cx += w*fr
            xs.append(cx)
        c.setFillColor(colors.HexColor("#e8e8e8"))
        c.rect(x, y, w, row_h, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.setLineWidth(0.5)
        c.rect(x, y-total_h+row_h, w, total_h)
        for xi in xs[1:-1]:
            c.line(xi, y-total_h+row_h, xi, y+row_h)
        ry = y
        for ridx, row in enumerate(rows):
            if ridx > 0:
                c.line(x, ry, x+w, ry)
            for ci, val in enumerate(row):
                c.setFont("Helvetica-Bold" if ridx == 0 else "Helvetica", font)
                c.drawString(xs[ci]+5, ry-row_h+5, str(val))
            ry -= row_h
        return y-total_h

    part_name = g.get("source_file", "part")

    # ---------------- PAGE 1: envelope + bend table ----------------
    frame()
    model_type = "Surface Model" if is_surface else "STEP file only"
    header(f"{part_name} - Geometry-Derived Quoting Data ({model_type})",
           "Surface analysis: bounding box, face classification, feature detection"
           if is_surface else
           "Computed directly from the solid: face classification, bend detection, cross-section unfold, feature clustering")

    y = PAGE_H - margin - 1.0*inch

    # Surface model notice
    if is_surface:
        note = g.get("surface_analysis_note", "")
        if note:
            c.setFont("Helvetica-Oblique", 9)
            c.setFillColor(colors.HexColor("#cc6600"))
            c.drawString(margin, y, f"Note: {note}")
            c.setFillColor(colors.black)
            y -= 0.25*inch

    env = g.get("envelope", {})
    bbox = env.get("bbox_mm", {})

    env_rows = [["Property", "Value"]]
    if bbox:
        env_rows.append(["Overall length (X)", f"{bbox.get('xlen',0)/25.4:.2f} in"])
        env_rows.append(["Overall width (Y)", f"{bbox.get('ylen',0)/25.4:.2f} in"])
        env_rows.append(["Overall height (Z)", f"{bbox.get('zlen',0)/25.4:.2f} in"])

    thickness = g.get("thickness_in")
    if thickness is not None and thickness > 0:
        gauge = g.get("gauge")
        t_str = f"{thickness:.4f} in"
        if gauge:
            t_str += f" ({gauge})"
        env_rows.append(["Sheet thickness (derived)", t_str])

    bend_r = g.get("bend_radius_in")
    if bend_r is not None and bend_r > 0:
        env_rows.append(["Bend radius (derived)", f"{bend_r:.3f} in"])
    else:
        env_rows.append(["Bend radius (derived)", "n/a"])

    num_bends = g.get("num_bends", 0)
    env_rows.append(["Number of bends detected", f"{num_bends}"])

    bend_angles = g.get("bend_angles_deg", [])
    if bend_angles:
        env_rows.append(["Bend angles (deg)", ", ".join(str(a) for a in bend_angles)])

    flat_w = g.get("flat_width_in")
    k_factor = g.get("k_factor_assumed")
    if flat_w is not None and flat_w > 0:
        k_str = f" (K={k_factor})" if k_factor else ""
        env_rows.append(["Computed flat/developed width", f"{flat_w:.2f} in{k_str}"])

    flat_l = g.get("flat_length_in")
    if flat_l is not None and flat_l > 0:
        env_rows.append(["Computed flat/developed length", f"{flat_l:.2f} in"])

    vol = env.get("volume_cm3")
    if vol is not None and vol > 0:
        env_rows.append(["Solid volume", f"{vol:.1f} cm3"])
    elif is_surface:
        env_rows.append(["Solid volume", "n/a (surface model)"])

    mass_lb = env.get("mass_lb", 0)
    mass_kg = env.get("mass_kg", 0)
    if mass_lb > 0:
        env_rows.append(["Est. weight", f"{mass_lb:.2f} lb ({mass_kg:.2f} kg)"])

    # Face counts for surface models
    face_counts = g.get("face_counts", {})
    if is_surface and face_counts:
        env_rows.append(["Total faces analyzed", f"{face_counts.get('total', 0)} (planar: {face_counts.get('planar', 0)}, cylindrical: {face_counts.get('cylindrical', 0)}, other: {face_counts.get('other', 0)})"])

    y_after = table(margin, y, (PAGE_W-2*margin), env_rows, [0.45, 0.55])
    y = y_after - 0.3*inch

    # Feature summary
    features = g.get("features", [])
    if features:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, "FEATURE SUMMARY (auto-classified)")
        y -= 0.26*inch
        from collections import Counter
        # Handle features that may use "count" field (surface models)
        feat_rows = [["Feature type", "Count found"]]
        if features and "count" in features[0]:
            # Surface model style: each feature has type + count
            for feat in features:
                feat_rows.append([feat.get("type", "unknown"), feat.get("count", 1)])
        else:
            # Solid model style: each feature is individual
            type_counts = Counter(f.get("type", "unknown") for f in features)
            for t, n in type_counts.items():
                feat_rows.append([t, n])
        feat_rows.append(["Unclassified (needs manual review)", g.get("features_unclassified_count", 0)])
        table(margin, y, (PAGE_W-2*margin)*0.5, feat_rows, [0.6, 0.4])

    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(colors.grey)
    c.drawString(margin, margin+0.15*inch,
      "Auto-generated by step_quote_extract.py -- see INSTRUCTIONS.md for what to sanity-check before using for CAM.")
    c.setFillColor(colors.black)
    c.showPage()

    # ---------------- PAGE 2: views + flat pattern ----------------
    has_views = os.path.isfile(os.path.join(views_dir, "view_iso.png")) if views_dir else False
    has_flat = os.path.isfile(flat_pattern_path) if flat_pattern_path else False

    if has_views or has_flat:
        frame()
        header(f"{part_name} - Views & Computed Flat Pattern", "")
        content_top = PAGE_H - margin - 0.7*inch
        half_h = (content_top - margin - 0.2*inch) / 2 - 0.1*inch

        box(margin, content_top-half_h, (PAGE_W-2*margin)*0.5-0.1*inch, half_h, "ISOMETRIC")
        place_image(os.path.join(views_dir, "view_iso.png"), margin, content_top-half_h, (PAGE_W-2*margin)*0.5-0.1*inch, half_h)
        box(margin+(PAGE_W-2*margin)*0.5+0.1*inch, content_top-half_h, (PAGE_W-2*margin)*0.5-0.1*inch, half_h, "TOP")
        place_image(os.path.join(views_dir, "view_top.png"), margin+(PAGE_W-2*margin)*0.5+0.1*inch, content_top-half_h,
                    (PAGE_W-2*margin)*0.5-0.1*inch, half_h)

        y2 = content_top - half_h - 0.2*inch
        box(margin, y2-half_h, PAGE_W-2*margin, half_h, "COMPUTED FLAT PATTERN")
        place_image(flat_pattern_path, margin, y2-half_h, PAGE_W-2*margin, half_h)
        c.showPage()

    # ---------------- PAGE 3: full feature table ----------------
    if features:
        frame()
        header(f"{part_name} - Hole/Feature Position Table", "(Length, Transverse) in inches on the developed flat pattern")
        y = PAGE_H - margin - 1.0*inch

        # Handle both solid and surface model feature formats
        rows = [["Type", "Length (in)", "Transverse (in)", "Size"]]
        for feat in features:
            ftype = feat.get("type", "unknown")
            if "count" in feat:
                # Surface model: features have count and diameter_mm
                dia_mm = feat.get("diameter_mm")
                if dia_mm:
                    size = f"dia {dia_mm/25.4:.3f} in"
                else:
                    size = "-"
                for i in range(feat.get("count", 1)):
                    rows.append([ftype, "-", "-", size])
            else:
                # Solid model: individual features with positions
                if ftype == "slot":
                    size = f"{feat.get('width_in','?')} x {feat.get('slot_length_in','?')} in"
                elif "diameter_in" in feat:
                    size = f"dia {feat['diameter_in']} in"
                elif "size_in" in feat:
                    size = f"{feat['size_in'][0]} x {feat['size_in'][1]} in"
                else:
                    size = "-"
                rows.append([ftype, feat.get("length_in","-"), feat.get("transverse_in","-"), size])

        if len(rows) > 1:
            half = len(rows)//2 + 1
            left, right = [rows[0]]+rows[1:half], [rows[0]]+rows[half:]
            col_w = (PAGE_W-2*margin-0.3*inch)/2
            table(margin, y, col_w, left, [0.34,0.22,0.22,0.22], row_h=15, font=8)
            if len(right) > 1:
                table(margin+col_w+0.3*inch, y, col_w, right, [0.34,0.22,0.22,0.22], row_h=15, font=8)
        c.showPage()

    c.save()
    print("saved", out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--views", default="views")
    ap.add_argument("--flatpattern", default="flat_pattern.png")
    ap.add_argument("--out", default="report.pdf")
    args = ap.parse_args()
    make_report(args.json_path, args.views, args.flatpattern, args.out)
