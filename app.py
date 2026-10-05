"""Web UI for darkpdf: upload a PDF, convert it, download the result."""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path

from flask import Flask, abort, render_template_string, request, send_file

from darkpdf import ConversionError, invert_raster, invert_vector

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB uploads

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>darkpdf</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #141414; color: #eee;
         display: flex; justify-content: center; padding: 40px 16px; }
  main { width: 100%; max-width: 560px; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  p.sub { color: #999; margin-bottom: 24px; font-size: 0.9rem; }
  form { background: #1e1e1e; border: 1px solid #333; border-radius: 10px; padding: 24px; }
  label { display: block; font-size: 0.85rem; color: #bbb; margin: 14px 0 4px; }
  input[type=file] { width: 100%; }
  select, input[type=number], input[type=text], input[type=password] {
    width: 100%; padding: 8px 10px; background: #2a2a2a; color: #eee;
    border: 1px solid #444; border-radius: 6px; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 14px; font-size: 0.9rem; }
  button { margin-top: 22px; width: 100%; padding: 12px; background: #4c8dff; color: #fff;
           border: 0; border-radius: 8px; font-size: 1rem; cursor: pointer; }
  button:hover { background: #3a7bf0; }
  .err { background: #4a1f1f; border: 1px solid #7a3333; color: #ffb0b0;
         padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 0.9rem; }
</style>
</head>
<body><main>
  <h1>darkpdf</h1>
  <p class="sub">Turn a white PDF into a dark-mode PDF. Your file is processed in memory and not stored.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="post" action="/convert" enctype="multipart/form-data">
    <label for="pdf">PDF file</label>
    <input type="file" id="pdf" name="pdf" accept="application/pdf,.pdf" required>

    <label for="mode">Mode</label>
    <select id="mode" name="mode">
      <option value="vector" selected>vector (default, keeps text selectable)</option>
      <option value="raster">raster (pixel-exact fallback)</option>
    </select>

    <div class="row">
      <div>
        <label for="dpi">DPI (raster)</label>
        <input type="number" id="dpi" name="dpi" value="200" min="50" max="600">
      </div>
      <div>
        <label for="format">Image format (raster)</label>
        <select id="format" name="format">
          <option value="png" selected>png</option>
          <option value="jpeg">jpeg</option>
        </select>
      </div>
      <div>
        <label for="quality">JPEG quality</label>
        <input type="number" id="quality" name="quality" value="90" min="1" max="100">
      </div>
    </div>

    <label for="password">Password (if encrypted)</label>
    <input type="password" id="password" name="password" autocomplete="off">

    <label class="check"><input type="checkbox" name="background" checked> Paint black backdrop (vector)</label>
    <label class="check"><input type="checkbox" name="keep_text" checked> Keep searchable text layer (raster)</label>

    <button type="submit">Convert &amp; download</button>
  </form>
</main></body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(_PAGE, error=None)


@app.post("/convert")
def convert():
    upload = request.files.get("pdf")
    if upload is None or not upload.filename:
        return render_template_string(_PAGE, error="Please choose a PDF file."), 400
    if not upload.filename.lower().endswith(".pdf"):
        return render_template_string(_PAGE, error="Only .pdf files are accepted."), 400

    mode = request.form.get("mode", "vector")
    try:
        dpi = max(50, min(600, int(request.form.get("dpi", 200))))
        quality = max(1, min(100, int(request.form.get("quality", 90))))
    except ValueError:
        return render_template_string(_PAGE, error="DPI and quality must be numbers."), 400
    image_format = request.form.get("format", "png")
    if image_format not in ("png", "jpeg"):
        image_format = "png"
    password = request.form.get("password") or None
    background = "background" in request.form
    keep_text = "keep_text" in request.form

    # Sweep stale converted files (older than 1 hour) from the temp dir.
    cutoff = time.time() - 3600
    for stale in Path(tempfile.gettempdir()).glob("darkpdf_*.pdf"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.pdf"
        dst = Path(tmp) / "output.pdf"
        upload.save(src)
        try:
            if mode == "raster":
                invert_raster(
                    src, dst, dpi=dpi, password=password,
                    keep_text=keep_text, image_format=image_format, quality=quality,
                )
            else:
                invert_vector(src, dst, password=password, background=background)
        except ConversionError as exc:
            return render_template_string(_PAGE, error=f"Conversion failed: {exc}"), 422
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user
            return render_template_string(_PAGE, error=f"Conversion failed: {exc}"), 422

        out_name = Path(upload.filename).stem + "_dark.pdf"
        # Copy into a persistent temp file so it survives the context manager.
        final = Path(tempfile.gettempdir()) / f"darkpdf_{uuid.uuid4().hex}.pdf"
        final.write_bytes(dst.read_bytes())

    return send_file(final, as_attachment=True, download_name=out_name,
                     mimetype="application/pdf")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
