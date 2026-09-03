"""
Drawing Extractor - PDF/DWG Drawing Spec Extraction
====================================================
Extracts dimensions, material callouts, tolerances, bend info,
thickness, finish, and part numbers from engineering PDF drawings.

Uses PyMuPDF for text extraction with tesseract OCR fallback for scanned drawings.
"""

import re
import json
import sys
import os
import argparse

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# ââ Material database ââââââââââââââââââââââââââââââââââââââââââââââââââ
KNOWN_MATERIALS = {
    # Stainless steels
    "304": {"name": "Stainless Steel 304", "density_gcc": 7.9, "family": "stainless"},
    "304L": {"name": "Stainless Steel 304L", "density_gcc": 7.9, "family": "stainless"},
    "316": {"name": "Stainless Steel 316", "density_gcc": 8.0, "family": "stainless"},
    "316L": {"name": "Stainless Steel 316L", "density_gcc": 8.0, "family": "stainless"},
    "301": {"name": "Stainless Steel 301", "density_gcc": 7.9, "family": "stainless"},
    "409": {"name": "Stainless Steel 409", "density_gcc": 7.7, "family": "stainless"},
    "430": {"name": "Stainless Steel 430", "density_gcc": 7.7, "family": "stainless"},
    "SUS304": {"name": "Stainless Steel SUS304", "density_gcc": 7.9, "family": "stainless"},
    # Carbon / mild steels
    "A36": {"name": "ASTM A36 Steel", "density_gcc": 7.85, "family": "carbon"},
    "1018": {"name": "AISI 1018 Steel", "density_gcc": 7.87, "family": "carbon"},
    "1020": {"name": "AISI 1020 Steel", "density_gcc": 7.87, "family": "carbon"},
    "1045": {"name": "AISI 1045 Steel", "density_gcc": 7.87, "family": "carbon"},
    "CRS": {"name": "Cold Rolled Steel", "density_gcc": 7.85, "family": "carbon"},
    "HRS": {"name": "Hot Rolled Steel", "density_gcc": 7.85, "family": "carbon"},
    "HRPO": {"name": "Hot Rolled Pickled & Oiled", "density_gcc": 7.85, "family": "carbon"},
    # Aluminum
    "6061": {"name": "Aluminum 6061", "density_gcc": 2.7, "family": "aluminum"},
    "5052": {"name": "Aluminum 5052", "density_gcc": 2.68, "family": "aluminum"},
    "3003": {"name": "Aluminum 3003", "density_gcc": 2.73, "family": "aluminum"},
    "7075": {"name": "Aluminum 7075", "density_gcc": 2.81, "family": "aluminum"},
    # Galvanized
    "GALV": {"name": "Galvanized Steel", "density_gcc": 7.85, "family": "galvanized"},
    "G90": {"name": "Galvanized G90", "density_gcc": 7.85, "family": "galvanized"},
    "G60": {"name": "Galvanized G60", "density_gcc": 7.85, "family": "galvanized"},
    # Copper / brass
    "C110": {"name": "Copper C110", "density_gcc": 8.94, "family": "copper"},
    "C260": {"name": "Brass C260", "density_gcc": 8.53, "family": "brass"},
}

GAUGE_TO_INCHES = {
    7: 0.1793, 8: 0.1644, 9: 0.1495, 10: 0.1345, 11: 0.1196,
    12: 0.1046, 13: 0.0897, 14: 0.0747, 15: 0.0673, 16: 0.0598,
    17: 0.0538, 18: 0.0478, 19: 0.0418, 20: 0.0359, 21: 0.0329,
    22: 0.0299, 23: 0.0269, 24: 0.0239, 25: 0.0209, 26: 0.0179,
    27: 0.0164, 28: 0.0149, 29: 0.0135, 30: 0.0120,
}

FINISH_KEYWORDS = [
    "powder coat", "powdercoat", "anodize", "anodized", "paint", "painted",
    "galvanize", "galvanized", "zinc plate", "zinc plated", "chrome",
    "chromate", "passivat", "bead blast", "sandblast", "tumble",
    "deburr", "electropolis", "brushed", "mirror", "#4 finish", "#2B",
    "mill finish", "clear coat", "prime", "primer", "e-coat", "ecoat",
    "black oxide", "nickel plate", "tin plate", "hot dip",
]


