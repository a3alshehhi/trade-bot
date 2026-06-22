# تشغيل البوت بانتظام عبر مُجدوِل خارجي (cron-job.org)

الهدف: استدعاء البوت كل 15 دقيقة بدقة، بدل الاعتماد على جدولة GitHub المتقلّبة.
الفكرة: خدمة مجانية (cron-job.org) تنادي GitHub كل 15 دقيقة وتطلب تشغيل الـworkflow.
البوت يبقى على GitHub Actions كما هو — نغيّر فقط "الزناد".

تم التحقق: لا يوجد أي توكن أو معرّف مكتوب في الكود، فآمن جعل المستودع عاماً.

---

## الخطوة 1 — اجعل المستودع عاماً (public)

> لازم تسويها أنت بنفسك (تغيير صلاحيات المستودع).

1. افتح: https://github.com/a3alshehhi/trade-bot/settings
2. انزل لآخر الصفحة → قسم **Danger Zone**.
3. **Change repository visibility** → **Change to public** → أكّد.

السبب: المستودع العام تشغيل Actions فيه **مجاني بلا حدود**، فالتشغيل كل 15 دقيقة لا يكلّف شيئاً.
الأسرار في Secrets تبقى مخفية ولا تنكشف.

---

## الخطوة 2 — أنشئ توكن GitHub (Fine-grained)

> لازم تسويها أنت (إنشاء توكن). لا تشاركه مع أحد ولا تكتبه في الشات.

1. افتح: https://github.com/settings/personal-access-tokens/new
2. **Token name**: `cronjob-trigger`
3. **Expiration**: سنة (أو حسب رغبتك).
4. **Repository access** → **Only select repositories** → اختر `trade-bot`.
5. **Permissions** → **Repository permissions** → **Actions** → اضبطها على **Read and write**.
   (Metadata = Read تنضبط تلقائياً، اتركها.)
6. **Generate token** → انسخ التوكن واحتفظ به مؤقتاً (يظهر مرة واحدة فقط).

---

## الخطوة 3 — أنشئ المهمة في cron-job.org

> لازم تسويها أنت (إنشاء حساب + لصق التوكن).

1. سجّل حساباً مجانياً: https://cron-job.org → Sign up.
2. **Create cronjob**.
3. **Title**: `trade-bot reversal`
4. **URL**:
   ```
   https://api.github.com/repos/a3alshehhi/trade-bot/actions/workflows/reversal.yml/dispatches
   ```
5. **Schedule**: Every 15 minutes (اختر "Every 15 minutes" أو خصّص الدقائق 0,15,30,45).
6. افتح **Advanced** (أو "Show advanced settings"):
   - **Request method**: `POST`
   - **Request body** (Custom):
     ```json
     {"ref":"main"}
     ```
   - **Headers** — أضف هذي الثلاثة:
     | Key | Value |
     |---|---|
     | `Accept` | `application/vnd.github+json` |
     | `Authorization` | `Bearer ضع_التوكن_هنا` |
     | `X-GitHub-Api-Version` | `2022-11-28` |
7. **Save / Create**.

> ملاحظة: نجاح الطلب يرجع رمز **204** (بلا محتوى) — هذا طبيعي ويعني نجح.

---

## الخطوة 4 — تأكيد

- بعد دقيقة، افتح: https://github.com/a3alshehhi/trade-bot/actions
- المفروض تشوف تشغيل جديد لـ **reversal-live** بحدث `workflow_dispatch`.
- في cron-job.org → سجل المهمة (History) يبيّن استجابة 204 = نجاح.

---

## (اختياري) إيقاف جدولة GitHub المكرّرة

بعد ما يشتغل المُجدوِل الخارجي، تقدر تشيل سطر `schedule` من `reversal.yml`
عشان ما يصير تشغيلان. أو اتركه كاحتياط — التكرار غير مضرّ.

## استراتيجية trendwave مربوطة تلقائياً ✅
استراتيجية **trendwave** الجديدة أُضيفت **داخل** وركفلو `reversal.yml` نفسه (على 15m/1h/4h)،
وهو الوركفلو الذي يستدعيه cron-job.org كل 15 دقيقة. لذلك **لا تحتاج أي مهمة جديدة في
cron-job.org** — كل تشغيل دوري يفحص trendwave أيضاً ويرسل إشاراتها (رسائل مُعنونة «🌟 trendwave»)
مع منع التكرار عبر ملف `trendwave_alerts.json`. لو رغبت لاحقاً بجدولة منفصلة لها، يمكن فصلها
في وركفلو مستقل، لكن الدمج الحالي أأمن (يتجنّب تعارض حفظ الحالة وتيليجرام).

---

## أمان
- لا تضع التوكن في أي ملف داخل المستودع. مكانه الوحيد: cron-job.org.
- لو انكشف التوكن، احذفه من https://github.com/settings/tokens وأنشئ غيره.
