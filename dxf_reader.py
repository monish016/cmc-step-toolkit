"""
dxf_reader.py - dependency-free reader for ASCII DXF files
===========================================================
Extracts what quoting needs from a DXF (typically a laser flat pattern or a
2D drawing):

  * cut geometry: LINE, ARC, CIRCLE, LWPOLYLINE/POLYLINE (incl. bulge arcs),
    ELLIPSE, SPLINE (approx.), and block INSERTs (translate/scale/rotate)
  * exact cut length, bounding box, holes (circles grouped by diameter)
  * bend lines (entities on a BEND layer or with a dashed/centre linetype)
  * TEXT / MTEXT / DIMENSION strings so the text extractor can read title blocks

Units come from $INSUNITS (1 = inch, 4 = mm); unitless files are guessed from size.
All lengths returned in inches.
"""

import math
import re

_NON_CUT_LAYERS = re.compile(r'DIM|TEXT|NOTE|BORDER|TITLE|FRAME|SHEET|ANNO|HATCH|CENTER|CENTRE|HIDDEN|'
                             r'BEND|ETCH|MARK|SCRIBE|DEFPOINTS|VIEWPORT', re.IGNORECASE)
_BEND_LAYERS = re.compile(r'BEND|FOLD|FORM', re.IGNORECASE)
_NON_CUT_LTYPES = re.compile(r'CENTER|CENTRE|DASH|HIDDEN|PHANTOM|DOT|DIVIDE', re.IGNORECASE)
_UNIT_TO_IN = {1: 1.0, 2: 12.0, 4: 1 / 25.4, 5: 1 / 2.54, 6: 1000 / 25.4, 8: 1e-6, 9: 0.001}


def _pairs(text):
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip())
        except ValueError:
            continue
        yield code, lines[i + 1].strip()


def _parse(text):
    """Return (header, blocks{name: [entities]}, entities[]). Entity = (type, [(code, value), ...])."""
    header, blocks, entities = {}, {}, []
    section = None          # current SECTION name
    expect_section_name = False
    cur = None              # entity being collected
    block_name = None       # current block (BLOCKS section)
    in_block_header = False
    last_var = None

    def finish():
        if cur is None:
            return
        if section == "ENTITIES":
            entities.append(cur)
        elif section == "BLOCKS" and block_name:
            blocks.setdefault(block_name, []).append(cur)

    for code, val in _pairs(text):
        if code == 0:
            finish()
            cur = None
            if val == "SECTION":
                expect_section_name = True
                continue
            if val == "ENDSEC":
                section = None
                continue
            if val == "EOF":
                break
            if section == "BLOCKS" and val == "BLOCK":
                block_name, in_block_header = None, True
                continue
            if section == "BLOCKS" and val == "ENDBLK":
                block_name, in_block_header = None, False
                continue
            if section in ("ENTITIES", "BLOCKS"):
                in_block_header = False
                cur = (val, [])
            continue
        if expect_section_name and code == 2:
            section, expect_section_name = val, False
            continue
        if section == "HEADER":
            if code == 9:
                last_var = val
            elif last_var and last_var not in header:
                header[last_var] = val
            continue
        if in_block_header:
            if code == 2 and block_name is None:
                block_name = val
            continue
        if cur is not None:
            cur[1].append((code, val))
    finish()
    return header, blocks, entities


def _g(pairs, code, default=None, cast=float):
    for c, v in pairs:
        if c == code:
            try:
                return cast(v)
            except ValueError:
                return default
    return default


def _gall(pairs, code, cast=float):
    out = []
    for c, v in pairs:
        if c == code:
            try:
                out.append(cast(v))
            except ValueError:
                pass
    return out


class _Xf:
    """2D affine transform for block inserts."""
    def __init__(self, dx=0.0, dy=0.0, sx=1.0, sy=1.0, rot_deg=0.0, parent=None):
        self.dx, self.dy, self.sx, self.sy = dx, dy, sx, sy
        self.c, self.s = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
        self.parent = parent

    def pt(self, x, y):
        x, y = x * self.sx, y * self.sy
        x, y = x * self.c - y * self.s + self.dx, x * self.s + y * self.c + self.dy
        return self.parent.pt(x, y) if self.parent else (x, y)

    def scale(self):
        s = (abs(self.sx) + abs(self.sy)) / 2
        return s * (self.parent.scale() if self.parent else 1.0)


