#!/usr/bin/env python3
"""
دستیار وب - Bale Bot
A comprehensive web assistant bot for Bale messenger.
"""

import os
import re
import io
import json
import time
import zipfile
import logging
import tempfile
import requests
import threading
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bs4 import BeautifulSoup
from PIL import Image
import pytesseract

# ─── Configuration ────────────────────────────────────────────────────────────
TOKEN          = os.getenv("BALE_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_URL       = f"https://tapi.bale.ai/bot{TOKEN}"
MAX_FILE_SIZE  = 50 * 1024 * 1024   # 50 MB
MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10 MB
MAX_OCR_SIZE   =  5 * 1024 * 1024   #  5 MB

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d — %(message)s",
    handlers=[
        logging.StreamHandler(),                        # stdout
        logging.FileHandler("bale_bot.log", encoding="utf-8"),  # file
    ],
)
log = logging.getLogger(__name__)
# Silence noisy urllib3 debug noise, keep our logs clean
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ─── Per-user state ───────────────────────────────────────────────────────────
user_state: dict[int, dict] = {}
user_stats: dict[int, dict] = {}

# ─── Shared requests session with automatic retry ─────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.4,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST", "HEAD"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

WEB = _make_session()   # All external web requests go through this

# ══════════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ══════════════════════════════════════════════════════════════════════════════

def api(method: str, **kwargs) -> dict:
    """Call a Bale Bot API method."""
    try:
        r = requests.post(f"{BASE_URL}/{method}", json=kwargs, timeout=30)
        return r.json()
    except Exception as e:
        log.error("API error %s: %s", method, e)
        return {"ok": False}


def send_message(chat_id: int, text: str, reply_markup=None,
                 reply_to_message_id: int = None, parse_mode: str = None) -> dict:
    kw = dict(chat_id=chat_id, text=text[:4096])
    if reply_markup:
        kw["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        kw["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return api("sendMessage", **kw)


def send_document(chat_id: int, file_bytes: bytes, filename: str,
                  caption: str = "", reply_to_message_id: int = None) -> bool:
    if not file_bytes:
        log.error("send_document: empty bytes for %s", filename)
        return False
    files = {"document": (filename, io.BytesIO(file_bytes), "application/octet-stream")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    try:
        r = requests.post(f"{BASE_URL}/sendDocument", data=data, files=files, timeout=180)
        resp = r.json()
        if not resp.get("ok"):
            log.error("sendDocument failed: %s | file=%s size=%d",
                      resp, filename, len(file_bytes))
            return False
        return True
    except Exception as e:
        log.error("sendDocument exception: %s | file=%s", e, filename)
        return False


def send_video_bytes(chat_id: int, video_bytes: bytes, filename: str,
                     caption: str = "") -> bool:
    """Send video using sendVideo endpoint (better playback in Bale)."""
    files = {"video": (filename, io.BytesIO(video_bytes), "video/mp4")}
    data = {"chat_id": chat_id, "caption": caption[:1024], "supports_streaming": "true"}
    try:
        r = requests.post(f"{BASE_URL}/sendVideo", data=data, files=files, timeout=180)
        resp = r.json()
        if not resp.get("ok"):
            log.error("sendVideo failed: %s | file=%s size=%d",
                      resp, filename, len(video_bytes))
            return False
        return True
    except Exception as e:
        log.error("sendVideo exception: %s", e)
        return False



def send_photo_bytes(chat_id: int, img_bytes: bytes, caption: str = "") -> dict:
    if not img_bytes or len(img_bytes) < 100:
        log.error("send_photo_bytes: empty/tiny image")
        return {"ok": False}
    files = {"photo": ("image.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    try:
        r = requests.post(f"{BASE_URL}/sendPhoto", data=data, files=files, timeout=60)
        resp = r.json()
        if not resp.get("ok"):
            log.error("sendPhoto failed: %s", resp)
        return resp
    except Exception as e:
        log.error("sendPhoto exception: %s", e)
        return {"ok": False}


def send_audio_bytes(chat_id: int, audio_bytes: bytes, filename: str,
                     caption: str = "") -> dict:
    files = {"audio": (filename, io.BytesIO(audio_bytes), "audio/mpeg")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    try:
        r = requests.post(f"{BASE_URL}/sendAudio", data=data, files=files, timeout=120)
        return r.json()
    except Exception as e:
        log.error("sendAudio error: %s", e)
        return {"ok": False}


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    api("sendChatAction", chat_id=chat_id, action=action)


def get_file_url(file_id: str) -> Optional[str]:
    resp = api("getFile", file_id=file_id)
    if resp.get("ok"):
        path = resp["result"]["file_path"]
        return f"https://tapi.bale.ai/file/bot{TOKEN}/{path}"
    return None


def download_file(url: str, max_bytes: int = MAX_FILE_SIZE) -> Optional[bytes]:
    try:
        r = WEB.get(url, timeout=30, stream=True)
        chunks = []
        total = 0
        for chunk in r.iter_content(8192):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return None
        return b"".join(chunks)
    except Exception as e:
        log.error("download_file error: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Keyboards
# ══════════════════════════════════════════════════════════════════════════════

def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🔎 جستجو در وب", "callback_data": "mode_search"},
                {"text": "🌐 باز کردن سایت", "callback_data": "mode_open"},
            ],
            [
                {"text": "📄 نتایج HTML", "callback_data": "mode_html_search"},
                {"text": "🗜 ZIP آفلاین", "callback_data": "mode_zip"},
            ],
            [
                {"text": "📥 GitHub دانلود", "callback_data": "mode_github"},
                {"text": "🌐 ترجمه متن", "callback_data": "mode_translate"},
            ],
            [
                {"text": "🖼 OCR متن از عکس", "callback_data": "mode_ocr"},
                {"text": "📚 مقاله علمی", "callback_data": "mode_scholar"},
            ],
            [
                {"text": "📖 ویکی‌پدیا", "callback_data": "mode_wiki"},
                {"text": "📺 یوتیوب", "callback_data": "mode_youtube"},
            ],
            [
                {"text": "🎵 دانلود موسیقی MP3", "callback_data": "mode_music"},
                {"text": "📌 پینترست", "callback_data": "mode_pinterest"},
            ],
            [
                {"text": "🖼 تصاویر گوگل", "callback_data": "mode_gimages"},
                {"text": "📷 Pexels عکس رایگان", "callback_data": "mode_pexels"},
            ],
            [
                {"text": "💱 تبدیل ارز", "callback_data": "mode_currency"},
                {"text": "🌐 اطلاعات IP/دامنه", "callback_data": "mode_iplookup"},
            ],
            [
                {"text": "🔗 کوتاه‌سازی لینک", "callback_data": "mode_shorten"},
                {"text": "🔍 باز کردن لینک کوتاه", "callback_data": "mode_expand"},
            ],
            [
                {"text": "📋 اشتراک‌گذاری متن", "callback_data": "mode_paste"},
                {"text": "📱 ساخت QR کد", "callback_data": "mode_qr"},
            ],
            [
                {"text": "📊 اطلاعات کاربری", "callback_data": "stats"},
                {"text": "❓ راهنما", "callback_data": "help"},
            ],
        ]
    }


def cancel_keyboard():
    return {
        "inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "cancel"}]]
    }


def pagination_keyboard(prev_cb: str, next_cb: str, page: int, has_next: bool) -> dict:
    """Generic prev/next keyboard."""
    row = []
    if page > 0:
        row.append({"text": "◀️ صفحه قبل", "callback_data": prev_cb})
    if has_next:
        row.append({"text": "صفحه بعد ▶️", "callback_data": next_cb})
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "🏠 منوی اصلی", "callback_data": "cancel"}])
    return {"inline_keyboard": buttons}


def translate_keyboard():
    langs = [
        ("🇮🇷 فارسی", "fa"), ("🇬🇧 انگلیسی", "en"),
        ("🇸🇦 عربی", "ar"),   ("🇩🇪 آلمانی", "de"),
        ("🇫🇷 فرانسوی", "fr"),("🇷🇺 روسی", "ru"),
    ]
    rows = []
    row = []
    for label, code in langs:
        row.append({"text": label, "callback_data": f"trlang_{code}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text": "❌ انصراف", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def youtube_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📥 دانلود ویدیو", "callback_data": "yt_video"},
                {"text": "🔍 جستجوی یوتیوب", "callback_data": "yt_search"},
            ],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ]
    }


# ══════════════════════════════════════════════════════════════════════════════
# Feature implementations
# ══════════════════════════════════════════════════════════════════════════════

