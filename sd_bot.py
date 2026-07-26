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
from concurrent.futures import ThreadPoolExecutor

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
    # ── تشديد بنية الاتجاه (2026-07-06، طلب بو محمد) — قابلة للإطفاء للمقارنة ──
    require_hh=0,          # يدخل فقط إذا كانت قمة الـCHoCH ≥ القمة المحورية السابقة (قمة أعلى لا أدنى)
    require_macd4c=0,      # يشترط زخم MACD 4C صعودياً عند الدخول (لا قمة أدنى في الزخم)
    macd4c_min=1,          # أدنى حالة MACD4C مقبولة: 1=أخضر (hist>0)، 2=أخضر متنامٍ فقط، -1=يشمل بداية التحوّل
    require_os21=0,        # يشترط تشبّعاً بيعياً واحداً+ على RSI21 (≤ rsi21_os) قبل الـCHoCH
    rsi21_os=30,           # عتبة التشبّع البيعي على RSI21 قبل CHoCH
    # سلالم فيبو للتوسيط تحت الدخول المباشر (نِسَب تصحيح لساق الاندفاع leg_low→leg_high)
    dca_fibs=(0.382, 0.5, 0.618, 0.786),
    # ── إدارة الخروج نظام ب (الفائزة في بوت الصيد): جني جزئي + وقف متحرّك شانديلير ──
    tp1_frac=0.5,          # نسبة الجني عند الهدف الأول ثم تتبّع الباقي
    trail_atr=2.5,         # مضاعف وقف شانديلير المتحرّك (قمة − trail_atr×ATR) للباقي
    bt_hold=48,            # (backtest) أقصى شموع لإمساك الصفقة
    fee_rate=0.00075,      # (backtest) رسوم جهة واحدة (كسر من السعر) — 0.075% تقريب رسوم تيكر
    # ── بوابة الحجم (2026-07-12، بعد فحص المتانة على 80 رمز) ──
    # touchVolZ≥1.5 = الفائز الموثوق على الفريمين (صمد في نصفي holdout، منحنى مونوتوني):
    # يرفع التوقّع من ~+0.7R إلى ~+1.0-1.5R مقابل احتفاظ ~25% من الصفقات.
    # الافتراضي 0 (معطّل) كي لا يمسّ البوتين التجريبيين؛ يُفعَّل للرئيسي عبر SD_MIN_TOUCHVOLZ=1.5.
    min_touchvolz=0.0,
)
# ── تجاوز فريم الدخول/السياق عبر البيئة (لتشغيل البوت على كل الفريمات: 15m/1h/4h) ──
# مثال: SD_ENTRY_TF=15m SD_HTF=1h  |  SD_ENTRY_TF=4h SD_HTF=1d
CFG["entry_tf"] = os.environ.get("SD_ENTRY_TF", CFG["entry_tf"])
CFG["htf"]      = os.environ.get("SD_HTF", CFG["htf"])
CFG["bt_hold"]  = int(os.environ.get("SD_BT_HOLD", CFG["bt_hold"]))
CFG["fee_rate"] = float(os.environ.get("SD_FEE_RATE", CFG["fee_rate"]))
CFG["pages_1h"] = int(os.environ.get("SD_PAGES", CFG["pages_1h"]))   # تقليل الصفحات = تسريع الجلب
CFG["require_choch"] = int(os.environ.get("SD_REQUIRE_CHOCH", CFG["require_choch"]))
CFG["max_ema_dist"]  = float(os.environ.get("SD_MAX_EMA_DIST", CFG["max_ema_dist"]))
CFG["trail_atr"]     = float(os.environ.get("SD_TRAIL_ATR", CFG["trail_atr"]))
CFG["tp1_frac"]      = float(os.environ.get("SD_TP1_FRAC", CFG["tp1_frac"]))
CFG["require_ob_after_os"] = int(os.environ.get("SD_REQUIRE_OBOS", CFG["require_ob_after_os"]))
CFG["min_touchvolz"] = float(os.environ.get("SD_MIN_TOUCHVOLZ", CFG["min_touchvolz"]))  # بوابة الحجم
CFG["os_lookback"]   = int(os.environ.get("SD_OS_LOOKBACK", CFG["os_lookback"]))
CFG["rsi_entry_len"] = int(os.environ.get("SD_RSI_ENTRY_LEN", CFG["rsi_entry_len"]))
CFG["rsi_entry_ob"]  = float(os.environ.get("SD_RSI_ENTRY_OB", CFG["rsi_entry_ob"]))
CFG["require_hh"]     = int(os.environ.get("SD_REQUIRE_HH", CFG["require_hh"]))
CFG["require_macd4c"] = int(os.environ.get("SD_REQUIRE_MACD4C", CFG["require_macd4c"]))
CFG["macd4c_min"]     = int(os.environ.get("SD_MACD4C_MIN", CFG["macd4c_min"]))
CFG["require_os21"]   = int(os.environ.get("SD_REQUIRE_OS21", CFG["require_os21"]))
CFG["rsi21_os"]       = float(os.environ.get("SD_RSI21_OS", CFG["rsi21_os"]))
# دايفرجنس صعودي على هيستوجرام MACD كبوابة دخول اختيارية (0 = معطّل، الافتراضي)
CFG["require_div"]    = int(os.environ.get("SD_REQUIRE_DIV", 0))
# شرط إضافي على الدايفرجنس (طلب بو محمد): أن يتكوّن قاعاه والسعر مغلق تحت SMA(n).
# n = طول المتوسط (الافتراضي 50 = SMA50)؛ 0 = تعطيل الشرط.
CFG["div_below_ma"]   = int(os.environ.get("SD_DIV_BELOW_MA", 50))
# فلتر الاتجاه القابل للاختيار: live = EMA الحيّ (الافتراضي)، أو smaN / emaN
CFG["trend_ma"]       = os.environ.get("SD_TREND_MA", "live").strip().lower()
# عتبة فلتر التعلّم الآلي (0 = تعطيل الترشيح ML، مطابق للباك-تست)
CFG["ml_threshold"]   = float(os.environ.get("SD_ML_THRESHOLD", CFG["ml_threshold"]))
# نمط الدخول حسب الفريم (طلب بو محمد 2026-07-05): 15m = زخم RSI + توسيط DCA؛
# الساعة (وأعلى) = اختراق قمة الـCHoCH. قابل للتجاوز عبر SD_ENTRY_MODE (momentum/breakout).
# نمط الدخول (طلب بو محمد 2026-07-05): كلا الفريمين = اختراق قمة الـCHoCH (الأقوى على 1h)؛
# 15m يضيف توسيط DCA بفيبو فوق الاختراق. زخم RSI يبقى متاحاً عبر SD_ENTRY_MODE=momentum.
CFG["entry_mode"] = os.environ.get("SD_ENTRY_MODE", "breakout")
# توسيط DCA بفيبو: مفعّل على 15m فقط افتراضياً (دخول مباشر + سلالم فيبو للمعدّل). SD_USE_DCA للتجاوز.
CFG["use_dca"] = int(os.environ.get("SD_USE_DCA", 1 if CFG["entry_tf"] == "15m" else 0))
# فلتر الاتجاه: EMA365 افتراضياً على 15m (تجربة بو محمد 2026-07-05) بدل EMA200، وإلا 200.
# قابل للتجاوز عبر SD_EMA_LEN.
CFG["ema_len"] = int(os.environ.get(
    "SD_EMA_LEN", 365 if CFG["entry_tf"] == "15m" else CFG["ema_len"]))
# نافذة الحداثة: أقصى شموع بين تكوين المنطقة والدخول (تضييقها = دخول أبكر = أقل تأخّراً)
CFG["max_bars_to_touch"] = int(os.environ.get("SD_MAX_BARS", CFG["max_bars_to_touch"]))
# ═══ استراتيجية «الفيواب الأسبوعي» الجديدة (طلب بو محمد 2026-07-17) ═══
# SD_STRATEGY=vwap_wave تستبدل محرك العرض/الطلب القديم بالكامل (لا مناطق/CHoCH/ML/حجم):
#   1) تشبّع بيعي RSI21 ≤ 20 (لمسة أو أكثر بنفس الموجة)
#   2) اختراق الفيواب الأسبوعي ثم RSI21 ≥ 80 والسعر فوق الفيواب (فلتر الاتجاه)
#   3) نهاية الموجة: تحوّل هيستوجرام MACD 4C من الأخضر إلى الأحمر ← إشارة
#   الدخول: DCA بانتظار مستويات فيبو التصحيح الأربعة (بلا شراء فوري)
#   الوقف: قاع الموجة التي اخترقت الفيواب أول مرة · الأهداف: امتداد 1.272 و1.618
CFG["strategy"] = os.environ.get("SD_STRATEGY", "legacy").strip().lower()
CFG["vw_os"] = float(os.environ.get("SD_VW_OS", "20"))   # عتبة التشبّع البيعي RSI21
CFG["vw_ob"] = float(os.environ.get("SD_VW_OB", "80"))   # عتبة التشبّع الشرائي RSI21
# نوع مؤشر التشبّع: classic = RSI(21) الكلاسيكي القياسي (الافتراضي منذ 2026-07-26 بطلب بو محمد
# بعد أن ثبت أن Ultimate RSI يطلق التشبّع 80 على حركات يكون فيها RSI21 الكلاسيكي ~70 مثل ARPAUSDT).
# ultimate = Ultimate RSI من LuxAlgo (متاح عبر SD_VW_RSI=ultimate).
CFG["vw_rsi"] = os.environ.get("SD_VW_RSI", "classic").strip().lower()
# صلاحية إشارة «انتظار المستويات» قبل أن يتجاهلها المنفّذ (ساعات)
CFG["wait_max_age_h"] = float(os.environ.get("SD_WAIT_MAX_AGE_H", "48"))
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

