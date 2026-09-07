import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, urldefrag
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from tqdm import tqdm


# ============================================================
# CONFIG
# ============================================================

START_URL = "https://www.website.domain/"
DOMAIN = "www.website.domain"

OUTPUT_DIR = Path("federato_crawl")
HTML_DIR = OUTPUT_DIR / "html"
MARKDOWN_DIR = OUTPUT_DIR / "markdown"

REQUEST_DELAY = 0.25
TIMEOUT = 30

# Safety valve. Increase/remove if necessary.
MAX_PAGES = 5000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "Chrome/140 Safari/537.36"
)


# ============================================================
# SETUP
# ============================================================

HTML_DIR.mkdir(parents=True, exist_ok=True)
MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT
})


# ============================================================
# URL HELPERS
# ============================================================

SKIP_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".css",
    ".js",
    ".json",
    ".xml",
    ".mp4",
    ".mov",
    ".avi",
    ".mp3",
    ".wav",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
)


def normalize_url(url):
    """
    Normalize a URL so the same page is not crawled several times.
    """

    # Remove #fragment
    url, _ = urldefrag(url)

    parsed = urlparse(url)

    # Force https
    scheme = "https"

    hostname = parsed.netloc.lower()

    # Treat website.domain and www.website.domain as the same site
    if hostname == "website.domain":
        hostname = DOMAIN

    # Drop query parameters.
    #
    # This avoids duplicates like:
    # ?utm_source=...
    # ?ref=...
    # etc.
    query = ""

    path = parsed.path or "/"

    # Normalize duplicate slashes
    path = re.sub(r"/+", "/", path)

    # Remove trailing slash except homepage
    if path != "/":
        path = path.rstrip("/")

    return urlunparse((
        scheme,
        hostname,
        path,
        "",
        query,
        ""
    ))


def is_internal_url(url):
    parsed = urlparse(url)

    return parsed.netloc.lower() in (
        DOMAIN,
        "website.domain",
    )


def is_crawlable_url(url):
    """
    Decide whether this looks like an HTML page we want.
    """

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    if not is_internal_url(url):
        return False

    path = parsed.path.lower()

    if path.endswith(SKIP_EXTENSIONS):
        return False

    # Things that are clearly not pages
    excluded_prefixes = (
        "/cdn-cgi/",
    )

    if path.startswith(excluded_prefixes):
        return False

    return True


# ============================================================
# FILE NAMING
# ============================================================

def slug_from_url(url):
    """
    Turn:

        https://www.website.domain/articles/my-article

    into:

        articles__my-article
    """

    parsed = urlparse(url)

    path = parsed.path.strip("/")

    if not path:
        return "index"

    slug = path.replace("/", "__")

    slug = re.sub(
        r"[^a-zA-Z0-9._-]",
        "_",
        slug
    )

    # Avoid ridiculously long filenames
    return slug[:220]


# ============================================================
# ROBOTS.TXT
# ============================================================

robots = RobotFileParser()

try:
    robots.set_url(
        "https://www.website.domain/robots.txt"
    )
    robots.read()

    ROBOTS_AVAILABLE = True

except Exception:
    print("Could not read robots.txt.")
    print("Continuing without robots.txt filtering.")
    ROBOTS_AVAILABLE = False


def robots_allows(url):

    if not ROBOTS_AVAILABLE:
        return True

    return robots.can_fetch(
        USER_AGENT,
        url
    )


# ============================================================
# SITEMAP
# ============================================================

def discover_sitemaps():
    """
    Start with the conventional sitemap location.

    Additional sitemap URLs can be added here if needed.
    """

    return [
        "https://www.website.domain/sitemap.xml"
    ]


