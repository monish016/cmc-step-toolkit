"""
CMC STEP Quoting Toolkit - Web Application
===========================================
Upload STEP files, get geometry extraction + quoting PDF back.
Built for Chicago Metalcraft sheet-metal parts.

v3.4 - Gauge detection, hardware callouts, complexity scoring, countersink/chamfer detection, feature detail table, improved UX, Excel shop rates import, responsive mobile/tablet layout
"""
import os
import uuid
import json
import math
import shutil
import subprocess
import time
import sqlite3
import io
from datetime import datetime
from flask import Flask, request, render_template_string, send_file, jsonify, url_for
from werkzeug.utils import secure_filename
try:
    import openpyxl
except ImportError:
    openpyxl = None
import cost_engine

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB max upload
app.config["UPLOAD_FOLDER"] = "/tmp/step_uploads"

ALLOWED_EXTENSIONS = {"step", "stp", "STEP", "STP", "pdf", "PDF", "dwg", "DWG", "dxf", "DXF",
                      "xlsx", "XLSX", "xls", "XLS", "csv", "CSV",
                      "sldprt", "SLDPRT", "sldasm", "SLDASM", "igs", "IGS", "iges", "IGES"}

# ---------- SQLite persistent job history ----------
DB_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DB_DIR, "jobs.db")
MAX_HISTORY = 200


