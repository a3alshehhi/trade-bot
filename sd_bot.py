#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت العرض/الطلب بالذكاء الاصطناعي (v3) — إشارات فقط (لا تنفيذ صفقات)
=====================================================================
يرمّز مدرسة العرض/الطلب (مناطق طلب، كتل أوامر، FVG، اصطياد سيولة، كسر بنية، حجم)
ثم نموذج تعلّم آلي (Logistic Regression) يرشّح الإعدادات، ويرسل أفضلها كإشارة تيليجرام.

إعداد E (محافظ): دخول 1h، سياق 4h، فلتر «السعر فوق EMA200» + «4h غير هابط»، عتبة ML 0.60،
مخاطرة مقترحة 0.5% لكل صفقة وحد 5 مراكز. إدارة خروج: جني 50% عند +1R + تعادل + وقف متحرّك.

تحسينات الدخول (2026-07-04) لعلاج «دخول متأخر ثم ضرب الوقف»:
  • الدخول عند تصحيح فيبو 61.8% لساق الاندفاع (لا عند قمة المنطقة).
  • الوقف خارج المنطقة (تحت الأصل بـ0.5×ATR) لا داخلها.
  • أهداف فيبو (هدف1 = قمة الاندفاع، هدف2 = امتداد 1.618).
  • شمعة تأكيد إلزامية + رفض المناطق الفضفاضة/المسنّة.

تشديد الدخول (2026-07-04، طلب بو محمد) — «CHoCH في بداية الموجة + قرب من الاتجاه»:
  • CHoCH إلزامي: لا يدخل إلا إذا كسرت شمعة الاندفاع الهيكل صعوداً لأول مرة (انعكاس
    بداية موجة/زخم شرائي)، لا مجرد استمرار BOS (require_choch).
  • قريب من فلتر الاتجاه: الدخول فوق EMA200 لكن ضمن max_ema_dist (لا متمدّداً بعيداً).

الدخول الجديد (2026-07-05، طلب بو محمد) — «زخم مباشر + توسيط DCA بفيبو»:
  • حُذف دخول فيبو 61.8%. الدخول المباشر = أول شمعة (بعد CHoCH) يبلغ فيها RSI(rsi_entry_len=21)
    التشبّع الشرائي (≥ rsi_entry_ob=80) → تأكيد زخم، ندخل بإغلاقها.
  • توسيط DCA: سلالم فيبو (dca_fibs) أسفل الدخول المباشر لعمل متوسط أفضل (بين الوقف والدخول).
  • الوقف عند قاع الموجة الكاملة التي صنعت CHoCH (أدنى قاع من آخر قاع محوري قبل الاندفاع)
    − 0.5×ATR — أبعد من قاع شمعة الدخول → احتمال ضرب الوقف أقل.
  • الأهداف تبقى فيبو (امتدادات الساق 1.272/1.618).

الأوضاع:
  python sd_bot.py train     # يبني العيّنات من التاريخ ويدرّب النموذج (sd_model.joblib)
  python sd_bot.py scan      # يفحص آخر شمعة مغلقة ويرسل الإشارات لتيليجرام
  python sd_bot.py backtest  # باك-تست حقيقي يقارن التوقّع بين المنطق القديم والجديد
  python sd_bot.py both      # تدريب ثم فحص (الافتراضي)

