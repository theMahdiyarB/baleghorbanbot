# 🤖 بله قربان — Bale Web Assistant Bot

یک ربات جامع برای پیام‌رسان **بله** با قابلیت‌های متعدد جستجو، دانلود، شبکه‌های اجتماعی و بیشتر.

---

## ✨ قابلیت‌ها

| ابزار | توضیح |
|---|---|
| 🔎 **جستجو در وب** | تا ۱۰ نتیجه از DuckDuckGo — نتایج قابل کلیک با صفحه‌بندی |
| 🌐 **مشاهده سایت** | اسکرین‌شات ۱۹۲۰×۱۰۸۰ + دکمه‌های متن / HTML / ZIP / PDF |
| 📚 **مقاله علمی** | جستجوی Google Scholar با صفحه‌بندی و نتایج قابل کلیک |
| 📖 **ویکی‌پدیا** | جستجو + خواندن مقاله کامل (فارسی و انگلیسی) |
| 📺 **یوتیوب** | جستجو با تامبنیل، **انتخاب کیفیت ویدیو** (360p–1080p+)، **انتخاب زیرنویس**، دانلود صدا، ۳ استراتژی yt-dlp + Cobalt fallback |
| 🎵 **موسیقی MP3** | دانلود صوتی از YouTube Music / SoundCloud / Spotify |
| 🖼 **دانلود عکس** | Bing / Pinterest / Pixabay / Wikimedia با «دانلود بیشتر» |
| 🐙 **GitHub** | جستجوی مخازن + دانلود ZIP + دانلود Release |
| ✈️ **کانال تلگرام** | خواندن پیام‌های کانال عمومی (scrape) یا MTProto |
| 🐦 **توییتر / X** | تایم‌لاین کاربر + دانلود عکس خودکار + دانلود ویدیو |
| 📸 **اینستاگرام** | پست‌های پروفایل + دانلود عکس خودکار + دانلود ریل (۳ استراتژی: instaloader → yt-dlp → Cobalt API) |
| 🎵 **تیک‌تاک** | لیست ویدیوها با تامبنیل + دانلود مستقیم |
| 📱 **APK دانلود** | جستجو در Google Play + دانلود APK از APKPure / APKMirror / F-Droid / Aptoide |
| 📰 **اخبار RSS** | دریافت فید + کشف خودکار فید از سایت |
| 📚 **Z-Library** | جستجو و دانلود کتاب الکترونیکی |
| 🌐 **ترجمه** | ترجمه به فارسی، انگلیسی، عربی، آلمانی، فرانسوی، روسی |
| 🖼 **OCR** | استخراج متن از عکس + خروجی PDF |
| 🌐 **IP / دامنه** | موقعیت، اپراتور، منطقه زمانی |
| 🔒 **حریم خصوصی** | توضیح کامل نحوه عدم ذخیره داده |

### 📺 دانلود یوتیوب — جزئیات

فرآیند دانلود سه مرحله‌ای است:

1. **انتخاب کیفیت** — ربات فرمت‌های واقعی موجود برای ویدیو را بررسی کرده و به‌صورت دکمه نمایش می‌دهد (مثلاً 1080p 60fps / 720p / 480p / 360p). گزینه «فقط صدا (MP3)» هم همیشه موجود است.
2. **انتخاب زیرنویس** — اگر زیرنویس (دستی یا خودکار) موجود باشد، لیست زبان‌ها نمایش داده می‌شود. گزینه «بدون زیرنویس» هم موجود است.
3. **دانلود** — با فرمت دقیق انتخابی دانلود می‌شود.

**ترتیب استراتژی‌ها:**
- استراتژی ۱: probe + دانلود با ۵ player client مختلف (yt-dlp)
- استراتژی ۲: broad safety-net selector (yt-dlp)
- استراتژی ۳: Cobalt API — فقط اگر هر دو استراتژی yt-dlp شکست بخورند

### 🔗 تشخیص خودکار لینک
فقط لینک بفرستید — ربات خودکار تشخیص می‌دهد:
- `youtube.com` / `youtu.be` → انتخاب کیفیت + دانلود
- `tiktok.com` / `vm.tiktok.com` → دانلود ویدیو
- `twitter.com` / `x.com` → دانلود رسانه
- `instagram.com` → دانلود پست/ریل
- `t.me/…` → دانلود رسانه تلگرام
- سایر لینک‌ها → اسکرین‌شات + گزینه‌های بیشتر

### 📤 ارسال فایل هوشمند
- فایل‌های بزرگ به **قطعه‌های ۱۹MB** تقسیم می‌شوند
- پسوندهای غیر پشتیبانی‌شده در **ZIP** بسته‌بندی می‌شوند
- پیام ترکیب قطعه‌ها ارسال می‌شود: `cat file.part*of3.ext > file.ext`

---

## 🛠 نصب و راه‌اندازی

### ۱. پیش‌نیازهای سیستم

```bash
sudo apt-get update
sudo apt-get install -y \
  tesseract-ocr tesseract-ocr-fas tesseract-ocr-eng \
  ffmpeg wkhtmltopdf python3-pip python3-dev
```