def _get_db():
    """Return a sqlite3 connection (one per call — safe for gunicorn)."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Create the jobs table if it doesn't exist."""
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT UNIQUE NOT NULL,
            filename    TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            fab_type    TEXT,
            dimensions  TEXT,
            num_bends   INTEGER DEFAULT 0,
            weight      TEXT,
            report_url  TEXT,
            json_url    TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def _insert_job(entry):
    """Insert a job record into SQLite."""
    conn = _get_db()
    conn.execute("""
        INSERT OR REPLACE INTO jobs
            (job_id, filename, timestamp, fab_type, dimensions, num_bends, weight, report_url, json_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry["job_id"], entry["filename"], entry["timestamp"],
        entry.get("fab_type"), entry.get("dimensions"),
        entry.get("num_bends", 0), entry.get("weight"),
        entry.get("report_url"), entry.get("json_url"),
    ))
    conn.commit()
    conn.close()


def _get_jobs(limit=200):
    """Return recent jobs as list of dicts."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- SQLite shop config persistence ----------

def _init_config_db():
    """Create the shop_config table if it doesn't exist."""
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shop_config (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            config_json TEXT NOT NULL,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def _get_config():
    """Return the saved shop config dict, or DEFAULT_CONFIG if none saved."""
    conn = _get_db()
    row = conn.execute("SELECT config_json FROM shop_config WHERE id = 1").fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    return cost_engine.get_default_config()


def _save_config(config_dict):
    """Save or update the shop config in SQLite."""
    conn = _get_db()
    conn.execute("""
        INSERT INTO shop_config (id, config_json, updated_at)
        VALUES (1, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET config_json = excluded.config_json, updated_at = datetime('now')
    """, (json.dumps(config_dict),))
    conn.commit()
    conn.close()


# Initialise DB at import time (runs once per gunicorn worker)
_init_db()
_init_config_db()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1] in ALLOWED_EXTENSIONS


# ── Density-to-material name mapping for cost engine ────────────────
DENSITY_TO_MATERIAL = {
    "7.9":      "Stainless Steel (SUS304)",
    "7.85":     "Mild/Carbon Steel",
    "7.85_galv": "Galvanized Steel",
    "2.71":     "Aluminum 6061",
    "2.68":     "Aluminum 5052",
    "8.96":     "Copper (C110)",
    "8.53":     "Brass (C260)",
}


def _build_cost_geometry(geometry):
    """Convert STEP extraction JSON into the dict that cost_engine.estimate_cost() expects."""
    fab_type = geometry.get("fab_type", "sheet_metal")
    env = geometry.get("envelope", {})
    bbox = env.get("bbox_mm", {})

    # Dimensions in inches
    x_in = bbox.get("xlen", 0) / 25.4
    y_in = bbox.get("ylen", 0) / 25.4
    z_in = bbox.get("zlen", 0) / 25.4

    thickness = geometry.get("thickness_in", 0.0) or 0.0
    flat_w = float(geometry.get("flat_width_in", 0) or 0)
    flat_l = float(geometry.get("flat_length_in", 0) or 0)
    features = geometry.get("features", [])

    # Classify features for secondary operations
    hole_count = 0
    tap_count = 0
    csink_count = 0
    hardware_count = 0
    tap_sizes = []

    for f in features:
        ftype = f.get("type", "")
        hint = (f.get("hardware_hint", "") or "").lower()

        if ftype == "countersink":
            csink_count += 1
            hole_count += 1
        elif ftype == "round":
            hole_count += 1
            if "tap" in hint:
                tap_count += 1
                dia = f.get("diameter_in", 0) or 0
                if dia < 0.15:
                    tap_sizes.append("small")
                elif dia < 0.35:
                    tap_sizes.append("medium")
                else:
                    tap_sizes.append("large")
            elif "clearance" in hint:
                hardware_count += 1

    tap_size_class = max(set(tap_sizes), key=tap_sizes.count) if tap_sizes else "medium"

    # Estimate cut perimeter from flat pattern + features
    outer_perim = 2 * (flat_w + flat_l) if flat_w > 0 and flat_l > 0 else 2 * (x_in + y_in)
    feature_perim = 0.0
    for f in features:
        ftype = f.get("type", "")
        if ftype == "round":
            dia = f.get("diameter_in", 0) or 0
            feature_perim += math.pi * dia
        elif ftype == "square_or_rect":
            sz = f.get("size_in", [0, 0])
            feature_perim += 2 * (sz[0] + sz[1])
        elif ftype == "countersink":
            dia = f.get("diameter_in", 0) or 0
            feature_perim += math.pi * dia
        elif ftype == "slot":
            sw = f.get("width_in", 0) or 0
            sl = f.get("slot_length_in", 0) or 0
            feature_perim += 2 * sl + math.pi * sw
    cut_perim = outer_perim + feature_perim

    # Volume in in^3
    vol_in3 = env.get("volume_mm3", 0) / 16387.064

    # Fab type label
    fab_label = "Sheet Metal" if fab_type == "sheet_metal" else "Machined"

    # Gauge number from thickness for hole quality checks
    gauge_num = geometry.get("gauge", None)

    return {
        "fab_type": fab_label,
        "thickness_in": thickness,
        "dims": {"length": x_in, "width": y_in, "height": z_in},
        "bend_count": geometry.get("num_bends", 0) or 0,
        "cut_perimeter_in": round(cut_perim, 2),
        "flat_width_in": flat_w,
        "flat_length_in": flat_l,
        "weight_lb": env.get("mass_lb", 0) or 0,
        "hole_count": hole_count,
        "tap_count": tap_count,
        "tap_size_class": tap_size_class,
        "csink_count": csink_count,
        "hardware_count": hardware_count,
        "volume_in3": vol_in3,
        "machining_type": geometry.get("machining_type", None),
        "material_removal_ratio": geometry.get("material_removal_ratio", 0),
        "gauge_num": gauge_num,
        "features_list": features,
        "bend_details": geometry.get("bend_details", []),
        "nesting": geometry.get("nesting", None),
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CMC Quoting Toolkit</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Arial, Helvetica, sans-serif; background: #f4f5f7; color: #1a1a1a; }
  .header { background: #1a3a1a; color: #fff; padding: 1.2rem 2rem; display: flex; align-items: center; gap: 1.2rem; }
  .header-logo { height: 48px; filter: brightness(0) invert(1); flex-shrink: 0; }
  .header h1 { font-size: 1.4rem; font-weight: 700; }
  .header .sub { font-size: 0.85rem; color: #a0c8a0; }
  .container { max-width: 900px; margin: 2rem auto; padding: 0 1.5rem; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); padding: 2rem; margin-bottom: 1.5rem; }
  .card h2 { font-size: 1.15rem; margin-bottom: 1rem; color: #1a3a1a; }
  .upload-zone { border: 2px dashed #b0b8c0; border-radius: 8px; padding: 2.5rem 1rem; text-align: center; cursor: pointer; transition: border-color 0.2s, background 0.2s; }
  .upload-zone:hover, .upload-zone.dragover { border-color: #1a3a1a; background: #f0f7f0; }
  .upload-zone p { font-size: 1rem; color: #555; margin-bottom: 0.5rem; }
  .upload-zone .hint { font-size: 0.8rem; color: #999; }
  input[type="file"] { display: none; }
  .file-list { margin-top: 0.8rem; }
  .file-chip { display: inline-flex; align-items: center; gap: 0.4rem; background: #e8f0e8; border: 1px solid #c0d8c0; border-radius: 16px; padding: 0.3rem 0.8rem; font-size: 0.8rem; margin: 0.2rem; }
  .file-chip .remove { cursor: pointer; color: #c00; font-weight: bold; font-size: 1rem; line-height: 1; }
  .params { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1.2rem; }
  .param-group label { display: block; font-size: 0.85rem; font-weight: 600; color: #333; margin-bottom: 0.3rem; }
  .param-group input, .param-group select { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; }
  .btn { display: inline-block; background: #1a3a1a; color: #fff; padding: 0.7rem 2rem; border: none; border-radius: 6px; font-size: 1rem; font-weight: 600; cursor: pointer; margin-top: 1.2rem; transition: background 0.2s; }
  .btn:hover { background: #2a5a2a; }
  .btn:disabled { background: #999; cursor: not-allowed; }
  .btn-sm { padding: 0.4rem 1rem; font-size: 0.85rem; margin-top: 0; border-radius: 4px; }
  .btn-outline { background: transparent; color: #1a3a1a; border: 2px solid #1a3a1a; }
  .btn-outline:hover { background: #1a3a1a; color: #fff; }
  .progress { display: none; margin-top: 1rem; }
  .progress .bar-wrap { background: #e0e0e0; border-radius: 4px; height: 8px; overflow: hidden; }
  .progress .bar { background: linear-gradient(90deg, #1a3a1a, #2a5a2a); height: 100%; width: 0%; transition: width 0.3s; border-radius: 4px; }
  .progress .status { font-size: 0.85rem; color: #555; margin-top: 0.5rem; }
  .progress .elapsed { font-size: 0.75rem; color: #999; margin-top: 0.2rem; }
  .progress .step-list { margin-top: 0.5rem; }
  .progress .step-item { font-size: 0.8rem; color: #999; padding: 2px 0; }
  .progress .step-item.active { color: #1a3a1a; font-weight: 600; }
  .progress .step-item.done { color: #2a5a2a; }
  .progress .step-item.done::before { content: "\\2713 "; color: #2a5a2a; }
  .progress .step-item.active::before { content: ""; display: inline-block; width: 12px; height: 12px; border: 2px solid #1a3a1a; border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 4px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .results { display: none; }
  .result-section { border: 1px solid #dde5dd; border-radius: 8px; margin-bottom: 1.5rem; overflow: hidden; }
  .result-header { background: #f0f4f0; padding: 0.8rem 1.2rem; display: flex; justify-content: space-between; align-items: center; cursor: pointer; }
  .result-header h3 { font-size: 1rem; color: #1a3a1a; }
  .result-header .badge { font-size: 0.75rem; background: #1a3a1a; color: #fff; padding: 0.15rem 0.6rem; border-radius: 10px; }
  .result-body { padding: 1.2rem; }
  .result-body.collapsed { display: none; }
  .geo-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
  .geo-stat { background: #f8faf8; border: 1px solid #e0e8e0; border-radius: 6px; padding: 0.8rem; text-align: center; }
  .geo-stat .value { font-size: 1.3rem; font-weight: 700; color: #1a3a1a; }
  .geo-stat .label { font-size: 0.75rem; color: #666; margin-top: 0.2rem; }
  .detail-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin: 0.8rem 0; }
  .detail-table th, .detail-table td { padding: 0.5rem 0.8rem; border: 1px solid #ddd; text-align: left; }
  .detail-table th { background: #f0f4f0; font-weight: 600; width: 40%; }
  .view-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin: 1rem 0; }
  .view-item { background: #f8faf8; border: 1px solid #dde5dd; border-radius: 6px; padding: 0.6rem; text-align: center; }
  .view-item img { max-width: 100%; border-radius: 4px; margin-bottom: 0.3rem; }
  .view-item .view-label { font-size: 0.8rem; color: #555; }
  .dl-row { display: flex; gap: 0.8rem; flex-wrap: wrap; margin-top: 1rem; }
  .dl-btn { display: inline-block; background: #2a5a2a; color: #fff; padding: 0.5rem 1.2rem; border-radius: 4px; text-decoration: none; font-weight: 600; font-size: 0.85rem; }
  .dl-btn:hover { background: #1a3a1a; }
  .dl-btn.secondary { background: #555; border: none; cursor: pointer; }
  .dl-btn.secondary:hover { background: #333; }
  .nesting-box { margin: 1rem 0; padding: 0.8rem; background: #f8f8f0; border: 1px solid #ddd; border-radius: 6px; }
  .nesting-box h4 { margin: 0 0 0.5rem 0; color: #2a5a2a; font-size: 0.95rem; }
  .error { color: #c00; background: #fff0f0; border: 1px solid #fcc; border-radius: 6px; padding: 1rem; margin-top: 1rem; display: none; }
  .history-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  .history-table th, .history-table td { padding: 0.5rem 0.8rem; border-bottom: 1px solid #eee; text-align: left; }
  .history-table th { font-weight: 600; color: #555; font-size: 0.8rem; text-transform: uppercase; }
  .history-table tr:hover { background: #f8faf8; }
  .empty-state { text-align: center; padding: 2rem; color: #999; font-size: 0.9rem; }
  .batch-summary { background: #f0f7f0; border: 1px solid #c0d8c0; border-radius: 6px; padding: 1rem; margin-bottom: 1rem; display: flex; justify-content: space-around; text-align: center; }
  .batch-summary .stat .num { font-size: 1.5rem; font-weight: 700; color: #1a3a1a; }
  .batch-summary .stat .lbl { font-size: 0.75rem; color: #666; }
  .tab-bar { display: flex; gap: 0; border-bottom: 2px solid #dde5dd; margin-bottom: 1.5rem; }
  .tab { padding: 0.6rem 1.2rem; cursor: pointer; font-size: 0.9rem; font-weight: 600; color: #666; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.2s; }
  .tab:hover { color: #1a3a1a; }
  .tab.active { color: #1a3a1a; border-bottom-color: #1a3a1a; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .footer { text-align: center; padding: 2rem; font-size: 0.8rem; color: #999; }
  /* Shop Rates config panel */
  .cfg-section { margin-bottom: 1.5rem; }
  .cfg-section h3 { font-size: 0.95rem; color: #1a3a1a; margin-bottom: 0.5rem; cursor: pointer; padding: 0.5rem 0.7rem; background: #f0f7f0; border-radius: 4px; display: flex; justify-content: space-between; align-items: center; }
  .cfg-section h3:hover { background: #e0efe0; }
  .cfg-section h3 .toggle { font-size: 0.7rem; color: #888; }
  .cfg-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  .cfg-table th { text-align: left; padding: 0.4rem 0.6rem; background: #f8f8f8; border-bottom: 1px solid #ddd; font-weight: 600; color: #555; }
  .cfg-table td { padding: 0.3rem 0.6rem; border-bottom: 1px solid #eee; }
  .cfg-table td:first-child { font-weight: 500; color: #333; min-width: 200px; }
  .cfg-table input { width: 100px; padding: 0.25rem 0.4rem; border: 1px solid #ccc; border-radius: 3px; font-size: 0.82rem; text-align: right; }
  .cfg-table input:focus { border-color: #1a3a1a; outline: none; box-shadow: 0 0 0 2px rgba(26,58,26,0.15); }
  .cfg-table input.changed { background: #fffbe6; border-color: #c0a000; }
  .cfg-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; gap: 0.8rem; flex-wrap: wrap; }
  .cfg-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .btn-cfg { margin: 0; padding: 0.5rem 1.2rem; font-size: 0.85rem; }
  .cost-summary-row { display: flex; gap: 0; border-bottom: 1px solid #ddd; flex-wrap: wrap; }
  .cost-summary-cell { flex: 1; padding: 0.8rem 1rem; text-align: center; min-width: 120px; }
  .cost-section { overflow-x: auto; }
  /* ── Responsive: Tablet (max 768px) ── */
  @media (max-width: 768px) {
    .header { padding: 1rem 1.2rem; }
    .header h1 { font-size: 1.2rem; }
    .container { padding: 0 1rem; margin: 1rem auto; }
    .card { padding: 1.2rem; }
    .geo-grid { grid-template-columns: 1fr 1fr; }
    .view-grid { grid-template-columns: 1fr 1fr; }
    .tab { padding: 0.5rem 0.8rem; font-size: 0.82rem; }
    .cfg-table td:first-child { min-width: 140px; }
    .history-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .detail-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  }

  /* ── Responsive: Mobile (max 480px) ── */
  @media (max-width: 480px) {
    .header { padding: 0.8rem 1rem; flex-direction: row; align-items: center; gap: 0.8rem; }
    .header-logo { height: 36px; }
    .header h1 { font-size: 1.1rem; }
    .header .sub { font-size: 0.75rem; }
    .container { padding: 0 0.6rem; margin: 0.6rem auto; }
    .card { padding: 0.8rem; margin-bottom: 1rem; }
    .card h2 { font-size: 1rem; }
    .upload-zone { padding: 1.5rem 0.8rem; }
    .upload-zone p { font-size: 0.9rem; }
    .params { grid-template-columns: 1fr; }
    .btn { padding: 0.6rem 1.2rem; font-size: 0.9rem; width: 100%; text-align: center; }
    .tab-bar { overflow-x: auto; -webkit-overflow-scrolling: touch; flex-wrap: nowrap; }
    .tab { padding: 0.5rem 0.7rem; font-size: 0.78rem; white-space: nowrap; flex-shrink: 0; }
    .geo-grid { grid-template-columns: 1fr 1fr; gap: 0.4rem; }
    .geo-stat { padding: 0.5rem; }
    .geo-stat .value { font-size: 1rem; }
    .geo-stat .label { font-size: 0.65rem; }
    .view-grid { grid-template-columns: 1fr; }
    .result-header { flex-direction: column; gap: 0.4rem; align-items: flex-start; }
    .result-header h3 { font-size: 0.9rem; }
    .dl-row { flex-direction: column; }
    .dl-btn { text-align: center; width: 100%; display: block; }
    .batch-summary { flex-direction: column; gap: 0.5rem; }
    .batch-summary .stat .num { font-size: 1.2rem; }
    .history-table th, .history-table td { padding: 0.4rem 0.5rem; font-size: 0.75rem; white-space: nowrap; }
    .detail-table th, .detail-table td { padding: 0.3rem 0.5rem; font-size: 0.78rem; }
    .detail-table th { width: auto; min-width: 90px; }
    .cfg-section h3 { font-size: 0.85rem; padding: 0.4rem 0.5rem; }
    .cfg-table { font-size: 0.75rem; }
    .cfg-table td:first-child { min-width: 120px; }
    .cfg-table input { width: 80px; font-size: 0.75rem; }
    .file-chip { font-size: 0.7rem; padding: 0.2rem 0.6rem; }
    .nesting-box { padding: 0.6rem; }
    .nesting-box h4 { font-size: 0.85rem; }
    .cfg-header { flex-direction: column; align-items: stretch; }
    .cfg-header h2 { font-size: 1rem; margin-bottom: 0.3rem; }
    .cfg-actions { flex-direction: column; }
    .cfg-actions .btn-cfg { width: 100%; text-align: center; display: block; }
    .cost-summary-row { flex-direction: column; }
    .cost-summary-cell { border-right: none !important; border-bottom: 1px solid #eee; padding: 0.6rem 0.8rem; }
    .footer { padding: 1rem; font-size: 0.7rem; }
  }

  /* ── Responsive: Small phone (max 360px) ── */
  @media (max-width: 360px) {
    .geo-grid { grid-template-columns: 1fr; }
    .geo-stat .value { font-size: 0.9rem; }
    .header h1 { font-size: 1rem; }
    .tab { font-size: 0.72rem; padding: 0.4rem 0.5rem; }
  }
</style>
</head>
<body>
<div class="header">
  <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAbMAAAB8CAMAAADU+q5gAAAABnRFWHRUaXRsZQCo7tInAAAACHRFWHRDb21tZW50APbMlr8AAAAHdEVYdFNvdXJjZQD1/4PrAAAACHRFWHRXYXJuaW5nAMAb5ocAAAALdEVYdERpc2NsYWltZXIAt8C0jwAAAAl0RVh0U29mdHdhcmUAXXD/OgAAAA50RVh0Q3JlYXRpb24gdGltZQA19w8JAAAACnRFWHRDb3B5cmlnaHQArA/MOgAAAAx0RVh0RGVzY3JpcHRpb24AEwkhIwAAAAd0RVh0QXV0aG9yAKmuzEgAAAAJcEhZcwAADsQAAA7EAZUrDhsAAABOUExURUdwTAAAAAAAAAAAAAAAAAAAAAAAACIiIgAAAAAAAGZmZmZmZgAAAAAAAAAAAAAAAAAAAGZmZgAAAAAAAGZmZmZmZmZmZmZmZgAAAGZmZt1+vw8AAAAYdFJOUwBLYhvnhDYRCPY8wnWWxyfXfrip51mmjywYhbkAABZ5SURBVHja7F3pjuQoDG5ukCBE5E+9/5NuUrkMmCPVVTvVUpjVzHbnAPxhYxvb+fm5293udre7FZv+WLtp+2nM1PG3Aj/v/zR+jJ/Z//3x6qbv54ADf6e/zS5r/HL+AwnyJu3famR83Jj9rSbHx43ZH+Oy4fEwfZgpLwmx1hJCJENUF7VcmP8Q4rNrfr8m4TV2PlFUhdjSK7WWLs+2FKajGyI792gm10k9X6/6aEC7b5/3IKX0ByDrwkx74sLIzXK74cNEJUtvodzwpRk+pS9Ujm9tFOdMNd1/ywMpkOjo9cGXXn2VAn7a32dG20FR5qkbjtePQdRxgKMxz9tZW4yJGV/p58bYe+B7QtaBmZZ24g/YTHA0HrF251WaEnM8Lo0MIHk+ITAaERHGqFee9RqvGnDr2JwUoy7Ek3oMkyCqSAMxxaN5jPPSbRCOPuGd2xDcAt/Mn78DTg6PHsw0o8MDaY5A+mlRxoyB5wFm4AmL0YgjnZqJlFCDa+BhbEPKEWewSXGHvl97O2K3j7QusCnsZJZQswSg+veQtTDTBEXsST+waN6M2Sw4i736sjp1tlAVjt49Hj2zatLgMZJaR4QjD/wCsvDowUzTsTi9RyD6M5gpy8u9Dqh8Ya6fNMfc8fdTdWE0prZ1SoR4o/w1ZHXMtDC16RmrP4GZctVeUfkiY1YwTtVs0moz9MpoHo5dwuwR/GuQ+XOl1TBTdcjmh3dOeydmzV4xJkqF6SBfhWyeVMzAjdFUQJOoTA3sJcgm04OZbhHvwckH+KwmikrixafijtMra7+MgRat0TyMUJcwM9MLoDEAWQWzTBGYFVbhYsKo92OWcczS6xgtFF3X0NbHcBHEUnCfk4JdxkqOzQ0C4SaTbBEl3sB1F+MugxZzexkzH63IQRCv5iapDdl29kbM4nkOlvjZHGWSHL1yZDtTuSLIbVt0cEe3SRG64xYLVRlBNjoq59HM5jWNYCvpFbKgb14GLdlTi5hpGwmM0x+k/CovoEx4G2bR6LjzsNcxWSh14ky6pX0P9PTCaUaejoOY/NrFxuHhy9A+kpkFDPxwWZx26RUlzDScHrcsNkmXRQ9Vs7dh1uwVmy2q2mGaipogqInR4GczjJMixMb6eDRjUdVs8dkyNf06ZEXMoMGT9yADnz7hB4GyKzd9ZBjR/fvUKyYaTEXdh3slonNLR4sQ56+DoE2qRcNeHalHey9hBjUxZETSxkLkTZgBrQ5TrzxBNYsTCgJYI99lIAao1Z0sTc+roxFN08JPlwyWXu29hBkQC7w9PYhZercK/ZjZHgsr14OPkUrQWy6wIAaiQzjRR5UGYLsy4ueidOydn7K53C9gBjWxDi3nXXx2qmMlKiDtmNVzaz+0c5M6HTW55kICNMBtKmBhlAzlijnYBRoGWQkzIBo5+bmEmfNxg3K/gZkeXxD4OgA2gwIiZTRlu3xbGL1xScbGtqijZZu8w4uFQlbCDDB1j4MMYsZD1KbBdGPGXhGNJ2kHHVnNJhTt6a7dhLT8TXpqL7GaM6npxVIUfbiAGWCOHlcLxKzWGpjJc4hDrwVzKvqbP+Jc2TxebbLpJcmOLOt6IZh1WSzUfMwN0mocshJm9tJ29i7MCNgguo/beSIQzv0zUTQafKPV1jDMdMPtUBHlaiqDVhXRJci+CjP9CmbCpG4/V9A0SJVvFBFuaZPzuSMIxwxQiVdOxv1Q8S/X1Nbiwa9sqbnfzWensnToSmCukTOAVIXS+SL7Cp/Za+fVHfxZ1l6+aT/TL+xn58wOeXfaazGtZVVlO7lhk6gdmIk+NVfTF0CrPPN+vfEXfMZOzMa+41wVEJMEaCHw3AZ4JQbZ5rP36I0t5dEUTKlaUEfbPhvJ/4aZHkAEQZ+izxHGBAsObvLARkZ8tKyGGW55qKHXnKwpjyh9a5CVMAPypcf+hJgFS6ldwm3X/yg8UWz5QcJVP8i5gDklhD7/EHBOAtkVKhU54+R8Bv0K6HYFRHlrYftwKVaiHgBRwAzqmR1+nsh3laS4XfFduWuxLhr6lPm4hyvzwikJqdEp57MIY2w0onS6fTGmIePiRsxKyUcM+8AYTXX7iPUFHzHYeI3r8OLmItlUHESs5qfPMYN8hMUPQDd6O9a0hkNi4jcjw2TLWY5JBmUn2ovZFT6D9me+RSga9xq9uhnPFx87c9HGDOxXVcbs8rTpbs9jOzJMdngn05BCNu8YkZLwtnNqaPqnZFp6jbUsatpqD2QoKDxS1kAwi9S9FJYorKbH5axtabQBnphXopab8SDReR0XHnoMntwwkA9gFvkMRhv3yhMgO9gsfiBSb7lg+GZgcc1hiOK9pDNXDzBLx9bRXqibXFaLuyLRsghLyJHWSwjUrpYNn4j9jiSImQjSa2GEPfGKkSYwv32LEdLMk1Mq28JouNiT4JSnIfJA9UV3oMpjkoHAbJimMHBuXsEs8W2aYVpCAaeBg8Cl98d+63gxcqRXgpzL9gTO5t6FMTgrvaciSmyycFePaDBOYkknoyLOgxp6w7mRY+s0bk7Pa3ReotSKMJrLmDVyESIr4I0xqe1e97R9IEX4kDSo70P7WWfG7RBC8itbo/IYsvsvxONkGn/F+GXEjeYqZjXf5m79fCBevynQNxqBNyyKrQJtNgqB+hBpZb65JOLhtLcXcyHuLT1e4XXlRU78KmY1P2W8i781L4Y2LMoths5XXaK+5AuUQ2sXjDFoGkuX4ktjz2PTxTQrkeYiZjP5KhMEactvxaya9HbaHdRUhRMtnbzIUE/QSpXA+hLi10KCI89jj+7iJ3MRs8oEDTRY3pznWTFRjho0QMihFi0ItU+cb/X0pLzIjaz6Cq8m255qTZ+6ibiXW/nUvpDMM1D/8zHMfrwr9HpabLQRVgG93AkvFFLEVwc38i5mC6zG3QtJ7btM77UQctCadQs0QbhzTAZbwwyayR41MBEXPqOBV3uFaY+0td2nwlN7h6EwBosntWtUGeATfaVY2JYJ0b8PsukqZj9aSRGSFKC0lAbALPMJAMwGlM9QmmsmRcwNca/wIL2QajZUVsVsFSewDa5WgERlBTeCkC+W/NBL2Kyx6jJn9mP2nCGxs2EbwuScsAQpxOOtWJvNM/mJOK5FQnf/baHqh15y3Z69hmevMYUYPZ73pYOa/Q4nJLYoljk9JzUt1Tt8A4H5firW4Sz3k18U5JvX6xXIMv29s3bSbP4wxir1ZIBt1Hmt9kRHr7r5uFaNLvTy+qVIDlNdFDzuV+p3FRSZuCZVU5fUXaPsH7SrUjX2btyY/QmM3Y3ZX2tR1u2N2d9okb5/Y/Y3mjU3Zn+tRSn3N2Zft3mh9tJ0Y/a1GuJyRk3z+sHw/P7G7KsQ80tIgTFjyIxucWP2nRr96dM0aZVmam7Mvq8lhwdjMeTixuxbmCyLCeD2xuy7dzIkRS0C7cbs+2xm3oiBBt6rG7PvMJnxaAdwSExvzP5p82kZf1YqZuZuXf87GpvS4K5iRuFR0Pq2qf+tuuFMGgJTDv7dpSOMf74x+ycaYhJ0Val1vkU1Q1RvzP6JUp9Eq1cwW7P448/h3Jj9z5BhEZe1xIgno9HOc2r2LBkRXVdk+VUUDrdEtNGzzf9/xnVu1+z53/y+7KBheSmlWETuMgJa+4yO8vT5zbnUDc6I3TqeX1yOatNyuUei3+CzcFrgJmRO1F+FLK24VUvkWD5AFn8WoJIzuB6NRsHwK9xR/DsJ5Sw4EjoqTW4pFcjHYNcM5HJpRE9E2HIl4yjZ6LM84+RK37xaraIBibnP0riOwFdsTk5dhSxL1qkljKVOkgpmLouG37+ZBvvLM7sPlGHFqNMhk3a4vzQPX99qExWKTigCfaqjALikX6swAxpPuM8wz9HQrpgtipVB6v1uD4PlZOGsqp95GRg1j2uYnQhphwRr59vnRcz24eaFnzbM8MB2ln0WhFRMVDQ6freK8uIfeS2oY3TD42XMlCilDFeFI08TTtqYnZy/19/FMpTN2Q4xogl//uLoa2lp8vFR1Ne4wmpH80eYqDh6dsxA70j1o1MXyxJrdszOSR0LkY5wTtuMu74lomxZoHbmhndjtg/3YJsIs3W5c2f3iH3wVUxGhXNC7FAv2ewulVLHCsvqdNcwO7NeDgKac1tfMTPhORxeKlR3loAeSAEzQbdZncP21M1zcsOeabFU6uwqNZHWB+lntBcw26s1HKY4wmeA3nGSkz7G8yyiolP6gwS+TDhWMNs37cFKpaRd8nbArrRitm3zWwZdpsoAyZ3lyG6YBTyzSZ9ijmBz6oIs4e7eyny9mK164jlHhM+m2nBXAnNU6MMjhoxyRcz27DW3Kh6aiSildsOMwGWRCUfoDUoTsFYKmqmMBk2TIFst19Xi0nlyeCtm6x59/ozwmau6sSuYQYUpVU/KmG0TPPd+RqDCHmO28nlaXDAqIZAlFb4dM8zTEZefouadmK20OX3PmGxkP2vpP1UUZBzradXKhXxOKdXfyphtqZK06DiPhvnsP1Vx1ukFsr5qQuvkVaTHRcxwR0dkZDD3VswWYqrCxrMuIHNUU3G532LFDC0D+ZzL/L6VFSfWh9l/7V3pduMqDHY240CahMbY4/d/0YuQwGLz0qbn/gkzc5qxCRL6EFrsCgpnro9tmBX1DJm6dc2roGiE2f3RwW+/nR55KmUfZrXcVATaajmWXZjBMmSPeBaqEf0r1Qmv6xmWE7buPxJLyqNUMSNf/twtY9Z6w1WwZ7gE4fEUqvir9Pvh1/u3X4mP32BWL1fBPVZxeStmz0NXO6Qo26jzALOuZ25pOYfykLqfWzBrFjG7Hk+gIy3+kmTileIShC0RPz3bxZg624b3YLZUYeR5Oe12Q9Ywu7sTSq/319MVDricV/WshllBz9hzCRJz+0Y9+/eEEm3f91JSll7xdBP5xpi7W8Is8zp3YLZSXPg1H+q2rVDeKmYXfn7ZBU9OKtozv4t837bbM5zNC/wXssBRiqluz/DGij1bKtVCpZVaKJj8yk1LjtnP9Wy9ftfr6E6aFaeNFm0NMxszhcjCasqpomf39tFiKzz5qOrZF5X9gFzCPXf3634jsnQ9bMYsyXT4QrOvS0jTRE7rG+3ZlgTH9QlHLl9ez/fomVWCsM1+d3hU389i6lzPHoXpcEVbiM/wDN3Zz+xOpyXM0mr9BUWMUmfeb2xPdIhb9nsqWzHbk5N6l68PGxfFvbA/PCp6tiUP0hat2UJCp44Z5TBCyd3T8cWr73p797zfn9fS05aSJM+HPKb+bXzWtX8A2RbM/AzhHIGqnrm8G1XU36hnJTWLJedtHEbrLFz3yTtrwOHI8QP4SUxDyaG52djqQI5+t7pauLvvY+rf5kF2JKTejBmJAILSmp49j4cDnCNh/x1bsUnPKHq4nn27hpURY/aEVxJcO+WByPPrdqPnS7M+8vgMV3oxiZ5QZsm1N2H2N5AtYXYJe9UxVNyu6VmxwuKynpGn+HVoW8C7fVxSLyR7WnwsZ3grz89opK90KXgX5ny0lC3ttr09Ey/kPfnGP4Jsk55hZXuHxGM9PstfBijqGZW8YNEcleKa97gUs8i1y6oqMnc8yl3Rk6BbmuRks8BXX64zL2/Rsw1ldN+NGeoBzuN0pPelsJLlsZKYr2B2KGxPfkbMCtH5LLPsxSt9eBFVuH8lZd3Tsf1Al2sCKV7gD8dwVc3HndBq+V7F7PE/QLZNz+YnmU4XVt7hOadRdVHPCnaGqveFx2jZ3pikDA/hRYlrnJlGRfLhG/luTxHfveX7dHD3afks6Bk9DjgtQHb9K8zOC7XaodBe8vJfB4X9ouJ9DyjKZ/2DC/1NbqNwXSHA5KyPY1Y5EKr32e+3MQc0MHzMB77Bs/28PGF7gxqDfuyDG/Ywz+ErnYT7woW9I4C1DRfe10X262VSH69/f9cW+BKFmnddVlmvE0nL59GVLpf6ptf4qAtlBvP4IhqnS4btNlDuVqoUYof63cuVtzNrT97mEwG+7/xQ77l9vb54u8Cf7U/HP21761oX/OQNPdVDW2yP9hE19/COt/g05k/7C+SSN5r4na7pPgL6tE/7cxX8tE/7tI+efdqnfdqnfdqnNY2QEmJfKeXmr0i5p/c6/aot1drU4nI9DSlX06Q3UzVab5iBmqaI0XzemvfwjPT9WjZhQ5fF1rvZ95kM6m2C1r8JNT2p6sSAjtwgzr2YCQUj632YSTftaR0zXWV6T5fFNk6TaMw0mX2YTX1VIlUU9mFmIVO0HvtMMlLpn2M22JEVSm1YEt7PMBPDsEBZrnXZtOhgrkMdghJm8IUqyPo9mCEGkj5u2LG3Y9YDTUkfd2AmSwqzc1N7z/akJiVovgJWIIpJwc9RjfhZKEYMOpOEhLbaMDhdGJUx/QC9p14Z/JZdCiRH2693IBs12t08wwwuK+qSyshxNabUVBPT8JjJwfexA+JsOnup1/GiCzJ3A9kuPc2cDVDBjLEBm5z9IvZWcoSBjcKBYMo66+7I+S7AoeeVS2vdHtspwP5oR++DpUIOlROo/Txw7hlmuGEogr6HaxPZCnvLmQ1DzNJHbRHlqkWY2cuOuOD6P4TlPnlGZmpTE9Pwq6j3ffCT8QMMfLWTC6DDdue69CIaoIYZY0MjAwrFMrguZKy0Jzp3D+SoC90RyUy2bI52CjgjO5Q2vRcPx2wakmXqDaB1v0ZkQMFalkgdXFH7szca2bbyH2kw4Hs0BcwmZeINV3F9QYctotbENAiz0UpeunFAMCKgyT1QTYJyGuwcSJiOlcMYDVDdG2c2YCRgT6CZV1oEQGBjMTHXgZz2k3Dc62QmG00yyspJ1fkkKWYqtmdqDEs1TEb5OVFv6RjAFWDgnvZLTBfsmQYWRHQP/EaV7VEywSzQIMwUXtG4fyQay/xG9Hu9AzeCPR/cOPMAJR9ExWw4tglh0lMa0y3TlGtPjneZ9zI/k03xlncC3RhOvilmOvcbUSOkNSxh0TQJZrqZlUtRv9TNnTGT3NAE0CLMOLUpoUFc2rko5eYOm5wWkcZGoAkmRIXfmqIBqpjNbLjvC095YI68nPeMpHvSZcxmstERVLNtlgwzvk/y7r0apA8UfBAV6JUx8/1ST2sBs6ZT8R4VUatiFmRrvKIC+Ek8OaAazJgFT54NkGKmMaZmbKAj4CnrGDPZpFxnmAWB/AKzRM+mCmZ69hOUkJswq3nHS5j5FUyyi6lVMdOROrn/deOUSqP3jMmmiYTFBijbM85GrGdFzLLu79Wz2J45+7KMmSR3ZA0zFrL/ArOYWgWz2CSE/w1pIKUizIbZY2UDlDHjbDh7Zrw945gF25x0r9uzn2EW+40KApVVzEYxJJhZ90TKmAvhYhij92AmlZHkSMIPmVKrYOZ8HAHemTFCOIdYWqsWJQ2sf6txbwQ/hVzLQQhjogHqmAU2Yr+RY4Z+o467e3JFv/FnmGEeDlnDKGUFM9dLJZgNIT6budA8RtmI2WzzDX2IqFUww/CKDMXk96A4vTjNwZiPz4bQZx6g5uszNiisVE2OmZlyrkUSn/WBkd2YKTWyjAOtJlgExudBZNzdMNdOCe1uj34Q8hyk6+avGshJGEwBxBE9dsDMABvZ8hLSB7BfDyk1yoMYlq0xPqPRw/ekJymSNEgz9mGadlvpQ6IGHaswABEJeu+lwNgwSrM8iGkaluQwlAdh3T25Yh6ESavY/gNYzmJySak5AQAAAABJRU5ErkJggg==" alt="Chicago Metalcraft" class="header-logo">
  <div>
    <h1>CMC Quoting Toolkit</h1>
    <div class="sub">Sheet-metal geometry extraction and quoting data from STEP, PDF &amp; DWG files</div>
  </div>
</div>
<div class="container">

  <!-- Tab Navigation -->
  <div class="tab-bar">
    <div class="tab active" onclick="switchTab('upload')">Upload &amp; Analyze</div>
    <div class="tab" onclick="switchTab('history')">Recent Jobs <span id="historyCount"></span></div>
    <div class="tab" onclick="switchTab('config')">Shop Rates</div>
    <div class="tab" onclick="switchTab('changelog')">Revision History</div>
  </div>

  <!-- Upload Tab -->
  <div class="tab-content active" id="tab-upload">
    <div class="card">
      <h2>Upload files</h2>
      <form id="uploadForm" enctype="multipart/form-data">
        <div class="upload-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
          <p id="dropText">Drag and drop .STEP, .STP, .SLDPRT, .IGS, .PDF, or .DWG files here, or click to browse</p>
          <div class="hint">Max 100 MB per file. Multiple files supported. STEP for 3D analysis, PDF/DWG for drawing extraction.</div>
        </div>
        <input type="file" id="fileInput" name="step_file" accept=".step,.stp,.STEP,.STP,.pdf,.PDF,.dwg,.DWG,.dxf,.DXF,.sldprt,.SLDPRT,.sldasm,.SLDASM,.igs,.IGS,.iges,.IGES" multiple>
        <div class="file-list" id="fileList"></div>
        <div class="params">
          <div class="param-group">
            <label for="material">Material</label>
            <select id="material" name="material">
              <option value="7.9">Stainless Steel (SUS304) - 7.9 g/cm3</option>
              <option value="7.85">Mild/Carbon Steel - 7.85 g/cm3</option>
              <option value="7.85_galv">Galvanized Steel - 7.85 g/cm3</option>
              <option value="2.71">Aluminum 6061 - 2.71 g/cm3</option>
              <option value="2.68">Aluminum 5052 - 2.68 g/cm3</option>
              <option value="8.96">Copper (C110) - 8.96 g/cm3</option>
              <option value="8.53">Brass (C260) - 8.53 g/cm3</option>
              <option value="custom">Custom density...</option>
            </select>
          </div>
          <div class="param-group" id="customDensityGroup" style="display:none">
            <label for="customDensity">Custom density (g/cm3)</label>
            <input type="number" id="customDensity" name="custom_density" value="7.9" step="0.01" min="0.5" max="25">
          </div>
          <div class="param-group">
            <label for="quantity">Quantity</label>
            <input type="number" id="quantity" name="quantity" value="1" min="1" max="100000" step="1">
          </div>
        </div>
        <button type="submit" class="btn" id="submitBtn" disabled>Analyze files</button>
      </form>
      <div class="progress" id="progress">
        <div class="bar-wrap"><div class="bar" id="progressBar"></div></div>
        <div class="status" id="progressStatus">Uploading files...</div>
      </div>
      <div class="error" id="errorBox"></div>
    </div>

    <!-- Results -->
    <div class="card results" id="resultsCard">
      <h2>Analysis Results</h2>
      <div class="batch-summary" id="batchSummary" style="display:none"></div>
      <div id="resultsContainer"></div>
    </div>
  </div>

  <!-- History Tab -->
  <div class="tab-content" id="tab-history">
    <div class="card">
      <h2>Recent Jobs</h2>
      <div id="historyContent">
        <div class="empty-state">No jobs yet. Upload a STEP file to get started.</div>
      </div>
    </div>
  </div>

  <!-- Shop Rates Config Tab -->
  <div class="tab-content" id="tab-config">
    <div class="card">
      <div class="cfg-header">
        <h2 style="margin:0">Shop Rates &amp; Cost Parameters</h2>
        <div class="cfg-actions">
          <button class="btn btn-cfg" id="cfgSaveBtn" onclick="saveConfig()">Save Changes</button>
          <a href="/config/template" class="btn btn-cfg" style="background:#1a6b3a;text-decoration:none;display:inline-flex;align-items:center;">Download Template</a>
          <label class="btn btn-cfg" style="background:#1a4a8a;cursor:pointer;">Import Excel
            <input type="file" id="cfgFileInput" accept=".xlsx,.xls,.csv" style="display:none;" onchange="importRatesFile(this)">
          </label>
          <button class="btn btn-cfg" id="cfgResetBtn" onclick="resetConfig()" style="background:#8a1a1a;">Reset Defaults</button>
        </div>
      </div>
      <div id="cfgStatus" style="display:none;padding:0.5rem 1rem;border-radius:4px;margin-bottom:1rem;font-size:0.85rem;"></div>
      <p style="font-size:0.82rem;color:#666;margin-bottom:1.2rem;">Edit any value below, or upload an Excel file to bulk-import rates. Download the template to see the expected format. Changes are saved to the server and used for all future cost estimates.</p>
      <div id="cfgContent"><div class="empty-state">Loading configuration...</div></div>
    </div>
  </div>

</div>
  <!-- Revision History Tab -->
  <div class="tab-content" id="tab-changelog">
    <div class="card">
      <h2>Revision History</h2>
      <div style="max-width:800px">

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.9 - September 18, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">SLDPRT/IGES Format Support</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Accept SolidWorks native files (.sldprt, .sldasm) with auto-conversion to STEP</li>
            <li>Accept IGES format (.igs, .iges) with built-in OCP conversion to STEP</li>
            <li>Graceful error handling with clear SolidWorks export instructions when conversion unavailable</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.8 - September 17, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Bend Detection, Quote PDF Export, Material Nesting</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Auto-detect bends from STEP geometry with per-bend angle, radius, length, and direction</li>
            <li>Bend Schedule table in results showing detailed bend-by-bend breakdown</li>
            <li>One-click Customer Quote PDF export with CMC branding, cost breakdown, and bend schedule</li>
            <li>Material nesting simulation across 5 standard sheet sizes (4x8 through 5x12)</li>
            <li>Nesting comparison table with utilization %, layout, and scrap estimates</li>
            <li>Quantity-aware nesting recalculation for batch runs</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.7 - September 17, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Hole Detection Accuracy Fix</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>GUARD 3: Full-circle cylinder faces are now standalone clusters, preventing transitive merging through shared planar faces</li>
            <li>Capped dedup threshold at 15mm (was scaling to 80mm+ on wide parts, incorrectly merging distinct holes)</li>
            <li>CA260504D-PX04 hole count fixed: 24 detected -> 34 detected (17 round + 17 square/rect)</li>
            <li>Added pipeline debug diagnostics (_debug field in API response)</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.6 - September 16, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">CMC Branding</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Added Chicago Metalcraft logo to site header</li>
            <li>Responsive logo sizing across desktop and mobile viewports</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.5 - September 15, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Cost Engine v3.0 + Revision History</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Shop-calibrated laser/waterjet speed tables with auto machine routing (HSG G4020X, OMAX 60120)</li>
            <li>Assist gas auto-selection: O2 for carbon steel, N2 for stainless, compressed air (22TK) when eligible</li>
            <li>Burden-based all-in rates with full breakdown (labor + gas + electricity + consumables + depreciation)</li>
            <li>Smart press brake routing: Adira 160T vs Guifil 110T by tonnage and bed length</li>
            <li>Second operator rule for parts over 48 inches or 50 lbs</li>
            <li>3-tier deburring: Apex Time Saver (304 SS) > Grizzly Flap Wheel (>=0.5 sqft) > Hand</li>
            <li>Welding bench/off-bench routing with fixture tracking</li>
            <li>Setup itemization per operation (programming, material pull, staging, accounting, QA)</li>
            <li>Production ramp table for quantity scaling</li>
            <li>Frontend: per-operation annotations (assist gas, tonnage, speed, weld location), expandable burden/setup rows</li>
            <li>Added this revision history page</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.4 - September 14, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Feature Detection + Responsive Design</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Countersink, counterbore, and chamfer detection via cone face analysis</li>
            <li>Tap drill and clearance hole lookup tables with hardware hints</li>
            <li>Feature detail table with position, confidence, and hardware callouts</li>
            <li>Responsive mobile and tablet layout</li>
            <li>Excel upload for bulk shop rates import</li>
            <li>Fixed false hole counts (aspect ratio filter for slot/notch detection)</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.3 - September 12, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Shop Rates Admin + Cost Engine v2.0</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Built Shop Rates admin panel with live rate editing</li>
            <li>Refactored cost engine to load config from SQLite</li>
            <li>Cost API endpoints for rate management</li>
            <li>Dev branch with separate Railway deployment for testing</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.2 - September 9, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Cost Engine v1.0 + Complexity Scoring</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Initial cost engine with laser, brake, deburr, hardware, passivation operations</li>
            <li>Quantity break pricing with ramp efficiency</li>
            <li>Process time and cost complexity indicators</li>
            <li>Nesting estimate in results</li>
            <li>CSV and PDF export buttons</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.1 - September 8, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">PDF Drawing Extraction</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>PDF and DWG file upload support for drawing extraction</li>
            <li>Per-page drawing analysis with hole/feature callout parsing</li>
            <li>Flat pattern diagram and STEP-style report for PDF results</li>
            <li>Improved projected drawing views (darker/more visible)</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v3.0 - September 4, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Advanced Geometry Analysis</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Gauge auto-detection from material thickness</li>
            <li>K-factor lookup from CMC bend tables</li>
            <li>Round, square, slot, and obround hole classification</li>
            <li>Multi-cut cross-section flat pattern computation</li>
            <li>Bend deduction and flat length calculation</li>
          </ul>
        </div>

        <div style="border-left:3px solid #2e7d32;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#2e7d32">v2.0 - September 3, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Dual-Path Analysis + Web UI</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>Sheet metal vs machined part classifier</li>
            <li>Machined parts analyzer</li>
            <li>Batch multi-file upload with drag and drop</li>
            <li>SQLite persistent job history</li>
            <li>Multiple material support (stainless, carbon, aluminum, copper, brass)</li>
          </ul>
        </div>

        <div style="border-left:3px solid #888;padding-left:16px;margin-bottom:24px">
          <div style="font-weight:700;font-size:1.1rem;color:#555">v1.0 - September 2, 2026</div>
          <div style="color:#666;font-size:0.85rem;margin-bottom:6px">Initial Release</div>
          <ul style="margin:6px 0;padding-left:18px;color:#333">
            <li>STEP file upload and B-Rep face classification</li>
            <li>Flat pattern extraction with bend detection</li>
            <li>3D preview rendering</li>
            <li>PDF quoting report generation</li>
            <li>Deployed on Railway with Docker</li>
          </ul>
        </div>

      </div>
    </div>
  </div>

<div class="footer">Chicago Metalcraft Quoting Toolkit v3.9</div>

<script>
// --- Utility ---
function hideParent(el) { if (el && el.parentElement) el.parentElement.style.display = 'none'; }

// --- Tab switching ---
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'history') loadHistory();
  if (name === 'config') loadConfig();
}

// --- File management ---
const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const submitBtn = document.getElementById("submitBtn");
const materialSel = document.getElementById("material");
const customGroup = document.getElementById("customDensityGroup");
let selectedFiles = [];

materialSel.addEventListener("change", () => {
  customGroup.style.display = materialSel.value === "custom" ? "block" : "none";
});

["dragenter","dragover"].forEach(e => dropZone.addEventListener(e, ev => { ev.preventDefault(); dropZone.classList.add("dragover"); }));
["dragleave","drop"].forEach(e => dropZone.addEventListener(e, ev => { ev.preventDefault(); dropZone.classList.remove("dragover"); }));
dropZone.addEventListener("drop", ev => {
  const files = Array.from(ev.dataTransfer.files).filter(f => {
    const ext = f.name.split('.').pop().toLowerCase();
    return ['step','stp','pdf','dwg','dxf','sldprt','sldasm','igs','iges'].includes(ext);
  });
  addFiles(files);
});
fileInput.addEventListener("change", () => { addFiles(Array.from(fileInput.files)); });

function addFiles(files) {
  var rateFiles = [];
  files.forEach(f => {
    var ext = f.name.split('.').pop().toLowerCase();
    if (['xlsx', 'xls', 'csv'].includes(ext)) {
      rateFiles.push(f);
    } else if (!selectedFiles.find(sf => sf.name === f.name && sf.size === f.size)) {
      selectedFiles.push(f);
    }
  });
  renderFileList();
  // Auto-import Excel/CSV as shop rates
  if (rateFiles.length > 0) {
    rateFiles.forEach(function(rf) {
      var fd = new FormData();
      fd.append("file", rf);
      var statusEl = document.getElementById("cfgStatus");
      if (statusEl) {
        statusEl.textContent = "Importing shop rates from " + rf.name + "...";
        statusEl.className = "cfg-status info";
        statusEl.style.display = "block";
      }
      fetch("/config/import", { method: "POST", body: fd })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.error) {
            showCfgStatus("Import failed: " + data.error, false);
          } else {
            var msg = "Imported " + data.total_sections + " section(s) from " + rf.name;
            if (data.changes && data.changes.length > 0) {
              msg += ": " + data.changes.join(", ");
            }
            showCfgStatus(msg, true);
            fetch("/config").then(function(r) { return r.json(); }).then(function(cfg) {
              if (!cfg.error) { shopConfig = cfg; renderConfig(cfg); }
            });
            // Switch to Shop Rates tab to show results
            if (typeof switchTab === 'function') switchTab('config');
          }
        })
        .catch(function(err) { showCfgStatus("Import failed: " + err, false); });
    });
  }
}

function removeFile(idx) {
  selectedFiles.splice(idx, 1);
  renderFileList();
}

function renderFileList() {
  const list = document.getElementById("fileList");
  if (selectedFiles.length === 0) {
    list.innerHTML = "";
    submitBtn.disabled = true;
    document.getElementById("dropText").textContent = "Drag and drop .STEP, .STP, .SLDPRT, .IGS, .PDF, or .DWG files here, or click to browse";
    return;
  }
  submitBtn.disabled = false;
  document.getElementById("dropText").textContent = selectedFiles.length + " file(s) selected. Click to add more.";
  list.innerHTML = selectedFiles.map((f, i) =>
    '<span class="file-chip">' + f.name + ' (' + (f.size/1024/1024).toFixed(1) + ' MB)' +
    '<span class="remove" onclick="removeFile(' + i + ')">&times;</span></span>'
  ).join("");
}

// --- Submit ---
document.getElementById("uploadForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (selectedFiles.length === 0) return;

  submitBtn.disabled = true;
  const progress = document.getElementById("progress");
  const bar = document.getElementById("progressBar");
  const status = document.getElementById("progressStatus");
  const errorBox = document.getElementById("errorBox");
  errorBox.style.display = "none";
  progress.style.display = "block";
  document.getElementById("resultsCard").style.display = "none";

  // Elapsed time tracker
  const startTime = Date.now();
  let elapsedEl = progress.querySelector('.elapsed');
  if (!elapsedEl) { elapsedEl = document.createElement('div'); elapsedEl.className = 'elapsed'; progress.appendChild(elapsedEl); }
  const elapsedTimer = setInterval(() => {
    const s = Math.floor((Date.now() - startTime) / 1000);
    elapsedEl.textContent = 'Elapsed: ' + s + 's';
  }, 1000);

  // Step list for current file
  let stepListEl = progress.querySelector('.step-list');
  if (!stepListEl) { stepListEl = document.createElement('div'); stepListEl.className = 'step-list'; progress.appendChild(stepListEl); }

  const density = materialSel.value === "custom"
    ? document.getElementById("customDensity").value
    : materialSel.value;
  const kfactorEl = document.getElementById("kfactor");
  const kfactor = kfactorEl ? kfactorEl.value : "0.44";

  const allResults = [];
  const totalFiles = selectedFiles.length;

  function showSteps(filename, ext) {
    const isDrawing = ['pdf','dwg','dxf'].includes(ext);
    const steps = isDrawing
      ? ['Uploading file', 'Extracting text & dimensions', 'Classifying fab type', 'Generating reports']
      : ['Uploading file', 'Extracting geometry', 'Classifying features', 'Generating flat pattern', 'Building report PDF'];
    stepListEl.innerHTML = steps.map((s, i) =>
      '<div class="step-item' + (i === 0 ? ' active' : '') + '">' + s + '</div>'
    ).join('');
    // Simulate step progression
    let stepIdx = 0;
    const stepInterval = setInterval(() => {
      stepIdx++;
      if (stepIdx >= steps.length) { clearInterval(stepInterval); return; }
      const items = stepListEl.querySelectorAll('.step-item');
      items.forEach((el, j) => {
        el.className = 'step-item' + (j < stepIdx ? ' done' : (j === stepIdx ? ' active' : ''));
      });
    }, 2500);
    return stepInterval;
  }

  for (let i = 0; i < totalFiles; i++) {
    const f = selectedFiles[i];
    const ext = f.name.split('.').pop().toLowerCase();
    status.textContent = "Processing " + f.name + " (" + (i+1) + "/" + totalFiles + ")...";
    bar.style.width = ((i / totalFiles) * 80) + "%";
    const stepTimer = showSteps(f.name, ext);

    const form = new FormData();
    form.append("step_file", f);
    form.append("density", density);
    form.append("k_factor", kfactor);
    form.append("quantity", document.getElementById("quantity").value || "1");

    try {
      const resp = await fetch("/analyze", { method: "POST", body: form });
      if (!resp.ok) {
        const err = await resp.json();
        allResults.push({ filename: f.name, error: err.error || "Server error" });
      } else {
        const data = await resp.json();
        allResults.push({ filename: f.name, ...data });
      }
    } catch (err) {
      allResults.push({ filename: f.name, error: err.message });
    }
    clearInterval(stepTimer);
    bar.style.width = (((i+1) / totalFiles) * 100) + "%";
    // Mark all steps done
    stepListEl.querySelectorAll('.step-item').forEach(el => el.className = 'step-item done');
  }

  clearInterval(elapsedTimer);
  const totalSec = Math.floor((Date.now() - startTime) / 1000);
  elapsedEl.textContent = 'Completed in ' + totalSec + 's';
  stepListEl.innerHTML = '';
  status.textContent = "Done! " + allResults.filter(r => !r.error).length + "/" + totalFiles + " files processed.";
  renderResults(allResults);
  submitBtn.disabled = false;
  selectedFiles = [];
  renderFileList();
  loadHistory();
});

// --- Render results ---
var _allResults = [];
function renderResults(results) {
  _allResults = results;
  const rc = document.getElementById("resultsCard");
  rc.style.display = "block";

  const ok = results.filter(r => !r.error);
  const failed = results.filter(r => r.error);

  // Batch summary (only show for multiple files)
  const bs = document.getElementById("batchSummary");
  if (results.length > 1) {
    bs.style.display = "flex";
    bs.innerHTML = '<div class="stat"><div class="num">' + results.length + '</div><div class="lbl">Total Files</div></div>' +
      '<div class="stat"><div class="num" style="color:#2a5a2a">' + ok.length + '</div><div class="lbl">Succeeded</div></div>' +
      (failed.length ? '<div class="stat"><div class="num" style="color:#c00">' + failed.length + '</div><div class="lbl">Failed</div></div>' : '');
  } else {
    bs.style.display = "none";
  }

  const container = document.getElementById("resultsContainer");
  container.innerHTML = "";

  results.forEach((r, idx) => {
    if (r.error) {
      container.innerHTML += '<div class="result-section"><div class="result-header" onclick="toggleResult(' + idx + ')"><h3>' + r.filename + '</h3><span class="badge" style="background:#c00">Failed</span></div><div class="result-body" id="result-' + idx + '"><div class="error" style="display:block">' + r.error + '</div></div></div>';
      return;
    }

    // Check if this is a drawing extraction result vs STEP analysis
    if (r.drawing_data) {
      container.innerHTML += renderDrawingResult(r, idx);
      return;
    }

    const g = r.geometry;
    const env = g.envelope;
    const dims = (env.bbox_mm.xlen/25.4).toFixed(2) + '" x ' + (env.bbox_mm.ylen/25.4).toFixed(2) + '" x ' + (env.bbox_mm.zlen/25.4).toFixed(2) + '"';
    const fabType = g.fab_type || 'sheet_metal';
    const fabLabel = fabType === 'sheet_metal' ? 'Sheet Metal' : 'Machined';
    const subType = g.fab_sub_type ? ' (' + g.fab_sub_type + ')' : '';
    const confColor = g.fab_type_confidence === 'high' ? '#2a5a2a' : (g.fab_type_confidence === 'medium' ? '#b8860b' : '#c00');

    let html = '<div class="result-section"><div class="result-header" onclick="toggleResult(' + idx + ')">' +
      '<h3>' + r.filename + '</h3><div><span class="badge" style="background:' + confColor + '">' + fabLabel + subType + '</span> <span class="badge">OK</span></div></div>' +
      '<div class="result-body" id="result-' + idx + '">';

    if (fabType === 'sheet_metal') {
      html += renderSheetMetal(g, env, dims);
    } else {
      html += renderMachined(g, env, dims);
    }

    // Processes
    if (g.processes && g.processes.length) {
      html += '<table class="detail-table"><tr><th>Identified processes</th><td>' + g.processes.join(', ') + '</td></tr></table>';
    }

    // Cost Estimate
    if (r.cost_estimate) {
      html += renderCostEstimate(r.cost_estimate);
    }

    // Views
    html += '<div class="view-grid">';
    const views = [["Isometric", r.files.view_iso], ["Top", r.files.view_top], ["Front", r.files.view_front], ["Flat Pattern", r.files.flat_pattern]];
    views.forEach(([label, url]) => {
      if (url) html += '<div class="view-item"><img src="' + url + '" alt="' + label + '" onerror="hideParent(this)"><div class="view-label">' + label + '</div></div>';
    });
    html += '</div>';

    // Download buttons
    html += '<div class="dl-row">' +
      '<a class="dl-btn" href="' + r.files.report_pdf + '" download>Download PDF Report</a>' +
      '<button class="dl-btn" style="background:#1a5a1a" onclick="downloadQuotePDF(' + idx + ')">Download Customer Quote</button>' +
      '<a class="dl-btn secondary" href="' + r.files.geometry_json + '" download>Download JSON</a>' +
      '<button class="dl-btn secondary" onclick="exportCSV(' + idx + ')">Export CSV</button>' +
      '<button class="dl-btn secondary" onclick="printQuote(' + idx + ')">Print Quote</button>' +
      '</div>';

    html += '</div></div>';
    container.innerHTML += html;
  });
}

function renderSheetMetal(g, env, dims) {
  // Gauge display
  var gaugeStr = g.gauge ? (g.gauge + ' GA') : '';
  var thicknessDisplay = g.thickness_in + '"' + (gaugeStr ? ' <span style="color:#2a5a2a;font-weight:600">(' + gaugeStr + ')</span>' : '');

  let html = '<div class="geo-grid">' +
    '<div class="geo-stat"><div class="value">' + dims + '</div><div class="label">Overall Dimensions</div></div>' +
    '<div class="geo-stat"><div class="value">' + thicknessDisplay + '</div><div class="label">Sheet Thickness</div></div>' +
    '<div class="geo-stat"><div class="value">' + g.num_bends + '</div><div class="label">Bends</div></div>' +
    '<div class="geo-stat"><div class="value">' + env.mass_lb.toFixed(2) + ' lb</div><div class="label">Est. Weight</div></div>' +
    '<div class="geo-stat"><div class="value">' + g.flat_width_in + '"</div><div class="label" title="Developed width after unfolding all bends">Flat/Dev Width</div></div>' +
    '<div class="geo-stat"><div class="value">' + (g.flat_length_in || '-') + '"</div><div class="label" title="Length along the bend axis direction">Flat/Dev Length</div></div>' +
    '</div>';

  // Complexity indicator
  if (g.complexity) {
    var cx = g.complexity;
    var barColors = {1:'#4caf50',2:'#8bc34a',3:'#ff9800',4:'#f44336',5:'#b71c1c'};
    html += '<div style="display:flex;gap:1rem;margin:0.8rem 0;align-items:center;flex-wrap:wrap">';
    html += '<div style="background:#f8f8f0;border:1px solid #ddd;border-radius:6px;padding:0.5rem 1rem;flex:1;min-width:140px">' +
      '<div style="font-size:0.75rem;color:#666">Cut Complexity</div>' +
      '<div style="background:#e0e0e0;height:6px;border-radius:3px;margin:4px 0"><div style="width:' + (cx.cut_score*20) + '%;height:100%;border-radius:3px;background:' + barColors[cx.cut_score] + '"></div></div>' +
      '<div style="font-size:0.8rem;font-weight:600">' + cx.cut_score + '/5</div></div>';
    html += '<div style="background:#f8f8f0;border:1px solid #ddd;border-radius:6px;padding:0.5rem 1rem;flex:1;min-width:140px">' +
      '<div style="font-size:0.75rem;color:#666">Bend Complexity</div>' +
      '<div style="background:#e0e0e0;height:6px;border-radius:3px;margin:4px 0"><div style="width:' + (cx.bend_score*20) + '%;height:100%;border-radius:3px;background:' + barColors[cx.bend_score] + '"></div></div>' +
      '<div style="font-size:0.8rem;font-weight:600">' + cx.bend_score + '/5</div></div>';
    html += '<div style="background:' + barColors[cx.overall_score] + ';color:#fff;border-radius:6px;padding:0.5rem 1rem;text-align:center;min-width:120px">' +
      '<div style="font-size:0.75rem;opacity:0.85">Overall</div>' +
      '<div style="font-size:1.1rem;font-weight:700">' + cx.overall_label + '</div></div>';
    html += '</div>';
    if (cx.notes && cx.notes.length) {
      html += '<div style="margin-bottom:0.8rem">';
      cx.notes.forEach(function(n) {
        html += '<span style="display:inline-block;background:#fff3e0;border:1px solid #ffe0b2;border-radius:3px;padding:2px 8px;font-size:0.75rem;margin:2px">' + n + '</span>';
      });
      html += '</div>';
    }
  }

  html += '<table class="detail-table">' +
    '<tr><th>Bend radius</th><td>' + (g.bend_radius_in ? g.bend_radius_in + '"' : 'N/A') + '</td></tr>' +
    '<tr><th>Bend angles</th><td>' + (g.bend_angles_deg.length ? g.bend_angles_deg.join(", ") + '&deg;' : 'None') + '</td></tr>' +
    '<tr><th title="Neutral axis offset factor used for flat pattern development">K-factor used</th><td>' + g.k_factor_assumed + ' <span style="color:#888;font-size:0.85em">(' + (g.k_factor_source || 'default') + ')</span></td></tr>' +
    '<tr><th>Mass</th><td>' + env.mass_lb.toFixed(2) + ' lb / ' + env.mass_kg.toFixed(3) + ' kg</td></tr>' +
    '<tr><th>Volume</th><td>' + env.volume_mm3.toFixed(1) + ' mm&sup3;</td></tr>' +
    '<tr><th>Surface area</th><td>' + env.area_mm2.toFixed(1) + ' mm&sup2;</td></tr>' +
    '</table>';

  // --- Feature detail table ---
  if (g.features && g.features.length) {
    var counts = {};
    g.features.forEach(function(f) { counts[f.type] = (counts[f.type] || 0) + 1; });
    var summaryStr = Object.keys(counts).map(function(k) { return counts[k] + ' ' + k; }).join(', ');
    html += '<div style="margin:1rem 0"><h4 style="color:#1a3a1a;margin-bottom:0.5rem">Features (' + g.features.length + ': ' + summaryStr + ')</h4>';
    html += '<table class="detail-table" style="font-size:0.8rem"><thead><tr><th style="width:5%">#</th><th style="width:18%">Type</th><th style="width:22%">Size</th><th style="width:25%">Hardware Hint</th><th style="width:15%">Position</th><th style="width:15%">Confidence</th></tr></thead><tbody>';
    g.features.forEach(function(f, i) {
      var size = '';
      if (f.type === 'round') size = '&empty; ' + f.diameter_in + '"';
      else if (f.type === 'square_or_rect') size = f.size_in[0] + '" x ' + f.size_in[1] + '"';
      else if (f.type === 'slot') size = f.width_in + '" x ' + f.slot_length_in + '"';
      else if (f.type === 'countersink') size = f.angle_deg + '&deg; near &empty;' + (f.near_hole_dia_in || '?') + '"';
      else if (f.type === 'chamfer') size = f.angle_deg + '&deg;';

      var hw = f.hardware_hint || '';
      var confBadge = f.confidence === 'high' ? '<span style="color:#2a5a2a">high</span>' :
                      f.confidence === 'medium' ? '<span style="color:#b8860b">med</span>' :
                      '<span style="color:#c00">low</span>';
      var pos = '';
      if (f.length_in != null) pos = 'L:' + f.length_in + '"';
      if (f.transverse_in != null) pos += (pos ? ' ' : '') + 'T:' + f.transverse_in + '"';

      html += '<tr><td>' + (i+1) + '</td><td>' + f.type + '</td><td>' + size + '</td><td>' + hw + '</td><td style="font-size:0.75rem">' + pos + '</td><td>' + confBadge + '</td></tr>';
    });
    html += '</tbody></table>';
    if (g.features_unclassified_count > 0) {
      html += '<div style="color:#888;font-size:0.8em;margin-top:4px">' + g.features_unclassified_count + ' unclassified feature face(s) filtered out</div>';
    }
    html += '</div>';
  } else {
    html += '<div style="color:#888;margin:0.8rem 0">No cut features detected</div>';
  }

  // Bend Schedule (detailed per-bend table)
  if (g.bend_details && g.bend_details.length > 0) {
    html += '<div style="margin:1rem 0"><h4 style="color:#1a3a1a;margin-bottom:0.5rem">Bend Schedule (' + g.bend_details.length + ' bends)</h4>';
    html += '<table class="detail-table" style="font-size:0.85rem"><thead><tr><th>#</th><th>Angle</th><th>Radius</th><th>Bend Length</th><th>Direction</th></tr></thead><tbody>';
    g.bend_details.forEach(function(bd, i) {
      var dir = (bd.direction || '---').replace(/_/g, ' ');
      dir = dir.charAt(0).toUpperCase() + dir.slice(1);
      html += '<tr><td>' + (i+1) + '</td><td>' + bd.angle_deg + '&deg;</td><td>' + bd.inner_radius_in.toFixed(4) + '"</td><td>' + bd.bend_line_length_in.toFixed(3) + '"</td><td>' + dir + '</td></tr>';
    });
    html += '</tbody></table></div>';
  }

  // Nesting estimate
  var fw = parseFloat(g.flat_width_in) || 0;
  var fl = parseFloat(g.flat_length_in) || 0;
  if (fw > 0 && fl > 0) {
    var sheets = [[48,96,"48 x 96"],[48,120,"48 x 120"],[60,120,"60 x 120"],[48,144,"48 x 144"],[60,144,"60 x 144"]];
    var gap = 0.25;
    var pw = fw + gap;
    var pl = fl + gap;
    html += '<div class="nesting-box"><h4 style="color:#1a3a1a">Material Nesting</h4>';
    html += '<table class="detail-table"><thead><tr><th>Sheet Size</th><th>Parts/Sheet</th><th>Layout</th><th>Utilization</th></tr></thead><tbody>';
    sheets.forEach(function(s) {
      var nA = Math.floor(s[0]/pw) * Math.floor(s[1]/pl);
      var nB = Math.floor(s[0]/pl) * Math.floor(s[1]/pw);
      var best = Math.max(nA, nB);
      var colsA = Math.floor(s[0]/pw), rowsA = Math.floor(s[1]/pl);
      var colsB = Math.floor(s[0]/pl), rowsB = Math.floor(s[1]/pw);
      var cols, rows;
      if (nA >= nB) { cols = colsA; rows = rowsA; } else { cols = colsB; rows = rowsB; }
      var util = best > 0 ? ((fw * fl * best) / (s[0] * s[1]) * 100).toFixed(1) : '0.0';
      var utilColor = parseFloat(util) > 70 ? '#2a5a2a' : (parseFloat(util) > 50 ? '#b8860b' : '#c00');
      if (best > 0) {
        html += '<tr><td>' + s[2] + '"</td><td><strong>' + best + ' pcs</strong></td><td>' + cols + ' x ' + rows + '</td><td><span style="color:' + utilColor + ';font-weight:600">' + util + '%</span></td></tr>';
      }
    });
    html += '</tbody></table><div style="color:#888;font-size:0.8em;margin-top:4px">0.25" kerf/gap assumed. Flat pattern: ' + fw.toFixed(3) + '" x ' + fl.toFixed(3) + '"</div></div>';
  }
  return html;
}

function renderMachined(g, env, dims) {
  const stock = g.stock_size || {};
  const fs = g.feature_summary || {};
  let html = '<div class="geo-grid">' +
    '<div class="geo-stat"><div class="value">' + dims + '</div><div class="label">Overall Dimensions</div></div>' +
    '<div class="geo-stat"><div class="value">' + (g.machining_type || '-') + '</div><div class="label">Machining Type</div></div>' +
    '<div class="geo-stat"><div class="value">' + env.mass_lb.toFixed(2) + ' lb</div><div class="label">Est. Weight</div></div>' +
    '<div class="geo-stat"><div class="value">' + ((g.material_removal_ratio || 0) * 100).toFixed(0) + '%</div><div class="label">Material Removal</div></div>' +
    '<div class="geo-stat"><div class="value">' + (fs.num_holes || 0) + '</div><div class="label">Holes</div></div>' +
    '<div class="geo-stat"><div class="value">' + (fs.num_pockets || 0) + '</div><div class="label">Pockets</div></div>' +
    '</div>';
  html += '<table class="detail-table">' +
    '<tr><th>Recommended stock</th><td>' + (stock.description || '-') + '</td></tr>' +
    '<tr><th>Stock type</th><td>' + (stock.type || '-').replace(/_/g, ' ') + '</td></tr>' +
    '<tr><th>Total features</th><td>' + (fs.total_features || 0) + '</td></tr>' +
    (fs.hole_diameter_range_in ? '<tr><th>Hole diameter range</th><td>' + fs.hole_diameter_range_in + '</td></tr>' : '') +
    (fs.pocket_depth_range_mm ? '<tr><th>Pocket depth range</th><td>' + fs.pocket_depth_range_mm + ' mm</td></tr>' : '') +
    '<tr><th>Mass (metric)</th><td>' + env.mass_kg.toFixed(3) + ' kg</td></tr>' +
    '<tr><th>Volume</th><td>' + env.volume_mm3.toFixed(1) + ' mm3</td></tr>' +
    '<tr><th>Surface area</th><td>' + env.area_mm2.toFixed(1) + ' mm2</td></tr>' +
    '</table>';
  return html;
}

function renderCostEstimate(cost) {
  if (!cost) return '';
  var html = '<div class="cost-section" style="margin:1rem 0;border:2px solid #2a5a2a;border-radius:8px;overflow:hidden">';
  html += '<div style="background:#2a5a2a;color:#fff;padding:0.6rem 1rem;font-weight:700;font-size:1rem">Cost Estimate <span style="opacity:0.7;font-weight:400;font-size:0.85rem">(' + cost.material_display + ', Qty ' + cost.quantity + ')</span></div>';

  // Summary row
  html += '<div class="cost-summary-row">';
  html += '<div class="cost-summary-cell" style="border-right:1px solid #ddd"><div style="font-size:1.6rem;font-weight:700;color:#2a5a2a">$' + cost.unit_cost.toFixed(2) + '</div><div style="font-size:0.75rem;color:#666">Per Part</div></div>';
  html += '<div class="cost-summary-cell" style="border-right:1px solid #ddd"><div style="font-size:1.6rem;font-weight:700;color:#1a3a1a">$' + cost.total_cost.toFixed(2) + '</div><div style="font-size:0.75rem;color:#666">Total (' + cost.quantity + ' pcs)</div></div>';
  html += '<div class="cost-summary-cell"><div style="font-size:1.1rem;font-weight:600;color:#555">' + cost.total_time_hr.toFixed(2) + ' hr</div><div style="font-size:0.75rem;color:#666">Total Shop Time</div></div>';
  html += '</div>';

  // Operations breakdown
  if (cost.operations && cost.operations.length) {
    html += '<table class="detail-table" style="margin:0;border-radius:0;font-size:0.82rem">';
    html += '<thead><tr style="background:#f0f5f0"><th>Operation</th><th>Setup</th><th>Cycle</th><th>Run Time</th><th>Rate</th><th>Cost</th></tr></thead><tbody>';
    cost.operations.forEach(function(op) {
      var opNote = '';
      if (op.assist_gas) opNote += ' <span style="color:#0066aa;font-size:0.72rem">(' + op.assist_gas + ')</span>';
      if (op.tonnage_est) opNote += ' <span style="color:#666;font-size:0.72rem">' + op.tonnage_est + 'T</span>';
      if (op.speed_ipm) opNote += ' <span style="color:#888;font-size:0.72rem">' + op.speed_ipm + ' ipm</span>';
      if (op.weld_location) opNote += ' <span style="color:#795548;font-size:0.72rem">[' + op.weld_location + ']</span>';
      if (op.second_operator) opNote += ' <span style="color:#c62828;font-size:0.72rem">+2nd op $' + op.second_op_cost.toFixed(0) + '</span>';
      html += '<tr>';
      html += '<td style="font-weight:600">' + op.operation + opNote + '</td>';
      html += '<td>' + op.setup_hr.toFixed(2) + ' hr</td>';
      html += '<td>' + op.cycle_time_hr.toFixed(4) + ' hr</td>';
      html += '<td>' + op.run_time_hr.toFixed(2) + ' hr</td>';
      html += '<td>$' + op.rate_per_hr.toFixed(0) + '/hr</td>';
      html += '<td style="font-weight:600">$' + op.total_cost.toFixed(2) + '</td>';
      html += '</tr>';
      // Expandable burden detail row
      if (op.burden_detail) {
        var bd = op.burden_detail;
        var parts = [];
        if (bd.labor) parts.push('Labor $' + bd.labor.toFixed(0));
        if (bd.gas_cost) parts.push('Gas $' + bd.gas_cost.toFixed(2));
        if (bd.electricity) parts.push('Elec $' + bd.electricity.toFixed(2));
        if (bd.consumables) parts.push('Consumables $' + bd.consumables.toFixed(2));
        if (bd.depreciation) parts.push('Depr $' + bd.depreciation.toFixed(0));
        if (bd.abrasive) parts.push('Abrasive $' + bd.abrasive.toFixed(2));
        if (parts.length) {
          html += '<tr style="background:#f8faf5"><td colspan="6" style="padding:2px 1rem;font-size:0.72rem;color:#666">';
          html += 'Rate breakdown: ' + parts.join(' + ') + ' = $' + bd.total.toFixed(0) + '/hr';
          html += '</td></tr>';
        }
      }
      // Setup detail row
      if (op.setup_detail && op.setup_detail.items) {
        var sitems = [];
        op.setup_detail.items.forEach(function(si) {
          sitems.push(si.label + ' ' + si.hr.toFixed(2) + 'hr');
        });
        if (sitems.length) {
          html += '<tr style="background:#f8faf5"><td colspan="6" style="padding:2px 1rem;font-size:0.72rem;color:#666">';
          html += 'Setup: ' + sitems.join(' + ');
          html += '</td></tr>';
        }
      }
    });
    html += '</tbody></table>';
  }

  // Quantity breaks
  if (cost.qty_breaks && cost.qty_breaks.length) {
    html += '<div style="padding:0.6rem 1rem;border-top:1px solid #ddd;background:#fafdf8">';
    html += '<div style="font-weight:600;font-size:0.85rem;margin-bottom:0.4rem;color:#1a3a1a">Quantity Price Breaks</div>';
    html += '<div style="display:flex;gap:0;flex-wrap:wrap">';
    cost.qty_breaks.forEach(function(qb) {
      var sel = qb.selected;
      html += '<div style="flex:1;min-width:80px;text-align:center;padding:0.5rem 0.3rem;border:1px solid ' + (sel ? '#2a5a2a' : '#e0e0e0') + ';background:' + (sel ? '#e8f5e9' : '#fff') + ';margin:2px;border-radius:4px">';
      html += '<div style="font-size:0.7rem;color:#888">' + qb.qty + ' pcs</div>';
      html += '<div style="font-size:0.95rem;font-weight:' + (sel ? '700' : '500') + ';color:' + (sel ? '#2a5a2a' : '#333') + '">$' + qb.unit_cost.toFixed(2) + '</div>';
      html += '<div style="font-size:0.65rem;color:#999">ea</div>';
      html += '</div>';
    });
    html += '</div></div>';
  }

  // Warnings
  if (cost.warnings && cost.warnings.length) {
    html += '<div style="padding:0.5rem 1rem;background:#fff8e1;border-top:1px solid #ddd;font-size:0.8rem;color:#795548">';
    cost.warnings.forEach(function(w) { html += '<div>' + w + '</div>'; });
    html += '</div>';
  }

  // Ramp factor note
  html += '<div style="padding:0.3rem 1rem 0.5rem;font-size:0.7rem;color:#999;border-top:1px solid #eee">Ramp factor: ' + cost.ramp_factor + 'x (production efficiency at qty ' + cost.quantity + ')</div>';

  html += '</div>';
  return html;
}

function renderDrawingResult(r, idx) {
  const d = r.drawing_data;
  const fabLabel = d.likely_fab_type === 'sheet_metal' ? 'Sheet Metal' : (d.likely_fab_type === 'unknown' ? 'Unknown' : d.likely_fab_type);
  const confColor = d.fab_type_confidence === 'high' ? '#2a5a2a' : (d.fab_type_confidence === 'medium' ? '#b8860b' : '#888');
  const hasPages = d.pages && d.pages.length > 0;
  const drawingPageCount = d.drawing_page_count || (hasPages ? d.pages.length : 0);

  let html = '<div class="result-section"><div class="result-header" onclick="toggleResult(' + idx + ')">' +
    '<h3>' + r.filename + '</h3><div><span class="badge" style="background:#0066aa">Drawing</span> ' +
    '<span class="badge" style="background:' + confColor + '">' + fabLabel + '</span>' +
    (drawingPageCount > 1 ? ' <span class="badge" style="background:#555">' + drawingPageCount + ' parts</span>' : '') +
    '</div></div>' +
    '<div class="result-body" id="result-' + idx + '">';

  // Summary bar
  if (d.summary) {
    html += '<div style="background:#e8f4ff;border:1px solid #b0d0f0;border-radius:6px;padding:0.8rem;margin-bottom:1rem;font-size:0.9rem">' + d.summary + '</div>';
  }

  // STEP-style geometry stats grid
  var cf = d._computed_flat || {};
  var thkDisplay = '';
  if (d.thickness && d.thickness.length) {
    var t0 = d.thickness[0];
    thkDisplay = t0.value_in + '"';
    if (t0.gauge) thkDisplay += ' <span style="color:#2a5a2a;font-weight:600">(' + t0.gauge + ' GA)</span>';
  }
  var dimsDisplay = '';
  if (d.dimensions && d.dimensions.length) {
    var dm0 = d.dimensions[0];
    dimsDisplay = dm0.length + '" x ' + dm0.width + '"';
    if (dm0.height) dimsDisplay += ' x ' + dm0.height + '"';
  }
  var matDisplay = '';
  if (d.materials && d.materials.length) {
    var uniqueMats = [];
    var seen = {};
    d.materials.forEach(function(m) {
      var k = (m.name || m.raw_callout).toUpperCase();
      if (!seen[k]) { seen[k] = true; uniqueMats.push(m.name || m.raw_callout); }
    });
    matDisplay = uniqueMats.slice(0,2).join(', ');
  }

  html += '<div class="geo-grid">';
  if (dimsDisplay) html += '<div class="geo-stat"><div class="value">' + dimsDisplay + '</div><div class="label">Overall Dimensions</div></div>';
  if (thkDisplay) html += '<div class="geo-stat"><div class="value">' + thkDisplay + '</div><div class="label">Sheet Thickness</div></div>';
  html += '<div class="geo-stat"><div class="value">' + (cf.num_bends || 0) + '</div><div class="label">Bends</div></div>';
  if (matDisplay) html += '<div class="geo-stat"><div class="value" style="font-size:0.85rem">' + matDisplay + '</div><div class="label">Material</div></div>';
  if (cf.flat_width_in) html += '<div class="geo-stat"><div class="value">' + cf.flat_width_in + '"</div><div class="label" title="Developed width after unfolding all bends">Flat/Dev Width</div></div>';
  if (cf.flat_length_in) html += '<div class="geo-stat"><div class="value">' + cf.flat_length_in + '"</div><div class="label" title="Length along the bend axis">Flat/Dev Length</div></div>';
  html += '</div>';

  // Flat pattern image + views
  html += '<div class="view-grid">';
  if (r.files && r.files.flat_pattern) {
    html += '<div class="view-item"><img src="' + r.files.flat_pattern + '" alt="Flat Pattern" onerror="hideParent(this)"><div class="view-label">Flat Pattern</div></div>';
  }
  html += '</div>';

  // Detail table (bend info, tolerances, finishes)
  html += '<table class="detail-table">';
  if (cf.num_bends > 0 && cf.bend_angles && cf.bend_angles.length) {
    html += '<tr><th>Bend angles</th><td>' + cf.bend_angles.map(function(a){return a.toFixed(0) + ' deg';}).join(', ') + '</td></tr>';
    html += '<tr><th>Bend radius</th><td>' + (cf.bend_radius_in || '-') + '"</td></tr>';
    html += '<tr><th>K-factor</th><td>' + (cf.k_factor || 0.44) + '</td></tr>';
  }
  if (d.tolerances && d.tolerances.length) {
    var uniqTol = [];
    var seenTol = {};
    d.tolerances.forEach(function(t) { if (t.raw && !seenTol[t.raw]) { seenTol[t.raw] = true; uniqTol.push(t.raw); }});
    if (uniqTol.length) html += '<tr><th>Tolerances</th><td>' + uniqTol.slice(0,5).join(', ') + '</td></tr>';
  }
  if (d.finishes && d.finishes.length) {
    html += '<tr><th>Finish</th><td>' + d.finishes.map(function(f){return f.finish;}).join(', ') + '</td></tr>';
  }
  html += '<tr><th>Drawing pages</th><td>' + drawingPageCount + ' of ' + d.page_count + ' total</td></tr>';
  html += '</table>';

  // Download buttons (STEP-style)
  html += '<div class="dl-row">';
  if (r.files && r.files.report_pdf) {
    html += '<a class="dl-btn" href="' + r.files.report_pdf + '" download>Download PDF Report</a>';
  }
  if (r.files && r.files.geometry_json) {
    html += '<a class="dl-btn secondary" href="' + r.files.geometry_json + '" download>Download JSON</a>';
  }
  html += '</div>';

  // Overall features table
  if (d.features && d.features.length) {
    html += '<h4 style="margin:1rem 0 0.5rem;color:#2a5a2a;font-size:0.95rem">Extracted Features (from drawing callouts)</h4>';
    html += '<table class="detail-table"><tr><th>Type</th><th>Count</th><th>Size</th><th>Callout</th></tr>';
    d.features.forEach(function(f) {
      var size = '';
      if (f.type === 'round_hole' || f.type === 'counterbored_hole' || f.type === 'countersunk_hole') {
        size = 'Dia ' + f.diameter_in + '"';
        if (f.through) size += ' THRU';
      } else if (f.type === 'tapped_hole') {
        size = f.thread_spec;
        if (f.through) size += ' THRU';
      } else if (f.type === 'slot') {
        size = f.width_in + '" x ' + f.length_in + '"';
      }
      var label = f.type.replace(/_/g, ' ');
      html += '<tr><td>' + label + '</td><td>' + (f.count || 1) + '</td><td>' + size + '</td><td style="color:#666;font-size:0.75rem">' + (f.raw || '') + '</td></tr>';
    });
    html += '</table>';
  }

  // Per-page results
  if (hasPages) {
    html += '<h4 style="margin:1rem 0 0.5rem;color:#2a5a2a;font-size:0.95rem">Per-Page Extraction (' + d.pages.length + ' drawings)</h4>';
    d.pages.forEach(function(pg, pgIdx) {
      const pgId = 'pg-' + idx + '-' + pgIdx;
      const pgPart = (pg.part_info && pg.part_info.part_number) ? pg.part_info.part_number : '';
      const pgMat = (pg.materials && pg.materials.length) ? (pg.materials[0].name || pg.materials[0].raw_callout) : '';
      const pgFab = pg.likely_fab_type === 'sheet_metal' ? 'Sheet Metal' : (pg.likely_fab_type || 'Unknown');
      const pgConf = pg.fab_type_confidence === 'high' ? '#2a5a2a' : (pg.fab_type_confidence === 'medium' ? '#b8860b' : '#888');
      const pgLabel = pgPart ? ('Page ' + pg.page + ' - ' + pgPart) : ('Page ' + pg.page);

      html += '<div style="border:1px solid #ddd;border-radius:6px;margin-bottom:0.5rem;overflow:hidden">' +
        '<div onclick="togglePage(this)" data-target="' + pgId + '" style="cursor:pointer;display:flex;justify-content:space-between;align-items:center;padding:0.5rem 0.8rem;background:#f8f8f8;border-bottom:1px solid #eee">' +
        '<span style="font-weight:600;font-size:0.85rem">' + pgLabel + '</span>' +
        '<div>' +
        (pgMat ? '<span class="badge" style="background:#555;font-size:0.7rem">' + pgMat + '</span> ' : '') +
        '<span class="badge" style="background:' + pgConf + ';font-size:0.7rem">' + pgFab + '</span>' +
        '</div></div>' +
        '<div id="' + pgId + '" class="collapsed" style="padding:0.6rem 0.8rem">';

      // Per-page specs grid
      html += '<div class="geo-grid" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:0.4rem;margin-bottom:0.5rem">';
      if (pg.materials && pg.materials.length) {
        html += '<div class="geo-stat" style="padding:0.4rem"><div class="value" style="font-size:0.8rem">' + (pg.materials[0].name || pg.materials[0].raw_callout) + '</div><div class="label" style="font-size:0.65rem">Material</div></div>';
      }
      if (pg.thickness && pg.thickness.length) {
        const t = pg.thickness[0];
        html += '<div class="geo-stat" style="padding:0.4rem"><div class="value" style="font-size:0.8rem">' + t.value_in + '"' + (t.gauge ? ' (' + t.gauge + ' GA)' : '') + '</div><div class="label" style="font-size:0.65rem">Thickness</div></div>';
      }
      if (pg.dimensions && pg.dimensions.length) {
        const dm = pg.dimensions[0];
        let ds = dm.length + ' x ' + dm.width;
        if (dm.height) ds += ' x ' + dm.height;
        html += '<div class="geo-stat" style="padding:0.4rem"><div class="value" style="font-size:0.8rem">' + ds + '</div><div class="label" style="font-size:0.65rem">Dimensions</div></div>';
      }
      if (pg.part_info && pg.part_info.quantity) {
        html += '<div class="geo-stat" style="padding:0.4rem"><div class="value" style="font-size:0.8rem">' + pg.part_info.quantity + '</div><div class="label" style="font-size:0.65rem">Qty</div></div>';
      }
      html += '</div>';

      // Per-page detail table (compact)
      html += '<table class="detail-table" style="font-size:0.8rem">';
      if (pg.dimensions && pg.dimensions.length > 1) {
        html += '<tr><th>All dimensions</th><td>' + pg.dimensions.map(function(dm) { let s = dm.length + ' x ' + dm.width; if (dm.height) s += ' x ' + dm.height; return s; }).join('; ') + '</td></tr>';
      }
      if (pg.tolerances && pg.tolerances.length) {
        const uniqTol = [];
        const seenTol = {};
        pg.tolerances.forEach(function(t) { if (t.raw && !seenTol[t.raw]) { seenTol[t.raw] = true; uniqTol.push(t.raw); }});
        if (uniqTol.length) html += '<tr><th>Tolerances</th><td>' + uniqTol.slice(0,5).join(', ') + '</td></tr>';
      }
      const pgBends = pg.bends || {};
      if (pgBends.radii && pgBends.radii.length) {
        html += '<tr><th>Bend radii</th><td>' + pgBends.radii.map(function(b){return b.raw;}).join(', ') + '</td></tr>';
      }
      if (pgBends.angles && pgBends.angles.length) {
        html += '<tr><th>Bend angles</th><td>' + pgBends.angles.map(function(b){return b.raw;}).join(', ') + '</td></tr>';
      }
      if (pg.finishes && pg.finishes.length) {
        html += '<tr><th>Finish</th><td>' + pg.finishes.map(function(f){return f.finish;}).join(', ') + '</td></tr>';
      }
      html += '</table>';

      // Per-page features table
      if (pg.features && pg.features.length) {
        html += '<h5 style="margin:0.6rem 0 0.3rem;color:#0066aa;font-size:0.8rem">Extracted Features (from drawing callouts)</h5>';
        html += '<table class="detail-table" style="font-size:0.8rem"><tr><th>Type</th><th>Count</th><th>Size</th><th>Callout</th></tr>';
        pg.features.forEach(function(f) {
          var size = '';
          if (f.type === 'round_hole' || f.type === 'counterbored_hole' || f.type === 'countersunk_hole') {
            size = 'Dia ' + f.diameter_in + '"';
            if (f.cbore_dia_in) size += ' CBORE ' + f.cbore_dia_in + '"';
            if (f.csink_dia_in) size += ' CSINK ' + f.csink_dia_in + '"';
            if (f.through) size += ' THRU';
          } else if (f.type === 'tapped_hole') {
            size = f.thread_spec;
            if (f.through) size += ' THRU';
          } else if (f.type === 'slot') {
            size = f.width_in + '" x ' + f.length_in + '"';
          }
          var label = f.type.replace(/_/g, ' ');
          html += '<tr><td>' + label + '</td><td>' + (f.count || 1) + '</td><td>' + size + '</td><td style="color:#666;font-size:0.75rem">' + (f.raw || '') + '</td></tr>';
        });
        html += '</table>';
      }

      // Per-page missing info
      if (pg.missing_info && pg.missing_info.length) {
        html += '<div style="margin-top:0.4rem">';
        pg.missing_info.forEach(function(mi) {
          html += '<div style="background:#fff8e8;border:1px solid #e8d8a0;border-radius:3px;padding:0.3rem 0.5rem;margin-bottom:0.3rem;font-size:0.75rem">' +
            '<strong>' + mi.field + ':</strong> ' + mi.message + '</div>';
        });
        html += '</div>';
      }

      // Per-page download button
      const pgReportKey = 'page_' + pg.page + '_report';
      if (r.files && r.files[pgReportKey]) {
        html += '<div style="margin-top:0.5rem"><a class="dl-btn" style="font-size:0.75rem;padding:0.3rem 0.8rem" href="' + r.files[pgReportKey] + '" download>Download Page ' + pg.page + ' PDF</a></div>';
      }

      html += '</div></div>';
    });
  }

  html += '</div></div>';
  return html;
}

function toggleResult(idx) {
  const body = document.getElementById("result-" + idx);
  body.classList.toggle("collapsed");
}

function togglePage(el) {
  var target = el.getAttribute("data-target");
  document.getElementById(target).classList.toggle("collapsed");
}

function exportCSV(idx) {
  var r = _allResults.filter(function(x){return !x.error;})[idx] || _allResults[idx];
  if (!r || !r.geometry) return;
  var g = r.geometry, env = g.envelope;
  var rows = [["Field","Value"]];
  rows.push(["Filename", r.filename]);
  rows.push(["Fab Type", g.fab_type || "sheet_metal"]);
  rows.push(["Dimensions (in)", (env.bbox_mm.xlen/25.4).toFixed(2)+' x '+(env.bbox_mm.ylen/25.4).toFixed(2)+' x '+(env.bbox_mm.zlen/25.4).toFixed(2)]);
  rows.push(["Weight (lb)", env.mass_lb.toFixed(3)]);
  rows.push(["Weight (kg)", env.mass_kg.toFixed(3)]);
  rows.push(["Volume (mm3)", env.volume_mm3.toFixed(1)]);
  rows.push(["Surface Area (mm2)", env.area_mm2.toFixed(1)]);
  if (g.fab_type === "sheet_metal") {
    rows.push(["Thickness (in)", g.thickness_in]);
    rows.push(["Gauge", g.gauge || "N/A"]);
    rows.push(["Bends", g.num_bends]);
    rows.push(["Bend Radius (in)", g.bend_radius_in]);
    rows.push(["Bend Angles (deg)", (g.bend_angles_deg||[]).join("; ")]);
    rows.push(["Flat Width (in)", g.flat_width_in]);
    rows.push(["Flat Length (in)", g.flat_length_in || ""]);
    rows.push(["K-Factor", g.k_factor_assumed]);
    rows.push(["K-Factor Source", g.k_factor_source || "default"]);
    rows.push(["Features", g.features_raw_count]);
    if (g.complexity) {
      rows.push(["Complexity", g.complexity.overall_label]);
      rows.push(["Cut Complexity", g.complexity.cut_score + "/5"]);
      rows.push(["Bend Complexity", g.complexity.bend_score + "/5"]);
    }
  } else {
    rows.push(["Machining Type", g.machining_type || ""]);
    rows.push(["Material Removal %", ((g.material_removal_ratio||0)*100).toFixed(1)]);
    var fs = g.feature_summary || {};
    Object.keys(fs).forEach(function(k){rows.push(["Feature: "+k, fs[k]]);});
  }
  if (g.features && g.features.length) {
    var counts = {};
    g.features.forEach(function(f){counts[f.type]=(counts[f.type]||0)+1;});
    Object.keys(counts).forEach(function(k){rows.push(["Feature: "+k, counts[k]]);});
    // Per-feature detail rows
    g.features.forEach(function(f, i) {
      var detail = f.type;
      if (f.diameter_in) detail += ' dia=' + f.diameter_in + '"';
      if (f.size_in) detail += ' ' + f.size_in[0] + 'x' + f.size_in[1] + '"';
      if (f.hardware_hint) detail += ' (' + f.hardware_hint + ')';
      rows.push(["Feature #" + (i+1), detail]);
    });
  }
  var csv = rows.map(function(r){return r.map(function(c){return '"'+String(c).replace(/"/g,'""')+'"';}).join(",");}).join("\\n");
  var blob = new Blob([csv], {type:"text/csv"});
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = r.filename.replace(/\\.[^.]+$/,"") + "_quote.csv";
  a.click();
}

function printQuote(idx) {
  var el = document.getElementById("result-" + idx);
  if (!el) return;
  var w = window.open("","_blank");
  w.document.write('<html><head><title>CMC Quote Sheet</title><style>body{font-family:Arial,sans-serif;padding:20px;color:#222;}h2{color:#2a5a2a;border-bottom:2px solid #2a5a2a;padding-bottom:8px;}table{border-collapse:collapse;width:100%;margin:10px 0;}th,td{border:1px solid #ccc;padding:6px 10px;text-align:left;font-size:13px;}th{background:#f0f0f0;}img{max-width:200px;max-height:150px;}.geo-grid{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0;}.geo-stat{border:1px solid #ddd;border-radius:6px;padding:8px 12px;min-width:100px;text-align:center;}.geo-stat .value{font-weight:bold;font-size:1.1em;}.geo-stat .label{color:#666;font-size:0.8em;}.dl-row,.view-grid{display:flex;flex-wrap:wrap;gap:8px;}.view-item{text-align:center;}.nesting-box{margin:10px 0;padding:10px;background:#f8f8f0;border:1px solid #ddd;border-radius:6px;}.nesting-box h4{margin:0 0 8px 0;color:#2a5a2a;}.dl-row{display:none;}@media print{.dl-row{display:none !important;}}</style></head><body>');
  w.document.write('<h2>CMC Quoting Toolkit - Quote Sheet</h2>');
  w.document.write('<div style="color:#888;margin-bottom:12px;">Generated: ' + new Date().toLocaleString() + '</div>');
  w.document.write(el.innerHTML);
  w.document.write('</body></html>');
  w.document.close();
  w.print();
}

function downloadQuotePDF(idx) {
  var r = _allResults.filter(function(x){return !x.error;})[idx] || _allResults[idx];
  if (!r || !r.job_id) { alert("No job data available for PDF export."); return; }
  var mat = document.getElementById("material");
  var qty = document.getElementById("quantity");
  var material = mat ? mat.value : "mild_steel";
  var quantity = qty ? qty.value : "1";
  var url = "/quote-pdf/" + r.job_id + "?material=" + encodeURIComponent(material) + "&quantity=" + encodeURIComponent(quantity);
  window.open(url, "_blank");
}

// --- History ---
async function loadHistory() {
  try {
    const resp = await fetch("/history");
    const data = await resp.json();
    const el = document.getElementById("historyContent");
    const count = document.getElementById("historyCount");

    if (!data.jobs || data.jobs.length === 0) {
      el.innerHTML = '<div class="empty-state">No jobs yet. Upload a STEP file to get started.</div>';
      count.textContent = "";
      return;
    }

    count.textContent = "(" + data.jobs.length + ")";
    let html = '<table class="history-table"><thead><tr><th>File</th><th>Type</th><th>Date</th><th>Dimensions</th><th>Weight</th><th>Actions</th></tr></thead><tbody>';

    data.jobs.forEach(job => {
      const ft = job.fab_type === 'machined' ? 'Machined' : (job.fab_type === 'drawing' || job.fab_type === 'unknown' ? 'Drawing' : 'Sheet Metal');
      html += '<tr>' +
        '<td><strong>' + job.filename + '</strong></td>' +
        '<td>' + ft + '</td>' +
        '<td>' + job.timestamp + '</td>' +
        '<td>' + (job.dimensions || '-') + '</td>' +
        '<td>' + (job.weight || '-') + '</td>' +
        '<td>' +
        (job.report_url ? '<a class="dl-btn" style="font-size:0.75rem;padding:0.25rem 0.6rem" href="' + job.report_url + '" download>PDF</a> ' : '') +
        (job.json_url ? '<a class="dl-btn secondary" style="font-size:0.75rem;padding:0.25rem 0.6rem" href="' + job.json_url + '" download>JSON</a>' : '') +
        '</td></tr>';
    });

    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (err) {
    console.error("Failed to load history:", err);
  }
}

// Load history on page load
loadHistory();

// ═══════════════════════════════════════════════════════════
//  Shop Rates Config Panel
// ═══════════════════════════════════════════════════════════

var shopConfig = null;
var configLoaded = false;

var CFG_SECTIONS = {
  rates: {label: "Machine Rates ($/hr)", unit: "$/hr"},
  setup: {label: "Setup Times (hr)", unit: "hr"},
  laser_speeds: {label: "Laser Cut Speeds (IPM)", unit: "IPM", note: "Format: material|thickness"},
  laser_capable: {label: "Laser Capable Materials (max thickness in)", unit: "in"},
  waterjet_speeds: {label: "Water Jet Speeds (IPM)", unit: "IPM", note: "Values are [standard, precision]", readonly: true},
  machinability: {label: "Machinability Index (carbon steel = 1.0)", unit: "index"},
  bend_time_per_bend: {label: "Bend Time per Bend (hr)", unit: "hr"},
  weld_rates: {label: "Weld Rates (hr per weld-inch)", unit: "hr/in"},
  batch_handling: {label: "Batch Handling Time (hr)", unit: "hr"},
  density: {label: "Material Density (lb/in3)", unit: "lb/in3"},
  material_cost_per_lb: {label: "Raw Material Cost ($/lb)", unit: "$/lb"},
  tap_time_per_hole: {label: "Tapping Time per Hole (hr)", unit: "hr"},
  saw_time_per_cut: {label: "Saw Time per Cut (hr)", unit: "hr"},
  mrr_turning: {label: "CNC Turning MRR (in3/min)", unit: "in3/min"},
  mrr_milling: {label: "CNC Milling MRR (in3/min)", unit: "in3/min"},
};

var CFG_SCALARS = {
  hardware_time_per_insert: {label: "Hardware insertion time (hr per insert)", unit: "hr"},
  hardware_setup: {label: "Hardware setup time (hr)", unit: "hr"},
  tap_setup: {label: "Tap setup time (hr)", unit: "hr"},
  csink_time_per_hole: {label: "Countersink time per hole (hr)", unit: "hr"},
  csink_setup: {label: "Countersink setup time (hr)", unit: "hr"},
  passivation_time_per_sqft: {label: "Passivation time per sqft (hr)", unit: "hr"},
  passivation_setup: {label: "Passivation setup time (hr)", unit: "hr"},
  passivation_min_charge: {label: "Passivation min charge (hr)", unit: "hr"},
  passivation_parts_per_batch: {label: "Passivation parts per batch", unit: "count"},
  deburr_apex_hr_per_sqft: {label: "Deburr (Apex) time per sqft (hr)", unit: "hr"},
  deburr_hand_hr_per_part: {label: "Deburr (hand) time per part (hr)", unit: "hr"},
  deburr_apex_max_thickness: {label: "Deburr Apex max thickness (in)", unit: "in"},
  deburr_apex_max_width: {label: "Deburr Apex max width (in)", unit: "in"},
  packaging_time_per_part: {label: "Packaging time per part (hr)", unit: "hr"},
  packaging_setup: {label: "Packaging setup time (hr)", unit: "hr"},
  packaging_rate: {label: "Packaging labor rate ($/hr)", unit: "$/hr"},
  batch_threshold_hr: {label: "Batch handling threshold (hr)", unit: "hr"},
  second_op_weight_lb: {label: "Second operator weight threshold (lb)", unit: "lb"},
  second_op_size_in: {label: "Second operator size threshold (in)", unit: "in"},
  scrap_allowance_pct: {label: "Scrap allowance (%)", unit: "%"},
  minimum_order_charge: {label: "Minimum order charge ($)", unit: "$"},
  rush_premium_pct: {label: "Rush premium (%)", unit: "%"},
  material_markup_pct: {label: "Material markup (%)", unit: "%"},
  shop_markup_pct: {label: "Shop markup (%)", unit: "%"},
};

function formatKey(k) {
  return k.replace(/_/g, ' ').replace(/\|/g, ' | ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

function loadConfig() {
  if (configLoaded) return;
  fetch('/config').then(function(r) { return r.json(); }).then(function(cfg) {
    shopConfig = cfg;
    configLoaded = true;
    renderConfig(cfg);
  }).catch(function(err) {
    document.getElementById('cfgContent').innerHTML = '<div class="empty-state">Failed to load config: ' + err + '</div>';
  });
}

function renderConfig(cfg) {
  var html = '';

  // Dict sections (machine rates, laser speeds, etc.)
  var sectionKeys = Object.keys(CFG_SECTIONS);
  for (var si = 0; si < sectionKeys.length; si++) {
    var skey = sectionKeys[si];
    var sec = CFG_SECTIONS[skey];
    var data = cfg[skey];
    if (!data || typeof data !== 'object') continue;
    if (sec.readonly) {
      // Show read-only for complex types like waterjet [standard, precision]
      html += '<div class="cfg-section">';
      html += '<h3 onclick="toggleSection(this)">' + sec.label + ' <span class="toggle">click to expand</span></h3>';
      html += '<div class="cfg-body" style="display:none;">';
      if (sec.note) html += '<div style="font-size:0.75rem;color:#888;margin-bottom:0.4rem;">' + sec.note + '</div>';
      html += '<table class="cfg-table"><tr><th>Key</th><th>Value</th></tr>';
      var dk = Object.keys(data);
      for (var i = 0; i < dk.length; i++) {
        html += '<tr><td>' + formatKey(dk[i]) + '</td><td>' + JSON.stringify(data[dk[i]]) + '</td></tr>';
      }
      html += '</table></div></div>';
      continue;
    }
    html += '<div class="cfg-section">';
    html += '<h3 onclick="toggleSection(this)">' + sec.label + ' <span class="toggle">click to expand</span></h3>';
    html += '<div class="cfg-body" style="display:none;">';
    if (sec.note) html += '<div style="font-size:0.75rem;color:#888;margin-bottom:0.4rem;">' + sec.note + '</div>';
    html += '<table class="cfg-table"><tr><th>Parameter</th><th>Value (' + sec.unit + ')</th></tr>';
    var keys = Object.keys(data);
    for (var i = 0; i < keys.length; i++) {
      var v = data[keys[i]];
      if (typeof v === 'number') {
        html += '<tr><td>' + formatKey(keys[i]) + '</td>';
        html += '<td><input type="number" step="any" data-section="' + skey + '" data-key="' + keys[i] + '" value="' + v + '" onchange="markChanged(this)"></td></tr>';
      }
    }
    html += '</table></div></div>';
  }

  // Scalar values section
  html += '<div class="cfg-section">';
  html += '<h3 onclick="toggleSection(this)">Other Parameters <span class="toggle">click to expand</span></h3>';
  html += '<div class="cfg-body" style="display:none;">';
  html += '<table class="cfg-table"><tr><th>Parameter</th><th>Value</th></tr>';
  var scalarKeys = Object.keys(CFG_SCALARS);
  for (var i = 0; i < scalarKeys.length; i++) {
    var sk = scalarKeys[i];
    var sv = cfg[sk];
    if (typeof sv === 'number') {
      var info = CFG_SCALARS[sk];
      html += '<tr><td>' + info.label + '</td>';
      html += '<td><input type="number" step="any" data-scalar="' + sk + '" value="' + sv + '" onchange="markChanged(this)"> <span style="font-size:0.75rem;color:#888;">' + info.unit + '</span></td></tr>';
    }
  }
  html += '</table></div></div>';

  // Ramp table
  var ramp = cfg.ramp_table;
  if (ramp && ramp.length) {
    html += '<div class="cfg-section">';
    html += '<h3 onclick="toggleSection(this)">Production Ramp Table <span class="toggle">click to expand</span></h3>';
    html += '<div class="cfg-body" style="display:none;">';
    html += '<table class="cfg-table"><tr><th>Quantity</th><th>Ramp Factor</th></tr>';
    for (var i = 0; i < ramp.length; i++) {
      html += '<tr>';
      html += '<td><input type="number" step="1" data-ramp="' + i + '" data-ri="0" value="' + ramp[i][0] + '" onchange="markChanged(this)"></td>';
      html += '<td><input type="number" step="0.01" data-ramp="' + i + '" data-ri="1" value="' + ramp[i][1] + '" onchange="markChanged(this)"></td>';
      html += '</tr>';
    }
    html += '</table></div></div>';
  }

  document.getElementById('cfgContent').innerHTML = html;
}

function toggleSection(el) {
  var body = el.nextElementSibling;
  if (body.style.display === 'none') {
    body.style.display = 'block';
    el.querySelector('.toggle').textContent = 'click to collapse';
  } else {
    body.style.display = 'none';
    el.querySelector('.toggle').textContent = 'click to expand';
  }
}

function markChanged(el) { el.classList.add('changed'); }

function collectConfig() {
  // Start from current shopConfig and update with form values
  var cfg = JSON.parse(JSON.stringify(shopConfig));

  // Section fields
  var sInputs = document.querySelectorAll('#cfgContent input[data-section]');
  for (var i = 0; i < sInputs.length; i++) {
    var inp = sInputs[i];
    var sec = inp.getAttribute('data-section');
    var key = inp.getAttribute('data-key');
    cfg[sec][key] = parseFloat(inp.value);
  }

  // Scalar fields
  var scInputs = document.querySelectorAll('#cfgContent input[data-scalar]');
  for (var i = 0; i < scInputs.length; i++) {
    var inp = scInputs[i];
    var key = inp.getAttribute('data-scalar');
    cfg[key] = parseFloat(inp.value);
  }

  // Ramp table
  var rInputs = document.querySelectorAll('#cfgContent input[data-ramp]');
  for (var i = 0; i < rInputs.length; i++) {
    var inp = rInputs[i];
    var ri = parseInt(inp.getAttribute('data-ramp'));
    var ci = parseInt(inp.getAttribute('data-ri'));
    if (!cfg.ramp_table) cfg.ramp_table = [];
    if (!cfg.ramp_table[ri]) cfg.ramp_table[ri] = [0, 0];
    cfg.ramp_table[ri][ci] = parseFloat(inp.value);
  }

  return cfg;
}

function showCfgStatus(msg, ok) {
  var el = document.getElementById('cfgStatus');
  el.style.display = 'block';
  el.textContent = msg;
  el.style.background = ok ? '#e8f5e8' : '#fde8e8';
  el.style.color = ok ? '#1a5a1a' : '#8a1a1a';
  setTimeout(function() { el.style.display = 'none'; }, 4000);
}

function saveConfig() {
  var cfg = collectConfig();
  var btn = document.getElementById('cfgSaveBtn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  fetch('/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(cfg)})
    .then(function(r) { return r.json(); })
    .then(function(resp) {
      btn.disabled = false;
      btn.textContent = 'Save Changes';
      if (resp.ok) {
        shopConfig = cfg;
        showCfgStatus('Configuration saved. All future cost estimates will use these values.', true);
        // Remove changed highlights
        document.querySelectorAll('#cfgContent input.changed').forEach(function(el) { el.classList.remove('changed'); });
      } else {
        showCfgStatus('Save failed: ' + (resp.error || 'Unknown error'), false);
      }
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = 'Save Changes';
      showCfgStatus('Save failed: ' + err, false);
    });
}

function resetConfig() {
  if (!confirm('Reset all shop rates to factory defaults? This cannot be undone.')) return;
  var btn = document.getElementById('cfgResetBtn');
  btn.disabled = true;
  btn.textContent = 'Resetting...';
  fetch('/config/reset', {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(cfg) {
      btn.disabled = false;
      btn.textContent = 'Reset Defaults';
      if (cfg.error) {
        showCfgStatus('Reset failed: ' + cfg.error, false);
      } else {
        shopConfig = cfg;
        renderConfig(cfg);
        showCfgStatus('Configuration reset to factory defaults.', true);
      }
    })
    .catch(function(err) {
      btn.disabled = false;
      btn.textContent = 'Reset Defaults';
      showCfgStatus('Reset failed: ' + err, false);
    });
}

// --- Import Excel/CSV rates file ---
function importRatesFile(input) {
  if (!input.files || !input.files[0]) return;
  var file = input.files[0];
  var fd = new FormData();
  fd.append("file", file);
  var statusEl = document.getElementById("cfgStatus");
  statusEl.textContent = "Importing " + file.name + "...";
  statusEl.className = "cfg-status info";
  statusEl.style.display = "block";
  fetch("/config/import", { method: "POST", body: fd })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        showCfgStatus("Import failed: " + data.error, false);
      } else {
        var msg = "Imported " + data.total_sections + " section(s) from " + file.name;
        if (data.changes && data.changes.length > 0) {
          msg += ": " + data.changes.join(", ");
        }
        showCfgStatus(msg, true);
        // Reload config to reflect changes
        fetch("/config").then(function(r) { return r.json(); }).then(function(cfg) {
          if (!cfg.error) { shopConfig = cfg; renderConfig(cfg); }
        });
      }
      input.value = "";
    })
    .catch(function(err) {
      showCfgStatus("Import failed: " + err, false);
      input.value = "";
    });
}
</script>
</body>
</html>"""


def _generate_drawing_page_report(page_data, output_path, source_filename):
    """Generate a one-page PDF report for a single drawing page extraction."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DrawTitle", parent=styles["Heading1"], fontSize=14, spaceAfter=6)
    subtitle_style = ParagraphStyle("DrawSub", parent=styles["Heading2"], fontSize=11, spaceAfter=4, textColor=colors.HexColor("#336699"))
    normal = styles["Normal"]

    doc = SimpleDocTemplate(output_path, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    story = []

    pg_num = page_data.get("page", "?")
    part_num = page_data.get("part_info", {}).get("part_number", "")
    title_text = f"Drawing Extraction — Page {pg_num}"
    if part_num:
        title_text += f" — {part_num}"
    story.append(Paragraph(title_text, title_style))
    story.append(Paragraph(f"Source: {source_filename}", normal))
    if page_data.get("summary"):
        story.append(Paragraph(f"Summary: {page_data['summary']}", normal))
    story.append(Spacer(1, 12))

    # Specs table
    rows = [["Property", "Value"]]

    if page_data.get("materials"):
        mats = ", ".join(m.get("name") or m["raw_callout"] for m in page_data["materials"])
        rows.append(["Material", mats])

    if page_data.get("thickness"):
        thk = ", ".join(f'{t["value_in"]}"' + (f' ({t["gauge"]} GA)' if t.get("gauge") else "") for t in page_data["thickness"])
        rows.append(["Thickness", thk])

    if page_data.get("dimensions"):
        dims = "; ".join(
            f'{d["length"]} x {d["width"]}' + (f' x {d["height"]}' if "height" in d else "")
            for d in page_data["dimensions"][:8]
        )
        rows.append(["Dimensions", dims])

    if page_data.get("tolerances"):
        tols = ", ".join(t["raw"] for t in page_data["tolerances"][:5] if t.get("raw"))
        if tols:
            rows.append(["Tolerances", tols])

    bends = page_data.get("bends", {})
    if bends.get("radii"):
        rows.append(["Bend Radii", ", ".join(b["raw"] for b in bends["radii"])])
    if bends.get("angles"):
        rows.append(["Bend Angles", ", ".join(b["raw"] for b in bends["angles"])])

    if page_data.get("finishes"):
        rows.append(["Finish", ", ".join(f["finish"] for f in page_data["finishes"])])

    pi = page_data.get("part_info", {})
    if pi.get("quantity"):
        rows.append(["Quantity", str(pi["quantity"])])
    if pi.get("revision"):
        rows.append(["Revision", pi["revision"]])
    if pi.get("scale"):
        rows.append(["Scale", pi["scale"]])

    fab = page_data.get("likely_fab_type", "unknown")
    conf = page_data.get("fab_type_confidence", "low")
    rows.append(["Fab Type", f"{fab} ({conf} confidence)"])

    if len(rows) > 1:
        story.append(Paragraph("Extracted Specifications", subtitle_style))
        col_widths = [1.8 * inch, 5 * inch]
        tbl = Table(rows, colWidths=col_widths)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a5a2a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 12))

    # Features table
    feats = page_data.get("features", [])
    if feats:
        story.append(Paragraph("Extracted Features (from drawing callouts)", subtitle_style))
        feat_rows = [["Type", "Count", "Size", "Callout"]]
        for ft in feats:
            ftype = ft.get("type", "").replace("_", " ")
            count = str(ft.get("count", 1))
            if ft["type"] in ("round_hole", "counterbored_hole", "countersunk_hole"):
                size = f'Dia {ft.get("diameter_in", "?")}"'
                if ft.get("cbore_dia_in"):
                    size += f' CBORE {ft["cbore_dia_in"]}"'
                if ft.get("csink_dia_in"):
                    size += f' CSINK {ft["csink_dia_in"]}"'
                if ft.get("through"):
                    size += " THRU"
            elif ft["type"] == "tapped_hole":
                size = ft.get("thread_spec", "?")
                if ft.get("through"):
                    size += " THRU"
            elif ft["type"] == "slot":
                size = f'{ft.get("width_in", "?")}" x {ft.get("length_in", "?")}"'
            else:
                size = ft.get("raw", "")
            feat_rows.append([ftype, count, size, ft.get("raw", "")])
        feat_col_widths = [1.2 * inch, 0.6 * inch, 2.5 * inch, 2.5 * inch]
        feat_tbl = Table(feat_rows, colWidths=feat_col_widths)
        feat_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0066aa")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f6ff")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(feat_tbl)
        story.append(Spacer(1, 12))

    # Missing info
    missing = page_data.get("missing_info", [])
    if missing:
        story.append(Paragraph("Missing Information", subtitle_style))
        for mi in missing:
            story.append(Paragraph(f"* <b>{mi['field']}</b>: {mi['message']}", normal))
        story.append(Spacer(1, 8))

    doc.build(story)


def _generate_drawing_flat_pattern(drawing_data, out_path):
    """Generate an approximate flat pattern diagram from PDF drawing extraction data."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import math

    # Gather per-page data (use first page with meaningful data, or overall)
    pages = drawing_data.get("pages", [])
    # Pick best page: first one with dimensions and thickness
    best = None
    for pg in pages:
        has_dims = bool(pg.get("dimensions"))
        has_thk = bool(pg.get("thickness"))
        if has_dims and has_thk:
            best = pg
            break
        if has_dims and best is None:
            best = pg
    if best is None:
        best = drawing_data  # fallback to overall

    dims = best.get("dimensions", drawing_data.get("dimensions", []))
    thickness_list = best.get("thickness", drawing_data.get("thickness", []))
    bends = best.get("bends", drawing_data.get("bends", {}))
    features = best.get("features", drawing_data.get("features", []))

    # Use largest dimension set
    if dims:
        d = max(dims, key=lambda x: x.get("length", 0) * x.get("width", 0))
        overall_length = d.get("length", 10.0)
        overall_width = d.get("width", 5.0)
    else:
        overall_length = 10.0
        overall_width = 5.0

    thickness = thickness_list[0]["value_in"] if thickness_list else 0.060

    bend_angles = [b["value_deg"] for b in bends.get("angles", [])]
    bend_radii = [b["value_in"] for b in bends.get("radii", [])]
    num_bends = len(bend_angles) if bend_angles else 0
    bend_radius = bend_radii[0] if bend_radii else thickness
    k_factor = 0.44

    # Compute developed width: add bend allowances to the formed dimension
    if num_bends > 0:
        total_ba = 0
        for angle_deg in bend_angles:
            angle_rad = math.radians(angle_deg)
            ba = angle_rad * (bend_radius + k_factor * thickness)
            total_ba += ba
        flat_width = overall_width + total_ba
    else:
        flat_width = overall_width
    flat_length = overall_length

    # Store computed values back into drawing_data for frontend
    drawing_data["_computed_flat"] = {
        "flat_width_in": round(flat_width, 3),
        "flat_length_in": round(flat_length, 3),
        "thickness_in": thickness,
        "num_bends": num_bends,
        "bend_radius_in": round(bend_radius, 4),
        "k_factor": k_factor,
        "bend_angles": bend_angles,
    }

    # Create figure
    fig, ax = plt.subplots(figsize=(16, 4.5))
    LEN = flat_length
    W = flat_width

    # Outline
    ax.plot([0, LEN, LEN, 0, 0], [0, 0, W, W, 0], color="black", lw=1.4)

    # Bend lines (spaced proportionally)
    if num_bends > 0:
        segment_h = W / (num_bends + 1)
        for i, angle_deg in enumerate(bend_angles):
            y_pos = segment_h * (i + 1)
            ax.axhline(y_pos, color="tab:blue", lw=0.9, linestyle="--")
            ax.text(LEN + 0.15, y_pos, f"BEND {i+1} - {angle_deg:.0f} deg",
                    va="center", fontsize=8, color="tab:blue")

    # Feature markers (approximate positions along the centerline)
    color_map = {"round_hole": "tab:green", "tapped_hole": "tab:red",
                 "counterbored_hole": "tab:green", "countersunk_hole": "tab:green", "slot": "tab:purple"}
    marker_map = {"round_hole": "o", "tapped_hole": "^",
                  "counterbored_hole": "D", "countersunk_hole": "v", "slot": "s"}
    seen_types = set()
    feat_x_offset = 0.0
    total_features = sum(f.get("count", 1) for f in features)
    if total_features > 0 and LEN > 0:
        spacing = LEN / (total_features + 1)
    else:
        spacing = 1.0
    feat_idx = 0
    for feat in features:
        ftype = feat.get("type", "unknown")
        count = feat.get("count", 1)
        for c_i in range(count):
            feat_idx += 1
            fx = spacing * feat_idx
            fy = W / 2  # center
            label = ftype.replace("_", " ") if ftype not in seen_types else None
            ax.scatter([fx], [fy], marker=marker_map.get(ftype, "x"), s=90,
                       facecolors="none", edgecolors=color_map.get(ftype, "grey"),
                       linewidths=1.4, label=label, zorder=5)
            seen_types.add(ftype)

    ax.set_xlim(-0.5, LEN + 3.5)
    ax.set_ylim(-0.6, W + 0.6)
    ax.set_aspect("equal")
    ax.set_xlabel(f'Length (in) - {LEN:.3f}"')
    ax.set_ylabel("Developed Width (in)")
    title_parts = [f'Flat Pattern (from drawing) - {LEN:.3f}" x {W:.3f}"']
    title_parts.append(f'thickness={thickness}"')
    if num_bends > 0:
        title_parts.append(f'{num_bends} bends')
        title_parts.append(f'R={bend_radius}"')
        title_parts.append(f'K={k_factor}')
    ax.set_title(" | ".join(title_parts), fontsize=10)
    if seen_types:
        ax.legend(loc="upper left", bbox_to_anchor=(0, -0.18), ncol=5, fontsize=8, frameon=False)
    ax.text(LEN * 0.02, W + 0.15,
            "NOTE: Flat pattern approximated from drawing callouts. Bend line positions are evenly spaced estimates.",
            fontsize=7, color="grey", style="italic")

    plt.tight_layout()
    plt.savefig(out_path, dpi=220, facecolor="white")
    plt.close()


def _generate_drawing_overall_report(drawing_data, flat_pattern_path, out_path, source_filename):
    """Generate an overall PDF report for a drawing analysis (similar to STEP report)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DrawTitle", parent=styles["Heading1"], fontSize=16, spaceAfter=8)
    subtitle_style = ParagraphStyle("DrawSub", parent=styles["Heading2"], fontSize=12, spaceAfter=6, textColor=colors.HexColor("#336699"))
    normal = styles["Normal"]

    doc = SimpleDocTemplate(out_path, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    story = []

    # Title
    part_num = drawing_data.get("part_info", {}).get("part_number", "")
    title = f"Drawing Analysis Report - {source_filename}"
    if part_num:
        title += f" ({part_num})"
    story.append(Paragraph(title, title_style))
    if drawing_data.get("summary"):
        story.append(Paragraph(drawing_data["summary"], normal))
    story.append(Spacer(1, 12))

    # Geometry stats table
    computed = drawing_data.get("_computed_flat", {})
    rows = [["Property", "Value"]]
    if drawing_data.get("materials"):
        mats = ", ".join(set(m.get("name") or m["raw_callout"] for m in drawing_data["materials"][:5]))
        rows.append(["Material", mats])
    if computed.get("thickness_in"):
        thk = drawing_data.get("thickness", [{}])
        gauge_str = f' ({thk[0]["gauge"]} GA)' if thk and thk[0].get("gauge") else ""
        rows.append(["Thickness", f'{computed["thickness_in"]}"{gauge_str}'])
    if computed.get("flat_width_in"):
        rows.append(["Developed Width", f'{computed["flat_width_in"]}"'])
    if computed.get("flat_length_in"):
        rows.append(["Flat Length", f'{computed["flat_length_in"]}"'])
    if computed.get("num_bends", 0) > 0:
        rows.append(["Bends", str(computed["num_bends"])])
        rows.append(["Bend Angles", ", ".join(f'{a:.0f} deg' for a in computed.get("bend_angles", []))])
        rows.append(["Bend Radius", f'{computed.get("bend_radius_in", 0)}"'])
        rows.append(["K-factor", str(computed.get("k_factor", 0.44))])
    if drawing_data.get("finishes"):
        rows.append(["Finish", ", ".join(f["finish"] for f in drawing_data["finishes"])])
    fab = drawing_data.get("likely_fab_type", "unknown")
    conf = drawing_data.get("fab_type_confidence", "low")
    rows.append(["Fab Type", f"{fab} ({conf} confidence)"])

    if len(rows) > 1:
        story.append(Paragraph("Extracted Specifications", subtitle_style))
        col_widths = [2 * inch, 4.5 * inch]
        tbl = Table(rows, colWidths=col_widths)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a5a2a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 12))

    # Flat pattern image
    import os
    if flat_pattern_path and os.path.isfile(flat_pattern_path):
        story.append(Paragraph("Computed Flat Pattern", subtitle_style))
        try:
            img = RLImage(flat_pattern_path, width=6.5*inch, height=2*inch)
            story.append(img)
            story.append(Spacer(1, 12))
        except Exception:
            pass

    # Features table (combined from all pages)
    all_feats = drawing_data.get("features", [])
    if all_feats:
        story.append(Paragraph("Extracted Features", subtitle_style))
        feat_rows = [["Type", "Count", "Size", "Callout"]]
        for ft in all_feats:
            ftype = ft.get("type", "").replace("_", " ")
            count = str(ft.get("count", 1))
            if ft["type"] in ("round_hole", "counterbored_hole", "countersunk_hole"):
                size = f'Dia {ft.get("diameter_in", "?")}"'
                if ft.get("through"):
                    size += " THRU"
            elif ft["type"] == "tapped_hole":
                size = ft.get("thread_spec", "?")
                if ft.get("through"):
                    size += " THRU"
            elif ft["type"] == "slot":
                size = f'{ft.get("width_in", "?")}" x {ft.get("length_in", "?")}"'
            else:
                size = ft.get("raw", "")
            feat_rows.append([ftype, count, size, ft.get("raw", "")])
        feat_col_widths = [1.2*inch, 0.6*inch, 2.5*inch, 2.5*inch]
        feat_tbl = Table(feat_rows, colWidths=feat_col_widths)
        feat_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0066aa")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f6ff")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(feat_tbl)

    doc.build(story)


def _convert_cad_to_step(input_path, output_path):
    """Convert SLDPRT/SLDASM/IGS/IGES to STEP using available converters."""
    ext = os.path.splitext(input_path)[1].lower()

    # IGES: use OCP directly (already installed with CadQuery)
    if ext in ('.igs', '.iges'):
        try:
            from OCP.IGESControl import IGESControl_Reader
            from OCP.IFSelect import IFSelect_RetDone
            from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
            reader = IGESControl_Reader()
            status = reader.ReadFile(input_path)
            if status == IFSelect_RetDone:
                reader.TransferRoots()
                shape = reader.OneShape()
                writer = STEPControl_Writer()
                writer.Transfer(shape, STEPControl_AsIs)
                write_status = writer.Write(output_path)
                if write_status == 1 and os.path.exists(output_path):
                    return True, None
            return False, "Failed to read IGES file - it may be corrupt or unsupported."
        except Exception as e:
            return False, f"IGES conversion error: {str(e)[:200]}"

    # SLDPRT/SLDASM: try FreeCAD (must be installed separately)
    if ext in ('.sldprt', '.sldasm'):
        try:
            conv_script = f"""
import sys, os
try:
    import FreeCAD
    import Part
    import Import
    doc = FreeCAD.newDocument("conv")
    Import.insert("{input_path}", doc.Name)
    if not doc.Objects:
        print("ERROR:No objects imported")
        sys.exit(1)
    Part.export(doc.Objects, "{output_path}")
    FreeCAD.closeDocument(doc.Name)
    print("OK")
except Exception as e:
    print(f"ERROR:{{e}}")
    sys.exit(1)
"""
            result = subprocess.run(
                ["python3", "-c", conv_script],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True, None
            stderr = (result.stderr or result.stdout or "").strip()
            if "No module named" in stderr or "ModuleNotFoundError" in stderr:
                fmt = "SolidWorks" if ext == ".sldprt" else "SolidWorks Assembly"
                return False, (
                    f"This {fmt} file cannot be converted automatically on this server. "
                    "Please export it as STEP format from SolidWorks: "
                    "File -> Save As -> Save as type: STEP AP214 (*.step;*.stp)"
                )
            return False, f"Conversion failed: {stderr[-200:]}"
        except subprocess.TimeoutExpired:
            return False, "SolidWorks file conversion timed out."
        except Exception as e:
            return False, (
                "SLDPRT/SLDASM files require conversion to STEP format. "
                "Please export from SolidWorks: File -> Save As -> Save as type: STEP AP214 (*.step;*.stp)"
            )

    return False, f"Unsupported CAD format: {ext}"


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/analyze", methods=["POST"])
def analyze():
    if "step_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["step_file"]
    if not file or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Upload a .STEP, .STP, .SLDPRT, .SLDASM, .IGS, .IGES, .PDF, or .DWG file."}), 400

    # Determine file type
    file_ext = file.filename.rsplit(".", 1)[1].lower() if "." in file.filename else ""
    is_drawing = file_ext in ("pdf", "dwg", "dxf")
    is_native_cad = file_ext in ("sldprt", "sldasm", "igs", "iges")

    try:
        raw_density = request.form.get("density", "7.9")
        # Handle galvanized tag
        if raw_density.endswith("_galv"):
            density = float(raw_density.replace("_galv", ""))
            material = "steel"  # galvanized uses steel K-factor table
        else:
            density = float(raw_density)
            # Map density to material type for K-factor table lookup
            if density >= 7.85 and density <= 7.95:
                material = "stainless"
            else:
                material = "steel"
        k_factor = float(request.form.get("k_factor", 0.44))
    except (ValueError, TypeError):
        density, k_factor, material = 7.9, 0.44, "steel"

    # Quantity for cost estimation
    try:
        quantity = max(1, int(request.form.get("quantity", "1")))
    except (ValueError, TypeError):
        quantity = 1

    # Material name for cost engine
    material_name = DENSITY_TO_MATERIAL.get(raw_density, "Mild/Carbon Steel")

    # create job directory
    job_id = str(uuid.uuid4())[:12]
    job_dir = os.path.join(app.config["UPLOAD_FOLDER"], job_id)
    os.makedirs(job_dir, exist_ok=True)
    views_dir = os.path.join(job_dir, "views")
    os.makedirs(views_dir, exist_ok=True)

    # save uploaded file
    safe_name = secure_filename(file.filename)
    file_path = os.path.join(job_dir, safe_name)
    file.save(file_path)
    part_stem = os.path.splitext(safe_name)[0]

    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(job_dir, "geometry_extract.json")

    # ---- Drawing extraction path (PDF/DWG) ----
    if is_drawing:
        try:
            subprocess.run([
                "python3", os.path.join(script_dir, "drawing_extractor.py"),
                file_path, "--out", json_path
            ], check=True, capture_output=True, text=True, timeout=120)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            return jsonify({"error": f"Drawing extraction failed: {stderr[-300:] or e.stdout or str(e)}"}), 500
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Drawing extraction timed out (>120s)."}), 500
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {str(e)[:300]}"}), 500

        try:
            with open(json_path) as f:
                drawing_data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            return jsonify({"error": f"Failed to read extraction results: {e}"}), 500

        if "error" in drawing_data:
            return jsonify({"error": drawing_data["error"]}), 500

        # Generate flat pattern image from drawing data
        flat_path = os.path.join(job_dir, "flat_pattern.png")
        try:
            _generate_drawing_flat_pattern(drawing_data, flat_path)
        except Exception as fp_err:
            print(f"Warning: Failed to generate drawing flat pattern: {fp_err}")

        # Generate overall report PDF
        report_path = os.path.join(job_dir, f"{part_stem}_report.pdf")
        try:
            _generate_drawing_overall_report(drawing_data, flat_path, report_path, safe_name)
        except Exception as rpt_err:
            print(f"Warning: Failed to generate overall drawing report: {rpt_err}")

        # Generate per-page PDF reports if pages exist
        page_reports = {}
        if drawing_data.get("pages"):
            for pg in drawing_data["pages"]:
                pg_num = pg["page"]
                pg_report_name = f"page_{pg_num}_report.pdf"
                pg_report_path = os.path.join(job_dir, pg_report_name)
                try:
                    _generate_drawing_page_report(pg, pg_report_path, safe_name)
                    page_reports[pg_num] = pg_report_name
                except Exception as rpt_err:
                    print(f"Warning: Failed to generate report for page {pg_num}: {rpt_err}")

        # Build file URLs
        base = f"/files/{job_id}"
        files = {
            "geometry_json": f"{base}/geometry_extract.json",
            "flat_pattern": f"{base}/flat_pattern.png",
            "report_pdf": f"{base}/{part_stem}_report.pdf",
        }
        # Add per-page report URLs
        for pg_num, rpt_name in page_reports.items():
            files[f"page_{pg_num}_report"] = f"{base}/{rpt_name}"

        # Save to history
        dims = ""
        if drawing_data.get("dimensions"):
            d = drawing_data["dimensions"][0]
            dims = f'{d["length"]}" x {d["width"]}"'
        mat_name = ""
        if drawing_data.get("materials"):
            mat_name = drawing_data["materials"][0].get("name") or drawing_data["materials"][0].get("raw_callout", "")
        drawing_pages = drawing_data.get("drawing_page_count", 0)

        history_entry = {
            "job_id": job_id,
            "filename": safe_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "fab_type": drawing_data.get("likely_fab_type", "drawing"),
            "dimensions": f"{drawing_pages} drawing pages" if drawing_pages > 1 else dims,
            "num_bends": 0,
            "weight": mat_name or "PDF drawing",
            "report_url": files.get("report_pdf"),
            "json_url": files["geometry_json"],
        }
        try:
            _insert_job(history_entry)
        except Exception as db_err:
            print(f"Warning: Failed to save job to DB: {db_err}")

        return jsonify({"drawing_data": drawing_data, "files": files, "job_id": job_id})

    # ---- CAD format conversion (SLDPRT/SLDASM/IGS/IGES -> STEP) ----
    if is_native_cad:
        converted_path = os.path.join(job_dir, part_stem + "_converted.step")
        success, err_msg = _convert_cad_to_step(file_path, converted_path)
        if not success:
            return jsonify({"error": err_msg or "CAD file conversion failed."}), 400
        file_path = converted_path  # Use converted STEP file from here on

    # ---- STEP analysis path ----
    step_path = file_path
    flat_path = os.path.join(job_dir, "flat_pattern.png")
    report_path = os.path.join(job_dir, f"{part_stem}_report.pdf")

    try:
        # 1. extract geometry
        subprocess.run([
            "python3", os.path.join(script_dir, "step_quote_extract.py"),
            step_path, "--density", str(density), "--k", str(k_factor),
            "--material", material, "--out", json_path
        ], check=True, capture_output=True, text=True, timeout=120)

        # 2. render views (non-fatal - may OOM on limited memory)
        try:
            subprocess.run([
                "python3", os.path.join(script_dir, "generate_views.py"),
                step_path, "--outdir", views_dir
            ], check=True, capture_output=True, text=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as view_err:
            print(f"Warning: View generation failed (non-fatal): {view_err}")
            os.makedirs(views_dir, exist_ok=True)

        # 3. flat pattern
        subprocess.run([
            "python3", os.path.join(script_dir, "render_flat_pattern.py"),
            json_path, "--out", flat_path
        ], check=True, capture_output=True, text=True, timeout=60)

        # 4. report PDF
        subprocess.run([
            "python3", os.path.join(script_dir, "generate_report.py"),
            json_path, "--views", views_dir,
            "--flatpattern", flat_path, "--out", report_path
        ], check=True, capture_output=True, text=True, timeout=60)

    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        if "no solid bodies" in stderr.lower() or "wireframe" in stderr.lower():
            msg = "This STEP file contains no solid geometry (may be a wireframe or surface model)."
        elif "assembly" in stderr.lower():
            msg = f"Assembly detected — analyzed largest solid. Details: {stderr[-200:]}"
        elif "degenerate" in stderr.lower() or "zero volume" in stderr.lower():
            msg = "The geometry appears degenerate or has zero volume."
        else:
            msg = f"Processing failed: {stderr[-300:] or e.stdout or str(e)}"
        return jsonify({"error": msg}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timed out (>120s). The file may be too large or complex. Try a simpler part."}), 500
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)[:300]}"}), 500

    # read geometry JSON for response
    try:
        with open(json_path) as f:
            geometry = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        return jsonify({"error": f"Failed to read analysis results: {e}"}), 500

    # ── Cost estimation (uses saved shop config) ────────────────
    cost_estimate = None
    try:
        cost_geo = _build_cost_geometry(geometry)
        shop_config = _get_config()
        cost_estimate = cost_engine.estimate_cost(cost_geo, material_name, quantity, config=shop_config)
    except Exception as ce:
        print(f"Warning: Cost estimation failed (non-fatal): {ce}")

    # build file URLs
    base = f"/files/{job_id}"
    files = {
        "report_pdf": f"{base}/{part_stem}_report.pdf",
        "geometry_json": f"{base}/geometry_extract.json",
        "flat_pattern": f"{base}/flat_pattern.png",
        "view_iso": f"{base}/views/view_iso.png",
        "view_top": f"{base}/views/view_top.png",
        "view_front": f"{base}/views/view_front.png",
        "view_right": f"{base}/views/view_right.png",
    }

    # Add to job history
    env = geometry.get("envelope", {})
    bbox = env.get("bbox_mm", {})
    dims = ""
    if bbox:
        dims = f'{bbox.get("xlen",0)/25.4:.2f}" x {bbox.get("ylen",0)/25.4:.2f}" x {bbox.get("zlen",0)/25.4:.2f}"'

    history_entry = {
        "job_id": job_id,
        "filename": safe_name,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fab_type": geometry.get("fab_type", "sheet_metal"),
        "dimensions": dims,
        "num_bends": geometry.get("num_bends", 0),
        "weight": f'{env.get("mass_lb", 0):.2f} lb',
        "report_url": files["report_pdf"],
        "json_url": files["geometry_json"],
    }
    try:
        _insert_job(history_entry)
    except Exception as db_err:
        print(f"Warning: Failed to save job to DB: {db_err}")

    # Re-run nesting with actual quantity
    nesting_result = geometry.get("nesting", None)
    if quantity > 1 and geometry.get("flat_width_in") and geometry.get("flat_length_in"):
        try:
            from step_quote_extract import nest_parts
            nesting_result = nest_parts(
                geometry["flat_width_in"], geometry["flat_length_in"], quantity
            )
        except Exception as ne:
            print(f"Warning: Nesting recalc failed: {ne}")

    resp = {"geometry": geometry, "files": files, "job_id": job_id}
    if cost_estimate:
        resp["cost_estimate"] = cost_estimate
    if nesting_result:
        resp["nesting"] = nesting_result
    return jsonify(resp)


@app.route("/quote-pdf/<job_id>")
def generate_quote_pdf(job_id):
    """Generate a CMC-branded customer-facing quote PDF."""
    job_dir = os.path.join(app.config["UPLOAD_FOLDER"], job_id)
    json_path = os.path.join(job_dir, "geometry_extract.json")

    if not os.path.exists(json_path):
        return jsonify({"error": "Job not found"}), 404

    try:
        with open(json_path) as f:
            geometry = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read job data: {e}"}), 500

    # Get parameters from query string
    material_name = request.args.get("material", "Mild/Carbon Steel")
    try:
        quantity = max(1, int(request.args.get("quantity", "1")))
    except (ValueError, TypeError):
        quantity = 1
    customer_name = request.args.get("customer", "")
    po_number = request.args.get("po", "")
    notes = request.args.get("notes", "")

    # Build cost estimate
    cost_estimate = None
    try:
        cost_geo = _build_cost_geometry(geometry)
        shop_config = _get_config()
        cost_estimate = cost_engine.estimate_cost(cost_geo, material_name, quantity, config=shop_config)
    except Exception as ce:
        print(f"Warning: Cost estimation for quote PDF failed: {ce}")

    # Nesting
    nesting_result = None
    if geometry.get("flat_width_in") and geometry.get("flat_length_in"):
        try:
            from step_quote_extract import nest_parts
            nesting_result = nest_parts(
                geometry["flat_width_in"], geometry["flat_length_in"], quantity
            )
        except Exception:
            pass

    # Generate the PDF
    quote_path = os.path.join(job_dir, "customer_quote.pdf")
    try:
        _generate_customer_quote_pdf(
            geometry, cost_estimate, nesting_result,
            material_name, quantity, customer_name, po_number, notes,
            quote_path, job_id
        )
    except Exception as e:
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    return send_file(quote_path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"CMC_Quote_{job_id}.pdf")


def _generate_customer_quote_pdf(geometry, cost_est, nesting, material, qty,
                                  customer, po, notes, out_path, job_id):
    """Build a CMC-branded customer quote PDF using reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor, white, black
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable
    )
    from reportlab.lib import colors

    CMC_GREEN = HexColor("#1a3a1a")
    CMC_LIGHT = HexColor("#f0f7f0")
    BORDER = HexColor("#d0d8d0")

    styles = getSampleStyleSheet()
    title_s = ParagraphStyle('QTitle', parent=styles['Title'], fontSize=20,
                             textColor=CMC_GREEN, fontName='Helvetica-Bold', spaceAfter=4)
    sub_s = ParagraphStyle('QSub', parent=styles['Normal'], fontSize=11,
                           textColor=HexColor("#4a5a4a"), fontName='Helvetica')
    head_s = ParagraphStyle('QHead', parent=styles['Heading2'], fontSize=13,
                            textColor=CMC_GREEN, fontName='Helvetica-Bold',
                            spaceBefore=14, spaceAfter=6)
    body_s = ParagraphStyle('QBody', parent=styles['Normal'], fontSize=10,
                            leading=14, fontName='Helvetica')
    bold_s = ParagraphStyle('QBold', parent=body_s, fontName='Helvetica-Bold')
    right_s = ParagraphStyle('QRight', parent=body_s, alignment=TA_RIGHT)
    right_bold = ParagraphStyle('QRightBold', parent=bold_s, alignment=TA_RIGHT)
    small_s = ParagraphStyle('QSmall', parent=styles['Normal'], fontSize=8.5,
                             textColor=HexColor("#666666"), fontName='Helvetica')

    def header_footer(canvas, doc):
        canvas.saveState()
        # Green header bar
        canvas.setFillColor(CMC_GREEN)
        canvas.rect(0, letter[1] - 50, letter[0], 50, fill=True, stroke=False)
        canvas.setFillColor(white)
        canvas.setFont('Helvetica-Bold', 14)
        canvas.drawString(50, letter[1] - 35, "CMC Manufacturing")
        canvas.setFont('Helvetica', 9)
        canvas.drawRightString(letter[0] - 50, letter[1] - 30, "Sales Quotation")
        canvas.drawRightString(letter[0] - 50, letter[1] - 42, f"Quote #{job_id[:8].upper()}")
        # Footer
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(50, 45, letter[0] - 50, 45)
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(HexColor("#888888"))
        canvas.drawString(50, 32, "CMC Manufacturing - Precision Sheet Metal & Machining")
        canvas.drawRightString(letter[0] - 50, 32, f"Page {doc.page}")
        canvas.drawCentredString(letter[0]/2, 32,
            "This quote is valid for 30 days from the date of issue.")
        canvas.restoreState()

    doc = SimpleDocTemplate(out_path, pagesize=letter,
                            topMargin=70, bottomMargin=65,
                            leftMargin=50, rightMargin=50)
    story = []

    # Quote header info
    from datetime import datetime as dt
    today = dt.now().strftime("%B %d, %Y")

    info_data = [
        [Paragraph("<b>Date:</b>", body_s), Paragraph(today, body_s),
         Paragraph("<b>Quote #:</b>", body_s), Paragraph(job_id[:8].upper(), body_s)],
        [Paragraph("<b>Customer:</b>", body_s), Paragraph(customer or "---", body_s),
         Paragraph("<b>PO #:</b>", body_s), Paragraph(po or "---", body_s)],
        [Paragraph("<b>Material:</b>", body_s), Paragraph(material, body_s),
         Paragraph("<b>Quantity:</b>", body_s), Paragraph(str(qty), body_s)],
    ]
    t = Table(info_data, colWidths=[1.0*inch, 2.0*inch, 1.0*inch, 2.0*inch])
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=CMC_GREEN))
    story.append(Spacer(1, 8))

    # Part details
    story.append(Paragraph("Part Details", head_s))
    env = geometry.get("envelope", {})
    bbox = env.get("bbox_mm", {})
    fab_type = geometry.get("fab_type", "sheet_metal")
    fab_label = "Sheet Metal" if fab_type == "sheet_metal" else "Machined"

    part_rows = [
        [Paragraph("<b>Property</b>", bold_s), Paragraph("<b>Value</b>", bold_s)],
        [Paragraph("Fab Type", body_s), Paragraph(fab_label, body_s)],
    ]
    if bbox:
        dims = f'{bbox.get("xlen",0)/25.4:.2f}" x {bbox.get("ylen",0)/25.4:.2f}" x {bbox.get("zlen",0)/25.4:.2f}"'
        part_rows.append([Paragraph("Dimensions", body_s), Paragraph(dims, body_s)])
    if geometry.get("thickness_in"):
        gauge = f' ({geometry["gauge"]} GA)' if geometry.get("gauge") else ''
        part_rows.append([Paragraph("Thickness", body_s),
                         Paragraph(f'{geometry["thickness_in"]}"{gauge}', body_s)])
    if env.get("mass_lb"):
        part_rows.append([Paragraph("Weight", body_s),
                         Paragraph(f'{env["mass_lb"]:.2f} lb', body_s)])
    if geometry.get("num_bends", 0) > 0:
        angles = geometry.get("bend_angles_deg", [])
        angle_str = ", ".join(f"{a} deg" for a in angles)
        part_rows.append([Paragraph("Bends", body_s),
                         Paragraph(f'{geometry["num_bends"]} ({angle_str})', body_s)])
    if geometry.get("flat_width_in"):
        part_rows.append([Paragraph("Flat Pattern", body_s),
                         Paragraph(f'{geometry["flat_width_in"]}" x {geometry.get("flat_length_in", "-")}"', body_s)])

    features = geometry.get("features", [])
    if features:
        round_ct = sum(1 for f in features if f.get("type") == "round")
        rect_ct = sum(1 for f in features if f.get("type") == "square_or_rect")
        slot_ct = sum(1 for f in features if f.get("type") == "slot")
        other_ct = len(features) - round_ct - rect_ct - slot_ct
        feat_str = []
        if round_ct: feat_str.append(f"{round_ct} round")
        if rect_ct: feat_str.append(f"{rect_ct} rectangular")
        if slot_ct: feat_str.append(f"{slot_ct} slot")
        if other_ct: feat_str.append(f"{other_ct} other")
        part_rows.append([Paragraph("Features", body_s),
                         Paragraph(f'{len(features)} total ({", ".join(feat_str)})', body_s)])

    t = Table(part_rows, colWidths=[2.0*inch, 4.2*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), CMC_GREEN),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, CMC_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))

    # Bend details table (if bends exist)
    bend_details = geometry.get("bend_details", [])
    if bend_details:
        story.append(Paragraph("Bend Schedule", head_s))
        bend_rows = [
            [Paragraph("<b>#</b>", bold_s), Paragraph("<b>Angle</b>", bold_s),
             Paragraph("<b>Radius</b>", bold_s), Paragraph("<b>Length</b>", bold_s),
             Paragraph("<b>Direction</b>", bold_s)]
        ]
        for i, bd in enumerate(bend_details, 1):
            bend_rows.append([
                Paragraph(str(i), body_s),
                Paragraph(f'{bd["angle_deg"]} deg', body_s),
                Paragraph(f'{bd["inner_radius_in"]:.4f}"', body_s),
                Paragraph(f'{bd["bend_line_length_in"]:.3f}"', body_s),
                Paragraph(bd.get("direction", "---").replace("_", " ").title(), body_s),
            ])
        t = Table(bend_rows, colWidths=[0.5*inch, 1.2*inch, 1.2*inch, 1.2*inch, 1.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), CMC_GREEN),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, CMC_LIGHT]),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('ALIGN', (0,0), (0,-1), 'CENTER'),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))

    # Cost breakdown
    if cost_est:
        story.append(Paragraph("Cost Estimate", head_s))
        ops = cost_est.get("operations", [])
        cost_rows = [
            [Paragraph("<b>Operation</b>", bold_s), Paragraph("<b>Time</b>", bold_s),
             Paragraph("<b>Rate</b>", bold_s), Paragraph("<b>Cost</b>", right_bold)]
        ]
        for op in ops:
            t_hr = op.get("time_hr", 0)
            rate = op.get("rate_per_hr", 0)
            cost = op.get("cost", 0)
            time_str = f'{t_hr*60:.1f} min' if t_hr < 1 else f'{t_hr:.2f} hr'
            cost_rows.append([
                Paragraph(op.get("operation", ""), body_s),
                Paragraph(time_str, body_s),
                Paragraph(f'${rate:.0f}/hr' if rate else '---', body_s),
                Paragraph(f'${cost:.2f}', right_s),
            ])

        # Totals
        unit_cost = cost_est.get("unit_cost", 0)
        total_cost = cost_est.get("total_cost", 0)
        mat_cost = cost_est.get("material_cost", 0)

        cost_rows.append([Paragraph("", body_s), Paragraph("", body_s),
                         Paragraph("<b>Material:</b>", bold_s),
                         Paragraph(f'<b>${mat_cost:.2f}</b>', right_bold)])
        cost_rows.append([Paragraph("", body_s), Paragraph("", body_s),
                         Paragraph("<b>Unit Cost:</b>", bold_s),
                         Paragraph(f'<b>${unit_cost:.2f}</b>', right_bold)])
        if qty > 1:
            cost_rows.append([Paragraph("", body_s), Paragraph("", body_s),
                             Paragraph(f"<b>Total ({qty} pcs):</b>", bold_s),
                             Paragraph(f'<b>${total_cost:.2f}</b>', right_bold)])

        t = Table(cost_rows, colWidths=[2.2*inch, 1.2*inch, 1.2*inch, 1.6*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), CMC_GREEN),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('GRID', (0, 0), (-1, len(ops)), 0.5, BORDER),
            ('ROWBACKGROUNDS', (0, 1), (-1, len(ops)), [colors.white, CMC_LIGHT]),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('LINEABOVE', (2, len(ops)+1), (-1, len(ops)+1), 1, CMC_GREEN),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))

    # Nesting info
    if nesting:
        story.append(Paragraph("Material Nesting", head_s))
        nest_rows = [
            [Paragraph("<b>Parameter</b>", bold_s), Paragraph("<b>Value</b>", bold_s)],
            [Paragraph("Best Sheet Size", body_s), Paragraph(nesting["sheet_size_label"], body_s)],
            [Paragraph("Parts per Sheet", body_s), Paragraph(str(nesting["parts_per_sheet"]), body_s)],
            [Paragraph("Sheets Needed", body_s), Paragraph(str(nesting["sheets_needed"]), body_s)],
            [Paragraph("Material Utilization", body_s), Paragraph(f'{nesting["utilization_pct"]}%', body_s)],
            [Paragraph("Scrap", body_s), Paragraph(f'{nesting["scrap_pct"]}%', body_s)],
        ]
        t = Table(nest_rows, colWidths=[2.5*inch, 3.0*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), CMC_GREEN),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, CMC_LIGHT]),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))

    # Notes
    if notes:
        story.append(Paragraph("Notes", head_s))
        story.append(Paragraph(notes, body_s))
        story.append(Spacer(1, 12))

    # Terms
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Terms: Net 30. FOB Origin. Quote valid 30 days. "
        "Pricing based on quantities shown; changes in quantity, material, or specifications "
        "may affect pricing. Raw material pricing subject to market conditions at time of order.",
        small_s
    ))

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)


@app.route("/nest", methods=["POST"])
def recalculate_nesting():
    """Recalculate nesting with given flat dimensions and quantity."""
    try:
        data = request.get_json(force=True)
        flat_w = float(data.get("flat_width_in", 0))
        flat_l = float(data.get("flat_length_in", 0))
        qty = max(1, int(data.get("quantity", 1)))
        gap = float(data.get("gap_in", 0.25))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid parameters"}), 400

    try:
        from step_quote_extract import nest_parts
        result = nest_parts(flat_w, flat_l, qty, part_gap_in=gap)
        if result:
            return jsonify(result)
        else:
            return jsonify({"error": "Part does not fit any standard sheet"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/config", methods=["GET"])
def get_config():
    """Return the current shop configuration."""
    try:
        cfg = _get_config()
        return jsonify(cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/config", methods=["POST"])
def save_config():
    """Save updated shop configuration."""
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected JSON object"}), 400
        _save_config(data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/config/reset", methods=["POST"])
def reset_config():
    """Reset shop configuration to factory defaults."""
    try:
        default = cost_engine.get_default_config()
        _save_config(default)
        return jsonify(default)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Sheet name -> config key mapping for Excel import ─────────────
_SHEET_TO_CONFIG = {
    "machine rates":       "rates",
    "rates":               "rates",
    "setup times":         "setup",
    "setup":               "setup",
    "laser speeds":        "laser_speeds",
    "laser capable":       "laser_capable",
    "waterjet speeds":     "waterjet_speeds",
    "material costs":      "material_cost_per_lb",
    "material cost":       "material_cost_per_lb",
    "density":             "density",
    "machinability":       "machinability",
    "bend time":           "bend_time_per_bend",
    "weld rates":          "weld_rates",
    "ramp table":          "ramp_table",
    "batch handling":      "batch_handling",
    "tap time":            "tap_time_per_hole",
    "saw time":            "saw_time_per_cut",
    "mrr turning":         "mrr_turning",
    "mrr milling":         "mrr_milling",
    "other settings":      "_scalar",
}

# Scalar keys (top-level non-dict values)
_SCALAR_KEYS = {
    "hardware_time_per_insert", "hardware_setup", "tap_setup",
    "csink_time_per_hole", "csink_setup",
    "passivation_time_per_sqft", "passivation_setup",
    "passivation_min_charge", "passivation_parts_per_batch",
    "second_op_weight_lb", "second_op_size_in",
    "batch_threshold_hr",
    "deburr_apex_hr_per_sqft", "deburr_hand_hr_per_part",
    "deburr_apex_max_thickness", "deburr_apex_max_width",
    "packaging_time_per_part", "packaging_setup", "packaging_rate",
    "scrap_allowance_pct", "minimum_order_charge",
    "rush_premium_pct", "material_markup_pct", "shop_markup_pct",
}


def _parse_excel_to_config(file_bytes, filename):
    """Parse an uploaded Excel file into a partial config dict + change summary."""
    if openpyxl is None:
        raise RuntimeError("openpyxl not installed on server")

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    updates = {}
    changes = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        config_key = _SHEET_TO_CONFIG.get(sheet_name.strip().lower())
        if config_key is None:
            changes.append({"sheet": sheet_name, "status": "skipped", "reason": "unknown sheet name"})
            continue

        rows = list(ws.iter_rows(min_row=1, values_only=True))
        if len(rows) < 2:
            changes.append({"sheet": sheet_name, "status": "skipped", "reason": "empty or header-only"})
            continue

        # First row is header
        header = [str(c).strip().lower() if c else "" for c in rows[0]]
        data_rows = rows[1:]

        if config_key == "_scalar":
            # Other Settings: col0=setting name, col1=value
            count = 0
            for row in data_rows:
                if not row or not row[0]:
                    continue
                key = str(row[0]).strip()
                if key in _SCALAR_KEYS and row[1] is not None:
                    try:
                        val = float(row[1])
                        updates[key] = val
                        count += 1
                    except (ValueError, TypeError):
                        pass
            changes.append({"sheet": sheet_name, "status": "imported", "count": count})

        elif config_key == "ramp_table":
            # Ramp table: col0=qty, col1=factor
            ramp = []
            for row in data_rows:
                if not row or row[0] is None or row[1] is None:
                    continue
                try:
                    ramp.append([int(float(row[0])), float(row[1])])
                except (ValueError, TypeError):
                    pass
            if ramp:
                updates["ramp_table"] = sorted(ramp, key=lambda x: x[0])
                changes.append({"sheet": sheet_name, "status": "imported", "count": len(ramp)})
            else:
                changes.append({"sheet": sheet_name, "status": "skipped", "reason": "no valid rows"})

        elif config_key == "waterjet_speeds":
            # col0=thickness, col1=standard IPM, col2=precision IPM
            wj = {}
            for row in data_rows:
                if not row or row[0] is None:
                    continue
                try:
                    thick = str(row[0]).strip()
                    std = float(row[1]) if row[1] is not None else 0
                    prec = float(row[2]) if len(row) > 2 and row[2] is not None else std / 2
                    wj[thick] = [std, prec]
                except (ValueError, TypeError, IndexError):
                    pass
            if wj:
                updates["waterjet_speeds"] = wj
                changes.append({"sheet": sheet_name, "status": "imported", "count": len(wj)})
            else:
                changes.append({"sheet": sheet_name, "status": "skipped", "reason": "no valid rows"})

        else:
            # Standard two-column dict: col0=key, col1=value
            section = {}
            for row in data_rows:
                if not row or row[0] is None or row[1] is None:
                    continue
                try:
                    key = str(row[0]).strip()
                    val = float(row[1])
                    section[key] = val
                except (ValueError, TypeError):
                    pass
            if section:
                updates[config_key] = section
                changes.append({"sheet": sheet_name, "status": "imported", "count": len(section)})
            else:
                changes.append({"sheet": sheet_name, "status": "skipped", "reason": "no valid rows"})

    wb.close()
    return updates, changes


def _parse_csv_to_config(file_bytes):
    """Parse a CSV with columns: section, key, value."""
    import csv
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 3:
        raise ValueError("CSV must have at least 3 columns: section, key, value")

    updates = {}
    count = 0
    for row in reader:
        if len(row) < 3 or not row[0].strip() or not row[1].strip():
            continue
        section = row[0].strip()
        key = row[1].strip()
        try:
            val = float(row[2])
        except (ValueError, TypeError):
            continue
        if section in _SCALAR_KEYS:
            updates[section] = val
            count += 1
        else:
            if section not in updates:
                updates[section] = {}
            if isinstance(updates[section], dict):
                updates[section][key] = val
                count += 1
    return updates, [{"sheet": "CSV", "status": "imported", "count": count}]


@app.route("/config/import", methods=["POST"])
def import_config():
    """Import shop rates from an uploaded Excel or CSV file."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "Empty filename"}), 400

        ext = f.filename.rsplit(".", 1)[-1].lower()
        file_bytes = f.read()

        if ext in ("xlsx", "xls"):
            updates, changes = _parse_excel_to_config(file_bytes, f.filename)
        elif ext == "csv":
            updates, changes = _parse_csv_to_config(file_bytes)
        else:
            return jsonify({"error": "Unsupported file type. Use .xlsx or .csv"}), 400

        if not updates:
            return jsonify({"error": "No valid rate data found in file", "details": changes}), 400

        # Merge into existing config
        cfg = _get_config()
        for key, val in updates.items():
            if isinstance(val, dict) and key in cfg and isinstance(cfg[key], dict):
                cfg[key].update(val)
            else:
                cfg[key] = val
        _save_config(cfg)

        return jsonify({"ok": True, "changes": changes, "total_sections": len(updates)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/config/template")
def config_template():
    """Download an Excel template pre-filled with current shop rates."""
    if openpyxl is None:
        return jsonify({"error": "openpyxl not installed"}), 500

    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    cfg = _get_config()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2c3e50", end_color="2c3e50", fill_type="solid")

    def write_dict_sheet(ws, title, data, col0_name="Key", col1_name="Value"):
        ws.title = title
        ws.append([col0_name, col1_name])
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
        for k, v in data.items():
            ws.append([k, v])
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 16

    # Machine Rates
    write_dict_sheet(wb.active, "Machine Rates", cfg.get("rates", {}), "Machine", "Rate ($/hr)")

    # Setup Times
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Setup Times", cfg.get("setup", {}), "Machine", "Setup (hr)")

    # Laser Speeds
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Laser Speeds", cfg.get("laser_speeds", {}), "Material|Thickness", "Speed (IPM)")

    # Material Costs
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Material Costs", cfg.get("material_cost_per_lb", {}), "Material", "Cost ($/lb)")

    # Density
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Density", cfg.get("density", {}), "Material", "Density (lb/in3)")

    # Machinability
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Machinability", cfg.get("machinability", {}), "Material", "Index")

    # Weld Rates
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Weld Rates", cfg.get("weld_rates", {}), "Type", "hr/weld-inch")

    # Bend Time
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Bend Time", cfg.get("bend_time_per_bend", {}), "Machine", "hr/bend")

    # Tap Time
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Tap Time", cfg.get("tap_time_per_hole", {}), "Size Class", "hr/hole")

    # Saw Time
    ws = wb.create_sheet()
    write_dict_sheet(ws, "Saw Time", cfg.get("saw_time_per_cut", {}), "Material", "hr/cut")

    # MRR Turning
    ws = wb.create_sheet()
    write_dict_sheet(ws, "MRR Turning", cfg.get("mrr_turning", {}), "Material", "in3/min")

    # MRR Milling
    ws = wb.create_sheet()
    write_dict_sheet(ws, "MRR Milling", cfg.get("mrr_milling", {}), "Material", "in3/min")

    # Ramp Table
    ws = wb.create_sheet("Ramp Table")
    ws.append(["Quantity", "Ramp Factor"])
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
    for entry in cfg.get("ramp_table", []):
        ws.append(entry)
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 16

    # Other Settings (scalars)
    ws = wb.create_sheet("Other Settings")
    ws.append(["Setting", "Value"])
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
    for key in sorted(_SCALAR_KEYS):
        if key in cfg:
            ws.append([key, cfg[key]])
    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 16

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="CMC_Shop_Rates_Template.xlsx")


@app.route("/history")
def history():
    """Return recent job history from SQLite."""
    try:
        jobs = _get_jobs(MAX_HISTORY)
        # Filter out jobs whose files have been cleaned up
        valid_jobs = []
        for job in jobs:
            job_dir = os.path.join(app.config["UPLOAD_FOLDER"], job["job_id"])
            if os.path.isdir(job_dir):
                valid_jobs.append(job)
        return jsonify({"jobs": valid_jobs})
    except Exception as e:
        print(f"Warning: Failed to read job history: {e}")
        return jsonify({"jobs": []})


@app.route("/files/<job_id>/<path:filename>")
def serve_file(job_id, filename):
    job_dir = os.path.join(app.config["UPLOAD_FOLDER"], job_id)
    file_path = os.path.join(job_dir, filename)
    if not os.path.isfile(file_path):
        return "File not found", 404
    return send_file(file_path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
