#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت «صيد الارتفاعات» (Hunter) — بوت منفصل مستقل — إشارات شراء فقط، فريم 15د
==============================================================================
يرمّز استراتيجية Smart Money Concepts (SMC) التي صمّمناها:
  • منطقة الخصم/التوازن (Premium / Discount / Equilibrium) من مدى آخر سوينق.
  • تحوّل الطابع CHoCH ثم تأكيد الهيكل BOS (كسر آخر قمة هابطة ثم بناء صاعد).
  • ريبون EMA: المتوسط السريع يقطع فوق البطيء.
  • انفجار حجم على شمعة الكسر (Volume Z أعلى من المتوسط).
  • RSI يخرج من التشبّع البيعي ويعبر فوق 50.
  • الدخول: ريتست فيبو 61.8% لأقرب كتلة أوامر / فجوة قيمة (OB / FVG) داخل الخصم.
  • الوقف: تحت آخر قاع قوي (Strong Low) − بافر ATR.
  • الأهداف (فيبوناتشي): هدف1 = التوازن، هدف2 = القمة/Premium، هدف3 = امتداد 1.618.
  • فلتر تجنّب المطاردة: يتجاهل إذا ابتعد السعر >X%% فوق OB أو RSI>75.
  • قاعدة صفقة واحدة باليوم (أنظف Setup) — يمكن رفعها بمتغيّر بيئة.

الأوضاع:
  python hunter_bot.py scan       # يفحص آخر شمعة مغلقة ويرسل أفضل الإشارات لتيليجرام (الافتراضي)
  python hunter_bot.py backtest   # باك-تست حقيقي على بيانات فعلية ويطبع حافة R
  python hunter_bot.py exec       # فحص + تنفيذ تجريبي على بايبت/بايننس (خلف أعلام بيئة)

