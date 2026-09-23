"""
drawing_layout.py - coordinate-aware parsing of CAD-exported PDF drawings
=========================================================================
Plain text extraction loses *where* text sits on the sheet, and different PDF
libraries return the same words in different orders.  This module works from
word bounding boxes + font sizes instead, which lets us:

  * read title-block cells by position ("value in the FINISH cell"), using the
    fact that title-block labels are printed small (~5-7 pt) and the filled-in
    values larger (>= 8 pt);
  * pair a nominal dimension with its stacked limit deviations
    (e.g. "1.000 F7" with "-.001" printed above and "-.002" below it).

Works with PyMuPDF (production) and falls back to pdfplumber (local testing).
All functions are defensive: any failure returns empty results so the
regex-based extractor keeps working.
"""

import re

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None


# ── Word extraction ────────────────────────────────────────────────────

def _words_from_chars(chars):
    """Build words from characters given in content-stream order.

    Each char: {"c", "x0", "x1", "oy" (baseline y), "size", "upright"}.
    PyMuPDF only splits words at space characters, so text that is merely
    *positioned* apart (stacked limits like "F7" / "-.001", or cells packed into
    one text object) would otherwise merge.  We also split on horizontal gaps,
    backward jumps and baseline changes.  Word boxes are derived from the
    baseline and font size (height = size), matching pdfplumber's geometry.
    """
    words = []
    cur = []

    def flush():
        if not cur:
            return
        size = max(ch["size"] for ch in cur)
        oy = sum(ch["oy"] for ch in cur) / len(cur)
        words.append({"text": "".join(ch["c"] for ch in cur),
                      "x0": min(ch["x0"] for ch in cur), "x1": max(ch["x1"] for ch in cur),
                      "top": oy - 0.8 * size, "bottom": oy + 0.2 * size,
                      "size": size, "upright": cur[0]["upright"]})
        cur.clear()

    # Rotated text: keep stream order (only used for the upright checks)
    for ch in chars:
        if ch["upright"] or ch["size"] <= 0:
            continue
        if ch["c"].isspace():
            flush()
        else:
            if cur and abs(ch["size"] - cur[-1]["size"]) > 0.6:
                flush()
            cur.append(ch)
    flush()

    # Upright text: group by baseline + font size geometrically, then read left to right.
    ups = [ch for ch in chars if ch["upright"] and ch["size"] > 0 and ch["x1"] > ch["x0"] - 0.01
           and not (ch["x0"] == 0 and ch["x1"] == 0)]
    ups.sort(key=lambda ch: (ch["oy"], ch["x0"]))
    rows = []
    for ch in ups:
        placed = False
        for row in rows[-6:]:
            if abs(row["oy"] - ch["oy"]) <= 0.25 * max(row["size"], ch["size"]) and \
                    abs(row["size"] - ch["size"]) <= 0.6:
                row["chars"].append(ch)
                placed = True
                break
        if not placed:
            rows.append({"oy": ch["oy"], "size": ch["size"], "chars": [ch]})
    for row in rows:
        row["chars"].sort(key=lambda ch: ch["x0"])
        for ch in row["chars"]:
            if ch["c"].isspace():
                flush()
                continue
            if cur:
                prev = cur[-1]
                sz = max(prev["size"], ch["size"], 0.1)
                gap = ch["x0"] - prev["x1"]
                if gap > 0.25 * sz or gap < -0.4 * sz or abs(ch["size"] - prev["size"]) > 0.2:
                    flush()
            cur.append(ch)
        flush()
    return words


def _words_from_fitz(pdf_path):
    pages = []
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            chars = []
            raw = page.get_text("rawdict")
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    ldir = line.get("dir", (1, 0))
                    upright = abs(ldir[0] - 1.0) < 0.01 and abs(ldir[1]) < 0.01
                    for span in line.get("spans", []):
                        size = float(span.get("size", 0))
                        for ch in span.get("chars", []):
                            x0, y0, x1, y1 = ch["bbox"]
                            oy = ch.get("origin", (x0, y1))[1]
                            chars.append({"c": ch.get("c", ""), "x0": x0, "x1": x1, "oy": oy,
                                          "size": size, "upright": upright})
                        # span boundary = word boundary
                        chars.append({"c": " ", "x0": 0, "x1": 0, "oy": 0, "size": size, "upright": upright})
                    chars.append({"c": " ", "x0": 0, "x1": 0, "oy": 0, "size": 0, "upright": upright})
            pages.append(_words_from_chars(chars))
    finally:
        doc.close()
    return pages