### ۲. نصب کتابخانه‌های Python

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m playwright install-deps chromium
```

### ۳. متغیرهای محیطی (ضروری)

```bash
# توکن ربات بله (الزامی)
export BALE_TOKEN="توکن_ربات_شما"
```

### ۴. متغیرهای اختیاری

```bash
# YouTube — کوکی مرورگر (توصیه‌شده برای سرورهای دیتاسنتر)
yt-dlp --cookies-from-browser chrome --cookies /path/yt_cookies.txt https://youtube.com
export YOUTUBE_COOKIES_FILE=/path/yt_cookies.txt

# WARP Proxy — Cloudflare proxy برای دور زدن بلاک دیتاسنتر (اختیاری)
# نصب: https://pkg.cloudflareclient.com/
# warp-cli set-mode proxy && warp-cli connect
export WARP_PROXY=socks5://127.0.0.1:40000

# Cobalt API — fallback نهایی بعد از شکست تمام استراتژی‌های yt-dlp
# self-host: https://github.com/imputnet/cobalt
# برای غیرفعال کردن: خالی بگذارید
export COBALT_URL=http://localhost:9000

# GitHub — افزایش rate limit API
export GITHUB_TOKEN=your_github_personal_token

# Telegram MTProto — خواندن کانال‌های خصوصی
# از https://my.telegram.org بگیرید
export TG_API_ID=12345678
export TG_API_HASH=abcdef1234567890abcdef1234567890

# Twitter — کوکی برای دانلود توییت‌های محدودشده
export TWITTER_COOKIES_FILE=/path/twitter_cookies.txt

# Instagram — برای پروفایل‌های خصوصی
export INSTAGRAM_USER=your_username
export INSTAGRAM_PASS=your_password
# کوکی فایل برای yt-dlp (جایگزین user/pass)
export INSTAGRAM_COOKIES_FILE=/path/instagram_cookies.txt

# Spotify — اختیاری، برای پلیلیست‌ها مفید است
# از https://developer.spotify.com/dashboard بگیرید
export SPOTIFY_CLIENT_ID=your_client_id
export SPOTIFY_CLIENT_SECRET=your_client_secret

# Z-Library: ایمیل و رمز حساب از z-library.sk/login
export ZLIB_EMAIL=your@email.com
export ZLIB_PASSWORD=your_password
```

### ۵. اجرا

```bash
python bale_bot.py
```

---

## 📋 دستورات ربات

| دستور | عملکرد |
|---|---|
| `/start` | شروع و نمایش منوی اصلی |
| `/help` | نمایش راهنمای کامل |
| `/stats` | نمایش آمار کاربری |
| `/cancel` | لغو عملیات جاری |
| `/ocr` | ریپلای روی عکس → استخراج متن |

---

## 🚀 اجرا به‌عنوان سرویس (systemd)

```ini
[Unit]
Description=بله قربان Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/bot
EnvironmentFile=/path/to/bot/.env
ExecStart=/usr/bin/python3 /path/to/bot/bale_bot.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/bale_bot.log
StandardError=append:/var/log/bale_bot.log

[Install]
WantedBy=multi-user.target
```

فایل `.env`:
```bash
BALE_TOKEN=your_token_here
YOUTUBE_COOKIES_FILE=/path/yt_cookies.txt
GITHUB_TOKEN=optional_token
# WARP_PROXY=socks5://127.0.0.1:40000
# COBALT_URL=http://localhost:9000
```

```bash
sudo systemctl enable bale-bot
sudo systemctl start bale-bot
sudo systemctl status bale-bot
sudo journalctl -u bale-bot -f   # مشاهده لاگ
```

---

## 🔧 ساختار فایل

```
bale_bot.py        ← فایل اصلی ربات (۵۵۰۰+ خط)
requirements.txt   ← کتابخانه‌های مورد نیاز
env.example        ← نمونه تنظیمات محیطی
README.md          ← این فایل
bale_bot.log       ← فایل لاگ (ایجاد می‌شود)
tg_session.session ← نشست Telethon (ایجاد می‌شود)
```

---

## 📊 معماری فنی

- **Long Polling** — بدون نیاز به IP ثابت یا دامنه
- **result_cache** — نتایج جستجو ذخیره موقت برای دکمه‌های callback
- **url_cache** — ذخیره URL برای دکمه‌های site-view و download
- **smart_send** — ارسال هوشمند با chunking 19MB (مطابق index.js)
- **127+ log statement** — لاگ DEBUG کامل در stdout + فایل
- **YouTube ۳ مرحله** — انتخاب کیفیت → انتخاب زیرنویس → دانلود
- **YouTube ۳ استراتژی** — probe+download (5 client) → safety-net → Cobalt fallback
- **youtube_get_formats** — probe واقعی فرمت‌های موجود برای نمایش picker دقیق
- **Instagram ۳ استراتژی** — instaloader → yt-dlp (با کوکی) → Cobalt API
- **APK ۴ استراتژی** — APKPure → APKMirror → Aptoide → F-Droid
- **همه پیام‌ها فارسی** — تمام متن‌های ارسالی به کاربر به زبان فارسی است

---

**© ۱۴۰۵ — بله قربان**