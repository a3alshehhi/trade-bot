# -*- coding: utf-8 -*-
"""وقف متدرّج بنيوي (structure-based staged trailing) — طلب بو محمد 2026-07-26.

يخصّ بوتَي «العرض/الطلب» (شراء/الفيواب الأسبوعي) و«الفيواب الأسبوعي · BTC» فقط.
يُضاف *فوق* نظام الخروج الحالي (جني 50% عند الهدف الأول + تعادل + قفل 0.3R):
الوقف الأعلى (الأكثر حماية) يفوز دائماً، والوقف لا ينزل أبداً (ratchet).

المراحل (تعديل بو محمد 2026-07-26):
  (بوابة) انتهاء التصحيح عبر هيستوجرام الماكد — شمعة خضراء (hist>0) أعلى من التي
      قبلها + السعر صاعد — **شرط تمكين فقط، الوقف لا يتحرك عندها**.
  (1) قمة أعلى من قمة (بعد انتهاء التصحيح) → الوقف تحت «أقرب قاع للتصحيح» ناقص 0.5%.
  (2) بلوغ الهدف الأول → الوقف فوق متوسط الدخول + 0.5%.

مرحلة الخروج الفوري (طلب بو محمد 2026-07-28) — دالة مستقلّة `hard_exit`:
  إذا لم يبلغ السعر الهدف الأول ثم بدأ يتراجع فأغلق **تحت الفيواب الأسبوعي**
  و**تحوّل هيستوجرام الماكد إلى الأحمر (hist<0)** → خروج فوري من كامل الصفقة.
  ليست رفعاً للوقف بل إغلاق كامل. افتراضياً لا تُسلَّح إلا بعد انتهاء التصحيح
  (نفس البوابة) لتفادي الخروج أثناء انخفاض الدخول الطبيعي — يُضبط بـ
  STAGED_EXIT_REQUIRE_GATE، والميزة كلها بـ STAGED_HARD_EXIT.

الدوال نقية و idempotent: تُعيد حساب الحالة من كامل شموع الصفقة المغلقة في كل
نداء (لا حالة تراكمية هشّة). المُنادي يدمج ناتج `compute_staged_stop` بـ:
    new_stop = max(cur_stop, target)   # الأعلى يفوز، لا ينزل أبداً
"""

import os

# عتبات قابلة للضبط بيئياً
SUPPORT_BUF = float(os.environ.get("STAGED_SUPPORT_BUF", "0.005"))   # 0.5% تحت أقرب قاع تصحيح (عند قمة أعلى)
TP1_BUF     = float(os.environ.get("STAGED_TP1_BUF", "0.005"))       # 0.5% فوق المتوسط عند الهدف1
PIVOT_W     = int(os.environ.get("STAGED_PIVOT_W", "2"))             # نصف نافذة القمة المحورية

# مرحلة الخروج الفوري (تحت الفيواب الأسبوعي + هيستوجرام أحمر قبل الهدف1)
HARD_EXIT      = os.environ.get("STAGED_HARD_EXIT", "1").strip() not in ("0", "", "false", "False")
EXIT_REQ_GATE  = os.environ.get("STAGED_EXIT_REQUIRE_GATE", "1").strip() not in ("0", "", "false", "False")

# تفعيل عام + قائمة البوتات المعنيّة (تُطابق حقل label في tracked_signals)
ENABLED = os.environ.get("STAGED_TRAIL", "1").strip() not in ("0", "", "false", "False")
_DEFAULT_LABELS = "العرض/الطلب،الفيواب الأسبوعي · BTC"
_LABELS = os.environ.get("STAGED_TRAIL_LABELS", _DEFAULT_LABELS)


def applies_to(label):
    """هل يُطبَّق الوقف المتدرّج على هذا البوت؟ (مفعّل + الوسم ضمن القائمة)."""
    if not ENABLED or not label:
        return False
    label = str(label).strip()
    for sep in ("،", ","):
        for x in _LABELS.split(sep):
            if x.strip() and x.strip() == label:
                return True
    return False


def _ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def macd_hist(closes, fast=12, slow=26, signal=9):
    """هيستوجرام الماكد الكلاسيكي (EMA) — بايثون نقي بلا اعتماديات."""
    n = len(closes)
    if n < slow + signal:
        return [0.0] * n
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    sig = _ema(macd, signal)
    return [m - s for m, s in zip(macd, sig)]


def _pivot_highs(highs, w=2):
    """فهارس القمم المحورية المؤكَّدة: high[i] هو الأعلى ضمن ±w وأعلى من جاره اليساري."""
    piv = []
    n = len(highs)
    for i in range(w, n - w):
        seg = highs[i - w:i + w + 1]
        if highs[i] == max(seg) and highs[i] > highs[i - 1]:
            piv.append(i)
    return piv


