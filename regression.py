"""
regression.py - regression checks for the CMC Quoting Toolkit
==============================================================
Every file Shane (or anyone) reports a problem with becomes a fixture: we store
the file's SHA-256 and the values the analysis must produce.  Files are matched
by hash, so no customer file names or drawings are stored in the repo.

Two ways to run:
  * In the app:  open /regression, drop the fixture files, press Run.
    (Runs through the real production pipeline, incl. STEP analysis.)
  * CLI:  python regression.py FILE [FILE ...]            (local, PDFs + STEP if available)
          python regression.py --url https://...app FILE  (post to a deployed app)

Adding a fixture: run the file once, copy the "summary" JSON the page prints for
an unknown file into regression_expected.json, keep only the keys you care
about, and mark status "verified" once a person has confirmed the numbers.
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED_PATH = os.path.join(HERE, "regression_expected.json")


def load_expected(path=EXPECTED_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def part_hash(s):
    return hashlib.sha256((s or "").strip().upper().encode()).hexdigest()[:16]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Summaries (the only fields checks look at) ─────────────────────────

def summarize_step(g):
    s = {
        "fab": g.get("fab_type"),
        "thk": g.get("thickness_in"),
        "gauge": g.get("gauge"),
        "bends": g.get("num_bends"),
        "angles": sorted(int(round(a)) for a in (g.get("bend_angles_deg") or [])),
        "flat_w": g.get("flat_width_in"),
        "flat_l": g.get("flat_length_in"),
    }
    total = 0
    for f in g.get("features") or []:
        k = "f_" + str(f.get("type"))
        n = int(f.get("count", 1) or 1)
        s[k] = s.get(k, 0) + n
        total += n
    s["f_total"] = total
    if g.get("machining_type"):
        s["machining_type"] = g["machining_type"]
    return s


def summarize_drawing(d):
    cf = d.get("_computed_flat") or {}
    pi = d.get("part_info") or {}
    st = d.get("machined_stock") or {}
    thk = (d.get("thickness") or [None])[0] if d.get("thickness") else None
    holes = tapped = 0
    for f in d.get("features") or []:
        n = int(f.get("count", 1) or 1)
        if f.get("type") in ("round_hole", "counterbored_hole", "countersunk_hole"):
            holes += n
        elif f.get("type") == "tapped_hole":
            tapped += n
    fins = [f.get("finish") for f in (d.get("finishes") or [])]
    bends = cf.get("num_bends")
    if bends is None:
        bends = len((d.get("bends") or {}).get("angles", []))
    return {
        "fab": d.get("likely_fab_type"),
        "thk": thk.get("value_in") if thk else None,
        "gauge": thk.get("gauge") if thk else None,
        "bends": bends,
        "qty": pi.get("quantity"),
        "rev": pi.get("revision"),
        "part_h": part_hash(pi.get("part_number")) if pi.get("part_number") else None,
        "mat": d.get("material_callout"),
        "finish0": fins[0] if fins else None,
        "fits": [t.get("raw") for t in (d.get("tolerances") or []) if t.get("type") in ("fit", "limit")],
        "od": st.get("od_in"),
        "len": st.get("overall_length_in"),
        "tight": d.get("tightest_tolerance_in"),
        "holes": holes,
        "tapped": tapped,
        "pages": d.get("drawing_page_count"),
        "units": d.get("units"),
        "cut": d.get("dxf_cut_length_in"),
        "flat_l": (d.get("_computed_flat") or {}).get("flat_length_in"),
        "flat_w": (d.get("_computed_flat") or {}).get("flat_width_in"),
        "priced": bool(d.get("cost_estimate")),
    }


def summarize_response(resp):
    """Summarise an /analyze JSON response (or an error)."""
    if not isinstance(resp, dict):
        return {"error": str(resp)}
    if resp.get("error"):
        return {"error": str(resp["error"])}
    if resp.get("drawing_data"):
        return summarize_drawing(resp["drawing_data"])
    if resp.get("geometry"):
        return summarize_step(resp["geometry"])
    return {"error": "unrecognised response"}


# ── Comparison ─────────────────────────────────────────────────────────

def _num_ok(a, b, tol):
    try:
        return abs(float(a) - float(b)) <= max(tol.get("abs", 0.001), tol.get("rel", 0.003) * abs(float(b)))
    except (TypeError, ValueError):
        return False


def _eq(actual, expected, tol):
    if expected is None:
        return actual is None or actual == [] or actual == "" or actual == {}
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        return actual is not None and _num_ok(actual, expected, tol)
    if isinstance(expected, str):
        return isinstance(actual, str) and actual.strip().upper() == expected.strip().upper()
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and \
            all(_eq(a, e, tol) for a, e in zip(actual, expected))
    return actual == expected


def check(sha, summary, expected=None):
    exp = expected or load_expected()
    fx = exp.get("fixtures", {}).get(sha)
    if not fx:
        return {"sha": sha, "known": False, "summary": summary}
    tol = exp.get("tolerance", {"abs": 0.001, "rel": 0.003})
    rows = []
    for key, want in fx["checks"].items():
        if key == "error_contains":
            got = summary.get("error")
            ok = bool(got) and want.lower() in got.lower()
        else:
            got = summary.get(key, 0 if key.startswith("f_") else None)
            ok = _eq(got, want, tol)
        rows.append({"key": key, "expected": want, "actual": got, "ok": bool(ok)})
    return {"sha": sha, "known": True, "label": fx["label"], "status": fx.get("status"),
            "note": fx.get("note", ""), "passed": all(r["ok"] for r in rows), "rows": rows,
            "summary": summary}


# ── CLI ────────────────────────────────────────────────────────────────

def _run_local(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        sys.path.insert(0, HERE)
        import drawing_extractor
        d = drawing_extractor.analyze_drawing(path)
        return {"error": d["error"]} if d.get("error") else {"drawing_data": d}
    if ext in (".step", ".stp"):
        import subprocess, tempfile
        out = os.path.join(tempfile.mkdtemp(), "g.json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "step_quote_extract.py"), path,
                            "--density", "7.9", "--k", "0.44", "--material", "stainless", "--out", out],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout)[-300:]}
        with open(out) as f:
            return {"geometry": json.load(f)}
    return {"error": f"unsupported: {ext}"}


def _run_url(url, path):
    import urllib.request, uuid
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    fields = {"density": "7.9", "k_factor": "0.44", "quantity": "1"}
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"step_file\"; "
             f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url.rstrip("/") + "/analyze", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}


def main(argv):
    url = None
    if "--url" in argv:
        i = argv.index("--url")
        url = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    exp = load_expected()
    if not url:
        try:
            import fitz  # noqa: F401
        except ImportError:
            print("WARNING: PyMuPDF not installed - PDF results may differ from production "
                  "(rotated text is missed). Use --url against the deployed app for the real test.\n")
    fails = unknown = 0
    for path in argv:
        sha = sha256_file(path)
        resp = _run_url(url, path) if url else _run_local(path)
        res = check(sha, summarize_response(resp), exp)
        name = os.path.basename(path)
        if not res["known"]:
            unknown += 1
            print(f"?    {name}  (not in test set)\n     summary: {json.dumps(res['summary'])}")
            continue
        mark = "PASS" if res["passed"] else "FAIL"
        fails += 0 if res["passed"] else 1
        print(f"{mark} {res['label']}  [{res['status']}]  ({name})")
        for r in res["rows"]:
            if not r["ok"]:
                print(f"     x {r['key']}: expected {r['expected']!r}, got {r['actual']!r}")
    print(f"\n{len(argv) - unknown - fails} passed, {fails} failed, {unknown} unknown")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