# ═══ قاعدة رئيسية دائمة (طلب بو محمد 2026-07-20) ═══
# أي صفقة يكون هدفها الأول (TP1) أقرب من 1% من سعر الدخول تُلغى ولا يُدخل فيها نهائياً
# لأنها غير مجدية (الربح المحتمل لا يغطّي العمولات والمخاطرة). تُطبَّق على كل البوتات.
MIN_TP1_PCT = float(os.environ.get("MIN_TP1_PCT", "1.3"))

def tp1_too_close(entry, tp1):
    """True إذا كان الهدف الأول أقرب من الحد الأدنى (%) ← ترفض الإشارة."""
    try:
        entry = float(entry); tp1 = float(tp1)
        if entry <= 0:
            return False
        return abs(tp1 - entry) / entry * 100.0 < MIN_TP1_PCT
    except Exception:
        return False

# سجل المتتبّع المشترك: نكتب فيه إشاراتنا لتظهر وتُتابَع في اللوحة مثل بقية البوتات.
# (يتابعها trackmon في reversal.yml كل 15 دقيقة ويُصدّر paper_data.json للوحة)
TRACK_FILE = os.environ.get("SD_TRACK", "tracked_signals.json")  # ملف التتبّع (قابل للعزل لكل بوت عبر SD_TRACK)
DASH_LABEL = os.environ.get("SD_LABEL", "العرض/الطلب")   # اسم/وسم البوت (لتمييز الإشارات والتنفيذ)
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

def sma(arr, n):
    """متوسط متحرك بسيط بطول n؛ NaN للشمعات الأولى قبل اكتمال النافذة."""
    out = [float("nan")] * len(arr); s = 0.0
    for i, x in enumerate(arr):
        s += x
        if i >= n:
            s -= arr[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out

def trend_ma_series(c):
    """سلسلة فلتر الاتجاه المختار عبر CFG['trend_ma']:
       'live' ⇒ None (استخدم فلتر EMA الحيّ f['emaRel'])؛ 'smaN'/'emaN' ⇒ المتوسط المطلوب."""
    raw = CFG.get("trend_ma", "live")
    if raw in ("", "live"):
        return None
    kind = "sma" if raw.startswith("sma") else ("ema" if raw.startswith("ema") else None)
    try:
        n = int(raw[3:])
    except ValueError:
        n = 0
    if kind == "sma" and n > 0:
        return sma(c, n)
    if kind == "ema" and n > 0:
        return ema(c, n)
    return None

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

def macd(c, fast=12, slow=26, sig=9):
    """MACD كلاسيكي (12,26,9). يعيد (خط، إشارة، هيستوجرام)."""
    ef = ema(c, fast); es = ema(c, slow)
    line = [ef[i] - es[i] for i in range(len(c))]
    signal = ema(line, sig)
    hist = [line[i] - signal[i] for i in range(len(c))]
    return line, signal, hist

def macd4c_state(hist, i):
    """حالة MACD «4 ألوان» عند شمعة i — تكشف قمة أعلى/أدنى في الزخم:
       2 = أخضر متنامٍ (hist>0 وصاعد)   = زخم صعودي قوي / قمة أعلى
       1 = أخضر خافت (hist>0 وهابط)     = زخم صعودي يخفت
      -1 = أحمر خافت (hist<0 وصاعد)     = زخم هبوطي يخفت (بداية تحوّل صعودي)
      -2 = أحمر متنامٍ (hist<0 وهابط)   = زخم هبوطي قوي / قمة أدنى
       0 = غير محدّد (بيانات ناقصة)."""
    if i < 1 or not (math.isfinite(hist[i]) and math.isfinite(hist[i - 1])):
        return 0
    up = hist[i] > hist[i - 1]
    if hist[i] >= 0:
        return 2 if up else 1
    return -1 if up else -2

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
    if CFG["use_dca"]:                                   # 15m: دخول مباشر + سلالم توسيط DCA بفيبو
        ladder = [leg_high - lv * span for lv in CFG["dca_fibs"]]
        legs = [entry] + [p for p in ladder if stop < p < entry]    # بين الوقف والدخول فقط
    else:                                               # 1h: دخول اختراق واحد بلا توسيط
        legs = [entry]
    _exts = [leg_low + m * span for m in (1.272, CFG["tp2_ext"], 2.0, 2.618)]  # أهداف امتداد فيبو
    _above = [x for x in _exts if x > entry]
    tp1, tp2 = (_above[0], _above[1]) if len(_above) >= 2 else (entry + 0.618 * span, entry + span)
    return dict(tch=tch, entry=entry, stop=stop, tp1=tp1, tp2=tp2, legs=legs,
                leg_low=leg_low, leg_high=leg_high, peak_idx=peak_idx, atch=atch)

# ----------------------- ميزات الإعداد -----------------------
def setup_features(sym, d1, d4):
    o, h, l, c, v, t = d1["o"], d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
    a = atr(h, l, c, CFG["atr_len"]); vz = vol_z(v, CFG["vol_len"]); e200 = ema(c, CFG["ema_len"])
    ma_div = sma(c, CFG["div_below_ma"]) if CFG["div_below_ma"] else None  # SMA50 لشرط «دايفرجنس تحت المتوسط»
    rs = rsi(c, CFG["rsi_len"])
    rs_en = rsi(c, CFG["rsi_entry_len"])          # RSI(21) لإشارة الدخول الزخمي
    piv, ev = structure(h, l, c, CFG["pivL"], CFG["pivR"])
    low_idx = [p[0] for p in piv if p[2] == "L"]  # مؤشّرات القيعان المحورية (لقاع الموجة)
    choch_hi = choch_high_levels(c, piv, CFG["pivR"])   # مستويات قمم الـCHoCH (لدخول الاختراق)
    hi_pivs = [(p[0], p[1]) for p in piv if p[2] == "H"]  # القمم المحورية (idx, سعر) لفحص قمة-أعلى
    _, _, mhist = macd(c)                               # هيستوجرام MACD 4C لتأكيد زخم القمة
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
        # ── تشديد بنية الاتجاه (2026-07-06) ──
        # (1) قمة أعلى: مستوى قمة الـCHoCH المكسور يجب أن يكون ≥ القمة المحورية السابقة له.
        cb = next((b for b in (j, j + 1, j + 2) if b in choch_hi), None)
        hh = 1
        if cb is not None:
            lvl = choch_hi[cb]
            hs = [pp for (pi, pp) in hi_pivs if pi < cb]   # أسعار القمم قبل الكسر بترتيب الزمن
            hh = 1 if (len(hs) < 2 or hs[-1] >= hs[-2]) else 0  # القمة المكسورة ≥ التي قبلها
        # (2) MACD 4C: حالة الزخم اللونية عند شمعة الدخول (2=أخضر متنامٍ … -2=أحمر متنامٍ)
        m4 = macd4c_state(mhist, tch)
        # (3) تشبّع بيعي على RSI21 (≤ عتبة) مرة+ قبل بدء موجة الـCHoCH
        os21 = 1 if any(math.isfinite(rs_en[x]) and rs_en[x] <= CFG["rsi21_os"]
                        for x in range(os_lo, j + 1)) else 0
        # (4) دايفرجنس صعودي «عادي» على هيستوجرام MACD: آخر قاعين محوريين للسعر في النافذة
        #     قبل الموجة — قاع سعر أدنى + قاع هيستوجرام أعلى (بوادر انعكاس صعودي).
        _dpts = [x for x in low_idx if os_lo <= x <= j and math.isfinite(mhist[x])]
        bulldiv = 1 if (len(_dpts) >= 2 and l[_dpts[-1]] < l[_dpts[-2]]
                        and mhist[_dpts[-1]] > mhist[_dpts[-2]]) else 0
        # شرط إضافي (طلب بو محمد): الدايفرجنس يتكوّن والسعر تحت SMA50 —
        # قاعا الدايفرجنس (الأقدم والأحدث) مغلقان تحت المتوسط.
        if bulldiv and ma_div is not None:
            a_, b_ = _dpts[-2], _dpts[-1]
            if not (math.isfinite(ma_div[a_]) and math.isfinite(ma_div[b_])
                    and c[a_] < ma_div[a_] and c[b_] < ma_div[b_]):
                bulldiv = 0
        f = dict(strength=z["strength"],
                 heightATR=round((z["proximal"] - z["distal"]) / (a[j] or R), 2),
                 baseVolZ=round(vz[j] or 0, 2), touchVolZ=round(vz[tch] or 0, 2),
                 bos=bos, choch=choch, rsiObOs=rsi_obos, fvg=fvg, sweep=sweep, htf=hb(t[tch]),
                 emaRel=round(ema_rel, 4), barsToTouch=tch - j,
                 hour=dt.datetime.fromtimestamp(t[tch] / 1000, dt.timezone.utc).hour,
                 confirm=confirm, closeLoc=round(close_loc, 2),
                 hh=hh, macd4c=m4, os21=os21, bulldiv=bulldiv)
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
    if CFG["strategy"] == "whale":         # زخم سيولة الحيتان: بلا نموذج ولا مناطق
        return scan_whale(basket)
    if CFG["strategy"] == "vwap_wave":     # الاستراتيجية الجديدة: بلا نموذج ولا مناطق
        return scan_vwave(basket)
    bundle = load_model()
    if bundle and bundle.get("keys") != ML_KEYS:
        print("model feature-set changed -> retraining"); train(); bundle = load_model()
    if not bundle:
        print("no model; run train first"); return []
    model = bundle["model"]
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:60]
    state = load_state(); sent = set(state.get("sent", []))
    signals = []
    # جلب بيانات كل الرموز بالتوازي (شبكة I/O) ثم معالجة تسلسلية بنفس المنطق تماماً.
    # يختصر زمن المسح من دقائق لثوانٍ بلا تغيير في منطق الإشارة/الترتيب. SD_SCAN_WORKERS يضبط عدد الخيوط.
    def _fetch_pair(s):
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], 2)
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_4h"])
            return s, d1, d4
        except Exception:
            return s, None, None
    _workers = max(1, int(os.environ.get("SD_SCAN_WORKERS", "8")))
    with ThreadPoolExecutor(max_workers=_workers) as _pool:
        fetched = list(_pool.map(_fetch_pair, basket))
    for s, d1, d4 in fetched:
        try:
            if not d1 or not d4 or len(d1["c"]) < 300:
                continue
            setups, h, l, c = setup_features(s, d1, d4)
            last = len(c) - 1
            ma = trend_ma_series(c)            # فلتر الاتجاه المختار (None = EMA الحيّ)
            for st in setups:
                if st["touch"] != last:        # الدخول تحقّق على الشمعة المغلقة الأخيرة فقط
                    continue
                f = st["f"]
                tchk = st["touch"]
                # فلتر الاتجاه: rel = بُعد السعر فوق المتوسط عند شمعة اللمس
                if ma is None:
                    rel = f["emaRel"]          # الفلتر الحيّ (EMA200/365)
                else:
                    m = ma[tchk] if tchk < len(ma) else float("nan")
                    if not math.isfinite(m) or m <= 0:
                        continue
                    rel = (c[tchk] - m) / m
                if rel <= 0:                   # فلتر E: فوق المتوسط
                    continue
                if CFG["max_ema_dist"] and rel > CFG["max_ema_dist"]:  # قريب من الاتجاه لا متمدّد
                    continue
                if f["htf"] < 0:               # فلتر E: 4h غير هابط
                    continue
                if CFG["require_choch"] and not f["choch"]:  # CHoCH إلزامي: بداية موجة/انعكاس لا استمرار
                    continue
                if CFG["require_hh"] and not f["hh"]:         # قمة الـCHoCH ≥ القمة السابقة (قمة أعلى)
                    continue
                if CFG["require_macd4c"] and f["macd4c"] < CFG["macd4c_min"]:  # زخم MACD 4C صعودي
                    continue
                if CFG["require_os21"] and not f["os21"]:     # تشبّع بيعي RSI21 قبل CHoCH
                    continue
                if CFG["require_div"] and not f.get("bulldiv"):  # دايفرجنس صعودي على هيستوجرام MACD
                    continue
                if CFG["require_ob_after_os"] and not f["rsiObOs"]:  # تشبّع شرائي بعد بيعي (فوق CHoCH)
                    continue
                if CFG["require_confirm"] and not f["confirm"]:   # شمعة تأكيد إلزامية
                    continue
                if f["heightATR"] > CFG["max_height_atr"]:        # رفض المناطق الفضفاضة
                    continue
                if f["barsToTouch"] > CFG["max_bars_to_touch"]:   # رفض المناطق المسنّة
                    continue
                if CFG["min_touchvolz"] and (f.get("touchVolZ", 0.0) or 0.0) < CFG["min_touchvolz"]:
                    continue                     # بوابة الحجم: قفزة حجم عند شمعة الدخول (فحص متانة 2026-07-12)
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
                if tp1_too_close(entry, st["tp1"]):   # هدف أول < 1% ← إلغاء الصفقة
                    continue
                signals.append(dict(key=key, sym=s, prob=round(float(prob), 3),
                    tf=CFG["entry_tf"],
                    entry=round(entry, 8), stop=round(stop, 8),
                    legs=[round(p, 8) for p in st.get("legs", [entry])],
                    tp1=round(st["tp1"], 8), tp2=round(st["tp2"], 8), ts=st["ts"],
                    reasons=_reasons(f)))
        except Exception as ex:
            print("scan skip", s, ex)
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
    r = [(f"دخول زخم RSI{CFG['rsi_entry_len']}≥{CFG['rsi_entry_ob']:.0f}"
          if CFG["entry_mode"] == "momentum" else "دخول اختراق قمة الـCHoCH")
         + (" + توسيط DCA بفيبو" if CFG["use_dca"] else "")]
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
            f"🟢 إشارة {DASH_LABEL} — شراء",
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

