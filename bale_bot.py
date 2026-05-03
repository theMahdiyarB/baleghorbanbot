#!/usr/bin/env python3
"""
دستیار وب — Bale Bot  (v5)
Full-featured web assistant for Bale messenger.
"""

import os, re, io, json, time, zipfile, logging, tempfile
import requests, subprocess, urllib.parse, threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
TOKEN          = os.getenv("BALE_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_URL       = f"https://tapi.bale.ai/bot{TOKEN}"
MAX_FILE_SIZE  = 20 * 1024 * 1024
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_OCR_SIZE   =  5 * 1024 * 1024
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")   # optional, raises rate limits

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bale_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HTTP SESSION
# ═══════════════════════════════════════════════════════════════════════════════
def _make_session() -> requests.Session:
    s = requests.Session()
    r = Retry(total=3, backoff_factor=0.4,
              status_forcelist=[429, 500, 502, 503, 504],
              allowed_methods=["GET", "POST", "HEAD"])
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.mount("http://",  HTTPAdapter(max_retries=r))
    return s

WEB = _make_session()

# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════
user_state: dict[int, dict] = {}
user_stats: dict[int, dict] = {}
# Cache pending search results so callback buttons can refer to them
result_cache: dict[str, list] = {}   # key → list[dict]

# ═══════════════════════════════════════════════════════════════════════════════
# BALE API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# Chunk size matches index.js: 19 MB per part
CHUNK_SIZE = 19 * 1024 * 1024

# Extensions Bale accepts natively — no ZIP wrapping needed (mirrors index.js)
EXEMPT_EXT = {
    "jpg","jpeg","png","gif","webp","bmp","tiff","tif","svg","ico","heic","heif","avif",
    "mp4","mkv","avi","mov","wmv","flv","webm","m4v","3gp","ts","mts",
    "mp3","ogg","wav","flac","m4a","aac","wma","opus","aiff",
    "zip","rar","7z","tar","gz","bz2","xz","zst","lz4",
    "pdf","doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","epub",
}

MIME_MAP = {
    "jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif",
    "webp":"image/webp","mp4":"video/mp4","mkv":"video/x-matroska",
    "mov":"video/quicktime","avi":"video/x-msvideo","webm":"video/webm",
    "mp3":"audio/mpeg","ogg":"audio/ogg","wav":"audio/wav","flac":"audio/flac",
    "m4a":"audio/mp4","aac":"audio/aac","opus":"audio/opus",
    "pdf":"application/pdf","zip":"application/zip","7z":"application/x-7z-compressed",
    "txt":"text/plain","html":"text/html","json":"application/json",
}

def _mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MIME_MAP.get(ext, "application/octet-stream")

def _should_wrap(filename: str) -> bool:
    """Non-exempt extensions should be ZIP-wrapped so Bale accepts them."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext not in EXEMPT_EXT

def _wrap_zip(data: bytes, filename: str) -> tuple[bytes, str]:
    """Wrap data in a ZIP and return (zip_bytes, new_filename)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, data)
    buf.seek(0)
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return buf.read(), f"{base}.zip"

def _safe_json(r: requests.Response) -> dict:
    """Parse JSON safely — log raw body on failure."""
    try:
        return r.json()
    except Exception:
        log.error("Non-JSON response (status=%d): %r", r.status_code, r.text[:200])
        return {"ok": False, "_raw": r.text[:200]}

def api(method: str, **kwargs) -> dict:
    try:
        r = requests.post(f"{BASE_URL}/{method}", json=kwargs, timeout=30)
        return _safe_json(r)
    except Exception as e:
        log.error("api %s: %s", method, e)
        return {"ok": False}

def send_message(chat_id, text, reply_markup=None,
                 reply_to_message_id=None, parse_mode=None) -> dict:
    kw = dict(chat_id=chat_id, text=str(text)[:4096])
    if reply_markup:
        kw["reply_markup"] = (json.dumps(reply_markup)
                               if not isinstance(reply_markup, str)
                               else reply_markup)
    if reply_to_message_id:
        kw["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        kw["parse_mode"] = parse_mode
    return api("sendMessage", **kw)

def _post_file(endpoint: str, field: str, filename: str,
               data_bytes: bytes, extra_data: dict) -> bool:
    """Low-level multipart file POST to Bale API."""
    files = {field: (filename, io.BytesIO(data_bytes), _mime(filename))}
    try:
        r = requests.post(f"{BASE_URL}/{endpoint}", data=extra_data,
                          files=files, timeout=180)
        resp = _safe_json(r)
        ok = resp.get("ok", False)
        if not ok:
            log.error("%s failed: %s  file=%s  size=%dKB",
                      endpoint, resp, filename, len(data_bytes)//1024)
        return ok
    except Exception as e:
        log.error("%s exception: %s  file=%s", endpoint, e, filename)
        return False

def _send_one_chunk(chat_id, data_bytes: bytes, filename: str,
                    caption="", media_type="document") -> bool:
    """Send a single chunk using the correct Bale endpoint."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    extra = {"chat_id": str(chat_id), "caption": caption[:1024]}

    if media_type == "video" or ext in {"mp4","mkv","avi","mov","webm","m4v"}:
        extra["supports_streaming"] = "true"
        return _post_file("sendVideo", "video", filename, data_bytes, extra)
    elif media_type == "audio" or ext in {"mp3","ogg","wav","flac","m4a","aac","opus"}:
        return _post_file("sendAudio", "audio", filename, data_bytes, extra)
    elif media_type == "photo" or ext in {"jpg","jpeg","png","gif","webp"}:
        return _post_file("sendPhoto", "photo", filename, data_bytes, extra)
    else:
        return _post_file("sendDocument", "document", filename, data_bytes, extra)

def smart_send(chat_id, data: bytes, filename: str,
               caption="", media_type="auto") -> bool:
    """
    Smart file sender that mirrors index.js logic:
    1. Wrap unsupported extensions in ZIP
    2. If > CHUNK_SIZE, split into .part1ofN chunks
    3. Send each chunk with correct endpoint
    Returns True if all chunks sent successfully.
    """
    if not data:
        log.error("smart_send: empty data for %s", filename)
        return False

    total_mb = len(data) / 1024 / 1024
    log.info("smart_send: %s  %.1fMB  type=%s", filename, total_mb, media_type)

    # Step 1: ZIP wrap if needed (skip for chunks and exempt types)
    import re as _re
    is_chunk = bool(_re.search(r'\.part\d+of\d+\.', filename))
    if not is_chunk and _should_wrap(filename):
        log.info("smart_send: wrapping %s in ZIP", filename)
        send_message(chat_id, f"📦 در حال زیپ کردن `{filename}`…", parse_mode="Markdown")
        data, filename = _wrap_zip(data, filename)
        log.info("smart_send: zipped → %s  %.1fMB", filename, len(data)/1024/1024)

    total_size = len(data)

    # Step 2: Send in one shot if small enough
    if total_size <= CHUNK_SIZE:
        return _send_one_chunk(chat_id, data, filename, caption, media_type)

    # Step 3: Chunk it
    total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    ext  = ("." + filename.rsplit(".", 1)[1]) if "." in filename else ""
    log.info("smart_send: splitting into %d chunks", total_chunks)
    send_message(chat_id,
                 f"📤 فایل بزرگ است — ارسال در *{total_chunks} بخش*…",
                 parse_mode="Markdown")
    all_ok = True
    for i in range(total_chunks):
        start = i * CHUNK_SIZE
        end   = min(start + CHUNK_SIZE, total_size)
        chunk = data[start:end]
        chunk_name = f"{base}.part{i+1}of{total_chunks}{ext}"
        chunk_mb = len(chunk) / 1024 / 1024
        send_message(chat_id,
                     f"📤 ارسال بخش {i+1} از {total_chunks} ({chunk_mb:.1f}MB)…")
        ok = _send_one_chunk(chat_id, chunk, chunk_name, caption="", media_type="document")
        if not ok:
            send_message(chat_id, f"❌ ارسال بخش {i+1} ناموفق بود.")
            all_ok = False
            break
    if all_ok:
        send_message(chat_id,
                     f"✅ همه {total_chunks} بخش ارسال شدند!\n\n"
                     f"برای ترکیب:\n"
                     f"`cat {base}.part*of{total_chunks}{ext} > {filename}`",
                     parse_mode="Markdown")
    return all_ok

# Convenience wrappers (keep old call sites working)
def send_document(chat_id, file_bytes: bytes, filename: str,
                  caption="", reply_to=None) -> bool:
    return smart_send(chat_id, file_bytes, filename, caption, media_type="document")

def send_video(chat_id, video_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, video_bytes, filename, caption, media_type="video")

def send_photo(chat_id, img_bytes: bytes, caption="", reply_markup=None) -> bool:
    """Photos don't chunk — just post directly."""
    if not img_bytes:
        return False
    ext = "jpg"
    files = {"photo": (f"image.{ext}", io.BytesIO(img_bytes), "image/jpeg")}
    data  = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{BASE_URL}/sendPhoto", data=data, files=files, timeout=60)
        resp = _safe_json(r)
        ok = resp.get("ok", False)
        if not ok:
            log.error("sendPhoto failed: %s", resp)
        return ok
    except Exception as e:
        log.error("sendPhoto exception: %s", e)
        return False

def send_audio(chat_id, audio_bytes: bytes, filename: str, caption="") -> bool:
    return smart_send(chat_id, audio_bytes, filename, caption, media_type="audio")

def chat_action(chat_id, action="typing"):
    api("sendChatAction", chat_id=chat_id, action=action)

def get_file_url(file_id: str) -> Optional[str]:
    resp = api("getFile", file_id=file_id)
    if resp.get("ok"):
        return f"https://tapi.bale.ai/file/bot{TOKEN}/{resp['result']['file_path']}"
    return None


def download_bytes(url: str, max_bytes: int = 500 * 1024 * 1024) -> Optional[bytes]:
    log.debug("download_bytes: %s  max=%.0fMB", url, max_bytes/1024/1024)
    try:
        r = WEB.get(url, timeout=120, stream=True,
                    headers={"User-Agent": UA_DESK})
        log.debug("download_bytes: status=%d content-length=%s",
                  r.status_code, r.headers.get("content-length", "?"))
        chunks, total = [], 0
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                log.warning("download_bytes: exceeded %.0fMB cap", max_bytes/1024/1024)
                return None
        result = b"".join(chunks)
        log.info("download_bytes: got %.1fMB", len(result)/1024/1024)
        return result
    except Exception as e:
        log.error("download_bytes: %s — %s", url, e)
        return None

def answer_cb(cb_id: str, text=""):
    try: api("answerCallbackQuery", callback_query_id=cb_id, text=text)
    except Exception: pass

# ═══════════════════════════════════════════════════════════════════════════════
# RESULT CACHE  (stores search results so buttons can reference them)
# ═══════════════════════════════════════════════════════════════════════════════
import hashlib

def cache_set(key: str, data: list):
    result_cache[key] = data
    # Prune if too large
    if len(result_cache) > 500:
        oldest = list(result_cache.keys())[:100]
        for k in oldest:
            result_cache.pop(k, None)

def cache_get(key: str) -> list:
    return result_cache.get(key, [])

def make_cache_key(prefix: str, query: str, page: int) -> str:
    h = hashlib.md5(f"{prefix}:{query}:{page}".encode()).hexdigest()[:8]
    return f"{prefix}_{h}"

# ═══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
def main_menu_kb():
    return {"inline_keyboard": [
        [{"text": "🔎 جستجو در وب",      "callback_data": "mode_search"},
         {"text": "🌐 مشاهده سایت",      "callback_data": "mode_open"}],
        [{"text": "📚 مقاله علمی",        "callback_data": "mode_scholar"},
         {"text": "📖 ویکی‌پدیا",         "callback_data": "mode_wiki"}],
        [{"text": "📺 یوتیوب",            "callback_data": "mode_youtube"},
         {"text": "🎵 دانلود موسیقی MP3", "callback_data": "mode_music"}],
        [{"text": "🖼 دانلود عکس",        "callback_data": "mode_images"},
         {"text": "🐙 GitHub",            "callback_data": "mode_github"}],
        [{"text": "🌐 ترجمه",             "callback_data": "mode_translate"},
         {"text": "🖼 OCR متن از عکس",   "callback_data": "mode_ocr"}],
        [{"text": "💱 تبدیل ارز",         "callback_data": "mode_currency"},
         {"text": "🌐 IP / دامنه",        "callback_data": "mode_iplookup"}],
        [{"text": "🔗 کوتاه‌سازی لینک",  "callback_data": "mode_shorten"},
         {"text": "📱 ساخت QR کد",        "callback_data": "mode_qr"}],
        [{"text": "📋 اشتراک‌گذاری متن", "callback_data": "mode_paste"},
         {"text": "🔍 باز کردن لینک",    "callback_data": "mode_expand"}],
        [{"text": "📊 آمار کاربری",       "callback_data": "stats"},
         {"text": "🔒 حریم خصوصی",       "callback_data": "privacy"}],
        [{"text": "❓ راهنما",             "callback_data": "help"}],
    ]}

def cancel_kb():
    return {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "cancel"}]]}