def _words_from_pdfplumber(pdf_path):
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in pdf.pages:
            ws = p.extract_words(extra_attrs=["size", "upright"])
            pages.append([{"text": w["text"], "x0": w["x0"], "top": w["top"], "x1": w["x1"],
                           "bottom": w["bottom"], "size": float(w.get("size", 0)),
                           "upright": bool(w.get("upright", True))} for w in ws])
    return pages


def get_page_words(pdf_path):
    """Return a list (one per page) of word dicts with bbox, font size and orientation."""
    try:
        if fitz is not None:
            return _words_from_fitz(pdf_path)
    except Exception:
        pass
    try:
        return _words_from_pdfplumber(pdf_path)
    except Exception:
        return []


# ── Helpers ────────────────────────────────────────────────────────────

def _h(w):
    return max(0.1, w["bottom"] - w["top"])


def _cy(w):
    return (w["top"] + w["bottom"]) / 2


def _norm(t):
    return re.sub(r'[^A-Z0-9#]', '', t.upper())


def _same_row(a, b, tol=0.5):
    return abs(_cy(a) - _cy(b)) <= tol * max(_h(a), _h(b))


# Every word that is a title-block *label* (never a value)
_LABEL_VOCAB = {
    "UNLESS", "OTHERWISE", "SPECIFIED", "NAME", "DATE", "DRAWN", "DIMENSIONS", "ARE", "IN",
    "INCHES", "MILLIMETERS", "TOLERANCES", "CHECKED", "FRACTIONAL", "ANGULAR", "MACH", "BEND",
    "ENG", "APPR", "TWO", "THREE", "PLACE", "DECIMAL", "MFG", "QA", "INTERPRET", "GEOMETRIC",
    "TOLERANCING", "PER", "TITLE", "COMMENTS", "MATERIAL", "FINISH", "SIZE", "DWG", "NO", "REV",
    "SCALE", "WEIGHT", "SHEET", "OF", "NEXT", "ASSY", "USED", "ON", "APPLICATION", "DO", "NOT",
    "DRAWING", "PROPRIETARY", "CONFIDENTIAL", "JOB", "PART", "QTY", "REQUIRED", "DESCRIPTION",
    "BY", "ANGLES", "PROJECTION", "THIRD", "FIRST", "ANGLE",
    # legal / boilerplate text printed in title blocks
    "CHICAGO", "METALCRAFT", "PROPERTY", "INFORMATION", "CONTAINED", "THIS", "THE", "IS",
    "PROHIBITED", "WITHOUT", "WRITTEN", "PERMISSION", "REPRODUCTION", "WHOLE", "ANY", "SOLE",
    "AS", "OR", "AND", "COPYRIGHT", "INC",
}

# field -> list of label token sequences (normalised, punctuation stripped)
TITLE_FIELDS = {
    "material": [["MATERIAL"], ["MATL"]],
    "finish": [["FINISH"], ["SURFACE", "FINISH"]],
    "title": [["TITLE"]],
    "dwg_no": [["DWG", "NO"], ["DRAWING", "NO"], ["DRAWING", "NUMBER"]],
    "part": [["PART:"], ["PART", "NO"], ["PART", "NUMBER"], ["P/N"]],
    "rev": [["REV"], ["REVISION"]],
    "qty": [["QTY", "REQUIRED"], ["QTY", "REQD"], ["QTY"], ["QUANTITY"]],
    "size": [["SIZE"]],
    "weight": [["WEIGHT"]],
}