def web_search(query: str, max_results: int = 10, page: int = 0) -> list[dict]:
    """DuckDuckGo HTML scrape with page support."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "fa,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # DDG paginates with &s= offset (multiples of 30) via POST
    data = {"q": query, "b": "", "kl": "ir-fa"}
    if page > 0:
        data["s"] = str(page * 30)
        data["dc"] = str(page * 30 + 1)
        data["v"] = "l"
        data["o"] = "json"
        data["nextParams"] = ""
    try:
        r = WEB.post(
            "https://html.duckduckgo.com/html/",
            data=data, headers=headers, timeout=20,
        )
        log.debug("DDG response: status=%d  content_len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".result, .web-result")[:max_results + 5]:
            title_tag = div.select_one(".result__title a, .result__a, h2 a")
            if not title_tag:
                continue
            href = title_tag.get("href", "")
            m = re.search(r"uddg=([^&]+)", href)
            link = urllib.parse.unquote(m.group(1)) if m else href
            if not link.startswith("http"):
                continue
            snippet_tag = div.select_one(".result__snippet, .result__body")
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
            results.append({"title": title_tag.get_text(strip=True),
                             "link": link, "snippet": snippet})
            if len(results) >= max_results:
                break
        log.info("web_search: returning %d results", len(results))
        if not results:
            log.warning("web_search: 0 results — DDG HTML head: %s", r.text[:400])
        return results
    except Exception as e:
        log.error("web_search error: %s", e, exc_info=True)
        return []


def search_to_html(query: str, page: int = 0) -> bytes:
    results = web_search(query, 10, page)
    rows = ""
    if not results:
        rows = '<tr><td colspan="3" style="text-align:center;color:#999">نتیجه‌ای یافت نشد</td></tr>'
    for i, r in enumerate(results, page * 10 + 1):
        snippet = r.get("snippet", "")
        rows += (
            f'<tr><td style="width:30px;text-align:center">{i}</td>'
            f'<td><a href="{r["link"]}" target="_blank">{r["title"]}</a>'
            f'{"<br><small style=color:#666>" + snippet + "</small>" if snippet else ""}</td></tr>\n'
        )
    page_info = f"صفحه {page + 1}" if page > 0 else "صفحه ۱"
    html = f"""<!DOCTYPE html>
    log.info("web_search: query=%r page=%d", query, page)