def compute_staged_stop(highs, lows, closes, entry, tp1, w=PIVOT_W):
    """يحسب الوقف المتدرّج المستهدَف (idempotent) من شموع الصفقة المغلقة منذ الدخول.

    highs/lows/closes: قوائم شموع مغلقة، العنصر [0] ≈ شمعة الدخول.
    entry: متوسط الدخول. tp1: الهدف الأول (قمة التصحيح).
    يعيد (target_stop | None, stage 0..3, note).
      target_stop=None يعني لم تتحقق أي مرحلة بعد (لا رفع مقترح).
    """
    n = len(closes)
    if n < 3:
        return None, 0, ""
    hist = macd_hist(closes)

    # ── البوابة: انتهاء التصحيح عبر هيستوجرام الماكد (الوقف لا يتحرك عندها) ──
    s1 = None
    for j in range(1, n):
        if hist[j] > 0 and hist[j] > hist[j - 1] and closes[j] > closes[j - 1]:
            s1 = j
            break
    if s1 is None:
        return None, 0, ""            # التصحيح لم يتأكّد انتهاؤه → الوقف ثابت

    target = None
    stage = 1                          # 1 = انتهى التصحيح فقط (بلا تحريك للوقف)
    note = ""

    # ── المرحلة (1): قمة أعلى من قمة بعد انتهاء التصحيح → الوقف تحت أقرب قاع −0.5% ──
    #   أول قمة محورية بعد s1 = القمة المرجعية، وأول اختراق لها لاحقاً = «قمة أعلى من قمة».
    corr_low = min(lows[:s1 + 1])      # أقرب قاع للتصحيح (أدنى قاع حتى انتهائه)
    piv = [i for i in _pivot_highs(highs, w) if i >= s1]
    if piv:
        ref_high = highs[piv[0]]
        for j in range(piv[0] + 1, n):
            if highs[j] > ref_high:
                cand = corr_low * (1 - SUPPORT_BUF)
                if target is None or cand > target:
                    target = cand
                stage = max(stage, 2)
                note = "قمة أعلى من قمة → الوقف تحت أقرب قاع −0.5%"
                break

    # ── المرحلة (2): الهدف الأول → الوقف فوق متوسط الدخول +0.5% ──
    if tp1 and max(highs) >= tp1:
        be_plus = entry * (1 + TP1_BUF)
        if target is None or be_plus > target:
            target = be_plus
        stage = max(stage, 3)
        note = "الهدف الأول → الوقف فوق متوسط الدخول +0.5%"

    return target, stage, note


def hard_exit(highs, lows, closes, entry, tp1, wvwap):
    """مرحلة الخروج الفوري (طلب بو محمد 2026-07-28) — idempotent، بلا حالة.

    قبل بلوغ الهدف الأول، إذا أغلقت آخر شمعة **تحت الفيواب الأسبوعي** وكان
    هيستوجرام الماكد **أحمر (hist<0)** → خروج فوري من كامل الصفقة.

    highs/lows/closes: شموع الصفقة المغلقة منذ الدخول (العنصر [0] ≈ الدخول).
    entry/tp1: متوسط الدخول والهدف الأول. wvwap: قيمة الفيواب الأسبوعي عند آخر
      شمعة مغلقة (يحسبها المُنادي على كامل السلسلة كي يبقى الإرساء الأسبوعي صحيحاً).
    يعيد (should_exit: bool, note: str).
    """
    if not HARD_EXIT:
        return False, ""
    n = len(closes)
    if n < 3 or wvwap is None or wvwap != wvwap:          # nan-safe
        return False, ""
    if tp1 and max(highs) >= tp1:                          # الهدف تحقّق → تُدار 50/50 لا خروج فوري
        return False, ""

    hist = macd_hist(closes)

    # البوابة (افتراضياً مطلوبة): لا يُسلَّح الخروج إلا بعد انتهاء التصحيح — أي
    # ارتد الزخم للأخضر صعوداً مرّة، ثم «بدأ يتراجع» بعدها. يمنع الخروج أثناء
    # انخفاض الدخول الطبيعي. STAGED_EXIT_REQUIRE_GATE=0 يُلغي هذا الشرط.
    if EXIT_REQ_GATE:
        gated = False
        for j in range(1, n):
            if hist[j] > 0 and hist[j] > hist[j - 1] and closes[j] > closes[j - 1]:
                gated = True
                break
        if not gated:
            return False, ""

    if closes[-1] < wvwap and hist[-1] < 0:
        return True, "تراجع تحت الفيواب الأسبوعي + هيستوجرام أحمر (قبل الهدف1) → خروج فوري"
    return False, ""
