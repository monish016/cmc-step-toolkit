"""
step_quote_extract.py
======================
Reusable pipeline: extract quoting-grade data from a STEP file's B-rep solid.
Supports BOTH sheet-metal and machined parts (auto-classified).

Sheet metal: envelope, weight, bend table, flat-pattern width, hole/slot/feature table.
Machined: envelope, weight, stock size, turning/milling classification,
          pocket/slot/hole/face feature table.

Usage:
    python3 step_quote_extract.py <path_to_step_file> [--density 7.9] [--k 0.44]
"""
import sys
import math
import json
import argparse
from collections import defaultdict

import cadquery as cq
from cadquery import importers
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.GeomAbs import (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone,
                          GeomAbs_Sphere, GeomAbs_Torus, GeomAbs_BSplineSurface,
                          GeomAbs_Line, GeomAbs_Circle)
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopoDS import TopoDS

# ==========================================================================
# CMC K-FACTOR LOOKUP TABLES
# Green-highlighted (recommended) values from CMC press brake tables.
# Key: thickness_in -> list of (bend_radius_in, k_factor) sorted by preference.
# ==========================================================================
_CMC_KFACTOR_STEEL = {
    0.030: [(0.078, 0.406), (0.109, 0.487)],
    0.036: [(0.078, 0.406), (0.109, 0.487)],
    0.048: [(0.078, 0.406), (0.109, 0.487), (0.141, 0.500)],
    0.060: [(0.078, 0.406), (0.109, 0.487), (0.141, 0.500)],
    0.075: [(0.078, 0.406), (0.109, 0.487), (0.141, 0.500)],
    0.105: [(0.109, 0.406), (0.141, 0.487), (0.203, 0.500)],
    0.120: [(0.141, 0.438), (0.203, 0.487)],
    0.135: [(0.141, 0.438), (0.203, 0.487)],
    0.188: [(0.203, 0.406), (0.313, 0.390)],
    0.250: [(0.250, 0.435), (0.313, 0.396)],
    0.375: [(0.406, 0.380)],
    0.500: [(0.406, 0.380), (0.500, 0.406)],
}
_CMC_KFACTOR_STAINLESS = {
    0.030: [(0.078, 0.406), (0.109, 0.487)],
    0.036: [(0.078, 0.406), (0.109, 0.487)],
    0.048: [(0.078, 0.406), (0.109, 0.487), (0.141, 0.500)],
    0.060: [(0.078, 0.406), (0.109, 0.487), (0.141, 0.500)],
    0.075: [(0.078, 0.406), (0.109, 0.487), (0.141, 0.500)],
    0.105: [(0.109, 0.406), (0.141, 0.487), (0.203, 0.500)],
    0.120: [(0.141, 0.438), (0.203, 0.487)],
    0.135: [(0.141, 0.438), (0.203, 0.487)],
    0.188: [(0.203, 0.406), (0.313, 0.390)],
    0.250: [(0.250, 0.435), (0.313, 0.396)],
    0.375: [(0.406, 0.452)],
    0.500: [(0.406, 0.452), (0.500, 0.406)],
}

# ==========================================================================
# GAUGE LOOKUP TABLE — Standard sheet metal gauges (inches)
# ==========================================================================
_GAUGE_TABLE = {
    # gauge: (steel_in, stainless_in, aluminum_in)
    7:  (0.1793, 0.1875, 0.1443),
    8:  (0.1644, 0.1719, 0.1285),
    9:  (0.1495, 0.1563, 0.1144),
    10: (0.1345, 0.1406, 0.1019),
    11: (0.1196, 0.1250, 0.0907),
    12: (0.1046, 0.1094, 0.0808),
    13: (0.0897, 0.0938, 0.0720),
    14: (0.0747, 0.0781, 0.0641),
    15: (0.0673, 0.0703, 0.0571),
    16: (0.0598, 0.0625, 0.0508),
    17: (0.0538, 0.0563, 0.0453),
    18: (0.0478, 0.0500, 0.0403),
    19: (0.0418, 0.0438, 0.0359),
    20: (0.0359, 0.0375, 0.0320),
    21: (0.0329, 0.0344, 0.0285),
    22: (0.0299, 0.0313, 0.0253),
    23: (0.0269, 0.0281, 0.0226),
    24: (0.0239, 0.0250, 0.0201),
    25: (0.0209, 0.0219, 0.0179),
    26: (0.0179, 0.0188, 0.0159),
    28: (0.0149, 0.0156, 0.0126),
    30: (0.0120, 0.0125, 0.0100),
}

def lookup_gauge(thickness_in, material="steel"):
    """Return (gauge_number, nominal_thickness_in) or (None, None) if no match."""
    col = 1 if "stainless" in material.lower() else (2 if "aluminum" in material.lower() else 0)
    best_ga, best_diff = None, 999
    for ga, vals in _GAUGE_TABLE.items():
        diff = abs(vals[col] - thickness_in)
        if diff < best_diff:
            best_diff = diff
            best_ga = ga
    # Only match if within 8% of a gauge entry
    if best_ga and best_diff / max(thickness_in, 0.001) < 0.08:
        return best_ga, _GAUGE_TABLE[best_ga][col]
    return None, None


# ==========================================================================
# HARDWARE CALLOUT TABLE — standard hole sizes → likely fastener
# ==========================================================================
_HARDWARE_HOLES_IN = [
    # (diameter_in, description)
    # Metric clearance holes (close fit)
    (0.126, "M3 clearance"),
    (0.165, "M4 clearance"),
    (0.209, "M5 clearance"),
    (0.252, "M6 clearance"),
    (0.323, "M8 clearance"),
    (0.394, "M10 clearance"),
    (0.512, "M12 clearance"),
    # Metric tap drill holes
    (0.098, "M3×0.5 tap drill"),
    (0.136, "M4×0.7 tap drill"),
    (0.169, "M5×0.8 tap drill"),
    (0.197, "M6×1.0 tap drill"),
    (0.260, "M8×1.25 tap drill"),
    (0.323, "M10×1.5 tap drill"),
    (0.397, "M12×1.75 tap drill"),
    # Imperial clearance holes
    (0.120, "#4 clearance"),
    (0.144, "#6 clearance"),
    (0.170, "#8 clearance"),
    (0.196, "#10 clearance"),
    (0.266, "1/4\" clearance"),
    (0.332, "5/16\" clearance"),
    (0.397, "3/8\" clearance"),
    (0.531, "1/2\" clearance"),
    # Imperial tap drills
    (0.089, "#4-40 tap drill"),
    (0.106, "#6-32 tap drill"),
    (0.136, "#8-32 tap drill"),
    (0.149, "#10-24 tap drill"),
    (0.159, "#10-32 tap drill"),
    (0.201, "1/4\"-20 tap drill"),
    (0.257, "5/16\"-18 tap drill"),
    (0.316, "3/8\"-16 tap drill"),
    (0.422, "1/2\"-13 tap drill"),
]

def lookup_hardware(diameter_in):
    """Return the closest hardware callout if within 3% tolerance, else None."""
    best, best_diff = None, 999
    for dia, desc in _HARDWARE_HOLES_IN:
        diff = abs(dia - diameter_in)
        if diff < best_diff:
            best_diff = diff
            best = desc
    if best and best_diff / max(diameter_in, 0.001) < 0.03:
        return best
    return None

