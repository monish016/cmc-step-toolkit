"""
CMC STEP Quoting Toolkit - Web Application
===========================================
Upload STEP files, get geometry extraction + quoting PDF back.
Built for Chicago Metalcraft sheet-metal parts.

v2.2 - SQLite persistent history, error handling hardening
"""
import os
import uuid
import json
import shutil
import subprocess
import time
import sqlite3
from datetime import datetime
from flask import Flask, request, render_template_string, send_file, jsonify, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB max upload
app.config["UPLOAD_FOLDER"] = "/tmp/step_uploads"

ALLOWED_EXTENSIONS = {"step", "stp", "STEP", "STP"}

# ---------- SQLite persistent job history ----------
DB_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DB_DIR, "jobs.db")
MAX_HISTORY = 200


def _get_db():
    """Return a sqlite3 connection (one per call â safe for gunicorn)."""
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


# Initialise DB at import time (runs once per gunicorn worker)
_init_db()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1] in ALLOWED_EXTENSIONS


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CMC STEP Quoting Toolkit</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Arial, Helvetica, sans-serif; background: #f4f5f7; color: #1a1a1a; }
  .header { background: #1a3a1a; color: #fff; padding: 1.2rem 2rem; display: flex; align-items: center; gap: 1rem; }
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
  .progress .bar { background: #1a3a1a; height: 100%; width: 0%; transition: width 0.3s; border-radius: 4px; }
  .progress .status { font-size: 0.85rem; color: #555; margin-top: 0.5rem; }
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
  .dl-btn.secondary { background: #555; }
  .dl-btn.secondary:hover { background: #333; }
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
  @media (max-width: 600px) {
    .geo-grid { grid-template-columns: 1fr 1fr; }
    .view-grid { grid-template-columns: 1fr; }
    .params { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>CMC STEP Quoting Toolkit</h1>
    <div class="sub">Sheet-metal geometry extraction and quoting data from STEP files</div>
  </div>
</div>
<div class="container">

  <!-- Tab Navigation -->
  <div class="tab-bar">
    <div class="tab active" onclick="switchTab('upload')">Upload &amp; Analyze</div>
    <div class="tab" onclick="switchTab('history')">Recent Jobs <span id="historyCount"></span></div>
  </div>

  <!-- Upload Tab -->
  <div class="tab-content active" id="tab-upload">
    <div class="card">
      <h2>Upload STEP files</h2>
      <form id="uploadForm" enctype="multipart/form-data">
        <div class="upload-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
          <p id="dropText">Drag and drop .STEP or .STP files here, or click to browse</p>
          <div class="hint">Max 100 MB per file. Multiple files supported.</div>
        </div>
        <input type="file" id="fileInput" name="step_file" accept=".step,.stp,.STEP,.STP" multiple>
        <div class="file-list" id="fileList"></div>
        <div class="params">
          <div class="param-group">
            <label for="material">Material</label>
            <select id="material" name="material">
              <option value="7.9">Stainless Steel (SUS304) - 7.9 g/cm3</option>
              <option value="7.85">Mild/Carbon Steel - 7.85 g/cm3</option>
              <option value="2.70">Aluminum - 2.70 g/cm3</option>
              <option value="custom">Custom density...</option>
            </select>
          </div>
          <div class="param-group">
            <label for="kfactor">K-Factor (bend allowance)</label>
            <input type="number" id="kfactor" name="k_factor" value="0.44" step="0.01" min="0.1" max="0.9">
          </div>
          <div class="param-group" id="customDensityGroup" style="display:none">
            <label for="customDensity">Custom density (g/cm3)</label>
            <input type="number" id="customDensity" name="custom_density" value="7.9" step="0.01" min="0.5" max="25">
          </div>
        </div>
        <button type="submit" class="btn" id="submitBtn" disabled>Analyze STEP files</button>
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

</div>
<div class="footer">Chicago Metalcraft STEP Quoting Toolkit v2.2</div>

<script>
// --- Tab switching ---
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'history') loadHistory();
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
    return ['step','stp'].includes(ext);
  });
  addFiles(files);
});
fileInput.addEventListener("change", () => { addFiles(Array.from(fileInput.files)); });

function addFiles(files) {
  files.forEach(f => {
    if (!selectedFiles.find(sf => sf.name === f.name && sf.size === f.size)) {
      selectedFiles.push(f);
    }
  });
  renderFileList();
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
    document.getElementById("dropText").textContent = "Drag and drop .STEP or .STP files here, or click to browse";
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

  const density = materialSel.value === "custom"
    ? document.getElementById("customDensity").value
    : materialSel.value;
  const kfactor = document.getElementById("kfactor").value;

  const allResults = [];
  const totalFiles = selectedFiles.length;

  for (let i = 0; i < totalFiles; i++) {
    const f = selectedFiles[i];
    status.textContent = "Processing " + f.name + " (" + (i+1) + "/" + totalFiles + ")...";
    bar.style.width = ((i / totalFiles) * 80) + "%";

    const form = new FormData();
    form.append("step_file", f);
    form.append("density", density);
    form.append("k_factor", kfactor);

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
    bar.style.width = (((i+1) / totalFiles) * 100) + "%";
  }

  status.textContent = "Done! " + allResults.filter(r => !r.error).length + "/" + totalFiles + " files processed.";
  renderResults(allResults);
  submitBtn.disabled = false;
  selectedFiles = [];
  renderFileList();
  loadHistory();
});

// --- Render results ---
function renderResults(results) {
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

    // Views
    html += '<div class="view-grid">';
    const views = [["Isometric", r.files.view_iso], ["Top", r.files.view_top], ["Front", r.files.view_front], ["Flat Pattern", r.files.flat_pattern]];
    views.forEach(([label, url]) => {
      if (url) html += '<div class="view-item"><img src="' + url + '" alt="' + label + '" onerror="this.parentElement.style.display=\'none\'"><div class="view-label">' + label + '</div></div>';
    });
    html += '</div>';

    // Download buttons
    html += '<div class="dl-row">' +
      '<a class="dl-btn" href="' + r.files.report_pdf + '" download>Download PDF Report</a>' +
      '<a class="dl-btn secondary" href="' + r.files.geometry_json + '" download>Download JSON</a>' +
      '</div>';

    html += '</div></div>';
    container.innerHTML += html;
  });
}

