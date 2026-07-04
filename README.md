# 🤖 بله قربان — Bale Web Assistant Bot

یک ربات جامع برای پیام‌رسان **بله** با قابلیت‌های متعدد جستجو، دانلود، شبکه‌های اجتماعی و بیشتر.

## ✨ قابلیت‌ها

| ابزار | توضیح |
|------|--------|
| 🔎 **جستجو در وب** | تا ۱۰ نتیجه از DuckDuckGo — نتایج قابل کلیک با صفحه‌بندی |
| 🌐 **مشاهده سایت** | اسکرین‌شات ۱۹۲۰×۱۰۸۰ + دکمه‌های متن / HTML / ZIP / PDF |
| 📚 **مقاله علمی** | جستجوی Google Scholar با صفحه‌بندی و نتایج قابل کلیک |
| 📖 **ویکی‌پدیا** | جستجو + خواندن مقاله کامل (فارسی و انگلیسی) |
| 📺 **یوتیوب** | جستجو با تامبنیل + توضیحات، **انتخاب کیفیت با نمایش حجم فایل** (360p–1080p+)، **انتخاب زیرنویس**، دانلود صدا، **۳ استراتژی yt-dlp** + **Cobalt fallback** |
| 🎵 **موسیقی MP3** | دانلود صوتی از YouTube Music / SoundCloud / Spotify / JioSaavn / Deezer |
| 🖼 **دانلود عکس** | Bing (بهبودیافته) / Pinterest (fallback چندلایه) / Pixabay (نیاز API key) / Wikimedia (رتبه‌بندی هوشمند) با «دانلود بیشتر» |
| 🐙 **GitHub** | جستجوی مخازن + دانلود ZIP + دانلود Release |
| ✈️ **کانال تلگرام** | خواندن پیام‌های کانال عمومی (scrape) یا MTProto |
| 🐦 **توییتر / X** | تایم‌لاین کاربر + دانلود عکس خودکار + دانلود ویدیو |
| 📸 **اینستاگرام** | پست‌های پروفایل + دانلود عکس خودکار + دانلود ریل (۳ استراتژی: instaloader → yt-dlp → Cobalt API) |
| 🎵 **تیک‌تاک** | لیست ویدیوها با تامبنیل + دانلود مستقیم (Cobalt API ثابت‌شده) |
| 📱 **APK دانلود** | جستجو در Google Play + دانلود APK از APKPure / APKMirror / Aptoide / F-Droid |
| 📰 **اخبار RSS** | دریافت فید + کشف خودکار فید از سایت |
| 📚 **Z-Library** | جستجو و دانلود کتاب الکترونیکی (PDF، EPUB، MOBI، FB2) — سرعت بهبود یافته |
| 🌐 **ترجمه** | ترجمه به فارسی، انگلیسی، عربی، آلمانی، فرانسوی، روسی |
| 🖼 **OCR** | استخراج متن از عکس + خروجی PDF |
| 🌐 **IP / دامنه** | موقعیت، اپراتور، منطقه زمانی |
| 🔒 **حریم خصوصی** | توضیح کامل نحوه عدم ذخیره داده |
| 🛡 **اسکن ویروس** | اسکن فایل و URL با VirusTotal (نیاز به API key) |
| 🦋 **بلواسکای** | دانلود پست، متن و رسانه از Bluesky |
| 📄 **مقاله علمی** | جستجو via Semantic Scholar + دانلود PDF از Sci-Hub/LibGen/Unpaywall — سرعت بهبود یافته |

### 📺 دانلود یوتیوب — جزئیات

فرآیند دانلود چهار مرحله‌ای است:

1. **تامبنیل + توضیحات** — ابتدا عکس تامبنیل و توضیحات کامل ویدیو ارسال می‌شود.
2. **انتخاب کیفیت** — ربات فرمت‌های واقعی موجود برای ویدیو را بررسی کرده و به‌صورت دکمه با **نمایش حجم فایل** نمایش می‌دهد (مثلاً 1080p 60fps [25MB] / 720p [15MB] / 480p [8MB] / 360p [5MB]). گزینه «فقط صدا (MP3)» هم همیشه موجود است.
3. **انتخاب زیرنویس** — اگر زیرنویس (دستی یا خودکار) موجود باشد، لیست زبان‌ها نمایش داده می‌شود. گزینه «بدون زیرنویس» هم موجود است.
4. **دانلود** — با فرمت دقیق انتخابی دانلود می‌شود.

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

- فایل‌های بزرگ به **قطعه‌های ۲۰MB** (Bale) / **۴۸MB** (Telegram) تقسیم می‌شوند
- پسوندهای غیر پشتیبانی‌شده در **ZIP** بسته‌بندی می‌شوند
- پیام ترکیب قطعه‌ها ارسال می‌شود: `cat file.part*of3.ext > file.ext`

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

