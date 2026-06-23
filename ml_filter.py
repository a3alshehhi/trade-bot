# -*- coding: utf-8 -*-
"""
ml_filter.py — فلتر تعلّم آلي يقرأ سياق كل إشارة وحالة السوق وقتها،
ويتنبّأ باحتمال نجاحها بناءً على نتائج الصفقات السابقة (يتعلّم من الأخطاء).

مبدأ مهم: كل الميزات تُحسب من بيانات *سابقة فقط* (حتى شمعة الإشارة i شاملةً)،
بلا أي نظرة للمستقبل (no lookahead) — وإلا كانت النتائج وهماً.

الوحدة تُستخدم في موضعين:
  • ml_train.py    : يستدعي compute_features لبناء بيانات التدريب.
  • trading_bot.py : يستدعي score_signal لترشيح الإشارات الحيّة.
"""
import os
import numpy as np
import pandas as pd

# rsi/atr من نفس البوت لضمان تطابق الحساب
from trading_bot import rsi, atr

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_model.joblib")

# ترتيب الميزات ثابت — يجب أن يطابق التدريب والاستدلال
FEATURE_NAMES = [
    "rsi", "rsi_chg", "atr_pct", "ret_5", "ret_10", "ret_20",
    "vol_ratio", "dist_ma50", "dist_ma200", "trend_up", "ma50_slope",
    "volatility", "range_pct", "pos_in_range", "consec_up",
    "dow", "hour", "is_crypto",
    # === التحليل الموجي + MACD 4C ===
    "macd_color",        # حالة الهيستوجرام 4 ألوان: +2 صاعد قوي، +1 صاعد واهٍ، -1 هابط واهٍ، -2 هابط قوي
    "macd_hist_norm",    # قيمة الهيستوجرام منسوبة للسعر
    "macd_hist_slope",   # ميل الهيستوجرام (تسارع/تباطؤ الموجة)
    "macd_above_zero",   # MACD فوق الصفر (موجة دفع صاعدة)؟
    "bars_in_color",     # عمر لون الزخم الحالي (طول الموجة)
    "hist_wave_count",   # عدد موجات الهيستوجرام في آخر ~50 شمعة (موقع تقريبي في التسلسل الموجي)
    "bars_since_flip",   # عمر موجة الزخم الحالية منذ آخر عبور للصفر
    "bull_div",          # دايفرجنس صاعد: قاع سعري أدنى لكن قاع هيستوجرام أعلى (نهاية موجة تصحيح)
]


def _macd(close, fast=12, slow=26, signal=9):
    """يحسب MACD وخط الإشارة والهيستوجرام (EMA)."""
    s = pd.Series(close, dtype=float)
    ema_f = s.ewm(span=fast, adjust=False).mean()
    ema_s = s.ewm(span=slow, adjust=False).mean()
    macd = ema_f - ema_s
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd.values, sig.values, hist.values


def _macd_color(hist, j):
    """لون شمعة MACD 4C عند j: +2/+1/-1/-2 (مثل مؤشر MACD 4-color الشهير)."""
    if j < 1 or np.isnan(hist[j]) or np.isnan(hist[j - 1]):
        return 0.0
    rising = hist[j] >= hist[j - 1]
    if hist[j] >= 0:
        return 2.0 if rising else 1.0        # أخضر فاتح (قوي) / أخضر غامق (واهٍ)
    else:
        return -2.0 if not rising else -1.0  # أحمر فاتح (قوي) / أحمر غامق (واهٍ)


def _wave_features(close, low, hist, j):
    """ميزات موجية مشتقّة من MACD 4C + بنية السعر، بيانات سابقة فقط."""
    color = _macd_color(hist, j)
    h = hist[j] if not np.isnan(hist[j]) else 0.0
    hist_norm = float(h / close[j]) if close[j] > 0 else 0.0
    hist_slope = float((hist[j] - hist[j - 1]) / close[j]) if j >= 1 and close[j] > 0 and not np.isnan(hist[j - 1]) else 0.0
    above_zero = 1.0 if h > 0 else 0.0

    # عمر اللون الحالي (نفس إشارة الميل ونفس جهة الصفر)
    bars_in_color = 0
    k = j
    while k >= 1 and _macd_color(hist, k) == color and color != 0.0:
        bars_in_color += 1
        k -= 1

    # عدد عبورات الصفر (موجات الزخم) في آخر 50 شمعة + عمر الموجة الحالية
    lookback = 50
    start = max(1, j - lookback)
    flips = 0
    last_flip = j
    for k in range(start, j + 1):
        if np.isnan(hist[k]) or np.isnan(hist[k - 1]):
            continue
        if (hist[k] >= 0) != (hist[k - 1] >= 0):
            flips += 1
            last_flip = k
    bars_since_flip = float(j - last_flip)

    # دايفرجنس صاعد بسيط: مقارنة آخر قاعين سعريين بقاعي الهيستوجرام
    bull_div = 0.0
    win = 40
    s0 = max(1, j - win)
    seg_low = low[s0:j + 1]
    seg_hist = hist[s0:j + 1]
    if len(seg_low) > 10:
        mid = len(seg_low) // 2
        p1 = np.argmin(seg_low[:mid]); p2 = mid + np.argmin(seg_low[mid:])
        if seg_low[p2] < seg_low[p1] and seg_hist[p2] > seg_hist[p1]:
            bull_div = 1.0

    return {
        "macd_color": color,
        "macd_hist_norm": round(hist_norm, 6),
        "macd_hist_slope": round(hist_slope, 6),
        "macd_above_zero": above_zero,
        "bars_in_color": float(bars_in_color),
        "hist_wave_count": float(flips),
        "bars_since_flip": bars_since_flip,
        "bull_div": bull_div,
    }