بوت منفصل تماماً عن sd_bot: ملفات حالة/تتبّع خاصة، وأسرار التيليجرام نفسها.
تنبيه: أداة تحليل تعليمية. لا تنفّذ صفقات بأموال حقيقية افتراضياً. التداول مخاطرة، وليست نصيحة مالية.
"""
import os, sys, time, math, json, datetime as dt
from collections import deque
import requests

# ----------------------- إعدادات الاستراتيجية -----------------------
CFG = dict(
    entry_tf="1h",             # فريم الدخول (الوصفة الفائزة: 1س — 15د خاسرة في الباك-تست)
    htf="4h",                  # سياق أعلى للانحياز الاتجاهي
    pages=3, pages_htf=2,      # صفحات جلب البيانات (كل صفحة ≈ 1000 شمعة)
    swing_L=5, swing_R=5,      # نصف نافذة تحديد القمم/القيعان (pivots)
    struct_lookback=60,        # مدى البحث عن CHoCH/BOS بالشموع
    ema_fast=21, ema_slow=55,  # ريبون EMA (سريع/بطيء)
    ema_trend=200,             # متوسط الاتجاه العام
    rsi_len=14,
    atr_len=14,
    vol_len=50,                # نافذة Volume Z
    vol_z_min=1.0,             # حد انفجار الحجم على شمعة الكسر
    fib_entry=0.618,           # الدخول عند تصحيح فيبو 61.8% لساق الاندفاع
    fib_ext=1.618,             # امتداد فيبو للهدف الثالث
    stop_buf_atr=0.5,          # بافر الوقف تحت القاع = ×ATR
    chase_max_atr=1.5,         # فلتر المطاردة: بُعد السعر عن الدخول > ×ATR = فات القطار
    rsi_chase_max=75,          # فلتر المطاردة: RSI فوق هذا = متأخر
    rsi_cross=50,              # عبور RSI المطلوب
    require_monthly_vwap=0,    # (قديم) فلتر VWAP الشهري — عُطّل لصالح بوابة الزخم
    # ── بوابة الزخم الإلزامية (2026-07-04): CHoCH ← فوق MA365 ← RSI21 تشبّع شرائي ← فوليوم عالٍ ← دخول ──
    momentum_gate=1,           # 1 = تفعيل تسلسل الزخم الإلزامي
    trend_filter="wvwap",      # فلتر الاتجاه في البوابة: "wvwap"=VWAP الأسبوعي | "ma365"=متوسط 365
    ma_len=365,                # المتوسط المتحرّك 365 (يُستخدم إذا trend_filter=ma365)
    rsi_mom_len=21,            # طول RSI لشرط التشبّع الشرائي
    rsi_ob=70,                 # عتبة التشبّع الشرائي
    vol_entry_z=2.0,           # فوليوم الدخول العالي (Volume Z ≥ هذا على شمعة الدخول)
    seq_lookback=60,           # نافذة تحقّق التسلسل (بالشموع)
    stop_lookback=10,          # نافذة قاع الوقف (أقرب للاختراق = وقف أضيق)
    fib_ext1=1.272, fib_ext2=1.618, fib_ext3=2.618,  # امتدادات فيبو للأهداف
    # ── إدارة الخروج (الأهم): وقف متحرّك شانديلير + جني جزئي ──
    trail_atr=2.5,             # مضاعف الوقف المتحرّك (قمة − trail_atr×ATR)
    tp1_frac=0.5,              # نسبة الجني عند الهدف الأول قبل تفعيل التتبّع
    min_score=4,               # أدنى درجة (من 5 شروط) لقبول الإشارة
    one_per_day=1,             # 1 = صفقة واحدة باليوم (أعلى درجة)، 0 = كل الإشارات
    top_n=5,                   # أقصى عدد إشارات للإرسال
    bt_hold=48,                # (باك-تست) أقصى شموع لإمساك الصفقة
    risk_pct=0.01,             # مخاطرة مقترحة لكل صفقة (1%)
    max_symbols=60,            # حد رموز الفحص
)
# ── تجاوز الفريمات عبر البيئة (لتشغيل على 15m/1h/... مثل بقية البوتات) ──
CFG["entry_tf"] = os.environ.get("HUNTER_TF", CFG["entry_tf"])
CFG["htf"]      = os.environ.get("HUNTER_HTF", CFG["htf"])
CFG["one_per_day"] = int(os.environ.get("HUNTER_ONE_PER_DAY", CFG["one_per_day"]))
CFG["min_score"]   = int(os.environ.get("HUNTER_MIN_SCORE", CFG["min_score"]))
CFG["require_monthly_vwap"] = int(os.environ.get("HUNTER_REQUIRE_VWAP", CFG["require_monthly_vwap"]))
CFG["momentum_gate"] = int(os.environ.get("HUNTER_MOMENTUM", CFG["momentum_gate"]))
CFG["trend_filter"] = os.environ.get("HUNTER_TREND_FILTER", CFG["trend_filter"]).lower()
CFG["ma_len"]     = int(os.environ.get("HUNTER_MA_LEN", CFG["ma_len"]))
CFG["rsi_ob"]     = float(os.environ.get("HUNTER_RSI_OB", CFG["rsi_ob"]))
CFG["vol_entry_z"] = float(os.environ.get("HUNTER_VOL_Z", CFG["vol_entry_z"]))
CFG["trail_atr"]  = float(os.environ.get("HUNTER_TRAIL_ATR", CFG["trail_atr"]))
CFG["tp1_frac"]   = float(os.environ.get("HUNTER_TP1_FRAC", CFG["tp1_frac"]))
CFG["bt_symbols"] = int(os.environ.get("HUNTER_BT_SYMBOLS", 40))   # عدد رموز الباك-تست (قاعدة الإحياء: 40+)

# ── تجريبي (باك-تست فقط، معطّل افتراضياً): فلتر Extra High + دائرة دعم ──
# مقتبس من مؤشري TradingView: Heatmap Volume [xdecow] (Z-score حجم) و
# Volumatic Support/Resistance [BigBeluga] (مستويات دعم/مقاومة موزونة بالحجم).
CFG["xh_len"]          = int(os.environ.get("HUNTER_XH_LEN", 610))     # طول قاعدة Z-score (كالمؤشر الأصلي)
CFG["xh_thresh"]       = float(os.environ.get("HUNTER_XH_THRESH", 4.0))  # عتبة "Extra High"
CFG["circle_len"]      = int(os.environ.get("HUNTER_CIRCLE_LEN", 25))    # نافذة تحديد سوينق الدعم
CFG["circle_vol_window"] = int(os.environ.get("HUNTER_CIRCLE_VOLWIN", 500))  # نافذة أقصى حجم (للنسبة المئوية)
CFG["circle_thresh"]   = float(os.environ.get("HUNTER_CIRCLE_THRESH", 80))   # عتبة الدائرة (% من الأقصى)
CFG["require_xh"]      = int(os.environ.get("HUNTER_REQUIRE_XH", 0))     # 1 = إلزامي Extra High على شمعة الدخول
CFG["require_circle"]  = int(os.environ.get("HUNTER_REQUIRE_CIRCLE", 0))  # 1 = إلزامي دائرة دعم ضمن نافذة التسلسل
# فريمات باك-تست فلتر Extra High/دائرة دعم — الافتراضي: 1h/15m/5m معاً (كل واحد بسياقه الأعلى المعتاد بالمشروع)
# تخصيص عبر HUNTER_BT_FRAMES="1h:4h,15m:1h,5m:15m"
CFG["bt_frames"] = os.environ.get("HUNTER_BT_FRAMES", "1h:4h,15m:1h,5m:15m")

BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
WATCHLIST = "watchlist.txt"
STATE_PATH = os.environ.get("HUNTER_STATE", "hunter_state.json")
POSITIONS_PATH = os.environ.get("HUNTER_POSITIONS", "hunter_positions.json")
TRACK_FILE = "tracked_signals.json"        # مشترك مع اللوحة (نضيف فقط إشاراتنا)
DASH_LABEL = "صيد الارتفاعات"
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", os.environ.get("TG_TOKEN", ""))
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_CHAT", ""))
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
                    data = r.json(); break
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
        t = (h[i] - l[i]) if i == 0 else max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        tr.append(t); s += t
        if i >= n: s -= tr[i-n]
        if i >= n-1: out[i] = s / n
    return out

def ema(arr, n):
    k = 2/(n+1); out = [float("nan")]*len(arr); prev = None
    for i, x in enumerate(arr):
        prev = x if i == 0 else x*k + prev*(1-k)
        out[i] = prev
    return out

def sma(arr, n):
    out = [float("nan")]*len(arr); s = 0.0
    for i, x in enumerate(arr):
        s += x
        if i >= n: s -= arr[i-n]
        if i >= n-1: out[i] = s/n
    return out

def rsi(c, n):
    out = [float("nan")]*len(c)
    if len(c) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n+1):
        d = c[i]-c[i-1]
        gains += max(d, 0.0); losses += max(-d, 0.0)
    ag, al = gains/n, losses/n
    out[n] = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
    for i in range(n+1, len(c)):
        d = c[i]-c[i-1]
        ag = (ag*(n-1) + max(d, 0.0))/n
        al = (al*(n-1) + max(-d, 0.0))/n
        out[i] = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
    return out

def vol_z(v, L):
    out = [float("nan")]*len(v)
    for i in range(L-1, len(v)):
        win = v[i-L+1:i+1]
        m = sum(win)/L
        sd = math.sqrt(sum((x-m)**2 for x in win)/L)
        out[i] = (v[i]-m)/sd if sd > 0 else 0.0
    return out

def vwap_monthly(t, h, l, c, v):
    """VWAP مرسّى لبداية كل شهر تقويمي (UTC): Σ(السعر النموذجي×الحجم)/Σ(الحجم)،
       يُصفّر عند بداية كل شهر. السعر النموذجي = (high+low+close)/3."""
    out = [float("nan")]*len(c)
    cum_pv = cum_v = 0.0; cur_month = None
    for i in range(len(c)):
        d = dt.datetime.utcfromtimestamp(t[i]/1000)
        mk = (d.year, d.month)
        if mk != cur_month:
            cur_month = mk; cum_pv = cum_v = 0.0
        tp = (h[i]+l[i]+c[i])/3.0
        cum_pv += tp*v[i]; cum_v += v[i]
        out[i] = (cum_pv/cum_v) if cum_v > 0 else float("nan")
    return out

def vwap_weekly(t, h, l, c, v):
    """VWAP مرسّى لبداية كل أسبوع ISO (يبدأ الإثنين، UTC): Σ(السعر النموذجي×الحجم)/Σ(الحجم)،
       يُصفّر عند بداية كل أسبوع. السعر النموذجي = (high+low+close)/3."""
    out = [float("nan")]*len(c)
    cum_pv = cum_v = 0.0; cur_week = None
    for i in range(len(c)):
        d = dt.datetime.utcfromtimestamp(t[i]/1000)
        iso = d.isocalendar()
        wk = (iso[0], iso[1])            # (سنة ISO، رقم الأسبوع)
        if wk != cur_week:
            cur_week = wk; cum_pv = cum_v = 0.0
        tp = (h[i]+l[i]+c[i])/3.0
        cum_pv += tp*v[i]; cum_v += v[i]
        out[i] = (cum_pv/cum_v) if cum_v > 0 else float("nan")
    return out

def vol_z_fast(v, L):
    """نفس معادلة vol_z لكن O(n) بمجموع متحرّك — لازمة للنوافذ الطويلة (مثل 610)
       تفادياً لبطء O(n×L) في الباك-تست. تُستخدم فقط لفلتر Extra High التجريبي."""
    n = len(v)
    out = [float("nan")] * n
    s = s2 = 0.0
    for i in range(n):
        s += v[i]; s2 += v[i] * v[i]
        if i >= L:
            s -= v[i - L]; s2 -= v[i - L] * v[i - L]
        if i >= L - 1:
            m = s / L
            var = s2 / L - m * m
            sd = math.sqrt(var) if var > 0 else 0.0
            out[i] = (v[i] - m) / sd if sd > 0 else 0.0
    return out

def _rolling_min(a, L):
    out = [float("nan")] * len(a); dq = deque()
    for i, x in enumerate(a):
        while dq and a[dq[-1]] >= x:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - L:
            dq.popleft()
        out[i] = a[dq[0]]
    return out

def _rolling_max(a, L):
    out = [float("nan")] * len(a); dq = deque()
    for i, x in enumerate(a):
        while dq and a[dq[-1]] <= x:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - L:
            dq.popleft()
        out[i] = a[dq[0]]
    return out

def pct_of_recent_max(v, window):
    """يحاكي n_vol بمؤشر Volumatic S/R: نسبة حجم الشمعة من أقصى حجم بآخر window شمعة (≤100)."""
    mx = _rolling_max(v, window)
    out = [0.0] * len(v)
    for i in range(len(v)):
        out[i] = (v[i] / mx[i] * 100.0) if mx[i] and mx[i] > 0 else 0.0
    return out

def support_circle_hit(l, v, cfg, lo, hi):
    """يحاكي «دائرة دعم» بمؤشر Volumatic S/R: يبحث ضمن [lo, hi] عن سوينق قاع مؤكّد
       بتأخير شمعة واحدة (كالمؤشر الأصلي) بحجم n_vol أعلى من عتبة الدائرة."""
    L = cfg["circle_len"]; TH = cfg["circle_thresh"]
    lo_win = _rolling_min(l, L)
    nvol = pct_of_recent_max(v, cfg["circle_vol_window"])
    start = max(1, lo)
    for i in range(start, hi + 1):
        if lo_win[i - 1] == l[i - 1] and l[i] > lo_win[i - 1] and nvol[i - 1] > TH:
            return True
    return False

def pivots(h, l, L, R):
    """يعيد قائمتين: قمم سوينق (ph) وقيعان سوينق (pl) كـ (index, price)."""
    ph, pl = [], []
    for i in range(L, len(h)-R):
        if all(h[i] >= h[i-k] for k in range(1, L+1)) and all(h[i] >= h[i+k] for k in range(1, R+1)):
            ph.append((i, h[i]))
        if all(l[i] <= l[i-k] for k in range(1, L+1)) and all(l[i] <= l[i+k] for k in range(1, R+1)):
            pl.append((i, l[i]))
    return ph, pl


# ----------------------- منطق SMC: بنية + خصم + كتل أوامر + FVG -----------------------
def find_setup(sym, d1, d4):
    """يبحث عن Setup «صيد ارتفاع» عند آخر شمعة مغلقة.
       يعيد dict للإشارة أو None. الدرجة score من 0..5 حسب تحقّق الشروط الخمسة."""
    o, h, l, c, v = d1["o"], d1["h"], d1["l"], d1["c"], d1["v"]
    n = len(c)
    if n < max(CFG["ema_trend"], CFG["ma_len"] + 30, 250):   # نحتاج بيانات تكفي لـ MA365
        return None
    last = n - 1
    A = atr(h, l, c, CFG["atr_len"])
    ef = ema(c, CFG["ema_fast"]); es = ema(c, CFG["ema_slow"]); et = ema(c, CFG["ema_trend"])
    R = rsi(c, CFG["rsi_len"])
    VZ = vol_z(v, CFG["vol_len"])
    VW = vwap_monthly(d1["t"], h, l, c, v) if CFG["require_monthly_vwap"] else None  # يُحسب فقط عند تفعيل فلتر VWAP
    a = A[last]
    if not (a and math.isfinite(a)) or a <= 0:
        return None

    ph, pl = pivots(h, l, CFG["swing_L"], CFG["swing_R"])
    if len(ph) < 2 or len(pl) < 2:
        return None

    # --- بنية: قمم/قيعان + تحوّل طابع صاعد CHoCH ---
    swing_hi = ph[-1][1]; swing_lo = pl[-1][1]
    rng = swing_hi - swing_lo
    if rng <= 0:
        return None
    eq = swing_lo + 0.5 * rng
    price = c[last]

    # CHoCH صاعد = قاع أعلى (higher-low) أو كسر آخر قمة سوينق للأعلى.
    # نستخدم البيفوتات العامة (لا نافذة ضيّقة) لأن التحوّل يحصل مبكراً قبل امتداد الرالي.
    choch = False
    if len(pl) >= 2 and pl[-1][1] > pl[-2][1]:
        choch = True                            # قاع أعلى = بداية تحوّل صاعد
    if not choch and len(ph) >= 1 and ph[-1][0] + 1 < n:
        if max(c[ph[-1][0] + 1:]) > ph[-1][1]:  # إغلاق كسر آخر قمة سوينق
            choch = True

    # --- مؤشّرات بوابة الزخم ---
    Rm = rsi(c, CFG["rsi_mom_len"])             # RSI(21)
    ema_bull = math.isfinite(ef[last]) and math.isfinite(es[last]) and ef[last] > es[last]
    # فلتر الاتجاه: VWAP الأسبوعي (افتراضي) أو متوسط 365
    if CFG["trend_filter"] == "ma365":
        TR = sma(c, CFG["ma_len"]); trend_name = "فوق متوسط 365"
    else:
        TR = vwap_weekly(d1["t"], h, l, c, v); trend_name = "فوق VWAP الأسبوعي"
    above_ma = math.isfinite(TR[last]) and price > TR[last]                    # (شرط) فوق فلتر الاتجاه
    win0 = max(0, last - CFG["seq_lookback"])
    rsi_ob_hit = any(math.isfinite(Rm[i]) and Rm[i] >= CFG["rsi_ob"]           # (شرط) RSI21 بلغ التشبّع الشرائي
                     for i in range(win0, n))
    rsi_now = Rm[last] if math.isfinite(Rm[last]) else 50.0
    vol_entry = (c[last] > o[last]) and math.isfinite(VZ[last]) and VZ[last] >= CFG["vol_entry_z"]  # (شرط) شمعة فوليوم عالٍ

    # ── البوابة الإلزامية: التسلسل الكامل (CHoCH ← فوق MA365 ← RSI21 تشبّع ← فوليوم عالٍ) ──
    if CFG["momentum_gate"] and not (choch and above_ma and rsi_ob_hit and vol_entry):
        return None

    # ── فلتر تجريبي (معطّل افتراضياً، للباك-تست فقط): Extra High + دائرة دعم ──
    xh_ok = circle_ok = None
    if CFG["require_xh"]:
        VZX = vol_z_fast(v, CFG["xh_len"])
        xh_ok = math.isfinite(VZX[last]) and VZX[last] > CFG["xh_thresh"]
        if not xh_ok:
            return None
    if CFG["require_circle"]:
        circle_ok = support_circle_hit(l, v, CFG, win0, last)
        if not circle_ok:
            return None

    # --- الدخول = إغلاق شمعة الفوليوم العالي (دخول استمرار الزخم) ---
    entry = price
    swing_base = min(l[win0:last+1])                    # أدنى قاع في نافذة التسلسل (أساس الساق)
    recent_hi = max(h[win0:last+1])                     # قمة النافذة الحديثة (قمة الرالي)
    stop_lo = min(l[max(0, last-CFG["stop_lookback"]):last+1])   # قاع قريب للاختراق = وقف أضيق
    stop = stop_lo - CFG["stop_buf_atr"] * a            # الوقف تحته − بافر ATR
    if stop >= entry:
        return None
    ob_low, ob_high = stop_lo, recent_hi

    # --- الأهداف: امتدادات فيبو للساق البنيوية (قاع النافذة → قمة النافذة) ---
    leg = recent_hi - swing_base
    if leg <= 0:
        return None
    tp1 = swing_base + CFG["fib_ext1"] * leg
    tp2 = swing_base + CFG["fib_ext2"] * leg
    tp3 = swing_base + CFG["fib_ext3"] * leg
    R0 = entry - stop
    if tp1 <= entry: tp1 = entry + R0
    if tp2 <= tp1:   tp2 = tp1 + R0
    if tp3 <= tp2:   tp3 = tp2 + R0

    # --- الدرجة: الشروط الخمسة (أربعة منها إلزامية عبر البوابة + EMA صاعد) ---
    score = sum([choch, above_ma, rsi_ob_hit, vol_entry, ema_bull])

    # سياق HTF (اختياري): 1h غير هابط = المتوسط السريع فوق البطيء
    htf_ok = True
    if d4 and len(d4["c"]) > CFG["ema_slow"]:
        e1 = ema(d4["c"], CFG["ema_fast"]); e2 = ema(d4["c"], CFG["ema_slow"])
        if math.isfinite(e1[-1]) and math.isfinite(e2[-1]):
            htf_ok = e1[-1] >= e2[-1]

    reasons = []
    if choch: reasons.append("CHoCH صاعد")
    if above_ma: reasons.append(trend_name)
    if rsi_ob_hit: reasons.append(f"RSI21 تشبّع شرائي (≥{int(CFG['rsi_ob'])})")
    if vol_entry: reasons.append("دخول فوليوم عالٍ")
    if ema_bull: reasons.append("EMA صاعد")
    if htf_ok: reasons.append("سياق 1h غير هابط")
    if xh_ok: reasons.append(f"Extra High (Z>{CFG['xh_thresh']})")
    if circle_ok: reasons.append("دائرة دعم (حجم عالٍ عند القاع)")

    return dict(
        sym=sym, tf=CFG["entry_tf"], score=int(score),
        entry=round(entry, 8), stop=round(stop, 8),
        tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
        rsi=round(rsi_now, 1), htf_ok=bool(htf_ok),
        ts=d1["t"][last], reasons=reasons,
        ob_low=round(ob_low, 8), ob_high=round(ob_high, 8), eq=round(eq, 8),
        atr=round(a, 8),                         # ATR عند الدخول (لوقف شانديلير المتحرّك)
    )


# ----------------------- watchlist -----------------------
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


# ----------------------- الحالة (منع التكرار) -----------------------
def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {"sent": [], "last_day": ""}

def save_state(state):
    state["sent"] = state.get("sent", [])[-800:]
    try:
        json.dump(state, open(STATE_PATH, "w"))
    except Exception as ex:
        print("state save error", ex)


# ----------------------- تيليجرام + اللوحة -----------------------
def _fmt(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"

def format_message(signals):
    nums = ["1️⃣", "2️⃣", "3️⃣"]
    now = dt.datetime.now().strftime("%H:%M:%S")
    tf = CFG["entry_tf"]
    sep = "\n➖➖➖➖➖➖➖➖➖\n"
    blocks = []
    for s in signals:
        entry, stop = s["entry"], s["stop"]
        risk_pct = ((entry-stop)/entry*100) if entry else 0.0
        tps = [s.get("tp1"), s.get("tp2"), s.get("tp3")]
        lines = [
            "🟢 صيد ارتفاع — شراء (SMC)",
            f"💎 {s['sym']} · ⏱️ {tf}",
            f"⭐ الدرجة: {s['score']}/5 · RSI {s['rsi']}",
            f"📊 الأسباب: {'، '.join(s['reasons'])}",
            "",
            f"📍 الدخول (ريتست فيبو 61.8%): {_fmt(entry)}",
            f"🛑 الوقف: {_fmt(stop)}  (−{risk_pct:.2f}%)",
            "",
            "🎯 الأهداف (فيبوناتشي):",
        ]
        for k, t in enumerate(tps):
            if not t:
                continue
            gain = ((t-entry)/entry*100) if entry else 0.0
            lines.append(f"{nums[k]} {_fmt(t)}  (+{gain:.2f}%)")
        lines += [
            "",
            f"⚖️ المخاطرة المقترحة: {int(CFG['risk_pct']*100)}% من المحفظة · "
            f"إدارة الخروج: جني {int(CFG['tp1_frac']*100)}% عند الهدف الأول ثم وقف متحرّك شانديلير للباقي",
            f"⏰ {now}",
        ]
        blocks.append("\n".join(lines))
    header = ("🏹 <b>بوت صيد الارتفاعات (SMC) — شراء فقط</b>\n"
              "<i>خصم + CHoCH/BOS + EMA + حجم + RSI · دخول ريتست فيبو</i>")
    footer = "⚠️ تحليل تعليمي — ليس نصيحة مالية"
    return header + sep + sep.join(blocks) + sep + footer

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("TG not configured; message:\n", text); return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
        print(f"sent {text.count(chr(0x1F3F9))} signals to telegram")
        return (r.json().get("result") or {}).get("message_id")
    except Exception as ex:
        print("telegram error", ex); return None

def track_for_dashboard(signals, message_id, path=TRACK_FILE):
    tf = CFG["entry_tf"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    cutoff = (dt.datetime.now() - dt.timedelta(days=14)).isoformat()
    data = {k: v for k, v in data.items()
            if not (isinstance(v, dict) and v.get("label") == DASH_LABEL
                    and v.get("created", "") < cutoff)}
    added = 0
    for s in signals:
        entry, stop, tp1 = s["entry"], s["stop"], s["tp1"]
        if entry - stop <= 0:
            continue
        tp2 = s.get("tp2") or round(entry + 2*(entry-stop), 8)
        bar_ts = dt.datetime.fromtimestamp(s["ts"]/1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        key = f"{DASH_LABEL}|{s['sym']}|{bar_ts}"
        if key in data:
            continue
        data[key] = {
            "symbol": s["sym"], "label": DASH_LABEL, "timeframe": tf,
            "message_id": message_id,
            "entry": entry, "stop": stop, "init_stop": stop, "cur_stop": stop,
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


# ----------------------- إدارة الخروج الحيّة (نظام ب: جني جزئي + وقف متحرّك شانديلير) -----------------------
def load_positions():
    try:
        d = json.load(open(POSITIONS_PATH))
        return d if isinstance(d, dict) else {"open": []}
    except Exception:
        return {"open": []}

def save_positions(pos):
    try:
        json.dump(pos, open(POSITIONS_PATH, "w"), ensure_ascii=False, indent=2)
    except Exception as ex:
        print("positions save error", ex)

def open_position(sig):
    """يسجّل صفقة جديدة لإدارتها حيّاً بنظام (ب). لا يُنفّذ أموالاً — تتبّع خروج فقط."""
    pos = load_positions()
    key = f"{sig['sym']}:{sig['ts']}"
    if any(p.get("key") == key for p in pos["open"]):
        return
    pos["open"].append({
        "key": key, "sym": sig["sym"], "tf": sig.get("tf"),
        "entry": sig["entry"], "init_stop": sig["stop"], "stop": sig["stop"],
        "tp1": sig["tp1"], "atr": sig.get("atr") or (sig["entry"] - sig["stop"]),
        "peak": sig["entry"], "took1": False,
        "opened": dt.datetime.now().isoformat(timespec="seconds"),
        "bar_ts": sig["ts"],
    })
    save_positions(pos)

def _alert(text):
    send_telegram(text)

def monitor_positions():
    """يتابع المراكز المفتوحة على آخر شمعة مغلقة ويطبّق نظام الخروج (ب):
       • جني tp1_frac عند بلوغ الهدف الأول ثم نقل الوقف للتعادل.
       • بعد الجني: وقف متحرّك شانديلير (قمة − trail_atr×ATR)، لا ينزل عن التعادل.
       • إغلاق ما تبقّى عند لمس الوقف. يرسل تنبيهات تيليجرام ويحدّث الحالة."""
    pos = load_positions()
    if not pos["open"]:
        print("لا مراكز مفتوحة"); return
    k = CFG["trail_atr"]; f1 = CFG["tp1_frac"]
    still_open = []
    for p in pos["open"]:
        try:
            d1 = fetch_klines(p["sym"], p.get("tf") or CFG["entry_tf"], 1)
            if not d1 or len(d1["c"]) < 2:
                still_open.append(p); continue
            last = len(d1["c"]) - 1
            hi, lo, cl = d1["h"][last], d1["l"][last], d1["c"][last]
            entry, av = p["entry"], p["atr"]
            R = entry - p["init_stop"]
            p["peak"] = max(p.get("peak", entry), hi)
            # تحديث وقف شانديلير بعد الجني الجزئي
            if p["took1"]:
                p["stop"] = max(p["stop"], p["peak"] - k*av, entry)
            # لمس الوقف = إغلاق ما تبقّى
            if lo <= p["stop"]:
                r = ((p["stop"] - entry) / R) if R > 0 else 0.0
                remain = (1.0 - f1) if p["took1"] else 1.0
                _alert(f"🔔 {p['sym']} · ⏱️ {p.get('tf')}\n"
                       f"إغلاق {'الباقي' if p['took1'] else 'الصفقة'} عند الوقف {_fmt(p['stop'])}\n"
                       f"النتيجة على هذا الجزء: {r:+.2f}R" +
                       ("  (بعد جني 50% عند الهدف الأول)" if p["took1"] else ""))
                continue    # تُزال من المفتوحة
            # بلوغ الهدف الأول = جني جزئي + نقل الوقف للتعادل
            if (not p["took1"]) and hi >= p["tp1"]:
                p["took1"] = True
                p["stop"] = max(p["stop"], entry)
                _alert(f"🎯 {p['sym']} · ⏱️ {p.get('tf')}\n"
                       f"بلغ الهدف الأول {_fmt(p['tp1'])} — جني {int(f1*100)}% ونقل الوقف للتعادل.\n"
                       f"الباقي {int((1-f1)*100)}% بوقف متحرّك (شانديلير {k}×ATR).")
            still_open.append(p)
        except Exception as ex:
            print("monitor skip", p.get("sym"), ex); still_open.append(p)
        time.sleep(0.03)
    pos["open"] = still_open
    save_positions(pos)
    print(f"مراكز مفتوحة الآن: {len(still_open)}")


# ----------------------- الفحص -----------------------
def scan(basket=None, send=True):
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:CFG["max_symbols"]]
    state = load_state(); sent = set(state.get("sent", []))
    signals = []
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], CFG["pages"])
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_htf"])
            if not d1 or len(d1["c"]) < 300:
                continue
            sig = find_setup(s, d1, d4)
            if not sig:
                continue
            if sig["score"] < CFG["min_score"]:
                continue
            key = f"{s}:{sig['ts']}"
            if key in sent:
                continue
            sig["key"] = key
            signals.append(sig)
        except Exception as ex:
            print("scan skip", s, ex)
        time.sleep(0.03)

    # ترتيب حسب الدرجة ثم قرب الدخول
    signals.sort(key=lambda x: (x["score"], x["htf_ok"]), reverse=True)

    # قاعدة صفقة واحدة باليوم: أنظف Setup فقط
    if CFG["one_per_day"] and signals:
        signals = signals[:1]
    else:
        signals = signals[:CFG["top_n"]]

    if signals and send:
        mid = send_telegram(format_message(signals))
        track_for_dashboard(signals, mid)
        for sig in signals:
            open_position(sig)          # فتح صفقة لإدارة الخروج الحيّة (نظام ب)
            state.setdefault("sent", []).append(sig["key"])
        state["last_day"] = dt.date.today().isoformat()
        save_state(state)
    elif not signals:
        print("no signals this scan")
    return signals


# ----------------------- باك-تست حقيقي -----------------------
def _sim_static(entry, stop, tp1, tp2, tp3, h, l, c, tch, hold):
    """(أ) الخروج الحالي: 40/40/20 عند الأهداف + الوقف→تعادل بعد الهدف الأول. الناتج بوحدات R."""
    R = entry - stop
    if R <= 0:
        return None
    r1, r2, r3 = (tp1-entry)/R, (tp2-entry)/R, (tp3-entry)/R
    if r1 <= 0:
        return None
    end = min(len(c), tch+hold)
    took1 = took2 = False; sl = stop; realized = 0.0
    for i in range(tch, end):
        if l[i] <= sl:
            frac = 1.0 - (0.4 if took1 else 0.0) - (0.4 if took2 else 0.0)
            return realized + frac * ((sl-entry)/R)
        if not took1 and h[i] >= tp1:
            took1 = True; realized += 0.4*r1; sl = entry
        if took1 and not took2 and h[i] >= tp2:
            took2 = True; realized += 0.4*r2
        if took2 and h[i] >= tp3:
            realized += 0.2*r3; return realized
    frac = 1.0 - (0.4 if took1 else 0.0) - (0.4 if took2 else 0.0)
    return realized + frac * ((c[end-1]-entry)/R)

def _sim_trail_partial(entry, stop, tp1, av, h, l, c, tch, hold):
    """(ب) جني جزئي عند الهدف1 (tp1_frac) ثم وقف متحرّك شانديلير (قمة−k×ATR) للباقي،
       بحدّ أدنى عند التعادل بعد الجني. يركب امتداد الترند بدل الخروج المبكر عند الأهداف."""
    R = entry - stop
    if R <= 0 or av <= 0:
        return None
    r1 = (tp1-entry)/R
    if r1 <= 0:
        return None
    k = CFG["trail_atr"]; f1 = CFG["tp1_frac"]
    end = min(len(c), tch+hold)
    sl = stop; peak = entry; took1 = False; realized = 0.0
    for i in range(tch, end):
        peak = max(peak, h[i])
        if took1:
            sl = max(sl, peak - k*av, entry)      # التتبّع مفعّل بعد الجني، لا ينزل عن التعادل
        if l[i] <= sl:
            frac = 1.0 - (f1 if took1 else 0.0)
            return realized + frac * ((sl-entry)/R)
        if not took1 and h[i] >= tp1:
            took1 = True; realized += f1*r1; sl = max(sl, entry)
    frac = 1.0 - (f1 if took1 else 0.0)
    return realized + frac * ((c[end-1]-entry)/R)

def _sim_trail_full(entry, stop, av, h, l, c, tch, hold):
    """(ج) وقف متحرّك شانديلير كامل من البداية (بلا جني جزئي) — أقصى ركوب للترند."""
    R = entry - stop
    if R <= 0 or av <= 0:
        return None
    k = CFG["trail_atr"]
    end = min(len(c), tch+hold)
    sl = stop; peak = entry
    for i in range(tch, end):
        peak = max(peak, h[i])
        sl = max(sl, peak - k*av)
        if l[i] <= sl:
            return (sl-entry)/R
    return (c[end-1]-entry)/R

def _stats(rs):
    if not rs:
        return "لا صفقات"
    n = len(rs); wins = [x for x in rs if x > 0]; losses = [x for x in rs if x <= 0]
    wr = len(wins)/n*100
    gp = sum(wins); gl = -sum(losses)
    pf = (gp/gl) if gl > 0 else float("inf")
    exp = sum(rs)/n
    return (f"صفقات={n} · فوز={wr:.1f}% · توقّع={exp:+.3f}R · PF={pf:.2f} · مجموع={sum(rs):+.1f}R")

def backtest(basket=None):
    """يقارن ٣ أنظمة خروج على نفس الإشارات (إدارة الخروج = أهم رافعة):
       (أ) 40/40/20 + تعادل  (ب) جني جزئي + وقف متحرّك شانديلير  (ج) وقف متحرّك كامل."""
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:CFG["bt_symbols"]]
    hold = CFG["bt_hold"]
    rs_a, rs_b, rs_c = [], [], []
    print(f"باك-تست صيد الارتفاعات | tf={CFG['entry_tf']} htf={CFG['htf']} | {len(basket)} رمز | hold={hold} "
          f"| min_score={CFG['min_score']} | vol_z={CFG['vol_entry_z']} | trail={CFG['trail_atr']}×ATR")
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], CFG["pages"])
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_htf"])
            if not d1 or len(d1["c"]) < 400:
                continue
            h, l, c = d1["h"], d1["l"], d1["c"]
            A = atr(h, l, c, CFG["atr_len"])          # ATR للوقف المتحرّك
            N = len(c)
            step = 3
            for cut in range(300, N-hold, step):
                sub = {k: d1[k][:cut+1] for k in ("t", "o", "h", "l", "c", "v")}
                sig = find_setup(s, sub, d4)
                if not sig or sig["score"] < CFG["min_score"]:
                    continue
                e, st = sig["entry"], sig["stop"]
                av = A[cut] if (A[cut] and math.isfinite(A[cut])) else (e - st)
                ra = _sim_static(e, st, sig["tp1"], sig["tp2"], sig["tp3"], h, l, c, cut, hold)
                rb = _sim_trail_partial(e, st, sig["tp1"], av, h, l, c, cut, hold)
                rc = _sim_trail_full(e, st, av, h, l, c, cut, hold)
                if ra is not None: rs_a.append(ra)
                if rb is not None: rs_b.append(rb)
                if rc is not None: rs_c.append(rc)
        except Exception as ex:
            print("bt skip", s, ex)
        time.sleep(0.03)
    print("(أ) 40/40/20 + تعادل     :", _stats(rs_a))
    print("(ب) جني جزئي + تريل شانديلير:", _stats(rs_b))
    print("(ج) تريل شانديلير كامل    :", _stats(rs_c))
    return {"static": rs_a, "trail_partial": rs_b, "trail_full": rs_c}


# ----------------------- باك-تست فلتر Extra High + دائرة دعم (تجريبي) -----------------------
def backtest_entry_filters(basket=None, frames=None):
    """يقارن 4 نسخ من الدخول (باستخدام نفس بوابة الزخم الحالية كأساس) — الأساس بدون
       إضافة، Extra High فقط، دائرة دعم فقط، والتقاطع الكامل — على عدّة فريمات (افتراضياً
       1h/15m/5m، كل واحد بسياقه الأعلى المعتاد بالمشروع)، كلها بنفس خطة الخروج الحيّة
       المطبّقة حالياً (نظام ب: جني جزئي + وقف متحرّك شانديلير). لا تُطبَّق حياً.
       يجلب بيانات كل رمز مرّة واحدة لكل فريم ويعيد استخدامها عبر الأربع نسخ (بدل إعادة
       الجلب لكل نسخة) لتقليل عدد طلبات الشبكة والوقت."""
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:CFG["bt_symbols"]]
    hold = CFG["bt_hold"]
    variants = [
        ("baseline (بدون فلتر إضافي)", {"xh": 0, "circle": 0, "sma200": 0}),
        ("Extra High فقط",              {"xh": 1, "circle": 0, "sma200": 0}),
        ("Extra High + SMA200",          {"xh": 1, "circle": 0, "sma200": 1}),  # نسخة جديدة
        ("دائرة دعم فقط",                {"xh": 0, "circle": 1, "sma200": 0}),
        ("Extra High + دائرة دعم",       {"xh": 1, "circle": 1, "sma200": 0}),
    ]
    if frames is None:
        frames = []
        for pair in CFG["bt_frames"].split(","):
            pair = pair.strip()
            if not pair:
                continue
            tf, htf = pair.split(":")
            frames.append((tf.strip(), htf.strip()))

    all_results = {}
    for tf, htf in frames:
        CFG["entry_tf"] = tf
        CFG["htf"] = htf
        min_hist = max(CFG["xh_len"] + 60, 700)   # يحتاج تاريخ يكفي لقاعدة Z-score الطويلة (610)
        print(f"\n=== فريم {tf} (سياق {htf}) ===")
        print(f"باك-تست فلتر Extra High/دائرة دعم/SMA200 | tf={tf} htf={htf} | {len(basket)} رمز | "
              f"xh_len={CFG['xh_len']} xh_thresh={CFG['xh_thresh']} | circle_len={CFG['circle_len']} "
              f"circle_thresh={CFG['circle_thresh']}")

        # جلب بيانات كل رمز مرّة واحدة لهذا الفريم (تُستخدم عبر النسخ)
        cache = {}
        for s in basket:
            try:
                d1 = fetch_klines(s, tf, CFG["pages"])
                d4 = fetch_klines(s, htf, CFG["pages_htf"])
                if d1 and len(d1["c"]) >= min_hist:
                    cache[s] = (d1, d4)
            except Exception as ex:
                print("bt_filters fetch skip", s, ex)
            time.sleep(0.03)

        results = {}
        # حساب SMA200 لكل رمز مرة واحدة (سيُستخدم في نسخة Extra High + SMA200)
        sma200_cache = {}
        for s, (d1, d4) in cache.items():
            c = d1["c"]
            if len(c) >= 200:
                sma200_cache[s] = sma(c, 200)
            else:
                sma200_cache[s] = None

        for name, filters_config in variants:
            CFG["require_xh"] = filters_config["xh"]
            CFG["require_circle"] = filters_config["circle"]
            use_sma200 = filters_config["sma200"]
            rs = []; n_signals = 0
            for s, (d1, d4) in cache.items():
                try:
                    h, l, c = d1["h"], d1["l"], d1["c"]
                    A = atr(h, l, c, CFG["atr_len"])
                    N = len(c)
                    step = 3
                    for cut in range(min_hist - 50, N - hold, step):
                        sub = {k: d1[k][:cut + 1] for k in ("t", "o", "h", "l", "c", "v")}
                        sig = find_setup(s, sub, d4)
                        if not sig or sig["score"] < CFG["min_score"]:
                            continue
                        # فلتر SMA200: إذا كانت النسخة الحالية تستخدم SMA200، تحقق من السعر فوق SMA200
                        if use_sma200:
                            sma200_vals = sma200_cache.get(s)
                            if sma200_vals is None or cut >= len(sma200_vals):
                                continue  # لا توجد قيمة SMA200 صالحة
                            sma200_val = sma200_vals[cut]
                            if not (math.isfinite(sma200_val) and c[cut] > sma200_val):
                                continue  # السعر ليس فوق SMA200، تخطّ
                        n_signals += 1
                        e, st = sig["entry"], sig["stop"]
                        av = A[cut] if (A[cut] and math.isfinite(A[cut])) else (e - st)
                        rb = _sim_trail_partial(e, st, sig["tp1"], av, h, l, c, cut, hold)
                        if rb is not None:
                            rs.append(rb)
                except Exception as ex:
                    print("bt_filters skip", s, ex)
            print(f"[{name}] إشارات مؤهلة={n_signals} · {_stats(rs)}")
            results[name] = {"n_signals": n_signals, "trades": rs}
        all_results[f"{tf}/{htf}"] = results

    CFG["require_xh"] = 0; CFG["require_circle"] = 0   # إعادة الافتراضي (بلا أثر حيّ أصلاً)
    return all_results


# ----------------------- تنفيذ تجريبي على بايبت/بايننس -----------------------
def execute_signals(signals):
    """تنفيذ شراء تجريبي (Bybit Testnet / Binance Demo) لأعلى الإشارات، خلف أعلام بيئة.
       HUNTER_EXEC_BYBIT=1 و/أو HUNTER_EXEC_BINANCE=1 لتفعيله. USDT لكل صفقة عبر HUNTER_USDT."""
    usdt = float(os.environ.get("HUNTER_USDT", "50"))
    do_bybit = os.environ.get("HUNTER_EXEC_BYBIT", "0") == "1"
    do_binance = os.environ.get("HUNTER_EXEC_BINANCE", "0") == "1"
    if not (do_bybit or do_binance):
        print("التنفيذ معطّل (اضبط HUNTER_EXEC_BYBIT=1 أو HUNTER_EXEC_BINANCE=1)"); return
    for s in signals:
        sym = s["sym"]
        if do_bybit:
            try:
                import bybit_exec
                res = bybit_exec.market_buy(sym, usdt)
                print(f"[Bybit] شراء {sym} بـ{usdt}$ →", res)
            except Exception as ex:
                print(f"[Bybit] فشل {sym}:", ex)
        if do_binance:
            try:
                import binance_exec
                res = binance_exec.market_buy(sym, usdt)
                print(f"[Binance] شراء {sym} بـ{usdt}$ →", res)
            except Exception as ex:
                print(f"[Binance] فشل {sym}:", ex)


# ----------------------- main -----------------------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "backtest":
        backtest()
    elif mode == "backtest_filters":
        backtest_entry_filters()
    elif mode == "monitor":
        monitor_positions()          # إدارة الخروج الحيّة (نظام ب) للمراكز المفتوحة
    elif mode == "exec":
        sigs = scan(send=True)
        if sigs:
            execute_signals(sigs)
    else:  # scan
        scan()

if __name__ == "__main__":
    main()