def read_sitemap(sitemap_url, already_seen=None):
    """
    Recursively read sitemap files.

    Supports both:

        <urlset>
        <sitemapindex>
    """

    if already_seen is None:
        already_seen = set()

    if sitemap_url in already_seen:
        return set()

    already_seen.add(sitemap_url)

    found_urls = set()

    try:
        response = session.get(
            sitemap_url,
            timeout=TIMEOUT
        )
        response.raise_for_status()

        root = ET.fromstring(response.content)

    except Exception as exc:
        print(
            f"Could not read sitemap "
            f"{sitemap_url}: {exc}"
        )
        return found_urls

    # Remove namespaces from tag names
    root_name = root.tag.split("}")[-1]

    # --------------------------------------------------------
    # Sitemap index
    # --------------------------------------------------------

    if root_name == "sitemapindex":

        for element in root.iter():

            if element.tag.split("}")[-1] == "loc":

                if element.text:

                    child_sitemap = element.text.strip()

                    found_urls.update(
                        read_sitemap(
                            child_sitemap,
                            already_seen
                        )
                    )

    # --------------------------------------------------------
    # Normal sitemap
    # --------------------------------------------------------

    elif root_name == "urlset":

        for element in root.iter():

            if element.tag.split("}")[-1] == "loc":

                if not element.text:
                    continue

                url = normalize_url(
                    element.text.strip()
                )

                if is_crawlable_url(url):
                    found_urls.add(url)

    return found_urls


# ============================================================
# HTML CLEANING
# ============================================================

def clean_for_markdown(soup):
    """
    Remove elements which normally create useless Markdown.
    """

    for tag in soup.find_all([
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "iframe",
        "template",
    ]):
        tag.decompose()

    return soup


def find_main_content(soup):
    """
    Try to isolate actual page content instead of navigation/footer.
    """

    # Best option
    main = soup.find("main")

    if main:
        return main

    # Sometimes sites use role=main
    main = soup.find(
        attrs={"role": "main"}
    )

    if main:
        return main

    # Fallback to body
    body = soup.find("body")

    if body:

        # Remove repeated global navigation
        for tag in body.find_all([
            "nav",
            "footer",
        ]):
            tag.decompose()

        return body

    return soup


def make_links_absolute(element, current_url):
    """
    Convert relative Markdown links into absolute website URLs.
    """

    for tag in element.find_all("a", href=True):

        href = tag["href"].strip()

        if href.startswith((
            "mailto:",
            "tel:",
            "javascript:"
        )):
            continue

        tag["href"] = urljoin(
            current_url,
            href
        )

    for tag in element.find_all("img", src=True):

        tag["src"] = urljoin(
            current_url,
            tag["src"]
        )


def clean_markdown(text):

    # Strip trailing whitespace
    lines = [
        line.rstrip()
        for line in text.splitlines()
    ]

    text = "\n".join(lines)

    # Collapse huge whitespace blocks
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# INITIAL DISCOVERY
# ============================================================

print()
print("Reading website sitemap...")
print()

urls = set()

for sitemap in discover_sitemaps():

    urls.update(
        read_sitemap(sitemap)
    )

# Always include homepage
urls.add(
    normalize_url(START_URL)
)

queue = list(sorted(urls))
queued = set(queue)
visited = set()

print(
    f"Found {len(queue)} pages in sitemap."
)

print()
print("Starting crawl...")
print()


# ============================================================
# STATS
# ============================================================

manifest = []

stats = {
    "saved": 0,
    "failed": 0,
    "skipped": 0,
    "discovered": len(queue),
}


# ============================================================
# PROGRESS BAR
# ============================================================

progress = tqdm(
    total=len(queue),
    desc="Crawling",
    unit="page",
    dynamic_ncols=True
)


# ============================================================
# CRAWL
# ============================================================