def home_kb():
    return {"inline_keyboard": [[{"text": "🏠 منوی اصلی", "callback_data": "home"}]]}

def paged_kb(prev_cb, next_cb, page, has_next):
    row = []
    if page > 0:
        row.append({"text": "◀️ قبلی", "callback_data": prev_cb})
    if has_next:
        row.append({"text": "بعدی ▶️", "callback_data": next_cb})
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": buttons}

def image_source_kb():
    return {"inline_keyboard": [
        [{"text": "🖼 تصاویر Bing",     "callback_data": "img_src_bing"},
         {"text": "📌 پینترست",          "callback_data": "img_src_pinterest"}],
        [{"text": "📷 Pixabay/Pexels",   "callback_data": "img_src_pexels"},
         {"text": "🎨 Wikimedia",        "callback_data": "img_src_wiki"}],
        [{"text": "❌ انصراف",            "callback_data": "cancel"}],
    ]}

def youtube_action_kb():
    return {"inline_keyboard": [
        [{"text": "📥 دانلود ویدیو",    "callback_data": "yt_video"},
         {"text": "🔍 جستجوی یوتیوب",  "callback_data": "yt_search"}],
        [{"text": "❌ انصراف",           "callback_data": "cancel"}],
    ]}

def github_action_kb():
    return {"inline_keyboard": [
        [{"text": "🔍 جستجوی مخازن",   "callback_data": "gh_search"},
         {"text": "📥 دانلود ZIP",      "callback_data": "gh_zip"}],
        [{"text": "📦 دانلود Release",  "callback_data": "gh_release"}],
        [{"text": "❌ انصراف",           "callback_data": "cancel"}],
    ]}