تنبيه: أداة تحليل تعليمية. لا تنفّذ صفقات ولا تحرّك أموالاً. التداول مخاطرة، وليست نصيحة مالية.
"""
import os, sys, time, math, json, datetime as dt
import requests

# ----------------------- إعدادات -----------------------
CFG = dict(
    pivL=3, pivR=3, impK=1.0, base_max_body=0.6, base_max=3,
    atr_len=50, vol_len=200, ema_len=200, react_k=48,
    distal_buf_atr=0.1, ml_threshold=0.60,
    risk_pct=0.005, max_concurrent=5,           # إعداد E
    entry_tf="1h", htf="4h", pages_1h=4, pages_4h=2,
    top_n=8,
    # ── تحسينات الدخول (2026-07-04) — علاج «دخول متأخر ثم ضرب الوقف» ──
    fib_entry=0.618,       # الدخول عند تصحيح فيبو 61.8% لساق الاندفاع (لا عند قمة المنطقة)
    stop_buf_atr=0.5,      # الوقف تحت أصل المنطقة (distal) بمقدار ×ATR — خارج المنطقة لا داخلها
    tp2_ext=1.618,         # هدف2 = امتداد فيبو 1.618 لساق الاندفاع
    max_height_atr=2.0,    # رفض المناطق الفضفاضة: ارتفاع القاعدة > ×ATR
    max_bars_to_touch=60,  # رفض المناطق المسنّة: لمسة بعد أكثر من N شمعة من التكوين
    require_confirm=1,     # اشتراط شمعة تأكيد (إغلاق فوق أصل المنطقة وفي نصفها العلوي)
    # ── تشديد الدخول (2026-07-04) — «CHoCH في بداية الموجة + قرب من الاتجاه» ──
    require_choch=1,       # يدخل فقط إذا كسرت شمعة الاندفاع الهيكل صعوداً لأول مرة (انعكاس CHoCH)
    max_ema_dist=0.06,     # أقصى بُعد للدخول فوق EMA200 (قريب من فلتر الاتجاه لا متمدّد)؛ 0 = تعطيل
    # ── تشبّع شرائي بعد تشبّع بيعي (فوق CHoCH) — طلب بو محمد 2026-07-04 ──
    # مُعطّل افتراضياً: الباك-تست أظهر أنه يقصّ العدد ويخفض الحافة (وRSI≥70 يؤخّر الدخول لا يقدّمه).
    require_ob_after_os=0, # يشترط: تشبّع شرائي (RSI≥rsi_ob) في موجة الاندفاع مسبوق بتشبّع بيعي واحد+
    rsi_len=14, rsi_ob=70, rsi_os=30,   # طول RSI وعتبتا التشبّع الشرائي/البيعي (لميزة obos)
    os_lookback=100,       # نافذة البحث عن تشبّع بيعي قبل بدء موجة الاندفاع (شموع)
    # ── الدخول الجديد (2026-07-05، طلب بو محمد): زخم مباشر بعد CHoCH + توسيط DCA بفيبو ──
    rsi_entry_len=21,      # طول RSI لإشارة الدخول (بديل دخول فيبو 61.8%)
    rsi_entry_ob=80,       # عتبة التشبّع الشرائي للدخول المباشر (RSI21 ≥ 80 = تأكيد زخم)
    # سلالم فيبو للتوسيط تحت الدخول المباشر (نِسَب تصحيح لساق الاندفاع leg_low→leg_high)
    dca_fibs=(0.382, 0.5, 0.618, 0.786),
    # ── إدارة الخروج نظام ب (الفائزة في بوت الصيد): جني جزئي + وقف متحرّك شانديلير ──
    tp1_frac=0.5,          # نسبة الجني عند الهدف الأول ثم تتبّع الباقي
    trail_atr=2.5,         # مضاعف وقف شانديلير المتحرّك (قمة − trail_atr×ATR) للباقي
    bt_hold=48,            # (backtest) أقصى شموع لإمساك الصفقة
)
# ── تجاوز فريم الدخول/السياق عبر البيئة (لتشغيل البوت على كل الفريمات: 15m/1h/4h) ──
# مثال: SD_ENTRY_TF=15m SD_HTF=1h  |  SD_ENTRY_TF=4h SD_HTF=1d
CFG["entry_tf"] = os.environ.get("SD_ENTRY_TF", CFG["entry_tf"])
CFG["htf"]      = os.environ.get("SD_HTF", CFG["htf"])
CFG["bt_hold"]  = int(os.environ.get("SD_BT_HOLD", CFG["bt_hold"]))
CFG["pages_1h"] = int(os.environ.get("SD_PAGES", CFG["pages_1h"]))   # تقليل الصفحات = تسريع الجلب
CFG["require_choch"] = int(os.environ.get("SD_REQUIRE_CHOCH", CFG["require_choch"]))
CFG["max_ema_dist"]  = float(os.environ.get("SD_MAX_EMA_DIST", CFG["max_ema_dist"]))
CFG["trail_atr"]     = float(os.environ.get("SD_TRAIL_ATR", CFG["trail_atr"]))
CFG["tp1_frac"]      = float(os.environ.get("SD_TP1_FRAC", CFG["tp1_frac"]))
CFG["require_ob_after_os"] = int(os.environ.get("SD_REQUIRE_OBOS", CFG["require_ob_after_os"]))
CFG["os_lookback"]   = int(os.environ.get("SD_OS_LOOKBACK", CFG["os_lookback"]))
CFG["rsi_entry_len"] = int(os.environ.get("SD_RSI_ENTRY_LEN", CFG["rsi_entry_len"]))
CFG["rsi_entry_ob"]  = float(os.environ.get("SD_RSI_ENTRY_OB", CFG["rsi_entry_ob"]))
# نمط الدخول حسب الفريم (طلب بو محمد 2026-07-05): 15m = زخم RSI + توسيط DCA؛
# الساعة (وأعلى) = اختراق قمة الـCHoCH. قابل للتجاوز عبر SD_ENTRY_MODE (momentum/breakout).
CFG["entry_mode"] = os.environ.get(
    "SD_ENTRY_MODE", "momentum" if CFG["entry_tf"] == "15m" else "breakout")
# نافذة الحداثة: أقصى شموع بين تكوين المنطقة والدخول (تضييقها = دخول أبكر = أقل تأخّراً)
CFG["max_bars_to_touch"] = int(os.environ.get("SD_MAX_BARS", CFG["max_bars_to_touch"]))
BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
# ملفات النموذج/الحالة قابلة للتخصيص لكل فريم (لتفادي التضارب بين الفريمات)
MODEL_PATH = os.environ.get("SD_MODEL", "sd_model.joblib")
STATE_PATH = os.environ.get("SD_STATE", "sd_state.json")
WATCHLIST = "watchlist.txt"
MODEL_MAX_AGE_H = 24                  # يعيد التدريب إذا تجاوز عمر النموذج هذا
ML_KEYS = ["strength", "heightATR", "baseVolZ", "touchVolZ", "bos", "choch", "rsiObOs", "fvg", "sweep",
           "htf", "emaRel", "barsToTouch", "hour", "confirm", "closeLoc"]
# أسماء الأسرار نفسها التي يستخدمها workflow الحالي (sd_bot.yml)
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", os.environ.get("TG_TOKEN", ""))
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_CHAT", ""))

# سجل المتتبّع المشترك: نكتب فيه إشاراتنا لتظهر وتُتابَع في اللوحة مثل بقية البوتات.
# (يتابعها trackmon في reversal.yml كل 15 دقيقة ويُصدّر paper_data.json للوحة)
TRACK_FILE = "tracked_signals.json"
DASH_LABEL = "العرض/الطلب"
_TF_MS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000,
          "1h": 3600000, "2h": 7200000, "4h": 14400000, "1d": 86400000}

# ----------------------- جلب البيانات -----------------------
def fetch_klines(symbol, interval, pages=2):
    all_rows, end_time = [], None
    for _ in range(pages):
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        data = None
        for base in BINANCE_BASES:
            try:
                r = requests.get(f"{base}/api/v3/klines", params=params, timeout=12)
                if r.status_code == 200 and r.json():
                    data = r.json()
                    break
            except Exception:
                continue
        if not data:
            break
        all_rows = data + all_rows
        end_time = data[0][0] - 1
        if len(data) < 1000:
            break
    if not all_rows:
        return None
    m = {row[0]: row for row in all_rows}
    rows = sorted(m.values(), key=lambda x: x[0])
    return dict(
        t=[r[0] for r in rows], o=[float(r[1]) for r in rows], h=[float(r[2]) for r in rows],
        l=[float(r[3]) for r in rows], c=[float(r[4]) for r in rows], v=[float(r[5]) for r in rows])

# ----------------------- مؤشرات -----------------------
def atr(h, l, c, n):
    out = [float("nan")] * len(c); s = 0.0; tr = []
    for i in range(len(c)):
        t = (h[i] - l[i]) if i == 0 else max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        tr.append(t); s += t
        if i >= n: s -= tr[i-n]
        if i >= n-1: out[i] = s / n
    return out

def ema(arr, n):
    k = 2 / (n + 1); out = [float("nan")] * len(arr); prev = None
    for i, x in enumerate(arr):
        prev = x if i == 0 else x * k + prev * (1 - k)
        out[i] = prev
    return out

def rsi(c, n=14):
    """RSI بطريقة Wilder (تنعيم أُسّي). يعيد قائمة بطول c، أول n قيمة NaN."""
    out = [float("nan")] * len(c)
    if len(c) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = c[i] - c[i - 1]
        gains += max(ch, 0.0); losses += max(-ch, 0.0)
    ag = gains / n; al = losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n + 1, len(c)):
        ch = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out

def vol_z(v, L):
    out = [float("nan")] * len(v)
    for i in range(L - 1, len(v)):
        win = v[i - L + 1:i + 1]
        m = sum(win) / L
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / L)
        out[i] = (v[i] - m) / sd if sd > 0 else 0.0
    return out

def pivots(h, l, L, R):
    piv = []
    for i in range(L, len(h) - R):
        hi = lo = True
        for k in range(1, L + 1):
            if h[i] < h[i-k]: hi = False
            if l[i] > l[i-k]: lo = False
        for k in range(1, R + 1):
            if h[i] < h[i+k]: hi = False
            if l[i] > l[i+k]: lo = False
        if hi: piv.append((i, h[i], "H"))
        if lo: piv.append((i, l[i], "L"))
    piv.sort(key=lambda x: x[0])
    return piv

def structure(h, l, c, L, R):
    piv = pivots(h, l, L, R); events = []
    ref_h = ref_l = None; bias = 0; pidx = 0
    for i in range(len(c)):
        while pidx < len(piv) and piv[pidx][0] + R <= i:
            p = piv[pidx]; pidx += 1
            if p[2] == "H": ref_h = p
            else: ref_l = p
        if ref_h and c[i] > ref_h[1]:
            events.append((i, "up")); bias = 1; ref_h = None
        elif ref_l and c[i] < ref_l[1]:
            events.append((i, "dn")); bias = -1; ref_l = None
    return piv, events

def choch_ups(events):
    """مؤشّرات الشموع التي حصل عندها تغيّر هيكل صاعد (CHoCH-up):
    أول كسر صاعد بعد كسر هابط — أي انعكاس بداية موجة، لا مجرد استمرار (BOS).
    نميّز CHoCH عن BOS: 'up' يسبقها 'dn' = انعكاس؛ 'up' يسبقها 'up' = استمرار."""
    s = set(); prev = None
    for i, k in events:
        if k == "up" and prev == "dn":
            s.add(i)
        prev = k
    return s

def choch_high_levels(c, piv, R):
    """يعيد dict: {مؤشّر شمعة CHoCH-up → سعر القمة المحورية المكسورة} = «قمة الـCHoCH».
    نكرّر منطق structure ونسجّل مستوى القمة (ref_h) عند كل كسر صاعد يسبقه كسر هابط.
    يُستخدم لدخول الاختراق على فريم الساعة: الدخول عند كسر السعر قمة الـCHoCH."""
    ref_h = ref_l = None; pidx = 0; prev = None; out = {}
    for i in range(len(c)):
        while pidx < len(piv) and piv[pidx][0] + R <= i:
            p = piv[pidx]; pidx += 1
            if p[2] == "H": ref_h = p
            else: ref_l = p
        if ref_h and c[i] > ref_h[1]:
            if prev == "dn":
                out[i] = ref_h[1]           # مستوى قمة الـCHoCH المكسورة
            prev = "up"; ref_h = None
        elif ref_l and c[i] < ref_l[1]:
            prev = "dn"; ref_l = None
    return out

def demand_zones(o, h, l, c, v, a):
    zones = []
    for j in range(2, len(c)):
        A = a[j]
        if not (A and A > 0):
            continue
        body = c[j] - o[j]
        if not (c[j] > o[j] and body > CFG["impK"] * A):
            continue
        base = []; k = j - 1
        while k >= 0 and len(base) < CFG["base_max"] and abs(c[k] - o[k]) < CFG["base_max_body"] * A:
            base.append(k); k -= 1
        if not base:
            continue
        top = max(max(o[x], c[x], h[x]) for x in base)
        bot = min(l[x] for x in base)
        if not (top > bot):
            continue
        zones.append(dict(created=j, proximal=top, distal=bot, height=top - bot,
                          strength=round(body / A, 2)))
    return zones

def htf_bias_fn(d4):
    _, ev = structure(d4["h"], d4["l"], d4["c"], CFG["pivL"], CFG["pivR"])
    pts = [(d4["t"][i], 1 if k == "up" else -1) for i, k in ev]
    def f(ts):
        b = 0
        for t, u in pts:
            if t <= ts: b = u
            else: break
        return b
    return f

# ----------------------- خطة الدخول (زخم مباشر + توسيط DCA بفيبو) -----------------------
def _wave_low(z, l, low_idx):
    """قاع الموجة الكاملة التي صنعت CHoCH: أدنى قاع من آخر قاع محوري (swing-low) قبل شمعة
    الاندفاع حتى الاندفاع نفسه — لا مجرد قاعدة المنطقة (طلب بو محمد 2026-07-05)."""
    j = z["created"]; origin = 0
    for idx in low_idx:                 # low_idx مرتّبة تصاعدياً
        if idx <= j: origin = idx
        else: break
    wl = min(l[origin:j + 1]) if j + 1 > origin else l[j]
    return min(wl, z["distal"])         # لا يعلو عن قاعدة المنطقة

def _entry_plan(z, h, l, c, a, rs_en, low_idx, stop_buf, choch_hi=None, mode=None):
    """الدخول حسب الفريم (طلب بو محمد 2026-07-05):
      • momentum (15m): أول شمعة بعد CHoCH يبلغ فيها RSI(entry) التشبّع الشرائي ≥ العتبة =
        دخول زخم مباشر بإغلاقها + سلالم توسيط DCA بفيبو أسفله.
      • breakout (1h+): الدخول عند كسر السعر «قمة الـCHoCH» (القمة المحورية المكسورة) =
        دخول اختراق مباشر واحد بلا توسيط.
    في الحالتين: الوقف عند قاع الموجة الكاملة − هامش ATR، والأهداف امتدادات فيبو للساق.
    يعيد None إذا لم يتحقّق دخول صالح."""
    mode = mode or CFG["entry_mode"]
    j = z["created"]
    leg_low = _wave_low(z, l, low_idx)
    run_high = max(z["proximal"], h[j]); tch = -1; entry = leg_high = 0.0; peak_idx = j
    if mode == "breakout":
        lvl = None                                       # قمة الـCHoCH قرب التكوين
        for cb in (j, j + 1, j + 2):
            if choch_hi and cb in choch_hi:
                lvl = choch_hi[cb]; break
        if lvl is None:
            return None
        for i in range(j + 1, len(c)):
            if h[i - 1] > run_high:
                run_high = h[i - 1]; peak_idx = i - 1
            if h[i] >= lvl:                              # كسر السعر قمة الـCHoCH → دخول اختراق
                tch = i; leg_high = max(run_high, h[i]); entry = lvl; break
    else:                                                # momentum (15m)
        for i in range(j + 1, len(c)):
            if h[i - 1] > run_high:
                run_high = h[i - 1]; peak_idx = i - 1
            if math.isfinite(rs_en[i]) and rs_en[i] >= CFG["rsi_entry_ob"]:
                tch = i; leg_high = max(run_high, h[i]); entry = c[i]; break
    if tch < 0:
        return None
    atch = a[tch] or (leg_high - leg_low)
    stop = leg_low - stop_buf * atch                    # قاع الموجة الكاملة − ATR
    if not (entry - stop > 0):
        return None
    span = leg_high - leg_low
    if mode == "breakout":
        legs = [entry]                                  # الساعة: دخول اختراق واحد بلا توسيط
    else:
        ladder = [leg_high - lv * span for lv in CFG["dca_fibs"]]   # سلالم توسيط DCA بفيبو
        legs = [entry] + [p for p in ladder if stop < p < entry]    # بين الوقف والدخول فقط
    _exts = [leg_low + m * span for m in (1.272, CFG["tp2_ext"], 2.0, 2.618)]  # أهداف امتداد فيبو
    _above = [x for x in _exts if x > entry]
    tp1, tp2 = (_above[0], _above[1]) if len(_above) >= 2 else (entry + 0.618 * span, entry + span)
    return dict(tch=tch, entry=entry, stop=stop, tp1=tp1, tp2=tp2, legs=legs,
                leg_low=leg_low, leg_high=leg_high, peak_idx=peak_idx, atch=atch)

# ----------------------- ميزات الإعداد -----------------------
def setup_features(sym, d1, d4):
    o, h, l, c, v, t = d1["o"], d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
    a = atr(h, l, c, CFG["atr_len"]); vz = vol_z(v, CFG["vol_len"]); e200 = ema(c, CFG["ema_len"])
    rs = rsi(c, CFG["rsi_len"])
    rs_en = rsi(c, CFG["rsi_entry_len"])          # RSI(21) لإشارة الدخول الزخمي
    piv, ev = structure(h, l, c, CFG["pivL"], CFG["pivR"])
    low_idx = [p[0] for p in piv if p[2] == "L"]  # مؤشّرات القيعان المحورية (لقاع الموجة)
    choch_hi = choch_high_levels(c, piv, CFG["pivR"])   # مستويات قمم الـCHoCH (لدخول الاختراق)
    bos_up = set(i for i, k in ev if k == "up")
    choch_up = choch_ups(ev)
    hb = htf_bias_fn(d4); zones = demand_zones(o, h, l, c, v, a)
    out = []
    for z in zones:
        j = z["created"]
        plan = _entry_plan(z, h, l, c, a, rs_en, low_idx, CFG["stop_buf_atr"], choch_hi)
        if plan is None:
            continue
        tch = plan["tch"]; entry = plan["entry"]; stop = plan["stop"]
        tp1 = plan["tp1"]; tp2 = plan["tp2"]; legs = plan["legs"]
        leg_low = plan["leg_low"]; leg_high = plan["leg_high"]; peak_idx = plan["peak_idx"]
        R = entry - stop
        # شمعة التأكيد: أغلقت فوق أصل المنطقة (لم تكسرها) وفي نصفها العلوي (رفض/ارتداد لا اختراق).
        rng = h[tch] - l[tch]
        close_loc = ((c[tch] - l[tch]) / rng) if rng > 0 else 0.0
        confirm = 1 if (c[tch] > leg_low and close_loc >= 0.5) else 0
        fvg = 1 if l[j] > h[j-2] else 0
        bos = 1 if (j in bos_up or (j+1) in bos_up or (j+2) in bos_up) else 0
        # CHoCH: هل كسرت شمعة الاندفاع (أو التالية) الهيكل صعوداً لأول مرة (انعكاس بداية موجة)؟
        choch = 1 if (j in choch_up or (j+1) in choch_up or (j+2) in choch_up) else 0
        # تشبّع شرائي بعد بيعي: هل بلغت موجة الاندفاع (j..قمة) تشبّعاً شرائياً (RSI≥ob)
        # مسبوقاً بتشبّع بيعي (RSI≤os) واحد أو أكثر في النافذة قبل بدء الموجة؟
        ob_after = 1 if any(math.isfinite(rs[x]) and rs[x] >= CFG["rsi_ob"]
                            for x in range(j, min(peak_idx, tch) + 1)) else 0
        os_lo = max(1, j - CFG["os_lookback"])
        os_before = 1 if any(math.isfinite(rs[x]) and rs[x] <= CFG["rsi_os"]
                             for x in range(os_lo, j + 1)) else 0
        rsi_obos = 1 if (ob_after and os_before) else 0
        lo, hi = max(0, j - 30), max(1, j - 5)
        prior_low = min(l[lo:hi]) if hi > lo else l[j]
        sweep = 1 if z["distal"] < prior_low else 0
        ema_rel = (c[tch] - e200[tch]) / e200[tch] if e200[tch] else 0.0
        f = dict(strength=z["strength"],
                 heightATR=round((z["proximal"] - z["distal"]) / (a[j] or R), 2),
                 baseVolZ=round(vz[j] or 0, 2), touchVolZ=round(vz[tch] or 0, 2),
                 bos=bos, choch=choch, rsiObOs=rsi_obos, fvg=fvg, sweep=sweep, htf=hb(t[tch]),
                 emaRel=round(ema_rel, 4), barsToTouch=tch - j,
                 hour=dt.datetime.fromtimestamp(t[tch] / 1000, dt.timezone.utc).hour,
                 confirm=confirm, closeLoc=round(close_loc, 2))
        out.append(dict(sym=sym, created=j, touch=tch, ts=t[tch], f=f,
                        entry=entry, stop=stop, tp1=tp1, tp2=tp2, legs=legs,
                        height=leg_high - leg_low))
    return out, h, l, c

# ----------------------- تسمية للتدريب -----------------------
def label_setup(s, h, l, c):
    # هدف التدريب: هل تحقّق الهدف الأول (قمة الاندفاع، فيبو) قبل ضرب الوقف؟
    entry, stop, tch = s["entry"], s["stop"], s["touch"]
    tgt = s.get("tp1", entry + s["height"])
    if entry - stop <= 0:
        return None
    end = min(len(c), tch + CFG["react_k"])
    for i in range(tch, end):
        if l[i] <= stop: return 0
        if h[i] >= tgt: return 1
    return None

# ----------------------- قائمة الكريبتو -----------------------
def parse_watchlist_crypto(path):
    try:
        raw = open(path, encoding="utf-8").read()
    except Exception:
        return []
    toks = []
    for line in raw.splitlines():
        if "\t" in line:
            line = line.split("\t", 1)[1]
        toks += [x.strip() for x in line.split(",") if x.strip()]
    seen, uniq = set(), []
    for tok in toks:
        if tok.startswith("#") or ":" not in tok:
            continue
        exch, sym = tok.split(":", 1); sym = sym.strip().upper()
        if exch.upper() in {"BINANCE", "BYBIT", "MEXC", "BINANCEUS"} and sym.endswith("USDT"):
            if sym not in seen:
                seen.add(sym); uniq.append(sym)
    return uniq

# ----------------------- تدريب النموذج -----------------------
def train(basket=None):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    import joblib
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:40]
    X, Y = [], []
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = setup_features(s, d1, d4)
            for st in setups:
                y = label_setup(st, h, l, c)
                if y is None:
                    continue
                feat = [st["f"][k] for k in ML_KEYS]
                # تجاهل العيّنات ذات الميزات غير المنتهية (NaN/inf من فترة إحماء ATR/الحجم/المتوسط)
                if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in feat):
                    continue
                X.append(feat); Y.append(y)
        except Exception as ex:
            print("train skip", s, ex)
        time.sleep(0.05)
    if len(X) < 200:
        print("not enough samples:", len(X)); return None
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=1000))
    model.fit(X, Y)
    joblib.dump(dict(model=model, keys=ML_KEYS, trained=len(X),
                     date=dt.date.today().isoformat()), MODEL_PATH)
    print(f"trained on {len(X)} setups, base_win={sum(Y)/len(Y):.3f} -> {MODEL_PATH}")
    return model

# ----------------------- الفحص الحيّ + الإشارات -----------------------
def load_model():
    import joblib
    return joblib.load(MODEL_PATH) if os.path.exists(MODEL_PATH) else None

def model_is_fresh():
    # نعتمد على التاريخ المخزّن داخل النموذج لا وقت الملف (git checkout يعيد ضبط الوقت)
    # فيُعاد التدريب مرة واحدة يومياً (أول تشغيل في يوم جديد).
    if not os.path.exists(MODEL_PATH):
        return False
    try:
        import joblib
        b = joblib.load(MODEL_PATH)
        # يعاد التدريب إذا تغيّرت الميزات (عدد/أسماء) كي لا ينهار التنبّؤ بنموذج قديم
        if b.get("keys") != ML_KEYS:
            return False
        return b.get("date") == dt.date.today().isoformat()
    except Exception:
        return False

def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {"sent": []}

def save_state(state):
    state["sent"] = state.get("sent", [])[-800:]   # نحتفظ بآخر 800 مفتاح فقط
    try:
        json.dump(state, open(STATE_PATH, "w"))
    except Exception as ex:
        print("state save error", ex)

def scan(basket=None):
    bundle = load_model()
    if bundle and bundle.get("keys") != ML_KEYS:
        print("model feature-set changed -> retraining"); train(); bundle = load_model()
    if not bundle:
        print("no model; run train first"); return []
    model = bundle["model"]
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:60]
    state = load_state(); sent = set(state.get("sent", []))
    signals = []
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], 2)
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 300:
                continue
            setups, h, l, c = setup_features(s, d1, d4)
            last = len(c) - 1
            for st in setups:
                if st["touch"] != last:        # الدخول تحقّق على الشمعة المغلقة الأخيرة فقط
                    continue
                f = st["f"]
                if f["emaRel"] <= 0:           # فلتر E: فوق EMA200
                    continue
                if CFG["max_ema_dist"] and f["emaRel"] > CFG["max_ema_dist"]:  # قريب من الاتجاه لا متمدّد
                    continue
                if f["htf"] < 0:               # فلتر E: 4h غير هابط
                    continue
                if CFG["require_choch"] and not f["choch"]:  # CHoCH إلزامي: بداية موجة/انعكاس لا استمرار
                    continue
                if CFG["require_ob_after_os"] and not f["rsiObOs"]:  # تشبّع شرائي بعد بيعي (فوق CHoCH)
                    continue
                if CFG["require_confirm"] and not f["confirm"]:   # شمعة تأكيد إلزامية
                    continue
                if f["heightATR"] > CFG["max_height_atr"]:        # رفض المناطق الفضفاضة
                    continue
                if f["barsToTouch"] > CFG["max_bars_to_touch"]:   # رفض المناطق المسنّة
                    continue
                key = f"{s}:{st['ts']}"          # منع التكرار: نفس اللمسة لا تُرسل مرتين
                if key in sent:
                    continue
                feat = [f[k] for k in ML_KEYS]
                # تخطّي الإعدادات ذات الميزات غير المنتهية (NaN/inf) كي لا ينهار التنبّؤ
                if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in feat):
                    continue
                prob = model.predict_proba([feat])[0][1]
                if prob < CFG["ml_threshold"]:
                    continue
                entry, stop = st["entry"], st["stop"]
                signals.append(dict(key=key, sym=s, prob=round(float(prob), 3),
                    tf=CFG["entry_tf"],
                    entry=round(entry, 8), stop=round(stop, 8),
                    legs=[round(p, 8) for p in st.get("legs", [entry])],
                    tp1=round(st["tp1"], 8), tp2=round(st["tp2"], 8), ts=st["ts"],
                    reasons=_reasons(f)))
        except Exception as ex:
            print("scan skip", s, ex)
        time.sleep(0.05)
    signals.sort(key=lambda x: x["prob"], reverse=True)
    signals = signals[:CFG["top_n"]]
    if signals:
        mid = send_telegram(format_message(signals))
        track_for_dashboard(signals, mid)        # تظهر في لوحة المتتبّع مثل بقية البوتات
        for sig in signals:
            state.setdefault("sent", []).append(sig["key"])
        save_state(state)
    else:
        print("no signals this scan")
    return signals

def _reasons(f):
    r = [f"دخول زخم RSI{CFG['rsi_entry_len']}≥{CFG['rsi_entry_ob']:.0f} + توسيط DCA بفيبو"
         if CFG["entry_mode"] == "momentum" else "دخول اختراق قمة الـCHoCH"]
    if f.get("choch"): r.append("تغيّر هيكل CHoCH (بداية موجة)")
    if f.get("rsiObOs"): r.append("تشبّع شرائي بعد بيعي (RSI)")
    if f.get("confirm"): r.append("شمعة تأكيد/ارتداد")
    if f["sweep"]: r.append("اصطياد سيولة")
    if f["bos"]: r.append("كسر بنية صاعد")
    if f["htf"] > 0: r.append("4h صاعد")
    if 0 < f["emaRel"] <= CFG["max_ema_dist"]: r.append("قريب من متوسط 200")
    elif f["emaRel"] > 0: r.append("فوق متوسط 200")
    if f["baseVolZ"] >= 1: r.append("حجم قوي عند القاعدة")
    return r

def _fmt(v):
    """تنسيق السعر بنفس أسلوب بقية البوتات (دقّة أعلى للأسعار الصغيرة)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"


