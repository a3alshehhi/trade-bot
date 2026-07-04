# بوت «صيد الارتفاعات» (Hunter / SMC) — دليل الرفع والتشغيل

بوت **منفصل ومستقل** يطبّق استراتيجية صيد الارتفاعات: شراء فقط، فريم 15د،
دخول عند ريتست فيبو داخل منطقة الخصم بعد تحوّل الاتجاه.

## الملفات
- `hunter_bot.py` — البوت (فحص + باك-تست + تنفيذ).
- `.github/workflows/hunter_bot.yml` — الفحص المجدول (كل ساعة، 15m و1h) وإرسال أفضل إشارة لتيليجرام.
- `.github/workflows/hunter_backtest.yml` — باك-تست حقيقي على بيانات فعلية (تشغيل يدوي).
- `.github/workflows/hunter_exec.yml` — تنفيذ تجريبي على **Bybit Testnet** و**Binance Demo** معاً.

## 1) الرفع على GitHub (نفس المستودع الحالي a3alshehhi/trade-bot)
البوت يستخدم نفس `watchlist.txt` ونفس أسرار التيليجرام الموجودة. فقط ارفع الملفات الجديدة:

```
hunter_bot.py
.github/workflows/hunter_bot.yml
.github/workflows/hunter_backtest.yml
.github/workflows/hunter_exec.yml
```

من صفحة المستودع → **Add file → Upload files** → اسحبها → **Commit**.
(الوركفلوهات تروح تلقائياً تحت تبويب **Actions**.)

## 2) تشغيل الفحص فوراً
تبويب **Actions** → **Hunter Bot (صيد الارتفاعات SMC)** → **Run workflow**.
تصلك أفضل إشارة على تيليجرام (نفس القناة)، وتظهر في لوحة المتتبّع مثل بقية البوتات.

الجدولة السحابية تعمل كل ساعة تلقائياً. لو أردت كل 15 دقيقة، أضِف نقطة نهاية
الوركفلو في cron-job.org (نفس طريقة بقية البوتات).

## 3) الباك-تست الحقيقي (قبل أي تنفيذ)
تبويب **Actions** → **Hunter Backtest** → **Run workflow** → اختر الفريم والدرجة الدنيا.
بعد الانتهاء، نزّل ملف النتيجة `hunter_backtest_result.txt` من قسم Artifacts.
النتيجة تُقاس بوحدات R (التوقّع، نسبة الفوز، عامل الربح PF).

> ⚠️ لا تُفعّل التنفيذ قبل أن يُظهر الباك-تست حافة موجبة (توقّع +R و PF > 1).

## 4) التنفيذ التجريبي على بايبت وبايننس
البوت لا يحرّك أموالاً حقيقية. التنفيذ التجريبي فقط:
- **Bybit** → Testnet
- **Binance** → Demo (demo.binance.com)

الأسرار المطلوبة (Settings → Secrets and variables → Actions → New secret):

| السر | الشرح |
|------|-------|
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | موجودة أصلاً |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | من testnet.bybit.com ← API |
| `BYBIT_BASE_URL` | بروكسي Bybit (لأن IP أمريكا محجوب) — نفس البروكسي المستخدم حالياً |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | من demo.binance.com ← API Management |
| `BINANCE_BASE_URL` | بروكسي Binance (مثال: `https://<proxy>.vercel.app/binance`) |

ثم: **Actions → Hunter Exec → Run workflow**. ينفّذ شراءً تجريبياً بـ50 USDT
لأعلى إشارة على المنصّتين، ويطبع النتيجة في سجل التشغيل.

للتحكّم بالحجم: عدّل `HUNTER_USDT` في `hunter_exec.yml`.
لإيقاف منصّة: اجعل `HUNTER_EXEC_BYBIT` أو `HUNTER_EXEC_BINANCE` = `"0"`.

## إعدادات الاستراتيجية (متغيّرات بيئة)
| المتغيّر | الافتراضي | الشرح |
|----------|-----------|-------|
| `HUNTER_TF` | `15m` | فريم الدخول |
| `HUNTER_HTF` | `1h` | السياق الأعلى |
| `HUNTER_MIN_SCORE` | `4` | أدنى درجة (من 5 شروط) |
| `HUNTER_ONE_PER_DAY` | `1` | 1 = أنظف صفقة باليوم، 0 = عدّة إشارات |
| `HUNTER_USDT` | `50` | حجم الصفقة التجريبية |

---
⚠️ أداة تحليل تعليمية. لا تنفّذ صفقات بأموال حقيقية. التداول مخاطرة عالية خصوصاً
في العملات الصغيرة، وهذا ليس نصيحة مالية. القرار والمسؤولية عليك.