def translate_kb():
    langs = [("🇮🇷 فارسی","fa"),("🇬🇧 انگلیسی","en"),
             ("🇸🇦 عربی","ar"),("🇩🇪 آلمانی","de"),
             ("🇫🇷 فرانسوی","fr"),("🇷🇺 روسی","ru")]
    rows, row = [], []
    for label, code in langs:
        row.append({"text": label, "callback_data": f"trlang_{code}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{"text": "❌ انصراف", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}

def site_view_kb(url_key: str):
    """Buttons shown after screenshot of a website."""
    return {"inline_keyboard": [
        [{"text": "📝 متن صفحه",  "callback_data": f"site_text_{url_key}"},
         {"text": "🌐 فایل HTML", "callback_data": f"site_html_{url_key}"}],
        [{"text": "🗜 ZIP آفلاین", "callback_data": f"site_zip_{url_key}"},
         {"text": "📑 PDF صفحه",  "callback_data": f"site_pdf_{url_key}"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]}

def search_results_kb(results: list, cache_key: str, page: int,
                      has_next: bool, mode: str) -> dict:
    """Each search result becomes an inline button."""
    rows = []
    offset = page * 10
    for i, r in enumerate(results):
        num = offset + i + 1
        title = r.get("title", r.get("name", f"نتیجه {num}"))[:45]
        cb = f"res_{cache_key}_{i}"
        rows.append([{"text": f"{num}. {title}", "callback_data": cb}])
    # Pagination row
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"page_{mode}_{page-1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"page_{mode}_{page+1}"})
    if pag:
        rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

def yt_results_kb(results: list, cache_key: str, page: int, has_next: bool) -> dict:
    rows = []
    offset = page * 8
    for i, r in enumerate(results):
        num = offset + i + 1
        title = r.get("title", f"ویدیو {num}")[:40]
        dur = f" [{r.get('duration','')}]" if r.get("duration") else ""
        rows.append([{"text": f"{num}. {title}{dur}", "callback_data": f"yt_res_{cache_key}_{i}"}])
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"yt_page_{page-1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"yt_page_{page+1}"})
    if pag: rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

def gh_repo_kb(results: list, cache_key: str, page: int, has_next: bool) -> dict:
    rows = []
    offset = page * 8
    for i, r in enumerate(results):
        num = offset + i + 1
        name = r.get("full_name", f"repo {num}")[:40]
        stars = r.get("stargazers_count", 0)
        rows.append([{"text": f"{num}. {name} ⭐{stars}",
                       "callback_data": f"gh_res_{cache_key}_{i}"}])
    pag = []
    if page > 0:
        pag.append({"text": "◀️ قبلی", "callback_data": f"gh_page_{page-1}"})
    if has_next:
        pag.append({"text": "بعدی ▶️", "callback_data": f"gh_page_{page+1}"})
    if pag: rows.append(pag)
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

def gh_repo_action_kb(repo_full: str) -> dict:
    safe = repo_full.replace("/", "__")
    return {"inline_keyboard": [
        [{"text": "📥 دانلود ZIP کل مخزن",     "callback_data": f"ghact_zip_{safe}"},
         {"text": "📦 آخرین Release",           "callback_data": f"ghact_rel_{safe}"}],
        [{"text": "📋 اطلاعات بیشتر",           "callback_data": f"ghact_info_{safe}"}],
        [{"text": "🔙 برگشت به نتایج",          "callback_data": "gh_back"}],
    ]}

def images_more_kb(cache_key: str, page: int, source: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "📥 دانلود بیشتر", "callback_data": f"img_more_{source}_{cache_key}_{page+1}"}],
        [{"text": "🔄 منبع دیگر",    "callback_data": "mode_images"},
         {"text": "🏠 منوی اصلی",    "callback_data": "home"}],
    ]}

def wiki_result_kb(results: list, cache_key: str, lang: str) -> dict:
    rows = []
    for i, r in enumerate(results):
        title = r.get("title", f"مقاله {i+1}")[:50]
        rows.append([{"text": f"📖 {title}", "callback_data": f"wiki_art_{cache_key}_{i}_{lang}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return {"inline_keyboard": rows}

# ═══════════════════════════════════════════════════════════════════════════════
# WEB SCRAPING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════
UA_DESK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.0.0 Safari/537.36")
UA_MOB  = ("Mozilla/5.0 (Linux; Android 13; SM-G991B) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/122.0.6261.119 Mobile Safari/537.36")

def web_search(query: str, max_results=10, page=0) -> list[dict]:
    log.info("web_search: %r  page=%d", query, page)
    data = {"q": query, "b": "", "kl": "wt-wt"}
    if page > 0:
        data.update({"s": str(page*30), "dc": str(page*30+1), "v": "l", "o": "json"})
    try:
        r = WEB.post("https://html.duckduckgo.com/html/", data=data,
                     headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
                     timeout=20)
        log.debug("DDG: status=%d len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".result, .web-result"):
            ta = div.select_one(".result__title a, .result__a, h2 a")
            if not ta: continue
            href = ta.get("href","")
            m = re.search(r"uddg=([^&]+)", href)
            link = urllib.parse.unquote(m.group(1)) if m else href
            if not link.startswith("http"): continue
            sn = div.select_one(".result__snippet, .result__body")
            results.append({"title": ta.get_text(strip=True),
                             "link": link,
                             "snippet": sn.get_text(strip=True) if sn else ""})
            if len(results) >= max_results: break
        log.info("web_search: %d results", len(results))
        if not results:
            log.warning("DDG returned 0 results. HTML head: %s", r.text[:300])
        return results
    except Exception as e:
        log.error("web_search error: %s", e, exc_info=True)
        return []

def fetch_page(url: str) -> Optional[bytes]:
    log.info("fetch_page: %s", url)
    headers = {"User-Agent": UA_DESK,
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    try:
        r = WEB.get(url, headers=headers, timeout=25, allow_redirects=True)
        log.debug("fetch_page: status=%d len=%d", r.status_code, len(r.content))
        r.raise_for_status()
        return r.content
    except requests.exceptions.SSLError:
        log.warning("fetch_page: SSL error, retrying without verify")
        try:
            r = WEB.get(url, headers=headers, timeout=25, verify=False)
            return r.content
        except Exception as e:
            log.error("fetch_page SSL fallback: %s", e); return None
    except Exception as e:
        log.error("fetch_page: %s", e, exc_info=True); return None

def screenshot_page(url: str) -> Optional[bytes]:
    """
    1920×1080 viewport screenshot using Playwright.
    Single shot — not scrolled. Returns JPEG bytes.
    """
    log.info("screenshot_page: %s", url)
    try:
        from playwright.sync_api import sync_playwright
        VW, VH = 1920, 1080
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
            ])
            page = browser.new_page(viewport={"width": VW, "height": VH})
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(2)
            img = page.screenshot(type="jpeg", quality=82, clip={
                "x": 0, "y": 0, "width": VW, "height": VH,
            })
            browser.close()
        log.info("screenshot_page: %dKB", len(img)//1024)
        return img
    except Exception as e:
        log.error("screenshot_page error: %s", e, exc_info=True)
        return None

def page_to_zip(url: str) -> Optional[bytes]:
    log.info("page_to_zip: %s", url)
    html = fetch_page(url)
    if not html: return None
    soup = BeautifulSoup(html, "html.parser")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html)
        base = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        for tag in soup.find_all(["img","link","script"])[:25]:
            src = tag.get("src") or tag.get("href","")
            if not src or src.startswith("data:"): continue
            asset_url = urllib.parse.urljoin(base, src)
            try:
                ar = WEB.get(asset_url, timeout=8)
                fname = Path(urllib.parse.urlparse(asset_url).path).name or "asset"
                zf.writestr(f"assets/{fname}", ar.content)
            except Exception: pass
    buf.seek(0)
    return buf.read()

def page_to_text(url: str) -> str:
    html = fetch_page(url)
    if not html: return "❌ خطا در دریافت صفحه."
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script","style","nav","footer","aside"]): t.decompose()
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    return "\n".join(lines[:100])

def page_to_pdf(url: str) -> Optional[bytes]:
    """
    Generate a full-page PDF using Playwright's built-in PDF print.
    This captures the entire page (scrollable content) as a proper PDF,
    not a screenshot. Falls back to wkhtmltopdf, then raw HTML.
    """
    log.info("page_to_pdf: %s", url)

    # Strategy 1: Playwright PDF (best quality, full page)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
            ])
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(url, timeout=30000, wait_until="networkidle")
            time.sleep(2)
            pdf_bytes = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm",
                        "left": "10mm", "right": "10mm"},
            )
            browser.close()
        if pdf_bytes and len(pdf_bytes) > 500:
            log.info("page_to_pdf (playwright): %dKB", len(pdf_bytes)//1024)
            return pdf_bytes
    except Exception as e:
        log.warning("page_to_pdf playwright failed: %s", e)

    # Strategy 2: wkhtmltopdf
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            out = tf.name
        result = subprocess.run(
            ["wkhtmltopdf",
             "--quiet",
             "--load-error-handling", "ignore",
             "--load-media-error-handling", "ignore",
             "--no-stop-slow-scripts",
             "--javascript-delay", "2000",
             "--page-size", "A4",
             "--encoding", "utf-8",
             url, out],
            capture_output=True, timeout=60,
        )
        if Path(out).exists() and Path(out).stat().st_size > 500:
            data = Path(out).read_bytes()
            Path(out).unlink(missing_ok=True)
            log.info("page_to_pdf (wkhtmltopdf): %dKB", len(data)//1024)
            return data
        log.warning("wkhtmltopdf rc=%d stderr=%s",
                    result.returncode, result.stderr.decode()[:200])
        Path(out).unlink(missing_ok=True)
    except subprocess.TimeoutExpired:
        log.warning("page_to_pdf: wkhtmltopdf timed out")
    except Exception as e:
        log.error("page_to_pdf wkhtmltopdf: %s", e)

    # Strategy 3: raw HTML fallback (better than nothing)
    log.warning("page_to_pdf: falling back to raw HTML")
    return fetch_page(url)

# ─── Scholar ──────────────────────────────────────────────────────────────────
def scholar_search(query: str, page=0) -> list[dict]:
    log.info("scholar_search: %r page=%d", query, page)
    start = page * 10
    try:
        r = WEB.get("https://scholar.google.com/scholar",
                    params={"q": query, "hl": "en", "num": 10, "start": start},
                    headers={"User-Agent": UA_DESK, "Accept-Language": "en-US,en;q=0.9"},
                    timeout=20)
        log.debug("scholar: status=%d len=%d", r.status_code, len(r.text))
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".gs_ri"):
            ta = div.select_one(".gs_rt a") or div.select_one("h3 a")
            if not ta: continue
            sn = div.select_one(".gs_rs")
            meta = div.select_one(".gs_a")
            results.append({
                "title": ta.get_text(strip=True),
                "link":  ta.get("href",""),
                "snippet": sn.get_text(strip=True) if sn else "",
                "meta":    meta.get_text(strip=True) if meta else "",
            })
        log.info("scholar_search: %d results", len(results))
        return results
    except Exception as e:
        log.error("scholar_search: %s", e, exc_info=True)
        return []

# ─── Wikipedia ────────────────────────────────────────────────────────────────
def wikipedia_search(query: str, lang="fa") -> list[dict]:
    log.info("wikipedia_search: %r lang=%s", query, lang)
    HDR = {"User-Agent": "BaleBot/1.0"}
    for base in [f"https://{lang}.wikipedia.org",
                 f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(f"{base}/w/api.php",
                        params={"action":"opensearch","search":query,
                                "limit":8,"namespace":0,"format":"json"},
                        headers=HDR, timeout=15)
            log.debug("wiki opensearch: status=%d", r.status_code)
            if r.status_code == 200:
                d = r.json()
                titles = d[1] if len(d)>1 else []
                descs  = d[2] if len(d)>2 else []
                urls   = d[3] if len(d)>3 else []
                results = [{"title": t,
                             "snippet": descs[i] if i<len(descs) else "",
                             "url": urls[i] if i<len(urls) else "",
                             "key": t.replace(" ","_"),
                             "lang": lang}
                           for i,t in enumerate(titles)]
                if results:
                    log.info("wiki_search: %d results", len(results))
                    return results
        except Exception as e:
            log.error("wikipedia_search %s: %s", base, e)
    return []

def wikipedia_article(title: str, lang="fa") -> Optional[str]:
    log.info("wikipedia_article: %r lang=%s", title, lang)
    HDR = {"User-Agent": "BaleBot/1.0"}
    key = urllib.parse.quote(title.replace(" ","_"))
    for base in [f"https://{lang}.wikipedia.org",
                 f"https://{lang}.m.wikipedia.org"]:
        try:
            r = WEB.get(f"{base}/api/rest_v1/page/summary/{key}",
                        headers=HDR, timeout=15)
            log.debug("wiki summary: status=%d", r.status_code)
            if r.status_code == 200:
                extract = r.json().get("extract","")
                if extract and len(extract) > 80:
                    # Try full text
                    try:
                        r2 = WEB.get(f"{base}/w/api.php",
                                     params={"action":"query","titles":title,
                                             "prop":"extracts","explaintext":1,
                                             "exsectionformat":"plain",
                                             "format":"json","utf8":1},
                                     headers=HDR, timeout=20)
                        log.debug("wiki full: status=%d", r2.status_code)
                        if r2.status_code == 200:
                            for pg in r2.json().get("query",{}).get("pages",{}).values():
                                full = pg.get("extract","")
                                if full and len(full) > len(extract):
                                    return full
                    except Exception: pass
                    return extract
        except Exception as e:
            log.error("wikipedia_article %s: %s", base, e)
    return None

# ─── YouTube ──────────────────────────────────────────────────────────────────
def youtube_search(query: str, max_results=8) -> list[dict]:
    log.info("youtube_search: %r", query)
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print",
             "%(id)s|||%(title)s|||%(uploader)s|||%(duration_string)s",
             f"ytsearch{max_results}:{query}", "--no-warnings",
             "--no-check-certificate"],
            capture_output=True, text=True, timeout=30,
        )
        items = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 2 and parts[0].strip():
                vid_id = parts[0].strip()
                items.append({
                    "id": vid_id,
                    "title": parts[1].strip(),
                    "uploader": parts[2].strip() if len(parts)>2 else "",
                    "duration":  parts[3].strip() if len(parts)>3 else "",
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                })
        log.info("youtube_search: %d results", len(items))
        return items
    except Exception as e:
        log.error("youtube_search: %s", e, exc_info=True)
        return []