def format_message(signals):
    """بطاقة تيليجرام بنفس نسق بقية البوتات (انعكاس/RSI70/trendwave):
    رمز·فريم، ثقة الفلتر، أسباب، دخول، وقف بنسبة −%، أهداف بنسبة +%، مخاطرة، وقت."""
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    now = dt.datetime.now().strftime("%H:%M:%S")
    tf = CFG["entry_tf"]
    sep = "\n➖➖➖➖➖➖➖➖➖\n"
    blocks = []
    for s in signals:
        entry, stop = s["entry"], s["stop"]
        legs = s.get("legs") or [entry]
        avg = sum(legs) / len(legs)                      # متوسط لو امتلأت كل السلالم
        risk_pct = ((entry - stop) / entry * 100) if entry else 0.0
        tps = [t for t in (s.get("tp1"), s.get("tp2")) if t]
        lines = [
            "🟢 إشارة العرض/الطلب — شراء",
            f"💎 {s['sym']} · ⏱️ {tf}",
            f"🤖 ثقة الفلتر التعلّمي (عرض/طلب + ML): {int(s['prob']*100)}%",
            f"📊 الأسباب: {'، '.join(s['reasons'])}",
            "",
            (f"📍 الدخول المباشر (زخم): {_fmt(entry)}" if CFG["entry_mode"] == "momentum"
             else f"📍 الدخول (اختراق قمة CHoCH): {_fmt(entry)}"),
        ]
        if len(legs) > 1:
            lines.append("➕ سلالم التوسيط DCA بفيبو: " + " · ".join(_fmt(p) for p in legs[1:]))
            lines.append(f"⚖️ المتوسط لو امتلأت السلالم: {_fmt(avg)}")
        lines += [
            f"🛑 الوقف (قاع الموجة): {_fmt(stop)}  (−{risk_pct:.2f}%)",
            "",
            "🎯 الأهداف:",
        ]
        for k, t in enumerate(tps):
            gain = ((t - entry) / entry * 100) if entry else 0.0
            n = nums[k] if k < len(nums) else f"{k+1})"
            lines.append(f"{n} {_fmt(t)}  (+{gain:.2f}%)")
        lines += [
            "",
            f"⚖️ المخاطرة لكل صفقة: {risk_pct:.2f}% من الدخول · إدارة 50/50 "
            "(جني 50% عند الهدف الأول + تعادل + وقف متحرّك)",
            f"⏰ {now}",
        ]
        blocks.append("\n".join(lines))
    header = ("📊 <b>إشارات العرض/الطلب (v3) — شراء</b>\n"
              "<i>CHoCH بداية الموجة · قريب من الاتجاه · مخاطرة مقترحة 0.5% · حد 5 مراكز</i>")
    footer = "⚠️ تحليل تعليمي — ليس نصيحة مالية"
    return header + sep + sep.join(blocks) + sep + footer

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("TG not configured; message:\n", text); return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
        print(f"sent {text.count(chr(0x1F7E2))} signals to telegram")
        return (r.json().get("result") or {}).get("message_id")
    except Exception as ex:
        print("telegram error", ex)
        return None


