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

from bs4 import BeautifulSoup
from PIL import Image
import pytesseract

# ─── Configuration ────────────────────────────────────────────────────────────
TOKEN = os.getenv("BALE_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_URL = f"https://tapi.bale.ai/bot{TOKEN}"
MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_OCR_SIZE   =  5 * 1024 * 1024  #  5 MB

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Per-user state ───────────────────────────────────────────────────────────
user_state: dict[int, dict] = {}   # chat_id → {mode, data, …}
user_stats: dict[int, dict] = {}   # chat_id → {requests, joined, …}

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
                  caption: str = "", reply_to_message_id: int = None) -> dict:
    files = {"document": (filename, io.BytesIO(file_bytes), "application/octet-stream")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    try:
        r = requests.post(f"{BASE_URL}/sendDocument", data=data, files=files, timeout=60)
        return r.json()
    except Exception as e:
        log.error("sendDocument error: %s", e)
        return {"ok": False}


def send_photo_bytes(chat_id: int, img_bytes: bytes, caption: str = "") -> dict:
    files = {"photo": ("image.jpg", io.BytesIO(img_bytes), "image/jpeg")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    try:
        r = requests.post(f"{BASE_URL}/sendPhoto", data=data, files=files, timeout=60)
        return r.json()
    except Exception as e:
        log.error("sendPhoto error: %s", e)
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
        r = requests.get(url, timeout=30, stream=True)
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
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data=data,
            headers=headers,
            timeout=20,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        # Try multiple selector patterns DDG uses
        for div in soup.select(".result, .web-result")[:max_results + 5]:
            title_tag = div.select_one(".result__title a, .result__a, h2 a")
            if not title_tag:
                continue
            href = title_tag.get("href", "")
            # DDG wraps real URL in uddg= param
            m = re.search(r"uddg=([^&]+)", href)
            link = urllib.parse.unquote(m.group(1)) if m else href
            if not link.startswith("http"):
                continue
            snippet_tag = div.select_one(".result__snippet, .result__body")
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
            results.append({
                "title": title_tag.get_text(strip=True),
                "link": link,
                "snippet": snippet,
            })
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        log.error("web_search error: %s", e)
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
        r = requests.get(url, headers=headers, timeout=25,
                         allow_redirects=True, verify=True)
        r.raise_for_status()
        return r.content
    except requests.exceptions.SSLError:
        try:
            r = requests.get(url, headers=headers, timeout=25,
                             allow_redirects=True, verify=False)
            return r.content
        except Exception as e:
            log.error("fetch_page SSL fallback error: %s", e)
            return None
    except Exception as e:
        log.error("fetch_page error: %s", e)
        return None


def page_to_zip(url: str) -> Optional[bytes]:
    """Download a page and its assets into a ZIP."""
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
                ar = requests.get(asset_url, headers=headers, timeout=10)
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
            r = requests.get(zip_url, timeout=60, stream=True)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
    return None


def translate_text(text: str, target: str, source: str = "auto") -> str:
    """Translate using MyMemory API, handling long texts and HTML entities."""
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
            r = requests.get(
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
        r = requests.get(url, headers=headers, timeout=20)
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
    """Download YouTube video/audio, trimming video to fit 50 MB limit."""
    import shutil
    ffmpeg_path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    ffmpeg_dir = str(Path(ffmpeg_path).parent)

    with tempfile.TemporaryDirectory() as tmp:
        out_tpl = os.path.join(tmp, "%(title).60s.%(ext)s")
        base_cmd = [
            "yt-dlp",
            "--no-playlist",
            "-o", out_tpl,
            "--no-warnings",
            "--no-check-certificate",
            "--ffmpeg-location", ffmpeg_dir,
            "--geo-bypass",
            "--extractor-args", "youtube:player_client=android,web",
            "--socket-timeout", "30",
            "--retries", "3",
        ]
        if audio_only:
            cmd = base_cmd + [
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "5",
                "-f", "bestaudio/best",
                url,
            ]
        else:
            cmd = base_cmd + [
                "-f",
                "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]"
                "/bestvideo[ext=mp4]+bestaudio"
                "/best[ext=mp4]/best[height<=720]/best",
                "--merge-output-format", "mp4",
                url,
            ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=240)
            stderr_text = proc.stderr.decode(errors="replace")
            if proc.returncode != 0:
                log.error("yt-dlp stderr: %s", stderr_text[:600])
                # Try fallback with tv_embedded client for geo-restricted videos
                if "not available in your country" in stderr_text or "geo" in stderr_text.lower():
                    log.info("Retrying with tv_embedded client…")
                    cmd2 = [c if c != "android,web" else "android,tv_embedded" for c in cmd]
                    proc2 = subprocess.run(cmd2, capture_output=True, timeout=240)
                    if proc2.returncode != 0:
                        log.error("yt-dlp fallback stderr: %s",
                                  proc2.stderr.decode(errors="replace")[:400])
                        return None
                else:
                    return None

            files = list(Path(tmp).glob("*"))
            if not files:
                return None
            f = files[0]
            data = f.read_bytes()

            # Trim oversized videos with ffmpeg
            if not audio_only and len(data) > MAX_FILE_SIZE - 1024 * 1024:
                log.info("Video too large (%d MB), trimming…", len(data) // 1024 // 1024)
                trimmed = _trim_video_ffmpeg(f, tmp, ffmpeg_dir)
                if trimmed:
                    data, f = trimmed

            return data, Path(f).name
        except subprocess.TimeoutExpired:
            log.error("yt-dlp timeout")
            return None
        except Exception as e:
            log.error("yt-dlp error: %s", e)
            return None


def _trim_video_ffmpeg(src: Path, tmp: str, ffmpeg_dir: str = "/usr/bin") -> Optional[tuple[bytes, Path]]:
    """Re-encode video to fit ~48 MB."""
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
    """Search Pinterest images using their visual search API."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*, q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.pinterest.com/",
        "X-Requested-With": "XMLHttpRequest",
    }
    # Pinterest resource API
    params = {
        "source_url": f"/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
        "data": json.dumps({
            "options": {
                "query": query,
                "scope": "pins",
                "no_fetch_context_on_resource": False,
            },
            "context": {},
        }),
    }
    try:
        r = requests.get(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            headers=headers,
            params=params,
            timeout=20,
        )
        data = r.json()
        pins = (
            data.get("resource_response", {})
                .get("data", {})
                .get("results", [])
        )
        results = []
        for pin in pins:
            imgs = pin.get("images", {})
            # Prefer "orig" then "736x"
            for size in ("orig", "736x", "474x"):
                img = imgs.get(size, {})
                url_val = img.get("url", "")
                if url_val:
                    results.append({"url": url_val,
                                    "title": pin.get("title") or pin.get("description") or query})
                    break
            if len(results) >= 12:
                break
        if results:
            return results
    except Exception as e:
        log.error("pinterest API error: %s", e)

    # Fallback: scrape page HTML for image URLs
    try:
        r2 = requests.get(
            f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/122.0.0.0 Safari/537.36",
            },
            timeout=20,
        )
        # Extract CDN image urls from page JSON blobs
        matches = re.findall(r'"url":"(https://i\.pinimg\.com/[^"]+)"', r2.text)
        seen = set()
        results = []
        for img_url in matches:
            # Prefer larger images (736x or originals)
            if img_url in seen:
                continue
            seen.add(img_url)
            results.append({"url": img_url, "title": query})
            if len(results) >= 12:
                break
def pinterest_search(query: str) -> list[dict]:
    """Search Pinterest — tries API, then HTML scrape, then Google Images fallback."""
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/122.0.0.0 Safari/537.36")

    def _extract_from_html(text: str) -> list[dict]:
        seen: set[str] = set()
        found = []
        for pattern in [
            r'"orig":\s*\{"url":"(https://i\.pinimg\.com/[^"]+)"',
            r'"736x":\s*\{"url":"(https://i\.pinimg\.com/[^"]+)"',
            r'"(https://i\.pinimg\.com/originals/[^"]+\.(?:jpg|jpeg|png|webp))"',
            r'"(https://i\.pinimg\.com/736x/[^"]+\.(?:jpg|jpeg|png|webp))"',
        ]:
            for u in re.findall(pattern, text):
                clean = u.replace("\\u002F", "/")
                if clean not in seen:
                    seen.add(clean)
                    found.append({"url": clean, "title": query})
                if len(found) >= 12:
                    return found
        return found

    # Strategy 1: Resource API
    try:
        r = requests.get(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json", "Referer": "https://www.pinterest.com/"},
            params={
                "source_url": f"/search/pins/?q={urllib.parse.quote(query)}",
                "data": json.dumps({"options": {"query": query, "scope": "pins"}, "context": {}}),
                "_": str(int(time.time() * 1000)),
            },
            timeout=20,
        )
        if r.status_code == 200 and r.text.strip().startswith("{"):
            pins = (r.json().get("resource_response", {}).get("data", {}).get("results", []))
            results = []
            for pin in pins:
                for size in ("orig", "736x", "474x"):
                    img_url = pin.get("images", {}).get(size, {}).get("url", "")
                    if img_url:
                        results.append({"url": img_url, "title": pin.get("title") or query})
                        break
                if len(results) >= 12:
                    break
            if results:
                return results
    except Exception as e:
        log.error("Pinterest API: %s", e)

    # Strategy 2: HTML scrape
    try:
        r2 = requests.get(
            f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=25,
        )
        results = _extract_from_html(r2.text)
        if results:
            return results
    except Exception as e:
        log.error("Pinterest HTML: %s", e)

    # Strategy 3: Google Images fallback
    try:
        g = google_images_search(f"pinterest {query}", 8)
        return [{"url": x["img"], "title": query} for x in g if x.get("img")][:8]
    except Exception as e:
        log.error("Pinterest Google fallback: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Google Images search
# ══════════════════════════════════════════════════════════════════════════════

def google_images_search(query: str, max_results: int = 8) -> list[dict]:
    """Scrape Google Images for image URLs."""
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/122.0.0.0 Safari/537.36")
    params = {
        "q": query, "tbm": "isch", "hl": "fa", "gl": "ir",
        "safe": "off", "num": str(max_results),
    }
    try:
        r = requests.get("https://www.google.com/search",
                         params=params,
                         headers={"User-Agent": UA, "Accept-Language": "fa,en;q=0.9"},
                         timeout=20)
        text = r.text
        # Extract image data from Google's JSON blobs
        raw_imgs = re.findall(r'\["(https://[^"]+\.(?:jpg|jpeg|png|webp|gif))",[0-9]+,[0-9]+\]', text)
        results = []
        seen: set[str] = set()
        for img_url in raw_imgs:
            if img_url in seen or "google" in img_url:
                continue
            seen.add(img_url)
            results.append({"img": img_url, "title": query})
            if len(results) >= max_results:
                break
        # Second pattern if first returns nothing
        if not results:
            for m in re.finditer(r'"ou":"(https://[^"]+)"', text):
                u = m.group(1)
                if u not in seen:
                    seen.add(u)
                    results.append({"img": u, "title": query})
                if len(results) >= max_results:
                    break
        return results
    except Exception as e:
        log.error("google_images error: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Pexels (free stock photos)
# ══════════════════════════════════════════════════════════════════════════════

def pexels_search(query: str, per_page: int = 8) -> list[dict]:
    """Search Pexels free stock photos by scraping (no API key needed)."""
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/122.0.0.0 Safari/537.36")
    try:
        r = requests.get(
            f"https://www.pexels.com/search/{urllib.parse.quote(query)}/",
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for img_tag in soup.select("img[srcset], img[src]")[:per_page * 3]:
            # Pexels uses srcset — grab the largest
            srcset = img_tag.get("srcset", "")
            src = img_tag.get("src", "")
            img_url = ""
            if srcset:
                parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
                img_url = parts[-1] if parts else src
            else:
                img_url = src
            if not img_url or "data:" in img_url:
                continue
            if not img_url.startswith("http"):
                img_url = "https://www.pexels.com" + img_url
            alt = img_tag.get("alt", query)
            results.append({"url": img_url, "title": alt})
            if len(results) >= per_page:
                break
        return results
    except Exception as e:
        log.error("pexels_search error: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Wikipedia
# ══════════════════════════════════════════════════════════════════════════════

def wikipedia_search(query: str, lang: str = "fa") -> list[dict]:
    """Search Wikipedia and return list of matching articles."""
    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "search", "list": "search", "srsearch": query,
                "srlimit": 8, "format": "json", "utf8": 1,
            },
            headers={"User-Agent": "BaleWebBot/1.0"},
            timeout=15,
        )
        data = r.json()
        results = []
        for item in data.get("query", {}).get("search", []):
            results.append({
                "title": item["title"],
                "snippet": BeautifulSoup(item.get("snippet", ""), "html.parser").get_text(),
                "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(item['title'].replace(' ', '_'))}",
            })
        return results
    except Exception as e:
        log.error("wikipedia_search error: %s", e)
        return []


def wikipedia_article(title: str, lang: str = "fa") -> Optional[str]:
    """Get full plain-text of a Wikipedia article."""
    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "query", "titles": title,
                "prop": "extracts", "exintro": 0, "explaintext": 1,
                "format": "json", "utf8": 1,
            },
            headers={"User-Agent": "BaleWebBot/1.0"},
            timeout=20,
        )
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            return page.get("extract", "")
        return None
    except Exception as e:
        log.error("wikipedia_article error: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Currency converter (using free exchange API)
# ══════════════════════════════════════════════════════════════════════════════

def currency_convert(amount: float, from_cur: str, to_cur: str) -> Optional[str]:
    """Convert currency using exchangerate.host (free)."""
    try:
        r = requests.get(
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
        r2 = requests.get(
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
    try:
        # Resolve domain to IP if needed
        ip = target.strip()
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            import socket
            ip = socket.gethostbyname(ip)
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=15)
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
        r = requests.get(
            f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(long_url)}",
            timeout=15,
        )
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
        return "❌ خطا در کوتاه‌سازی لینک."
    except Exception as e:
        log.error("shorten_url error: %s", e)
        return "❌ خطا در کوتاه‌سازی لینک."


def expand_url(short_url: str) -> str:
    """Follow redirects to find the final URL."""
    try:
        r = requests.head(short_url, allow_redirects=True, timeout=15)
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
        r = requests.post(
            "https://paste.rs/",
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
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
            r = requests.get(
                f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(text)}",
                timeout=15,
            )
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
                     "• یا yt-dlp نیاز به آپدیت دارد: `pip install -U yt-dlp`",
                     parse_mode="Markdown",
                     reply_markup=main_menu_keyboard())
        return
    data, fname = result
    fname = Path(fname).name  # strip full path
    if len(data) > MAX_FILE_SIZE:
        send_message(chat_id,
                     f"❌ حجم ویدیو ({len(data)//1024//1024} MB) بیشتر از ۵۰ MB است.",
                     reply_markup=main_menu_keyboard())
        return
    send_document(chat_id, data, fname, caption=f"📺 {fname[:80]}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ ویدیو ارسال شد.", reply_markup=main_menu_keyboard())


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
    send_message(chat_id, "⏳ در حال جستجو در پینترست...")
    results = pinterest_search(query)
    if not results:
        send_message(chat_id, "❌ تصویری یافت نشد.", reply_markup=main_menu_keyboard())
        return
    sent = 0
    for r in results[:6]:
        try:
            img_bytes = download_file(r["url"], MAX_IMAGE_SIZE)
            if img_bytes:
                send_photo_bytes(chat_id, img_bytes, caption=f"📌 {query}")
                sent += 1
                time.sleep(0.5)
        except Exception:
            pass
    msg = f"✅ {sent} تصویر از پینترست ارسال شد." if sent else "❌ تصویری دانلود نشد."
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, msg, reply_markup=main_menu_keyboard())


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