def youtube_download(url: str, audio_only=False) -> Optional[tuple[bytes,str]]:
    log.info("youtube_download: url=%r audio=%s", url, audio_only)
    import shutil
    ffmpeg_dir = str(Path(shutil.which("ffmpeg") or "/usr/bin/ffmpeg").parent)

    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-check-certificate",
        "--ffmpeg-location", ffmpeg_dir,
        "--geo-bypass",
        "--socket-timeout", "30",
        "--retries", "5",
        # Try multiple clients in order — tv_embedded avoids most geo/format blocks
        "--extractor-args", "youtube:player_client=tv_embedded,ios,web",
    ]

    def _run(extra_args, target_url) -> Optional[tuple[bytes,str]]:
        with tempfile.TemporaryDirectory() as tmp:
            out_tpl = os.path.join(tmp, "%(title).60s.%(ext)s")
            cmd = base_cmd + ["-o", out_tpl] + extra_args + [target_url]
            log.debug("yt-dlp cmd: %s", " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            stderr = proc.stderr.decode(errors="replace")
            stdout = proc.stdout.decode(errors="replace")
            if proc.returncode != 0:
                log.error("yt-dlp rc=%d\nSTDERR: %s\nSTDOUT: %s",
                          proc.returncode, stderr[:1000], stdout[:300])
                return None
            files = list(Path(tmp).glob("*"))
            if not files:
                log.error("yt-dlp: no output files in %s  stdout=%s", tmp, stdout[:200])
                return None
            # Pick largest file (avoid .part files)
            f = sorted(files, key=lambda x: x.stat().st_size, reverse=True)[0]
            data = f.read_bytes()
            log.info("yt-dlp OK: %s  %.1fMB", f.name, len(data)/1024/1024)
            return data, f.name

    if audio_only:
        # Strategy 1: Let yt-dlp pick best audio, convert to mp3 — NO -f flag
        for args in [
            # No format filter at all — yt-dlp picks whatever is available
            ["-x", "--audio-format", "mp3", "--audio-quality", "5"],
            # Explicit bestaudio with no codec constraint
            ["-x", "--audio-format", "mp3", "--audio-quality", "5",
             "-f", "bestaudio"],
            # Absolute fallback: download anything and convert
            ["-x", "--audio-format", "mp3"],
        ]:
            result = _run(args, url)
            if result:
                return result
        log.error("youtube_download: all audio strategies failed for %s", url)
        return None
    else:
        # Strategy: try from best quality down, no codec constraints
        for fmt_args in [
            # Best video+audio, merged to mp4
            ["-f", "bestvideo+bestaudio", "--merge-output-format", "mp4"],
            # Best single-file
            ["-f", "best", "--merge-output-format", "mp4"],
            # Absolute fallback — no format spec
            ["--merge-output-format", "mp4"],
        ]:
            result = _run(fmt_args, url)
            if result:
                data, fname = result
                # Trim if too large for a single chunk
                if len(data) > 200 * 1024 * 1024:
                    log.info("Video %.1fMB very large, trimming…", len(data)/1024/1024)
                    with tempfile.TemporaryDirectory() as tmp2:
                        src = Path(tmp2) / fname
                        src.write_bytes(data)
                        trimmed = _trim_video(src, tmp2, ffmpeg_dir)
                        if trimmed:
                            data, fname_path = trimmed
                            fname = Path(fname_path).name
                return data, fname
        log.error("youtube_download: all video strategies failed for %s", url)
        return None

def _trim_video(src: Path, tmp: str, ffmpeg_dir="/usr/bin") -> Optional[tuple[bytes,Path]]:
    out = Path(tmp) / ("trimmed_" + src.name)
    ffprobe = str(Path(ffmpeg_dir)/"ffprobe")
    ffmpeg  = str(Path(ffmpeg_dir)/"ffmpeg")
    try:
        probe = subprocess.run([ffprobe,"-v","error","-show_entries","format=duration",
                                "-of","default=noprint_wrappers=1:nokey=1",str(src)],
                               capture_output=True, text=True, timeout=30)
        duration = float(probe.stdout.strip() or "300")
        vbr = max(300, int((48*1024*1024*8)/duration/1000) - 128)
        subprocess.run([ffmpeg,"-y","-i",str(src),"-c:v","libx264","-b:v",f"{vbr}k",
                        "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)],
                       capture_output=True, timeout=300, check=True)
        data = out.read_bytes()
        return data, out
    except Exception as e:
        log.error("_trim_video: %s", e)
        return None

# ─── GitHub ───────────────────────────────────────────────────────────────────
def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "BaleBot/1.0",
         "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

def github_search_repos(query: str, page=0) -> list[dict]:
    log.info("github_search_repos: %r page=%d", query, page)
    try:
        r = WEB.get("https://api.github.com/search/repositories",
                    params={"q": query, "sort": "stars", "per_page": 8,
                            "page": page+1},
                    headers=_gh_headers(), timeout=20)
        log.debug("github_search: status=%d", r.status_code)
        if r.status_code == 200:
            items = r.json().get("items", [])
            log.info("github_search: %d repos", len(items))
            return items
        log.error("github_search error: %s", r.text[:200])
        return []
    except Exception as e:
        log.error("github_search_repos: %s", e, exc_info=True)
        return []

def github_repo_info(full_name: str) -> Optional[dict]:
    log.info("github_repo_info: %s", full_name)
    try:
        r = WEB.get(f"https://api.github.com/repos/{full_name}",
                    headers=_gh_headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        log.error("github_repo_info: %s", e)
        return None

def github_latest_release(full_name: str) -> Optional[dict]:
    log.info("github_latest_release: %s", full_name)
    try:
        r = WEB.get(f"https://api.github.com/repos/{full_name}/releases/latest",
                    headers=_gh_headers(), timeout=15)
        log.debug("gh release: status=%d", r.status_code)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        log.error("github_latest_release: %s", e)
        return None

def github_zip(repo_url: str) -> Optional[bytes]:
    log.info("github_zip: %s", repo_url)
    m = re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", repo_url)
    if not m:
        log.error("github_zip: cannot parse URL")
        return None
    slug = m.group(1)
    for branch in ["main","master"]:
        url = f"https://github.com/{slug}/archive/refs/heads/{branch}.zip"
        try:
            r = WEB.get(url, timeout=120, stream=True)
            log.debug("github_zip: %s status=%d", url, r.status_code)
            if r.status_code == 200:
                data = r.content
                log.info("github_zip: %dMB", len(data)//1024//1024)
                return data
        except Exception as e:
            log.error("github_zip %s: %s", branch, e)
    return None

# ─── Images ───────────────────────────────────────────────────────────────────
def images_bing(query: str, max_results=8) -> list[dict]:
    log.info("images_bing: %r", query)
    try:
        r = WEB.get("https://www.bing.com/images/search",
                    params={"q": query, "form": "HDRSC2", "first":"1"},
                    headers={"User-Agent": UA_DESK, "Referer": "https://www.bing.com/"},
                    timeout=20)
        log.debug("bing_images: status=%d len=%d", r.status_code, len(r.text))
        results = []
        seen: set[str] = set()
        for pattern in [r'"murl":"(https?://[^"]+\.(?:jpg|jpeg|png|webp))"',
                         r'murl&quot;:&quot;(https?://[^&]+\.(?:jpg|jpeg|png))'
                         r'&quot;']:
            for u in re.findall(pattern, r.text):
                u = u.replace("&amp;","&")
                if u not in seen and not any(x in u for x in ["bing.com","microsoft.com"]):
                    seen.add(u)
                    results.append({"url": u, "title": query})
                if len(results) >= max_results: break
            if len(results) >= max_results: break
        log.info("images_bing: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_bing: %s", e, exc_info=True)
        return []

def images_pinterest(query: str, max_results=8) -> list[dict]:
    log.info("images_pinterest: %r", query)
    # Strategy 1: Pinterest API
    try:
        r = WEB.get(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            headers={"User-Agent": UA_DESK, "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json"},
            params={"source_url": f"/search/pins/?q={urllib.parse.quote(query)}",
                    "data": json.dumps({"options":{"query":query,"scope":"pins"},
                                        "context":{}}),
                    "_": str(int(time.time()*1000))},
            timeout=20)
        log.debug("pinterest_api: status=%d", r.status_code)
        if r.status_code == 200 and r.text.strip().startswith("{"):
            pins = (r.json().get("resource_response",{})
                            .get("data",{}).get("results",[]))
            results = []
            for pin in pins:
                for size in ("orig","736x","474x"):
                    u = pin.get("images",{}).get(size,{}).get("url","")
                    if u:
                        results.append({"url":u,"title":pin.get("title") or query})
                        break
                if len(results) >= max_results: break
            if results:
                log.info("pinterest_api: %d results", len(results))
                return results
    except Exception as e:
        log.error("pinterest_api: %s", e)

    # Strategy 2: HTML scrape
    try:
        r2 = WEB.get(f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed",
                     headers={"User-Agent": UA_DESK}, timeout=25)
        log.debug("pinterest_html: status=%d len=%d", r2.status_code, len(r2.text))
        seen: set[str] = set()
        results = []
        for pat in [r'"orig":\s*\{"url":"(https://i\.pinimg\.com/[^"]+)"',
                    r'"(https://i\.pinimg\.com/originals/[^"]+\.(?:jpg|jpeg|png|webp))"',
                    r'"(https://i\.pinimg\.com/736x/[^"]+\.(?:jpg|jpeg|png|webp))"']:
            for u in re.findall(pat, r2.text):
                u = u.replace("\\u002F","/")
                if u not in seen:
                    seen.add(u)
                    results.append({"url":u,"title":query})
                if len(results) >= max_results: break
        if results:
            log.info("pinterest_html: %d results", len(results))
            return results
        log.warning("pinterest_html: 0 results. Page head: %s", r2.text[:300])
    except Exception as e:
        log.error("pinterest_html: %s", e)

    # Strategy 3: Fall back to Bing with pinterest site: filter
    log.info("pinterest: falling back to Bing")
    return images_bing(f"site:pinterest.com {query}", max_results)

def images_pexels(query: str, max_results=8) -> list[dict]:
    """Pixabay API (free, no key for basic use) + Unsplash CDN."""
    log.info("images_pexels: %r", query)
    PIXABAY_KEY = "47075717-fbc72d1e73d12c83cfdb8b44e"
    try:
        r = WEB.get("https://pixabay.com/api/",
                    params={"key": PIXABAY_KEY, "q": query, "image_type": "photo",
                            "per_page": max_results, "safesearch": "true", "lang": "en"},
                    headers={"User-Agent": "BaleBot/1.0"}, timeout=15)
        log.debug("pixabay: status=%d", r.status_code)
        if r.status_code == 200:
            hits = r.json().get("hits", [])
            results = [{"url": h.get("webformatURL") or h.get("largeImageURL"),
                         "title": h.get("tags", query)[:60]}
                       for h in hits if h.get("webformatURL")]
            if results:
                log.info("pixabay: %d results", len(results))
                return results
        log.warning("pixabay: status=%d resp=%s", r.status_code, r.text[:100])
    except Exception as e:
        log.error("images_pexels pixabay: %s", e)

    # Unsplash CDN fallback (redirects to real images)
    try:
        slug = urllib.parse.quote(query.replace(" ",","))
        results = []
        for i in range(min(max_results, 6)):
            r2 = WEB.get(f"https://source.unsplash.com/featured/800x600?{slug}&sig={i}",
                         allow_redirects=True, timeout=20,
                         headers={"User-Agent": "BaleBot/1.0"})
            log.debug("unsplash %d: status=%d len=%d", i, r2.status_code, len(r2.content))
            if r2.status_code == 200 and len(r2.content) > 5000:
                results.append({"url": r2.url, "title": f"{query} #{i+1}",
                                 "_bytes": r2.content})
        if results:
            log.info("unsplash: %d results", len(results))
            return results
    except Exception as e:
        log.error("images_pexels unsplash: %s", e)
    return []

def images_wikimedia(query: str, max_results=8) -> list[dict]:
    log.info("images_wikimedia: %r", query)
    try:
        r = WEB.get("https://commons.wikimedia.org/w/api.php",
                    params={"action":"query","list":"search","srsearch":query,
                            "srnamespace":"6","srlimit":max_results,"format":"json"},
                    headers={"User-Agent":"BaleBot/1.0"}, timeout=15)
        log.debug("wikimedia_search: status=%d", r.status_code)
        results = []
        for p in r.json().get("query",{}).get("search",[]):
            ir = WEB.get("https://commons.wikimedia.org/w/api.php",
                         params={"action":"query","titles":p["title"],
                                 "prop":"imageinfo","iiprop":"url","format":"json"},
                         headers={"User-Agent":"BaleBot/1.0"}, timeout=10)
            log.debug("wikimedia_info: status=%d", ir.status_code)
            for pg in ir.json().get("query",{}).get("pages",{}).values():
                url = (pg.get("imageinfo") or [{}])[0].get("url","")
                if url and any(url.lower().endswith(e)
                               for e in (".jpg",".jpeg",".png",".webp")):
                    results.append({"url":url,"title":p.get("title",query)})
                    break
            if len(results) >= max_results: break
        log.info("images_wikimedia: %d results", len(results))
        return results
    except Exception as e:
        log.error("images_wikimedia: %s", e, exc_info=True)
        return []

# ─── Misc ─────────────────────────────────────────────────────────────────────
def translate_text(text: str, target: str, source="auto") -> str:
    log.info("translate_text: target=%s len=%d", target, len(text))
    import html as html_mod
    has_fa = bool(re.search(r'[\u0600-\u06FF]', text))
    if source == "auto":
        source = "fa" if has_fa else "en"
    if source == target:
        return text
    MAX = 490
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur)+len(line)+1 <= MAX:
            cur = (cur+"\n"+line).lstrip("\n")
        else:
            if cur: chunks.append(cur)
            while len(line) > MAX:
                chunks.append(line[:MAX]); line = line[MAX:]
            cur = line
    if cur: chunks.append(cur)
    parts = []
    for chunk in chunks:
        try:
            r = WEB.get("https://api.mymemory.translated.net/get",
                        params={"q": chunk, "langpair": f"{source}|{target}"},
                        timeout=20)
            log.debug("translate chunk: status=%d", r.status_code)
            t = r.json()["responseData"]["translatedText"]
            parts.append(html_mod.unescape(t))
        except Exception as e:
            log.error("translate chunk: %s", e)
            parts.append(chunk)
        time.sleep(0.2)
    return "\n".join(parts)

def ocr_image(img_bytes: bytes) -> str:
    log.info("ocr_image: %d bytes", len(img_bytes))
    try:
        img = Image.open(io.BytesIO(img_bytes))
        text = pytesseract.image_to_string(img, lang="fas+eng")
        log.info("ocr_image: extracted %d chars", len(text))
        return text.strip() or "(متنی یافت نشد)"
    except Exception as e:
        log.error("ocr_image: %s", e, exc_info=True)
        return "❌ خطا در پردازش تصویر."

def ocr_to_pdf(text: str) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    pdf = FPDF(); pdf.set_margins(15,15,15); pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in text.split("\n"):
        safe = line.encode("latin-1", errors="replace").decode("latin-1")
        pdf.cell(0, 8, text=safe, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())

def currency_convert(amount: float, frm: str, to: str) -> Optional[str]:
    log.info("currency_convert: %s %s->%s", amount, frm, to)
    try:
        r = WEB.get("https://api.frankfurter.app/latest",
                    params={"from": frm.upper(), "to": to.upper()}, timeout=15)
        log.debug("frankfurter: status=%d", r.status_code)
        if r.status_code == 200:
            rate = r.json().get("rates",{}).get(to.upper())
            if rate:
                return f"{amount:,.2f} {frm.upper()} = {amount*rate:,.4f} {to.upper()}"
    except Exception as e:
        log.error("currency_convert: %s", e)
    return None

def ip_lookup(target: str) -> str:
    log.info("ip_lookup: %s", target)
    try:
        import socket
        ip = target.strip()
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            ip = socket.gethostbyname(ip)
        r = WEB.get(f"https://ipapi.co/{ip}/json/",
                    headers={"User-Agent":"BaleBot/1.0"}, timeout=15)
        log.debug("ipapi: status=%d", r.status_code)
        d = r.json()
        if d.get("error"):
            return "❌ اطلاعاتی یافت نشد."
        return (f"🌐 *اطلاعات IP: {ip}*\n\n"
                f"🏳 کشور: {d.get('country_name','—')} ({d.get('country_code','')})\n"
                f"🏙 شهر: {d.get('city','—')} / {d.get('region','—')}\n"
                f"📡 اپراتور: {d.get('org','—')}\n"
                f"🕐 منطقه زمانی: {d.get('timezone','—')}\n"
                f"📍 مختصات: {d.get('latitude','—')}, {d.get('longitude','—')}")
    except Exception as e:
        log.error("ip_lookup: %s", e, exc_info=True)
        return "❌ خطا در جستجوی IP."

def shorten_url(url: str) -> str:
    log.info("shorten_url: %s", url)
    try:
        r = WEB.get(f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}",
                    timeout=15)
        log.debug("tinyurl: status=%d resp=%s", r.status_code, r.text[:60])
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text.strip()
        return "❌ خطا در کوتاه‌سازی."
    except Exception as e:
        log.error("shorten_url: %s", e)
        return "❌ خطا در کوتاه‌سازی."

def expand_url(url: str) -> str:
    log.info("expand_url: %s", url)
    try:
        r = WEB.head(url, allow_redirects=True, timeout=15)
        return f"🔗 لینک نهایی:\n{r.url}\n\n_(ریدایرکت‌ها: {len(r.history)})_"
    except Exception as e:
        log.error("expand_url: %s", e)
        return "❌ خطا در باز کردن لینک."

def paste_text(content: str) -> Optional[str]:
    log.info("paste_text: %d chars", len(content))
    try:
        r = WEB.post("https://paste.rs/", data=content.encode("utf-8"),
                     headers={"Content-Type":"text/plain"}, timeout=15)
        log.debug("paste.rs: status=%d resp=%s", r.status_code, r.text[:60])
        if r.status_code in (200,201) and r.text.startswith("http"):
            return r.text.strip()
        return None
    except Exception as e:
        log.error("paste_text: %s", e)
        return None

def generate_qr(text: str) -> Optional[bytes]:
    log.info("generate_qr: %r", text[:40])
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(text); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        return buf.read()
    except ImportError:
        r = WEB.get(f"https://api.qrserver.com/v1/create-qr-code/?size=300x300"
                    f"&data={urllib.parse.quote(text)}", timeout=15)
        return r.content if r.status_code == 200 else None
    except Exception as e:
        log.error("generate_qr: %s", e)
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# USER STATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def init_user(cid: int):
    if cid not in user_stats:
        user_stats[cid] = {"requests":0,"joined":datetime.now().strftime("%Y-%m-%d"),
                           "searches":0,"downloads":0,"translations":0,"ocr":0}

def bump(cid: int, key="requests"):
    init_user(cid)
    user_stats[cid]["requests"] = user_stats[cid].get("requests",0)+1
    user_stats[cid][key]        = user_stats[cid].get(key,0)+1

def get_state(cid: int) -> dict:
    return user_state.get(cid, {})

def set_state(cid: int, **kw):
    user_state[cid] = kw

def clear_state(cid: int):
    user_state[cid] = {"mode": None}

# ═══════════════════════════════════════════════════════════════════════════════
# STATIC TEXTS
# ═══════════════════════════════════════════════════════════════════════════════
HELP_TEXT = """❓ *راهنمای دستیار وب*

🔎 *جستجو در وب* — نتایج به‌صورت دکمه، کلیک برای باز کردن
🌐 *مشاهده سایت* — اسکرین‌شات + دکمه‌های متن / HTML / ZIP / PDF
📚 *مقاله علمی* — Google Scholar با صفحه‌بندی
📖 *ویکی‌پدیا* — جستجو + خواندن مقاله کامل
📺 *یوتیوب* — جستجو (نتایج قابل کلیک) یا دانلود ویدیو
🎵 *موسیقی MP3* — جستجو و دانلود MP3
🖼 *دانلود عکس* — Bing / Pinterest / Pixabay / Wikimedia
🐙 *GitHub* — جستجوی مخازن / دانلود ZIP / آخرین Release
🌐 *ترجمه* — ۶ زبان، متن طولانی
🖼 *OCR* — استخراج متن از عکس + PDF
💱 *تبدیل ارز* — مثال: `100 USD to IRR`
🌐 *IP/دامنه* — اطلاعات موقعیت
🔗 *کوتاه‌سازی لینک* — TinyURL
📱 *QR کد* — از هر متن یا لینک
📋 *اشتراک متن* — paste.rs
🔒 *حریم خصوصی* — توضیح کامل"""

PRIVACY_TEXT = """🔒 *حریم خصوصی دستیار وب*

این ربات هیچ پیام، جستجو یا فایلی از شما را *ذخیره نمی‌کند*.

📌 *چه چیزی پردازش می‌شود؟*
• پیام‌های شما فقط برای اجرای همان درخواست استفاده می‌شوند.
• نتایج جستجو به‌صورت موقت در حافظه نگه داشته می‌شوند تا دکمه‌ها کار کنند؛ پس از مدتی پاک می‌شوند.
• هیچ پایگاه‌داده‌ای نداریم.

🌐 *سرویس‌های خارجی*
ربات برای انجام کارها به سرویس‌هایی مثل DuckDuckGo، Wikipedia، YouTube، GitHub و ... درخواست می‌فرستد. این سرویس‌ها سیاست حریم خصوصی خودشان را دارند.

🛡 *امنیت*
• ربات روی سرور اختصاصی اجرا می‌شود.
• توکن ربات محرمانه است و در کد قرار نمی‌گیرد.

📩 *سوال دارید؟*
با مدیر ربات در تماس باشید."""

# ═══════════════════════════════════════════════════════════════════════════════
# URL KEY CACHE  (for site-view button callbacks)
# ═══════════════════════════════════════════════════════════════════════════════
url_cache: dict[str, str] = {}   # short_key → full_url

def store_url(url: str) -> str:
    key = hashlib.md5(url.encode()).hexdigest()[:10]
    url_cache[key] = url
    return key

def get_url(key: str) -> Optional[str]:
    return url_cache.get(key)

# ═══════════════════════════════════════════════════════════════════════════════
# DO_ HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════
def do_search(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = web_search(query, 10, page)
    key = make_cache_key("ws", query, page)
    cache_set(key, results)
    set_state(cid, mode="search", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ نتیجه‌ای یافت نشد.", reply_markup=home_kb())
        return
    text = f"🔎 *نتایج جستجو:* _{query}_  (صفحه {page+1})\nروی هر نتیجه کلیک کنید:"
    kb = search_results_kb(results, key, page, len(results)==10, "ws")
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_scholar(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = scholar_search(query, page)
    key = make_cache_key("sc", query, page)
    cache_set(key, results)
    set_state(cid, mode="scholar", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ مقاله‌ای یافت نشد.", reply_markup=home_kb())
        return
    text = f"📚 *Google Scholar:* _{query}_  (صفحه {page+1})"
    kb = search_results_kb(results, key, page, len(results)>=8, "sc")
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_wiki_search(cid: int, query: str):
    bump(cid, "searches")
    chat_action(cid)
    results = wikipedia_search(query, "fa")
    lang = "fa"
    if not results:
        results = wikipedia_search(query, "en")
        lang = "en"
    if not results:
        send_message(cid, "❌ مقاله‌ای در ویکی‌پدیا یافت نشد.", reply_markup=home_kb())
        return
    key = make_cache_key("wk", query, 0)
    cache_set(key, results)
    set_state(cid, mode="wiki", last_query=query, cache_key=key, wiki_lang=lang)
    text = f"📖 *ویکی‌پدیا:* _{query}_\nروی مقاله کلیک کنید:"
    kb = wiki_result_kb(results, key, lang)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_wiki_article(cid: int, title: str, lang: str):
    bump(cid, "searches")
    chat_action(cid)
    send_message(cid, f"⏳ در حال دریافت مقاله _{title}_…", parse_mode="Markdown")
    text = wikipedia_article(title, lang)
    if not text and lang == "fa":
        text = wikipedia_article(title, "en")
    if not text:
        send_message(cid, "❌ مقاله یافت نشد.", reply_markup=home_kb())
        return
    send_message(cid, f"📖 *{title}*\n\n{text[:3500]}", parse_mode="Markdown")
    if len(text) > 3500:
        send_document(cid, text.encode("utf-8"), f"{title[:40]}.txt",
                      caption="📄 متن کامل مقاله")
    send_message(cid, "✅", reply_markup=home_kb())
    clear_state(cid)

def do_open_url(cid: int, url: str):
    bump(cid, "downloads")
    if not url.startswith("http"):
        url = "https://" + url
    url_key = store_url(url)
    send_message(cid, f"⏳ در حال گرفتن اسکرین‌شات از:\n{url}")
    chat_action(cid, "upload_photo")
    ss = screenshot_page(url)
    if ss:
        short_url = url[:60] + ("…" if len(url)>60 else "")
        send_photo(cid, ss, caption=f"🌐 {short_url}",
                   reply_markup=site_view_kb(url_key))
    else:
        send_message(cid, "⚠️ اسکرین‌شات ممکن نبود. از دکمه‌های زیر استفاده کنید:",
                     reply_markup=site_view_kb(url_key))
    clear_state(cid)

def do_youtube_search_cmd(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = youtube_search(query, 8)
    key = make_cache_key("yt", query, page)
    cache_set(key, results)
    set_state(cid, mode="yt_search", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ ویدیویی یافت نشد.", reply_markup=home_kb())
        return
    text = f"📺 *یوتیوب:* _{query}_\nروی ویدیو کلیک کنید تا دانلود شود:"
    kb = yt_results_kb(results, key, page, len(results)==8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_youtube_dl(cid: int, url: str):
    bump(cid, "downloads")
    if not ("youtu.be" in url or "youtube.com" in url):
        send_message(cid, "❌ لینک یوتیوب معتبر وارد کنید.", reply_markup=home_kb())
        return
    send_message(cid, "⏳ در حال دانلود ویدیو… (ممکن است چند دقیقه طول بکشد)")
    chat_action(cid, "upload_video")
    result = youtube_download(url, audio_only=False)
    _finish_video(cid, result)

def _finish_video(cid: int, result):
    if not result:
        send_message(cid,
                     "❌ دانلود ناموفق بود.\n"
                     "• اجرا کنید: `yt-dlp -U`\n"
                     "• یا یک ویدیو دیگر امتحان کنید.",
                     parse_mode="Markdown", reply_markup=home_kb())
        return
    data, fname = result
    fname = Path(fname).name
    size_mb = len(data)/1024/1024
    log.info("_finish_video: %s  %.1fMB", fname, size_mb)
    # smart_send handles chunking automatically — no size limit needed here
    if smart_send(cid, data, fname, caption=f"📺 {fname[:60]}", media_type="video"):
        send_message(cid, f"✅ ارسال شد ({size_mb:.1f}MB).", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق بود.", reply_markup=home_kb())
    clear_state(cid)

def do_music(cid: int, query: str):
    bump(cid, "downloads")
    send_message(cid, f"⏳ در حال جستجو و دانلود MP3: _{query}_…", parse_mode="Markdown")
    chat_action(cid, "record_voice")
    result = youtube_download(f"ytsearch1:{query}", audio_only=True)
    if not result:
        send_message(cid, "❌ دانلود ناموفق بود.", reply_markup=home_kb())
        return
    data, fname = result
    fname = Path(fname).name
    if len(data) > MAX_FILE_SIZE:
        send_message(cid, "❌ حجم فایل زیاد است.", reply_markup=home_kb())
        return
    if not fname.lower().endswith(".mp3"):
        fname = re.sub(r"\.[^.]+$", ".mp3", fname)
    if send_audio(cid, data, fname, caption=f"🎵 {query}"):
        send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق.", reply_markup=home_kb())
    clear_state(cid)

def do_images(cid: int, query: str, source: str, page=0):
    bump(cid, "searches")
    chat_action(cid, "upload_photo")
    source_names = {"bing":"🖼 Bing","pinterest":"📌 پینترست",
                    "pexels":"📷 Pixabay","wiki":"🎨 Wikimedia"}
    send_message(cid, f"⏳ جستجوی عکس [{source_names.get(source,source)}]: _{query}_…",
                 parse_mode="Markdown")
    offset = page * 6
    fn_map = {"bing": images_bing, "pinterest": images_pinterest,
              "pexels": images_pexels, "wiki": images_wikimedia}
    fn = fn_map.get(source, images_bing)
    results = fn(query, max_results=offset+8)
    page_results = results[offset:offset+6]
    key = make_cache_key(f"img_{source}", query, page)
    cache_set(key, results)
    set_state(cid, mode=f"img_{source}", last_query=query, page=page,
              img_source=source, cache_key=key)
    if not page_results:
        send_message(cid, "❌ تصویری یافت نشد.", reply_markup=home_kb())
        return
    sent = 0
    for r in page_results[:6]:
        try:
            img_bytes = r.get("_bytes") or download_bytes(r["url"], MAX_IMAGE_SIZE)
            if img_bytes and len(img_bytes) > 1000:
                if send_photo(cid, img_bytes, caption=r.get("title","")[:80]):
                    sent += 1
            time.sleep(0.3)
        except Exception as ex:
            log.error("do_images send: %s", ex)
    kb = images_more_kb(key, page, source)
    send_message(cid, f"✅ {sent} عکس ارسال شد.", reply_markup=kb)

def do_github_search(cid: int, query: str, page=0):
    bump(cid, "searches")
    chat_action(cid)
    results = github_search_repos(query, page)
    key = make_cache_key("gh", query, page)
    cache_set(key, results)
    set_state(cid, mode="gh_search", last_query=query, page=page, cache_key=key)
    if not results:
        send_message(cid, "❌ مخزنی یافت نشد.", reply_markup=home_kb())
        return
    text = f"🐙 *GitHub:* _{query}_  (صفحه {page+1})\nروی مخزن کلیک کنید:"
    kb = gh_repo_kb(results, key, page, len(results)==8)
    send_message(cid, text, parse_mode="Markdown", reply_markup=kb)

def do_github_zip(cid: int, url: str):
    bump(cid, "downloads")
    send_message(cid, "⏳ در حال دانلود ZIP مخزن…")
    data = github_zip(url)
    if not data:
        send_message(cid, "❌ دانلود ناموفق.", reply_markup=home_kb())
        return
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git|/|$)", url)
    slug = m.group(1).replace("/","_") if m else "repo"
    if send_document(cid, data, f"{slug}.zip", caption=f"📥 {url[:60]}"):
        send_message(cid, "✅ ZIP ارسال شد.", reply_markup=home_kb())
    else:
        send_message(cid, "❌ ارسال ناموفق.", reply_markup=home_kb())
    clear_state(cid)

def do_github_release(cid: int, repo_full: str):
    bump(cid, "downloads")
    chat_action(cid)
    rel = github_latest_release(repo_full)
    if not rel:
        send_message(cid, "❌ هیچ Release‌ای یافت نشد.", reply_markup=home_kb())
        return
    tag = rel.get("tag_name","")
    body = rel.get("body","")[:500]
    assets = rel.get("assets",[])
    lines = [f"📦 *آخرین Release: {repo_full}*",
             f"🏷 نسخه: `{tag}`",
             f"📅 تاریخ: {rel.get('published_at','')[:10]}",
             f"\n{body}" if body else ""]
    if assets:
        lines.append("\n*فایل‌های قابل دانلود:*")
        for i, a in enumerate(assets[:8]):
            size_mb = a.get("size",0)//1024//1024
            lines.append(f"{i+1}. {a['name']} ({size_mb}MB)")
    # Build asset download buttons
    rows = []
    for i, a in enumerate(assets[:6]):
        safe_repo = repo_full.replace("/","__")
        rows.append([{"text": f"⬇️ {a['name'][:40]}",
                       "callback_data": f"ghrel_dl_{safe_repo}_{i}"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    # cache assets
    akey = f"ghrel_{repo_full.replace('/','__')}"
    cache_set(akey, assets)
    send_message(cid, "\n".join(lines), parse_mode="Markdown",
                 reply_markup={"inline_keyboard": rows})

def do_ocr_photo(cid: int, photos: list, reply_id=None):
    bump(cid, "ocr")
    chat_action(cid)
    photo = sorted(photos, key=lambda p: p.get("file_size",0))[-1]
    if photo.get("file_size",0) > MAX_OCR_SIZE:
        send_message(cid, "❌ حجم عکس بیشتر از ۵MB است.")
        return
    url = get_file_url(photo["file_id"])
    if not url:
        send_message(cid, "❌ خطا در دریافت فایل."); return
    data = download_bytes(url, MAX_OCR_SIZE)
    if not data:
        send_message(cid, "❌ خطا در دانلود عکس."); return
    send_message(cid, "⏳ در حال پردازش تصویر…")
    extracted = ocr_image(data)
    send_message(cid, f"📝 *متن استخراج شده:*\n\n{extracted[:3500]}",
                 parse_mode="Markdown", reply_to_message_id=reply_id)
    try:
        pdf = ocr_to_pdf(extracted)
        send_document(cid, pdf, "ocr_result.pdf", caption="📑 متن OCR به‌صورت PDF")
    except Exception as e:
        log.error("ocr_to_pdf: %s", e)
    clear_state(cid)
    send_message(cid, "✅ OCR انجام شد.", reply_markup=home_kb())

def do_translate(cid: int, text: str, lang: str):
    bump(cid, "translations")
    chat_action(cid)
    result = translate_text(text, lang)
    send_message(cid, f"🌐 *ترجمه:*\n\n{result}",
                 parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_currency(cid: int, text: str):
    m = re.match(r"([\d,\.]+)\s+([A-Za-z]{3})\s+(?:to\s+)?([A-Za-z]{3})",
                 text.strip(), re.IGNORECASE)
    if not m:
        send_message(cid, "❌ فرمت اشتباه. مثال: `100 USD to IRR`",
                     parse_mode="Markdown", reply_markup=home_kb())
        return
    amount, frm, to = m.groups()
    amount = float(amount.replace(",",""))
    result = currency_convert(amount, frm, to)
    send_message(cid, f"💱 *{result}*" if result else "❌ خطا در تبدیل.",
                 parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_ip_lookup(cid: int, target: str):
    chat_action(cid)
    result = ip_lookup(target.strip())
    send_message(cid, result, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_shorten(cid: int, url: str):
    if not url.startswith("http"): url = "https://"+url
    result = shorten_url(url)
    send_message(cid, f"🔗 {result}", reply_markup=home_kb())
    clear_state(cid)

def do_expand(cid: int, url: str):
    if not url.startswith("http"): url = "https://"+url
    result = expand_url(url)
    send_message(cid, result, parse_mode="Markdown", reply_markup=home_kb())
    clear_state(cid)

def do_paste(cid: int, text: str):
    url = paste_text(text)
    if url:
        send_message(cid, f"📋 آپلود شد:\n{url}", reply_markup=home_kb())
    else:
        send_message(cid, "❌ خطا در آپلود.", reply_markup=home_kb())
    clear_state(cid)

def do_qr(cid: int, text: str):
    chat_action(cid, "upload_photo")
    qr = generate_qr(text)
    if qr:
        send_photo(cid, qr, caption=f"📱 QR: {text[:60]}")
    else:
        send_message(cid, "❌ خطا در ساخت QR.")
    send_message(cid, "✅", reply_markup=home_kb())
    clear_state(cid)

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
def handle_message(msg: dict):
    cid    = msg["chat"]["id"]
    text   = msg.get("text","")
    photos = msg.get("photo")
    init_user(cid)
    log.info("MSG cid=%d text=%r has_photo=%s", cid, text[:60], bool(photos))

    # Commands
    if text.startswith("/start"):
        clear_state(cid)
        send_message(cid,
            "👋 سلام! به *دستیار وب* خوش آمدید.\n"
            "یکی از گزینه‌های زیر را انتخاب کنید:",
            parse_mode="Markdown", reply_markup=main_menu_kb())
        return
    if text.startswith("/help"):
        send_message(cid, HELP_TEXT, parse_mode="Markdown"); return
    if text.startswith("/cancel"):
        clear_state(cid)
        send_message(cid, "✅ لغو شد.", reply_markup=main_menu_kb()); return
    if text.startswith("/ocr"):
        reply = msg.get("reply_to_message")
        if reply and reply.get("photo"):
            do_ocr_photo(cid, reply["photo"], msg["message_id"]); return
        set_state(cid, mode="ocr")
        send_message(cid, "🖼 عکس حاوی متن ارسال کنید:", reply_markup=cancel_kb()); return

    # Photo
    if photos:
        st = get_state(cid)
        if st.get("mode") == "ocr" or not st.get("mode"):
            do_ocr_photo(cid, photos, msg["message_id"]); return

    if not text: return

    st   = get_state(cid)
    mode = st.get("mode")

    dispatch = {
        "search":       lambda: do_search(cid, text),
        "scholar":      lambda: do_scholar(cid, text),
        "wiki":         lambda: do_wiki_search(cid, text),
        "wiki_article": lambda: do_wiki_article(cid, text, st.get("wiki_lang","fa")),
        "open":         lambda: do_open_url(cid, text),
        "yt_dl":        lambda: do_youtube_dl(cid, text),
        "yt_search":    lambda: do_youtube_search_cmd(cid, text),
        "music":        lambda: do_music(cid, text),
        "gh_search":    lambda: do_github_search(cid, text),
        "gh_zip":       lambda: do_github_zip(cid, text),
        "gh_release":   lambda: do_github_release(cid, text),
        "translate":    lambda: do_translate(cid, text, st.get("target_lang","en")),
        "currency":     lambda: do_currency(cid, text),
        "iplookup":     lambda: do_ip_lookup(cid, text),
        "shorten":      lambda: do_shorten(cid, text),
        "expand":       lambda: do_expand(cid, text),
        "paste":        lambda: do_paste(cid, text),
        "qr":           lambda: do_qr(cid, text),
        "ocr":          lambda: send_message(cid,"🖼 لطفاً عکس ارسال کنید.",reply_markup=cancel_kb()),
        "images_pick":  lambda: do_images(cid, st.get("last_query", text),
                                          st.get("img_source","bing")),
    }
    if mode in dispatch:
        dispatch[mode]()
    elif text.startswith("http"):
        do_open_url(cid, text)
    else:
        do_search(cid, text)

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
def handle_callback(cb: dict):
    cid  = cb["message"]["chat"]["id"]
    data = cb.get("data","")
    answer_cb(cb["id"])
    init_user(cid)
    log.info("CB cid=%d data=%r", cid, data)

    st = get_state(cid)

    # ── Home / Cancel ─────────────────────────────────────────────────────
    if data in ("home","cancel"):
        clear_state(cid)
        send_message(cid, "🏠 منوی اصلی:", reply_markup=main_menu_kb()); return

    if data == "help":
        send_message(cid, HELP_TEXT, parse_mode="Markdown"); return

    if data == "privacy":
        send_message(cid, PRIVACY_TEXT, parse_mode="Markdown",
                     reply_markup=home_kb()); return

    if data == "stats":
        s = user_stats.get(cid,{})
        txt = (f"📊 *آمار کاربری*\n\n"
               f"🗓 عضویت: {s.get('joined','—')}\n"
               f"📈 مجموع: {s.get('requests',0)}\n"
               f"🔎 جستجو: {s.get('searches',0)}\n"
               f"📥 دانلود: {s.get('downloads',0)}\n"
               f"🌐 ترجمه: {s.get('translations',0)}\n"
               f"🖼 OCR: {s.get('ocr',0)}")
        send_message(cid, txt, parse_mode="Markdown", reply_markup=home_kb()); return

    # ── Mode launchers ────────────────────────────────────────────────────
    mode_prompts = {
        "mode_search":    ("search",    "🔎 کلمه یا عبارت جستجو را بنویسید:"),
        "mode_open":      ("open",      "🌐 آدرس سایت را وارد کنید (https://…):"),
        "mode_scholar":   ("scholar",   "📚 عنوان یا کلمه‌کلیدی مقاله:"),
        "mode_wiki":      ("wiki",      "📖 موضوع ویکی‌پدیا:"),
        "mode_music":     ("music",     "🎵 نام آهنگ یا آرتیست:"),
        "mode_currency":  ("currency",  "💱 مثال: 100 USD to IRR"),
        "mode_iplookup":  ("iplookup",  "🌐 آدرس IP یا دامنه:"),
        "mode_shorten":   ("shorten",   "🔗 لینک بلند:"),
        "mode_expand":    ("expand",    "🔍 لینک کوتاه:"),
        "mode_paste":     ("paste",     "📋 متن مورد نظر را ارسال کنید:"),
        "mode_qr":        ("qr",        "📱 متن یا لینک برای QR کد:"),
    }
    if data in mode_prompts:
        mode, prompt = mode_prompts[data]
        set_state(cid, mode=mode)
        send_message(cid, prompt, reply_markup=cancel_kb()); return

    if data == "mode_translate":
        set_state(cid, mode="translate_lang")
        send_message(cid, "🌐 زبان مقصد را انتخاب کنید:", reply_markup=translate_kb()); return

    if data.startswith("trlang_"):
        lang = data.split("_",1)[1]
        set_state(cid, mode="translate", target_lang=lang)
        send_message(cid, f"✅ زبان انتخاب شد. متن را بفرستید:", reply_markup=cancel_kb()); return

    if data == "mode_ocr":
        set_state(cid, mode="ocr")
        send_message(cid, "🖼 عکس حاوی متن ارسال کنید:", reply_markup=cancel_kb()); return

    # ── YouTube ───────────────────────────────────────────────────────────
    if data == "mode_youtube":
        send_message(cid, "📺 یوتیوب:", reply_markup=youtube_action_kb()); return
    if data == "yt_video":
        set_state(cid, mode="yt_dl")
        send_message(cid, "📥 لینک ویدیو یوتیوب:", reply_markup=cancel_kb()); return
    if data == "yt_search":
        set_state(cid, mode="yt_search")
        send_message(cid, "🔍 کلمه جستجو:", reply_markup=cancel_kb()); return

    # YouTube result click → download
    m = re.match(r"yt_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده. دوباره جستجو کنید."); return
        vid = results[idx]
        send_message(cid, f"⏳ دانلود: _{vid['title']}_…", parse_mode="Markdown")
        chat_action(cid, "upload_video")
        result = youtube_download(vid["url"], audio_only=False)
        _finish_video(cid, result); return

    m = re.match(r"yt_page_(\d+)$", data)
    if m:
        page = int(m.group(1))
        query = st.get("last_query","")
        if query: do_youtube_search_cmd(cid, query, page)
        return

    # ── GitHub ────────────────────────────────────────────────────────────
    if data == "mode_github":
        send_message(cid, "🐙 GitHub:", reply_markup=github_action_kb()); return
    if data == "gh_search":
        set_state(cid, mode="gh_search")
        send_message(cid, "🔍 نام مخزن یا کلمه کلیدی:", reply_markup=cancel_kb()); return
    if data == "gh_zip":
        set_state(cid, mode="gh_zip")
        send_message(cid, "📥 لینک مخزن GitHub:", reply_markup=cancel_kb()); return
    if data == "gh_release":
        set_state(cid, mode="gh_release")
        send_message(cid, "📦 نام کامل مخزن (مثال: microsoft/vscode):",
                     reply_markup=cancel_kb()); return

    # GitHub repo result click
    m = re.match(r"gh_res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی شده."); return
        repo = results[idx]
        full = repo.get("full_name","")
        desc = repo.get("description") or ""
        txt = (f"🐙 *{full}*\n"
               f"⭐ {repo.get('stargazers_count',0)} | "
               f"🍴 {repo.get('forks_count',0)} | "
               f"📝 {repo.get('language','—')}\n\n"
               f"{desc[:300]}\n\n"
               f"🔗 {repo.get('html_url','')}")
        send_message(cid, txt, parse_mode="Markdown",
                     reply_markup=gh_repo_action_kb(full)); return

    m = re.match(r"gh_page_(\d+)$", data)
    if m:
        page = int(m.group(1))
        query = st.get("last_query","")
        if query: do_github_search(cid, query, page)
        return

    # GitHub repo actions
    m = re.match(r"ghact_zip_(.+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        do_github_zip(cid, f"https://github.com/{repo}"); return

    m = re.match(r"ghact_rel_(.+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        do_github_release(cid, repo); return

    m = re.match(r"ghact_info_(.+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        info = github_repo_info(repo)
        if info:
            txt = (f"📋 *{repo}*\n"
                   f"⭐ Stars: {info.get('stargazers_count',0)}\n"
                   f"🍴 Forks: {info.get('forks_count',0)}\n"
                   f"👀 Watchers: {info.get('watchers_count',0)}\n"
                   f"📝 Language: {info.get('language','—')}\n"
                   f"📅 Created: {info.get('created_at','')[:10]}\n"
                   f"🔄 Updated: {info.get('updated_at','')[:10]}\n"
                   f"📄 License: {(info.get('license') or {}).get('name','—')}\n"
                   f"🌐 {info.get('homepage') or info.get('html_url','')}")
            send_message(cid, txt, parse_mode="Markdown", reply_markup=home_kb())
        else:
            send_message(cid, "❌ اطلاعاتی یافت نشد.", reply_markup=home_kb())
        return

    # GitHub release asset download
    m = re.match(r"ghrel_dl_(.+)_(\d+)$", data)
    if m:
        repo = m.group(1).replace("__","/")
        idx  = int(m.group(2))
        akey = f"ghrel_{m.group(1)}"
        assets = cache_get(akey)
        if not assets or idx >= len(assets):
            send_message(cid, "❌ نتیجه منقضی."); return
        asset = assets[idx]
        url = asset.get("browser_download_url","")
        size_mb = asset.get("size",0)/1024/1024
        send_message(cid, f"⏳ دانلود: {asset['name']} ({size_mb:.1f}MB)…")
        chat_action(cid, "upload_document")
        d = download_bytes(url, 500 * 1024 * 1024)  # allow up to 500MB download
        if d:
            smart_send(cid, d, asset["name"], caption=f"📦 {repo}")
            send_message(cid, "✅ ارسال شد.", reply_markup=home_kb())
        else:
            send_message(cid, "❌ دانلود ناموفق.", reply_markup=home_kb())
        return

    # ── Images ────────────────────────────────────────────────────────────
    if data == "mode_images":
        set_state(cid, mode="images_query")
        send_message(cid, "🖼 کلمه کلیدی عکس مورد نظر را بنویسید:",
                     reply_markup=cancel_kb()); return

    img_src_map = {"img_src_bing":"bing","img_src_pinterest":"pinterest",
                   "img_src_pexels":"pexels","img_src_wiki":"wiki"}
    if data in img_src_map:
        src = img_src_map[data]
        query = st.get("last_query","")
        if not query:
            send_message(cid, "❌ ابتدا کلمه کلیدی وارد کنید.", reply_markup=cancel_kb()); return
        set_state(cid, mode=f"img_{src}", last_query=query, img_source=src, page=0)
        do_images(cid, query, src, 0); return

    # "دانلود بیشتر" button
    m = re.match(r"img_more_(\w+)_(\w+)_(\d+)$", data)
    if m:
        source, key, page = m.group(1), m.group(2), int(m.group(3))
        query = st.get("last_query","")
        do_images(cid, query, source, page); return

    # ── Site view buttons ─────────────────────────────────────────────────
    m = re.match(r"site_(text|html|zip|pdf)_(\w+)$", data)
    if m:
        action, url_key = m.group(1), m.group(2)
        url = get_url(url_key)
        if not url:
            send_message(cid, "❌ لینک منقضی شده.", reply_markup=home_kb()); return
        chat_action(cid, "upload_document")
        if action == "text":
            txt = page_to_text(url)
            send_message(cid, f"📝 متن صفحه:\n\n{txt[:3500]}", reply_markup=home_kb())
            if len(txt) > 3500:
                send_document(cid, txt.encode("utf-8"), "page_text.txt")
        elif action == "html":
            html = fetch_page(url)
            if html: send_document(cid, html, "page.html", caption=f"🌐 {url[:60]}")
            else: send_message(cid, "❌ خطا در دریافت HTML.", reply_markup=home_kb())
        elif action == "zip":
            send_message(cid, "⏳ در حال ساخت ZIP…")
            zdata = page_to_zip(url)
            if zdata:
                domain = urllib.parse.urlparse(url).netloc.replace(".","_")
                send_document(cid, zdata, f"{domain}_offline.zip")
            else: send_message(cid, "❌ خطا در ZIP.", reply_markup=home_kb())
        elif action == "pdf":
            send_message(cid, "⏳ در حال تولید PDF…")
            pdf = page_to_pdf(url)
            if pdf:
                ext = "pdf" if pdf[:4]==b"%PDF" else "html"
                send_document(cid, pdf, f"page.{ext}", caption=f"📑 {url[:60]}")
            else: send_message(cid, "❌ خطا در PDF.", reply_markup=home_kb())
        send_message(cid, "✅", reply_markup=site_view_kb(url_key)); return

    # ── Search result click (web, scholar) ────────────────────────────────
    m = re.match(r"res_(\w+)_(\d+)$", data)
    if m:
        key, idx = m.group(1), int(m.group(2))
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی."); return
        r = results[idx]
        # Show detail with options
        link = r.get("link") or r.get("url","")
        snippet = r.get("snippet") or r.get("meta","")
        txt = (f"🔗 *{r.get('title','')}*\n\n"
               f"{snippet[:400]}\n\n"
               f"[باز کردن لینک]({link})")
        url_key = store_url(link)
        kb = {"inline_keyboard": [
            [{"text": "🌐 مشاهده سایت",  "callback_data": f"site_ss_{url_key}"},
             {"text": "📝 متن صفحه",     "callback_data": f"site_text_{url_key}"}],
            [{"text": "🌐 HTML",          "callback_data": f"site_html_{url_key}"},
             {"text": "🔙 برگشت به نتایج","callback_data": "results_back"}],
        ]}
        send_message(cid, txt, parse_mode="Markdown", reply_markup=kb); return

    m = re.match(r"site_ss_(\w+)$", data)
    if m:
        url_key = m.group(1)
        url = get_url(url_key)
        if not url: send_message(cid, "❌ لینک منقضی."); return
        send_message(cid, f"⏳ گرفتن اسکرین‌شات…")
        ss = screenshot_page(url)
        if ss: send_photo(cid, ss, caption=url[:60], reply_markup=site_view_kb(url_key))
        else: send_message(cid, "❌ اسکرین‌شات ممکن نبود.", reply_markup=site_view_kb(url_key))
        return

    if data == "results_back":
        query = st.get("last_query","")
        page  = st.get("page", 0)
        mode  = st.get("mode","")
        if "scholar" in mode: do_scholar(cid, query, page)
        elif query: do_search(cid, query, page)
        else: send_message(cid, "🏠", reply_markup=main_menu_kb())
        return

    # ── Pagination (web search, scholar) ──────────────────────────────────
    pm = re.match(r"page_(ws|sc)_(\d+)$", data)
    if pm:
        kind, page = pm.group(1), int(pm.group(2))
        query = st.get("last_query","")
        if not query: send_message(cid, "❌ جستجوی قبلی یافت نشد."); return
        if kind == "ws": do_search(cid, query, page)
        elif kind == "sc": do_scholar(cid, query, page)
        return

    # ── Wiki article click ────────────────────────────────────────────────
    m = re.match(r"wiki_art_(\w+)_(\d+)_(\w+)$", data)
    if m:
        key, idx, lang = m.group(1), int(m.group(2)), m.group(3)
        results = cache_get(key)
        if not results or idx >= len(results):
            send_message(cid, "❌ نتیجه منقضی."); return
        article = results[idx]
        do_wiki_article(cid, article["title"], lang); return

    # ── Images query entry ────────────────────────────────────────────────
    # (handled in handle_message when mode==images_query)
    log.warning("Unhandled callback: %r", data)

def handle_message_images_query(cid: int, text: str, st: dict):
    """Called from handle_message when mode==images_query."""
    set_state(cid, mode="images_source", last_query=text)
    send_message(cid, f"🖼 منبع عکس برای _{text}_ را انتخاب کنید:",
                 parse_mode="Markdown", reply_markup=image_source_kb())

# Patch handle_message dispatch for images_query
_orig_dispatch_key = "images_query"

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════
def handle_update(update: dict):
    if "message" in update:
        msg = update["message"]
        cid = msg["chat"]["id"]
        text = msg.get("text","")
        st = get_state(cid)
        # Special case: images_query mode
        if st.get("mode") == "images_query" and text and not text.startswith("/"):
            init_user(cid)
            handle_message_images_query(cid, text, st)
        else:
            handle_message(msg)
    elif "callback_query" in update:
        handle_callback(update["callback_query"])

def run():
    log.info("Bot starting — token ends with …%s", TOKEN[-6:] if len(TOKEN)>6 else "???")
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.critical("BALE_TOKEN not set! Run: export BALE_TOKEN=your_token")
        return
    offset = 0
    while True:
        try:
            resp = api("getUpdates", offset=offset, timeout=30)
            if not resp.get("ok"):
                log.warning("getUpdates not ok: %s", resp)
                time.sleep(5); continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    log.error("handle_update: %s", e, exc_info=True)
        except KeyboardInterrupt:
            log.info("Bot stopped."); break
        except Exception as e:
            log.error("Polling error: %s", e, exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    run()