def send_telegram(text, reply_to=None):
    """يرسل رسالة تيليجرام (HTML). reply_to = message_id لجعلها رداً على رسالة الصفقة الأصلية.
    يُرجع message_id للرسالة المُرسَلة (أو None)."""
    if not TG_TOKEN or not TG_CHAT:
        print("TG not configured; message:\n", text); return None
    try:
        payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data=payload, timeout=15)
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
        # حقول استراتيجية الفيواب الأسبوعي: سلالم DCA للمنفّذ + وضع انتظار المستويات
        if s.get("dca_levels"):
            data[key]["dca_levels"] = s["dca_levels"]
        if s.get("wait_entry"):
            data[key]["wait_entry"] = True
            data[key]["max_age_h"] = s.get("max_age_h", 48)
        added += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"tracked {added} signals to {path}")

# ═══════════ استراتيجية «الفيواب الأسبوعي + تشبّع RSI21 + MACD4C» (2026-07-17) ═══════════
def rma(arr, n):
    """متوسط Wilder (RMA): يُبذَر بمتوسط أول n قيمة صالحة ثم rma=(prev*(n-1)+x)/n."""
    out = [float("nan")] * len(arr)
    prev = None; buf = []
    for i, x in enumerate(arr):
        if not math.isfinite(x):
            continue
        if prev is None:
            buf.append(x)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
        else:
            prev = (prev * (n - 1) + x) / n
            out[i] = prev
    return out


def ultimate_rsi(c, n):
    """Ultimate RSI — ترجمة أمينة لمؤشر LuxAlgo (Pine v5، طريقة RMA الافتراضية):
      upper/lower = أعلى/أدنى إغلاق في نافذة n
      diff = +r عند قمة نافذة جديدة، −r عند قاع نافذة جديد، وإلا فرق الإغلاقين (r = upper−lower)
      arsi = RMA(diff,n) / RMA(|diff|,n) × 50 + 50
    (المصدر مرخّص CC BY-NC-SA 4.0 © LuxAlgo — استخدام شخصي غير تجاري.)"""
    m = len(c)
    diff = [float("nan")] * m
    prev_up = prev_dn = None
    for i in range(m):
        if i < n - 1:
            continue
        win = c[i - n + 1:i + 1]
        up, dn = max(win), min(win)
        if prev_up is not None:
            r = up - dn
            if up > prev_up:
                diff[i] = r
            elif dn < prev_dn:
                diff[i] = -r
            else:
                diff[i] = c[i] - c[i - 1]
        prev_up, prev_dn = up, dn
    num = rma(diff, n)
    den = rma([abs(x) if math.isfinite(x) else x for x in diff], n)
    out = [float("nan")] * m
    for i in range(m):
        if math.isfinite(num[i]) and math.isfinite(den[i]) and den[i] > 0:
            out[i] = num[i] / den[i] * 50 + 50
    return out