def _find_label_seqs(words, seq):
    """Find runs of words matching a token sequence on one row."""
    hits = []

    def tok_ok(word, tok):
        if tok.endswith(":"):
            return _norm(word["text"]) == _norm(tok) and word["text"].strip().endswith(":")
        return _norm(word["text"]) == _norm(tok)

    for i, w in enumerate(words):
        if not w["upright"] or not tok_ok(w, seq[0]):
            continue
        run = [w]
        last = w
        ok = True
        for tok in seq[1:]:
            nxt = None
            for c in words:
                if c is last or not c["upright"]:
                    continue
                if tok_ok(c, tok) and _same_row(last, c) and 0 <= c["x0"] - last["x1"] <= 2.0 * _h(last):
                    nxt = c
                    break
            if nxt is None:
                ok = False
                break
            run.append(nxt)
            last = nxt
        if ok and not run[-1]["text"].strip().endswith(":") and _in_prose(words, run):
            ok = False  # e.g. "...AND/OR MATERIAL COSTS" - the word is part of a sentence
        if ok:
            box = {"x0": min(r["x0"] for r in run), "x1": max(r["x1"] for r in run),
                   "top": min(r["top"] for r in run), "bottom": max(r["bottom"] for r in run),
                   "size": run[0]["size"], "upright": True, "text": " ".join(r["text"] for r in run),
                   "_ids": {id(r) for r in run}}
            hits.append(box)
    return hits


def _in_prose(words, run):
    """True if the label words sit inside running text of the same font size."""
    first, last = run[0], run[-1]
    ids = {id(r) for r in run}
    for c in words:
        if id(c) in ids or not c["upright"] or abs(c["size"] - first["size"]) > 0.3:
            continue
        if not _same_row(first, c):
            continue
        t = c["text"].strip()
        if re.fullmatch(r'[\d.:]+', t) or _norm(t) in _LABEL_VOCAB:
            continue
        gap_r = c["x0"] - last["x1"]
        gap_l = first["x0"] - c["x1"]
        if 0 <= gap_r <= 1.5 * _h(first) or 0 <= gap_l <= 1.5 * _h(first):
            return True
    return False


def _collect_line(words, first, used, max_gap_factor=1.2, grow_left=False):
    """Grow a value to the right (and optionally left) from `first` along its row."""
    line = [first]
    used.add(id(first))
    while grow_left:
        head = line[0]
        prv, best = None, None
        for c in words:
            if id(c) in used or not c["upright"] or _norm(c["text"]) in _LABEL_VOCAB:
                continue
            if abs(c["size"] - first["size"]) > 0.6 or not _same_row(head, c):
                continue
            gap = head["x0"] - c["x1"]
            if -1 <= gap <= max_gap_factor * _h(head) and (best is None or gap < best):
                best, prv = gap, c
        if prv is None:
            break
        line.insert(0, prv)
        used.add(id(prv))
    while True:
        last = line[-1]
        nxt = None
        best = None
        for c in words:
            if id(c) in used or not c["upright"]:
                continue
            if abs(c["size"] - first["size"]) > 0.6 or not _same_row(last, c):
                continue
            gap = c["x0"] - last["x1"]
            if -1 <= gap <= max_gap_factor * _h(last) and (best is None or gap < best):
                best, nxt = gap, c
        if nxt is None:
            break
        line.append(nxt)
        used.add(id(nxt))
    return line


_SINGLE_CHAR_OK = {"rev", "size", "qty"}


def _is_value_word(w, label, field, words):
    if not w["upright"]:
        return False
    t = _norm(w["text"])
    if not t or t in _LABEL_VOCAB:
        return False
    # values are never printed smaller than their label
    if w["size"] < label["size"] * 0.95:
        return False
    # single characters are usually sheet-border zone markers
    if len(t) == 1 and field not in _SINGLE_CHAR_OK:
        return False
    # if a *different* label sits immediately left of this word, it belongs to that label
    for c in words:
        if c is w or id(c) in label.get("_ids", ()) or not c["upright"]:
            continue
        if _norm(c["text"]) in _LABEL_VOCAB and _same_row(c, w) and 0 <= w["x0"] - c["x1"] <= 2.0 * _h(w):
            return False
    return True


def _label_between(words, L, w, label_ids):
    """True if another label word sits on the label's row between the label and w."""
    for c in words:
        if id(c) in label_ids or not c["upright"]:
            continue
        if _norm(c["text"]) in _LABEL_VOCAB and _same_row(c, L) and L["x1"] <= c["x0"] < w["x0"]:
            return True
    return False


