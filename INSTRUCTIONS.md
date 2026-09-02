# STEP File → Quoting Data: Reusable Procedure

**Purpose:** Given only a STEP file (no drawing/PDF), automatically extract everything
needed to quote and program a laser-cut/formed sheet-metal part: envelope, weight,
material thickness, bend table, flat-pattern (developed) width, and a hole/slot/feature
table with positions — all computed directly from the 3D solid geometry.

This was built and validated against `M140621.STEP` (Chicago Metalcraft nosebar side
frame). It is general-purpose but relies on some assumptions listed at the bottom —
always sanity-check a new part's output before using it for CAM.

---

## 1. What's in this folder

| File | Purpose |
|---|---|
| `step_quote_extract.py` | Core pipeline. Loads the STEP file, classifies faces, detects bends, cuts a cross-section to unfold the flat pattern, clusters hole/slot features, and writes `geometry_extract.json`. |
| `generate_views.py` | Renders isometric + top/front/right orthographic PNGs from the solid (for visual sanity-checking). |
| `render_flat_pattern.py` | Draws the computed flat pattern (bend lines + hole/slot positions) as a PNG, from the JSON. |
| `generate_report.py` | Assembles a 3-page quoting PDF (envelope/bend table, views + flat pattern, hole position table) from the JSON + images. |
| `INSTRUCTIONS.md` | This file. |

---

## 2. One-time environment setup

Run once per machine/container:

```bash
pip install cadquery cairosvg reportlab matplotlib pdf2image --break-system-packages
```

(`cadquery` pulls in the OpenCascade B-rep kernel — this is the real CAD engine
doing the geometry work, not a guess/LLM-based estimate.)

---

## 3. Running it on a new STEP file

```bash
# 1. Extract all geometry data -> geometry_extract.json
python3 step_quote_extract.py <PART>.STEP --density 7.9 --k 0.44

# 2. Render reference views (for visual cross-check)
python3 generate_views.py <PART>.STEP --outdir views

# 3. Draw the computed flat pattern
python3 render_flat_pattern.py geometry_extract.json --out flat_pattern.png

# 4. Build the final quoting PDF
python3 generate_report.py geometry_extract.json --views views \
    --flatpattern flat_pattern.png --out <PART>_report.pdf
```

Flags to change per material:
- `--density` — g/cm³ (7.9 = stainless SUS304, 2.70 = aluminum, 7.85 = mild steel)
- `--k` — bend-allowance K-factor (0.44 is a common default for air-bent stainless;
  adjust to your shop's standard bend-deduction table if you have one — see §5)

---

## 4. What the pipeline actually does (so you can trust/debug it)

1. **Load & classify.** Loads the solid with OpenCascade and classifies every face
   as planar or cylindrical.
2. **Detect bends.** Sheet-metal bend faces are the *large* cylindrical faces
   (their length along the fold axis spans most of the part) — as opposed to hole/
   slot/fillet cylinders, whose extent is only about one sheet-thickness. This
   directly gives:
   - **Material thickness** = outer bend radius − inner bend radius
   - **Bend radius** = the inner radius
   - **Bend angle** = the cylindrical face's angular sweep
3. **Cross-section cut.** Cuts the solid with a plane through the middle of the
   part, perpendicular to the dominant bend axis, and walks the resulting profile
   wire face-by-face to get the ordered sequence of flat segments and bend arcs.
4. **Flat-pattern width.** Sums the flat-segment lengths plus each bend's
   *developed* (unfolded) length, using the standard bend-allowance formula:
   `developed_length = angle_rad × (bend_radius + K × thickness)`.
5. **Feature detection.** Every hole, slot, and tab wall is a small face (a short
   cylinder for round holes/slot ends, or a handful of small planar walls for a
   square cutout). These are clustered by 3D proximity, then classified by shape
   signature (round / square / slot). Slot ends are paired up separately since
   they're often too far apart to cluster directly.
6. **Position mapping.** Every feature's 3D center is projected onto the developed
   flat pattern using the same segment geometry from step 3, giving true
   (length, transverse) coordinates on the unfolded blank.

---

## 5. What to check on every new part before quoting/CAM

This is a geometry-based *estimate*, not a substitute for engineering judgment.
Always do a quick pass on these:

- **Bend allowance / K-factor.** The computed flat width shifts by roughly
  ±0.02–0.05 in across a plausible K range (0.33–0.5). If your shop has a
  standard bend-deduction chart for this thickness/material, use that instead
  of trusting the default 0.44.
- **Threaded vs. clearance holes.** STEP solids almost never carry actual thread
  helix geometry — a tapped hole and a plain round hole of the same diameter are
  geometrically identical. Every hole is reported as **"round"** with a diameter;
  cross-check against the customer's intent/BOM for which ones are actually tapped.
- **Tapered / non-constant cross-section zones.** The flat-pattern unfold assumes
  a *constant* cross-section along the whole bend axis. If the part tapers,
  miters, or changes profile partway along its length (as M140621 does near its
  tip), that zone is **not** captured by the simple rectangle in
  `flat_pattern.png` — check the isometric/top views and the solid directly for
  that area.
- **"Unclassified" feature count.** The report lists how many candidate feature
  faces didn't confidently match round/square/slot. These are usually stray
  fillets, edge breaks, or partially-merged clusters near complex geometry
  (tapered ends, mitered corners) — open the STEP in a viewer and eyeball that
  zone before finalizing hole counts.
- **Reference origin ("length = 0").** The tool auto-picks a bounding-box corner
  as position zero — it does **not** know which physical end the shop
  conventionally measures from. Confirm which end is "0" before using positions
  for machine setup.
- **Units/orientation sanity check.** Compare the computed overall length/width/
  weight against whatever the customer or sales order says (even a rough verbal
  figure) as a first-order gut check that the STEP imported correctly (right
  units, not mirrored, etc.)

---

## 6. Known limitations (v1)

- Assumes a **single dominant bend axis** (i.e., all main bends are parallel —
  true for simple formed channels/brackets like M140621; a part with bends in
  two different directions will need the cross-section cut re-aimed manually).
- Does **not** reconstruct the true outline of tapered/mitered edges — only the
  bounding rectangle.
- Feature classification is heuristic (proximity clustering + shape signature),
  not a guarantee — always check the "unclassified" bucket.
- Tested on one part family (formed sheet-metal channel/bracket with square and
  round cutouts, slots, and a stepped/tapered tip). More part geometries should
  be run through and spot-checked before fully trusting the automation on
  unfamiliar shapes (e.g., parts with two bend directions, hems, louvers, or
  compound curves).

---

## 7. Quick reference — file outputs

Running the full pipeline on `<PART>.STEP` produces:

```
geometry_extract.json      <- all computed data (machine-readable)
views/view_iso.png         <- isometric
views/view_top.png         <- plan view
views/view_front.png       <- front/edge view
views/view_right.png       <- end-profile view
flat_pattern.png           <- computed flat pattern plot
<PART>_report.pdf          <- final 3-page quoting PDF
```
