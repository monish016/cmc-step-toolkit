"""
CMC STEP Quoting Toolkit - Web Application
===========================================
Upload a STEP file, get geometry extraction + quoting PDF back.
Built for Chicago Metalcraft sheet-metal parts.
"""
import os
import uuid
import json
import shutil
import subprocess
from flask import Flask, request, render_template_string, send_file, jsonify, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB max upload
app.config["UPLOAD_FOLDER"] = "/tmp/step_uploads"

ALLOWED_EXTENSIONS = {"step", "stp", "STEP", "STP"}

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
  .container { max-width: 800px; margin: 2rem auto; padding: 0 1.5rem; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); padding: 2rem; margin-bottom: 1.5rem; }
  .card h2 { font-size: 1.15rem; margin-bottom: 1rem; color: #1a3a1a; }
  .upload-zone { border: 2px dashed #b0b8c0; border-radius: 8px; padding: 2.5rem 1rem; text-align: center; cursor: pointer; transition: border-color 0.2s, background 0.2s; }
  .upload-zone:hover, .upload-zone.dragover { border-color: #1a3a1a; background: #f0f7f0; }
  .upload-zone p { font-size: 1rem; color: #555; margin-bottom: 0.5rem; }
  .upload-zone .hint { font-size: 0.8rem; color: #999; }
  input[type="file"] { display: none; }
  .params { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1.2rem; }
  .param-group label { display: block; font-size: 0.85rem; font-weight: 600; color: #333; margin-bottom: 0.3rem; }
  .param-group input, .param-group select { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; }
  .btn { display: inline-block; background: #1a3a1a; color: #fff; padding: 0.7rem 2rem; border: none; border-radius: 6px; font-size: 1rem; font-weight: 600; cursor: pointer; margin-top: 1.2rem; transition: background 0.2s; }
  .btn:hover { background: #2a5a2a; }
  .btn:disabled { background: #999; cursor: not-allowed; }
  .progress { display: none; margin-top: 1rem; }
  .progress .bar-wrap { background: #e0e0e0; border-radius: 4px; height: 8px; overflow: hidden; }
  .progress .bar { background: #1a3a1a; height: 100%; width: 0%; transition: width 0.3s; border-radius: 4px; }
  .progress .status { font-size: 0.85rem; color: #555; margin-top: 0.5rem; }
  .results { display: none; }
  .result-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }
  .result-item { background: #f8faf8; border: 1px solid #dde5dd; border-radius: 6px; padding: 1rem; text-align: center; }
  .result-item img { max-width: 100%; border-radius: 4px; margin-bottom: 0.5rem; }
  .result-item a { color: #1a3a1a; font-weight: 600; text-decoration: none; }
  .result-item a:hover { text-decoration: underline; }
  .dl-btn { display: inline-block; background: #2a5a2a; color: #fff; padding: 0.5rem 1.5rem; border-radius: 4px; text-decoration: none; font-weight: 600; margin-top: 0.5rem; }
  .dl-btn:hover { background: #1a3a1a; }
  .error { color: #c00; background: #fff0f0; border: 1px solid #fcc; border-radius: 6px; padding: 1rem; margin-top: 1rem; display: none; }
  .data-table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.85rem; }
  .data-table th, .data-table td { padding: 0.4rem 0.6rem; border: 1px solid #ddd; text-align: left; }
  .data-table th { background: #f0f4f0; font-weight: 600; }
  .footer { text-align: center; padding: 2rem; font-size: 0.8rem; color: #999; }
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
  <div class="card">
    <h2>Upload STEP file</h2>
    <form id="uploadForm" enctype="multipart/form-data">
      <div class="upload-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
        <p id="fileName">Drag and drop a .STEP or .STP file here, or click to browse</p>
        <div class="hint">Max file size: 100 MB</div>
      </div>
      <input type="file" id="fileInput" name="step_file" accept=".step,.stp,.STEP,.STP">
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
      <button type="submit" class="btn" id="submitBtn" disabled>Analyze STEP file</button>
    </form>
    <div class="progress" id="progress">
      <div class="bar-wrap"><div class="bar" id="progressBar"></div></div>
      <div class="status" id="progressStatus">Uploading file...</div>
    </div>
    <div class="error" id="errorBox"></div>
  </div>

  <div class="card results" id="resultsCard">
    <h2>Results</h2>
    <div id="summaryTable"></div>
    <div class="result-grid" id="resultGrid"></div>
    <div style="text-align:center; margin-top:1.5rem;">
      <a class="dl-btn" id="dlReport" href="#" download>Download Quoting PDF</a>
      <a class="dl-btn" id="dlJson" href="#" download style="background:#555">Download JSON Data</a>
    </div>
  </div>
</div>
<div class="footer">Chicago Metalcraft STEP Quoting Toolkit v1.0</div>

<script>
const dropZone = document.getElementById("dropZone");
const fileInput = document.getElementById("fileInput");
const fileName = document.getElementById("fileName");
const submitBtn = document.getElementById("submitBtn");
const materialSel = document.getElementById("material");
const customGroup = document.getElementById("customDensityGroup");

materialSel.addEventListener("change", () => {
  customGroup.style.display = materialSel.value === "custom" ? "block" : "none";
});

["dragenter","dragover"].forEach(e => dropZone.addEventListener(e, ev => { ev.preventDefault(); dropZone.classList.add("dragover"); }));
["dragleave","drop"].forEach(e => dropZone.addEventListener(e, ev => { ev.preventDefault(); dropZone.classList.remove("dragover"); }));
dropZone.addEventListener("drop", ev => {
  const files = ev.dataTransfer.files;
  if (files.length) { fileInput.files = files; handleFile(); }
});
fileInput.addEventListener("change", handleFile);

function handleFile() {
  if (fileInput.files.length) {
    const f = fileInput.files[0];
    fileName.textContent = f.name + " (" + (f.size/1024/1024).toFixed(1) + " MB)";
    submitBtn.disabled = false;
  }
}

document.getElementById("uploadForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData();
  form.append("step_file", fileInput.files[0]);
  const density = materialSel.value === "custom"
    ? document.getElementById("customDensity").value
    : materialSel.value;
  form.append("density", density);
  form.append("k_factor", document.getElementById("kfactor").value);

  submitBtn.disabled = true;
  const progress = document.getElementById("progress");
  const bar = document.getElementById("progressBar");
  const status = document.getElementById("progressStatus");
  const errorBox = document.getElementById("errorBox");
  errorBox.style.display = "none";
  progress.style.display = "block";
  document.getElementById("resultsCard").style.display = "none";

  const steps = [
    "Uploading file...",
    "Loading STEP geometry...",
    "Classifying faces and detecting bends...",
    "Computing flat pattern...",
    "Clustering features (holes, slots)...",
    "Rendering orthographic views...",
    "Drawing flat pattern...",
    "Generating quoting PDF..."
  ];
  let step = 0;
  const ticker = setInterval(() => {
    if (step < steps.length) {
      bar.style.width = ((step+1)/steps.length*90) + "%";
      status.textContent = steps[step];
      step++;
    }
  }, 2500);

  try {
    const resp = await fetch("/analyze", { method: "POST", body: form });
    clearInterval(ticker);
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.error || "Server error");
    }
    const data = await resp.json();
    bar.style.width = "100%";
    status.textContent = "Done!";

    // show results
    const rc = document.getElementById("resultsCard");
    rc.style.display = "block";

    // summary table
    const g = data.geometry;
    const env = g.envelope;
    document.getElementById("summaryTable").innerHTML = `
      <table class="data-table">
        <tr><th>Property</th><th>Value</th></tr>
        <tr><td>Overall dimensions</td><td>${(env.bbox_mm.xlen/25.4).toFixed(2)}" x ${(env.bbox_mm.ylen/25.4).toFixed(2)}" x ${(env.bbox_mm.zlen/25.4).toFixed(2)}"</td></tr>
        <tr><td>Sheet thickness</td><td>${g.thickness_in}" (derived)</td></tr>
        <tr><td>Bend radius</td><td>${g.bend_radius_in}"</td></tr>
        <tr><td>Number of bends</td><td>${g.num_bends}</td></tr>
        <tr><td>Bend angles</td><td>${g.bend_angles_deg.join(", ")}deg</td></tr>
        <tr><td>Flat/developed width</td><td>${g.flat_width_in}" (K=${g.k_factor_assumed})</td></tr>
        <tr><td>Est. weight</td><td>${env.mass_lb.toFixed(2)} lb (${env.mass_kg.toFixed(2)} kg)</td></tr>
        <tr><td>Features detected</td><td>${g.features_raw_count} (${g.features_unclassified_count} unclassified)</td></tr>
      </table>`;

    // images
    const grid = document.getElementById("resultGrid");
    grid.innerHTML = "";
    for (const [label, url] of [["Isometric View", data.files.view_iso], ["Top View", data.files.view_top],
                                  ["Flat Pattern", data.files.flat_pattern], ["Front View", data.files.view_front]]) {
      if (url) grid.innerHTML += `<div class="result-item"><img src="${url}" alt="${label}"><div>${label}</div></div>`;
    }

    document.getElementById("dlReport").href = data.files.report_pdf;
    document.getElementById("dlJson").href = data.files.geometry_json;
  } catch (err) {
    clearInterval(ticker);
    errorBox.textContent = "Error: " + err.message;
    errorBox.style.display = "block";
    bar.style.width = "0%";
    status.textContent = "";
  }
  submitBtn.disabled = false;
});
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

    density = float(request.form.get("density", 7.9))
    k_factor = float(request.form.get("k_factor", 0.44))

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

        # 2. render views
        subprocess.run([
            "python3", os.path.join(script_dir, "generate_views.py"),
            step_path, "--outdir", views_dir
        ], check=True, capture_output=True, text=True, timeout=120)

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
        return jsonify({"error": f"Processing failed: {e.stderr or e.stdout or str(e)}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timed out. The file may be too complex."}), 500

    # read geometry JSON for response
    with open(json_path) as f:
        geometry = json.load(f)

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

    return jsonify({"geometry": geometry, "files": files, "job_id": job_id})


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