def vwap_weekly(t, h, l, c, v):
    """فيواب أسبوعي مرسّى: يتجمّع (سعر نموذجي × حجم) ويُصفَّر مع بداية كل أسبوع ISO (UTC)."""
    out = [float("nan")] * len(c)
    cum_pv = cum_v = 0.0
    wk = None
    for i in range(len(c)):
        d = dt.datetime.fromtimestamp(t[i] / 1000, dt.timezone.utc)
        k = d.isocalendar()[:2]                 # (سنة، رقم الأسبوع)
        if k != wk:
            wk = k
            cum_pv = cum_v = 0.0
        tp = (h[i] + l[i] + c[i]) / 3.0
        cum_pv += tp * v[i]
        cum_v += v[i]
        if cum_v > 0:
            out[i] = cum_pv / cum_v
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  استراتيجية «زخم سيولة الحيتان» (Whale Liquidity Momentum) — SD_STRATEGY=whale
#  مبنية على ملف whale_liquidity_momentum_strategy.md (2026-07-20).
#  الفكرة: الفوليوم يفضح الحيتان. ندخل في «النافذة الذهبية» (المرحلة 3 من تسلسل
#  دخول الحيتان: كنس سيولة → قاعدة هادئة → اختراق مصغّر بحجم معتدل)، ونتجنّب
#  «تسارع الجمهور» (المرحلة 4) و«قمة التوزيع» (Pattern B). فريم القرار = 15د دائماً،
#  والفريمات الأعلى (1س/4س) للسياق فقط. إشارات فقط — لا تنفيذ آلي.
#  ملاحظة: نسخة Python تقارِب مؤشرات TradingView من بيانات OHLCV الخام:
#    Volume Profile (POC/VAH/VAL)، مستويات السيولة (قمم/قيعان محورية)، VWAP
#    أسبوعي/شهري، OBV، MFI، MACD4C. غير مُنفَّذ: ذاكرة momentum وaccumulation_footprint
#    والفلتر الكلّي (BTC.D/USDT.D) — تحتاج مصادر TradingView خارجية.
# ═══════════════════════════════════════════════════════════════════════════
WHALE = dict(
    qbase_win   = int(os.environ.get("WH_QBASE_WIN", "40")),   # نافذة القاعدة الهادئة (8-12س على 15د)
    qbase_skip  = int(os.environ.get("WH_QBASE_SKIP", "2")),   # تخطّي آخر شمعات (قد تكون الانفجار)
    qbase_trim  = float(os.environ.get("WH_QBASE_TRIM", "0.20")),  # اقتطاع أعلى % حجماً (سبايكات) من القاعدة
    rel_vol_min = float(os.environ.get("WH_RELVOL_MIN", "2.0")),   # الحجم النسبي الأدنى (rel_volume>2x)
    golden_lo   = float(os.environ.get("WH_GOLDEN_LO", "3.0")),    # النافذة الذهبية: مضاعف حجم أدنى (×القاعدة)
    golden_hi   = float(os.environ.get("WH_GOLDEN_HI", "9.0")),    # النافذة الذهبية: مضاعف حجم أقصى
    chase_ratio = float(os.environ.get("WH_CHASE_RATIO", "15.0")), # فوقه = مطاردة/تسارع جمهور (لا دخول)
    z_high      = float(os.environ.get("WH_Z_HIGH", "2.5")),       # عتبة z للتصنيف High (قمة توزيع محتملة)
    dist_rsi    = float(os.environ.get("WH_DIST_RSI", "65")),      # Pattern B: RSI21≥هذا + حجم High = توزيع
    mfi_len     = int(os.environ.get("WH_MFI_LEN", "14")),
    mfi_chase   = float(os.environ.get("WH_MFI_CHASE", "85")),     # MFI(4س) فوقه = مطاردة (لا دخول)
    min_dollar_vol = float(os.environ.get("WH_MIN_DOLLAR_VOL", "30000")),  # حارس السوق الرقيق (سيولة $/شمعة)
    obv_lb      = int(os.environ.get("WH_OBV_LB", "10")),          # نافذة تأكيد صعود OBV
    brk_lb      = int(os.environ.get("WH_BRK_LB", "12")),          # نافذة الاختراق المصغّر (higher-high)
    stop_atr    = float(os.environ.get("WH_STOP_ATR", "0.3")),     # حاجز الوقف تحت قاع القاعدة (×ATR)
    res_lb      = int(os.environ.get("WH_RES_LB", "60")),          # نافذة البحث عن مقاومات/جدران سيولة للأهداف
    vp_lb       = int(os.environ.get("WH_VP_LB", "480")),          # نافذة Volume Profile (~5 أيام على 15د)
    vp_bins     = int(os.environ.get("WH_VP_BINS", "50")),
    # ── تحديث 2026-07-20 (momentum_project_log): حجم موزّع + كنس سيولة ──
    dist_ratio  = float(os.environ.get("WH_DIST_RATIO", "1.5")),   # عتبة مضاعف الحجم لعدّ شمعة «مرتفعة»
    dist_min    = int(os.environ.get("WH_DIST_MIN", "2")),         # أدنى عدد شمعات مرتفعة = حجم موزّع (لا سبايك منفرد)
    require_distributed = int(os.environ.get("WH_REQUIRE_DIST", "1")),   # اشتراط حجم موزّع (نمط ج: قناعة > سبايك)
    sweep_lb    = int(os.environ.get("WH_SWEEP_LB", "20")),        # نافذة البحث عن كنس سيولة قبل الاختراق
    sweep_base  = int(os.environ.get("WH_SWEEP_BASE", "10")),      # نافذة قاع النطاق لتعريف الكنس
    require_sweep = int(os.environ.get("WH_REQUIRE_SWEEP", "0")),  # اشتراط كنس سيولة (نمط أ) — افتراضي: إبلاغ فقط
    # ── تحديث 2026-07-21 (جلسة الزخم البحثي — دروس ARKM): رفض الحركة المنتهية + بصمة تجميع مبكر ──
    reject_exhausted = int(os.environ.get("WH_REJECT_EXHAUSTED", "1")),  # رفض الاندفاع المنتهي (سبايك عمودي مضى)
    exhaust_lb  = int(os.environ.get("WH_EXHAUST_LB", "16")),      # نافذة فحص السبايك العمودي المنتهي (4س على 15د)
    footprint_mult = float(os.environ.get("WH_FOOTPRINT_MULT", "12")),  # مضاعف حجم «بصمة حوت» (تجميع مبكر)
    footprint_lb   = int(os.environ.get("WH_FOOTPRINT_LB", "192")),     # نافذة البحث عن البصمة (48س على 15د)
    require_footprint = int(os.environ.get("WH_REQUIRE_FOOTPRINT", "0")),  # اشتراط بصمة تجميع سابقة — افتراضي: إبلاغ
)


def _in_whale_window(ts_ms):
    """نافذة تجميع الحيتان (بطلب بو محمد 2026-07-21): فترة واسعة من 12:00 ظهراً بتوقيت الإمارات
    إلى 08:00 صباح اليوم التالي (تشمل الظهر/المساء/الليل/الفجر). بالإمارات UTC+4 يقابلها UTC:
    12:00 الإمارات = 08:00 UTC، و08:00 الإمارات = 04:00 UTC → النافذة = ساعة UTC ≥ 8 أو < 4.
    المستبعَد الوحيد: صباح الإمارات 08:00–12:00 (= 04:00–08:00 UTC)."""
    h = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).hour
    return h >= 8 or h < 4


def _whale_footprint(v, qm, i, lb, mult):
    """يبحث عن "بصمة حوت" (حجم ≥ mult×القاعدة الهادئة) خلال آخر lb شمعة قبل i = تجميع مبكر (24–48س
    قبل الاندفاع). المبدأ: الحوت يجمّع بهدوء قبل الصعود بيوم/يومين، والحجم يفضحه. اصطياد المُجمَّع
    أمس لا مطاردة الصاعد اليوم. يعيد (found, bars_ago) لأحدث بصمة، أو (False, None)."""
    if qm <= 0:
        return False, None
    start = max(0, i - lb)
    for j in range(i - 1, start - 1, -1):     # الأحدث أولاً
        if v[j] / qm >= mult:
            return True, i - j
    return False, None


def _distributed_volume(v, qm, i, window, ratio_min):
    """عدد الشمعات «المرتفعة» (مضاعف الحجم على القاعدة الهادئة ≥ ratio_min) داخل نافذة الاختراق.
    حجم موزّع على عدة شمعات = اختراق بقناعة (نمط ج)، أقوى من سبايك حجم منفرد."""
    if qm <= 0:
        return 0
    return sum(1 for k in range(max(0, i - window + 1), i + 1) if v[k] / qm >= ratio_min)


def _recent_sweep(l, c, i, lookback, base_lb):
    """كنس سيولة (شطف ستوبات): شمعة ضمن آخر lookback هبط قاعها تحت أدنى قاع النطاق السابق لها
    ثم أغلقت فوقه (استرداد). يمثّل المرحلة 3 من «بصمة الحوت الكامل» (نمط أ)."""
    start = max(base_lb, i - lookback)
    for j in range(start, i + 1):
        base_low = min(l[j - base_lb:j])
        if l[j] < base_low and c[j] > base_low:
            return True
    return False


