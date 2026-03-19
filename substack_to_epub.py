#!/usr/bin/env python3
"""
substack_to_epub.py — Download all posts from a Substack and generate an EPUB.

Usage:
    python3 substack_to_epub.py <substack_url> [options]

Examples:
    python3 substack_to_epub.py https://example.substack.com
    python3 substack_to_epub.py https://example.substack.com --session-id YOUR_SID
    python3 substack_to_epub.py https://example.substack.com --output my_book.epub
    python3 substack_to_epub.py https://example.substack.com --limit 20

To get your session ID (required for paywalled posts):
    1. Open your browser and log in to Substack
    2. Open DevTools → Application → Cookies
    3. Find the cookie named "substack.sid" for the substack domain
    4. Copy its value and pass it with --session-id
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image
from ebooklib import epub

SESSION_CACHE_PATH = Path.home() / ".config" / "substack_epub" / "session.json"

# Kindle Paperwhite specs: 1072x1448px, 300 DPI, 6" grayscale display
# We target a max width of 1072px and convert to grayscale
KINDLE_MAX_WIDTH = 1072
KINDLE_JPEG_QUALITY = 82
REQUEST_DELAY = 0.5  # seconds between API calls to be polite


def make_session(session_id) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    })
    if session_id:
        # Set with explicit domain so it doesn't conflict with domain-specific
        # cookies the server may set later (both share the same domain key).
        session.cookies.set("substack.sid", session_id,
                            domain=".substack.com", path="/")
    return session


def load_cached_session():
    """Return a cached substack.sid if one exists, else None."""
    try:
        if SESSION_CACHE_PATH.exists():
            data = json.loads(SESSION_CACHE_PATH.read_text())
            return data.get("substack.sid")
    except Exception:
        pass
    return None


def save_cached_session(sid: str) -> None:
    """Persist the substack.sid cookie so future runs don't need re-login."""
    SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_CACHE_PATH.write_text(json.dumps({"substack.sid": sid}, indent=2))
    SESSION_CACHE_PATH.chmod(0o600)  # owner-readable only


def clear_cached_session() -> None:
    try:
        SESSION_CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def login_interactive() -> str:
    """
    Perform an interactive Substack login via the magic-link email flow.

    Steps:
      1. Ask for the user's email address.
      2. POST to Substack's email-login API to trigger a magic-link email.
      3. Ask the user to paste the magic-link URL from their inbox.
      4. Follow the URL with a requests session to receive the session cookie.
      5. Return the substack.sid value.

    No browser or extra dependencies are required.
    """
    print("\n=== Substack Login ===")
    email = input("Email address: ").strip()
    if not email:
        raise SystemExit("No email provided.")

    temp_session = requests.Session()
    temp_session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Referer": "https://substack.com/",
        "Origin": "https://substack.com",
    })

    # Ask Substack to send the magic-link email
    print(f"Sending magic link to {email} ...", end="", flush=True)
    resp = temp_session.post(
        "https://substack.com/api/v1/email-login",
        json={"email": email, "redirect": "/", "for_pub": ""},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Login request failed ({resp.status_code}): {resp.text[:200]}"
        )
    print(" sent.")

    print(
        "\nCheck your inbox for an email from Substack.\n"
        "Right-click the login button / link and copy the URL, then paste it below.\n"
        "(The URL will look like: https://substack.com/sign-in?token=...)\n"
    )
    magic_url = input("Magic link URL: ").strip()
    if not magic_url:
        raise SystemExit("No URL provided.")

    # Follow the magic link — Substack will redirect and set the cookie
    print("Verifying link ...", end="", flush=True)
    resp = temp_session.get(magic_url, allow_redirects=True, timeout=30)

    sid = temp_session.cookies.get("substack.sid")
    if not sid:
        # Some redirect chains land on a page that includes the cookie in
        # a JSON body or a Set-Cookie header — check explicitly
        for r in resp.history + [resp]:
            sid = r.cookies.get("substack.sid")
            if sid:
                break

    if not sid:
        raise RuntimeError(
            "Login failed: no substack.sid cookie received after following the magic link.\n"
            "Make sure you copied the full URL including the token parameter."
        )

    print(" OK")
    return sid