def _poly_len(pts, bulges, closed):
    total = 0.0
    n = len(pts)
    segs = n if closed else n - 1
    for i in range(max(segs, 0)):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        chord = math.hypot(x2 - x1, y2 - y1)
        b = bulges[i] if i < len(bulges) else 0.0
        if abs(b) > 1e-9 and chord > 0:
            theta = 4 * math.atan(abs(b))
            total += chord * theta / (2 * math.sin(theta / 2))
        else:
            total += chord
    return total


def read_dxf(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:22].startswith(b"AutoCAD Binary DXF"):
        return {"error": "Binary DXF is not supported - re-save the file as ASCII DXF."}
    text = raw.decode("utf-8", errors="replace")
    header, blocks, entities = _parse(text)

    try:
        ins = int(float(header.get("$INSUNITS", 0)))
    except ValueError:
        ins = 0

    geo = {"cut_len": 0.0, "xs": [], "ys": [], "circles": [], "bend_lines": 0,
           "texts": [], "dims": [], "entity_counts": {}}

    def add_pt(x, y):
        geo["xs"].append(x)
        geo["ys"].append(y)

    def walk(ents, xf, depth=0):
        for etype, pairs in ents:
            geo["entity_counts"][etype] = geo["entity_counts"].get(etype, 0) + 1
            layer = _g(pairs, 8, "", str) or ""
            ltype = _g(pairs, 6, "", str) or ""
            if etype == "INSERT" and depth < 8:
                name = _g(pairs, 2, "", str)
                sub = _Xf(_g(pairs, 10, 0.0), _g(pairs, 20, 0.0), _g(pairs, 41, 1.0) or 1.0,
                          _g(pairs, 42, 1.0) or 1.0, _g(pairs, 50, 0.0), xf)
                if name in blocks:
                    walk(blocks[name], sub, depth + 1)
                continue
            if etype in ("TEXT", "MTEXT", "ATTRIB"):
                parts = [v for c, v in pairs if c in (1, 3)]
                t = "".join(parts)
                t = re.sub(r'\\[A-Za-z][^;]*;|\\P|[{}]', ' ', t)
                t = t.replace('%%C', 'Ø').replace('%%c', 'Ø').replace('%%D', '°').replace('%%d', '°') \
                     .replace('%%P', '±').replace('%%p', '±')
                if t.strip():
                    geo["texts"].append(t.strip())
                continue
            if etype == "DIMENSION":
                meas = _g(pairs, 42)
                override = _g(pairs, 1, "", str)
                if meas is not None:
                    geo["dims"].append(meas * xf.scale())
                if override and override not in ("<>", " "):
                    geo["texts"].append(override.replace("<>", "").replace("%%C", "Ø").strip())
                continue
            is_bend = bool(_BEND_LAYERS.search(layer))
            non_cut = bool(_NON_CUT_LAYERS.search(layer)) or bool(_NON_CUT_LTYPES.search(ltype))
            if etype == "LINE":
                p1 = xf.pt(_g(pairs, 10, 0.0), _g(pairs, 20, 0.0))
                p2 = xf.pt(_g(pairs, 11, 0.0), _g(pairs, 21, 0.0))
                if is_bend or (non_cut and _NON_CUT_LTYPES.search(ltype) and not _NON_CUT_LAYERS.search(layer)):
                    geo["bend_lines"] += 1 if is_bend else 0
                    continue
                if non_cut:
                    continue
                geo["cut_len"] += math.hypot(p2[0] - p1[0], p2[1] - p1[1])
                add_pt(*p1)
                add_pt(*p2)
            elif etype in ("CIRCLE", "ARC"):
                if non_cut:
                    continue
                lx, ly = _g(pairs, 10, 0.0), _g(pairs, 20, 0.0)
                lr = _g(pairs, 40, 0.0) or 0.0
                r = lr * xf.scale()
                if etype == "CIRCLE":
                    cx, cy = xf.pt(lx, ly)
                    geo["cut_len"] += 2 * math.pi * r
                    geo["circles"].append(2 * r)
                    add_pt(cx - r, cy - r)
                    add_pt(cx + r, cy + r)
                else:
                    a0, a1 = _g(pairs, 50, 0.0), _g(pairs, 51, 360.0)
                    sweep = (a1 - a0) % 360 or 360
                    geo["cut_len"] += math.radians(sweep) * r
                    for k in range(0, 9):   # sample in local coords, then transform
                        a = math.radians(a0 + sweep * k / 8)
                        add_pt(*xf.pt(lx + lr * math.cos(a), ly + lr * math.sin(a)))
            elif etype == "LWPOLYLINE":
                if non_cut:
                    continue
                xs, ys = _gall(pairs, 10), _gall(pairs, 20)
                # bulges (42) are per-vertex and optional; rebuild them in vertex order
                bulges, vi = [], -1
                for c, v in pairs:
                    if c == 10:
                        vi += 1
                        bulges.append(0.0)
                    elif c == 42 and vi >= 0:
                        try:
                            bulges[vi] = float(v)
                        except ValueError:
                            pass
                pts = [xf.pt(x, y) for x, y in zip(xs, ys)]
                closed = bool(int(_g(pairs, 70, 0.0) or 0) & 1)
                geo["cut_len"] += _poly_len(pts, bulges, closed)
                for p in pts:
                    add_pt(*p)
            elif etype == "ELLIPSE":
                if non_cut:
                    continue
                cx, cy = _g(pairs, 10, 0.0), _g(pairs, 20, 0.0)
                mx, my = _g(pairs, 11, 0.0), _g(pairs, 21, 0.0)
                ratio = _g(pairs, 40, 1.0)
                a = math.hypot(mx, my) * xf.scale()
                b = a * ratio
                frac = ((_g(pairs, 42, 2 * math.pi) - _g(pairs, 41, 0.0)) % (2 * math.pi) or 2 * math.pi) / (2 * math.pi)
                h = ((a - b) / (a + b)) ** 2 if a + b else 0
                geo["cut_len"] += math.pi * (a + b) * (1 + 3 * h / (10 + math.sqrt(4 - 3 * h))) * frac
                p = xf.pt(cx, cy)
                add_pt(p[0] - a, p[1] - a)
                add_pt(p[0] + a, p[1] + a)
            elif etype == "SPLINE":
                if non_cut:
                    continue
                xs, ys = _gall(pairs, 11) or _gall(pairs, 10), _gall(pairs, 21) or _gall(pairs, 20)
                pts = [xf.pt(x, y) for x, y in zip(xs, ys)]
                geo["cut_len"] += _poly_len(pts, [], False)
                for p in pts:
                    add_pt(*p)

    walk(entities, _Xf())

    # Old-style POLYLINE / VERTEX / SEQEND sequences
    i = 0
    while i < len(entities):
        etype, pairs = entities[i]
        if etype == "POLYLINE":
            layer = _g(pairs, 8, "", str) or ""
            closed = bool(int(_g(pairs, 70, 0.0) or 0) & 1)
            pts, bulges = [], []
            j = i + 1
            while j < len(entities) and entities[j][0] == "VERTEX":
                vp = entities[j][1]
                pts.append((_g(vp, 10, 0.0), _g(vp, 20, 0.0)))
                bulges.append(_g(vp, 42, 0.0) or 0.0)
                j += 1
            if not _NON_CUT_LAYERS.search(layer):
                geo["cut_len"] += _poly_len(pts, bulges, closed)
                for p in pts:
                    add_pt(*p)
            i = j
        i += 1

    # Units: header first, else guess from size (a >200-unit part is almost surely mm)
    if ins in _UNIT_TO_IN:
        k, units = _UNIT_TO_IN[ins], ("mm" if ins == 4 else "in")
    else:
        span = max((max(geo["xs"]) - min(geo["xs"])) if geo["xs"] else 0,
                   (max(geo["ys"]) - min(geo["ys"])) if geo["ys"] else 0)
        k, units = (1 / 25.4, "mm") if span > 200 else (1.0, "in")

    holes = {}
    for d in geo["circles"]:
        key = round(d * k, 4)
        holes[key] = holes.get(key, 0) + 1
    bbox = None
    if geo["xs"]:
        w = (max(geo["xs"]) - min(geo["xs"])) * k
        h = (max(geo["ys"]) - min(geo["ys"])) * k
        bbox = {"length_in": round(max(w, h), 3), "width_in": round(min(w, h), 3)}
    return {
        "units": units,
        "insunits": ins,
        "cut_length_in": round(geo["cut_len"] * k, 2),
        "bbox_in": bbox,
        "holes_in": [{"diameter_in": d, "count": n} for d, n in sorted(holes.items())],
        "bend_lines": geo["bend_lines"],
        "texts": geo["texts"],
        "dims_in": [round(d * k, 4) for d in geo["dims"]],
        "entity_counts": geo["entity_counts"],
    }