def obv(c, v):
    """On-Balance Volume: تراكم/توزيع الحجم حسب اتجاه الإغلاق."""
    out = [0.0] * len(c)
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + v[i]
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - v[i]
        else:
            out[i] = out[i - 1]
    return out


def mfi(h, l, c, v, n=14):
    """Money Flow Index: RSI مرجّح بالحجم على السعر النموذجي."""
    out = [float("nan")] * len(c)
    if len(c) <= n:
        return out
    tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(len(c))]
    rmf = [tp[i] * v[i] for i in range(len(c))]
    for i in range(n, len(c)):
        pos = neg = 0.0
        for k in range(i - n + 1, i + 1):
            if tp[k] > tp[k - 1]:
                pos += rmf[k]
            elif tp[k] < tp[k - 1]:
                neg += rmf[k]
        out[i] = 100.0 if neg == 0 else 100.0 - 100.0 / (1.0 + pos / neg)
    return out


def vwap_monthly(t, h, l, c, v):
    """فيواب شهري مرسّى: يُصفَّر مع بداية كل شهر تقويمي (UTC)."""
    out = [float("nan")] * len(c)
    cum_pv = cum_v = 0.0
    mo = None
    for i in range(len(c)):
        d = dt.datetime.fromtimestamp(t[i] / 1000, dt.timezone.utc)
        k = (d.year, d.month)
        if k != mo:
            mo = k
            cum_pv = cum_v = 0.0
        tp = (h[i] + l[i] + c[i]) / 3.0
        cum_pv += tp * v[i]
        cum_v += v[i]
        if cum_v > 0:
            out[i] = cum_pv / cum_v
    return out


def quiet_baseline(v, i, win, skip, trim):
    """قاعدة الحجم الهادئة قبل الشمعة i (قسم 3.2 من الملف): متوسط وانحراف على نافذة
    سابقة، مع تخطّي آخر `skip` شمعة واقتطاع أعلى `trim` نسبةً حجماً كي لا يدخل الانفجار
    نفسه في الحساب (يرفع المتوسط/الانحراف اصطناعياً ويخفي الإشارة). يعيد (mean, std)."""
    hi = i - skip
    lo = hi - win
    if lo < 0:
        return None, None
    window = sorted(v[lo:hi])
    if not window:
        return None, None
    cut = int(len(window) * trim)
    core = window[:len(window) - cut] if cut > 0 else window
    if not core:
        core = window
    m = sum(core) / len(core)
    sd = math.sqrt(sum((x - m) ** 2 for x in core) / len(core))
    return m, sd


def volume_profile(h, l, c, v, lo_i, hi_i, bins):
    """Volume Profile مبسّط على النطاق [lo_i, hi_i): يوزّع حجم كل شمعة على خانة سعرها
    النموذجي. يعيد (POC, VAH, VAL) — نقطة أعلى تحكّم + حدود منطقة القيمة 70%."""
    seg_h = h[lo_i:hi_i]; seg_l = l[lo_i:hi_i]
    seg_c = c[lo_i:hi_i]; seg_v = v[lo_i:hi_i]
    if not seg_c:
        return None, None, None
    pmin, pmax = min(seg_l), max(seg_h)
    if pmax <= pmin:
        return None, None, None
    step = (pmax - pmin) / bins
    vol = [0.0] * bins
    for j in range(len(seg_c)):
        tp = (seg_h[j] + seg_l[j] + seg_c[j]) / 3.0
        b = min(bins - 1, max(0, int((tp - pmin) / step)))
        vol[b] += seg_v[j]
    poc_b = max(range(bins), key=lambda b: vol[b])
    poc = pmin + (poc_b + 0.5) * step
    total = sum(vol); target = total * 0.70
    lo_b = hi_b = poc_b; acc = vol[poc_b]
    while acc < target and (lo_b > 0 or hi_b < bins - 1):
        down = vol[lo_b - 1] if lo_b > 0 else -1
        up = vol[hi_b + 1] if hi_b < bins - 1 else -1
        if up >= down:
            hi_b += 1; acc += max(up, 0)
        else:
            lo_b -= 1; acc += max(down, 0)
    val = pmin + lo_b * step
    vah = pmin + (hi_b + 1) * step
    return poc, vah, val


def _vol_class(z):
    """تصنيف الحجم حسب z-score (قسم 3.2): Normal/Medium/High/ExtraHigh."""
    if z is None or not math.isfinite(z):
        return "Normal"
    if z < 1.2:
        return "Normal"
    if z < 2.5:
        return "Medium"
    if z < 4.0:
        return "High"
    return "ExtraHigh"


def whale_signal(d15, mfi_ctx=None):
    """يقيّم آخر شمعة 15د مغلقة كدخول «نافذة ذهبية» (المرحلة 3). يعيد dict أو None.
    الشروط (شراء فقط):
      • انحياز: السعر فوق الفيواب الأسبوعي والشهري (اتجاه صاعد).
      • OBV صاعد + حجم نسبي > rel_vol_min.
      • حجم النافذة الذهبية: مضاعف الحجم على القاعدة الهادئة ضمن [golden_lo, golden_hi].
      • اختراق مصغّر: الإغلاق يكسر أعلى نطاق آخر brk_lb شمعة، مع قاع-أعلى محفوظ.
      • MACD4C أخضر على 15د (زخم صعودي).
      • رفض Pattern B (قمة توزيع): RSI21≥dist_rsi مع حجم High/ExtraHigh.
      • حارس المطاردة: المضاعف ≤ chase_ratio و MFI(4س) ≤ mfi_chase.
      • حارس السوق الرقيق: سيولة الدولار للشمعة ≥ min_dollar_vol.
    """
    h, l, c, v, t = d15["h"], d15["l"], d15["c"], d15["v"], d15["t"]
    n = len(c)
    if n < max(WHALE["vp_lb"], 120) + 5:
        return None
    i = n - 1
    W = WHALE

    rs21 = rsi(c, 21)
    _, _, hist = macd(c)
    vw_w = vwap_weekly(t, h, l, c, v)
    vw_m = vwap_monthly(t, h, l, c, v)
    ob = obv(c, v)
    a = atr(h, l, c, 14)

    for arr in (rs21[i], hist[i], hist[i - 1], vw_w[i], vw_m[i], a[i]):
        if not math.isfinite(arr):
            return None

    # (1) الانحياز: فوق الفيواب الأسبوعي والشهري
    if not (c[i] > vw_w[i] and c[i] > vw_m[i]):
        return None
    # (2) OBV صاعد
    if not (ob[i] > ob[i - W["obv_lb"]]):
        return None
    # (3) الحجم النسبي > العتبة
    vsma = sum(v[i - 20:i]) / 20.0 if i >= 20 else None
    if not vsma or v[i] / vsma < W["rel_vol_min"]:
        return None
    # تصنيف الحجم على القاعدة الهادئة (قسم 3.2)
    qm, qsd = quiet_baseline(v, i, W["qbase_win"], W["qbase_skip"], W["qbase_trim"])
    if not qm or qm <= 0 or not qsd or qsd <= 0:
        return None
    ratio = v[i] / qm
    z = (v[i] - qm) / qsd
    vclass = _vol_class(z)
    # (4) حجم النافذة الذهبية: معتدل (لا Normal ولا تسارع جمهور)
    if not (W["golden_lo"] <= ratio <= W["golden_hi"]):
        return None
    # (4ب) حجم موزّع لا سبايك منفرد (نمط ج، تحديث 2026-07-20): الاختراق مدعوم بعدة شمعات حجم مرتفعة
    n_elev = _distributed_volume(v, qm, i, W["brk_lb"], W["dist_ratio"])
    distributed = n_elev >= W["dist_min"]
    if W["require_distributed"] and not distributed:
        return None
    # (4ج) كنس سيولة قبل الإشعال (نمط أ «بصمة الحوت»): شطف ستوبات تحت النطاق ثم استرداد
    swept = _recent_sweep(l, c, i, W["sweep_lb"], W["sweep_base"])
    if W["require_sweep"] and not swept:
        return None
    # (4د) رفض الحركة المنتهية (درس ARKM 2026-07-21): سبايك عمودي (≥ عتبة المطاردة) وقع في النافذة
    # القريبة = الاندفاع مضى، والزخم الحالي ذيلٌ لا مقدمة. لا مطاردة ذيول الحركات المنتهية.
    if W["reject_exhausted"] and i >= W["exhaust_lb"]:
        recent_max = max(v[k] / qm for k in range(i - W["exhaust_lb"], i))
        if recent_max >= W["chase_ratio"]:
            return None
    # (4هـ) بصمة تجميع مبكر (24–48س): هل جمّع حوتٌ بهدوء قبل الاندفاع؟ (اصطياد المُجمَّع أمس)
    footprint, fp_bars = _whale_footprint(v, qm, i, W["footprint_lb"], W["footprint_mult"])
    if W["require_footprint"] and not footprint:
        return None
    # (5) اختراق مصغّر: الإغلاق يكسر أعلى النطاق السابق
    range_high = max(h[i - W["brk_lb"]:i])
    if not (c[i] > range_high):
        return None
    # قاع-أعلى محفوظ على الإغلاقات (يتحمّل كنس الفتائل: الاسترداد بالإغلاق لا يكسر البنية — نمط أ)
    recent_low = min(l[i - W["brk_lb"]:i])                 # فتيل: يُستخدم لوضع الوقف (تحت الكنس)
    recent_clow = min(c[i - W["brk_lb"]:i])                # إغلاق: يُستخدم لفحص البنية
    prior_clow = min(c[i - 2 * W["brk_lb"]:i - W["brk_lb"]]) if i >= 2 * W["brk_lb"] else recent_clow
    if recent_clow < prior_clow:
        return None
    # (6) MACD4C أخضر على 15د
    if macd4c_state(hist, i) < 1:
        return None
    # (7) رفض Pattern B — قمة توزيع الحيتان
    if rs21[i] >= W["dist_rsi"] and vclass in ("High", "ExtraHigh"):
        return None
    # (8) حارس المطاردة
    if ratio > W["chase_ratio"]:
        return None
    if mfi_ctx is not None and math.isfinite(mfi_ctx) and mfi_ctx > W["mfi_chase"]:
        return None
    # (9) حارس السوق الرقيق (سيولة دولارية)
    if v[i] * c[i] < W["min_dollar_vol"]:
        return None

    # ── الوقف والأهداف (بنيوية — جدران سيولة/مقاومات، لا R-multiples) ──
    entry = c[i]
    stop = recent_low - W["stop_atr"] * a[i]
    if stop >= entry:
        return None
    # مقاومات فوق الدخول = قمم محورية (جدران سيولة) داخل نافذة البحث
    piv = pivots(h, l, 3, 3)
    res = sorted({round(p, 10) for (pi, p, kind) in piv
                  if kind == "H" and pi >= i - W["res_lb"] and p > entry})
    # Volume Profile (POC/VAH/VAL) — سياق الغرفة وأهداف بديلة
    poc, vah, val = volume_profile(h, l, c, v, max(0, i - W["vp_lb"]), i + 1, W["vp_bins"])
    if len(res) >= 2:
        tp1, tp2 = res[0], res[1]
    elif len(res) == 1:
        tp1 = res[0]
        tp2 = vah if (vah and vah > tp1) else tp1 + (tp1 - stop)
    else:
        tp1 = vah if (vah and vah > entry) else entry + (entry - stop)
        tp2 = tp1 + (tp1 - stop)

    # قاعدة دائمة (بو محمد): لا دخول إذا كان الهدف الأول أقرب من MIN_TP1_PCT (=1% افتراضياً)
    if tp1_too_close(entry, tp1):
        return None

    return dict(
        i=i, ts=t[i], sym=None, entry=entry, stop=stop, tp1=tp1, tp2=tp2,
        distributed=distributed, n_elev=n_elev, swept=swept,
        footprint=footprint, footprint_h=(round(fp_bars / 4.0, 1) if fp_bars else None),
        in_window=_in_whale_window(t[i]),
        ratio=round(ratio, 2), z=round(z, 2), vclass=vclass,
        rsi=round(rs21[i], 1), mfi=(round(mfi_ctx, 1) if (mfi_ctx is not None and math.isfinite(mfi_ctx)) else None),
        rel_vol=round(v[i] / vsma, 2), poc=poc, vah=vah, val=val,
        dollar_vol=round(v[i] * c[i]))


