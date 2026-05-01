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
                {"text": "📑 PDF از صفحه", "callback_data": "mode_pdf"},
            ],
            [
                {"text": "🗜 آرشیو ZIP آفلاین", "callback_data": "mode_zip"},
                {"text": "📥 دانلود مخزن GitHub", "callback_data": "mode_github"},
            ],
            [
                {"text": "🌐 ترجمه متن", "callback_data": "mode_translate"},
                {"text": "🖼 OCR (متن از عکس)", "callback_data": "mode_ocr"},
            ],
            [
                {"text": "📚 مقاله علمی", "callback_data": "mode_scholar"},
                {"text": "📺 یوتیوب", "callback_data": "mode_youtube"},
            ],
            [
                {"text": "🎵 دانلود موسیقی MP3", "callback_data": "mode_music"},
                {"text": "📌 پینترست", "callback_data": "mode_pinterest"},
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

def web_search(query: str, max_results: int = 10) -> list[dict]:
    """DuckDuckGo HTML scrape."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BaleBot/1.0)"}
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for a in soup.select("a.result__a")[:max_results]:
            href = a.get("href", "")
            # DuckDuckGo wraps URLs
            m = re.search(r"uddg=([^&]+)", href)
            link = urllib.parse.unquote(m.group(1)) if m else href
            results.append({"title": a.get_text(strip=True), "link": link})
        return results
    except Exception as e:
        log.error("web_search error: %s", e)
        return []


def search_to_html(query: str) -> bytes:
    results = web_search(query, 10)
    rows = ""
    for i, r in enumerate(results, 1):
        rows += f'<tr><td>{i}</td><td><a href="{r["link"]}" target="_blank">{r["title"]}</a></td></tr>\n'
    html = f"""<!DOCTYPE html>
<html dir="rtl" lang="fa">
<head><meta charset="utf-8"><title>نتایج: {query}</title>
<style>
  body{{font-family:Tahoma,sans-serif;padding:20px;background:#f9f9f9}}
  h2{{color:#333}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#4a90d9;color:#fff;padding:8px}}
  td{{padding:8px;border-bottom:1px solid #ddd}}
  a{{color:#1a0dab;text-decoration:none}}
  a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<h2>🔎 نتایج جستجو برای: {query}</h2>
<p>تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<table><tr><th>#</th><th>عنوان و لینک</th></tr>
{rows}
</table>
</body></html>"""
    return html.encode("utf-8")


def fetch_page(url: str) -> Optional[bytes]:
    """Fetch raw HTML of a page."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BaleBot/1.0)"}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.content
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
    """Translate using MyMemory API (free, no key needed)."""
    pair = f"{source}|{target}" if source != "auto" else f"en|{target}"
    # Detect if Persian to translate to English
    has_persian = bool(re.search(r'[\u0600-\u06FF]', text))
    if source == "auto":
        pair = f"{'fa' if has_persian else 'en'}|{target}"
    try:
        url = "https://api.mymemory.translated.net/get"
        r = requests.get(url, params={"q": text[:500], "langpair": pair}, timeout=15)
        data = r.json()
        return data["responseData"]["translatedText"]
    except Exception as e:
        log.error("translate error: %s", e)
        return "❌ خطا در ترجمه."


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


def ocr_to_pdf(text: str, original_filename: str = "ocr") -> bytes:
    """Wrap OCR text in a simple PDF."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    # Use built-in font (no Persian support without custom font, but keeps it simple)
    pdf.set_font("Helvetica", size=12)
    pdf.set_right_margin(10)
    pdf.set_left_margin(10)
    for line in text.split("\n"):
        try:
            pdf.cell(0, 8, txt=line, ln=True)
        except Exception:
            pdf.cell(0, 8, txt=line.encode("latin-1", "replace").decode("latin-1"), ln=True)
    return pdf.output(dest="S").encode("latin-1")


def scholar_search(query: str) -> list[dict]:
    """Search Google Scholar via scraping."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BaleBot/1.0)"}
    url = f"https://scholar.google.com/scholar?q={urllib.parse.quote(query)}&hl=fa&num=10"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".gs_ri")[:8]:
            title_tag = div.select_one(".gs_rt a")
            snippet_tag = div.select_one(".gs_rs")
            meta_tag = div.select_one(".gs_a")
            if not title_tag:
                continue
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


def youtube_download(url: str, audio_only: bool = False,
                     quality: str = "best[filesize<50M]") -> Optional[tuple[bytes, str]]:
    """Download YouTube video/audio. Returns (bytes, filename)."""
    with tempfile.TemporaryDirectory() as tmp:
        out_tpl = os.path.join(tmp, "%(title)s.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist", "-o", out_tpl, "--no-warnings"]
        if audio_only:
            cmd += ["-x", "--audio-format", "mp3",
                    "--audio-quality", "192K",
                    "-f", "bestaudio"]
        else:
            cmd += ["-f", "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<45M]/best"]
        cmd.append(url)
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=True)
            files = list(Path(tmp).glob("*"))
            if not files:
                return None
            f = files[0]
            data = f.read_bytes()
            return data, f.name
        except Exception as e:
            log.error("yt-dlp error: %s", e)
            return None


def pinterest_search(query: str) -> list[dict]:
    """Search Pinterest images."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BaleBot/1.0)",
        "Accept": "application/json",
    }
    url = f"https://www.pinterest.com/search/pins/?q={urllib.parse.quote(query)}&rs=typed"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        # Extract image URLs from JSON blobs in page source
        matches = re.findall(r'"orig":\{"url":"([^"]+)"', r.text)
        results = []
        seen = set()
        for img_url in matches[:12]:
            if img_url not in seen:
                seen.add(img_url)
                results.append({"url": img_url})
        return results
    except Exception as e:
        log.error("pinterest error: %s", e)
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Stats helpers
# ══════════════════════════════════════════════════════════════════════════════

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

🔎 *جستجو در وب*
کلمه یا عبارت مورد نظر را تایپ کنید — تا ۱۰ نتیجه با عنوان و لینک می‌گیرید.

📄 *نتایج HTML*
همان جستجو ولی به‌صورت فایل HTML که می‌توانید ذخیره کنید.

🌐 *باز کردن سایت*
آدرس URL وارد کنید تا محتوای صفحه دریافت شود.

📑 *PDF از صفحه*
آدرس URL وارد کنید — یک فایل HTML از صفحه می‌گیرید.

🗜 *ZIP آفلاین*
آدرس URL وارد کنید — صفحه + منابع آن در قالب ZIP.

📥 *GitHub*
لینک مخزن GitHub بدهید — ZIP کل مخزن دانلود می‌شود.

🌐 *ترجمه*
زبان مقصد را انتخاب کنید، سپس متن را ارسال کنید.

🖼 *OCR*
عکس حاوی متن ارسال کنید یا روی عکس /ocr بفرستید.

📚 *مقاله علمی*
عنوان یا کلمه‌کلیدی مقاله را جستجو کنید.

📺 *یوتیوب*
لینک ویدیو برای دانلود یا کلمه برای جستجو وارد کنید.

🎵 *موسیقی MP3*
نام آهنگ یا آرتیست — اولین نتیجه یوتیوب را MP3 دانلود می‌کند.

📌 *پینترست*
کلمه کلیدی — تصاویر از پینترست می‌گیرید."""


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


# ══════════════════════════════════════════════════════════════════════════════
# Feature handlers
# ══════════════════════════════════════════════════════════════════════════════

def do_search(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    results = web_search(query)
    if not results:
        send_message(chat_id, "❌ نتیجه‌ای یافت نشد.", reply_markup=main_menu_keyboard())
        return
    lines = [f"🔎 *نتایج جستجو برای:* {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['title']}]({r['link']})")
    send_message(chat_id, "\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_keyboard())


def do_html_search(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "upload_document")
    html_bytes = search_to_html(query)
    send_document(chat_id, html_bytes, f"search_{query[:20]}.html",
                  caption=f"📄 نتایج جستجو برای: {query}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ فایل HTML ارسال شد.", reply_markup=main_menu_keyboard())


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


def do_scholar(chat_id: int, query: str):
    bump(chat_id, "searches")
    send_chat_action(chat_id, "typing")
    results = scholar_search(query)
    if not results:
        send_message(chat_id, "❌ مقاله‌ای یافت نشد.", reply_markup=main_menu_keyboard())
        return
    lines = [f"📚 *نتایج Google Scholar:* {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['title']}]({r['link']})")
        if r.get("meta"):
            lines.append(f"   _{r['meta']}_")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:150]}...")
        lines.append("")
    send_message(chat_id, "\n".join(lines)[:4000], parse_mode="Markdown",
                 reply_markup=main_menu_keyboard())


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
    if "youtu" not in url:
        send_message(chat_id, "❌ لینک معتبر یوتیوب وارد کنید.", reply_markup=main_menu_keyboard())
        return
    send_message(chat_id, "⏳ در حال دانلود ویدیو (ممکن است چند دقیقه طول بکشد)...")
    send_chat_action(chat_id, "upload_video")
    result = youtube_download(url, audio_only=False)
    if not result:
        send_message(chat_id, "❌ خطا در دانلود ویدیو یا حجم آن زیاد است.", reply_markup=main_menu_keyboard())
        return
    data, fname = result
    if len(data) > MAX_FILE_SIZE:
        send_message(chat_id, "❌ حجم ویدیو بیشتر از ۵۰ مگابایت است.", reply_markup=main_menu_keyboard())
        return
    send_document(chat_id, data, fname, caption=f"📺 {fname[:80]}")
    user_state[chat_id] = {"mode": None}
    send_message(chat_id, "✅ ویدیو ارسال شد.", reply_markup=main_menu_keyboard())


def do_music(chat_id: int, query: str):
    bump(chat_id, "downloads")
    send_message(chat_id, "⏳ در حال جستجو و دانلود MP3...")
    send_chat_action(chat_id, "record_voice")
    result = youtube_download(f"ytsearch1:{query}", audio_only=True)
    if not result:
        send_message(chat_id, "❌ خطا در دانلود موسیقی.", reply_markup=main_menu_keyboard())
        return
    data, fname = result
    if len(data) > MAX_FILE_SIZE:
        send_message(chat_id, "❌ حجم فایل صوتی زیاد است.", reply_markup=main_menu_keyboard())
        return
    # Make sure filename is mp3
    if not fname.endswith(".mp3"):
        fname = fname.rsplit(".", 1)[0] + ".mp3"
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
