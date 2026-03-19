#!/usr/bin/env python3
"""
Flask web interface for substack_to_epub.py

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
import time
import urllib.parse
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# token -> (file_path, download_name)
_file_store: dict = {}

app = Flask(__name__)

POPULAR_SUBSTACKS = [
    {
        "id": "heathercoxrichardson",
        "name": "Letters from an American",
        "url": "https://heathercoxrichardson.substack.com",
        "author": "Heather Cox Richardson",
        "description": "Historical perspective on current politics and events",
    },
    {
        "id": "astralcodexten",
        "name": "Astral Codex Ten",
        "url": "https://www.astralcodexten.com",
        "author": "Scott Alexander",
        "description": "Medicine, psychiatry, philosophy, and rationality",
    },
    {
        "id": "honest-broker",
        "name": "The Honest Broker",
        "url": "https://www.honest-broker.com",
        "author": "Ted Gioia",
        "description": "Music, culture, and the creative life",
    },
    {
        "id": "noahpinion",
        "name": "Noahpinion",
        "url": "https://www.noahpinion.blog",
        "author": "Noah Smith",
        "description": "Economics, policy, and technology commentary",
    },
    {
        "id": "mattstoller",
        "name": "BIG by Matt Stoller",
        "url": "https://mattstoller.substack.com",
        "author": "Matt Stoller",
        "description": "The political economy of monopoly power",
    },
    {
        "id": "slowboring",
        "name": "Slow Boring",
        "url": "https://www.slowboring.com",
        "author": "Matt Yglesias",
        "description": "Policy, politics, and the importance of being correct",
    },
    {
        "id": "pragmatic-engineer",
        "name": "The Pragmatic Engineer",
        "url": "https://newsletter.pragmaticengineer.com",
        "author": "Gergely Orosz",
        "description": "Big tech internals and engineering culture",
    },
    {
        "id": "freddiedeboer",
        "name": "Freddie deBoer",
        "url": "https://freddiedeboer.substack.com",
        "author": "Freddie deBoer",
        "description": "Education, culture, and left politics",
    },
    {
        "id": "notboring",
        "name": "Not Boring",
        "url": "https://www.notboring.co",
        "author": "Packy McCormick",
        "description": "Business strategy and ambitious technology",
    },
    {
        "id": "cremieux",
        "name": "Cremieux Recueil",
        "url": "https://www.cremieux.xyz",
        "author": "Cremieux",
        "description": "Quantitative analysis of social science questions",
    },
    {
        "id": "persuasion",
        "name": "Persuasion",
        "url": "https://www.persuasion.community",
        "author": "Yascha Mounk",
        "description": "Ideas for an open society",
    },
    {
        "id": "doomberg",
        "name": "Doomberg",
        "url": "https://doomberg.substack.com",
        "author": "Doomberg",
        "description": "Energy, finance, and policy from an anonymous green chicken",
    },
]

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


def _total_from_sitemap(base_url: str) -> int:
    """Fetch the sitemap and count post entries. Returns 0 on failure.

    Handles both flat Substack sitemaps (count /p/ URLs) and sitemap index
    files (follow the sub-sitemap whose name contains 'post').
    """
    try:
        resp = requests.get(f"{base_url}/sitemap.xml", headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            return 0
        xml = resp.text

        # Sitemap index — find and fetch the posts sub-sitemap
        if "<sitemapindex" in xml:
            sub_urls = re.findall(r"<loc>(.*?)</loc>", xml)
            posts_sitemap = next(
                (u for u in sub_urls if "post" in u.lower()),
                None,
            )
            if not posts_sitemap:
                return 0
            resp2 = requests.get(posts_sitemap, headers=BROWSER_HEADERS, timeout=15)
            if resp2.status_code != 200:
                return 0
            xml = resp2.text

        # Flat sitemap: count /p/ URLs (Substack format)
        p_count = len(re.findall(r"<loc>[^<]+/p/[^<]+</loc>", xml))
        if p_count > 0:
            return p_count

        # Non-Substack flat sitemap: count all <url> entries that aren't
        # obviously non-post pages (archive, about, podcast, tags, authors)
        all_locs = re.findall(r"<loc>(.*?)</loc>", xml)
        skip = re.compile(r"/(archive|about|podcast|tag|author|category|page)/|/$")
        return sum(1 for u in all_locs if not skip.search(u))

    except Exception:
        pass
    return 0


def _free_paid_from_archive(base_url: str):
    """Paginate the archive to count free/paid posts, capped at MAX_PAGES.

    The offset must advance by the actual batch size (not page_size) because
    Substack can return a short first page even when thousands more exist.
    Returns (free, paid, capped).
    """
    MAX_PAGES = 12
    page_size = 50
    free = 0
    paid = 0
    offset = 0

    for _ in range(MAX_PAGES):
        try:
            resp = requests.get(
                f"{base_url}/api/v1/archive",
                params={"limit": page_size, "offset": offset, "sort": "new"},
                headers=BROWSER_HEADERS,
                timeout=10,
            )
            if resp.status_code == 429:
                time.sleep(3)
                continue
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            for p in batch:
                if p.get("audience", "everyone") == "everyone":
                    free += 1
                else:
                    paid += 1
            offset += len(batch)
        except Exception:
            break
    else:
        return free, paid, True  # hit page cap — more posts exist

    return free, paid, False


def fetch_post_counts(base_url: str) -> dict:
    """Return total / free / paid post counts for a publication.

    Total comes from the sitemap (one request, always exact).
    Free/paid come from archive pagination (capped at ~600 posts for speed).
    Both fetches run in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        sitemap_future = ex.submit(_total_from_sitemap, base_url)
        archive_future = ex.submit(_free_paid_from_archive, base_url)
        sitemap_total = sitemap_future.result()
        free, paid, capped = archive_future.result()

    # Prefer the sitemap total (exact); fall back to archive sum if sitemap failed
    total = sitemap_total if sitemap_total > 0 else (free + paid)
    return {"total": total, "free": free, "paid": paid, "capped": capped}


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
    return render_template("index.html", substacks=POPULAR_SUBSTACKS)


@app.route("/api/counts")
def get_counts():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url parameter required"}), 400
    try:
        base_url = normalize_url(urllib.parse.unquote(url))
        counts = fetch_post_counts(base_url)
        return jsonify(counts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download")
def download_epub():
    url = request.args.get("url", "").strip()
    session_id = request.args.get("session_id", "").strip()
    sort = request.args.get("sort", "new")
    limit = request.args.get("limit", "").strip()

    if not url:
        return jsonify({"error": "url required"}), 400

    base_url = normalize_url(url)
    script_path = Path(__file__).parent / "substack_to_epub.py"
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
    print("Starting Substack Downloader web interface...")
    print("Open http://localhost:5000 in your browser")
    app.run(debug=False, port=5000)