def format_message_whale(signals):
    """بطاقة تيليجرام لاستراتيجية زخم سيولة الحيتان."""
    nums = ["1️⃣", "2️⃣"]
    now = dt.datetime.now().strftime("%H:%M:%S")
    sep = "\n➖➖➖➖➖➖➖➖➖\n"
    blocks = []
    for s in signals:
        entry, stop = s["entry"], s["stop"]
        risk_pct = ((entry - stop) / entry * 100) if entry else 0.0
        room = ""
        if s.get("poc"):
            pos = "تحت/عند POC (مجال للركض)" if entry <= s["poc"] * 1.005 else "فوق POC"
            room = f"  ·  {pos}"
        lines = [
            f"🟢 إشارة {DASH_LABEL} — شراء (نافذة ذهبية)",
            f"💎 {s['sym']} · ⏱️ {s.get('tf', CFG['entry_tf'])}",
            f"🐋 حجم الحيتان: {s['vclass']} · ×{s['ratio']} القاعدة الهادئة · z={s['z']} · حجم نسبي ×{s['rel_vol']}",
            f"🧱 حجم موزّع: {'نعم' if s.get('distributed') else 'لا'} ({s.get('n_elev', 0)} شمعات مرتفعة)"
            + (" · 🩸 كنس سيولة قبل الاختراق ✓" if s.get('swept') else ""),
            (f"🐋 بصمة تجميع مبكر: نعم (قبل ~{s.get('footprint_h')}س)" if s.get('footprint')
             else "🐋 بصمة تجميع مبكر: لا")
            + (" · ⏰ ضمن نافذة الحيتان (12ظ→08ص الإمارات) ✓" if s.get('in_window') else ""),
            f"📈 RSI21={s['rsi']}" + (f" · MFI(4س)={s['mfi']}" if s.get("mfi") is not None else "")
            + f" · سيولة ~${s['dollar_vol']:,}{room}",
            "",
            f"📍 الدخول (اختراق مصغّر مؤكّد): {_fmt(entry)}",
            f"🛑 الوقف (تحت قاع القاعدة): {_fmt(stop)}  (−{risk_pct:.2f}%)",
            "",
            "🎯 الأهداف (جدران سيولة/مقاومات):",
        ]
        for k, tgt in enumerate([s.get("tp1"), s.get("tp2")]):
            if not tgt:
                continue
            gain = ((tgt - entry) / entry * 100) if entry else 0.0
            lines.append(f"{nums[k]} {_fmt(tgt)}  (+{gain:.2f}%)")
        lines += [
            "",
            "⚖️ إدارة 50/50: جني 50% عند الهدف الأول + تعادل + قفل 0.3R",
            f"⏰ {now}",
        ]
        blocks.append("\n".join(lines))
    header = ("🐋 <b>زخم سيولة الحيتان — شراء</b>\n"
              "<i>كنس سيولة → قاعدة هادئة → نافذة ذهبية (حجم معتدل + اختراق مصغّر فوق الفيواب)</i>")
    footer = "⚠️ تحليل تعليمي — ليس نصيحة مالية"
    return header + sep + sep.join(blocks) + sep + footer


def scan_whale(basket=None):
    """المسح الحيّ لاستراتيجية زخم سيولة الحيتان — فريم القرار 15د، سياق MFI من 4س.
    بلا نموذج ML وبلا مناطق عرض/طلب. إشارات فقط (تيليجرام + لوحة المتتبّع)."""
    # فريم القرار 15د على كامل القائمة (btc crypto list) — لا تحديد (تفادي تفويت عملة كـACE).
    basket = basket or parse_watchlist_crypto(WATCHLIST)
    _wh_pages = int(os.environ.get("WH_PAGES", "2"))    # صفحتان ≈ 2000 شمعة (~20 يوم: فيواب شهري مرسّى)
    state = load_state(); sent = set(state.get("sent", []))
    signals = []

    def _fetch(s):
        try:
            d15 = fetch_klines(s, "15m", _wh_pages)
            d4h = fetch_klines(s, "4h", 1)
            return s, d15, d4h
        except Exception:
            return s, None, None

    _workers = max(1, int(os.environ.get("SD_SCAN_WORKERS", "8")))
    with ThreadPoolExecutor(max_workers=_workers) as _pool:
        fetched = list(_pool.map(_fetch, basket))
    for s, d15, d4h in fetched:
        try:
            if not d15 or len(d15["c"]) < 520:
                continue
            d15 = {k: vv[:-1] for k, vv in d15.items()}      # استبعاد الشمعة الجارية
            mfi_ctx = None
            if d4h and len(d4h["c"]) > WHALE["mfi_len"] + 2:
                d4 = {k: vv[:-1] for k, vv in d4h.items()}
                mvals = mfi(d4["h"], d4["l"], d4["c"], d4["v"], WHALE["mfi_len"])
                mfi_ctx = mvals[-1]
            sig = whale_signal(d15, mfi_ctx)
            if not sig:
                continue
            key = f"{s}:{sig['ts']}"
            if key in sent:
                continue
            if tp1_too_close(sig["entry"], sig["tp1"]):   # هدف أول < 1% ← إلغاء الصفقة
                continue
            sig["sym"] = s
            signals.append(dict(
                key=key, sym=s, tf="15m", ts=sig["ts"],
                entry=round(sig["entry"], 8), stop=round(sig["stop"], 8),
                legs=[round(sig["entry"], 8)], wait_entry=False,
                tp1=round(sig["tp1"], 8), tp2=round(sig["tp2"], 8),
                ratio=sig["ratio"], z=sig["z"], vclass=sig["vclass"], rsi=sig["rsi"],
                mfi=sig["mfi"], rel_vol=sig["rel_vol"], poc=sig["poc"],
                dollar_vol=sig["dollar_vol"],
                distributed=sig["distributed"], n_elev=sig["n_elev"], swept=sig["swept"],
                footprint=sig["footprint"], footprint_h=sig["footprint_h"], in_window=sig["in_window"]))
        except Exception as ex:
            print("whale skip", s, ex)
    # ترتيب: الأقرب للنافذة الذهبية المثالية (حجم معتدل عالٍ + مجال للركض)
    signals.sort(key=lambda x: x.get("ratio", 0), reverse=True)
    signals = signals[:CFG["top_n"]]
    if signals:
        mid = send_telegram(format_message_whale(signals))
        track_for_dashboard(signals, mid)
        for sig in signals:
            state.setdefault("sent", []).append(sig["key"])
        save_state(state)
    else:
        print("no whale signals this scan")
    return signals