while queue and len(visited) < MAX_PAGES:

    url = queue.pop(0)

    if url in visited:
        continue

    visited.add(url)

    # --------------------------------------------------------
    # ROBOTS
    # --------------------------------------------------------

    if not robots_allows(url):

        stats["skipped"] += 1

        progress.set_postfix(
            saved=stats["saved"],
            failed=stats["failed"],
            queued=len(queue)
        )

        progress.update(1)
        continue

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    try:

        response = session.get(
            url,
            timeout=TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

    except requests.RequestException as exc:

        stats["failed"] += 1

        tqdm.write(
            f"FAILED: {url} -> {exc}"
        )

        progress.set_postfix(
            saved=stats["saved"],
            failed=stats["failed"],
            queued=len(queue)
        )

        progress.update(1)

        continue

    # --------------------------------------------------------
    # HTML ONLY
    # --------------------------------------------------------

    content_type = response.headers.get(
        "Content-Type",
        ""
    ).lower()

    if "text/html" not in content_type:

        stats["skipped"] += 1

        progress.update(1)
        continue

    # --------------------------------------------------------
    # PARSE
    # --------------------------------------------------------

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = ""

    if soup.title:

        title = soup.title.get_text(
            " ",
            strip=True
        )

    # --------------------------------------------------------
    # FIND MORE INTERNAL LINKS
    # --------------------------------------------------------

    newly_found = 0

    for link in soup.find_all(
        "a",
        href=True
    ):

        href = link["href"].strip()

        # Ignore things that cannot be pages
        if href.startswith((
            "#",
            "mailto:",
            "tel:",
            "javascript:",
        )):
            continue

        absolute_url = urljoin(
            url,
            href
        )

        absolute_url = normalize_url(
            absolute_url
        )

        if not is_crawlable_url(
            absolute_url
        ):
            continue

        if absolute_url in visited:
            continue

        if absolute_url in queued:
            continue

        queue.append(
            absolute_url
        )

        queued.add(
            absolute_url
        )

        newly_found += 1

    # --------------------------------------------------------
    # UPDATE PROGRESS TOTAL
    # --------------------------------------------------------

    if newly_found:

        stats["discovered"] += newly_found

        progress.total += newly_found
        progress.refresh()

    # --------------------------------------------------------
    # FILENAME
    # --------------------------------------------------------

    slug = slug_from_url(url)

    html_file = (
        HTML_DIR
        / f"{slug}.html"
    )

    markdown_file = (
        MARKDOWN_DIR
        / f"{slug}.md"
    )

    # --------------------------------------------------------
    # SAVE ORIGINAL HTML
    # --------------------------------------------------------

    html_file.write_text(
        response.text,
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # CLEAN HTML FOR MARKDOWN
    # --------------------------------------------------------

    soup = clean_for_markdown(
        soup
    )

    content = find_main_content(
        soup
    )

    make_links_absolute(
        content,
        url
    )

    # --------------------------------------------------------
    # HTML -> MARKDOWN
    # --------------------------------------------------------

    markdown = md(
        str(content),
        heading_style="ATX",
        bullets="-",
        strip=[
            "script",
            "style"
        ]
    )

    markdown = clean_markdown(
        markdown
    )

    # --------------------------------------------------------
    # YAML FRONT MATTER
    # --------------------------------------------------------

    safe_title = title.replace(
        '"',
        "'"
    )

    markdown = f"""---
title: "{safe_title}"
source: "{url}"
---

{markdown}
"""

    # --------------------------------------------------------
    # SAVE MARKDOWN
    # --------------------------------------------------------

    markdown_file.write_text(
        markdown,
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # MANIFEST
    # --------------------------------------------------------

    manifest.append({
        "url": url,
        "title": title,
        "html": str(
            html_file.relative_to(
                OUTPUT_DIR
            )
        ),
        "markdown": str(
            markdown_file.relative_to(
                OUTPUT_DIR
            )
        ),
    })

    stats["saved"] += 1

    # --------------------------------------------------------
    # PROGRESS DISPLAY
    # --------------------------------------------------------

    progress.set_postfix(
        saved=stats["saved"],
        failed=stats["failed"],
        queued=len(queue)
    )

    progress.update(1)

    # Be polite to the server
    time.sleep(
        REQUEST_DELAY
    )


progress.close()


# ============================================================
# SAVE MANIFEST
# ============================================================

manifest_file = (
    OUTPUT_DIR
    / "manifest.json"
)

manifest_file.write_text(
    json.dumps(
        manifest,
        indent=2,
        ensure_ascii=False
    ),
    encoding="utf-8"
)


# ============================================================
# SAVE SUMMARY
# ============================================================

summary = {
    "start_url": START_URL,
    "pages_discovered": stats["discovered"],
    "pages_visited": len(visited),
    "pages_saved": stats["saved"],
    "pages_skipped": stats["skipped"],
    "pages_failed": stats["failed"],
}

summary_file = (
    OUTPUT_DIR
    / "summary.json"
)

summary_file.write_text(
    json.dumps(
        summary,
        indent=2,
        ensure_ascii=False
    ),
    encoding="utf-8"
)


# ============================================================
# FINISHED
# ============================================================

print()
print("=" * 60)
print("CRAWL FINISHED")
print("=" * 60)

print(
    f"Discovered: {stats['discovered']}"
)

print(
    f"Visited:    {len(visited)}"
)

print(
    f"Saved:      {stats['saved']}"
)

print(
    f"Skipped:    {stats['skipped']}"
)

print(
    f"Failed:     {stats['failed']}"
)

print()

print(
    f"Output folder: "
    f"{OUTPUT_DIR.resolve()}"
)