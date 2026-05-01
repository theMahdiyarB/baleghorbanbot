# 🤖 دستیار وب — Bale Web Assistant Bot

یک ربات جامع برای پیام‌رسان **بله** با قابلیت‌های متعدد جستجو، دانلود، ترجمه و بیشتر.

---

## ✨ قابلیت‌ها

| دستور / حالت | توضیح |
|---|---|
| 🔎 **جستجو در وب** | تا ۱۰ نتیجه از DuckDuckGo با عنوان و لینک |
| 📄 **نتایج HTML** | نتایج جستجو به‌صورت فایل HTML قابل ذخیره |
| 🌐 **باز کردن سایت** | دریافت متن و فایل HTML هر صفحه‌ای |
| 📑 **PDF از صفحه** | دانلود HTML صفحه برای مرور آفلاین |
| 🗜 **ZIP آفلاین** | صفحه + منابع (CSS/JS/تصاویر) در قالب ZIP |
| 📥 **GitHub** | دانلود کل مخزن GitHub به‌صورت ZIP |
| 🌐 **ترجمه** | ترجمه متن به فارسی، انگلیسی، عربی، آلمانی، فرانسوی، روسی |
| 🖼 **OCR** | استخراج متن از عکس + خروجی PDF |
| 📚 **مقاله علمی** | جستجو در Google Scholar |
| 📺 **یوتیوب** | دانلود ویدیو یا جستجو در یوتیوب |
| 🎵 **موسیقی MP3** | دانلود MP3 اولین نتیجه یوتیوب |
| 📌 **پینترست** | جستجو و دانلود تصاویر از Pinterest |
| 📊 **آمار کاربری** | تعداد درخواست‌ها، دانلودها و ... |

---

## 🛠 نصب و راه‌اندازی

### پیش‌نیازها

```bash
# Python 3.10+
python --version

# Tesseract OCR (برای قابلیت OCR)
sudo apt-get install tesseract-ocr tesseract-ocr-fas tesseract-ocr-eng
```

### نصب کتابخانه‌ها

```bash
pip install -r requirements.txt
```

### تنظیم توکن

```bash
export BALE_TOKEN="توکن_ربات_شما"
```

یا مستقیماً در فایل `bale_bot.py` خط زیر را ویرایش کنید:

```python
TOKEN = "توکن_ربات_شما"
```

### اجرا

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
| `/ocr` | (روی پیام عکس ریپلای کنید) استخراج متن |

---

## 🚀 اجرا به‌عنوان سرویس (systemd)

```ini
[Unit]
Description=Bale Web Assistant Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/bot
Environment="BALE_TOKEN=your_token_here"
ExecStart=/usr/bin/python3 /path/to/bot/bale_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable bale-bot
sudo systemctl start bale-bot
sudo systemctl status bale-bot
```

---

## ⚙️ محدودیت‌ها

- حداکثر حجم فایل ارسالی: **۵۰ مگابایت**
- حداکثر حجم تصویر برای OCR: **۵ مگابایت**
- حداکثر حجم تصویر آپلودی: **۱۰ مگابایت**
- متن ترجمه: حداکثر **۵۰۰ کاراکتر** در هر درخواست (API رایگان)

---

## 🔧 ساختار فایل

```
bale_bot.py       ← فایل اصلی ربات
requirements.txt  ← کتابخانه‌های مورد نیاز
README.md         ← این فایل
```

---

## 📝 نکات توسعه

- ربات از **Long Polling** استفاده می‌کند (نیازی به سرور با IP ثابت نیست)
- برای استفاده از **Webhook** می‌توانید متد `setWebhook` را در API بله فعال کنید
- API بله بر پایه API تلگرام طراحی شده — می‌توانید از کتابخانه‌های تلگرام نیز استفاده کنید

---

## 📞 پشتیبانی

در صورت مشکل، از بخش بازو در پشتیبانی بله استفاده کنید.

---

**© 1404 — دستیار وب برای بله**