# ââ Regex patterns âââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _compile_patterns():
    """Pre-compile all extraction patterns."""
    return {
        # Dimensions: 12.500, .125, 3/4, 1-1/2, with optional " or IN or MM
        "dimensions": re.compile(
            r'(\d+\.?\d*)\s*[xXÃ]\s*(\d+\.?\d*)'  # LxW
            r'(?:\s*[xXÃ]\s*(\d+\.?\d*))?'          # optional xH
            r'\s*(?:"|IN(?:CH(?:ES)?)?|MM|CM)?',
            re.IGNORECASE
        ),
        # Individual measurements with units
        "measurements": re.compile(
            r'(\d{1,4}\.?\d{0,4})\s*(?:"|IN(?:CH(?:ES)?)?)\b'
            r'|(\d{1,4}\.\d{1,4})\s*(?:MM)\b',
            re.IGNORECASE
        ),
        # Fractions: 3/4", 1-1/2"
        "fractions": re.compile(
            r'(\d+)?[-\s]?(\d+)/(\d+)\s*(?:"|IN)?',
            re.IGNORECASE
        ),
        # Material callouts
        "material_steel": re.compile(
            r'\b(30[14]L?|316L?|40[19]|430|SUS\s*30[14])\b'
            r'|\b(A-?36|ASTM\s*A-?36)\b'
            r'|\b(10[12][\d]|1045)\s*(?:CRS|HRS|STEEL|STL)?\b'
            r'|\b(CRS|HRS|HRPO|CR\s*STEEL|HR\s*STEEL)\b'
            r'|\bSTAINLESS\s*(?:STEEL)?\b'
            r'|\bMILD\s*STEEL\b'
            r'|\bCARBON\s*STEEL\b',
            re.IGNORECASE
        ),
        "material_aluminum": re.compile(
            r'\b(60[67]\d|50[35]\d|30[01]\d|7075)\s*(?:-?T\d+)?\b'
            r'|\bALUM(?:INUM|INIUM)?\b'
            r'|\bAL\s+\d{4}\b',
            re.IGNORECASE
        ),
        "material_galv": re.compile(
            r'\bGALV(?:ANIZED)?\b|\bG-?[69]0\b|\bGI\b',
            re.IGNORECASE
        ),
        "material_copper": re.compile(
            r'\bCOPPER\b|\bBRASS\b|\bC-?[12]\d{2}\b',
            re.IGNORECASE
        ),
        # Thickness: 0.060 THK, 16 GA, 16 GAUGE, .125" THICK
        "thickness_decimal": re.compile(
            r'(\d*\.?\d+)\s*(?:"|IN)?\s*(?:THK|THICK(?:NESS)?)\b'
            r'|(?:THK|THICK(?:NESS)?)\s*[:=]?\s*(\d*\.?\d+)\s*(?:"|IN)?',
            re.IGNORECASE
        ),
        "thickness_gauge": re.compile(
            r'(\d{1,2})\s*(?:GA(?:UGE)?|GAGE)\b'
            r'|(?:GA(?:UGE)?|GAGE)\s*[:=]?\s*(\d{1,2})\b',
            re.IGNORECASE
        ),
        # Tolerances
        "tolerance": re.compile(
            r'[Â±]\s*(\d*\.?\d+)\s*(?:"|IN|MM)?'
            r'|\+/?-\s*(\d*\.?\d+)\s*(?:"|IN|MM)?'
            r'|(?:TOL(?:ERANCE)?)\s*[:=]?\s*[Â±]?\s*(\d*\.?\d+)',
            re.IGNORECASE
        ),
        "tolerance_class": re.compile(
            r'\.X+\s*[Â±]\s*\.?\d+'
            r'|UNLESS\s+OTHERWISE\s+(?:NOTED|SPECIFIED|STATED)',
            re.IGNORECASE
        ),
        # Bend info
        "bend_radius": re.compile(
            r'(?:BEND\s*)?R(?:AD(?:IUS)?)?\.?\s*[:=]?\s*(\d*\.?\d+)\s*(?:"|IN|MM)?'
            r'|(?:INSIDE|BEND)\s+(?:RAD(?:IUS)?|R)\s*[:=]?\s*(\d*\.?\d+)',
            re.IGNORECASE
        ),
        "bend_angle": re.compile(
            r'(\d{1,3})\s*(?:Â°|DEG(?:REES?)?)\s*(?:BEND)?'
            r'|BEND\s+(?:ANGLE\s*)?[:=]?\s*(\d{1,3})\s*(?:Â°|DEG)?',
            re.IGNORECASE
        ),
        # Part number
        "part_number": re.compile(
            r'(?:PART\s*(?:NO\.?|NUM(?:BER)?|#)\s*[:=]?\s*)([A-Z0-9][\w\-\.]{2,20})'
            r'|(?:P/?N\s*[:=]?\s*)([A-Z0-9][\w\-\.]{2,20})'
            r'|(?:DWG\s*(?:NO\.?|#)\s*[:=]?\s*)([A-Z0-9][\w\-\.]{2,20})',
            re.IGNORECASE
        ),
        # Quantity
        "quantity": re.compile(
            r'(?:QTY|QUANTITY)\s*[:=]?\s*(\d+)'
            r'|(\d+)\s*(?:PCS?|PIECES?|EA(?:CH)?)\b',
            re.IGNORECASE
        ),
        # Scale
        "scale": re.compile(
            r'SCALE\s*[:=]?\s*(\d+)\s*[:=/]\s*(\d+)'
            r'|(\d+):(\d+)\s*SCALE',
            re.IGNORECASE
        ),
        # Revision
        "revision": re.compile(
            r'REV\.?\s*[:=]?\s*([A-Z0-9]{1,5})\b',
            re.IGNORECASE
        ),
    }

