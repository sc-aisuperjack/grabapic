import io
import re
import zipfile
import hashlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


# ----------------------------
# Helpers
# ----------------------------

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".avif"}
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class DownloadResult:
    url: str
    filename: str
    size_bytes: int
    content_type: str


def safe_filename(name: str, fallback: str = "image") -> str:
    name = name.strip()
    name = re.sub(r"[^\w\-. ]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    if not name:
        name = fallback
    # keep it reasonable
    return name[:180]


def guess_ext_from_content_type(ct: str) -> str:
    ct = (ct or "").lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
    }
    return mapping.get(ct, "")


def pick_best_from_srcset(srcset: str) -> str | None:
    """
    srcset looks like:
      "img-320.jpg 320w, img-640.jpg 640w"
    pick the largest w descriptor if possible.
    """
    if not srcset:
        return None

    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0].strip()
        w = 0
        if len(bits) > 1:
            m = re.match(r"(\d+)(w|x)$", bits[1].strip())
            if m:
                w = int(m.group(1))
        candidates.append((w, url))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def is_probably_image_url(u: str) -> bool:
    path = urlparse(u).path.lower()
    for ext in IMG_EXTS:
        if path.endswith(ext):
            return True
    # sometimes no extension but still image via content-type, allow it later
    return False


def extract_image_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: set[str] = set()

    for img in soup.find_all("img"):
        # Try srcset first (often has best quality)
        srcset = img.get("srcset")
        best = pick_best_from_srcset(srcset) if srcset else None

        # Then common lazy-load attributes
        src = (
            best
            or img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
        )

        if not src:
            continue

        abs_url = urljoin(base_url, src)
        urls.add(abs_url)

    # Keep stable order-ish
    return sorted(urls)


def unique_name(existing: set[str], proposed: str) -> str:
    if proposed not in existing:
        existing.add(proposed)
        return proposed

    stem, dot, ext = proposed.rpartition(".")
    if not dot:  # no ext
        stem, ext = proposed, ""
    for i in range(2, 10_000):
        candidate = f"{stem}_{i}{('.' + ext) if ext else ''}"
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    # last resort
    h = hashlib.sha256(proposed.encode("utf-8")).hexdigest()[:10]
    candidate = f"{stem}_{h}{('.' + ext) if ext else ''}"
    existing.add(candidate)
    return candidate


def build_zip_from_images(page_url: str, timeout_s: int = 25, max_images: int = 300) -> tuple[bytes, list[DownloadResult], list[str]]:
    """
    Returns (zip_bytes, downloaded_results, skipped_urls)
    """
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)

    # Fetch page
    resp = sess.get(page_url, timeout=timeout_s, allow_redirects=True)
    resp.raise_for_status()

    base_url = resp.url  # final URL after redirects
    image_urls = extract_image_urls(resp.text, base_url)

    if not image_urls:
        return b"", [], []

    image_urls = image_urls[:max_images]

    zip_buffer = io.BytesIO()
    downloaded: list[DownloadResult] = []
    skipped: list[str] = []

    used_names: set[str] = set()

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, img_url in enumerate(image_urls, start=1):
            try:
                r = sess.get(img_url, timeout=timeout_s, stream=True, allow_redirects=True)
                r.raise_for_status()

                ct = (r.headers.get("Content-Type") or "").lower()
                if not ct.startswith("image/"):
                    # Some sites return HTML for blocked assets
                    skipped.append(img_url)
                    continue

                raw = r.content
                if not raw:
                    skipped.append(img_url)
                    continue

                # Determine filename
                path_name = urlparse(img_url).path.split("/")[-1]
                path_name = safe_filename(path_name or f"image_{idx}")

                ext = ""
                # If path already has known ext, keep it. Else use content-type.
                lower_path = path_name.lower()
                if any(lower_path.endswith(e) for e in IMG_EXTS):
                    filename = path_name
                else:
                    ext = guess_ext_from_content_type(ct) or ".img"
                    filename = f"{path_name}{ext}"

                filename = unique_name(used_names, filename)

                zf.writestr(filename, raw)

                downloaded.append(
                    DownloadResult(
                        url=img_url,
                        filename=filename,
                        size_bytes=len(raw),
                        content_type=ct.split(";")[0],
                    )
                )
            except Exception:
                skipped.append(img_url)

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), downloaded, skipped


# ----------------------------
# Streamlit UI
# ----------------------------

st.set_page_config(page_title="Webpage Image Zipper", page_icon="🗜️", layout="centered")

st.title("🗜️ Download all images from a webpage")
st.write("Paste a webpage URL, hit **Fetch images**, then download a ZIP.")

url = st.text_input("Webpage URL", placeholder="https://example.com/some-page")

col1, col2 = st.columns([1, 1])
with col1:
    max_images = st.number_input("Max images", min_value=1, max_value=2000, value=300, step=50)
with col2:
    timeout_s = st.number_input("Timeout (seconds)", min_value=5, max_value=120, value=25, step=5)

go = st.button("Fetch images", type="primary", use_container_width=True)

if go:
    if not url or not url.startswith(("http://", "https://")):
        st.error("Please enter a valid URL starting with http:// or https://")
        st.stop()

    with st.spinner("Fetching page and downloading images..."):
        try:
            zip_bytes, downloaded, skipped = build_zip_from_images(
                url, timeout_s=int(timeout_s), max_images=int(max_images)
            )
        except requests.HTTPError as e:
            st.error(f"HTTP error: {e}")
            st.stop()
        except requests.RequestException as e:
            st.error(f"Network error: {e}")
            st.stop()
        except Exception as e:
            st.error(f"Unexpected error: {e}")
            st.stop()

    if not downloaded:
        st.warning("No downloadable images found (or the site blocked access).")
        if skipped:
            st.caption(f"Skipped/failed: {len(skipped)}")
        st.stop()

    parsed = urlparse(url)
    base = safe_filename(parsed.netloc or "images")
    zip_name = f"{base}_images.zip"

    total_bytes = sum(d.size_bytes for d in downloaded)
    st.success(f"Downloaded {len(downloaded)} images, zipped size depends on compression (raw total: {total_bytes:,} bytes).")

    st.download_button(
        label="⬇️ Download ZIP now",
        data=zip_bytes,
        file_name=zip_name,
        mime="application/zip",
        use_container_width=True,
    )

    with st.expander("See downloaded files"):
        for d in downloaded[:200]:
            st.write(f"- {d.filename}  ({d.content_type}, {d.size_bytes:,} bytes)")
        if len(downloaded) > 200:
            st.caption(f"Showing 200 of {len(downloaded)}")

    if skipped:
        with st.expander(f"Skipped/failed ({len(skipped)})"):
            for s in skipped[:200]:
                st.write(f"- {s}")
            if len(skipped) > 200:
                st.caption(f"Showing 200 of {len(skipped)}")

st.caption("Note: some websites block scraping or require cookies/auth. If it fails, try a public page or add headers/cookies.")
