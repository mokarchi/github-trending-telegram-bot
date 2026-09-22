# بات تلگرامی ترندهای GitHub

این پروژه بدون سرور دائمی کار می‌کند. GitHub Actions هر روز ساعت ۰۶:۳۰ به وقت تهران اجرا می‌شود و:

- ترندهای روزانه را هر روز می‌فرستد؛
- ترندهای هفتگی را دوشنبه می‌فرستد؛
- ترندهای ماهانه را روز اول هر ماه می‌فرستد.

برای هر ریپو، نام، لینک، زبان، ستاره‌ها، فورک‌ها، منبع کشف و توضیح فارسی ارسال می‌شود. علاوه بر GitHub Trending، بات داستان‌های تازه و برتر Hacker News را هم بررسی می‌کند، لینک‌های GitHub را استخراج می‌کند و ریپوهای تکراری را حذف می‌کند. برای Hacker News هیچ Secret جدیدی لازم نیست.

## راه‌اندازی

۱. این پوشه را در یک ریپوی GitHub قرار بده و پوشه‌ی `.github/workflows` را هم commit کن.

۲. در Telegram با `@BotFather` یک bot بساز و مقدار `TELEGRAM_BOT_TOKEN` را بگیر.

۳. به بات یک پیام مثل `/start` بفرست و برای پیدا کردن Chat ID از این آدرس استفاده کن:

```text
https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```

عدد `chat.id` را به‌عنوان `TELEGRAM_CHAT_ID` بردار.

۴. در GitHub برو به:

```text
Settings → Secrets and variables → Actions → New repository secret
```

این Secretها را اضافه کن:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
GEMINI_API_KEY
```

کلید Gemini از Google AI Studio برای توضیح فارسی استفاده می‌شود. مدل پیش‌فرض `gemini-3.5-flash-lite` است. اگر موقتاً کلید را نگذاری، بات همچنان ارسال می‌کند اما فقط توضیح رسمی GitHub را با یک متن فارسی ساده نمایش می‌دهد.

۵. در تب Actions، workflow با نام `GitHub Trending to Telegram` را باز کن و با گزینه‌ی `Run workflow` یک‌بار `all` را اجرا کن.

## تنظیمات

مقادیر پیش‌فرض در workflow قرار گرفته‌اند:

```text
TOP_N=10
TIMEZONE=Asia/Tehran
WEEKLY_DAY=0     # دوشنبه؛ Monday در Python
MONTHLY_DAY=1
```

برای ارسال هفتگی در شنبه، `WEEKLY_DAY` را به `5` تغییر بده.

## اجرای محلی برای تست

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

سپس متغیرهای `.env` را در محیط PowerShell تنظیم کن و اجرا کن:

```powershell
python src/main.py --period all
```

## نکته‌ی فنی

GitHub صفحه‌ی Trending را به‌صورت عمومی نمایش می‌دهد اما API رسمی مستقیمی برای «ترند» ندارد؛ کد ابتدا همان صفحه را می‌خواند و اگر ساختار صفحه موقتاً عوض شده باشد، از جست‌وجوی ریپوهای تازه‌ساخته‌شده به‌عنوان fallback استفاده می‌کند.

Hacker News از API رسمی عمومی خودش خوانده می‌شود. بات فقط داستان‌هایی را که به ریپوی GitHub لینک می‌شوند نگه می‌دارد و اطلاعات ستاره، فورک و زبان را از GitHub تکمیل می‌کند. در خروجی پیش‌فرض ۷۰٪ ظرفیت از GitHub Trending و ۳۰٪ از Hacker News رزرو می‌شود.