PATTERNS = _compile_patterns()


# ââ Extraction functions âââââââââââââââââââââââââââââââââââââââââââââââ

def extract_text_from_pdf(pdf_path):
    """Extract text from all pages of a PDF."""
    if fitz is None:
        raise ImportError("PyMuPDF (fitz) is required. Install with: pip install PyMuPDF")

    doc = fitz.open(pdf_path)
    pages = []
    full_text = ""
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        pages.append({"page": page_num + 1, "text": text, "has_text": len(text.strip()) > 20})
        full_text += text + "\n"

    # If most pages have no text, it's likely a scanned drawing
    text_pages = sum(1 for p in pages if p["has_text"])
    is_scanned = text_pages < len(pages) * 0.3

    doc.close()
    return full_text, pages, is_scanned


def extract_text_ocr(pdf_path):
    """Fallback: use tesseract OCR for scanned PDFs."""
    try:
        import subprocess
        # Convert PDF to images, then OCR
        # First try pdf2image
        from pdf2image import convert_from_path
        import pytesseract

        images = convert_from_path(pdf_path, dpi=300)
        full_text = ""
        for img in images:
            text = pytesseract.image_to_string(img)
            full_text += text + "\n"
        return full_text
    except ImportError:
        # If pdf2image or pytesseract not available, try PyMuPDF's built-in OCR
        try:
            doc = fitz.open(pdf_path)
            full_text = ""
            for page in doc:
                # Render page to image, extract text from image blocks
                pix = page.get_pixmap(dpi=300)
                # Try to get text from the rendered image via fitz
                tp = page.get_text("text")
                full_text += tp + "\n"
            doc.close()
            return full_text
        except Exception:
            return ""


def find_materials(text):
    """Extract material callouts from drawing text."""
    materials = []

    # Check steel
    for m in PATTERNS["material_steel"].finditer(text):
        raw = m.group(0).strip()
        # Try to match to known material
        for key, info in KNOWN_MATERIALS.items():
            if key.lower() in raw.lower() or (len(key) >= 3 and key in raw.upper()):
                materials.append({
                    "raw_callout": raw,
                    "material_key": key,
                    "name": info["name"],
                    "density_gcc": info["density_gcc"],
                    "family": info["family"],
                })
                break
        else:
            materials.append({"raw_callout": raw, "material_key": None, "family": "steel"})

    # Check aluminum
    for m in PATTERNS["material_aluminum"].finditer(text):
        raw = m.group(0).strip()
        for key, info in KNOWN_MATERIALS.items():
            if key in raw.upper():
                materials.append({
                    "raw_callout": raw,
                    "material_key": key,
                    "name": info["name"],
                    "density_gcc": info["density_gcc"],
                    "family": info["family"],
                })
                break
        else:
            materials.append({"raw_callout": raw, "material_key": None, "family": "aluminum"})

    # Check galvanized
    for m in PATTERNS["material_galv"].finditer(text):
        raw = m.group(0).strip()
        materials.append({"raw_callout": raw, "material_key": "GALV", "family": "galvanized",
                          "name": "Galvanized Steel", "density_gcc": 7.85})

    # Check copper/brass
    for m in PATTERNS["material_copper"].finditer(text):
        raw = m.group(0).strip()
        materials.append({"raw_callout": raw, "material_key": None, "family": "copper/brass"})

    # Deduplicate by raw_callout
    seen = set()
    unique = []
    for mat in materials:
        if mat["raw_callout"].upper() not in seen:
            seen.add(mat["raw_callout"].upper())
            unique.append(mat)
    return unique


