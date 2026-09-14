"""
step_quote_extract.py
======================
Reusable pipeline: extract quoting-grade data (envelope, weight, bend table,
flat-pattern width, hole/slot/feature table) directly from a sheet-metal
STEP file's B-rep solid -- no drawing/PDF required.

See INSTRUCTIONS.md in this folder for the full procedure and what to
sanity-check on each new part.

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
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Line, GeomAbs_Circle
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopoDS import TopoDS


# --------------------------------------------------------------------------
# 1. LOAD + ENVELOPE
# --------------------------------------------------------------------------
def load_step(path):
    shape = importers.importStep(path)
    solid = shape.val()
    return shape, solid


def get_envelope(solid, density_g_cm3=7.9):
    bb = solid.BoundingBox()
    vol_mm3 = solid.Volume()
    vol_cm3 = vol_mm3 / 1000.0
    mass_g = vol_cm3 * density_g_cm3
    mass_lb = mass_g / 453.592
    return {
        "bbox_mm": {"xlen": bb.xlen, "ylen": bb.ylen, "zlen": bb.zlen,
                     "xmin": bb.xmin, "xmax": bb.xmax,
                     "ymin": bb.ymin, "ymax": bb.ymax,
                     "zmin": bb.zmin, "zmax": bb.zmax},
        "volume_cm3": vol_cm3,
        "mass_lb": mass_lb,
        "mass_kg": mass_g / 1000.0,
    }


# --------------------------------------------------------------------------
# 2. FACE CLASSIFICATION + AUTO BEND-FACE DETECTION
# --------------------------------------------------------------------------
def classify_faces(shape):
    """Return lists of (idx, face, extra-data) for planar, cylindrical, and conical faces."""
    faces = shape.faces().vals()
    planar, cyl, cone, other = [], [], [], []
    for i, f in enumerate(faces):
        surf = BRepAdaptor_Surface(f.wrapped, True)
        st = surf.GetType()
        if st == GeomAbs_Plane:
            planar.append((i, f, surf))
        elif st == GeomAbs_Cylinder:
            cyl.append((i, f, surf))
        elif st == GeomAbs_Cone:
            cone.append((i, f, surf))
        else:
            other.append((i, f, surf))
    return faces, planar, cyl, cone, other


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


def detect_bend_faces(cyl, thickness_hint=None):
    """
    Auto-detect the sheet-metal bend cylindrical faces.
    Heuristic: bend faces have a V-length (extent along the fold axis) that is
    MUCH larger than the sheet thickness, whereas hole/slot/fillet cylindrical
    faces have V-length approx equal to the material thickness.
    Returns: thickness_mm, bend_radius_mm, list of bend-face clusters
             (each with axis, angle_deg, v_range, faces).
    """
    infos = [(i, cyl_face_info(f, s)) for i, f, s in cyl]
    vlens = sorted(info["v_len"] for _, info in infos)
    # thickness estimate = median of the smaller half of v_len values
    # (most cylindrical faces on a sheet-metal part are hole/slot walls)
    n = len(vlens)
    small_half = vlens[: max(1, n // 2)]
    thickness_est = sorted(small_half)[len(small_half) // 2] if small_half else 1.0
    if thickness_hint:
        thickness_est = thickness_hint

    bend_candidates = [(i, info) for i, info in infos if info["v_len"] > 8 * thickness_est]

    # cluster bend candidates by (axis direction rounded, location proximity)
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
            close = math.dist(info["loc"][1:], info2["loc"][1:]) < 2.0  # compare off-axis coords
            if same_axis and close:
                group.append((j, info2))
                used.add(j)
        clusters.append(group)

    bend_lines = []
    for group in clusters:
        radii = [info["radius"] for _, info in group]
        r_in, r_out = min(radii), max(radii)
        angle = max(info["u_sweep_deg"] for _, info in group)
        # axis direction (use first face's axis, normalized sign convention: prefer +component)
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
# 3. CROSS-SECTION CUT + WIRE WALK  ->  ordered flat/bend segment sequence
# --------------------------------------------------------------------------
def dominant_bend_axis(bend_lines):
    """Pick the most common axis direction among detected bend faces (rounded to a unit vector)."""
    counts = defaultdict(int)
    reps = {}
    for b in bend_lines:
        key = tuple(round(abs(c), 1) for c in b["axis"])
        counts[key] += 1
        reps[key] = b["axis"]
    best = max(counts, key=counts.get)
    return reps[best]


def cut_cross_section(solid, axis_dir, cut_point):
    """Cut the solid with a plane perpendicular to axis_dir at cut_point (3-tuple)."""
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
    """
    Project edge endpoints/geometry onto the 2D plane perpendicular to axis_dir.
    Returns dict with type ('line'/'arc'), endpoints (2D), length, and (for arcs)
    radius + sweep angle. The 2D basis is chosen automatically (any two axes
    perpendicular to axis_dir).
    """
    curve = BRepAdaptor_Curve(edge)
    t = curve.GetType()
    p0 = curve.Value(curve.FirstParameter())
    p1 = curve.Value(curve.LastParameter())

    # build a 2D basis perpendicular to axis_dir
    ax = axis_dir
    # pick a helper vector not parallel to ax
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
    """
    Given all section-edge 2D infos (forming one closed loop = outer path + end
    cap + inner path (reversed) + other end cap), split at the two 'cap' edges
    (short line segments whose length is approximately the sheet thickness) and
    return ONE ordered chain (list of segments in sequence: flat/bend/flat/...).
    """
    def pt_key(p):
        return (round(p[0], 2), round(p[1], 2))

    # build adjacency graph over endpoints
    adj = defaultdict(list)  # point_key -> list of (edge_index, other_endpoint_key)
    for idx, info in enumerate(edges_info):
        a, b = pt_key(info["p0"]), pt_key(info["p1"])
        adj[a].append((idx, b))
        adj[b].append((idx, a))

    # identify cap edges: line type, length close to thickness
    cap_idxs = [i for i, info in enumerate(edges_info)
                if info["type"] == "line" and abs(info["length"] - thickness_mm) < max(0.3, 0.25 * thickness_mm)]

    # walk the loop starting after a cap edge, stop at the next cap edge
    visited_edges = set()
    if not cap_idxs:
        # fallback: no caps found (closed tube profile) -- walk the whole loop once
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
        if chosen in cap_idxs:  # reached the other end cap -- stop (don't include the cap itself)
            break
        info = edges_info[chosen]
        chain.append(info)
        cur_point = other
    return chain


def build_flat_layout(chain, bend_radius_mm, thickness_mm, k_factor=0.44):
    """
    Convert the ordered chain of line/arc segments into a cumulative
    developed-length layout: [(kind, start_offset, end_offset, extra), ...]
    kind is 'flat' or 'bend' (with angle_deg for bends).
    Also returns the raw (p0,p1) 2D endpoints for each flat segment so hole
    positions can later be projected onto them.
    """
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
# 4. HARDWARE-HINT LOOKUP TABLES
# --------------------------------------------------------------------------
# Standard UNC/UNF tap-drill diameters (inches) -> tap size label
_TAP_DRILLS = [
    (0.0890, "#3-48"),  (0.0960, "#4-40"),  (0.1015, "#4-48"),
    (0.1065, "#6-32"),  (0.1130, "#6-40"),
    (0.1360, "#8-32"),  (0.1405, "#8-36"),
    (0.1495, "#10-24"), (0.1590, "#10-32"),
    (0.2010, "1/4-20"), (0.2090, "1/4-28"),
    (0.2570, "5/16-18"),(0.2720, "5/16-24"),
    (0.3125, "3/8-16"), (0.3320, "3/8-24"),
    (0.4219, "1/2-13"), (0.4531, "1/2-20"),
]

# Standard clearance-hole diameters (close + normal fit) -> screw size
_CLEARANCE_HOLES = [
    (0.1285, "#4"),  (0.1405, "#4"),
    (0.1495, "#6"),  (0.1610, "#6"),
    (0.1770, "#8"),  (0.1890, "#8"),
    (0.1990, "#10"), (0.2130, "#10"),
    (0.2570, "1/4"), (0.2720, "1/4"),
    (0.3230, "5/16"),(0.3440, "5/16"),
    (0.3860, "3/8"), (0.3970, "3/8"),
    (0.5160, "1/2"), (0.5310, "1/2"),
]


def _hole_hardware_hint(diameter_in):
    """Match a hole diameter against standard tap-drill and clearance-hole tables.
    Returns e.g. 'tap_1/4-20', 'clearance_#10', or '' if no match."""
    # Tap drills first (tighter tolerance -- drilled to specific size)
    for drill_dia, tap_size in _TAP_DRILLS:
        if abs(diameter_in - drill_dia) <= 0.004:
            return "tap_" + tap_size
    # Clearance holes (wider tolerance)
    for clear_dia, screw_size in _CLEARANCE_HOLES:
        if abs(diameter_in - clear_dia) <= 0.008:
            return "clearance_" + screw_size
    return ""


# --------------------------------------------------------------------------
# 5. FEATURE (HOLE/SLOT/TAB) AUTO-CLUSTERING
# --------------------------------------------------------------------------
def find_feature_faces(shape, bend_face_idxs, small_area_thresh=45):
    """Collect small planar + small-radius cylindrical faces = hole/slot/tab candidates."""
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
            candidates.append({"idx": i, "center": (ctr.x, ctr.y, ctr.z),
                                "bbox": (bb.xlen, bb.ylen, bb.zlen), "area": area, "kind": "planar"})
        elif st == GeomAbs_Cylinder:
            cylg = surf.Cylinder()
            r = cylg.Radius()
            if r < 8:
                candidates.append({"idx": i, "center": (ctr.x, ctr.y, ctr.z),
                                    "bbox": (bb.xlen, bb.ylen, bb.zlen), "area": area,
                                    "kind": "cyl", "radius": r,
                                    "u_sweep": math.degrees(surf.LastUParameter() - surf.FirstUParameter())})
        elif st == GeomAbs_Cone:
            coneg = surf.Cone()
            semi_angle = abs(math.degrees(coneg.SemiAngle()))
            ref_r = coneg.RefRadius()
            # Countersinks: semi-angle 35-50 deg (70-100 deg included)
            # Chamfers: semi-angle typically 45 deg
            if ref_r < 15 and area < small_area_thresh * 3:
                candidates.append({"idx": i, "center": (ctr.x, ctr.y, ctr.z),
                                    "bbox": (bb.xlen, bb.ylen, bb.zlen), "area": area,
                                    "kind": "cone", "semi_angle_deg": semi_angle,
                                    "ref_radius": ref_r,
                                    "u_sweep": math.degrees(surf.LastUParameter() - surf.FirstUParameter())})
    return candidates


def cluster_features(candidates, thresh=7.0):
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
    """
    Two matching-radius cylindrical clusters (no planar faces) that are
    collinear and reasonably close together are the two rounded ends of one
    slot. Detect and merge those pairs; return (slot_features, remaining_clusters).
    """
    cyl_only = []
    remaining = []
    for m in clusters:
        cyls = [x for x in m if x["kind"] == "cyl"]
        planars = [x for x in m if x["kind"] == "planar"]
        if cyls and not planars and len(m) <= 2:
            r = sum(c["radius"] for c in cyls) / len(cyls)
            cx = sum(c["center"][0] for c in cyls) / len(cyls)
            cy = sum(c["center"][1] for c in cyls) / len(cyls)
            cz = sum(c["center"][2] for c in cyls) / len(cyls)
            cyl_only.append({"r": r, "center": (cx, cy, cz), "used": False, "orig": m})
        else:
            remaining.append(m)

    slots = []
    for i in range(len(cyl_only)):
        if cyl_only[i]["used"]:
            continue
        for j in range(i+1, len(cyl_only)):
            if cyl_only[j]["used"]:
                continue
            a, b = cyl_only[i], cyl_only[j]
            if abs(a["r"] - b["r"]) > 0.05:
                continue
            d = math.dist(a["center"], b["center"])
            if 2*a["r"] < d < 30.0:
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
    """Heuristic shape classification for a cluster of feature faces.
    Returns None for clusters too small/degenerate to be a real hole/slot
    (e.g. a single stray wall face, or a tiny edge-break fillet) -- these are
    reported separately as 'unclassified' so a human can eyeball them.

    Detects: round holes (with hardware_hint for tap/clearance matching),
    countersinks (cone+cylinder combo), chamfers (standalone cone),
    and square/rectangular cutouts."""
    n_planar = sum(1 for m in members if m["kind"] == "planar")
    cyls = [m for m in members if m["kind"] == "cyl"]
    cones = [m for m in members if m["kind"] == "cone"]
    xs = [m["center"][0] for m in members]
    ys = [m["center"][1] for m in members]
    zs = [m["center"][2] for m in members]
    cx, cy, cz = sum(xs)/len(xs), sum(ys)/len(ys), sum(zs)/len(zs)
    spread = (max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs))

    if cones and cyls:
        # Cone + cylinder in same cluster = countersink hole
        radii = [c["radius"] for c in cyls]
        r_avg = sum(radii) / len(radii)
        if r_avg * 2 / 25.4 < 0.08:
            return None
        dia_in = 2 * r_avg / 25.4
        cone_angle = max(c["semi_angle_deg"] for c in cones)
        csink_included = round(2 * cone_angle, 0)
        hint = _hole_hardware_hint(dia_in)
        return {"type": "countersink",
                "diameter_in": round(dia_in, 3),
                "csink_angle_deg": csink_included,
                "center": (cx, cy, cz), "confidence": "high",
                "hardware_hint": hint}

    if cones and not cyls:
        # Standalone cone = edge chamfer (not a hole countersink)
        cone = cones[0]
        if cone["semi_angle_deg"] < 10 or cone["area"] < 1.0:
            return None  # too small / flat -- fillet break, not real chamfer
        return {"type": "chamfer",
                "angle_deg": round(cone["semi_angle_deg"], 1),
                "center": (cx, cy, cz), "confidence": "medium"}

    if cyls:
        radii = [c["radius"] for c in cyls]
        r_avg = sum(radii) / len(radii)
        if r_avg * 2 / 25.4 < 0.08:  # < ~2mm dia -- edge/fillet break
            return None
        dia_in = 2 * r_avg / 25.4
        # Match against standard tap-drill and clearance-hole tables.
        # STEP B-rep does not preserve thread geometry, but matching to
        # known drill sizes gives a strong hint for quoting.
        hint = _hole_hardware_hint(dia_in)
        return {"type": "round",
                "diameter_in": round(dia_in, 3), "center": (cx, cy, cz),
                "confidence": "high", "hardware_hint": hint}
    else:
        xl, yl = spread[0], spread[1]
        if xl < 2.0 or yl < 2.0:  # < ~0.08in -- single stray wall face
            return None
        return {"type": "square_or_rect", "size_in": (round(xl/25.4,3), round(yl/25.4,3)),
                "center": (cx, cy, cz), "confidence": "high"}


# --------------------------------------------------------------------------
# 6. MAIN DRIVER
# --------------------------------------------------------------------------
def run(step_path, density=7.9, k_factor=0.44, out_json="geometry_extract.json"):
    shape, solid = load_step(step_path)
    envelope = get_envelope(solid, density)

    faces, planar, cyl, cone, other = classify_faces(shape)
    thickness_mm, bend_radius_mm, bend_lines = detect_bend_faces(cyl)

    axis_dir = dominant_bend_axis(bend_lines) if bend_lines else (1, 0, 0)
    bb = envelope["bbox_mm"]
    # cut point: center of bounding box along the bend axis's dominant coordinate
    cut_point = ((bb["xmin"]+bb["xmax"])/2, (bb["ymin"]+bb["ymax"])/2, (bb["zmin"]+bb["zmax"])/2)

    edges = cut_cross_section(solid, axis_dir, cut_point)
    edges_info = [edge_2d_info(e, axis_dir) for e in edges]
    chain = walk_closed_loop(edges_info, thickness_mm)
    layout, flat_width_mm = build_flat_layout(chain, bend_radius_mm, thickness_mm, k_factor)

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

    xmin = bb["xmin"]

    # reference "length=0" point: whichever bbox corner gives the minimum
    # projection onto the bend axis (handles any axis sign/orientation)
    corners = [(bb["xmin"] if i&1 else bb["xmax"],
                bb["ymin"] if i&2 else bb["ymax"],
                bb["zmin"] if i&4 else bb["zmax"]) for i in range(8)]

    def project_point_to_axis(p3d):
        return sum(p3d[k]*axis_dir[k] for k in range(3))

    ref_proj = min(project_point_to_axis(c) for c in corners)

    bend_face_idxs = set()
    for b in bend_lines:
        bend_face_idxs.update(b["face_idx"])
    candidates = find_feature_faces(shape, bend_face_idxs)
    clusters = cluster_features(candidates)
    slot_features, remaining_clusters = merge_slot_pairs(clusters)
    classified = [classify_cluster(m) for m in remaining_clusters if len(m) > 0]
    features = slot_features + [c for c in classified if c is not None]
    unclassified_count = sum(1 for c in classified if c is None)

    # attach (length_in, transverse_in) position to every feature
    for feat in features:
        cx, cy, cz = feat["center"]
        length_along_axis = project_point_to_axis((cx, cy, cz))
        feat["length_in"] = round((length_along_axis - ref_proj) / 25.4, 3)
        yz = to_2d((cx, cy, cz))
        t_mm = transverse_pos_mm(yz)
        feat["transverse_in"] = round(t_mm/25.4, 3) if t_mm is not None else None

    result = {
        "source_file": step_path,
        "envelope": envelope,
        "thickness_in": round(thickness_mm/25.4, 4),
        "bend_radius_in": round(bend_radius_mm/25.4, 4) if bend_radius_mm else None,
        "num_bends": len(bend_lines),
        "bend_angles_deg": sorted([round(b["angle_deg"],1) for b in bend_lines]),
        "k_factor_assumed": k_factor,
        "flat_width_in": round(flat_width_mm/25.4, 3),
        "layout_segments": [
            {"kind": s["kind"], "start_in": round(s["start"]/25.4,3), "end_in": round(s["end"]/25.4,3),
             **({"angle_deg": s["angle_deg"]} if s["kind"]=="bend" else {})}
            for s in layout
        ],
        "features_raw_count": len(features),
        "features_unclassified_count": unclassified_count,
        "features": features,
    }
    with open(out_json, "w") as f:
        json.dump(result, f, indent=2, default=str)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step_file")
    ap.add_argument("--density", type=float, default=7.9, help="g/cm3, default 7.9 (stainless)")
    ap.add_argument("--k", type=float, default=0.44, help="bend-allowance K-factor")
    ap.add_argument("--out", default="geometry_extract.json")
    args = ap.parse_args()
    res = run(args.step_file, args.density, args.k, args.out)
    print(json.dumps(res, indent=2, default=str))