_MODEL = None  # كاش للنموذج المحمّل


def compute_features(df, i, kind, side="buy"):
    """يحسب متجه الميزات لإشارة عند الشمعة i باستخدام df[:i+1] فقط.
    يرجع dict (اسم الميزة → قيمة) أو None إن لم تتوفّر بيانات كافية."""
    if df is None or i < 30 or i >= len(df):
        return None
    sub = df.iloc[: i + 1].reset_index(drop=True)
    j = len(sub) - 1
    close = sub["close"].values
    high = sub["high"].values
    low = sub["low"].values
    vol = sub["volume"].values

    r = rsi(sub["close"], 21).values
    a = atr(sub, 14).values
    if np.isnan(r[j]) or np.isnan(a[j]):
        return None

    ma50 = pd.Series(close).rolling(50).mean().values
    ma200 = pd.Series(close).rolling(200).mean().values

    def ret(k):
        return float(close[j] / close[j - k] - 1.0) if j - k >= 0 and close[j - k] > 0 else 0.0

    # نسبة الحجم مقابل متوسط 20
    if j >= 20:
        vbase = float(np.mean(vol[j - 20:j])) or 1.0
        vol_ratio = float(vol[j] / vbase) if vbase else 1.0
    else:
        vol_ratio = 1.0

    dist_ma50 = float(close[j] / ma50[j] - 1.0) if not np.isnan(ma50[j]) and ma50[j] > 0 else 0.0
    dist_ma200 = float(close[j] / ma200[j] - 1.0) if not np.isnan(ma200[j]) and ma200[j] > 0 else 0.0
    trend_up = 1.0 if (not np.isnan(ma200[j]) and close[j] > ma200[j]) else 0.0
    ma50_slope = (float(ma50[j] / ma50[j - 10] - 1.0)
                  if j >= 10 and not np.isnan(ma50[j - 10]) and ma50[j - 10] > 0 else 0.0)

    # التذبذب: انحراف العوائد اليومية آخر 20 شمعة
    if j >= 21:
        rets = close[j - 20:j + 1] / close[j - 21:j] - 1.0
        volatility = float(np.std(rets))
    else:
        volatility = 0.0

    range_pct = float((high[j] - low[j]) / close[j]) if close[j] > 0 else 0.0

    # الموقع داخل مدى آخر 20 شمعة (0=قاع، 1=قمة)
    w_hi = float(np.max(high[max(0, j - 20):j + 1]))
    w_lo = float(np.min(low[max(0, j - 20):j + 1]))
    pos_in_range = float((close[j] - w_lo) / (w_hi - w_lo)) if w_hi > w_lo else 0.5

    # عدد شموع الصعود المتتالية
    consec = 0
    k = j
    while k > 0 and close[k] > close[k - 1]:
        consec += 1
        k -= 1

    # سمات الوقت
    dt = pd.to_datetime(sub["date"].iloc[j])
    dow = float(dt.dayofweek)
    hour = float(dt.hour)

    # === الموجات + MACD 4C ===
    _, _, hist = _macd(close)
    wave = _wave_features(close, low, hist, j)

    feats = {
        "rsi": round(float(r[j]), 2),
        "rsi_chg": round(float(r[j] - r[j - 1]), 2) if not np.isnan(r[j - 1]) else 0.0,
        "atr_pct": round(float(a[j] / close[j]), 5) if close[j] > 0 else 0.0,
        "ret_5": round(ret(5), 5),
        "ret_10": round(ret(10), 5),
        "ret_20": round(ret(20), 5),
        "vol_ratio": round(vol_ratio, 3),
        "dist_ma50": round(dist_ma50, 5),
        "dist_ma200": round(dist_ma200, 5),
        "trend_up": trend_up,
        "ma50_slope": round(ma50_slope, 5),
        "volatility": round(volatility, 5),
        "range_pct": round(range_pct, 5),
        "pos_in_range": round(pos_in_range, 3),
        "consec_up": float(consec),
        "dow": dow,
        "hour": hour,
        "is_crypto": 1.0 if kind == "crypto" else 0.0,
    }
    feats.update(wave)
    return feats


def features_to_vector(feats):
    """يحوّل dict الميزات إلى مصفوفة بالترتيب الثابت."""
    return np.array([[feats[name] for name in FEATURE_NAMES]], dtype=float)


def load_model(path=MODEL_PATH):
    """يحمّل النموذج المدرَّب (مع كاش). يرجع dict أو None إن لم يوجد."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    if not os.path.exists(path):
        return None
    import joblib
    _MODEL = joblib.load(path)
    return _MODEL


def score_signal(df, i, kind, side="buy", path=MODEL_PATH):
    """يرجع احتمال نجاح الإشارة [0..1] أو None إن لم يتوفّر نموذج/ميزات."""
    bundle = load_model(path)
    if bundle is None:
        return None
    feats = compute_features(df, i, kind, side)
    if feats is None:
        return None
    x = features_to_vector(feats)
    try:
        return float(bundle["model"].predict_proba(x)[0, 1])
    except Exception:
        return None


def passes_filter(df, i, kind, side="buy", path=MODEL_PATH):
    """قرار القبول/الرفض. يرجع (مقبول؟, الاحتمال, العتبة).
    إن لم يوجد نموذج → يقبل دائماً (سلوك آمن لا يكسر الفحص)."""
    bundle = load_model(path)
    if bundle is None:
        return True, None, None
    prob = score_signal(df, i, kind, side, path)
    if prob is None:
        return True, None, None
    thr = float(bundle.get("threshold", 0.5))
    return (prob >= thr), prob, thr