def track_for_dashboard(signals, message_id, tf=None, path=TRACK_FILE):
    """يسجّل إشارات هذا الفحص في tracked_signals.json بنفس صيغة بقية البوتات،
    فتظهر وتُتابَع في لوحة المتتبّع (إدارة 50/50: هدف1 +1R جني 50%+تعادل، هدف2 +2R).
    لا يمسّ إشارات البوتات الأخرى — يُضيف فقط ويُنظّف إشاراته القديمة (>14 يوماً)."""
    tf = tf or CFG["entry_tf"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    # تنظيف إشارات هذا البوت القديمة فقط (دون المساس بإشارات البوتات الأخرى)
    cutoff = (dt.datetime.now() - dt.timedelta(days=14)).isoformat()
    data = {k: v for k, v in data.items()
            if not (isinstance(v, dict) and v.get("label") == DASH_LABEL
                    and v.get("created", "") < cutoff)}
    added = 0
    for s in signals:
        entry, stop, tp1 = s["entry"], s["stop"], s["tp1"]
        R = entry - stop
        if R <= 0:
            continue
        tp2 = s.get("tp2") or round(entry + 2 * R, 8)   # أهداف فيبو من الإشارة
        bar_ts = dt.datetime.fromtimestamp(s["ts"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        key = f"{DASH_LABEL}|{s['sym']}|{bar_ts}"
        if key in data:
            continue
        data[key] = {
            "symbol": s["sym"], "label": DASH_LABEL, "timeframe": tf,
            "message_id": message_id,
            "entry": entry, "stop": stop, "init_stop": stop, "cur_stop": stop,
            "legs": s.get("legs", [entry]),          # سلالم DCA للتوسيط (للتنفيذ الحيّ)
            "last_alert_stop": stop, "armed": False,
            "targets": [tp1, tp2], "tp_split": [50, 50],
            "is_trendwave": False, "mgmt": "5050", "breakeven_done": False,
            "bar_ts": bar_ts, "last_bar": bar_ts,
            "hits": [], "stopped": False, "hi_seen": entry, "lo_seen": entry,
            "created": dt.datetime.now().isoformat(timespec="seconds"),
        }
        added += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"tracked {added} signals to {path}")

# ----------------------- باك-تست حقيقي (مقارنة القديم/الجديد) -----------------------
def _dca_average(legs, stop, tp1, h, l, c, tch, hold):
    """متوسط دخول DCA بمحاكاة مسار: الساق الأولى تُملأ عند الدخول المباشر، وتُملأ سلالم فيبو
    التي يهبط إليها السعر (حتى أدنى قاع) قبل بلوغ الهدف الأول أو الوقف. أوزان متساوية.
    إن لم يرتدّ السعر لأي سلّم = المتوسط هو الدخول المباشر نفسه (لا أفضلية توسيط)."""
    if not legs:
        return None
    end = min(len(c), tch + hold)
    lo_seen = l[tch]
    for i in range(tch, end):
        if l[i] < lo_seen:
            lo_seen = l[i]
        if l[i] <= stop or h[i] >= tp1:      # يتوقّف الامتلاء عند الوقف أو أول هدف
            break
    filled = [legs[0]] + [p for p in legs[1:] if p >= lo_seen]   # سلّم يُملأ إذا وصله السعر
    return sum(filled) / len(filled)

def _sim_5050(entry, stop, tp1, tp2, h, l, c, tch, hold):
    """يحاكي صفقة بإدارة 50/50: نصف عند الهدف1 ثم نقل الوقف للتعادل، والباقي للهدف2.
    يعيد الناتج بوحدات المخاطرة R. (عند تعارض الوقف والهدف بنفس الشمعة نُرجّح الوقف تحفّظاً.)"""
    R = entry - stop
    if R <= 0:
        return None
    r1, r2 = (tp1 - entry) / R, (tp2 - entry) / R
    if r1 <= 0:
        return None
    end = min(len(c), tch + hold)
    half = False; sl = stop
    for i in range(tch, end):
        if l[i] <= sl:
            return 0.5 * r1 if half else -1.0     # نصف مجنيّ + نصف عند التعادل = 0
        if not half and h[i] >= tp1:
            half = True; sl = entry               # نقل الوقف للتعادل بعد جني النصف
        if half and h[i] >= tp2:
            return 0.5 * r1 + 0.5 * r2
    r_last = (c[end - 1] - entry) / R             # إغلاق زمني على آخر شمعة
    return (0.5 * r1 + 0.5 * r_last) if half else r_last

def _sim_trailb(entry, stop, tp1, av, h, l, c, tch, hold):
    """نظام ب (الفائز في بوت الصيد): جني tp1_frac عند الهدف الأول، ثم وقف متحرّك
    شانديلير (قمة − trail_atr×ATR) للباقي لا ينزل عن التعادل — يركب امتداد الترند
    بدل خروج ثابت عند هدف2. av = ATR عند الدخول. الناتج بوحدات R."""
    R = entry - stop
    if R <= 0 or av <= 0:
        return None
    r1 = (tp1 - entry) / R
    if r1 <= 0:
        return None
    k = CFG["trail_atr"]; f1 = CFG["tp1_frac"]
    end = min(len(c), tch + hold)
    sl = stop; peak = entry; took1 = False; realized = 0.0
    for i in range(tch, end):
        peak = max(peak, h[i])
        if took1:
            sl = max(sl, peak - k * av, entry)    # التتبّع مفعّل بعد الجني، لا ينزل عن التعادل
        if l[i] <= sl:                            # ضرب الوقف (تحفّظاً نرجّحه قبل الهدف بنفس الشمعة)
            frac = 1.0 - (f1 if took1 else 0.0)
            return realized + frac * ((sl - entry) / R)
        if not took1 and h[i] >= tp1:
            took1 = True; realized += f1 * r1; sl = max(sl, entry)
    frac = 1.0 - (f1 if took1 else 0.0)
    return realized + frac * ((c[end - 1] - entry) / R)

def _stats(rs):
    if not rs:
        return "لا صفقات"
    n = len(rs); wins = [x for x in rs if x > 0]; losses = [x for x in rs if x <= 0]
    wr = len(wins) / n * 100
    gp = sum(wins); gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else float("inf")
    exp = sum(rs) / n
    return (f"صفقات={n} · فوز={wr:.1f}% · توقّع={exp:+.3f}R · "
            f"PF={pf:.2f} · مجموع={sum(rs):+.1f}R")

def backtest(basket=None):
    """يبني الإعدادات على بيانات حقيقية ويقارن التوقّع بين المنطق القديم والجديد (بلا ML — حافة خام).
       القديم: دخول=قمة المنطقة، وقف=distal−0.1ATR، أهداف +1R/+2R، بلا تأكيد.
       الجديد: دخول فيبو 61.8%، وقف خارج المنطقة، أهداف فيبو، مع تأكيد+فلاتر."""
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:40]
    hold = CFG["bt_hold"]; old_rs, new_rs, new_b_rs = [], [], []
    print(f"backtest SD | tf={CFG['entry_tf']} htf={CFG['htf']} | {len(basket)} رمز | hold={hold}")
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            o, h, l, c, v, t = d1["o"], d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
            a = atr(h, l, c, CFG["atr_len"]); e200 = ema(c, CFG["ema_len"])
            hb = htf_bias_fn(d4)
            # ── المنطق الجديد (نفس setup_features بكل الفلاتر) ──
            setups, _, _, _ = setup_features(s, d1, d4)
            for st in setups:
                f = st["f"]
                if f["emaRel"] <= 0 or f["htf"] < 0:
                    continue
                if CFG["max_ema_dist"] and f["emaRel"] > CFG["max_ema_dist"]:
                    continue
                if CFG["require_choch"] and not f["choch"]:
                    continue
                if CFG["require_ob_after_os"] and not f["rsiObOs"]:
                    continue
                if CFG["require_confirm"] and not f["confirm"]:
                    continue
                if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
                    continue
                tch = st["touch"]
                # متوسط دخول DCA (الساق المباشرة + سلالم الفيبو المُمتلئة) بدل دخول مفرد
                avg = _dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"], h, l, c, tch, hold)
                r = _sim_5050(avg, st["stop"], st["tp1"], st["tp2"], h, l, c, tch, hold)
                if r is not None:
                    new_rs.append(r)
                av = a[tch] or (st["tp1"] - avg)           # ATR عند الدخول لوقف شانديلير
                rb = _sim_trailb(avg, st["stop"], st["tp1"], av, h, l, c, tch, hold)
                if rb is not None:
                    new_b_rs.append(rb)
            # ── المنطق القديم (دخول عند proximal، وقف داخل المنطقة، بلا تأكيد) ──
            zones = demand_zones(o, h, l, c, v, a)
            for z in zones:
                j = z["created"]; tch = -1
                for i in range(j + 1, len(c)):
                    if l[i] <= z["proximal"]:
                        tch = i; break
                if tch < 0:
                    continue
                entry = z["proximal"]; stop = z["distal"] - CFG["distal_buf_atr"] * (a[tch] or z["height"])
                R = entry - stop
                if R <= 0:
                    continue
                ema_rel = (c[tch] - e200[tch]) / e200[tch] if e200[tch] else 0.0
                if ema_rel <= 0 or hb(t[tch]) < 0:     # نفس فلترَي القديم فقط
                    continue
                r = _sim_5050(entry, stop, entry + R, entry + 2 * R, h, l, c, tch, hold)
                if r is not None:
                    old_rs.append(r)
        except Exception as ex:
            print("bt skip", s, ex)
        time.sleep(0.03)
    entry_desc = (f"زخم RSI{CFG['rsi_entry_len']}≥{CFG['rsi_entry_ob']:.0f}+DCA فيبو"
                  if CFG["entry_mode"] == "momentum" else "اختراق قمة CHoCH")
    report = ("📊 مقارنة باك-تست العرض/الطلب (حافة خام بلا ML)\n"
              f"الفريم: دخول {CFG['entry_tf']} / سياق {CFG['htf']} · "
              f"{entry_desc} · وقف قاع الموجة · "
              f"CHoCH · قرب≤{CFG['max_ema_dist']:.0%} · حداثة≤{CFG['max_bars_to_touch']} شمعة\n"
              f"— القديم (5050): {_stats(old_rs)}\n"
              f"— الجديد (5050): {_stats(new_rs)}\n"
              f"— الجديد (نظام ب شانديلير {CFG['trail_atr']}×ATR): {_stats(new_b_rs)}")
    print("\n" + report)
    send_telegram(report)
    return old_rs, new_rs, new_b_rs

def _precompute(sym, d1, d4):
    """يحسب المؤشّرات الثقيلة والمناطق مرة واحدة لكل رمز (مستقلّة عن معاملات فيبو/الوقف)."""
    o, h, l, c, v, t = d1["o"], d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
    a = atr(h, l, c, CFG["atr_len"]); vz = vol_z(v, CFG["vol_len"]); e200 = ema(c, CFG["ema_len"])
    rsi_arr = rsi(c, CFG["rsi_len"]); rsi_en = rsi(c, CFG["rsi_entry_len"])
    piv, ev = structure(h, l, c, CFG["pivL"], CFG["pivR"])
    low_idx = [p[0] for p in piv if p[2] == "L"]
    choch_hi = choch_high_levels(c, piv, CFG["pivR"])
    bos_up = set(i for i, k in ev if k == "up")
    choch_up = choch_ups(ev)
    hb = htf_bias_fn(d4); zones = demand_zones(o, h, l, c, v, a)
    return dict(o=o, h=h, l=l, c=c, v=v, t=t, a=a, vz=vz, e200=e200, rsi=rsi_arr,
                rsi_entry=rsi_en, low_idx=low_idx, choch_hi=choch_hi,
                bos_up=bos_up, choch_up=choch_up, hb=hb, zones=zones)

def _eval_combo(P, fib_entry, stop_buf_atr):
    """يقيّم هامش الوقف على بيانات مُحسَّبة مسبقاً (fib_entry متروك للتوافق —
    الدخول الآن زخم RSI + توسيط DCA بفيبو، والوقف عند قاع الموجة الكاملة)."""
    h, l, c, t, a, e200, hb = P["h"], P["l"], P["c"], P["t"], P["a"], P["e200"], P["hb"]
    choch_up = P["choch_up"]; rs_en = P["rsi_entry"]; low_idx = P["low_idx"]; choch_hi = P["choch_hi"]
    hold = CFG["bt_hold"]; rs = []
    for z in P["zones"]:
        j = z["created"]
        if CFG["require_choch"] and not (j in choch_up or (j+1) in choch_up or (j+2) in choch_up):
            continue
        plan = _entry_plan(z, h, l, c, a, rs_en, low_idx, stop_buf_atr, choch_hi)
        if plan is None:
            continue
        tch = plan["tch"]; entry = plan["entry"]; stop = plan["stop"]
        ema_rel = (c[tch] - e200[tch]) / e200[tch] if e200[tch] else 0.0
        if ema_rel <= 0 or hb(t[tch]) < 0:
            continue
        if CFG["max_ema_dist"] and ema_rel > CFG["max_ema_dist"]:
            continue
        rng = h[tch] - l[tch]; close_loc = ((c[tch] - l[tch]) / rng) if rng > 0 else 0.0
        if CFG["require_confirm"] and not (c[tch] > plan["leg_low"] and close_loc >= 0.5):
            continue
        R = entry - stop
        height_atr = (z["proximal"] - z["distal"]) / (a[j] or R)
        if height_atr > CFG["max_height_atr"] or (tch - j) > CFG["max_bars_to_touch"]:
            continue
        avg = _dca_average(plan["legs"], stop, plan["tp1"], h, l, c, tch, hold)
        r = _sim_5050(avg, stop, plan["tp1"], plan["tp2"], h, l, c, tch, hold)
        if r is not None:
            rs.append(r)
    return rs

def sweep(basket=None):
    """مسح معاملات: يجرّب توليفات (نسبة فيبو للدخول × هامش الوقف) ويرتّبها بالتوقّع.
       يجلب البيانات ويحسب المؤشّرات مرة واحدة لكل رمز، ثم كل توليفة شبه فورية (بلا ML)."""
    n_sym = int(os.environ.get("SD_SWEEP_SYMS", "30"))
    basket = (basket or parse_watchlist_crypto(WATCHLIST))[:n_sym]
    fib_grid = [0.5, 0.618, 0.705, 0.786]
    stop_grid = [0.3, 0.5, 0.8]
    min_trades = int(os.environ.get("SD_SWEEP_MINTR", "40"))
    min_bars = min(800, CFG["pages_1h"] * 1000 - 50)   # يتوافق مع عدد الصفحات المطلوب
    print(f"sweep SD | tf={CFG['entry_tf']} htf={CFG['htf']} | جلب+حساب {len(basket)} رمز "
          f"({CFG['pages_1h']}×1000 شمعة) مرة واحدة...", flush=True)
    precomp = []
    for idx, s in enumerate(basket, 1):
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
            d4 = fetch_klines(s, CFG["htf"], 1)     # صفحة واحدة للسياق تكفي وتُسرّع
            if d1 and d4 and len(d1["c"]) >= min_bars:
                precomp.append(_precompute(s, d1, d4))
        except Exception as ex:
            print("sweep skip", s, ex)
        if idx % 5 == 0:
            print(f"  ...أُنجز {idx}/{len(basket)} (صالح {len(precomp)})", flush=True)
        time.sleep(0.03)
    print(f"جاهز: {len(precomp)} رمز · {len(fib_grid)*len(stop_grid)} توليفة — جارٍ التقييم...", flush=True)
    base_fe, base_sb = CFG["fib_entry"], CFG["stop_buf_atr"]
    rows = []
    for fe in fib_grid:
        for sb in stop_grid:
            rs = []
            for P in precomp:
                rs += _eval_combo(P, fe, sb)
            n = len(rs)
            exp = (sum(rs) / n) if n else 0.0
            wins = [x for x in rs if x > 0]; losses = [x for x in rs if x <= 0]
            wr = (len(wins) / n * 100) if n else 0.0
            gl = -sum(losses); pf = (sum(wins) / gl) if gl > 0 else float("inf")
            rows.append(dict(fe=fe, sb=sb, n=n, wr=wr, exp=exp, pf=pf, tot=sum(rs)))
    # ترتيب: التوقّع تنازلياً مع اشتراط حدّ أدنى للصفقات
    rows.sort(key=lambda r: (r["n"] >= min_trades, r["exp"]), reverse=True)
    lines = ["📊 مسح معاملات العرض/الطلب (حافة خام بلا ML)",
             f"الفريم: دخول {CFG['entry_tf']} / سياق {CFG['htf']} · حدّ أدنى {min_trades} صفقة",
             "فيبو | وقف×ATR | صفقات | فوز% | توقّع | PF | مجموع"]
    for r in rows:
        star = "★" if (abs(r["fe"] - base_fe) < 1e-9 and abs(r["sb"] - base_sb) < 1e-9) else " "
        flag = "" if r["n"] >= min_trades else "  (عيّنة صغيرة)"
        pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        lines.append(f"{star}{r['fe']:.3f} | {r['sb']:.1f}     | {r['n']:5d} | "
                     f"{r['wr']:4.1f} | {r['exp']:+.3f}R | {pf} | {r['tot']:+.1f}R{flag}")
    lines.append("★ = الإعداد الحالي")
    report = "\n".join(lines)
    print("\n" + report)
    send_telegram(report)
    return rows

# ----------------------- main -----------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode == "train":
        train()
    elif mode == "scan":
        scan()
    elif mode == "backtest":
        backtest()
    elif mode == "sweep":
        sweep()
    else:  # both: يدرّب فقط إذا غاب النموذج أو تجاوز عمره 24 ساعة، ثم يفحص
        if not model_is_fresh():
            print("model missing/stale -> training")
            train()
        else:
            print("model fresh -> skip training")
        scan()