# اختیاری: ربات تلگرام همزمان (برای پشتیبانی دوplatform)
export TELEGRAM_TOKEN="توکن_ربات_تلگرام_شما"
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

# Instagram — برای پروفایل‌های خصوصی‌ها
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

# VirusTotal — اسکن فایل و URL (حداقل ۴ درخواست در دقیقه، ۵۰۰ در روز)
export VIRUSTOTAL_API_KEY=your_64char_vt_key_here

# Pixabay — کلید API برای جستجوی عکس (اختیاری - دریافت از pixabay.com/api/docs/)
export PIXABAY_KEY=your_pixabay_key

# Unpaywall — ایمیل برای دانلود مقاله علمی (اختیاری - ثبت‌نام در unpaywall.org)
export UNPAYWALL_EMAIL=your_email@example.com
```

### ۵. اجرا

```bash
python bale_bot.py
```

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
TELEGRAM_TOKEN=optional_telegram_token
YOUTUBE_COOKIES_FILE=/path/yt_cookies.txt
GITHUB_TOKEN=optional_token
# WARP_PROXY=socks5://127.0.0.1:40000
# COBALT_URL=http://localhost:9000
# TG_API_ID=12345678
# TG_API_HASH=abcdef1234567890abcdef1234567890
# TWITTER_COOKIES_FILE=/path/twitter_cookies.txt
# INSTAGRAM_USER=your_username
# INSTAGRAM_PASS=your_password
# INSTAGRAM_COOKIES_FILE=/path/instagram_cookies.txt
# SPOTIFY_CLIENT_ID=your_client_id
# SPOTIFY_CLIENT_SECRET=your_client_secret
# ZLIB_EMAIL=your@email.com
# ZLIB_PASSWORD=your_password
# VIRUSTOTAL_API_KEY=your_64char_vt_key_here
# PIXABAY_KEY=your_pixabay_key
# UNPAYWALL_EMAIL=your_email@example.com
```

```bash
sudo systemctl enable bale-bot
sudo systemctl start bale-bot
sudo systemctl status bale-bot
sudo journalctl -u bale-bot -f   # مشاهده لاگ
```

## 📊 معماری فنی

- **Long Polling** — بدون نیاز به IP ثابت یا دامنه
- **result_cache** — نتایج جستجو ذخیره موقت برای دکمه‌های callback
- **url_cache** — ذخیره URL برای دکمه‌های site-view و download
- **smart_send** — ارسال هوشمند با chunking 20MB (Bale) / 48MB (Telegram)
- **127+ log statement** — لاگ DEBUG کامل در stdout + فایل
- **YouTube ۴ مرحله** — تامبنیل+توضیحات → انتخاب کیفیت (با حجم فایل) → انتخاب زیرنویس → دانلود
- **YouTube ۳ استراتژی** — probe+download (5 client) → safety-net → Cobalt fallback
- **youtube_get_formats** — probe واقعی فرمت‌های موجود برای نمایش picker دقیق با حجم فایل
- **Instagram ۳ استراتژی** — instaloader → yt-dlp (با کوکی) → Cobalt API
- **APK ۴ استراتژی** — APKPure → APKMirror → Aptoide → F-Droid
- **Z-Library/Paper** — آینه‌های بهینه‌شده برای سرعت بالاتر (حذف آینه‌های timeout‌دار)
- **Image Search** — Bing با regex بهبودیافته، Wikimedia با رتبه‌بندی امتیاز، Pinterest با fallback چندلایه
- **TikTok** — Cobalt API ثابت‌شده برای payload صحیح
- **همه پیام‌ها فارسی** — تمام متن‌های ارسالی به کاربر به زبان فارسی است

## 📋 دستورات ربات

| دستور | عملکرد |
|------|--------|
| `/start` | شروع و نمایش منوی اصلی |
| `/help` | نمایش راهنمای کامل |
| `/stats` | نمایش آمار کاربری |
| `/cancel` | لغو عملیات جاری |
| `/ocr` | ریپلای روی عکس → استخراج متن |

## 📁 ساختار فایل

```
baleghorbanbot/
├── bale_bot.py        ← فایل اصلی ربات (۹۵۰۰+ خط)
├── requirements.txt   ← کتابخانه‌های مورد نیاز + مستندات پکیج‌های سیستمی
├── env.example        ← نمونه تنظیمات محیطی
├── README.md          ← این فایل
├── bale_bot.log       ← فایل لاگ (ایجاد می‌شود)
└── tg_session.session ← نشست Telethon (ایجاد می‌شود)
```

> **© ۱۴۰۵ — بله قربان**  
> ربات با ♥ در ایران توسعه یافته و حریم خصوصی کاربران را به شدت محترم می‌دارد. هیچ داده‌ای از کاربران ذخیره نمی‌شود.