def get_publication_info(base_url: str, session: requests.Session) -> dict:
    """Fetch publication metadata. Falls back gracefully on auth errors."""
    for path in ["/api/v1/publication", "/api/v1/publication/by_author"]:
        try:
            resp = session.get(f"{base_url}{path}", timeout=30)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    # Last resort: parse the homepage for basic metadata
    try:
        resp = session.get(base_url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        name = (
            (soup.find("meta", property="og:site_name") or {}).get("content")
            or (soup.find("title") and soup.find("title").get_text(strip=True).split("|")[0].strip())
            or base_url.split("//")[-1].split(".")[0].title()
        )
        desc = (soup.find("meta", property="og:description") or {}).get("content", "")
        author = (soup.find("meta", attrs={"name": "author"}) or {}).get("content", name)
        return {"name": name, "description": desc, "author_name": author}
    except Exception:
        return {"name": base_url.split("//")[-1].split(".")[0].title()}


def fetch_all_posts(base_url: str, session: requests.Session, limit) -> list:
    """Fetch all post stubs from the API, paginating as needed.

    When a limit is set and the session is unauthenticated, fetching continues
    until at least `limit` *free* posts have been collected so that paid posts
    skipped during build don't reduce the final chapter count below the limit.
    """
    authenticated = bool(session.cookies.get("substack.sid"))
    posts = []
    offset = 0
    page_size = 50
    print("Fetching post list...", end="", flush=True)

    while True:
        # /api/v1/archive is the correct Substack endpoint for post listings
        url = f"{base_url}/api/v1/archive"
        params = {"limit": page_size, "offset": offset, "sort": "new"}
        for attempt in range(5):
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"\n  Rate limited — waiting {wait}s ...", end="", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        if not resp.content.strip():
            break
        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        print(f" {len(posts)}", end="", flush=True)
        if limit:
            if authenticated:
                # Every post is downloadable; stop once we have enough total.
                if len(posts) >= limit:
                    posts = posts[:limit]
                    break
            else:
                # Paid posts will be skipped; keep fetching until we have
                # at least `limit` free posts to fill the requested count.
                free_so_far = sum(
                    1 for p in posts if p.get("audience", "everyone") == "everyone"
                )
                if free_so_far >= limit:
                    break
        # Advance by the actual number returned, not page_size.
        # Substack's first page can return fewer than page_size even when more
        # posts exist further back, so we must not stop on a short batch.
        offset += len(batch)
        time.sleep(REQUEST_DELAY)

    print(f"\nFound {len(posts)} posts.")
    return posts


def fetch_post_content(base_url: str, slug: str, session: requests.Session) -> dict:
    """Fetch full post content including body_html."""
    url = f"{base_url}/api/v1/posts/{slug}"
    for attempt in range(5):
        resp = session.get(url, timeout=30)
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"\n  Rate limited — waiting {wait}s ...", end="", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()  # raise after exhausting retries


def fetch_post_html_fallback(post_url: str, session: requests.Session) -> str:
    """
    Scrape the post HTML directly from the post page as a fallback when
    the API doesn't return body_html (e.g., custom domains or paywall).
    Returns the inner content HTML or empty string on failure.
    """
    try:
        resp = session.get(post_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Substack renders post body in .available-content or .body.markup
        for sel in [".available-content", ".body.markup", "div.post-content", "article"]:
            el = soup.select_one(sel)
            if el:
                return str(el)
        return ""
    except Exception as e:
        print(f"\n  Warning: HTML fallback failed: {e}")
        return ""


def process_image(img_data: bytes, url: str):
    """
    Convert image to grayscale JPEG sized for Kindle.
    Returns JPEG bytes or None on failure.
    """
    try:
        img = Image.open(io.BytesIO(img_data))
        # Convert to RGB first (handles PNG with transparency, etc.)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Convert to grayscale for Kindle
        img = img.convert("L")

        # Resize if wider than Kindle screen
        if img.width > KINDLE_MAX_WIDTH:
            ratio = KINDLE_MAX_WIDTH / img.width
            new_size = (KINDLE_MAX_WIDTH, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=KINDLE_JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"\n  Warning: could not process image {url}: {e}")
        return None


def download_and_embed_images(
    html: str,
    session: requests.Session,
    book: epub.EpubBook,
    image_map: dict,
) -> str:
    """
    Download all images in the HTML, embed them in the EPUB, and rewrite src attributes.
    image_map is a shared dict {original_url: html_relative_src} to deduplicate across chapters.

    Chapter files live at posts/*.xhtml, so image paths in HTML must be ../images/foo.jpg
    while the epub item file_name (used in the OPF manifest) stays as images/foo.jpg.
    """
    soup = BeautifulSoup(html, "html.parser")

    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue

        # Use srcset's last (highest-res) source if available, otherwise src.
        # srcset entries are separated by ", " (comma + space). We must NOT
        # split on bare "," because Substack CDN URLs contain commas in their
        # transform params (e.g. "w_1456,c_limit,f_auto,q_auto:good,...").
        srcset = img_tag.get("srcset", "")
        if srcset:
            candidates = [s.strip().split()[0] for s in re.split(r",\s+", srcset) if s.strip()]
            if candidates:
                src = candidates[-1]

        if src in image_map:
            img_tag["src"] = image_map[src]  # already the images/... relative path
            img_tag.attrs.pop("srcset", None)
            img_tag.attrs.pop("data-src", None)
            continue

        try:
            resp = session.get(src, timeout=30)
            resp.raise_for_status()
            img_data = process_image(resp.content, src)
            if img_data is None:
                continue

            # Build a safe filename
            parsed = urllib.parse.urlparse(src)
            basename = os.path.basename(parsed.path)
            basename = re.sub(r"[^\w.\-]", "_", basename)
            stem = Path(basename).stem[:60]
            epub_file_name = f"images/{stem}_{len(image_map)}.jpg"
            # Chapters live at the EPUB root alongside images/, so no ../
            html_src = f"images/{stem}_{len(image_map)}.jpg"

            img_item = epub.EpubItem(
                uid=f"img_{len(image_map)}",
                file_name=epub_file_name,
                media_type="image/jpeg",
                content=img_data,
            )
            book.add_item(img_item)
            image_map[src] = html_src

            img_tag["src"] = html_src
            img_tag.attrs.pop("srcset", None)
            img_tag.attrs.pop("data-src", None)

            time.sleep(0.1)
        except Exception as e:
            print(f"\n  Warning: could not download image {src}: {e}")

    return str(soup)


def clean_html_for_epub(html: str, title: str) -> str:
    """Strip Substack-specific clutter and wrap in valid XHTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove subscribe buttons, paywall divs, share widgets
    for sel in [
        ".subscription-widget-wrap",
        ".subscribe-widget",
        ".paywall",
        ".post-footer",
        ".share-dialog",
        "[data-component-name='SubscribeWidget']",
        ".button-wrapper",
        "button",
        "iframe",
        "script",
        "style",
    ]:
        for el in soup.select(sel):
            el.decompose()

    # Remove <source> elements — their srcsets point to external CDN URLs
    # (often WebP) that don't work in an offline EPUB. The <img> src already
    # holds the locally-embedded path set by download_and_embed_images.
    for source in soup.find_all("source"):
        source.decompose()

    # Unwrap <picture> elements: keep the inner <img>, drop the wrapper.
    for picture in soup.find_all("picture"):
        picture.unwrap()

    # Remove empty paragraphs
    for p in soup.find_all("p"):
        if not p.get_text(strip=True) and not p.find("img"):
            p.decompose()

    body = str(soup)
    return f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
<head>
  <title>{title}</title>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
</head>
<body>
{body}
</body>
</html>"""


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:60]


def build_epub(
    posts_data: list[dict],
    pub_info: dict,
    session: requests.Session,
    output_path: str,
    post_limit: int = None,
) -> None:
    book = epub.EpubBook()

    title = pub_info.get("name", "Substack Publication")
    author = pub_info.get("author_name") or pub_info.get("name", "Unknown Author")
    description = pub_info.get("description", "")

    book.set_identifier(f"substack-{slugify(title)}-{int(datetime.now().timestamp())}")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    if description:
        book.add_metadata("DC", "description", description)

    # Cover image
    cover_url = pub_info.get("logo_url") or pub_info.get("cover_photo_url")
    if cover_url:
        try:
            resp = session.get(cover_url, timeout=30)
            cover_data = process_image(resp.content, cover_url)
            if cover_data:
                book.set_cover("cover.jpg", cover_data)
                print("Cover image added.")
        except Exception as e:
            print(f"Warning: could not fetch cover: {e}")

    # CSS for Kindle
    css = epub.EpubItem(
        uid="style",
        file_name="style/main.css",
        media_type="text/css",
        content=b"""
body { font-family: serif; font-size: 1em; margin: 0.5em 1em; color: #000; }
h1 { font-size: 1.6em; margin-top: 1.5em; margin-bottom: 0.3em; }
h2 { font-size: 1.3em; margin-top: 1.2em; }
h3 { font-size: 1.1em; }
p { margin: 0.5em 0; line-height: 1.5; }
img { max-width: 100%; height: auto; display: block; margin: 0.8em auto; }
blockquote { margin: 0.8em 2em; font-style: italic; border-left: 3px solid #666; padding-left: 0.8em; }
pre, code { font-family: monospace; font-size: 0.9em; }
.post-title { font-size: 1.8em; font-weight: bold; margin-bottom: 0.2em; }
.post-subtitle { font-size: 1.1em; color: #444; margin-bottom: 0.5em; }
.post-date { font-size: 0.85em; color: #666; margin-bottom: 1.5em; }
hr { border: none; border-top: 1px solid #ccc; margin: 1.5em 0; }
""",
    )
    book.add_item(css)

    image_map = {}  # type: dict
    chapters = []
    spine = ["nav"]
    free_count = 0
    skipped_paid = 0
    downloaded = 0
    authenticated = bool(session.cookies.get("substack.sid"))

    print(f"\nProcessing {len(posts_data)} posts:")
    for i, post in enumerate(posts_data, 1):
        slug = post.get("slug", "")
        post_title = post.get("title", f"Post {i}")

        # Determine paywall status from the post stub before fetching content.
        # Substack sets audience to "everyone" for free posts and "paid" /
        # "founding_member" for subscriber-only posts.
        audience = post.get("audience", "everyone")
        is_paid = audience != "everyone"

        if is_paid and not authenticated:
            skipped_paid += 1
            print(f"  [{i}/{len(posts_data)}] {post_title[:60]} (paid, skipping)")
            continue

        print(f"  [{i}/{len(posts_data)}] {post_title[:60]}", end="", flush=True)

        try:
            base_url_for_post = post.get("_base_url", "")

            # Fetch full post content via API
            full = fetch_post_content(base_url_for_post, slug, session)
            body_html = full.get("body_html", "") or post.get("body_html", "")

            # If API returned no body, try scraping the post page directly
            if not body_html:
                canonical_url = full.get("canonical_url") or post.get("canonical_url", "")
                if canonical_url:
                    print(" (trying HTML scrape)", end="", flush=True)
                    body_html = fetch_post_html_fallback(canonical_url, session)

            if not body_html:
                if full.get("truncated_body_text"):
                    # Authenticated but still truncated — treat as paid
                    skipped_paid += 1
                    print(" (paywalled)")
                    continue
                else:
                    print(" (no content, skipping)")
                    continue

            subtitle = full.get("subtitle", "") or post.get("subtitle", "")
            pub_date_str = full.get("post_date") or post.get("post_date", "")
            try:
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                date_display = pub_date.strftime("%B %-d, %Y")
            except Exception:
                date_display = pub_date_str[:10] if pub_date_str else ""

            # Prepend title block
            header = f"""<div class="post-title">{post_title}</div>"""
            if subtitle:
                header += f"""<div class="post-subtitle">{subtitle}</div>"""
            if date_display:
                header += f"""<div class="post-date">{date_display}</div>"""
            header += "<hr/>"
            body_html = header + body_html

            # Download and embed images
            body_html = download_and_embed_images(body_html, session, book, image_map)

            # Clean and wrap in XHTML
            chapter_html = clean_html_for_epub(body_html, post_title)

            # Keep chapters at the EPUB content root so relative paths to
            # images/ and style/ are simple (no ../ prefix needed).
            chapter_filename = f"{i:04d}-{slugify(post_title)}.xhtml"
            chapter = epub.EpubHtml(
                title=post_title,
                file_name=chapter_filename,
                lang="en",
            )
            chapter.content = chapter_html.encode("utf-8")
            chapter.add_item(css)
            book.add_item(chapter)
            chapters.append(chapter)
            spine.append(chapter)
            if not is_paid:
                free_count += 1
            downloaded += 1
            print(" ✓")
            if post_limit and downloaded >= post_limit:
                break

        except Exception as e:
            print(f" ERROR: {e}")

        time.sleep(REQUEST_DELAY)

    # Table of contents
    # Use the chapter's own uid (set to its file stem) to guarantee uniqueness
    book.toc = tuple(epub.Link(c.file_name, c.title, c.file_name.split("/")[-1].replace(".xhtml", "")) for c in chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    epub.write_epub(output_path, book)
    print(f"\nEPUB written to: {output_path}")
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"File size: {size_mb:.1f} MB")
    print(f"\nDownloaded {free_count} free post{'s' if free_count != 1 else ''}. "
          f"Skipped {skipped_paid} paid post{'s' if skipped_paid != 1 else ''}.")


def normalize_url(url: str) -> str:
    """Ensure the URL has no trailing slash and has https://."""
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    return url


def main():
    parser = argparse.ArgumentParser(
        description="Download a Substack publication and export it as an EPUB for Kindle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "url",
        help="Substack URL, e.g. https://example.substack.com or https://example.com",
    )
    parser.add_argument(
        "--session-id",
        metavar="SID",
        help=(
            "Your substack.sid cookie value. "
            "Use --login instead to authenticate interactively."
        ),
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Authenticate via Substack magic-link email flow. "
            "The session is cached in ~/.config/substack_epub/session.json "
            "so you only need to do this once."
        ),
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Clear the cached Substack session and exit.",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Output EPUB filename (default: <publication-name>.epub)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of posts to download (default: all)",
    )
    parser.add_argument(
        "--sort",
        choices=["new", "old", "popular"],
        default="new",
        help=(
            "Order of posts in the EPUB: "
            "'new' = newest first (default), "
            "'old' = oldest first, "
            "'popular' = most liked first (uses reaction_count from the API)"
        ),
    )

    args = parser.parse_args()

    # --logout: wipe cached credentials and exit
    if args.logout:
        clear_cached_session()
        print("Cached session cleared.")
        sys.exit(0)

    base_url = normalize_url(args.url)

    # Resolve the session ID: explicit flag > --login flow > cached > none
    sid = args.session_id
    if not sid and args.login:
        try:
            sid = login_interactive()
            save_cached_session(sid)
            print(f"Session saved to {SESSION_CACHE_PATH}\n")
        except Exception as e:
            print(f"Login failed: {e}")
            sys.exit(1)
    if not sid:
        sid = load_cached_session()
        if sid:
            print(f"Using cached session from {SESSION_CACHE_PATH}")

    session = make_session(sid)
    if not sid:
        print(
            "Note: Not authenticated. Paywalled posts will be skipped.\n"
            "      Run with --login to sign in, or pass --session-id SID.\n"
        )

    # Fetch publication metadata
    try:
        print(f"Connecting to {base_url} ...")
        pub_info = get_publication_info(base_url, session)
    except Exception as e:
        print(f"Error fetching publication info: {e}")
        print("Trying to continue with minimal metadata...")
        pub_info = {"name": base_url.split("//")[-1].split(".")[0].title()}

    pub_name = pub_info.get("name", "substack")
    print(f"Publication: {pub_name}")

    # Fetch post list
    posts = fetch_all_posts(base_url, session, args.limit)
    if not posts:
        print("No posts found. Exiting.")
        sys.exit(1)

    # Inject base_url into each post dict so fetch_post_content can use it
    for p in posts:
        p["_base_url"] = base_url

    # Sort order (archive API returns newest-first by default)
    if args.sort == "old":
        posts = list(reversed(posts))
    elif args.sort == "popular":
        posts.sort(key=lambda p: p.get("reaction_count", 0), reverse=True)
        print(f"Sorted by popularity (reaction count).")

    # Determine output filename
    if args.output:
        output_path = args.output
    else:
        safe_name = re.sub(r"[^\w\- ]", "", pub_name).strip().replace(" ", "_")
        output_path = f"{safe_name}.epub"

    build_epub(posts, pub_info, session, output_path, post_limit=args.limit)


if __name__ == "__main__":
    main()