def lookup_k_factor(thickness_in, bend_radius_in=None, material="steel"):
    """Look up CMC-recommended K-factor from press brake tables."""
    table = _CMC_KFACTOR_STAINLESS if "stainless" in material.lower() else _CMC_KFACTOR_STEEL
    best_thick = min(table.keys(), key=lambda t: abs(t - thickness_in))
    if abs(best_thick - thickness_in) / max(thickness_in, 0.001) > 0.15:
        return (0.44, None, "default (no table match)")
    entries = table[best_thick]
    if bend_radius_in is not None and bend_radius_in > 0:
        best = min(entries, key=lambda e: abs(e[0] - bend_radius_in))
        return (best[1], best[0], "CMC table (gauge " + str(best_thick) + '\")')
    else:
        return (entries[0][1], entries[0][0], "CMC table (gauge " + str(best_thick) + '\")')


# --------------------------------------------------------------------------
# 1. LOAD + ENVELOPE
# --------------------------------------------------------------------------
def load_step(path):
    shape = importers.importStep(path)
    # Handle assemblies: if the STEP file contains multiple solids,
    # fuse them or pick the largest by volume.
    solids = shape.solids().vals()
    if not solids:
        raise ValueError("STEP file contains no solid bodies â possibly a wireframe or surface model.")
    if len(solids) == 1:
        solid = solids[0]
    else:
        # Multiple solids (assembly) â pick largest by volume for analysis
        solid = max(solids, key=lambda s: abs(s.Volume()))
        print(f"Warning: STEP file contains {len(solids)} solids (assembly). "
              f"Analyzing the largest solid by volume.")
    return shape, solid


def get_envelope(solid, density_g_cm3=7.9):
    bb = solid.BoundingBox()
    vol_mm3 = abs(solid.Volume())  # abs() guards against reversed normals
    if vol_mm3 < 1e-6:
        raise ValueError("Solid has near-zero volume â degenerate or empty geometry.")
    vol_cm3 = vol_mm3 / 1000.0
    mass_g = vol_cm3 * density_g_cm3
    mass_lb = mass_g / 453.592
    area_mm2 = 0
    try:
        faces = cq.Workplane().add(solid).faces().vals()
        area_mm2 = sum(abs(f.Area()) for f in faces)
    except Exception:
        pass
    # Guard against degenerate bounding boxes
    xlen = max(bb.xlen, 1e-6)
    ylen = max(bb.ylen, 1e-6)
    zlen = max(bb.zlen, 1e-6)
    return {
        "bbox_mm": {"xlen": xlen, "ylen": ylen, "zlen": zlen,
                     "xmin": bb.xmin, "xmax": bb.xmax,
                     "ymin": bb.ymin, "ymax": bb.ymax,
                     "zmin": bb.zmin, "zmax": bb.zmax},
        "volume_mm3": vol_mm3,
        "volume_cm3": vol_cm3,
        "area_mm2": area_mm2,
        "mass_g": mass_g,
        "mass_lb": mass_lb,
        "mass_kg": mass_g / 1000.0,
    }


# --------------------------------------------------------------------------
# 2. FACE CLASSIFICATION
# --------------------------------------------------------------------------
def classify_faces(shape):
    """Return lists of (idx, face, extra-data) for planar, cylindrical, and other faces."""
    faces = shape.faces().vals()
    planar, cyl, other = [], [], []
    for i, f in enumerate(faces):
        surf = BRepAdaptor_Surface(f.wrapped, True)
        st = surf.GetType()
        if st == GeomAbs_Plane:
            planar.append((i, f, surf))
        elif st == GeomAbs_Cylinder:
            cyl.append((i, f, surf))
        else:
            other.append((i, f, surf))
    return faces, planar, cyl, other


def cyl_face_info(f, surf):
    cylg = surf.Cylinder()
    loc = cylg.Location()
    axis = cylg.Axis().Direction()
    r = cylg.Radius()
    u0, u1 = surf.FirstUParameter(), surf.LastUParameter()
    v0, v1 = surf.FirstVParameter(), surf.LastVParameter()
    ctr = f.Center()
    return {
        "radius": r,
        "axis": (axis.X(), axis.Y(), axis.Z()),
        "loc": (loc.X(), loc.Y(), loc.Z()),
        "center": (ctr.x, ctr.y, ctr.z),
        "u_sweep_deg": math.degrees(u1 - u0),
        "v_len": v1 - v0,
        "area": f.Area(),
        "bbox": f.BoundingBox(),
    }