def find_thickness(text):
    """Extract sheet thickness from drawing."""
    results = []

    # Decimal thickness
    for m in PATTERNS["thickness_decimal"].finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            try:
                t = float(val)
                if 0.005 <= t <= 1.0:  # reasonable sheet metal thickness in inches
                    results.append({"value_in": round(t, 4), "gauge": None, "raw": m.group(0).strip()})
            except ValueError:
                pass

    # Gauge thickness
    for m in PATTERNS["thickness_gauge"].finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            try:
                g = int(val)
                if g in GAUGE_TO_INCHES:
                    results.append({
                        "value_in": GAUGE_TO_INCHES[g],
                        "gauge": g,
                        "raw": m.group(0).strip()
                    })
            except ValueError:
                pass

    # Deduplicate
    seen = set()
    unique = []
    for t in results:
        key = t["value_in"]
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def find_tolerances(text):
    """Extract tolerance specifications."""
    results = []

    for m in PATTERNS["tolerance"].finditer(text):
        val = m.group(1) or m.group(2) or m.group(3)
        if val:
            try:
                t = float(val)
                if 0.0001 <= t <= 1.0:
                    results.append({"value": t, "raw": m.group(0).strip()})
            except ValueError:
                pass

    # General tolerance notes
    for m in PATTERNS["tolerance_class"].finditer(text):
        results.append({"value": None, "raw": m.group(0).strip(), "type": "general_note"})

    return results


def find_bends(text):
    """Extract bend radius and angle info."""
    radii = []
    angles = []

    for m in PATTERNS["bend_radius"].finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            try:
                r = float(val)
                if 0.01 <= r <= 10.0:
                    radii.append({"value_in": round(r, 4), "raw": m.group(0).strip()})
            except ValueError:
                pass

    for m in PATTERNS["bend_angle"].finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            try:
                a = float(val)
                if 1 <= a <= 180:
                    angles.append({"value_deg": a, "raw": m.group(0).strip()})
            except ValueError:
                pass

    return {"radii": radii, "angles": angles}


def find_dimensions(text):
    """Extract overall dimensions from drawing."""
    dims = []

    # LxW or LxWxH patterns
    for m in PATTERNS["dimensions"].finditer(text):
        d = {"length": float(m.group(1)), "width": float(m.group(2))}
        if m.group(3):
            d["height"] = float(m.group(3))
        d["raw"] = m.group(0).strip()
        # Filter out obviously wrong matches (too small or too large)
        if 0.01 <= d["length"] <= 500 and 0.01 <= d["width"] <= 500:
            dims.append(d)

    return dims


def find_part_info(text):
    """Extract part number, quantity, revision, scale."""
    info = {}

    # Part number
    for m in PATTERNS["part_number"].finditer(text):
        pn = m.group(1) or m.group(2) or m.group(3)
        if pn and len(pn) >= 3:
            info["part_number"] = pn.strip()
            break

    # Quantity
    for m in PATTERNS["quantity"].finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            info["quantity"] = int(val)
            break

    # Revision
    for m in PATTERNS["revision"].finditer(text):
        info["revision"] = m.group(1).strip()
        break

    # Scale
    for m in PATTERNS["scale"].finditer(text):
        s1 = m.group(1) or m.group(3)
        s2 = m.group(2) or m.group(4)
        if s1 and s2:
            info["scale"] = f"{s1}:{s2}"
            break

    return info


def find_finishes(text):
    """Extract finish/surface treatment callouts."""
    found = []
    text_upper = text.upper()
    for kw in FINISH_KEYWORDS:
        if kw.upper() in text_upper:
            # Get surrounding context
            idx = text_upper.index(kw.upper())
            start = max(0, idx - 20)
            end = min(len(text), idx + len(kw) + 30)
            context = text[start:end].strip()
            found.append({"finish": kw, "context": context})

    # Deduplicate by finish keyword
    seen = set()
    unique = []
    for f in found:
        if f["finish"].lower() not in seen:
            seen.add(f["finish"].lower())
            unique.append(f)
    return unique


