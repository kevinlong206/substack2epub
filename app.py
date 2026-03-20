#!/usr/bin/env python3
"""
Flask web interface for substack2epub.py

Run:
    python3 app.py
Then open http://localhost:5000
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# token -> (file_path, download_name)
_file_store: dict = {}

app = Flask(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    return url


@app.route("/api/detect-session")
def detect_session():
    """Try to read substack.sid from the local browser's cookie store."""
    try:
        import browser_cookie3
        for loader in (browser_cookie3.chrome, browser_cookie3.firefox, browser_cookie3.safari):
            try:
                cj = loader(domain_name="substack.com")
                for c in cj:
                    if c.name == "substack.sid":
                        return jsonify({"found": True, "sid": c.value})
            except Exception:
                continue
    except ImportError:
        return jsonify({"found": False, "error": "browser-cookie3 not installed"})
    return jsonify({"found": False})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download")
def download_epub():
    url = request.args.get("url", "").strip()
    session_id = request.args.get("session_id", "").strip()
    sort = request.args.get("sort", "new")
    limit = request.args.get("limit", "").strip()

    if not url:
        return jsonify({"error": "url required"}), 400

    base_url = normalize_url(url)
    script_path = Path(__file__).parent / "substack2epub.py"
    output_dir = tempfile.mkdtemp(prefix="substack_epub_")
    output_path = os.path.join(output_dir, "output.epub")

    cmd = [sys.executable, str(script_path), base_url, "--output", output_path, "--sort", sort]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if limit and limit.isdigit():
        cmd.extend(["--limit", limit])

    def generate():
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in process.stdout:
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            process.wait()
            if process.returncode == 0 and os.path.exists(output_path):
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                safe_name = re.sub(r"[^\w\- ]", "", request.args.get("name", "substack")).strip().replace(" ", "_") or "substack"
                token = secrets.token_urlsafe(16)
                _file_store[token] = (output_path, f"{safe_name}.epub")
                yield f"data: {json.dumps({'done': True, 'token': token, 'size_mb': round(size_mb, 1)})}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'Download failed', 'code': process.returncode})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/file/<token>")
def serve_file(token):
    entry = _file_store.get(token)
    if not entry:
        return "File not found", 404
    path, name = entry
    if not os.path.exists(path):
        return "File no longer available", 410
    return send_file(path, as_attachment=True, download_name=name)


if __name__ == "__main__":
    print("Starting substack2epub web interface...")
    print("Open http://localhost:5000 in your browser")
    app.run(debug=False, port=5000)