# ==========================================================================
# FAB TYPE CLASSIFIER
# ==========================================================================
def classify_fab_type(shape, solid, envelope, planar, cyl, other):
    """
    Auto-classify the part as 'sheet_metal' or 'machined'.

    Heuristics:
    - Sheet metal: uniform thin wall, high surface-area-to-volume ratio,
      cylindrical bend faces with large v_len, few face types.
    - Machined: blocky, pockets, varied face normals, no bend signatures,
      or turning geometry (mostly cylindrical faces sharing an axis).

    Returns: (fab_type, confidence, sub_type, reasoning)
      fab_type: 'sheet_metal' or 'machined'
      sub_type: None for sheet_metal; 'milling', 'turning', or 'mill_turn' for machined
    """
    bb = envelope["bbox_mm"]
    dims = sorted([max(bb["xlen"], 1e-6), max(bb["ylen"], 1e-6), max(bb["zlen"], 1e-6)])
    vol_mm3 = envelope["volume_mm3"]
    bbox_vol = dims[0] * dims[1] * dims[2]
    fill_ratio = vol_mm3 / bbox_vol if bbox_vol > 1e-9 else 0

    total_faces = len(planar) + len(cyl) + len(other)
    if total_faces == 0:
        return "machined", "low", "milling", ["no_faces_found"]
    planar_ratio = len(planar) / total_faces
    cyl_ratio = len(cyl) / total_faces

    # Aspect ratio: thinnest dimension vs average of other two
    avg_other = (dims[1] + dims[2]) / 2
    aspect_ratio = dims[0] / avg_other if avg_other > 1e-6 else 1.0

    # Check for bend faces (sheet metal signature)
    cyl_infos = []
    for i, f, s in cyl:
        try:
            cyl_infos.append((i, cyl_face_info(f, s)))
        except Exception:
            pass  # skip degenerate cylindrical faces
    vlens = sorted(info["v_len"] for _, info in cyl_infos) if cyl_infos else []
    n = len(vlens)
    small_half = vlens[:max(1, n // 2)] if vlens else [1.0]
    thickness_est = sorted(small_half)[len(small_half) // 2]

    bend_candidates = [(i, info) for i, info in cyl_infos
                       if info["v_len"] > 8 * thickness_est]
    has_bends = len(bend_candidates) >= 1

    # Sheet metal score
    sm_score = 0
    reasons = []

    # Thin aspect ratio (thinnest dim < 15% of average of other two)
    if aspect_ratio < 0.15:
        sm_score += 3
        reasons.append(f"thin_aspect={aspect_ratio:.3f}")
    elif aspect_ratio < 0.25:
        sm_score += 2
        reasons.append(f"moderate_aspect={aspect_ratio:.3f}")

    # Low fill ratio (sheet metal wraps around, doesn't fill bbox)
    if fill_ratio < 0.15:
        sm_score += 2
        reasons.append(f"low_fill={fill_ratio:.3f}")
    elif fill_ratio < 0.30:
        sm_score += 1
        reasons.append(f"moderate_fill={fill_ratio:.3f}")

    # Bend faces detected
    if has_bends:
        sm_score += 3
        reasons.append(f"bend_faces={len(bend_candidates)}")

    # High surface-area-to-volume ratio (thin parts have high SA/V)
    sa_v = envelope["area_mm2"] / vol_mm3 if vol_mm3 > 0 else 0
    if sa_v > 0.5:
        sm_score += 1
        reasons.append(f"high_sa_v={sa_v:.3f}")

    # Machined indicators
    mach_score = 0

    # High fill ratio = blocky stock
    if fill_ratio > 0.40:
        mach_score += 2
        reasons.append(f"high_fill={fill_ratio:.3f}")

    # Thick aspect ratio
    if aspect_ratio > 0.3:
        mach_score += 2
        reasons.append(f"thick_aspect={aspect_ratio:.3f}")

    # No bends
    if not has_bends:
        mach_score += 1
        reasons.append("no_bends")

    # Many non-planar/non-cylindrical faces (cones, tori, splines = machined complexity)
    if len(other) > 3:
        mach_score += 1
        reasons.append(f"complex_faces={len(other)}")

    # Decision
    if sm_score >= 5 and sm_score > mach_score:
        fab_type = "sheet_metal"
        confidence = "high" if sm_score >= 7 else "medium"
        sub_type = None
    elif mach_score >= 3 and mach_score >= sm_score:
        fab_type = "machined"
        confidence = "high" if mach_score >= 5 else "medium"
        # Sub-classify: turning vs milling
        sub_type = classify_machining_type(cyl_infos, planar, total_faces, dims)
    else:
        # Ambiguous â default to sheet metal if we have bends, else machined
        if has_bends:
            fab_type = "sheet_metal"
            sub_type = None
        else:
            fab_type = "machined"
            sub_type = classify_machining_type(cyl_infos, planar, total_faces, dims)
        confidence = "low"

    return fab_type, confidence, sub_type, reasons


def classify_machining_type(cyl_infos, planar, total_faces, dims_sorted):
    """
    Sub-classify machined parts as 'turning', 'milling', or 'mill_turn'.

    Turning: mostly cylindrical faces sharing a common axis, roughly axisymmetric.
    Milling: mostly planar faces at varied heights, pockets, steps.
    Mill-turn: significant features of both.
    """
    if not cyl_infos:
        return "milling"

    # Check if cylindrical faces share a common axis
    def axis_key(a):
        # Normalize direction (prefer positive dominant component)
        ax = list(a)
        dominant = max(range(3), key=lambda i: abs(ax[i]))
        if ax[dominant] < 0:
            ax = [-c for c in ax]
        return tuple(round(c, 1) for c in ax)

    axis_counts = defaultdict(int)
    axis_area = defaultdict(float)
    total_cyl_area = 0
    for _, info in cyl_infos:
        key = axis_key(info["axis"])
        axis_counts[key] += 1
        axis_area[key] += info["area"]
        total_cyl_area += info["area"]

    if not axis_counts:
        return "milling"

    # Dominant axis = the one with the most cylindrical surface area
    dominant_axis = max(axis_area, key=axis_area.get)
    dominant_area_ratio = axis_area[dominant_axis] / total_cyl_area if total_cyl_area > 0 else 0
    dominant_count_ratio = axis_counts[dominant_axis] / len(cyl_infos) if cyl_infos else 0

    cyl_face_ratio = len(cyl_infos) / total_faces if total_faces > 0 else 0

    # Full-revolution faces (360 deg sweep) on the dominant axis = strong turning indicator
    full_rev_count = sum(1 for _, info in cyl_infos
                         if axis_key(info["axis"]) == dominant_axis
                         and info["u_sweep_deg"] > 350)

    # Turning: >60% of cyl area on one axis, many full-revolution faces
    # Also check aspect ratio â turning parts tend to be long in one dimension
    turning_score = 0
    if dominant_area_ratio > 0.7:
        turning_score += 2
    if full_rev_count >= 3:
        turning_score += 2
    if cyl_face_ratio > 0.4:
        turning_score += 1

    # Milling indicators: many planar faces, low cyl ratio
    milling_score = 0
    planar_ratio = len(planar) / total_faces if total_faces > 0 else 0
    if planar_ratio > 0.5:
        milling_score += 2
    if cyl_face_ratio < 0.25:
        milling_score += 1
    if full_rev_count < 2:
        milling_score += 1

    if turning_score >= 4 and turning_score > milling_score:
        return "turning"
    elif milling_score >= 3 and milling_score > turning_score:
        return "milling"
    elif turning_score >= 2 and milling_score >= 2:
        return "mill_turn"
    else:
        return "milling"


# ==========================================================================
# MACHINED PARTS ANALYSIS
# ==========================================================================
def compute_stock_size(envelope, machining_type):
    """
    Compute recommended raw stock dimensions with machining allowance.
    Returns stock size in mm and inches.
    """
    bb = envelope["bbox_mm"]
    dims = [bb["xlen"], bb["ylen"], bb["zlen"]]

    if machining_type == "turning":
        # For turning: stock is a round bar or tube
        # Diameter = max of the two shorter dims + allowance
        # Length = longest dim + allowance
        sorted_dims = sorted(dims)
        diameter = max(sorted_dims[0], sorted_dims[1]) + 4.0  # 2mm allowance per side
        length = sorted_dims[2] + 6.0  # 3mm allowance per end
        return {
            "type": "round_bar",
            "diameter_mm": round(diameter, 1),
            "diameter_in": round(diameter / 25.4, 3),
            "length_mm": round(length, 1),
            "length_in": round(length / 25.4, 3),
            "allowance_mm": 4.0,
            "description": f"{diameter:.0f}mm dia x {length:.0f}mm round bar"
        }
    else:
        # For milling: stock is a rectangular block
        # Add 2-3mm per side allowance
        allowance = 4.0  # 2mm per side
        stock = [round(d + allowance, 1) for d in dims]
        return {
            "type": "rectangular_block",
            "x_mm": stock[0], "y_mm": stock[1], "z_mm": stock[2],
            "x_in": round(stock[0] / 25.4, 3),
            "y_in": round(stock[1] / 25.4, 3),
            "z_in": round(stock[2] / 25.4, 3),
            "allowance_mm": allowance,
            "description": f"{stock[0]:.0f} x {stock[1]:.0f} x {stock[2]:.0f} mm block"
        }


def analyze_machined_features(shape, faces_list, planar, cyl, other, envelope):
    """
    Analyze features on a machined part: pockets, holes, slots, faces, curved features.
    Returns a structured feature list.
    """
    bb = envelope["bbox_mm"]
    features = []
    total_area = envelope["area_mm2"]

    # --- Holes: cylindrical faces with full or near-full revolution ---
    hole_groups = defaultdict(list)
    for i, f, surf in cyl:
        try:
            info = cyl_face_info(f, surf)
        except Exception:
            continue  # skip degenerate faces
        if info["u_sweep_deg"] > 350:  # Full revolution = hole or shaft
            # Group by radius (within tolerance)
            r_key = round(info["radius"], 1)
            hole_groups[r_key].append(info)

    for r_key, holes in hole_groups.items():
        for h in holes:
            dia_mm = h["radius"] * 2
            depth_mm = h["v_len"]
            features.append({
                "type": "hole",
                "diameter_mm": round(dia_mm, 2),
                "diameter_in": round(dia_mm / 25.4, 3),
                "depth_mm": round(depth_mm, 2),
                "depth_in": round(depth_mm / 25.4, 3),
                "center": h["center"],
                "confidence": "high"
            })

    # --- Pockets / steps: planar faces at different Z-levels (normals parallel to a primary axis) ---
    # Group planar faces by their normal direction
    normal_groups = defaultdict(list)
    for i, f, surf in planar:
        pln = surf.Plane()
        n = pln.Axis().Direction()
        n_key = (round(abs(n.X()), 1), round(abs(n.Y()), 1), round(abs(n.Z()), 1))
        loc = pln.Location()
        area = f.Area()
        ctr = f.Center()
        normal_groups[n_key].append({
            "idx": i,
            "normal": (n.X(), n.Y(), n.Z()),
            "location": (loc.X(), loc.Y(), loc.Z()),
            "area": area,
            "center": (ctr.x, ctr.y, ctr.z),
        })

    # For each normal direction, find faces at different depths = potential pockets/steps
    pocket_candidates = []
    for n_key, face_group in normal_groups.items():
        if len(face_group) < 2:
            continue
        # Determine the projection axis (dominant component of normal)
        sample_n = face_group[0]["normal"]
        proj_axis = max(range(3), key=lambda i: abs(sample_n[i]))

        # Group by depth along the projection axis
        depth_groups = defaultdict(list)
        for fg in face_group:
            depth = round(fg["center"][proj_axis], 1)
            depth_groups[depth].append(fg)

        # The outermost depth (highest absolute value along axis) = top face
        # Inner depths = pocket floors
        if len(depth_groups) > 1:
            depths = sorted(depth_groups.keys())
            # Consider non-largest faces at inner depths as pockets
            outer_depth = depths[-1] if sample_n[proj_axis] > 0 else depths[0]
            for d in depths:
                if d == outer_depth:
                    continue
                for fg in depth_groups[d]:
                    pocket_depth = abs(outer_depth - d)
                    if pocket_depth > 0.5 and fg["area"] > 10:  # Meaningful pocket
                        pocket_candidates.append({
                            "type": "pocket",
                            "depth_mm": round(pocket_depth, 2),
                            "depth_in": round(pocket_depth / 25.4, 3),
                            "area_mm2": round(fg["area"], 1),
                            "center": fg["center"],
                            "confidence": "medium"
                        })

    # Deduplicate pockets by proximity
    used = set()
    for i, p in enumerate(pocket_candidates):
        if i in used:
            continue
        for j in range(i + 1, len(pocket_candidates)):
            if j in used:
                continue
            if math.dist(p["center"], pocket_candidates[j]["center"]) < 5.0:
                # Merge: keep larger
                if pocket_candidates[j]["area_mm2"] > p["area_mm2"]:
                    p.update(pocket_candidates[j])
                used.add(j)
        features.append(p)

    # --- Slots: partial cylindrical faces (arcs < 360) that aren't bend faces ---
    for i, f, surf in cyl:
        try:
            info = cyl_face_info(f, surf)
        except Exception:
            continue
        if 10 < info["u_sweep_deg"] < 350:
            # Partial cylinder â could be a slot end or fillet
            if info["radius"] < 20 and info["v_len"] > 1.0:
                features.append({
                    "type": "slot_or_fillet",
                    "radius_mm": round(info["radius"], 2),
                    "radius_in": round(info["radius"] / 25.4, 3),
                    "sweep_deg": round(info["u_sweep_deg"], 1),
                    "length_mm": round(info["v_len"], 2),
                    "center": info["center"],
                    "confidence": "low"
                })

    # --- Curved/complex features from 'other' faces ---
    for i, f, surf in other:
        st = surf.GetType()
        area = f.Area()
        ctr = f.Center()
        type_name = {
            GeomAbs_Cone: "cone",
            GeomAbs_Sphere: "sphere",
            GeomAbs_Torus: "torus",
            GeomAbs_BSplineSurface: "freeform_surface",
        }.get(st, "complex_surface")
        if area > 5:  # Skip tiny edge blends
            features.append({
                "type": type_name,
                "area_mm2": round(area, 1),
                "center": (ctr.x, ctr.y, ctr.z),
                "confidence": "medium"
            })

    return features


def summarize_machined_features(features):
    """Produce counts and summary stats for the feature list."""
    counts = defaultdict(int)
    for f in features:
        counts[f["type"]] += 1

    holes = [f for f in features if f["type"] == "hole"]
    pockets = [f for f in features if f["type"] == "pocket"]

    summary = {
        "total_features": len(features),
        "feature_counts": dict(counts),
        "num_holes": len(holes),
        "num_pockets": len(pockets),
    }

    if holes:
        diameters = [h["diameter_mm"] for h in holes]
        summary["hole_diameters_mm"] = sorted(set(round(d, 1) for d in diameters))
        summary["hole_diameter_range_in"] = f'{min(diameters)/25.4:.3f}" - {max(diameters)/25.4:.3f}"'

    if pockets:
        depths = [p["depth_mm"] for p in pockets]
        summary["pocket_depth_range_mm"] = f"{min(depths):.1f} - {max(depths):.1f}"

    return summary


# ==========================================================================
# SHEET METAL ANALYSIS (existing logic, preserved)
# ==========================================================================
def detect_bend_faces(cyl, thickness_hint=None):
    """
    Auto-detect the sheet-metal bend cylindrical faces.
    """
    infos = []
    for i, f, s in cyl:
        try:
            infos.append((i, cyl_face_info(f, s)))
        except Exception:
            pass  # skip degenerate faces
    vlens = sorted(info["v_len"] for _, info in infos)
    n = len(vlens)
    small_half = vlens[: max(1, n // 2)]
    thickness_est = sorted(small_half)[len(small_half) // 2] if small_half else 1.0
    if thickness_hint:
        thickness_est = thickness_hint

    bend_candidates = [(i, info) for i, info in infos if info["v_len"] > 8 * thickness_est]

    def axis_key(a):
        return tuple(round(c, 2) for c in a)

    clusters = []
    used = set()
    for idx, (i, info) in enumerate(bend_candidates):
        if i in used:
            continue
        group = [(i, info)]
        used.add(i)
        for j, info2 in bend_candidates:
            if j in used:
                continue
            same_axis = axis_key(info["axis"]) == axis_key(info2["axis"]) or \
                        axis_key(tuple(-c for c in info["axis"])) == axis_key(info2["axis"])
            close = math.dist(info["loc"][1:], info2["loc"][1:]) < 2.0
            if same_axis and close:
                group.append((j, info2))
                used.add(j)
        clusters.append(group)

    bend_lines = []
    for group in clusters:
        radii = [info["radius"] for _, info in group]
        r_in, r_out = min(radii), max(radii)
        angle = max(info["u_sweep_deg"] for _, info in group)
        axis = group[0][1]["axis"]
        bend_lines.append({
            "inner_radius": r_in,
            "outer_radius": r_out,
            "thickness": r_out - r_in,
            "angle_deg": angle,
            "axis": axis,
            "face_idx": [i for i, _ in group],
        })

    thickness_mm = sum(b["thickness"] for b in bend_lines) / len(bend_lines) if bend_lines else thickness_est
    bend_radius_mm = sum(b["inner_radius"] for b in bend_lines) / len(bend_lines) if bend_lines else None

    return thickness_mm, bend_radius_mm, bend_lines


# --------------------------------------------------------------------------
# CROSS-SECTION + WIRE WALK -> flat/bend segment sequence (sheet metal)
# --------------------------------------------------------------------------
def dominant_bend_axis(bend_lines):
    counts = defaultdict(int)
    reps = {}
    for b in bend_lines:
        key = tuple(round(abs(c), 1) for c in b["axis"])
        counts[key] += 1
        reps[key] = b["axis"]
    best = max(counts, key=counts.get)
    return reps[best]


def cut_cross_section(solid, axis_dir, cut_point):
    pln = gp_Pln(gp_Pnt(*cut_point), gp_Dir(*axis_dir))
    sec = BRepAlgoAPI_Section(solid.wrapped, pln)
    sec.Build()
    result = sec.Shape()
    exp = TopExp_Explorer(result, TopAbs_EDGE)
    edges = []
    while exp.More():
        edges.append(TopoDS.Edge_s(exp.Current()))
        exp.Next()
    return edges


def edge_2d_info(edge, axis_dir):
    curve = BRepAdaptor_Curve(edge)
    t = curve.GetType()
    p0 = curve.Value(curve.FirstParameter())
    p1 = curve.Value(curve.LastParameter())

    ax = axis_dir
    helper = (1, 0, 0) if abs(ax[0]) < 0.9 else (0, 1, 0)
    def cross(a, b):
        return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    def norm(v):
        m = math.sqrt(sum(c*c for c in v))
        return tuple(c/m for c in v)
    u_axis = norm(cross(ax, helper))
    v_axis = norm(cross(ax, u_axis))

    def to_2d(p):
        v = (p.X(), p.Y(), p.Z())
        return (sum(v[k]*u_axis[k] for k in range(3)), sum(v[k]*v_axis[k] for k in range(3)))

    p0_2d, p1_2d = to_2d(p0), to_2d(p1)
    info = {"p0": p0_2d, "p1": p1_2d, "p0_3d": (p0.X(), p0.Y(), p0.Z()), "p1_3d": (p1.X(), p1.Y(), p1.Z())}
    if t == GeomAbs_Line:
        info["type"] = "line"
        info["length"] = math.dist(p0_2d, p1_2d)
    elif t == GeomAbs_Circle:
        circ = curve.Circle()
        info["type"] = "arc"
        info["radius"] = circ.Radius()
        info["sweep_deg"] = math.degrees(curve.LastParameter() - curve.FirstParameter())
        info["length"] = math.radians(info["sweep_deg"]) * info["radius"]
    else:
        info["type"] = "other"
        info["length"] = math.dist(p0_2d, p1_2d)
    return info


def walk_closed_loop(edges_info, thickness_mm, tol=0.5):
    def pt_key(p):
        return (round(p[0], 2), round(p[1], 2))

    adj = defaultdict(list)
    for idx, info in enumerate(edges_info):
        a, b = pt_key(info["p0"]), pt_key(info["p1"])
        adj[a].append((idx, b))
        adj[b].append((idx, a))

    cap_idxs = [i for i, info in enumerate(edges_info)
                if info["type"] == "line" and abs(info["length"] - thickness_mm) < max(0.3, 0.25 * thickness_mm)]

    visited_edges = set()
    if not cap_idxs:
        start_idx = 0
        chain = []
        cur_edge = start_idx
        cur_point = pt_key(edges_info[cur_edge]["p0"])
        for _ in range(len(edges_info)):
            info = edges_info[cur_edge]
            chain.append(info)
            visited_edges.add(cur_edge)
            other = pt_key(info["p1"]) if pt_key(info["p0"]) == cur_point else pt_key(info["p0"])
            nxt = None
            for e_idx, pt in adj[other]:
                if e_idx not in visited_edges:
                    nxt = (e_idx, other)
                    break
            if nxt is None:
                break
            cur_edge, cur_point = nxt[0], other
        return chain

    start_cap = cap_idxs[0]
    cur_point = pt_key(edges_info[start_cap]["p1"])
    visited_edges.add(start_cap)
    chain = []
    for _ in range(len(edges_info)):
        other = None
        chosen = None
        for e_idx, pt in adj[cur_point]:
            if e_idx not in visited_edges:
                chosen = e_idx
                other = pt
                break
        if chosen is None:
            break
        visited_edges.add(chosen)
        if chosen in cap_idxs:
            break
        info = edges_info[chosen]
        chain.append(info)
        cur_point = other
    return chain


def build_flat_layout(chain, bend_radius_mm, thickness_mm, k_factor=0.44):
    layout = []
    cum = 0.0
    for seg in chain:
        if seg["type"] == "line":
            length = seg["length"]
            layout.append({"kind": "flat", "start": cum, "end": cum + length,
                            "p0": seg["p0"], "p1": seg["p1"]})
            cum += length
        elif seg["type"] == "arc":
            angle = seg["sweep_deg"]
            dev_len = math.radians(angle) * (bend_radius_mm + k_factor * thickness_mm)
            layout.append({"kind": "bend", "start": cum, "end": cum + dev_len,
                            "angle_deg": angle})
            cum += dev_len
    return layout, cum


# --------------------------------------------------------------------------
# SHEET METAL FEATURE DETECTION
# --------------------------------------------------------------------------
def find_feature_faces(shape, bend_face_idxs, small_area_thresh=45):
    """
    Find faces that are likely part of cut features (holes, slots, rectangles).
    Filters out tiny edge blends and non-feature geometry.
    """
    faces = shape.faces().vals()
    candidates = []
    for i, f in enumerate(faces):
        if i in bend_face_idxs:
            continue
        surf = BRepAdaptor_Surface(f.wrapped, True)
        st = surf.GetType()
        area = f.Area()
        ctr = f.Center()
        bb = f.BoundingBox()
        if st == GeomAbs_Plane and area < small_area_thresh:
            # Skip very tiny planar faces (edge chamfers, micro-blends)
            if area < 0.5:
                continue
            candidates.append({"idx": i, "center": (ctr.x, ctr.y, ctr.z),
                                "bbox": (bb.xlen, bb.ylen, bb.zlen), "area": area, "kind": "planar"})
        elif st == GeomAbs_Cylinder:
            cylg = surf.Cylinder()
            r = cylg.Radius()
            sweep_deg = math.degrees(surf.LastUParameter() - surf.FirstUParameter())
            # Skip very tiny cylindrical faces (micro edge blends)
            if area < 0.3:
                continue
            # Allow holes up to ~4" diameter (50mm radius).
            # Larger cylinders are likely outer contour, not holes.
            if r > 50:
                continue
            # Skip very small sweep arcs (< 45 deg) -- these are edge
            # blends or tiny fillets, never standalone features.
            if sweep_deg < 45:
                continue
            candidates.append({"idx": i, "center": (ctr.x, ctr.y, ctr.z),
                                "bbox": (bb.xlen, bb.ylen, bb.zlen), "area": area,
                                "kind": "cyl", "radius": r,
                                "u_sweep": sweep_deg})
    return candidates


def cluster_features(candidates, thresh=12.0):
    n = len(candidates)
    parent = list(range(n))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for i in range(n):
        for j in range(i + 1, n):
            d = math.dist(candidates[i]["center"], candidates[j]["center"])
            if d < thresh:
                union(i, j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(candidates[i])
    return list(groups.values())


def merge_slot_pairs(clusters):
    cyl_only = []
    remaining = []
    for m in clusters:
        cyls = [x for x in m if x["kind"] == "cyl"]
        planars = [x for x in m if x["kind"] == "planar"]
        if cyls and not planars and len(m) <= 2:
            r = sum(c["radius"] for c in cyls) / len(cyls)
            avg_sweep = sum(c.get("u_sweep", 360) for c in cyls) / len(cyls)
            cx = sum(c["center"][0] for c in cyls) / len(cyls)
            cy = sum(c["center"][1] for c in cyls) / len(cyls)
            cz = sum(c["center"][2] for c in cyls) / len(cyls)
            cyl_only.append({"r": r, "center": (cx, cy, cz), "used": False, "orig": m,
                                 "avg_sweep": avg_sweep})
        else:
            remaining.append(m)
    slots = []
    for i in range(len(cyl_only)):
        if cyl_only[i]["used"]:
            continue
        # Skip corner radii / edge fillets (sweep < 140 deg) from slot pairing
        if cyl_only[i].get("avg_sweep", 360) < 140:
            continue
        for j in range(i+1, len(cyl_only)):
            if cyl_only[j]["used"]:
                continue
            if cyl_only[j].get("avg_sweep", 360) < 140:
                continue
            a, b = cyl_only[i], cyl_only[j]
            if abs(a["r"] - b["r"]) > 0.05:
                continue
            d = math.dist(a["center"], b["center"])
            if 2*a["r"] < d < 200.0:
                diffs = sorted(abs(a["center"][k]-b["center"][k]) for k in range(3))
                if diffs[0] < 1.0 and diffs[1] < 3.0:
                    a["used"] = b["used"] = True
                    r_mm = (a["r"]+b["r"])/2
                    slots.append({
                        "type": "slot",
                        "width_in": round(2*r_mm/25.4, 3),
                        "slot_length_in": round(d/25.4 + 2*r_mm/25.4, 3),
                        "center": tuple((a["center"][k]+b["center"][k])/2 for k in range(3)),
                        "confidence": "high",
                    })
                    break
    leftover_clusters = remaining + [c["orig"] for c in cyl_only if not c["used"]]
    return slots, leftover_clusters


def classify_cluster(members):
    """
    Classify a cluster of faces as a feature type.

    Key distinction: cylindrical faces with ~360Â° sweep = round holes,
    cylindrical faces with small sweep (~90Â°) alongside planar faces = corner
    fillets of a square/rectangular hole.
    """
    planars = [m for m in members if m["kind"] == "planar"]
    cyls = [m for m in members if m["kind"] == "cyl"]
    n_planar = len(planars)
    n_cyl = len(cyls)

    xs = [m["center"][0] for m in members]
    ys = [m["center"][1] for m in members]
    zs = [m["center"][2] for m in members]
    cx, cy, cz = sum(xs)/len(xs), sum(ys)/len(ys), sum(zs)/len(zs)
    spread = (max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs))

    if cyls:
        # Separate full-circle cylinders (round holes) from partial arcs (corner fillets)
        full_circle_cyls = [c for c in cyls if c["u_sweep"] > 300]
        partial_cyls = [c for c in cyls if c["u_sweep"] <= 300]

        # Case 1: Full-circle cylinders with no/few planars = ROUND hole
        if full_circle_cyls and n_planar <= 1:
            radii = [c["radius"] for c in full_circle_cyls]
            r_avg = sum(radii) / len(radii)
            if r_avg * 2 / 25.4 < 0.08:
                return None
            dia_in = 2 * r_avg / 25.4
            return {"type": "round",
                    "diameter_in": round(dia_in, 3), "center": (cx, cy, cz), "confidence": "high"}

        # Case 2: Only partial arcs (corner fillets) + planar faces = SQUARE/RECT hole
        if partial_cyls and n_planar >= 2:
            # Use the spread of ALL member faces to determine the rectangle size
            xl, yl, zl = spread
            # Pick the two largest spread dimensions as the hole size
            dims = sorted([xl, yl, zl], reverse=True)
            d1, d2 = dims[0], dims[1]
            if d1 < 1.5 or d2 < 1.5:
                return None
            return {"type": "square_or_rect",
                    "size_in": (round(d1/25.4, 3), round(d2/25.4, 3)),
                    "center": (cx, cy, cz), "confidence": "high"}

        # Case 3: Partial arcs without enough planars â likely corner fillets
        # that didn't cluster with their planar walls. Check if they form
        # a rectangular pattern (4 corners at ~90Â° each)
        if partial_cyls and len(partial_cyls) >= 4 and n_planar == 0:
            avg_sweep = sum(c["u_sweep"] for c in partial_cyls) / len(partial_cyls)
            if 70 < avg_sweep < 110:  # ~90Â° corner fillets
                # First check: if total sweep ~360 and all radii match,
                # this is a round hole split into quadrants, not a rect cutout
                total_sw = sum(c["u_sweep"] for c in partial_cyls)
                radii_pc = [c["radius"] for c in partial_cyls]
                r_spread_pc = max(radii_pc) - min(radii_pc) if radii_pc else 999
                if total_sw > 300 and r_spread_pc < 0.5:
                    r_avg = sum(radii_pc) / len(radii_pc)
                    if r_avg * 2 / 25.4 >= 0.08:
                        dia_in = 2 * r_avg / 25.4
                        return {"type": "round",
                                "diameter_in": round(dia_in, 3), "center": (cx, cy, cz),
                                "confidence": "medium"}
                xl, yl, zl = spread
                dims = sorted([xl, yl, zl], reverse=True)
                d1, d2 = dims[0], dims[1]
                if d1 >= 1.5 and d2 >= 1.5:
                    return {"type": "square_or_rect",
                            "size_in": (round(d1/25.4, 3), round(d2/25.4, 3)),
                            "center": (cx, cy, cz), "confidence": "medium"}

        # Case 3b: Multiple partial cylinders whose sweeps sum to ~360°
        # = round hole split into segments by the CAD kernel (e.g. two 180° halves)
        if partial_cyls and len(partial_cyls) >= 2 and n_planar <= 1:
            total_sweep = sum(c["u_sweep"] for c in partial_cyls)
            radii = [c["radius"] for c in partial_cyls]
            r_spread = max(radii) - min(radii)
            if total_sweep > 300 and r_spread < 0.5:  # matching radii, full circle
                r_avg = sum(radii) / len(radii)
                if r_avg * 2 / 25.4 < 0.08:
                    return None
                dia_in = 2 * r_avg / 25.4
                return {"type": "round",
                        "diameter_in": round(dia_in, 3), "center": (cx, cy, cz), "confidence": "high"}

        # Case 4: Mixed or ambiguous â fall back to checking if it's round
        if full_circle_cyls:
            radii = [c["radius"] for c in full_circle_cyls]
            r_avg = sum(radii) / len(radii)
            if r_avg * 2 / 25.4 < 0.08:
                return None
            dia_in = 2 * r_avg / 25.4
            return {"type": "round",
                    "diameter_in": round(dia_in, 3), "center": (cx, cy, cz), "confidence": "medium"}

        # Partial cyls only, few of them â likely edge fillets, not a feature
        if len(partial_cyls) <= 2 and n_planar == 0:
            return None

        # Last resort: use spread
        radii = [c["radius"] for c in cyls]
        r_avg = sum(radii) / len(radii)
        if r_avg * 2 / 25.4 < 0.08:
            return None
        dia_in = 2 * r_avg / 25.4
        return {"type": "round",
                "diameter_in": round(dia_in, 3), "center": (cx, cy, cz), "confidence": "low"}
    else:
        # Planar-only cluster
        xl, yl, zl = spread
        dims = sorted([xl, yl, zl], reverse=True)
        d1, d2 = dims[0], dims[1]
        if d1 < 1.5 or d2 < 1.5:
            return None
        return {"type": "square_or_rect", "size_in": (round(d1/25.4, 3), round(d2/25.4, 3)),
                "center": (cx, cy, cz), "confidence": "high"}


# ==========================================================================
def _compute_complexity(features, bend_lines, flat_width_mm, flat_length_mm, thickness_mm):
    """
    Compute relative complexity scores for laser cutting and bending.
    Returns dict with scores (1-5 scale) and notes.
    """
    notes = []

    # --- Laser/cut complexity ---
    n_features = len([f for f in features if f["type"] in ("round", "square_or_rect", "slot")])
    cut_score = 1
    if n_features > 20:
        cut_score = 5
        notes.append("Very high feature count (>20)")
    elif n_features > 10:
        cut_score = 4
    elif n_features > 5:
        cut_score = 3
    elif n_features > 2:
        cut_score = 2

    # Small features increase complexity
    small_holes = [f for f in features if f["type"] == "round" and f.get("diameter_in", 1) < 0.15]
    if small_holes:
        cut_score = min(5, cut_score + 1)
        notes.append(f"{len(small_holes)} small hole(s) <0.15\" dia")

    # Check hole-to-thickness ratio (holes smaller than thickness are difficult)
    thickness_in = thickness_mm / 25.4
    tiny_holes = [f for f in features if f["type"] == "round"
                  and f.get("diameter_in", 1) < thickness_in]
    if tiny_holes:
        cut_score = min(5, cut_score + 1)
        notes.append(f"{len(tiny_holes)} hole(s) smaller than material thickness")

    # --- Bend complexity ---
    bend_score = 1
    n_bends = len(bend_lines)
    if n_bends > 6:
        bend_score = 5
        notes.append("Many bends (>6)")
    elif n_bends > 4:
        bend_score = 4
    elif n_bends > 2:
        bend_score = 3
    elif n_bends > 0:
        bend_score = 2

    # Non-90° bends increase complexity
    non_90 = [b for b in bend_lines if abs(b["angle_deg"] - 90) > 5]
    if non_90:
        bend_score = min(5, bend_score + 1)
        notes.append(f"{len(non_90)} non-90° bend(s)")

    # --- Overall complexity ---
    overall = max(cut_score, bend_score)
    labels = {1: "Simple", 2: "Standard", 3: "Moderate", 4: "Complex", 5: "Very Complex"}

    return {
        "cut_score": cut_score,
        "bend_score": bend_score,
        "overall_score": overall,
        "overall_label": labels[overall],
        "notes": notes,
    }


# ==========================================================================
# 5. MAIN DRIVER â unified entry point
# ==========================================================================
def run(step_path, density=7.9, k_factor=0.44, out_json="geometry_extract.json", material="steel"):
    try:
        shape, solid = load_step(step_path)
    except ValueError as e:
        # Re-raise with clear message for the caller
        raise
    except Exception as e:
        raise ValueError(f"Failed to load STEP file: {e}")

    envelope = get_envelope(solid, density)

    try:
        faces_list, planar, cyl, other = classify_faces(shape)
    except Exception as e:
        print(f"Warning: Face classification failed: {e}")
        faces_list, planar, cyl, other = [], [], [], []

    # --- Auto-classify fab type ---
    fab_type, confidence, sub_type, reasons = classify_fab_type(
        shape, solid, envelope, planar, cyl, other)

    result = {
        "source_file": step_path,
        "fab_type": fab_type,
        "fab_type_confidence": confidence,
        "fab_sub_type": sub_type,
        "classification_reasons": reasons,
        "envelope": envelope,
        "face_counts": {
            "planar": len(planar),
            "cylindrical": len(cyl),
            "other": len(other),
            "total": len(planar) + len(cyl) + len(other),
        },
    }

    try:
        if fab_type == "sheet_metal":
            result.update(run_sheet_metal(shape, solid, envelope, planar, cyl, other, k_factor, material))
        else:
            result.update(run_machined(shape, solid, envelope, faces_list, planar, cyl, other, sub_type))
    except Exception as e:
        print(f"Warning: Detailed analysis failed, returning envelope-only: {e}")
        result["analysis_error"] = str(e)

    with open(out_json, "w") as f:
        json.dump(result, f, indent=2, default=str)
    return result


def run_sheet_metal(shape, solid, envelope, planar, cyl, other_faces, k_factor, material="steel"):
    """Sheet metal analysis path (original logic)."""
    thickness_mm, bend_radius_mm, bend_lines = detect_bend_faces(cyl)

    # Guard against zero thickness (degenerate geometry)
    if thickness_mm < 1e-6:
        thickness_mm = 1.0  # fallback 1mm
    if bend_radius_mm is not None and bend_radius_mm < 1e-6:
        bend_radius_mm = thickness_mm  # fallback to thickness

    # --- CMC K-factor lookup ---
    thickness_in = thickness_mm / 25.4
    bend_radius_in = (bend_radius_mm / 25.4) if bend_radius_mm else None
    looked_up_k, matched_br, k_source = lookup_k_factor(thickness_in, bend_radius_in, material)
    k_factor = looked_up_k  # override default with table value

    axis_dir = dominant_bend_axis(bend_lines) if bend_lines else (1, 0, 0)
    bb = envelope["bbox_mm"]
    cut_point = ((bb["xmin"]+bb["xmax"])/2, (bb["ymin"]+bb["ymax"])/2, (bb["zmin"]+bb["zmax"])/2)

    try:
        edges = cut_cross_section(solid, axis_dir, cut_point)
        edges_info = [edge_2d_info(e, axis_dir) for e in edges]
        chain = walk_closed_loop(edges_info, thickness_mm)
        layout, flat_width_mm = build_flat_layout(
            chain, bend_radius_mm or thickness_mm, thickness_mm, k_factor)
    except Exception as e:
        print(f"Warning: Cross-section / flat layout failed: {e}")
        layout, flat_width_mm = [], 0.0

    def norm2(v):
        m = math.hypot(*v)
        return (v[0]/m, v[1]/m) if m > 1e-9 else (0, 0)

    def to_2d(p3d):
        ax = axis_dir
        helper = (1, 0, 0) if abs(ax[0]) < 0.9 else (0, 1, 0)
        def cross(a, b):
            return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
        def norm3(v):
            m = math.sqrt(sum(c*c for c in v)); return tuple(c/m for c in v)
        u_axis = norm3(cross(ax, helper))
        v_axis = norm3(cross(ax, u_axis))
        return (sum(p3d[k]*u_axis[k] for k in range(3)), sum(p3d[k]*v_axis[k] for k in range(3)))

    def transverse_pos_mm(y_z):
        best = None
        for seg in layout:
            if seg["kind"] != "flat":
                continue
            p0, p1 = seg["p0"], seg["p1"]
            d = norm2((p1[0]-p0[0], p1[1]-p0[1]))
            v = (y_z[0]-p0[0], y_z[1]-p0[1])
            tproj = v[0]*d[0] + v[1]*d[1]
            perp = abs(v[0]*d[1] - v[1]*d[0])
            seglen = seg["end"] - seg["start"]
            if -3 <= tproj <= seglen+3 and perp < 3.0:
                cand = seg["start"] + max(0, min(seglen, tproj))
                if best is None or perp < best[1]:
                    best = (cand, perp)
        return best[0] if best else None

    corners = [(bb["xmin"] if i&1 else bb["xmax"],
                bb["ymin"] if i&2 else bb["ymax"],
                bb["zmin"] if i&4 else bb["zmax"]) for i in range(8)]

    def project_point_to_axis(p3d):
        return sum(p3d[k]*axis_dir[k] for k in range(3))

    ref_proj = min(project_point_to_axis(c) for c in corners)
    max_proj = max(project_point_to_axis(c) for c in corners)
    flat_length_mm = max_proj - ref_proj

    bend_face_idxs = set()
    for b in bend_lines:
        bend_face_idxs.update(b["face_idx"])
    candidates = find_feature_faces(shape, bend_face_idxs)
    clusters = cluster_features(candidates)
    slot_features, remaining_clusters = merge_slot_pairs(clusters)
    classified = [classify_cluster(m) for m in remaining_clusters if len(m) > 0]
    features = slot_features + [c for c in classified if c is not None]
    unclassified_count = sum(1 for c in classified if c is None)

    for feat in features:
        cx, cy, cz = feat["center"]
        length_along_axis = project_point_to_axis((cx, cy, cz))
        feat["length_in"] = round((length_along_axis - ref_proj) / 25.4, 3)
        yz = to_2d((cx, cy, cz))
        t_mm = transverse_pos_mm(yz)
        feat["transverse_in"] = round(t_mm/25.4, 3) if t_mm is not None else None

    # --- Gauge auto-detection ---
    gauge_num, gauge_nominal = lookup_gauge(thickness_mm / 25.4, material)

    # --- Hardware callouts for features ---
    for feat in features:
        if feat["type"] == "round" and "diameter_in" in feat:
            hw = lookup_hardware(feat["diameter_in"])
            if hw:
                feat["hardware_hint"] = hw

    # --- Countersink / chamfer detection ---
    # Look for cone faces near round holes
    countersinks = []
    chamfers = []
    for i, f, s in other_faces:
        st = s.GetType()
        if st == GeomAbs_Cone:
            area = f.Area()
            ctr = f.Center()
            cone_center = (ctr.x, ctr.y, ctr.z)
            cone = s.Cone()
            half_angle_deg = math.degrees(cone.SemiAngle())
            # Check if near a round hole
            matched_hole = False
            for feat in features:
                if feat["type"] == "round":
                    d = math.dist(cone_center, feat["center"])
                    if d < 8.0:  # within 8mm of a hole center
                        countersinks.append({
                            "type": "countersink",
                            "angle_deg": round(abs(half_angle_deg) * 2, 1),
                            "near_hole_dia_in": feat["diameter_in"],
                            "center": cone_center,
                            "confidence": "medium",
                        })
                        matched_hole = True
                        break
            if not matched_hole and area > 2.0:
                chamfers.append({
                    "type": "chamfer",
                    "angle_deg": round(abs(half_angle_deg), 1),
                    "center": cone_center,
                    "confidence": "low",
                })
    features.extend(countersinks)
    # Only add chamfers if there are a meaningful number (otherwise noise)
    if len(chamfers) <= 8:
        features.extend(chamfers)

    return {
        "thickness_in": round(thickness_mm/25.4, 4),
        "gauge": gauge_num,
        "gauge_nominal_in": gauge_nominal,
        "bend_radius_in": round(bend_radius_mm/25.4, 4) if bend_radius_mm else None,
        "num_bends": len(bend_lines),
        "bend_angles_deg": sorted([round(b["angle_deg"],1) for b in bend_lines]),
        "k_factor_assumed": k_factor,
        "k_factor_source": k_source,
        "k_factor_matched_bend_radius_in": matched_br,
        "flat_width_in": round(flat_width_mm/25.4, 3),
        "flat_length_in": round(flat_length_mm/25.4, 3),
        "layout_segments": [
            {"kind": s["kind"], "start_in": round(s["start"]/25.4,3), "end_in": round(s["end"]/25.4,3),
             **({"angle_deg": s["angle_deg"]} if s["kind"]=="bend" else {})}
            for s in layout
        ],
        "features_raw_count": len(features),
        "features_unclassified_count": unclassified_count,
        "features": features,
        "processes": ["blanking", "bending"] + (
            ["tapping (check manually)"] if any(f["type"] == "round" and f.get("diameter_in", 1) < 0.5 for f in features) else []
        ),
        "complexity": _compute_complexity(features, bend_lines, flat_width_mm, flat_length_mm, thickness_mm),
    }


def run_machined(shape, solid, envelope, faces_list, planar, cyl, other, machining_type):
    """Machined part analysis path."""
    stock = compute_stock_size(envelope, machining_type)
    features = analyze_machined_features(shape, faces_list, planar, cyl, other, envelope)
    summary = summarize_machined_features(features)

    bb = envelope["bbox_mm"]
    bbox_vol = bb["xlen"] * bb["ylen"] * bb["zlen"]
    material_removal = 1 - (envelope["volume_mm3"] / bbox_vol) if bbox_vol > 0 else 0

    processes = []
    if machining_type == "turning":
        processes.append("turning")
    if machining_type == "milling":
        processes.append("milling")
    if machining_type == "mill_turn":
        processes.extend(["turning", "milling"])

    # Infer additional processes
    if summary["num_holes"] > 0:
        processes.append("drilling")
    has_small_holes = any(f["type"] == "hole" and f["diameter_mm"] < 12 for f in features)
    if has_small_holes:
        processes.append("tapping (check manually)")

    return {
        "machining_type": machining_type,
        "stock_size": stock,
        "material_removal_ratio": round(material_removal, 3),
        "feature_summary": summary,
        "features": features,
        "features_raw_count": len(features),
        "processes": processes,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step_file")
    ap.add_argument("--density", type=float, default=7.9, help="g/cm3, default 7.9 (stainless)")
    ap.add_argument("--k", type=float, default=0.44, help="bend-allowance K-factor")
    ap.add_argument("--material", default="steel", help="steel or stainless (for K-factor table)")
    ap.add_argument("--out", default="geometry_extract.json")
    args = ap.parse_args()
    res = run(args.step_file, args.density, args.k, args.out, args.material)
    print(json.dumps(res, indent=2, default=str))
