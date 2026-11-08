# EQForge — راه‌اندازی سریع (فارسی)

این صفحه کوتاه‌ترین مسیر نصب و به‌روزرسانی EQForge روی لینوکس است. برای جزئیات
کامل، [`installation.md`](installation.md) و [`cli.md`](cli.md) را ببینید.

---

## ۱. نصب یا به‌روزرسانی با یک اسکریپت

یک اسکریپت، هم **نصب** است و هم **به‌روزرسانی**. لازم نیست بدانید نسخه‌ی قبلی
نصب هست یا نه؛ خودش تشخیص می‌دهد:

```sh
curl -fsSL https://raw.githubusercontent.com/erfanjaster/eqforge/main/scripts/eqforge-update.sh | bash
```

یا اگر فایل اسکریپت را در اختیار دارید:

```sh
bash eqforge-update.sh
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
sudo dnf install python3 gcc make git curl pipewire pipewire-jack-audio-connection-kit wireplumber pipewire-devel
```

اسکریپت بدون `sudo` همه‌چیز را در `~/.local` نصب می‌کند.

| وضعیت سیستم | کاری که اسکریپت می‌کند |
|---|---|
| هیچ نسخه‌ای نصب نیست | نصب کامل: هسته‌ی C، هاست بلادرنگ، ماژول PipeWire، بستهٔ پایتون |
| نسخهٔ قدیمی نصب است | به‌روزرسانی در همان محل؛ نسخهٔ قبلی برای بازگشت نگه داشته می‌شود |
| همان نسخه نصب است | «already up to date» و هیچ تغییری نمی‌دهد |
| نصب دستی قبلی (pip یا checkout) | نسخهٔ مدیریت‌شدهٔ خودش را نصب می‌کند و می‌گوید نسخهٔ قبلی را چطور پاک کنید |

### گزینه‌های پرکاربرد

```sh
bash eqforge-update.sh --check        # فقط بگو نسخهٔ نصب‌شده و آخرین نسخه چیست
bash eqforge-update.sh --dry-run      # فقط برنامه را نشان بده، چیزی را تغییر نده
bash eqforge-update.sh --ref v1.1.0   # نصب یک نسخهٔ مشخص
bash eqforge-update.sh --channel main # آخرین تغییرات شاخهٔ main
bash eqforge-update.sh --rollback     # برگشت به نسخهٔ قبلی
bash eqforge-update.sh --uninstall    # حذف نصب (پروفایل‌ها می‌مانند)
bash eqforge-update.sh --uninstall --purge   # حذف کامل همراه با تنظیمات
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