def identify_missing_info(result):
    """Flag what's missing that Sales would need to ask about."""
    missing = []

    if not result.get("materials"):
        missing.append({
            "field": "Material",
            "message": "No material callout found on drawing. Ask customer for material specification."
        })

    if not result.get("thickness"):
        missing.append({
            "field": "Thickness",
            "message": "No sheet thickness or gauge found. Ask customer for material thickness."
        })

    if not result.get("part_info", {}).get("quantity"):
        missing.append({
            "field": "Quantity",
            "message": "No quantity specified. Ask customer for order quantity."
        })

    if not result.get("tolerances"):
        missing.append({
            "field": "Tolerances",
            "message": "No tolerance specs found. Standard shop tolerances will apply unless specified."
        })

    if not result.get("finishes"):
        missing.append({
            "field": "Finish",
            "message": "No finish or surface treatment specified. Confirm if parts need finishing."
        })

    return missing


def analyze_drawing(pdf_path):
    """Main analysis pipeline for a PDF drawing."""
    if not os.path.isfile(pdf_path):
        return {"error": f"File not found: {pdf_path}"}

    ext = os.path.splitext(pdf_path)[1].lower()
    if ext not in (".pdf",):
        return {"error": f"Unsupported file type: {ext}. Currently supports PDF."}

    # 1. Extract text
    try:
        full_text, pages, is_scanned = extract_text_from_pdf(pdf_path)
    except Exception as e:
        return {"error": f"Failed to read PDF: {e}"}

    # 2. If scanned, try OCR
    if is_scanned:
        ocr_text = extract_text_ocr(pdf_path)
        if len(ocr_text.strip()) > len(full_text.strip()):
            full_text = ocr_text

    if len(full_text.strip()) < 10:
        return {
            "error": "Could not extract text from this PDF. It may be a scanned image without OCR capability.",
            "is_scanned": True,
            "page_count": len(pages),
        }

    # 3. Run all extractors
    result = {
        "source_file": os.path.basename(pdf_path),
        "file_type": "pdf_drawing",
        "page_count": len(pages),
        "is_scanned": is_scanned,
        "materials": find_materials(full_text),
        "thickness": find_thickness(full_text),
        "dimensions": find_dimensions(full_text),
        "tolerances": find_tolerances(full_text),
        "bends": find_bends(full_text),
        "finishes": find_finishes(full_text),
        "part_info": find_part_info(full_text),
    }

    # 4. Identify what's missing
    result["missing_info"] = identify_missing_info(result)

    # 5. Determine likely fab type
    bend_data = result["bends"]
    has_bends = len(bend_data.get("radii", [])) > 0 or len(bend_data.get("angles", [])) > 0
    has_thickness = len(result["thickness"]) > 0

    if has_thickness or has_bends:
        result["likely_fab_type"] = "sheet_metal"
        result["fab_type_confidence"] = "high" if (has_thickness and has_bends) else "medium"
    else:
        result["likely_fab_type"] = "unknown"
        result["fab_type_confidence"] = "low"

    # 6. Build summary
    summary_parts = []
    if result["part_info"].get("part_number"):
        summary_parts.append(f"Part: {result['part_info']['part_number']}")
    if result["materials"]:
        summary_parts.append(f"Material: {result['materials'][0].get('name') or result['materials'][0]['raw_callout']}")
    if result["thickness"]:
        t = result["thickness"][0]
        tk = f"{t['value_in']}\"" + (f" ({t['gauge']} GA)" if t["gauge"] else "")
        summary_parts.append(f"Thickness: {tk}")
    if result["dimensions"]:
        d = result["dimensions"][0]
        dim_str = f"{d['length']}\" x {d['width']}\""
        if "height" in d:
            dim_str += f" x {d['height']}\""
        summary_parts.append(f"Size: {dim_str}")
    if result["part_info"].get("quantity"):
        summary_parts.append(f"Qty: {result['part_info']['quantity']}")

    result["summary"] = " | ".join(summary_parts) if summary_parts else "Limited data extracted"

    return result


# ââ CLI interface ââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def main():
    parser = argparse.ArgumentParser(description="Extract specs from engineering PDF drawings")
    parser.add_argument("input_file", help="Path to PDF drawing")
    parser.add_argument("--out", "-o", help="Output JSON path", default=None)
    args = parser.parse_args()

    result = analyze_drawing(args.input_file)

    out_path = args.out or os.path.splitext(args.input_file)[0] + "_extracted.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Extracted specs written to {out_path}")
    print(f"Summary: {result['summary']}")
    if result["missing_info"]:
        print(f"Missing info ({len(result['missing_info'])} items):")
        for mi in result["missing_info"]:
            print(f"  - {mi['field']}: {mi['message']}")


if __name__ == "__main__":
    main()