<html dir="rtl" lang="fa">
<head><meta charset="utf-8"><title>نتایج: {query}</title>
<style>
  body{{font-family:Tahoma,Arial,sans-serif;padding:20px;background:#f9f9f9;direction:rtl}}
  h2{{color:#333;font-size:18px}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)}}
  th{{background:#4a90d9;color:#fff;padding:10px 14px;text-align:right}}
  td{{padding:10px 14px;border-bottom:1px solid #eee;vertical-align:top}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#f0f7ff}}
  a{{color:#1a0dab;text-decoration:none;font-weight:bold}}
  a:hover{{text-decoration:underline}}
  small{{font-size:12px;line-height:1.5;display:block;margin-top:4px}}
  .meta{{color:#888;font-size:12px;margin-top:2px}}
</style>
</head>
<body>
<h2>🔎 نتایج جستجو برای: <em>{query}</em> — {page_info}</h2>
<p style="color:#888;font-size:13px">تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<table><tr><th>#</th><th>عنوان و لینک</th></tr>
{rows}
</table>
</body></html>"""
    return html.encode("utf-8")


def fetch_page(url: str) -> Optional[bytes]:
    """Fetch raw HTML of a page with proper browser headers."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "fa,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        r = WEB.get(url, headers=headers, timeout=25,
                    allow_redirects=True, verify=True)
        log.debug("fetch_page: status=%d  content_len=%d  url=%s",
                  r.status_code, len(r.content), r.url)
        r.raise_for_status()
        return r.content
    except requests.exceptions.SSLError:
        log.warning("fetch_page: SSL error for %s — retrying without verify", url)
        try:
            r = WEB.get(url, headers=headers, timeout=25,
                        allow_redirects=True, verify=False)
            return r.content
        except Exception as e:
            log.error("fetch_page SSL fallback error: %s", e, exc_info=True)
            return None
    except Exception as e:
        log.error("fetch_page error: %s", e, exc_info=True)
        return None


def page_to_zip(url: str) -> Optional[bytes]:
    """Download a page and its assets into a ZIP."""
    log.info("fetch_page: url=%r", url)
    html_bytes = fetch_page(url)
    if not html_bytes:
        return None
    soup = BeautifulSoup(html_bytes, "html.parser")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html_bytes)
        headers = {"User-Agent": "Mozilla/5.0"}
        base = urllib.parse.urlparse(url)
        base_url = f"{base.scheme}://{base.netloc}"
        for tag in soup.find_all(["img", "link", "script"])[:30]:
            src = tag.get("src") or tag.get("href", "")
            if not src or src.startswith("data:"):
                continue
            asset_url = urllib.parse.urljoin(base_url, src)
            try:
                ar = WEB.get(asset_url, headers=headers, timeout=10)
                fname = Path(urllib.parse.urlparse(asset_url).path).name or "asset"
                zf.writestr(f"assets/{fname}", ar.content)
            except Exception:
                pass
    buf.seek(0)
    return buf.read()


def github_zip(repo_url: str) -> Optional[bytes]:
    """Download GitHub repo as ZIP."""
    # Normalize URL
    m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", repo_url)
    if not m:
        return None
    slug = m.group(1)
    for branch in ["main", "master"]:
        zip_url = f"https://github.com/{slug}/archive/refs/heads/{branch}.zip"
        try:
            r = WEB.get(zip_url, timeout=60, stream=True)
            log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
    return None


def translate_text(text: str, target: str, source: str = "auto") -> str:
    """Translate using MyMemory API, handling long texts and HTML entities."""
    log.info("github_zip: url=%r", repo_url)
    import html as html_mod
    has_persian = bool(re.search(r'[\u0600-\u06FF]', text))
    if source == "auto":
        source = "fa" if has_persian else "en"
    if source == target:
        return text  # nothing to do

    # MyMemory max 500 chars per request — split on sentences
    def _translate_chunk(chunk: str) -> str:
        pair = f"{source}|{target}"
        try:
            r = WEB.get(
                "https://api.mymemory.translated.net/get",
                params={"q": chunk, "langpair": pair},
                timeout=20,
            )
            data = r.json()
            translated = data["responseData"]["translatedText"]
            # Unescape HTML entities (&#10; → \n, &amp; → & etc.)
            return html_mod.unescape(translated)
        except Exception as e:
            log.error("translate chunk error: %s", e)
            return chunk  # return original on failure

    # Split into ≤500-char chunks on newlines/sentences
    MAX_CHUNK = 490
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 <= MAX_CHUNK:
            current = (current + "\n" + line).lstrip("\n")
        else:
            if current:
                chunks.append(current)
            # If a single line is too long, split on ". "
            while len(line) > MAX_CHUNK:
                chunks.append(line[:MAX_CHUNK])
                line = line[MAX_CHUNK:]
            current = line
    if current:
        chunks.append(current)

    translated_parts = []
    for chunk in chunks:
        translated_parts.append(_translate_chunk(chunk))
        time.sleep(0.3)  # be polite to free API

    return "\n".join(translated_parts)


def ocr_image(img_bytes: bytes) -> str:
    """Extract text from image using Tesseract OCR."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        # Try Persian + English
        text = pytesseract.image_to_string(img, lang="fas+eng")
        return text.strip() or "(متنی یافت نشد)"
    except Exception as e:
        log.error("OCR error: %s", e)
        return "❌ خطا در پردازش تصویر."


def ocr_to_pdf(text: str) -> bytes:
    """Wrap OCR text in a PDF using updated fpdf2 API."""
    log.info("ocr_image: img_bytes=%d", len(img_bytes))
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in text.split("\n"):
        # Sanitize to latin-1 for built-in font (OCR text may have odd chars)
        safe_line = line.encode("latin-1", errors="replace").decode("latin-1")
        pdf.cell(
            0, 8,
            text=safe_line,
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
    # fpdf2 output() returns bytearray
    result = pdf.output()
    return bytes(result)


def scholar_search(query: str, page: int = 0) -> list[dict]:
    """Search Google Scholar via scraping with page support."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    start = page * 10
    url = (
        f"https://scholar.google.com/scholar"
        f"?q={urllib.parse.quote(query)}&hl=en&num=10&start={start}"
    )
    try:
        r = WEB.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".gs_ri"):
            title_tag = div.select_one(".gs_rt a")
            if not title_tag:
                # Try h3 > a
                title_tag = div.select_one("h3 a")
            if not title_tag:
                continue
            snippet_tag = div.select_one(".gs_rs")
            meta_tag = div.select_one(".gs_a")
            results.append({
                "title": title_tag.get_text(strip=True),
                "link": title_tag.get("href", ""),
                "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
                "meta": meta_tag.get_text(strip=True) if meta_tag else "",
            })
        return results
    except Exception as e:
        log.error("scholar_search error: %s", e)
        return []


def youtube_search(query: str, max_results: int = 8) -> list[dict]:
    """Search YouTube via yt-dlp."""
    log.info("scholar_search: query=%r page=%d", query, page)
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print",
             "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s",
             f"ytsearch{max_results}:{query}", "--no-warnings"],
            capture_output=True, text=True, timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2:
                vid_id = parts[0].strip()
                items.append({
                    "id": vid_id,
                    "title": parts[1].strip(),
                    "uploader": parts[2].strip() if len(parts) > 2 else "",
                    "duration": parts[3].strip() if len(parts) > 3 else "",
                    "url": f"https://youtu.be/{vid_id}",
                })
        return items
    except Exception as e:
        log.error("youtube_search error: %s", e)
        return []


def youtube_download(url: str, audio_only: bool = False) -> Optional[tuple[bytes, str]]:
    """Download YouTube video/audio. Returns (bytes, safe_filename) or None."""
    import shutil as _shutil
    ffmpeg_dir = str(Path(_shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)

    # Use a persistent temp dir (not context manager) so files survive
    tmp = tempfile.mkdtemp(prefix="baleyt_")
    try:
        out_tpl = os.path.join(tmp, "%(title).50s.%(ext)s")
        base = [
            "yt-dlp", "--no-playlist", "-o", out_tpl,
            "--no-warnings", "--no-check-certificate",
            "--ffmpeg-location", ffmpeg_dir,
            "--geo-bypass",
            "--socket-timeout", "30", "--retries", "3",
            "--fragment-retries", "3",
        ]
        if audio_only:
            cmd = base + [
                "-x", "--audio-format", "mp3", "--audio-quality", "5",
                "--prefer-ffmpeg",
                "-f", "bestaudio/best",
                url,
            ]
        else:
            cmd = base + [
                "-f",
                ("bestvideo[ext=mp4][height<=720][filesize<45M]"
                 "+bestaudio[ext=m4a]"
                 "/bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]"
                 "/best[ext=mp4][filesize<45M]"
                 "/best[filesize<45M]/best"),
                "--merge-output-format", "mp4",
                url,
            ]

        log.info("yt-dlp cmd: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        stdout_text = proc.stdout.decode(errors="replace")
        stderr_text = proc.stderr.decode(errors="replace")

        if proc.returncode != 0:
            log.error("yt-dlp failed (rc=%d): %s", proc.returncode, stderr_text[:800])
            # Geo-bypass retry
            if any(x in stderr_text for x in ["not available in your country", "geo", "not available"]):
                log.info("Retrying with tv_embedded…")
                cmd2 = base + ["--extractor-args", "youtube:player_client=tv_embedded"]
                if audio_only:
                    cmd2 += ["-x", "--audio-format", "mp3", "--audio-quality", "5",
                              "--prefer-ffmpeg", "-f", "bestaudio/best", url]
                else:
                    cmd2 += ["-f", "best[filesize<45M]/best",
                              "--merge-output-format", "mp4", url]
                proc2 = subprocess.run(cmd2, capture_output=True, timeout=300)
                if proc2.returncode != 0:
                    log.error("Geo retry failed: %s",
                              proc2.stderr.decode(errors="replace")[:400])
                    return None
            else:
                return None

        # Find downloaded file
        files = [f for f in Path(tmp).iterdir() if f.is_file()]
        log.info("yt-dlp output files: %s", files)
        if not files:
            log.error("yt-dlp: no output files in %s", tmp)
            return None

        # Pick largest file (avoids .part files)
        f = max(files, key=lambda x: x.stat().st_size)
        data = f.read_bytes()
        fname = f.name
        log.info("Downloaded: %s (%d MB)", fname, len(data) // 1024 // 1024)

        if not audio_only and len(data) > MAX_FILE_SIZE - 2 * 1024 * 1024:
            log.info("Trimming oversized video…")
            trimmed = _trim_video_ffmpeg(f, tmp, ffmpeg_dir)
            if trimmed:
                data, fp = trimmed
                fname = Path(fp).name

        # Sanitize filename for Bale API
        fname = re.sub(r'[^\w\s\-\.]', '', fname).strip() or "video.mp4"
        return data, fname

    except subprocess.TimeoutExpired:
        log.error("yt-dlp timed out")
        return None
    except Exception as e:
        log.error("youtube_download exception: %s", e)
        return None
    finally:
        # Clean up temp dir
        try:
            import shutil as _s
            _s.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def _trim_video_ffmpeg(src: Path, tmp: str, ffmpeg_dir: str = "/usr/bin") -> Optional[tuple[bytes, Path]]:
    """Re-encode video to fit ~48 MB."""
    log.info("youtube_download: url=%r audio=%s", url, audio_only)
    out_path = Path(tmp) / ("trimmed_" + src.name)
    target_bytes = 48 * 1024 * 1024
    ffprobe = str(Path(ffmpeg_dir) / "ffprobe")
    ffmpeg  = str(Path(ffmpeg_dir) / "ffmpeg")
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(probe.stdout.strip() or "300")
        video_bitrate = max(300, int((target_bytes * 8) / duration / 1000) - 128)
        subprocess.run(
            [ffmpeg, "-y", "-i", str(src),
             "-c:v", "libx264", "-b:v", f"{video_bitrate}k",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(out_path)],
            capture_output=True, timeout=300, check=True,
        )
        data = out_path.read_bytes()
        return data, out_path
    except Exception as e:
        log.error("ffmpeg trim error: %s", e)
        return None


def pinterest_search(query: str) -> list[dict]:
    """Search Pinterest images via DDG image search (site:pinterest.com) + direct scrape."""
    results: list[dict] = []
    UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.6261.119 Mobile Safari/537.36")

    # Strategy 1: DDG image search scoped to pinterest.com
    try:
        # Get VQD token
        r0 = WEB.get(
            "https://duckduckgo.com/",
            params={"q": f"site:pinterest.com {query}", "ia": "images", "iax": "images"},
            headers={"User-Agent": UA},
            timeout=15,
        )
        vqd_m = re.search(r'vqd=(["\'])([^"\']+)\1', r0.text) or \
                re.search(r'vqd=([\d\-]+)', r0.text)
        if vqd_m:
            vqd = vqd_m.group(2) if vqd_m.lastindex == 2 else vqd_m.group(1)
            ir = WEB.get(
                "https://duckduckgo.com/i.js",
                params={"q": f"site:pinterest.com {query}", "o": "json",
                        "vqd": vqd, "f": ",,,,,", "p": "1", "l": "us-en"},
                headers={"User-Agent": UA, "Referer": "https://duckduckgo.com/"},
                timeout=15,
            )
            if ir.status_code == 200:
                for item in ir.json().get("results", []):
                    img = item.get("image") or item.get("thumbnail")
                    if img and "pinimg.com" in img:
                        results.append({"url": img, "title": item.get("title", query)})
                    elif img:
                        results.append({"url": img, "title": item.get("title", query)})
                    if len(results) >= 10:
                        break
                if results:
                    log.info("Pinterest via DDG: %d", len(results))
                    return results
    except Exception as e:
        log.error("Pinterest DDG: %s", e)

    # Strategy 2: Direct Pinterest HTML scrape with realistic headers
    try:
        pin_headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        # First hit homepage to get cookies
        WEB.get("https://www.pinterest.com/", timeout=10, headers=pin_headers)
        r2 = WEB.get(
            f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
            timeout=20, headers=pin_headers,
        )
        text = r2.text
        # Extract all pinimg CDN URLs
        seen: set[str] = set()
        for pattern in [
            r'"orig":\{"url":"(https://i\.pinimg\.com/[^"]+)"',
            r'"736x":\{"url":"(https://i\.pinimg\.com/[^"]+)"',
            r'"(https://i\.pinimg\.com/originals/[^"]+\.(?:jpg|jpeg|png))"',
            r'"(https://i\.pinimg\.com/736x/[^"]+\.(?:jpg|jpeg|png))"',
            r'src="(https://i\.pinimg\.com/[^"]+)"',
        ]:
            for u in re.findall(pattern, text):
                clean = u.replace("\\u002F", "/").replace("\\/", "/")
                if clean not in seen and len(clean) > 20:
                    seen.add(clean)
                    results.append({"url": clean, "title": query})
            if len(results) >= 10:
                break
        if results:
            log.info("Pinterest HTML: %d", len(results))
            return results
    except Exception as e:
        log.error("Pinterest HTML: %s", e)

    # Strategy 3: Fall back to general DDG image search (not scoped to pinterest)
    try:
        general = google_images_search(f"pinterest {query}", 8)
        if general:
            log.info("Pinterest via general images: %d", len(general))
            return [{"url": g["img"], "title": g.get("title", query)} for g in general]
    except Exception as e:
        log.error("Pinterest general fallback: %s", e)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Google Images search
# ══════════════════════════════════════════════════════════════════════════════

def google_images_search(query: str, max_results: int = 8) -> list[dict]:
    """
    log.info("pinterest_search: query=%r", query)
    Search images — multiple strategies ordered by Iran-accessibility:
    1. Bing Images (accessible from Iran, no bot-detection on mobile UA)
    2. DuckDuckGo Images via vqd token
    3. Wikimedia Commons API
    4. Constructed Unsplash CDN URLs (fallback)
    """
    UA_MOB = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0.6261.119 Mobile Safari/537.36")
    UA_DESK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/122.0.0.0 Safari/537.36")
    results: list[dict] = []

    # ── Strategy 1: Bing Images ───────────────────────────────────────────
    try:
        r = WEB.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2", "first": "1", "tsc": "ImageHoverTitle"},
            headers={
                "User-Agent": UA_DESK,
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.bing.com/",
            },
            timeout=20,
        )
        # Bing embeds image URLs in murl: and imgurl: fields in data-m attrs
        for pattern in [
            r'"murl":"(https?://[^"]+\.(?:jpg|jpeg|png|webp|gif))"',
            r'murl&quot;:&quot;(https?://[^&]+\.(?:jpg|jpeg|png|webp))&quot;',
        ]:
            for url_val in re.findall(pattern, r.text):
                url_val = url_val.replace("&amp;", "&")
                if url_val not in {x["img"] for x in results}:
                    results.append({"img": url_val, "title": query})
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        if results:
            log.info("Bing images: %d", len(results))
            return results
    except Exception as e:
        log.error("Bing images: %s", e)

    # ── Strategy 2: DuckDuckGo Images ────────────────────────────────────
    try:
        r0 = WEB.get(
            "https://duckduckgo.com/",
            params={"q": query, "iax": "images", "ia": "images"},
            headers={"User-Agent": UA_MOB},
            timeout=15,
        )
        vqd_m = re.search(r'vqd=(["\']?)([^"\'&\s]+)\1', r0.text)
        if vqd_m:
            vqd = vqd_m.group(2)
            img_r = WEB.get(
                "https://duckduckgo.com/i.js",
                params={"l": "us-en", "o": "json", "q": query, "vqd": vqd,
                        "f": ",,,,,", "p": "-1"},
                headers={"User-Agent": UA_MOB, "Referer": "https://duckduckgo.com/"},
                timeout=15,
            )
            if img_r.status_code == 200:
                for item in img_r.json().get("results", [])[:max_results]:
                    img_url = item.get("image") or item.get("thumbnail")
                    if img_url:
                        results.append({"img": img_url, "title": item.get("title", query)})
                if results:
                    log.info("DDG images: %d", len(results))
                    return results
    except Exception as e:
        log.error("DDG images: %s", e)

    # ── Strategy 3: Wikimedia Commons ────────────────────────────────────
    try:
        r2 = WEB.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "srnamespace": "6", "srlimit": max_results, "format": "json"},
            headers={"User-Agent": "BaleBot/1.0"},
            timeout=15,
        )
        for p in r2.json().get("query", {}).get("search", []):
            ir = WEB.get(
                "https://commons.wikimedia.org/w/api.php",
                params={"action": "query", "titles": p["title"],
                        "prop": "imageinfo", "iiprop": "url", "format": "json"},
                headers={"User-Agent": "BaleBot/1.0"}, timeout=10,
            )
            for pg in ir.json().get("query", {}).get("pages", {}).values():
                url = (pg.get("imageinfo") or [{}])[0].get("url", "")
                if url and any(url.lower().endswith(e)
                               for e in (".jpg", ".jpeg", ".png", ".webp")):
                    results.append({"img": url, "title": p.get("title", query)})
                    break
            if len(results) >= max_results:
                break
        if results:
            log.info("Wikimedia images: %d", len(results))
            return results
    except Exception as e:
        log.error("Wikimedia images: %s", e)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Pexels (free stock photos)
# ══════════════════════════════════════════════════════════════════════════════

def pexels_search(query: str, per_page: int = 6) -> list[dict]:
    """
    Free stock photos — strategies:
    1. Pixabay free API (no key needed for read-only searches)
    2. Unsplash Source redirect CDN (always works)
    3. Wikimedia Commons images (open)
    """
    log.info("pexels_search: query=%r", query)
    results: list[dict] = []

    # ── Strategy 1: Pixabay ───────────────────────────────────────────────
    PIXABAY_KEY = "47075717-fbc72d1e73d12c83cfdb8b44e"  # public demo key
    try:
        r = WEB.get(
            "https://pixabay.com/api/",
            params={"key": PIXABAY_KEY, "q": query, "image_type": "photo",
                    "per_page": per_page, "safesearch": "true", "lang": "en"},
            headers={"User-Agent": "BaleBot/1.0"},
            timeout=15,
        )
        log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
        if r.status_code == 200:
            for h in r.json().get("hits", []):
                url = h.get("webformatURL") or h.get("largeImageURL")
                if url:
                    results.append({"url": url, "title": h.get("tags", query)[:60]})
            if results:
                log.info("Pixabay: %d results", len(results))
                return results
        else:
            log.warning("Pixabay status: %d", r.status_code)
    except Exception as e:
        log.error("Pixabay: %s", e)

    # ── Strategy 2: Unsplash Source redirect CDN ──────────────────────────
    # Each request to source.unsplash.com redirects to a real Unsplash image
    try:
        slug = urllib.parse.quote(query.replace(" ", ","))
        for i in range(min(per_page, 5)):
            r2 = WEB.get(
                f"https://source.unsplash.com/featured/800x600?{slug}&sig={i}",
                allow_redirects=True, timeout=20,
                headers={"User-Agent": "BaleBot/1.0"},
            )
            log.debug("HTTP %s status=%d len=%d", "r2", r2.status_code, len(r2.content if hasattr(r2, "content") else b""))
            if r2.status_code == 200 and len(r2.content) > 5000:
                results.append({"url": r2.url, "title": f"{query} #{i+1}",
                                 "_bytes": r2.content})
        if results:
            log.info("Unsplash CDN: %d results", len(results))
            return results
    except Exception as e:
        log.error("Unsplash CDN: %s", e)

    # ── Strategy 3: Wikimedia via google_images_search ────────────────────
    wiki = google_images_search(query, per_page)
    for w in wiki:
        results.append({"url": w["img"], "title": w.get("title", query)})
    return results


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Wikipedia
# ══════════════════════════════════════════════════════════════════════════════

def wikipedia_search(query: str, lang: str = "fa") -> list[dict]:
    """
    Search Wikipedia with multiple mirrors:
    - Primary: HTTPS Wikipedia API
    - Mirror 1: wikipedia.org via different endpoint
    - Mirror 2: For Persian, use fa.m.wikipedia.org (mobile, lighter)
    """
    log.info("wikipedia_search: query=%r lang=%r", query, lang)
    results: list[dict] = []
    HDR = {"User-Agent": "BaleBot/1.0 (educational; bale.ai)"}

    def _build_result(title: str, snippet: str) -> dict:
        key = title.replace(" ", "_")
        return {
            "title": title,
            "snippet": snippet[:150],
            "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(key)}",
            "key": key,
        }

    # Try 1: REST v1 search/title endpoint
    for base in [
        f"https://{lang}.wikipedia.org/api/rest_v1/page/search/title",
        f"https://{lang}.m.wikipedia.org/api/rest_v1/page/search/title",
    ]:
        try:
            r = WEB.get(base, params={"q": query, "limit": 8},
                             headers=HDR, timeout=15)
            log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
            if r.status_code == 200:
                for p in r.json().get("pages", []):
                    title = p.get("title", "")
                    snippet = p.get("description") or p.get("excerpt", "")
                    if title:
                        results.append(_build_result(title, snippet))
                if results:
                    log.info("Wikipedia REST (%s): %d results", lang, len(results))
                    return results
        except Exception as e:
            log.error("Wikipedia REST %s: %s", base, e)

    # Try 2: action API (opensearch — simpler, works without session)
    for base in [
        f"https://{lang}.wikipedia.org/w/api.php",
        f"https://{lang}.m.wikipedia.org/w/api.php",
    ]:
        try:
            r2 = WEB.get(base, params={
                "action": "opensearch", "search": query,
                "limit": 8, "namespace": 0, "format": "json",
            }, headers=HDR, timeout=15)
            log.debug("HTTP %s status=%d len=%d", "r2", r2.status_code, len(r2.content if hasattr(r2, "content") else b""))
            if r2.status_code == 200:
                data = r2.json()
                # opensearch returns [query, [titles], [descriptions], [urls]]
                titles = data[1] if len(data) > 1 else []
                descs  = data[2] if len(data) > 2 else []
                urls   = data[3] if len(data) > 3 else []
                for i, title in enumerate(titles):
                    snippet = descs[i] if i < len(descs) else ""
                    url = urls[i] if i < len(urls) else ""
                    results.append({
                        "title": title,
                        "snippet": snippet[:150],
                        "url": url or f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                        "key": title.replace(" ", "_"),
                    })
                if results:
                    log.info("Wikipedia opensearch (%s): %d", lang, len(results))
                    return results
        except Exception as e:
            log.error("Wikipedia opensearch %s: %s", base, e)

    return results


def wikipedia_article(title: str, lang: str = "fa") -> Optional[str]:
    """
    Fetch Wikipedia article plain text.
    Tries: REST summary → REST sections → action API extracts.
    Uses mobile subdomain as mirror if main fails.
    """
    log.info("wikipedia_article: title=%r lang=%r", title, lang)
    HDR = {"User-Agent": "BaleBot/1.0 (educational; bale.ai)"}
    key = urllib.parse.quote(title.replace(" ", "_"))

    # Try REST summary (fastest, ~2KB)
    for base in [f"https://{lang}.wikipedia.org", f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(f"{base}/api/rest_v1/page/summary/{key}",
                             headers=HDR, timeout=15)
            log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
            if r.status_code == 200:
                extract = r.json().get("extract", "")
                if extract and len(extract) > 80:
                    # Append more content via action API
                    try:
                        r2 = WEB.get(
                            f"{base}/w/api.php",
                            params={"action": "query", "titles": title,
                                    "prop": "extracts", "explaintext": 1,
                                    "exsectionformat": "plain",
                                    "format": "json", "utf8": 1},
                            headers=HDR, timeout=20,
                        )
                        log.debug("HTTP %s status=%d len=%d", "r2", r2.status_code, len(r2.content if hasattr(r2, "content") else b""))
                        if r2.status_code == 200:
                            pages = r2.json().get("query", {}).get("pages", {})
                            for pg in pages.values():
                                full = pg.get("extract", "")
                                if full and len(full) > len(extract):
                                    return full
                    except Exception:
                        pass
                    return extract
        except Exception as e:
            log.error("Wikipedia article %s/%s: %s", base, title, e)

    # Last resort: action API extracts only
    for base in [f"https://{lang}.wikipedia.org", f"https://{lang}.m.wikipedia.org"]:
        try:
            r3 = WEB.get(
                f"{base}/w/api.php",
                params={"action": "query", "titles": title,
                        "prop": "extracts", "explaintext": 1,
                        "exsectionformat": "plain",
                        "format": "json", "utf8": 1},
                headers=HDR, timeout=20,
            )
            log.debug("HTTP %s status=%d len=%d", "r3", r3.status_code, len(r3.content if hasattr(r3, "content") else b""))
            if r3.status_code == 200:
                for pg in r3.json().get("query", {}).get("pages", {}).values():
                    txt = pg.get("extract", "")
                    if txt:
                        return txt
        except Exception as e:
            log.error("Wikipedia action %s/%s: %s", base, title, e)

    return None




# ══════════════════════════════════════════════════════════════════════════════
# NEW: Currency converter (using free exchange API)
# ══════════════════════════════════════════════════════════════════════════════

def currency_convert(amount: float, from_cur: str, to_cur: str) -> Optional[str]:
    """Convert currency using exchangerate.host (free)."""
    try:
        r = WEB.get(
            "https://api.exchangerate.host/convert",
            params={"from": from_cur.upper(), "to": to_cur.upper(), "amount": amount},
            timeout=15,
        )
        data = r.json()
        if data.get("success"):
            result = data["result"]
            return f"{amount:,.2f} {from_cur.upper()} = {result:,.2f} {to_cur.upper()}"
    except Exception as e:
        log.error("currency error: %s", e)
    # Fallback: try frankfurter
    try:
        r2 = WEB.get(
            f"https://api.frankfurter.app/latest?from={from_cur.upper()}&to={to_cur.upper()}",
            timeout=15,
        )
        data2 = r2.json()
        rate = data2.get("rates", {}).get(to_cur.upper())
        if rate:
            result = amount * rate
            return f"{amount:,.2f} {from_cur.upper()} = {result:,.4f} {to_cur.upper()}"
    except Exception as e:
        log.error("currency fallback error: %s", e)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# NEW: IP / website info lookup
# ══════════════════════════════════════════════════════════════════════════════

def ip_lookup(target: str) -> str:
    """Look up IP or domain info."""
    log.info("currency_convert: %s %s->%s", amount, from_cur, to_cur)
    try:
        # Resolve domain to IP if needed
        ip = target.strip()
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            import socket
            ip = socket.gethostbyname(ip)
        r = WEB.get(f"https://ipapi.co/{ip}/json/", timeout=15)
        d = r.json()
        if d.get("error"):
            return "❌ اطلاعاتی یافت نشد."
        lines = [
            f"🌐 *اطلاعات IP: {ip}*\n",
            f"🏳 کشور: {d.get('country_name', '—')} ({d.get('country_code', '')})",
            f"🏙 شهر: {d.get('city', '—')} / {d.get('region', '—')}",
            f"📡 اپراتور: {d.get('org', '—')}",
            f"🕐 منطقه زمانی: {d.get('timezone', '—')}",
            f"📍 مختصات: {d.get('latitude', '—')}, {d.get('longitude', '—')}",
        ]
        return "\n".join(lines)
    except Exception as e:
        log.error("ip_lookup error: %s", e)
        return "❌ خطا در جستجوی IP."


# ══════════════════════════════════════════════════════════════════════════════
# NEW: URL shortener / expander
# ══════════════════════════════════════════════════════════════════════════════

def shorten_url(long_url: str) -> str:
    """Shorten URL using TinyURL (no auth needed)."""
    try:
        r = WEB.get(
            f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(long_url)}",
            timeout=15,
        )
        log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
        return "❌ خطا در کوتاه‌سازی لینک."
    except Exception as e:
        log.error("shorten_url error: %s", e)
        return "❌ خطا در کوتاه‌سازی لینک."


def expand_url(short_url: str) -> str:
    """Follow redirects to find the final URL."""
    log.info("shorten_url: url=%r", long_url)
    try:
        r = WEB.head(short_url, allow_redirects=True, timeout=15)
        final = r.url
        hops = len(r.history)
        return f"🔗 لینک نهایی:\n{final}\n\n_(تعداد ریدایرکت: {hops})_"
    except Exception as e:
        log.error("expand_url error: %s", e)
        return "❌ خطا در بازکردن لینک."


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Pastebin-style text share via paste.rs (free, no auth)
# ══════════════════════════════════════════════════════════════════════════════

def paste_text(content: str) -> Optional[str]:
    """Upload text to paste.rs and return public URL."""
    try:
        r = WEB.post(
            "https://paste.rs/",
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
        log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
        if r.status_code in (200, 201) and r.text.startswith("http"):
            return r.text.strip()
        return None
    except Exception as e:
        log.error("paste_text error: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# NEW: QR Code generator
# ══════════════════════════════════════════════════════════════════════════════

def generate_qr(text: str) -> Optional[bytes]:
    """Generate a QR code image for the given text."""
    log.info("paste_text: len=%d", len(content))
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()
    except ImportError:
        # Fallback: use online API
        try:
            r = WEB.get(
                f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(text)}",
                timeout=15,
            )
            log.debug("HTTP %s status=%d len=%d", "r", r.status_code, len(r.content if hasattr(r, "content") else b""))
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        return None
    except Exception as e:
        log.error("generate_qr error: %s", e)
        return None




def init_user(chat_id: int):
    if chat_id not in user_stats:
        user_stats[chat_id] = {
            "requests": 0,
            "joined": datetime.now().strftime("%Y-%m-%d"),
            "searches": 0,
            "downloads": 0,
            "translations": 0,
            "ocr": 0,
        }


def bump(chat_id: int, key: str = "requests"):
    init_user(chat_id)
    user_stats[chat_id]["requests"] = user_stats[chat_id].get("requests", 0) + 1
    user_stats[chat_id][key] = user_stats[chat_id].get(key, 0) + 1


def stats_text(chat_id: int) -> str:
    init_user(chat_id)
    s = user_stats[chat_id]
    return (
        f"📊 *اطلاعات کاربری شما*\n\n"
        f"🗓 تاریخ عضویت: {s['joined']}\n"
        f"📈 مجموع درخواست‌ها: {s['requests']}\n"
        f"🔎 جستجوها: {s.get('searches', 0)}\n"
        f"📥 دانلودها: {s.get('downloads', 0)}\n"
        f"🌐 ترجمه‌ها: {s.get('translations', 0)}\n"
        f"🖼 OCR: {s.get('ocr', 0)}\n"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Help text
# ══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """❓ *راهنمای دستیار وب*

🔎 *جستجو در وب* — تا ۱۰ نتیجه از DuckDuckGo با صفحه‌بندی
📄 *نتایج HTML* — نتایج جستجو به فایل HTML
🌐 *باز کردن سایت* — دریافت متن و HTML هر صفحه
🗜 *ZIP آفلاین* — صفحه + منابع در قالب ZIP
📥 *GitHub* — دانلود کل مخزن به ZIP
🌐 *ترجمه* — ۶ زبان، پشتیبانی از متن طولانی
🖼 *OCR* — استخراج متن از عکس + PDF
📚 *مقاله علمی* — جستجو Google Scholar با صفحه‌بندی
📖 *ویکی‌پدیا* — جستجو و خواندن مقاله (فارسی/انگلیسی)
📺 *یوتیوب* — دانلود ویدیو یا جستجو
🎵 *موسیقی MP3* — دانلود MP3 از یوتیوب
📌 *پینترست* — جستجو و دانلود تصاویر
🖼 *تصاویر گوگل* — جستجو و دانلود عکس از گوگل
📷 *Pexels* — عکس‌های رایگان با کیفیت بالا
💱 *تبدیل ارز* — نرخ روز (مثال: 100 USD to IRR)
🌐 *IP/دامنه* — اطلاعات موقعیت و اپراتور
🔗 *کوتاه‌سازی لینک* — با TinyURL
🔍 *بازکردن لینک کوتاه* — یافتن URL اصلی
📋 *اشتراک متن* — آپلود متن و گرفتن لینک
📱 *QR کد* — ساخت QR از هر متن یا لینک"""


# ══════════════════════════════════════════════════════════════════════════════
# Update dispatcher
# ══════════════════════════════════════════════════════════════════════════════

def handle_update(update: dict):
    """Route incoming update to correct handler."""
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])


def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    photo = msg.get("photo")
    document = msg.get("document")
    init_user(chat_id)

    # ── Commands ──────────────────────────────────────────────────────────
    if text.startswith("/start"):
        user_state[chat_id] = {"mode": None}
        send_message(
            chat_id,
            "👋 سلام! به *دستیار وب* خوش آمدید.\n"
            "یکی از گزینه‌های زیر را انتخاب کنید یا مستقیم سوال بپرسید:",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    if text.startswith("/help"):
        send_message(chat_id, HELP_TEXT, parse_mode="Markdown")
        return

    if text.startswith("/stats"):
        send_message(chat_id, stats_text(chat_id), parse_mode="Markdown")
        return

    if text.startswith("/cancel"):
        user_state[chat_id] = {"mode": None}
        send_message(chat_id, "✅ عملیات لغو شد.", reply_markup=main_menu_keyboard())
        return

    if text.startswith("/ocr"):
        # User replied with /ocr — check reply
        reply = msg.get("reply_to_message")
        if reply and reply.get("photo"):
            process_ocr_photo(chat_id, reply["photo"], msg["message_id"])
        else:
            user_state[chat_id] = {"mode": "ocr"}
            send_message(chat_id, "🖼 عکس حاوی متن را ارسال کنید.", reply_markup=cancel_keyboard())
        return

    # ── State-based handling ──────────────────────────────────────────────
    mode = user_state.get(chat_id, {}).get("mode")

    if photo:
        if mode == "ocr" or not mode:
            process_ocr_photo(chat_id, photo, msg["message_id"])
            return

    if not text:
        return  # ignore non-text non-photo

    if mode == "search":
        do_search(chat_id, text)

    elif mode == "html_search":
        do_html_search(chat_id, text)

    elif mode == "open":
        do_open_url(chat_id, text)

    elif mode == "pdf":
        do_page_pdf(chat_id, text)

    elif mode == "zip":
        do_page_zip(chat_id, text)

    elif mode == "github":
        do_github(chat_id, text)

    elif mode == "translate":
        lang = user_state[chat_id].get("target_lang", "en")
        do_translate(chat_id, text, lang)

    elif mode == "scholar":
        do_scholar(chat_id, text)

    elif mode == "youtube_download":
        do_youtube_download(chat_id, text)

    elif mode == "youtube_search":
        do_youtube_search(chat_id, text)

    elif mode == "music":
        do_music(chat_id, text)

    elif mode == "pinterest":
        do_pinterest(chat_id, text)

    elif mode == "gimages":
        do_google_images(chat_id, text)

    elif mode == "pexels":
        do_pexels(chat_id, text)

    elif mode == "wiki":
        do_wiki_search(chat_id, text)

    elif mode == "wiki_article":
        lang = user_state[chat_id].get("wiki_lang", "fa")
        do_wiki_article(chat_id, text, lang)

    elif mode == "currency":
        do_currency(chat_id, text)

    elif mode == "iplookup":
        do_ip_lookup(chat_id, text)

    elif mode == "shorten":
        do_shorten(chat_id, text)

    elif mode == "expand":
        do_expand(chat_id, text)

    elif mode == "paste":
        do_paste(chat_id, text)

    elif mode == "qr":
        do_qr(chat_id, text)

    elif mode == "ocr":
        send_message(chat_id, "🖼 لطفاً یک عکس ارسال کنید.", reply_markup=cancel_keyboard())

    else:
        # No mode — treat as a quick web search
        if text.startswith("http"):
            do_open_url(chat_id, text)
        else:
            do_search(chat_id, text)


def handle_callback(cb: dict):
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb.get("data", "")

    # Acknowledge
    try:
        api("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass

    if data == "cancel":
        user_state[chat_id] = {"mode": None}
        send_message(chat_id, "✅ عملیات لغو شد.", reply_markup=main_menu_keyboard())
        return

    if data == "help":
        send_message(chat_id, HELP_TEXT, parse_mode="Markdown")
        return

    if data == "stats":
        send_message(chat_id, stats_text(chat_id), parse_mode="Markdown")
        return

    mode_map = {
        "mode_search":     ("search",          "🔎 کلمه یا عبارت جستجو را بنویسید:"),
        "mode_html_search":("html_search",     "📄 کلمه یا عبارت جستجو را بنویسید (خروجی HTML):"),
        "mode_open":       ("open",            "🌐 آدرس سایت را وارد کنید (مثال: https://example.com):"),
        "mode_pdf":        ("pdf",             "📑 آدرس سایت را وارد کنید تا HTML آن دریافت شود:"),
        "mode_zip":        ("zip",             "🗜 آدرس سایت را وارد کنید برای دریافت ZIP آفلاین:"),
        "mode_github":     ("github",          "📥 لینک مخزن GitHub را وارد کنید:"),
        "mode_scholar":    ("scholar",         "📚 عنوان یا کلمه‌کلیدی مقاله را بنویسید:"),
        "mode_music":      ("music",           "🎵 نام آهنگ یا آرتیست را بنویسید:"),
        "mode_pinterest":  ("pinterest",       "📌 کلمه کلیدی برای جستجو در پینترست:"),
        "mode_gimages":    ("gimages",         "🖼 کلمه کلیدی برای جستجوی تصاویر گوگل:"),
        "mode_pexels":     ("pexels",          "📷 کلمه کلیدی برای جستجو در Pexels:"),
        "mode_currency":   ("currency",        "💱 تبدیل ارز را وارد کنید:\nمثال: 100 USD to IRR\nیا: 50 EUR to USD"),
        "mode_iplookup":   ("iplookup",        "🌐 آدرس IP یا دامنه را وارد کنید:\nمثال: 8.8.8.8 یا google.com"),
        "mode_shorten":    ("shorten",         "🔗 لینک بلند را برای کوتاه‌سازی وارد کنید:"),
        "mode_expand":     ("expand",          "🔍 لینک کوتاه را برای بازکردن وارد کنید:"),
        "mode_paste":      ("paste",           "📋 متن مورد نظر برای اشتراک‌گذاری را ارسال کنید:"),
        "mode_qr":         ("qr",              "📱 متن یا لینک مورد نظر برای ساخت QR کد را وارد کنید:"),
        "mode_wiki":       ("wiki",            "📖 موضوع مورد نظر را برای جستجو در ویکی‌پدیا بنویسید:"),
    }

    if data in mode_map:
        mode, prompt = mode_map[data]
        user_state[chat_id] = {"mode": mode}
        send_message(chat_id, prompt, reply_markup=cancel_keyboard())
        return

    if data == "mode_translate":
        user_state[chat_id] = {"mode": "translate_lang"}
        send_message(chat_id, "🌐 زبان مقصد ترجمه را انتخاب کنید:", reply_markup=translate_keyboard())
        return

    if data.startswith("trlang_"):
        lang = data.split("_", 1)[1]
        user_state[chat_id] = {"mode": "translate", "target_lang": lang}
        lang_names = {"fa": "فارسی", "en": "انگلیسی", "ar": "عربی",
                      "de": "آلمانی", "fr": "فرانسوی", "ru": "روسی"}
        send_message(
            chat_id,
            f"✅ زبان مقصد: *{lang_names.get(lang, lang)}*\n\nمتن مورد نظر برای ترجمه را ارسال کنید:",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )
        return

    if data == "mode_ocr":
        user_state[chat_id] = {"mode": "ocr"}
        send_message(chat_id, "🖼 عکس حاوی متن را ارسال کنید:", reply_markup=cancel_keyboard())
        return

    if data == "mode_youtube":
        user_state[chat_id] = {"mode": "youtube_menu"}
        send_message(chat_id, "📺 چه کاری می‌خواهید انجام دهید؟", reply_markup=youtube_keyboard())
        return

    if data == "yt_video":
        user_state[chat_id] = {"mode": "youtube_download"}
        send_message(chat_id, "📥 لینک یوتیوب را وارد کنید:", reply_markup=cancel_keyboard())
        return

    if data == "yt_search":
        user_state[chat_id] = {"mode": "youtube_search"}
        send_message(chat_id, "🔍 کلمه جستجو برای یوتیوب:", reply_markup=cancel_keyboard())
        return

    # ── Pagination callbacks ──────────────────────────────────────────────
    # Format: search_next_2  /  search_prev_2  /  scholar_next_1  etc.
    pag_match = re.match(r"(search|hsearch|scholar)_(next|prev)_(\d+)$", data)
    if pag_match:
        kind, direction, cur_page_str = pag_match.groups()
        cur_page = int(cur_page_str)
        new_page = cur_page + 1 if direction == "next" else cur_page - 1
        new_page = max(0, new_page)
        query = user_state.get(chat_id, {}).get("last_query", "")
        if not query:
            send_message(chat_id, "❌ جستجوی قبلی پیدا نشد. دوباره جستجو کنید.",
                         reply_markup=main_menu_keyboard())
            return
        if kind == "search":
            do_search(chat_id, query, new_page)
        elif kind == "hsearch":
            do_html_search(chat_id, query, new_page)
        elif kind == "scholar":
            do_scholar(chat_id, query, new_page)
        return


# ══════════════════════════════════════════════════════════════════════════════
# Feature handlers
# ══════════════════════════════════════════════════════════════════════════════

def do_search(chat_id: int, query: str, page: int = 0):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    results = web_search(query, 10, page)
    # Save state for pagination
    user_state[chat_id] = {"mode": "search", "last_query": query, "page": page}
    if not results:
        send_message(chat_id,
                     "❌ نتیجه‌ای یافت نشد.\n"
                     "_ممکن است DuckDuckGo موقتاً پاسخ ندهد. دوباره تلاش کنید._",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
        return
    offset = page * 10
    lines = [f"🔎 *نتایج جستجو:* _{query}_  (صفحه {page + 1})\n"]
    for i, r in enumerate(results, offset + 1):
        snippet = r.get("snippet", "")
        lines.append(f"{i}. [{r['title']}]({r['link']})")
        if snippet:
            lines.append(f"   _{snippet[:100]}_")
    kb = pagination_keyboard(
        f"search_prev_{page}", f"search_next_{page}",
        page, len(results) == 10,
    )
    send_message(chat_id, "\n".join(lines)[:4000],
                 parse_mode="Markdown", reply_markup=kb)


def do_html_search(chat_id: int, query: str, page: int = 0):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "upload_document")
    html_bytes = search_to_html(query, page)
    safe_q = re.sub(r"[^\w\u0600-\u06FF]", "_", query)[:20]
    send_document(chat_id, html_bytes, f"search_{safe_q}_p{page+1}.html",
                  caption=f"📄 نتایج جستجو: {query} (صفحه {page+1})")
    user_state[chat_id] = {"mode": "html_search", "last_query": query, "page": page}
    kb = pagination_keyboard(
        f"hsearch_prev_{page}", f"hsearch_next_{page}",
        page, True,
    )
    send_message(chat_id, "✅ فایل HTML ارسال شد.", reply_markup=kb)


def do_open_url(chat_id: int, url: str):
    bump(chat_id, "downloads")
    if not url.startswith("http"):
        url = "https://" + url
    send_chat_action(chat_id, "typing")
    content = fetch_page(url)
    if not content:
        send_message(chat_id, "❌ خطا در دریافت صفحه.", reply_markup=main_menu_keyboard())
        return
    soup = BeautifulSoup(content, "html.parser")
    # Extract readable text
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l for l in text.split("\n") if l.strip()][:60]
    preview = "\n".join(lines)
    send_message(chat_id, f"🌐 *محتوای صفحه:*\n\n{preview[:3000]}", parse_mode="Markdown")
    # Also send HTML file
    send_document(chat_id, content, "page.html", caption=f"📄 HTML صفحه: {url[:80]}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ صفحه دریافت شد.", reply_markup=main_menu_keyboard())


def do_page_pdf(chat_id: int, url: str):
    bump(chat_id, "downloads")
    if not url.startswith("http"):
        url = "https://" + url
    send_chat_action(chat_id, "upload_document")
    content = fetch_page(url)
    if not content:
        send_message(chat_id, "❌ خطا در دریافت صفحه.", reply_markup=main_menu_keyboard())
        return
    send_document(chat_id, content, "page.html", caption=f"📑 HTML صفحه: {url[:80]}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ فایل ارسال شد.", reply_markup=main_menu_keyboard())


def do_page_zip(chat_id: int, url: str):
    bump(chat_id, "downloads")
    if not url.startswith("http"):
        url = "https://" + url
    send_chat_action(chat_id, "upload_document")
    send_message(chat_id, "⏳ در حال دریافت صفحه و منابع آن...")
    zip_bytes = page_to_zip(url)
    if not zip_bytes:
        send_message(chat_id, "❌ خطا در ساخت فایل ZIP.", reply_markup=main_menu_keyboard())
        return
    domain = urllib.parse.urlparse(url).netloc.replace(".", "_")
    send_document(chat_id, zip_bytes, f"{domain}_offline.zip",
                  caption=f"🗜 آرشیو آفلاین: {url[:60]}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ ZIP ارسال شد.", reply_markup=main_menu_keyboard())


def do_github(chat_id: int, url: str):
    bump(chat_id, "downloads")
    send_chat_action(chat_id, "upload_document")
    send_message(chat_id, "⏳ در حال دانلود مخزن GitHub...")
    zip_bytes = github_zip(url)
    if not zip_bytes:
        send_message(chat_id, "❌ مخزن یافت نشد یا حجم آن زیاد است.", reply_markup=main_menu_keyboard())
        return
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", url)
    slug = m.group(1).replace("/", "_") if m else "repo"
    send_document(chat_id, zip_bytes, f"{slug}.zip", caption=f"📥 مخزن: {url[:80]}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ ZIP مخزن ارسال شد.", reply_markup=main_menu_keyboard())


def do_translate(chat_id: int, text: str, target: str):
    bump(chat_id, "translations")
    send_chat_action(chat_id, "typing")
    result = translate_text(text, target)
    send_message(chat_id, f"🌐 *ترجمه:*\n\n{result}", parse_mode="Markdown",
                 reply_markup=main_menu_keyboard())


def process_ocr_photo(chat_id: int, photos: list, reply_id: int = None):
    bump(chat_id, "ocr")
    send_chat_action(chat_id, "typing")
    # Get largest photo
    photo = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
    if photo.get("file_size", 0) > MAX_OCR_SIZE:
        send_message(chat_id, "❌ حجم عکس بیشتر از ۵ مگابایت است.")
        return
    file_url = get_file_url(photo["file_id"])
    if not file_url:
        send_message(chat_id, "❌ خطا در دریافت عکس.")
        return
    img_bytes = download_file(file_url, MAX_OCR_SIZE)
    if not img_bytes:
        send_message(chat_id, "❌ خطا در دانلود عکس.")
        return
    send_message(chat_id, "⏳ در حال پردازش تصویر...")
    extracted = ocr_image(img_bytes)
    send_message(chat_id, f"📝 *متن استخراج شده:*\n\n{extracted[:3500]}",
                 parse_mode="Markdown", reply_to_message_id=reply_id)
    # Send as PDF
    try:
        pdf_bytes = ocr_to_pdf(extracted)
        send_document(chat_id, pdf_bytes, "ocr_result.pdf", caption="📑 متن OCR به‌صورت PDF")
    except Exception as e:
        log.error("OCR PDF error: %s", e)
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ OCR انجام شد.", reply_markup=main_menu_keyboard())


def do_scholar(chat_id: int, query: str, page: int = 0):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    results = scholar_search(query, page)
    user_state[chat_id] = {"mode": "scholar", "last_query": query, "page": page}
    if not results:
        send_message(chat_id,
                     "❌ مقاله‌ای یافت نشد.\n"
                     "_ممکن است Google Scholar موقتاً دسترسی را محدود کرده باشد._",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
        return
    offset = page * 10
    lines = [f"📚 *نتایج Google Scholar:* _{query}_  (صفحه {page + 1})\n"]
    for i, r in enumerate(results, offset + 1):
        lines.append(f"{i}. [{r['title']}]({r['link']})")
        if r.get("meta"):
            lines.append(f"   _{r['meta'][:80]}_")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:120]}…")
        lines.append("")
    kb = pagination_keyboard(
        f"scholar_prev_{page}", f"scholar_next_{page}",
        page, len(results) >= 8,
    )
    send_message(chat_id, "\n".join(lines)[:4000],
                 parse_mode="Markdown", reply_markup=kb)


def do_youtube_search(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    results = youtube_search(query)
    if not results:
        send_message(chat_id, "❌ ویدیویی یافت نشد.", reply_markup=main_menu_keyboard())
        return
    lines = [f"📺 *نتایج یوتیوب:* {query}\n"]
    for i, r in enumerate(results, 1):
        dur = f" ({r['duration']})" if r.get("duration") else ""
        lines.append(f"{i}. [{r['title']}]({r['url']}){dur}")
        if r.get("uploader"):
            lines.append(f"   🎬 {r['uploader']}")
    send_message(chat_id, "\n".join(lines)[:4000], parse_mode="Markdown",
                 reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}


def do_youtube_download(chat_id: int, url: str):
    bump(chat_id, "downloads")
    url = url.strip()
    if "youtu" not in url and "yt.be" not in url:
        send_message(chat_id, "❌ لینک معتبر یوتیوب وارد کنید.\nمثال: https://youtu.be/xxxx",
                     reply_markup=main_menu_keyboard())
        return
    send_message(chat_id, "⏳ در حال دانلود ویدیو… (ممکن است چند دقیقه طول بکشد)")
    send_chat_action(chat_id, "upload_video")
    result = youtube_download(url, audio_only=False)
    if not result:
        send_message(chat_id,
                     "❌ خطا در دانلود ویدیو.\n"
                     "• لینک ممکن است محدود یا خصوصی باشد\n"
                     "• اجرا کنید: `yt-dlp -U` برای آپدیت",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
        return
    data, fname = result
    fname = Path(fname).name
    size_mb = len(data) // 1024 // 1024
    log.info("YT download: %s  size=%d MB", fname, size_mb)

    if len(data) > MAX_FILE_SIZE:
        send_message(chat_id,
                     f"❌ حجم ویدیو ({size_mb} MB) بیشتر از ۵۰ MB است.\n"
                     "فایل پس از trim هنوز بزرگ است.",
                     reply_markup=main_menu_keyboard())
        return

    # Try sendVideo first (inline playback), fallback to sendDocument
    sent = False
    if fname.lower().endswith(".mp4"):
        sent = send_video_bytes(chat_id, data, fname, caption=f"📺 {fname[:80]}")
    if not sent:
        sent = send_document(chat_id, data, fname, caption=f"📺 {fname[:80]}")

    user_state[chat_id] = {"mode": None}
    if sent:
        send_message(chat_id, f"✅ ویدیو ارسال شد ({size_mb} MB).",
                     reply_markup=main_menu_keyboard())
    else:
        send_message(chat_id,
                     "❌ ارسال ویدیو ناموفق بود.\n"
                     f"حجم: {size_mb} MB — سرور بله ممکن است آن را رد کرده باشد.",
                     reply_markup=main_menu_keyboard())


def do_music(chat_id: int, query: str):
    bump(chat_id, "downloads")
    send_message(chat_id, f"⏳ در حال جستجو و دانلود MP3 برای: _{query}_…",
                 parse_mode="Markdown")
    send_chat_action(chat_id, "record_voice")
    # Use a search URL directly so yt-dlp resolves the best match
    search_url = f"ytsearch1:{query}"
    result = youtube_download(search_url, audio_only=True)
    if not result:
        send_message(chat_id,
                     "❌ خطا در دانلود موسیقی.\n"
                     "• نام آهنگ را به انگلیسی امتحان کنید\n"
                     "• یا مستقیم لینک یوتیوب بدهید",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
        return
    data, fname = result
    fname = Path(fname).name
    if len(data) > MAX_FILE_SIZE:
        send_message(chat_id, "❌ حجم فایل صوتی زیاد است.", reply_markup=main_menu_keyboard())
        return
    if not fname.lower().endswith(".mp3"):
        fname = re.sub(r"\.[^.]+$", ".mp3", fname)
    send_audio_bytes(chat_id, data, fname, caption=f"🎵 {query}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ موسیقی ارسال شد.", reply_markup=main_menu_keyboard())


def do_pinterest(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "upload_photo")
    send_message(chat_id, f"⏳ در حال جستجو در پینترست: _{query}_…", parse_mode="Markdown")
    results = pinterest_search(query)
    if not results:
        send_message(chat_id, "❌ تصویری یافت نشد.", reply_markup=main_menu_keyboard())
        return
    sent = 0
    for r in results[:6]:
        try:
            img_bytes = download_file(r["url"], MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 1000:
                send_photo_bytes(chat_id, img_bytes, caption=f"📌 {r.get('title', query)[:80]}")
                sent += 1
                time.sleep(0.4)
        except Exception:
            pass
    msg = f"✅ {sent} تصویر از پینترست ارسال شد." if sent else "❌ تصویری دانلود نشد."
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, msg, reply_markup=main_menu_keyboard())


def do_google_images(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "upload_photo")
    send_message(chat_id, f"⏳ در حال جستجوی تصاویر گوگل: _{query}_…", parse_mode="Markdown")
    results = google_images_search(query, 8)
    if not results:
        send_message(chat_id, "❌ تصویری یافت نشد.", reply_markup=main_menu_keyboard())
        return
    sent = 0
    for r in results[:6]:
        try:
            img_bytes = download_file(r["img"], MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 1000:
                send_photo_bytes(chat_id, img_bytes, caption=f"🖼 {query}")
                sent += 1
                time.sleep(0.4)
        except Exception:
            pass
    msg = f"✅ {sent} تصویر از گوگل ارسال شد." if sent else "❌ تصویری دانلود نشد."
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, msg, reply_markup=main_menu_keyboard())


def do_pexels(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "upload_photo")
    send_message(chat_id, f"⏳ در حال جستجوی عکس رایگان: _{query}_…", parse_mode="Markdown")
    results = pexels_search(query, 8)
    if not results:
        send_message(chat_id, "❌ عکسی یافت نشد.", reply_markup=main_menu_keyboard())
        return
    sent = 0
    for r in results[:5]:
        try:
            img_bytes = r.get("_bytes") or download_file(r["url"], MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 1000:
                send_photo_bytes(chat_id, img_bytes, caption=f"📷 {r.get('title', query)[:60]}")
                sent += 1
                time.sleep(0.4)
        except Exception as e:
            log.error("do_pexels send: %s", e)
    msg = f"✅ {sent} عکس ارسال شد." if sent else "❌ عکسی دانلود نشد."
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, msg, reply_markup=main_menu_keyboard())


def do_wiki_search(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    # Try Persian first, then English
    results = wikipedia_search(query, "fa")
    lang_used = "fa"
    if not results:
        results = wikipedia_search(query, "en")
        lang_used = "en"
    if not results:
        send_message(chat_id, "❌ مقاله‌ای در ویکی‌پدیا یافت نشد.", reply_markup=main_menu_keyboard())
        return
    lines = [f"📖 *نتایج ویکی‌پدیا:* _{query}_\n"]
    for i, r in enumerate(results, 1):
        snippet = r.get("snippet", "")[:100]
        lines.append(f"{i}. [{r['title']}]({r['url']})")
        if snippet:
            lines.append(f"   _{snippet}…_")
    lines.append(f"\n💡 برای خواندن کامل مقاله، عنوان آن را بنویسید.")
    # Save state so next message reads the article
    user_state[chat_id] = {"mode": "wiki_article", "wiki_lang": lang_used,
                            "last_query": query}
    send_message(chat_id, "\n".join(lines)[:4000], parse_mode="Markdown",
                 reply_markup=cancel_keyboard())


def do_wiki_article(chat_id: int, title: str, lang: str = "fa"):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    send_message(chat_id, f"⏳ در حال دریافت مقاله: _{title}_…", parse_mode="Markdown")
    text = wikipedia_article(title, lang)
    if not lang == "fa" or not text:
        text2 = wikipedia_article(title, "en")
        text = text or text2
    if not text:
        send_message(chat_id, "❌ مقاله یافت نشد.", reply_markup=main_menu_keyboard())
        return
    # Send first 3500 chars as message, rest as text file
    preview = text[:3500]
    send_message(chat_id, f"📖 *{title}*\n\n{preview}", parse_mode="Markdown")
    if len(text) > 3500:
        send_document(chat_id, text.encode("utf-8"), f"{title[:40]}.txt",
                      caption="📄 متن کامل مقاله")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ مقاله دریافت شد.", reply_markup=main_menu_keyboard())


def do_currency(chat_id: int, text: str):
    bump(chat_id, "requests")
    # Parse: "100 USD to IRR"  or  "100 USD IRR"
    m = re.match(
        r"([\d,\.]+)\s+([A-Za-z]{3})\s+(?:to\s+)?([A-Za-z]{3})",
        text.strip(), re.IGNORECASE,
    )
    if not m:
        send_message(chat_id,
                     "❌ فرمت اشتباه.\n"
                     "مثال صحیح: `100 USD to IRR` یا `50 EUR USD`",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
        return
    amount_str, from_cur, to_cur = m.groups()
    amount = float(amount_str.replace(",", ""))
    send_chat_action(chat_id, "typing")
    result = currency_convert(amount, from_cur, to_cur)
    if result:
        send_message(chat_id, f"💱 *{result}*", parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
    else:
        send_message(chat_id, "❌ خطا در تبدیل ارز. کدهای ارزی را بررسی کنید.",
                     reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}


def do_ip_lookup(chat_id: int, target: str):
    bump(chat_id, "requests")
    send_chat_action(chat_id, "typing")
    result = ip_lookup(target.strip())
    send_message(chat_id, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}


def do_shorten(chat_id: int, url: str):
    bump(chat_id, "requests")
    if not url.startswith("http"):
        url = "https://" + url
    send_chat_action(chat_id, "typing")
    result = shorten_url(url)
    send_message(chat_id, f"🔗 لینک کوتاه شده:\n{result}", reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}


def do_expand(chat_id: int, url: str):
    bump(chat_id, "requests")
    if not url.startswith("http"):
        url = "https://" + url
    send_chat_action(chat_id, "typing")
    result = expand_url(url)
    send_message(chat_id, result, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}


def do_paste(chat_id: int, text: str):
    bump(chat_id, "requests")
    send_chat_action(chat_id, "typing")
    url = paste_text(text)
    if url:
        send_message(chat_id,
                     f"📋 متن آپلود شد:\n{url}\n\n_(لینک عمومی — هر کسی می‌تواند ببیند)_",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
    else:
        send_message(chat_id, "❌ خطا در آپلود متن.", reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}


def do_qr(chat_id: int, text: str):
    bump(chat_id, "requests")
    send_chat_action(chat_id, "upload_photo")
    qr_bytes = generate_qr(text)
    if qr_bytes:
        send_photo_bytes(chat_id, qr_bytes, caption=f"📱 QR کد برای:\n{text[:60]}")
    else:
        send_message(chat_id, "❌ خطا در ساخت QR کد.", reply_markup=main_menu_keyboard())
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅", reply_markup=main_menu_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
# Polling loop
# ══════════════════════════════════════════════════════════════════════════════

def run():
    log.info("Bot starting (long polling)…")
    offset = 0
    while True:
        try:
            resp = api("getUpdates", offset=offset, timeout=30)
            if not resp.get("ok"):
                time.sleep(5)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    log.error("handle_update error: %s", e)
        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error("Polling error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️  لطفاً BALE_TOKEN را تنظیم کنید:")
        print("   export BALE_TOKEN='your_token_here'")
        print("   python bale_bot.py")
    else:
        run()