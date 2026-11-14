# EQForge — راه‌اندازی سریع (فارسی)

این صفحه کوتاه‌ترین مسیر نصب و به‌روزرسانی EQForge روی لینوکس است. برای جزئیات
کامل، [`installation.md`](installation.md) و [`cli.md`](cli.md) را ببینید.

---

## ۱. نصب یا به‌روزرسانی با یک اسکریپت

برای نصب کامل و آماده‌به‌کار:

```sh
curl -fsSL https://raw.githubusercontent.com/erfanjaster/eqforge/main/scripts/eqforge.sh -o eqforge.sh
# باید دقیقاً این خط را چاپ کند: #!/usr/bin/env bash
head -n 1 eqforge.sh
bash eqforge.sh --install
```

`--install` هستهٔ C را کامپایل می‌کند، برنامه و وابستگی‌های Python را نصب
می‌کند، میان‌بر منوی برنامه‌ها را می‌سازد، نصب را بررسی می‌کند و در صورت
آماده‌بودن JACK/PipeWire و systemd کاربر، سرویس صوتی را راه‌اندازی می‌کند.
برای نسخهٔ موجود از `bash eqforge.sh --update` استفاده کنید؛ تنظیم اجرای
خودکار قبلی حفظ می‌شود. اسکریپت خودش `sudo` یا مدیر بستهٔ سیستم را اجرا
نمی‌کند؛ پیش‌نیازهای سیستم‌عامل را باید
پیشاپیش نصب کنید.

از پیوند **Raw** بالا استفاده کنید، نه صفحهٔ نمایش فایل با نشانی `/blob/` در
گیت‌هاب. ذخیرهٔ آن صفحه یک فایل HTML می‌دهد؛ تغییر پسوند به `.sh` آن را به
اسکریپت تبدیل نمی‌کند. اگر خط اول فایل HTML است، آن را اجرا نکنید و نسخهٔ خام
را دوباره دانلود کنید.

یا اگر فایل اسکریپت را در اختیار دارید:

```sh
bash eqforge.sh --install
```

پیش‌نیازها فقط این‌ها هستند (روی اکثر توزیع‌ها از قبل نصب‌اند):

```sh
# Debian / Ubuntu
sudo apt install python3 python3-venv build-essential git curl \
                 pipewire pipewire-audio wireplumber \
                 libpipewire-0.3-dev libspa-0.2-dev libjack-jackd2-dev python3-numpy

# Arch
sudo pacman -S base-devel python git curl pipewire pipewire-alsa pipewire-jack wireplumber

# Fedora
sudo dnf install python3 python3-pip gcc make git curl pipewire pipewire-jack-audio-connection-kit wireplumber pipewire-devel
```

اسکریپت بدون `sudo` همه‌چیز را در `~/.local` نصب می‌کند.

| وضعیت سیستم | کاری که اسکریپت می‌کند |
|---|---|
| `--install` | نصب یا تعمیر کامل: کامپایل هسته، نصب برنامه، میان‌بر دسکتاپ، بررسی نصب و راه‌اندازی خودکار سرویس در صورت پشتیبانی |
| `--update` | به‌روزرسانی نسخهٔ نصب‌شده؛ اگر نصب نیست، اجرای `--install` را پیشنهاد می‌دهد |
| اجرای بدون این دو گزینه | تشخیص خودکار نصب یا به‌روزرسانی، برای سازگاری با روش قدیمی |

ماژول بلادرنگ و SPA به هدرهای توسعهٔ JACK/PipeWire نیاز دارند و در نبود آن‌ها
رد می‌شوند؛ برنامهٔ CLI/GUI همچنان نصب می‌شود.

### گزینه‌های پرکاربرد

```sh
bash eqforge.sh --install      # نصب/تعمیر کامل، شامل کامپایل
bash eqforge.sh --update       # به‌روزرسانی نصب موجود
bash eqforge.sh --check        # فقط بگو نسخهٔ نصب‌شده و آخرین نسخه چیست
bash eqforge.sh --dry-run      # فقط برنامه را نشان بده، چیزی را تغییر نده
bash eqforge.sh --ref v1.1.0   # نصب یک نسخهٔ مشخص
bash eqforge.sh --channel main # آخرین تغییرات شاخهٔ main
bash eqforge.sh --rollback     # برگشت به نسخهٔ قبلی
bash eqforge.sh --uninstall    # حذف نصب (پروفایل‌ها می‌مانند)
bash eqforge.sh --uninstall --purge   # حذف کامل همراه با تنظیمات
```

---

## ۲. راه‌اندازی پردازش صدای سیستم (یک بار برای همیشه)

```sh
eqforge system autostart on        # اجرا در زمان ورود به سیستم (systemd --user)
eqforge system start --manage      # هاست بلادرنگ + اتصال به خروجی پیش‌فرض
eqforge set-profile music          # انتخاب پروفایل
eqforge status                     # وضعیت کامل
eqforge gui                        # رابط گرافیکی در مرورگر
```

## ۳. حذف تأخیر شروع صدا (EQ فقط بعد از یک ثانیه اعمال می‌شد)

اگر هنگام پخش، «اولین ثانیه» بدون اکولایزر می‌ماند و بعد افکت‌ها اضافه می‌شوند،
این یک مشکل زمان‌بندی مسیریابی است، نه باگ موتور صدا:

```sh
eqforge system always-on on --now   # استریم‌ها از همان اولین نمونه از EQForge رد می‌شوند
eqforge status                      # خط "graph events": events یعنی واکنش میلی‌ثانیه‌ای
```

سه لایه‌ای که این مشکل را می‌بندند:

1. **رویدادهای PipeWire** به‌جای نظرسنجی هر ۲ ثانیه → واکنش در حدود **۳۰ تا ۶۰ میلی‌ثانیه**.
2. **قانون مسیریابی WirePlumber** (دستور `always-on`) → استریم از **نمونهٔ صفرم**
   پردازش می‌شود.
3. **بارگذاری پیکربندی قبل از پخش صدا** در ماژول SPA و در `eqforge-rt` (تلاش
   مجدد سریع اگر فایل پیکربندی هنوز ساخته نشده باشد).

برای برگشت به حالت قبل:

```sh
eqforge system always-on off
```

---

## ۴. اگر مشکلی بود

```sh
eqforge doctor                                   # خودآزمایی کامل
eqforge status                                   # نسخه، پروفایل، هاست، رویدادها
journalctl --user -u eqforge-rt -n 50            # لاگ هاست بلادرنگ
journalctl --user -u eqforge-daemon -n 50        # لاگ دیمن
pw-link | grep eqforge                           # اتصال‌های گراف صدا
pw-dump | grep -i eqforge                        # نودهای EQForge
```

راهنمای کامل عیب‌یابی: [`troubleshooting.md`](troubleshooting.md)