def read_title_block(words):
    """Return {field: value_text} for title-block fields found by position."""
    out = {}
    if not words:
        return out
    label_ids = set()
    label_boxes = {}
    for field, seqs in TITLE_FIELDS.items():
        # prefer the longest matching label sequence
        for seq in sorted(seqs, key=len, reverse=True):
            hits = _find_label_seqs(words, seq)
            if hits:
                label_boxes[field] = hits
                for hbox in hits:
                    label_ids |= hbox["_ids"]
                break

    for field, hits in label_boxes.items():
        best_val, best_score = None, None
        for L in hits:
            lh = _h(L)
            for w in words:
                if id(w) in label_ids:
                    continue
                dx = w["x0"] - L["x1"]
                dy_c = _cy(w) - _cy(L)
                # Zone 1: to the right, same row or slightly lower (value baseline offset)
                right = (-2 <= dx <= 110) and (-0.6 * lh <= dy_c <= 1.8 * lh + 2)
                # Zone 2: below the label, starting near the label's left edge
                below = (L["bottom"] - 2 <= w["top"] <= L["bottom"] + 2.5 * lh + 8) and \
                        (L["x0"] - 12 <= w["x0"] <= L["x0"] + 90)
                if not (right or below):
                    continue
                if not _is_value_word(w, L, field, words):
                    continue
                if right and _label_between(words, L, w, L["_ids"]):
                    continue
                score = (abs(dx) if right else abs(w["x0"] - L["x0"])) + 1.5 * abs(dy_c)
                if best_score is None or score < best_score:
                    best_score, best_val = score, w
        if best_val is None:
            continue
        used = set(label_ids)
        line = _collect_line(words, best_val, used, grow_left=True)
        text = " ".join(w["text"] for w in line)
        # Multi-line values (e.g. a two-line TITLE): next row directly below, same size & x
        prev = line
        for _ in range(2):
            nxt_first = None
            for c in words:
                if id(c) in used or not c["upright"] or abs(c["size"] - best_val["size"]) > 0.6:
                    continue
                if 0.5 * _h(prev[0]) <= c["top"] - prev[0]["top"] <= 1.6 * _h(prev[0]) and abs(c["x0"] - line[0]["x0"]) <= 3:
                    nxt_first = c
                    break
            if nxt_first is None:
                break
            prev = _collect_line(words, nxt_first, used)
            text += " " + " ".join(w["text"] for w in prev)
        out[field] = text.strip()
    return out


# ── Stacked limit deviations & fits ────────────────────────────────────

_NUM_RE = re.compile(r'^\d*\.\d+$|^\d+\.\d*$')
_DEV_RE = re.compile(r'^([+\-±])\s*(\d*\.\d{2,4}|0)$')
_FIT_TOKEN = re.compile(
    r'^(CD|EF|FG|JS|Z[ABC]|[A-HJKMNP-VX-Z]|cd|ef|fg|js|z[abc]|[a-hjkmnp-vx-z])(\d{1,2})$')