def vwave_signal(d1):
    """آلة حالات على الشموع المغلقة (شروط بو محمد الخمسة):
      المرحلة 1: تشبّع بيعي RSI21 ≤ vw_os — لمسة أو أكثر بنفس الموجة، قاع الموجة يتحدّث.
      المرحلة 2: اختراق الفيواب الأسبوعي (يتجمّد قاع الموجة عندها) ثم RSI21 ≥ vw_ob فوق الفيواب.
      المرحلة 3: نهاية الموجة = تحوّل هيستوجرام MACD 4C من الأخضر (≥0) إلى الأحمر (<0) ← إشارة.
      إلغاء التكوين إذا كُسر قاع الموجة في أي لحظة بعد الاختراق وقبل الإشارة.
    يعيد dict إذا اكتملت الشروط على آخر شمعة مغلقة، وإلا None."""
    h, l, c, v, t = d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
    n = len(c)
    if n < 120:
        return None
    # مؤشر التشبّع: Ultimate RSI (LuxAlgo) بطول 21 — طلب بو محمد 2026-07-17.
    # SD_VW_RSI=classic يرجع لـ RSI Wilder الكلاسيكي للمقارنة.
    if CFG["vw_rsi"] == "classic":
        rs = rsi(c, CFG["rsi_entry_len"])
    else:
        rs = ultimate_rsi(c, CFG["rsi_entry_len"])
    _, _, hist = macd(c)
    vw = vwap_weekly(t, h, l, c, v)
    OS, OB = CFG["vw_os"], CFG["vw_ob"]
    phase = 0            # 0=انتظار تشبّع · 1=موجة تتكوّن · 2=فوق 80 بانتظار الأحمر
    os_hits = 0
    wave_low = None; wave_low_i = None; cross_i = None
    sig = None
    for i in range(1, n):
        if not (math.isfinite(rs[i]) and math.isfinite(vw[i]) and math.isfinite(hist[i])
                and math.isfinite(hist[i - 1])):
            continue
        if phase == 0:
            if rs[i] <= OS:                              # (1) دخول التشبّع البيعي
                phase, os_hits = 1, 1
                wave_low, wave_low_i, cross_i = l[i], i, None
        elif phase == 1:
            if rs[i] <= OS:                              # تشبّع إضافي بنفس الموجة (مسموح)
                os_hits += 1
                if c[i] < vw[i]:
                    cross_i = None                       # رجع تحت الفيواب: «أول اختراق» يصير القادم
            if cross_i is None:
                if l[i] < wave_low:
                    wave_low, wave_low_i = l[i], i       # قاع الموجة يتحدّث قبل الاختراق
                if c[i] > vw[i]:
                    cross_i = i                          # (2أ) اختراق الفيواب الأسبوعي أول مرة
            else:
                if l[i] < wave_low:                      # كسر القاع بعد الاختراق: إلغاء التكوين
                    phase, os_hits = 0, 0
                    wave_low = wave_low_i = cross_i = None
                    if rs[i] <= OS:                      # الكسر نفسه قد يبدأ تشبّعاً جديداً
                        phase, os_hits = 1, 1
                        wave_low, wave_low_i = l[i], i
                    continue
                if rs[i] >= OB and c[i] > vw[i]:         # (2ب) تشبّع شرائي فوق الفيواب
                    phase = 2
        else:                                            # phase == 2
            if l[i] < wave_low:                          # كسر القاع قبل الإشارة: إلغاء
                phase, os_hits = 0, 0
                wave_low = wave_low_i = cross_i = None
                continue
            if hist[i] < 0 <= hist[i - 1]:               # (3) أخضر ← أحمر: نهاية الموجة ← إلزامي ✅
                wave_high = max(h[wave_low_i:i + 1])
                span = wave_high - wave_low
                if span > 0:
                    levels = [wave_high - fb * span for fb in CFG["dca_fibs"]]
                    # 2026-07-17 (طلب بو محمد): TP1=قمة التصحيح، TP2=1.272
                    sig = dict(i=i, ts=t[i], os_hits=os_hits,
                               wave_low=wave_low, wave_high=wave_high,
                               entry=levels[0], levels=levels, stop=wave_low,
                               tp1=wave_high,                      # الهدف الأول = قمة التصحيح
                               tp2=wave_low + 1.272 * span,        # الهدف الثاني = 1.272
                               vwap=vw[i], rsi=rs[i])
                phase, os_hits = 0, 0                    # جاهز لدورة جديدة
                wave_low = wave_low_i = cross_i = None
    return sig if (sig and sig["i"] == n - 1) else None


def format_message_vwave(signals):
    """بطاقة تيليجرام لاستراتيجية الفيواب الأسبوعي (دخول بانتظار مستويات الفيبو)."""
    nums = ["1️⃣", "2️⃣"]
    now = dt.datetime.now().strftime("%H:%M:%S")
    sep = "\n➖➖➖➖➖➖➖➖➖\n"
    blocks = []
    for s in signals:
        entry, stop = s["entry"], s["stop"]
        legs = s.get("legs") or [entry]
        avg = sum(legs) / len(legs)
        risk_pct = ((entry - stop) / entry * 100) if entry else 0.0
        lines = [
            f"🟢 إشارة {DASH_LABEL} — شراء (انتظار المستويات)",
            f"💎 {s['sym']} · ⏱️ {s.get('tf', CFG['entry_tf'])}",
            f"📊 الشروط: تشبّع Ultimate RSI21≤{CFG['vw_os']:.0f} ({s.get('os_hits', 1)} لمسة) → "
            f"اختراق الفيواب الأسبوعي → RSI21≥{CFG['vw_ob']:.0f} → تحوّل MACD4C للأحمر",
            "",
            "📍 سلالم الدخول DCA (فيبو التصحيح — لا شراء قبل بلوغها):",
        ]
        for fb, p in zip(CFG["dca_fibs"], legs):
            lines.append(f"   • {fb:.3f} ← {_fmt(p)}")
        lines += [
            f"⚖️ المتوسط لو امتلأت السلالم: {_fmt(avg)}",
            f"🛑 الوقف (قاع الموجة التي اخترقت الفيواب): {_fmt(stop)}  (−{risk_pct:.2f}% من المستوى الأول)",
            "",
            "🎯 الأهداف (امتدادات فيبو):",
        ]
        for k, tgt in enumerate([s.get("tp1"), s.get("tp2")]):
            if not tgt:
                continue
            gain = ((tgt - entry) / entry * 100) if entry else 0.0
            lines.append(f"{nums[k]} {_fmt(tgt)}  (+{gain:.2f}% من المستوى الأول)")
        lines += [
            "",
            "⚖️ إدارة 50/50: جني 50% عند الهدف الأول + تعادل + قفل 0.3R",
            f"⏰ {now}",
        ]
        blocks.append("\n".join(lines))
    header = ("📊 <b>إشارات الفيواب الأسبوعي — شراء</b>\n"
              "<i>تشبّع → اختراق الفيواب → نهاية الموجة (MACD4C) → دخول DCA بفيبو</i>")
    footer = "⚠️ تحليل تعليمي — ليس نصيحة مالية"
    return header + sep + sep.join(blocks) + sep + footer