function renderSheetMetal(g, env, dims) {
  let html = '<div class="geo-grid">' +
    '<div class="geo-stat"><div class="value">' + dims + '</div><div class="label">Overall Dimensions</div></div>' +
    '<div class="geo-stat"><div class="value">' + g.thickness_in + '"</div><div class="label">Sheet Thickness</div></div>' +
    '<div class="geo-stat"><div class="value">' + g.num_bends + '</div><div class="label">Bends</div></div>' +
    '<div class="geo-stat"><div class="value">' + env.mass_lb.toFixed(2) + ' lb</div><div class="label">Est. Weight</div></div>' +
    '<div class="geo-stat"><div class="value">' + g.flat_width_in + '"</div><div class="label">Flat/Dev Width</div></div>' +
    '<div class="geo-stat"><div class="value">' + g.features_raw_count + '</div><div class="label">Features</div></div>' +
    '</div>';
  html += '<table class="detail-table">' +
    '<tr><th>Bend radius</th><td>' + g.bend_radius_in + '"</td></tr>' +
    '<tr><th>Bend angles</th><td>' + (g.bend_angles_deg.length ? g.bend_angles_deg.join(", ") + ' deg' : 'None') + '</td></tr>' +
    '<tr><th>K-factor used</th><td>' + g.k_factor_assumed + '</td></tr>' +
    '<tr><th>Mass (metric)</th><td>' + env.mass_kg.toFixed(3) + ' kg</td></tr>' +
    '<tr><th>Volume</th><td>' + env.volume_mm3.toFixed(1) + ' mm3</td></tr>' +
    '<tr><th>Surface area</th><td>' + env.area_mm2.toFixed(1) + ' mm2</td></tr>' +
    '<tr><th>Unclassified features</th><td>' + (g.features_unclassified_count || 0) + '</td></tr>' +
    '</table>';
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

function toggleResult(idx) {
  const body = document.getElementById("result-" + idx);
  body.classList.toggle("collapsed");
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
    let html = '<table class="history-table"><thead><tr><th>File</th><th>Dype</th><th>Date</th><th>Dimensions</th><th>Weight</th><th>Actions</th></tr></thead><tbody>';

    data.jobs.forEach(job => {
      const ft = job.fab_type === 'machined' ? 'Machined' : 'Sheet Metal';
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
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/analyze", methods=["POST"])
def analyze():
    if "step_file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["step_file"]
    if not file or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Upload a .STEP or .STP file."}), 400

    try:
        density = float(request.form.get("density", 7.9))
        k_factor = float(request.form.get("k_factor", 0.44))
    except (ValueError, TypeError):
        density, k_factor = 7.9, 0.44

    # create job directory
    job_id = str(uuid.uuid4())[:12]
    job_dir = os.path.join(app.config["UPLOAD_FOLDER"], job_id)
    os.makedirs(job_dir, exist_ok=True)
    views_dir = os.path.join(job_dir, "views")
    os.makedirs(views_dir, exist_ok=True)

    # save uploaded file
    safe_name = secure_filename(file.filename)
    step_path = os.path.join(job_dir, safe_name)
    file.save(step_path)
    part_stem = os.path.splitext(safe_name)[0]

    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(job_dir, "geometry_extract.json")
    flat_path = os.path.join(job_dir, "flat_pattern.png")
    report_path = os.path.join(job_dir, f"{part_stem}_report.pdf")

    try:
        # 1. extract geometry
        subprocess.run([
            "python3", os.path.join(script_dir, "step_quote_extract.py"),
            step_path, "--density", str(density), "--k", str(k_factor),
            "--out", json_path
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
            msg = f"Assembly detected â analyzed largest solid. Details: {stderr[-200:]}"
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

    return jsonify({"geometry": geometry, "files": files, "job_id": job_id})


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
    return send_file(file_path)


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