def find_stacked_tolerances(words):
    """Find nominal dimensions with limit deviations printed next to them.

    Returns a list of dicts:
      {nominal_in, fit_class or None, upper_in, lower_in, raw}
    """
    results = []
    if not words:
        return results
    up = [w for w in words if w["upright"]]
    devs = []
    for w in up:
        m = _DEV_RE.match(w["text"].strip())
        if m:
            sign, val = m.group(1), m.group(2)
            try:
                v = float(val)
            except ValueError:
                continue
            devs.append((w, sign, v))

    for w in up:
        t = w["text"].strip()
        if not _NUM_RE.match(t) or t.startswith(('+', '-')):
            continue
        try:
            nominal = float(t)
        except ValueError:
            continue
        anchor = w
        fit = None
        # optional fit class immediately to the right ("1.000 F7")
        for c in up:
            if c is w or not _same_row(w, c):
                continue
            if 0 <= c["x0"] - w["x1"] <= 1.2 * _h(w) and _FIT_TOKEN.match(c["text"].strip()):
                fit, anchor = c["text"].strip(), c
                break
        lh = _h(anchor)
        near = []
        for d, sign, v in devs:
            dx = d["x0"] - anchor["x1"]
            dy = _cy(d) - _cy(anchor)
            if -1 <= dx <= 2.5 * lh and abs(dy) <= 1.1 * lh and d["size"] <= anchor["size"] * 1.15:
                near.append((dy, d, sign, v))
        if not near:
            continue
        near.sort(key=lambda x: x[0])
        if len(near) == 1 and near[0][2] == "±":
            upper, lower = near[0][3], -near[0][3]
        elif len(near) >= 2:
            (dy1, d1, s1, v1), (dy2, d2, s2, v2) = near[0], near[-1]
            val1 = -v1 if s1 == "-" else v1
            val2 = -v2 if s2 == "-" else v2
            upper, lower = max(val1, val2), min(val1, val2)
        else:
            continue
        raw = t + (f" {fit}" if fit else "") + f" ({_fmt(upper)}/{_fmt(lower)})"
        results.append({"nominal_in": round(nominal, 4), "fit_class": fit,
                        "upper_in": round(upper, 4), "lower_in": round(lower, 4), "raw": raw})
    # dedupe identical callouts (same dimension shown in two views)
    seen, uniq = set(), []
    for r in results:
        k = (r["nominal_in"], r["fit_class"], r["upper_in"], r["lower_in"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def _fmt(v):
    s = f"{abs(v):.4f}".rstrip("0")
    if s.endswith("."):
        s += "000"
    if s.startswith("0."):
        s = s[1:]
    while len(s.split(".")[1]) < 3:
        s += "0"
    return ("+" if v >= 0 else "-") + s


# ── Part envelope / flat size estimate ─────────────────────────────────

_DIM_NUM = re.compile(r'^\d{1,4}[.,]\d{1,3}$')
_FLAT_VIEW_RE = re.compile(r'\b(?:UP|DOWN|DN)\s*\d{1,3}\s*°|\bFLAT\s+PATTERN\b|\bBEND\s+LINE', re.IGNORECASE)


def estimate_envelope(words, text="", units="in"):
    """Estimate the part's two largest overall dimensions from dimension text.

    Returns {"length_in", "width_in", "is_flat_view", "candidates_in"} or {}.
    Dimension text is the largest numeric text on a CAD drawing; title-block and
    tolerance-table numbers are printed much smaller and are ignored, as are
    hole/radius callouts and signed limit deviations.
    """
    if not words:
        return {}
    nums = []
    for w in words:
        t = w["text"].strip()
        if not _DIM_NUM.match(t):
            continue
        nums.append(w)
    if not nums:
        return {}
    max_size = max(w["size"] for w in nums)
    cands = []
    for w in nums:
        if w["size"] < 0.7 * max_size:
            continue
        # skip hole/radius callouts: a diameter/radius symbol or THRU right beside it
        skip = False
        for c in words:
            if c is w or c["upright"] != w["upright"]:
                continue
            ct = c["text"].strip().upper()
            if w["upright"] and _same_row(c, w):
                gap_l = w["x0"] - c["x1"]
                gap_r = c["x0"] - w["x1"]
                if 0 <= gap_l <= 1.5 * _h(w) and ct in ("Ø", "∅", "⌀", "R", "X", "X∅", "XØ"):
                    skip = True
                if 0 <= gap_r <= 1.5 * _h(w) and ct.startswith(("THRU", "DP", "DEEP", "X")):
                    skip = True
            if skip:
                break
        if skip:
            continue
        v = float(w["text"].replace(",", "."))
        if units == "mm":
            v = v / 25.4
        if 0.25 <= v <= 160:
            cands.append(round(v, 3))
    if not cands:
        return {}
    uniq = sorted(set(cands), reverse=True)
    length = uniq[0]
    width = next((u for u in uniq[1:] if u < length - 0.05), None)
    return {
        "length_in": length,
        "width_in": width,
        "is_flat_view": bool(_FLAT_VIEW_RE.search(text or "")),
        "candidates_in": uniq[:6],
    }