def scan_vwave(basket=None):
    """المسح الحيّ لاستراتيجية الفيواب الأسبوعي — بلا نموذج ML وبلا مناطق عرض/طلب."""
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:60]
    state = load_state(); sent = set(state.get("sent", []))
    signals = []

    def _fetch(s):
        try:
            return s, fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
        except Exception:
            return s, None

    _workers = max(1, int(os.environ.get("SD_SCAN_WORKERS", "8")))
    with ThreadPoolExecutor(max_workers=_workers) as _pool:
        fetched = list(_pool.map(_fetch, basket))
    for s, d1 in fetched:
        try:
            if not d1 or len(d1["c"]) < 300:
                continue
            d1 = {k: vv[:-1] for k, vv in d1.items()}    # استبعاد الشمعة الجارية (غير المغلقة)
            sig = vwave_signal(d1)
            if not sig:
                continue
            key = f"{s}:{sig['ts']}"                     # منع تكرار نفس الإشارة
            if key in sent:
                continue
            if tp1_too_close(sig["levels"][0], sig["tp1"]):   # هدف أول < 1% ← إلغاء الصفقة
                continue
            levels = [round(p, 8) for p in sig["levels"]]
            signals.append(dict(
                key=key, sym=s, tf=CFG["entry_tf"], ts=sig["ts"],
                entry=levels[0], stop=round(sig["stop"], 8),
                legs=levels, dca_levels=levels, wait_entry=True,
                max_age_h=CFG["wait_max_age_h"],
                tp1=round(sig["tp1"], 8), tp2=round(sig["tp2"], 8),
                os_hits=sig["os_hits"]))
        except Exception as ex:
            print("vwave skip", s, ex)
    signals = signals[:CFG["top_n"]]
    if signals:
        mid = send_telegram(format_message_vwave(signals))
        track_for_dashboard(signals, mid)                # نفس لوحة المتتبّع والتنفيذ
        for sig in signals:
            state.setdefault("sent", []).append(sig["key"])
        save_state(state)
    else:
        print("no vwave signals this scan")
    return signals


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

def _sim_exits(entry, stop, tp1, tp2, h, l, c, tch, hold):
    """يحاكي مسار السعر مرّة واحدة ويعيد ناتج R لأربعة أنظمة نقل وقف بعد جني النصف الأول،
    مع خصم الرسوم على كل تعبئة (fee_rate لكل جهة). كلها إدارة 50/50 والفرق فقط في مستوى الوقف:
      be      = الوقف إلى الدخول تماماً (تعادل — الوضع الحالي)
      be_cost = الوقف إلى الدخول + تغطية رسوم الذهاب/الإياب (تعادل حقيقي بعد الرسوم)
      lock    = الوقف إلى الدخول + 0.3R (قفل ربح صغير)
      nobe    = لا يُنقل الوقف إطلاقاً (يبقى الوقف الأصلي، يُترك الباقي للهدف2)
    الناتج بوحدات R صافية بعد الرسوم. (عند تعارض الوقف والهدف بنفس الشمعة نُرجّح الوقف تحفّظاً.)"""
    R = entry - stop
    if R <= 0:
        return None
    r1, r2 = (tp1 - entry) / R, (tp2 - entry) / R
    if r1 <= 0:
        return None
    fee = CFG["fee_rate"]
    end = min(len(c), tch + hold)
    def cst(px):
        return fee * px / R                        # رسوم جهة واحدة لوحدة كاملة بوحدات R
    out = {}
    for name, be_lvl in (("be", entry),
                         ("be_cost", entry + 2 * fee * entry),
                         ("lock", entry + 0.3 * R),
                         ("nobe", None)):
        sl = stop; half = False; realized = 0.0; res = None
        for i in range(tch, end):
            if l[i] <= sl:                          # ضُرب الوقف
                fr = 0.5 if half else 1.0           # الباقي بعد الجني نصف، وإلا كامل
                res = realized + fr * ((sl - entry) / R) - fr * cst(sl)
                if not half:
                    res -= cst(entry)               # رسوم دخول الكامل إن لم يُجنَ شيء بعد
                break
            if not half and h[i] >= tp1:            # جني النصف الأول
                half = True
                realized = 0.5 * r1 - cst(entry) - 0.5 * cst(tp1)   # رسوم دخول الكامل + خروج النصف
                if be_lvl is not None:
                    sl = be_lvl
            if half and h[i] >= tp2:                # الهدف2 للنصف الثاني
                res = realized + 0.5 * r2 - 0.5 * cst(tp2)
                break
        if res is None:                             # إغلاق زمني على آخر شمعة
            X = c[end - 1]; fr = 0.5 if half else 1.0
            res = realized + fr * ((X - entry) / R) - fr * cst(X)
            if not half:
                res -= cst(entry)
        out[name] = res
    return out

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
    newf_rs = []                   # الجديد + فلاتر بنية الاتجاه (قمة أعلى + MACD4C + تشبّع RSI21)
    bk = {"hh": [], "macd": [], "os21": [], "hh_macd": [], "hh_os21": []}  # عزل أثر كل فلتر منفرداً وأزواجه
    new_r1s, new_r2s = [], []      # تشخيص: بُعد الهدف1/الهدف2 عن الدخول بوحدات R (سلامة نسبة الفوز)
    exit_rs = {"be": [], "be_cost": [], "lock": [], "nobe": []}   # مقارنة أنظمة نقل الوقف (صافي بعد الرسوم)
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
                _hh = bool(f["hh"]); _md = f["macd4c"] >= CFG["macd4c_min"]; _os = bool(f["os21"])
                if r is not None:
                    new_rs.append(r)
                    if _hh and _md and _os:                 # يمرّ فلاتر بنية الاتجاه الثلاثة
                        newf_rs.append(r)
                    if _hh: bk["hh"].append(r)              # عزل كل فلتر منفرداً
                    if _md: bk["macd"].append(r)
                    if _os: bk["os21"].append(r)
                    if _hh and _md: bk["hh_macd"].append(r)
                    if _hh and _os: bk["hh_os21"].append(r)
                    _Rr = avg - st["stop"]                  # تشخيص بُعد الأهداف بوحدات R
                    if _Rr > 0:
                        new_r1s.append((st["tp1"] - avg) / _Rr); new_r2s.append((st["tp2"] - avg) / _Rr)
                ex = _sim_exits(avg, st["stop"], st["tp1"], st["tp2"], h, l, c, tch, hold)
                if ex is not None:                          # مقارنة أنظمة نقل الوقف على نفس الصفقات
                    for k in exit_rs:
                        exit_rs[k].append(ex[k])
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
    entry_desc = ((f"زخم RSI{CFG['rsi_entry_len']}≥{CFG['rsi_entry_ob']:.0f}"
                   if CFG["entry_mode"] == "momentum" else "اختراق قمة CHoCH")
                  + (" + توسيط DCA فيبو" if CFG["use_dca"] else ""))
    report = ("📊 مقارنة باك-تست العرض/الطلب (حافة خام بلا ML)\n"
              f"الفريم: دخول {CFG['entry_tf']} / سياق {CFG['htf']} · "
              f"{entry_desc} · وقف قاع الموجة · فلتر اتجاه EMA{CFG['ema_len']} · "
              f"CHoCH · قرب≤{CFG['max_ema_dist']:.0%} · حداثة≤{CFG['max_bars_to_touch']} شمعة\n"
              f"— القديم (5050): {_stats(old_rs)}\n"
              f"— الجديد (5050): {_stats(new_rs)}\n"
              f"— الجديد + بنية الاتجاه (قمة أعلى+MACD4C≥{CFG['macd4c_min']}+تشبّع RSI21≤{CFG['rsi21_os']:.0f}): {_stats(newf_rs)}\n"
              f"   • قمة أعلى وحده: {_stats(bk['hh'])}\n"
              f"   • MACD4C≥{CFG['macd4c_min']} وحده: {_stats(bk['macd'])}\n"
              f"   • تشبّع RSI21≤{CFG['rsi21_os']:.0f} وحده: {_stats(bk['os21'])}\n"
              f"   • قمة أعلى + MACD4C: {_stats(bk['hh_macd'])}\n"
              f"   • قمة أعلى + تشبّع RSI21: {_stats(bk['hh_os21'])}\n"
              f"— الجديد (نظام ب شانديلير {CFG['trail_atr']}×ATR): {_stats(new_b_rs)}\n"
              f"— تشخيص الأهداف: هدف1 متوسط {(sum(new_r1s)/len(new_r1s) if new_r1s else 0):.2f}R · "
              f"هدف2 {(sum(new_r2s)/len(new_r2s) if new_r2s else 0):.2f}R · "
              f"هدف1<1R = {(100*sum(1 for x in new_r1s if x < 1)/len(new_r1s) if new_r1s else 0):.0f}%\n"
              f"\n🔁 مقارنة أنظمة نقل الوقف بعد الهدف1 (صافي بعد رسوم {CFG['fee_rate']*100:.3f}%/جهة):\n"
              f"— تعادل (الحالي): {_stats(exit_rs['be'])}\n"
              f"— تعادل+رسوم: {_stats(exit_rs['be_cost'])}\n"
              f"— قفل ربح +0.3R: {_stats(exit_rs['lock'])}\n"
              f"— بلا نقل وقف: {_stats(exit_rs['nobe'])}")
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
        if CFG["strategy"] in ("vwap_wave", "whale"):
            print(f"{CFG['strategy']} strategy -> no model needed")
        elif not model_is_fresh():
            print("model missing/stale -> training")
            train()
        else:
            print("model fresh -> skip training")
        scan()
