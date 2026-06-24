#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
  بوت البحث عن الصفقات  —  AI Trade Scanner
=====================================================================
بوت يقرأ قائمة المتابعة (watchlist) المُصدّرة من TradingView،
ويبحث تلقائياً في الأسهم الأمريكية والعملات الرقمية،
ويحلّلها باستراتيجية فنية متعددة العوامل،
ثم يعطيك إشارات دخول/خروج مرتّبة مع الأسباب ووقف الخسارة والهدف.

مصادر البيانات (مجانية، بدون مفاتيح):
  - الكريبتو : Binance Public API  (أزواج USDT)
  - الأسهم   : Yahoo Finance عبر مكتبة yfinance

⚠️ تنبيه مهم: هذه أداة تحليل وتعليم فقط، وليست نصيحة مالية.
   البوت لا ينفّذ أي صفقة ولا يتصل بأي حساب وساطة. قرار التداول
   ومسؤوليته عليك وحدك. التداول ينطوي على مخاطر خسارة رأس المال.
=====================================================================
"""

import argparse
import sys
import time
import os
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------
#  الإعدادات الافتراضية  (يمكن تغييرها من سطر الأوامر)
# ----------------------------------------------------------------------
DEFAULTS = {
    "timeframe": "1d",     # الإطار الزمني: 1d / 4h / 1h
    "lookback": 220,       # عدد الشموع المطلوبة للتحليل
    "top": 25,             # كم فرصة نعرض في الملخص
    "min_score": 20,       # أقل درجة لاعتبارها إشارة جديرة بالعرض
    "workers": 8,          # عدد الطلبات المتوازية
    "assets": "all",       # all / crypto / stocks
    "side": "buy",         # buy / sell / both — الافتراضي: صفقات الشراء فقط
    "tp_method": "fib",    # fib أهداف فيبوناتشي | atr أهداف ATR
}

# نجرّب أكثر من نقطة وصول: العامة (data-api) لا تُحجب من خوادم أمريكا/السحابة
BINANCE_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]
# تعيين الإطار الزمني لصيغ Binance و yfinance
BINANCE_INTERVAL = {"1d": "1d", "4h": "4h", "1h": "1h", "15m": "15m"}
YF_INTERVAL = {"1d": "1d", "4h": "1h", "1h": "1h"}  # yfinance لا يدعم 4h مباشرة
YF_PERIOD = {"1d": "1y", "4h": "60d", "1h": "60d"}

CRYPTO_EXCHANGES = {"BINANCE", "BYBIT", "MEXC", "BINANCEUS", "KUCOIN", "OKX", "GATEIO"}
US_STOCK_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "OTC", "BATS", "CBOE"}
# رموز كلية/مؤشرات تُستخدم كسياق فقط (لا إشارات تداول مباشرة)
MACRO_EXCHANGES = {"CRYPTOCAP", "CAPITALCOM", "TVC", "SP", "OANDA", "FX", "FOREXCOM"}
# أسواق لا تتوفر لها بيانات مجانية موثوقة (تُتجاهل مع تنبيه)
UNSUPPORTED_EXCHANGES = {"ADX", "DFM", "TADAWUL", "EGX", "QSE"}


# ======================================================================
#  1) قراءة وتحليل ملف الـ watchlist
# ======================================================================
def parse_watchlist(path):
    """يقرأ ملف TradingView (EXCHANGE:SYMBOL مفصولة بفواصل) ويصنّفه."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # قد يبدأ السطر برقم (تصدير TradingView) — ننظّفه
    tokens = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # إزالة أي ترقيم بادئ مثل "1\t"
        if "\t" in line:
            line = line.split("\t", 1)[1]
        tokens.extend([t.strip() for t in line.split(",") if t.strip()])

    crypto, stocks, macro, skipped = [], [], [], []
    for tok in tokens:
        if tok.startswith("#"):            # فاصل قسم مثل ###STOCKS
            continue
        if ":" not in tok:
            skipped.append(tok)
            continue
        exch, sym = tok.split(":", 1)
        exch = exch.upper().strip()
        sym = sym.strip().upper()

        if exch in CRYPTO_EXCHANGES and sym.endswith(("USDT", "USD", "BTC", "ETH")):
            # نتعامل فقط مع أزواج USDT للتداول (الأكثر سيولة وتوفراً)
            if sym.endswith("USDT"):
                crypto.append({"raw": tok, "symbol": sym, "exch": exch})
            else:
                skipped.append(tok)
        elif exch in US_STOCK_EXCHANGES:
            stocks.append({"raw": tok, "symbol": sym, "exch": exch})
        elif exch in MACRO_EXCHANGES:
            macro.append(tok)
        elif exch in UNSUPPORTED_EXCHANGES:
            skipped.append(tok)
        else:
            skipped.append(tok)

    return {"crypto": crypto, "stocks": stocks, "macro": macro, "skipped": skipped}


# ======================================================================
#  2) جلب البيانات
# ======================================================================
def fetch_binance(symbol, interval, limit):
    """يجلب شموع OHLCV من Binance العامة. يجرّب أكثر من نقطة وصول."""
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    for base in BINANCE_BASES:
        try:
            r = requests.get(f"{base}/api/v3/klines", params=params, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data:
                continue
            df = pd.DataFrame(data, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "tbav", "tqav", "ignore"])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["date"] = pd.to_datetime(df["close_time"], unit="ms")
            return df[["date", "open", "high", "low", "close", "volume"]].dropna()
        except Exception:
            continue
    return None


def fetch_stock(symbol, interval, period):
    """يجلب بيانات سهم عبر yfinance. يرجع DataFrame أو None."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        df = yf.download(symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty:
            return None
        df = df.reset_index()
        # توحيد الأعمدة (yfinance قد يرجع MultiIndex)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        rename = {"Date": "date", "Datetime": "date", "Open": "open",
                  "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
        df = df.rename(columns=rename)
        keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep].dropna()
    except Exception:
        return None


# ======================================================================
#  3) المؤشرات الفنية  (بدون مكتبات خارجية)
# ======================================================================
def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _pivot_lows(arr, left=2, right=2):
    """مؤشرات القيعان المحلية (نقاط ارتداد للأسفل)."""
    out = []
    for i in range(left, len(arr) - right):
        seg = arr[i - left:i + right + 1]
        if not np.isnan(arr[i]) and arr[i] == np.nanmin(seg):
            out.append(i)
    return out


def _pivot_highs(arr, left=2, right=2):
    """مؤشرات القمم المحلية."""
    out = []
    for i in range(left, len(arr) - right):
        seg = arr[i - left:i + right + 1]
        if not np.isnan(arr[i]) and arr[i] == np.nanmax(seg):
            out.append(i)
    return out


def detect_divergence(price_low, price_high, ind, lookback=90):
    """يكشف الدايفرجنس بمقارنة آخر قاعين/قمتين بين السعر والمؤشر.
    يرجع 'bull' (إيجابي) أو 'bear' (سلبي) أو None."""
    pl, ph, iv = price_low[-lookback:], price_high[-lookback:], ind[-lookback:]
    # إيجابي: السعر قاع أدنى + المؤشر قاع أعلى
    lows = _pivot_lows(pl)
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if pl[b] < pl[a] and iv[b] > iv[a]:
            return "bull"
    # سلبي: السعر قمة أعلى + المؤشر قمة أدنى
    highs = _pivot_highs(ph)
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if ph[b] > ph[a] and iv[b] < iv[a]:
            return "bear"
    return None


# ======================================================================
#  4) الاستراتيجية متعددة العوامل  ->  درجة وإشارة
# ======================================================================
def analyze(df, tp_method="fib"):
    """تُرجع dict بالتحليل أو None إذا البيانات غير كافية.
    tp_method: 'fib' أهداف امتداد فيبوناتشي | 'atr' أهداف بمضاعفات ATR."""
    if df is None or len(df) < 60:
        return None
    df = df.copy().reset_index(drop=True)
    close = df["close"]

    df["ema20"] = ema(close, 20)
    df["ema50"] = ema(close, 50)
    df["ema200"] = ema(close, 200) if len(df) >= 200 else ema(close, 100)
    df["rsi"] = rsi(close, 14)
    df["rsi21"] = rsi(close, 21)
    macd_line, sig_line, hist = macd(close)
    df["macd_hist"] = hist
    df["macd_line"] = macd_line
    df["atr"] = atr(df, 14)
    df["vol_sma"] = df["volume"].rolling(20).mean()

    # كشف الدايفرجنس على RSI(21) و MACD
    low_arr = df["low"].values
    high_arr = df["high"].values
    div_rsi = detect_divergence(low_arr, high_arr, df["rsi21"].values)
    div_macd = detect_divergence(low_arr, high_arr, df["macd_line"].values)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["close"])
    if price <= 0 or np.isnan(price):
        return None

    atr_now = float(last["atr"]) if not np.isnan(last["atr"]) else price * 0.02
    if atr_now <= 0:
        atr_now = price * 0.02

    score = 0
    reasons = []

    # --- الاتجاه ---
    if price > last["ema50"]:
        score += 15; reasons.append("السعر فوق EMA50 (اتجاه صاعد)")
    else:
        score -= 15; reasons.append("السعر تحت EMA50 (اتجاه هابط)")

    if last["ema20"] > last["ema50"]:
        score += 15; reasons.append("EMA20 فوق EMA50 (تقاطع إيجابي)")
    else:
        score -= 15; reasons.append("EMA20 تحت EMA50 (تقاطع سلبي)")

    if price > last["ema200"]:
        score += 10; reasons.append("فوق المتوسط طويل المدى")
    else:
        score -= 10; reasons.append("تحت المتوسط طويل المدى")

    # --- الزخم: MACD ---
    if last["macd_hist"] > 0 and last["macd_hist"] > prev["macd_hist"]:
        score += 15; reasons.append("MACD إيجابي ومتصاعد")
    elif last["macd_hist"] < 0 and last["macd_hist"] < prev["macd_hist"]:
        score -= 15; reasons.append("MACD سلبي ومتراجع")
    elif last["macd_hist"] > 0:
        score += 7
    else:
        score -= 7

    # --- RSI ---
    rsi_val = float(last["rsi"]) if not np.isnan(last["rsi"]) else 50
    if 50 <= rsi_val <= 65:
        score += 10; reasons.append(f"RSI صحي ({rsi_val:.0f})")
    elif 65 < rsi_val <= 75:
        score += 4; reasons.append(f"RSI قوي ({rsi_val:.0f})")
    elif rsi_val > 75:
        score -= 10; reasons.append(f"RSI تشبّع شرائي ({rsi_val:.0f}) — حذر")
    elif 30 <= rsi_val < 50:
        score -= 6; reasons.append(f"RSI ضعيف ({rsi_val:.0f})")
    elif rsi_val < 30:
        score += 8; reasons.append(f"RSI تشبّع بيعي ({rsi_val:.0f}) — ارتداد محتمل")

    # --- الحجم ---
    vol, vsma = float(last["volume"]), float(last["vol_sma"]) if not np.isnan(last["vol_sma"]) else 0
    if vsma > 0 and vol > 1.5 * vsma:
        if price >= prev["close"]:
            score += 10; reasons.append("حجم تداول مرتفع مع صعود")
        else:
            score -= 5; reasons.append("حجم مرتفع مع هبوط")

    # --- الاختراق (آخر 20 شمعة) — مكافأة مخفّضة لتفادي مطاردة القمة ---
    hi20 = df["high"].iloc[-21:-1].max()
    lo20 = df["low"].iloc[-21:-1].min()
    if price >= hi20 * 0.99:
        score += 5; reasons.append("قرب قمة 20 شمعة")
    elif price <= lo20 * 1.01:
        score -= 5; reasons.append("قرب قاع 20 شمعة")

    # --- جودة نقطة الدخول: نفضّل الارتداد للمتوسط ونعاقب مطاردة الحركة ---
    # ext = بُعد السعر عن EMA20 بوحدات ATR، باتجاه الصفقة المرجّح
    ema20_v = float(last["ema20"])
    up_bias = price > float(last["ema50"])
    ext = (price - ema20_v) / atr_now if up_bias else (ema20_v - price) / atr_now
    if -0.5 <= ext <= 1.0:
        score += 12; reasons.append("دخول قرب المتوسط (ارتداد صحي ✅)")
    elif ext > 2.5:
        score -= 18; reasons.append("السعر ممتد بعيداً عن المتوسط (مطاردة — دخول رديء)")
    elif ext > 1.5:
        score -= 8; reasons.append("السعر ممتد قليلاً عن المتوسط")

    # --- الدايفرجنس (الانحراف) ---
    div_inds = []
    if div_rsi == "bull":
        div_inds.append("RSI21")
    if div_macd == "bull":
        div_inds.append("MACD")
    divergence = None
    if div_inds:
        score += 12
        divergence = "bull"
        reasons.append("دايفرجنس إيجابي على " + " و".join(div_inds))
    elif div_rsi == "bear" or div_macd == "bear":
        bear_inds = [n for n, d in (("RSI21", div_rsi), ("MACD", div_macd)) if d == "bear"]
        score -= 12
        divergence = "bear"
        div_inds = bear_inds
        reasons.append("دايفرجنس سلبي على " + " و".join(bear_inds))

    # --- التصنيف ---
    if score >= 40:
        signal = "شراء قوي"
    elif score >= 20:
        signal = "شراء"
    elif score <= -40:
        signal = "بيع قوي"
    elif score <= -20:
        signal = "بيع"
    else:
        signal = "حيادي"

    atr_val = atr_now
    direction = 1 if score > 0 else -1

    # --- وقف هيكلي: تحت آخر قاع حقيقي (شراء) أو فوق آخر قمة (بيع) + هامش ATR ---
    buf = 0.5 * atr_val
    swing_win = 40
    low_seg = df["low"].values[-swing_win:]
    high_seg = df["high"].values[-swing_win:]
    lows_idx = _pivot_lows(low_seg)
    highs_idx = _pivot_highs(high_seg)
    struct_stop = None
    if direction == 1 and lows_idx:
        swing_low = float(low_seg[lows_idx[-1]])
        if swing_low < price:
            struct_stop = swing_low - buf
    elif direction == -1 and highs_idx:
        swing_high = float(high_seg[highs_idx[-1]])
        if swing_high > price:
            struct_stop = swing_high + buf

    # احتياطي على التذبذب إن لم يوجد قاع/قمة هيكلية صالحة
    stop = struct_stop if struct_stop is not None else price - direction * 1.5 * atr_val

    # حدّ أقصى للمخاطرة: لا نسمح بوقف أبعد من 3.5×ATR (تفادي خسارة ضخمة)
    max_risk = 3.5 * atr_val
    if abs(price - stop) > max_risk:
        stop = price - direction * max_risk
    # حدّ أدنى للمخاطرة: لا نضع وقفاً أضيق من 1.0×ATR (تفادي الضرب بالضجيج)
    min_risk = 1.0 * atr_val
    if abs(price - stop) < min_risk:
        stop = price - direction * min_risk

    risk = abs(price - stop)

    # --- حساب الأهداف ---
    targets = []
    fib_ratios = (0.618, 1.0, 1.618)   # نسب امتداد فيبوناتشي
    use_fib = False
    if tp_method == "fib":
        # نطاق آخر حركة سعرية (swing) خلال آخر 50 شمعة
        win = df.iloc[-50:] if len(df) >= 50 else df
        swing_high = float(win["high"].max())
        swing_low = float(win["low"].min())
        rng = swing_high - swing_low
        # نتأكد أن النطاق منطقي (أكبر من المخاطرة) لتفادي أهداف ضيقة
        if rng > risk * 0.5:
            use_fib = True
            for ratio in fib_ratios:
                tp = price + direction * rng * ratio
                pct = (tp - price) / price * 100 * direction
                targets.append({"price": round(tp, 8), "pct": round(pct, 2),
                                "label": f"فيبو {ratio}"})

    if not use_fib:   # طريقة ATR (احتياطية أو عند اختيارها)
        for r_mult in (1, 2, 3):
            tp = price + direction * r_mult * risk
            pct = (tp - price) / price * 100 * direction
            targets.append({"price": round(tp, 8), "pct": round(pct, 2),
                            "label": f"{r_mult}R"})

    tp_used = "fib" if use_fib else "atr"
    target = targets[1]["price"]  # للتوافق (الهدف الأوسط)
    rr = abs(target - price) / abs(price - stop) if (price - stop) != 0 else 0

    # --- سلّم الدخول DCA (4 مستويات) ---
    dca_ratios = (0.382, 0.5, 0.618, 0.786)
    win2 = df.iloc[-50:] if len(df) >= 50 else df
    sh, sl = float(win2["high"].max()), float(win2["low"].min())
    rng2 = sh - sl
    if rng2 > risk * 0.5:                       # ارتدادات فيبوناتشي
        dca_levels = [round((sh - rng2 * r) if direction == 1 else (sl + rng2 * r), 8)
                      for r in dca_ratios]
        dca_method = "fib"
    else:                                       # احتياطي: نسب مئوية ثابتة
        steps = (0.0, 0.03, 0.06, 0.09)
        dca_levels = [round(price * (1 - s) if direction == 1 else price * (1 + s), 8)
                      for s in steps]
        dca_method = "pct"
    dca_avg = round(sum(dca_levels) / len(dca_levels), 8)
    dca_stop = round((min(dca_levels) - 0.5 * risk) if direction == 1
                     else (max(dca_levels) + 0.5 * risk), 8)
    dca = {"levels": dca_levels, "avg": dca_avg, "stop": dca_stop, "method": dca_method}
    # نسبة كل هدف من متوسط دخول DCA
    for t in targets:
        t["pct_avg"] = round((t["price"] - dca_avg) / dca_avg * 100 * direction, 2)

    # قوة الحجم: الترتيب المئوي لحجم آخر شمعة ضمن آخر 20 شمعة (0-100%)
    vol_window = df["volume"].iloc[-20:]
    vol_strength = round(float((vol_window < float(last["volume"])).mean()) * 100, 1) \
        if len(vol_window) else 50.0
    # قوة الاتجاه: تحويل الدرجة إلى نسبة 0-99% (أقصى درجة ممكنة ≈ 85)
    trend_strength = int(min(99, round(abs(score) / 85.0 * 100)))

    return {
        "price": price,
        "score": int(score),
        "signal": signal,
        "rsi": round(rsi_val, 1),
        "vol_strength": vol_strength,
        "trend_strength": trend_strength,
        "divergence": divergence,
        "div_inds": div_inds,
        "stop": round(stop, 6),
        "target": round(target, 6),
        "targets": targets,
        "tp_method": tp_used,
        "dca": dca,
        "rr": round(rr, 2),
        "atr": round(atr_val, 6),
        "reasons": reasons,
    }


# ======================================================================
#  5) فحص رمز واحد
# ======================================================================
def scan_symbol(item, kind, cfg):
    sym = item["symbol"]
    if kind == "crypto":
        df = fetch_binance(sym, BINANCE_INTERVAL[cfg["timeframe"]], cfg["lookback"])
    else:
        df = fetch_stock(sym, YF_INTERVAL[cfg["timeframe"]], YF_PERIOD[cfg["timeframe"]])
    res = analyze(df, tp_method=cfg.get("tp_method", "fib"))
    if res is None:
        return None
    res.update({"symbol": sym, "kind": kind, "raw": item["raw"]})
    return res


# الفريمات المستخدمة لتأكيد الصفقة (الكريبتو يدعم 4h، الأسهم لا)
CONFIRM_TFS_CRYPTO = ["1d", "4h", "1h"]
CONFIRM_TFS_STOCK = ["1d", "1h"]


def multi_tf_check(symbol, kind):
    """يحلّل الرمز على عدة فريمات، يرجع قائمة (الفريم، الدرجة)."""
    tfs = CONFIRM_TFS_CRYPTO if kind == "crypto" else CONFIRM_TFS_STOCK
    out = []
    for tf in tfs:
        if kind == "crypto":
            df = fetch_binance(symbol, BINANCE_INTERVAL[tf], DEFAULTS["lookback"])
        else:
            df = fetch_stock(symbol, YF_INTERVAL[tf], YF_PERIOD[tf])
        res = analyze(df)
        if res is not None:
            out.append((tf, res["score"]))
    return out


def market_regime(kind):
    """يحدّد اتجاه السوق العام: BTC للكريبتو، SPY للأسهم (إطار يومي).
    يرجع dict: {bullish: bool, label: str, detail: str} أو None عند الفشل."""
    if kind == "crypto":
        ref, name = "BTCUSDT", "BTC"
        df = fetch_binance(ref, "1d", 220)
    else:
        ref, name = "SPY", "S&P 500"
        df = fetch_stock(ref, "1d", "1y")
    if df is None or len(df) < 60:
        return None
    close = df["close"]
    ema50 = ema(close, 50).iloc[-1]
    ema200 = (ema(close, 200) if len(df) >= 200 else ema(close, 100)).iloc[-1]
    price = float(close.iloc[-1])
    # صاعد: السعر فوق EMA50 و EMA50 فوق/قرب EMA200
    bullish = price > ema50 and ema50 >= ema200 * 0.99
    if price > ema50 and ema50 > ema200:
        label, detail = "صاعد", f"{name} فوق المتوسطات"
    elif price > ema50:
        label, detail = "صاعد (مبكّر)", f"{name} فوق EMA50"
    elif price < ema50 and ema50 < ema200:
        label, detail = "هابط", f"{name} تحت المتوسطات"
    else:
        label, detail = "متذبذب", f"{name} بين المتوسطات"
    return {"bullish": bool(bullish), "label": label, "detail": detail, "ref": name}


# فترة المتوسط السنوي: 365 شمعة للكريبتو (سنة كاملة)، 252 للأسهم (سنة تداول ≈365 يوماً)
YEARLY_PERIOD = {"crypto": 365, "stock": 252}


def yearly_ma_status(symbol, kind):
    """حالة السعر مقابل متوسط ~سنة (من بيانات يومية).
    يرجع dict: {ma, price, above, crossed_up, date} أو None."""
    if kind == "crypto":
        df = fetch_binance(symbol, "1d", 500)
    else:
        df = fetch_stock(symbol, "1d", "2y")
    if df is None or len(df) < 60:
        return None
    period = YEARLY_PERIOD.get(kind, 365)
    if len(df) < period:           # بيانات أقل من سنة: نستخدم المتاح
        period = max(60, len(df) - 1)
    ma = df["close"].rolling(period).mean()
    if np.isnan(ma.iloc[-1]) or np.isnan(ma.iloc[-2]):
        return None
    last_c, prev_c = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
    last_ma, prev_ma = float(ma.iloc[-1]), float(ma.iloc[-2])
    try:
        date = str(pd.to_datetime(df["date"].iloc[-1]).date())
    except Exception:
        date = datetime.now().strftime("%Y-%m-%d")
    return {
        "ma": last_ma, "price": last_c, "above": last_c > last_ma,
        "crossed_up": (prev_c <= prev_ma) and (last_c > last_ma),
        "period": period, "date": date,
    }


# عتبات «الصفقة عالية القناعة ⭐»
CONVICTION_MIN_SCORE = 40


def evaluate_conviction(r):
    """يحدّد إن كانت الصفقة عالية القناعة: درجة قوية + توافق فريمين + (دايفرجنس
    إيجابي أو زخم MACD صاعد). يضيف الحقول للنتيجة."""
    is_buy = r["score"] > 0
    strong = abs(r["score"]) >= CONVICTION_MIN_SCORE

    mtf = r.get("mtf") or []
    aligned = sum(1 for _, sc in mtf if (sc > 0) == is_buy and abs(sc) >= 20)
    mtf_ok = aligned >= 2

    div_ok = (r.get("divergence") == "bull") if is_buy else (r.get("divergence") == "bear")
    momentum_ok = any("MACD" in x and ("متصاعد" in x or "إيجابي" in x) for x in r.get("reasons", []))
    confirm_ok = div_ok or momentum_ok

    # توافق مع اتجاه السوق العام
    mk = r.get("market")
    market_ok = True if mk is None else (mk["bullish"] if is_buy else not mk["bullish"])

    r["high_conviction"] = bool(strong and mtf_ok and confirm_ok and market_ok)
    r["conviction_parts"] = {
        "strong": strong, "mtf_aligned": aligned,
        "divergence": div_ok, "momentum": momentum_ok, "market_ok": market_ok,
    }
    return r["high_conviction"]


# ======================================================================
#  5.7) متابعة الصفقات المفتوحة (السعر اللحظي + حالة الأهداف/الوقف)
# ======================================================================
TRADES_FILE = "open_trades.json"


def get_price(symbol, kind):
    """السعر اللحظي الحالي للرمز. يرجع float أو None."""
    if kind == "crypto":
        for base in BINANCE_BASES:
            try:
                r = requests.get(f"{base}/api/v3/ticker/price",
                                 params={"symbol": symbol}, timeout=10)
                if r.status_code == 200:
                    return float(r.json()["price"])
            except Exception:
                continue
        return None
    else:
        try:
            import yfinance as yf
            fi = yf.Ticker(symbol).fast_info
            p = fi.get("last_price") or fi.get("lastPrice")
            if p:
                return float(p)
        except Exception:
            pass
        df = fetch_stock(symbol, "1d", "5d")
        if df is not None and len(df):
            return float(df["close"].iloc[-1])
        return None


def load_trades(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_trades(trades, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


# ── الصفقات الورقية: تسجيل الإشارات المعلّقة لزر تيليجرام ──────────────────
PENDING_FILE = "pending_signals.json"


def register_pending_signal(sig, label, cfg, path=PENDING_FILE):
    """يخزّن إشارة قابلة للفتح كصفقة ورقية، ويرجع مُعرّفاً قصيراً (≤ حد callback_data).
    يُنظّف الإشارات الأقدم من 72 ساعة."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            pend = json.load(f)
    except Exception:
        pend = {}
    if not isinstance(pend, dict):
        pend = {}

    # تنظيف القديم (> 72 ساعة)
    cutoff = (datetime.now() - timedelta(hours=72)).isoformat()
    pend = {k: v for k, v in pend.items()
            if isinstance(v, dict) and v.get("created", "") >= cutoff}

    # مُعرّف قصير وفريد: الوقت بالثواني + لاحقة من الرمز
    pid = f"{int(time.time())}{abs(hash(sig['symbol'])) % 1000:03d}"
    pend[pid] = {
        "symbol": sig["symbol"],
        "label": label,
        "timeframe": cfg.get("timeframe"),
        "strategy": "dca" if cfg.get("dca_fib") else "classic",
        "entry": sig["entry"],
        "stop": sig["stop"],
        "targets": sig["targets"],
        "dca": sig.get("dca"),
        "bar_ts": sig.get("bar_ts"),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pend, f, ensure_ascii=False, indent=2)
    return pid


def make_trade(r, cfg):
    """يبني سجل صفقة من نتيجة إشارة."""
    return {
        "id": f"{r['symbol']}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "symbol": r["symbol"], "kind": r["kind"],
        "side": "buy" if r["score"] > 0 else "sell",
        "timeframe": cfg["timeframe"],
        "entry": r["price"], "stop": r["stop"],
        "targets": [t["price"] for t in (r.get("targets") or [])],
        "targets_pct": [t["pct"] for t in (r.get("targets") or [])],
        "opened_at": datetime.now().isoformat(timespec="seconds"),
        "status": "open", "hit": [],
    }


def _fmt_price(v):
    return f"{v:.8f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"


def format_update_card(tr, event, price):
    """بطاقة تحديث: تحقق هدف / ضرب وقف."""
    is_buy = tr["side"] == "buy"
    pnl = (price - tr["entry"]) / tr["entry"] * 100 * (1 if is_buy else -1)
    head = {"tp1": "🎯 تحقق الهدف الأول ✅",
            "tp2": "🎯 تحقق الهدف الثاني ✅✅",
            "tp3": "🏆 تحقق الهدف الثالث ✅✅✅",
            "sl":  "🛑 ضرب وقف الخسارة"}[event]

    lines = [SEP, head, SEP, "",
             f"💰 العملة: {tr['symbol']}",
             f"⏱️ فريم الدخول: {tr['timeframe']}",
             f"🟢 سعر الدخول: {_fmt_price(tr['entry'])}",
             f"💵 السعر الحالي: {_fmt_price(price)}"]
    if event == "sl":
        lines.append(f"📉 النتيجة: {pnl:+.2f}%")
    else:
        lines.append(f"📈 الربح: {pnl:+.2f}%")
        n = int(event[-1])
        if n < len(tr["targets"]):
            lines.append(f"➡️ الهدف التالي: {_fmt_price(tr['targets'][n])}")
        else:
            lines.append("✅ اكتملت جميع الأهداف — تهانينا!")
    lines += [SEP, "", f"⏰ {datetime.now().strftime('%H:%M:%S')}",
              SEP, "", "💡 إدارة المخاطر سر النجاح"]
    return "\n".join(lines)


def monitor(cfg, state_path):
    """يفحص الصفقات المفتوحة ويرسل تحديثات الأهداف/الوقف."""
    token = cfg.get("tg_token") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = cfg.get("tg_chat") or os.environ.get("TELEGRAM_CHAT_ID")
    trades = load_trades(state_path)
    if not trades:
        print("لا توجد صفقات مفتوحة للمتابعة.")
        return

    open_trades = [t for t in trades if t["status"] == "open"]
    print(f"متابعة {len(open_trades)} صفقة مفتوحة ...")
    changed = False

    for tr in open_trades:
        price = get_price(tr["symbol"], tr["kind"])
        if price is None:
            continue
        is_buy = tr["side"] == "buy"

        # تحقق الأهداف بالترتيب
        for i, tp in enumerate(tr["targets"]):
            key = f"tp{i+1}"
            if key in tr["hit"]:
                continue
            reached = price >= tp if is_buy else price <= tp
            if reached:
                tr["hit"].append(key)
                changed = True
                print(f"  {tr['symbol']}: {key} تحقق عند {price}")
                if token and chat_id:
                    send_telegram(token, chat_id, format_update_card(tr, key, price))
                    time.sleep(0.6)
                if i == len(tr["targets"]) - 1:
                    tr["status"] = "closed_tp"
                    tr["closed_at"] = datetime.now().isoformat(timespec="seconds")

        # وقف الخسارة (إن لم تُغلق بالأهداف)
        if tr["status"] == "open":
            hit_sl = price <= tr["stop"] if is_buy else price >= tr["stop"]
            if hit_sl:
                tr["status"] = "closed_sl"
                tr["closed_at"] = datetime.now().isoformat(timespec="seconds")
                changed = True
                print(f"  {tr['symbol']}: ضرب وقف الخسارة عند {price}")
                if token and chat_id:
                    send_telegram(token, chat_id, format_update_card(tr, "sl", price))
                    time.sleep(0.6)

    if changed:
        save_trades(trades, state_path)
        print("✅ حُدّثت حالة الصفقات.")
    else:
        print("لا تغييرات — لم يتحقق هدف أو وقف جديد.")


# ======================================================================
#  5.8) تنبيه اختراق المتوسط السنوي (365 يوم)
# ======================================================================
YEARLY_STATE_FILE = "yearly_cross.json"


YEARLY_CROSS_TFS = {"crypto": ["1d", "4h", "1h"], "stock": ["1d"]}


def yearly_cross_multitf(symbol, kind):
    """يحسب المتوسط السنوي (من يومي) ثم يفحص عبوره على عدة فريمات.
    يرجع: {ma, period, price, tfs:{tf:{above,crossed_up,ts}}, crossed_tfs:[...]}"""
    # 1) المستوى السنوي + بيانات اليومي
    if kind == "crypto":
        ddf = fetch_binance(symbol, "1d", 500)
    else:
        ddf = fetch_stock(symbol, "1d", "2y")
    if ddf is None or len(ddf) < 60:
        return None
    period = YEARLY_PERIOD.get(kind, 365)
    if len(ddf) < period:
        period = max(60, len(ddf) - 1)
    ma = ddf["close"].rolling(period).mean()
    level = ma.iloc[-1]
    if np.isnan(level):
        return None
    level = float(level)

    def _cross(closes):
        if len(closes) < 2:
            return None
        prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
        return {"above": last > level,
                "crossed_up": prev <= level and last > level, "price": last}

    tfs_out = {}
    last_price = float(ddf["close"].iloc[-1])
    for tf in YEARLY_CROSS_TFS.get(kind, ["1d"]):
        if tf == "1d":
            cdf = ddf
        elif kind == "crypto":
            cdf = fetch_binance(symbol, tf, 5)
        else:
            continue
        if cdf is None or len(cdf) < 2:
            continue
        info = _cross(cdf["close"])
        if info is None:
            continue
        try:
            info["ts"] = str(pd.to_datetime(cdf["date"].iloc[-1]))
        except Exception:
            info["ts"] = datetime.now().isoformat()
        tfs_out[tf] = info
        if tf == "1h" or (tf == "4h" and "1h" not in YEARLY_CROSS_TFS.get(kind, [])):
            last_price = info["price"]

    return {"ma": level, "period": period, "price": last_price,
            "tfs": tfs_out, "crossed_tfs": [t for t, v in tfs_out.items() if v["crossed_up"]]}


def format_yearly_card(symbol, info, new_tfs):
    status = " | ".join(
        f"{tf} {'فوق ✅' if info['tfs'][tf]['above'] else 'تحت ⚠️'}"
        for tf in info["tfs"]
    )
    return "\n".join([
        SEP, "📅 اختراق المتوسط السنوي 🚀", SEP, "",
        f"💰 العملة: {symbol}",
        f"💵 السعر: {_fmt_price(info['price'])}",
        f"📊 متوسط {info['period']} يوم: {_fmt_price(info['ma'])}",
        SEP, "",
        f"🔭 اخترق على الفريمات: {'، '.join(new_tfs)}",
        f"📊 الوضع الحالي: {status}",
        "", "تجاوز السعر متوسطه السنوي — إشارة قوة طويلة المدى.",
        SEP, "", f"⏰ {datetime.now().strftime('%H:%M:%S')}",
        SEP, "", "💡 إدارة المخاطر سر النجاح",
        "⚠️ تحليل تعليمي — ليس نصيحة مالية",
    ])


def scan_yearly_crosses(cfg, watchlist_path, state_path=YEARLY_STATE_FILE):
    """يفحص كل القائمة، ويُنبّه عند اختراق المتوسط السنوي على أي فريم (مع منع التكرار)."""
    token = cfg.get("tg_token") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = cfg.get("tg_chat") or os.environ.get("TELEGRAM_CHAT_ID")
    parsed = parse_watchlist(watchlist_path)
    targets = []
    if cfg["assets"] in ("all", "stocks"):
        targets += [(it, "stock") for it in parsed["stocks"]]
    if cfg["assets"] in ("all", "crypto"):
        targets += [(it, "crypto") for it in parsed["crypto"]]

    alerted = load_trades(state_path) if os.path.exists(state_path) else {}
    if not isinstance(alerted, dict):
        alerted = {}

    print(f"فحص اختراق المتوسط السنوي (متعدد الفريمات) لـ {len(targets)} رمز ...")
    found = 0

    def work(item, kind):
        return item["symbol"], yearly_cross_multitf(item["symbol"], kind)

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = [ex.submit(work, it, kind) for it, kind in targets]
        for fut in as_completed(futs):
            try:
                sym, info = fut.result()
            except Exception:
                continue
            if not info or not info["crossed_tfs"]:
                continue
            # منع التكرار لكل فريم على حدة (حسب توقيت الشمعة)
            prev = alerted.get(sym, {})
            if not isinstance(prev, dict):
                prev = {}
            new_tfs = []
            for tf in info["crossed_tfs"]:
                ts = info["tfs"][tf]["ts"]
                if prev.get(tf) != ts:
                    new_tfs.append(tf)
                    prev[tf] = ts
            if not new_tfs:
                continue
            alerted[sym] = prev
            found += 1
            print(f"  🚀 {sym} اخترق المتوسط على: {', '.join(new_tfs)}")
            if token and chat_id:
                send_telegram(token, chat_id, format_yearly_card(sym, info, new_tfs))
                time.sleep(0.6)

    save_trades(alerted, state_path)
    print(f"اكتمل — تنبيهات جديدة: {found}.")


# ======================================================================
#  5.5) إرسال النتائج إلى تيليجرام
# ======================================================================
def send_telegram(token, chat_id, text, reply_markup=None, reply_to=None):
    """يرسل رسالة إلى تيليجرام. يقسّم الرسائل الطويلة (حد 4096 حرفاً).
    reply_markup: لوحة أزرار inline (dict) تُرفق بآخر مقطع فقط.
    reply_to: message_id لجعل الرسالة رداً على رسالة سابقة.
    يرجع message_id لأول مقطع عند النجاح، أو None عند الفشل."""
    if not token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    msg_id = None
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [text]
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk,
                   "disable_web_page_preview": True}
        # الأزرار تُرفق بآخر مقطع فقط
        if reply_markup is not None and idx == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        # الرد يُربط بأول مقطع فقط
        if reply_to is not None and idx == 0:
            payload["reply_to_message_id"] = reply_to
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ تيليجرام: {r.status_code} {r.text[:200]}")
            elif idx == 0:
                try:
                    msg_id = r.json()["result"]["message_id"]
                except Exception:
                    msg_id = None
        except Exception as e:
            print(f"⚠️ تعذّر الإرسال إلى تيليجرام: {e}")
    return msg_id


SEP = "━━━━━━━━━━━━━━━━━━"

# رابط لوحة المتتبّع (GitHub Pages) — يُرفق كزر في الرسائل
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://a3alshehhi.github.io/trade-bot/")


def _trend_label(pct):
    """يرجع (وصف، إيموجي) لقوة الاتجاه."""
    if pct >= 80:
        return "قوي جداً", "🔥"
    if pct >= 60:
        return "قوي", "💪"
    if pct >= 40:
        return "متوسط", "⚖️"
    return "ضعيف", "⚠️"


def _dir_emoji(score):
    """🟢 شراء | 🔴 بيع | ⚪ حيادي."""
    if score >= 20:
        return "🟢"
    if score <= -20:
        return "🔴"
    return "⚪"


def format_signal_card(r, cfg):
    """ينسّق صفقة واحدة كبطاقة بنفس نسق الصورة المرجعية."""
    is_buy = r["score"] > 0
    head_emoji = "🟢" if is_buy else "🔴"
    head_text = "BUY or LONG" if is_buy else "SELL or SHORT"
    t_label, t_emoji = _trend_label(r["trend_strength"])
    now = datetime.now().strftime("%H:%M:%S")

    def fmt(v):
        return f"{v:.8f}".rstrip("0").rstrip(".") if v < 1 else f"{v:,.2f}"

    price_str = fmt(r["price"])
    dca = r.get("dca") if cfg.get("dca") else None
    # عند تفعيل DCA: الوقف من سلّم DCA، ونِسب الأهداف من متوسط الدخول
    stop_str = fmt(dca["stop"] if dca else r["stop"])
    pct_key = "pct_avg" if dca else "pct"
    tps = r.get("targets") or []
    tp_lines = []
    for i, tp in enumerate(tps[:3]):
        pct = tp.get(pct_key, tp["pct"])
        tp_lines.append(f"🎯 الهدف {i+1}: {fmt(tp['price'])}  (+{pct:.2f}%)")

    dca_block = []
    if dca:
        dca_block.append("🪜 سلّم الدخول (DCA × 4):")
        for i, lvl in enumerate(dca["levels"]):
            dca_block.append(f"   دخول {i+1}: {fmt(lvl)}")
        dca_block.append(f"   ⚖️ متوسط الدخول: {fmt(dca['avg'])}")

    # --- كتلة تأكيد متعدد الفريمات ---
    mtf = r.get("mtf") or []
    mtf_block = []
    if mtf:
        line = " | ".join(f"{tf} {_dir_emoji(sc)}" for tf, sc in mtf)
        mtf_block = [SEP, "", f"🔍 الفريمات: {line}"]
        # الفريمات المتوافقة مع اتجاه الصفقة
        aligned = [tf for tf, sc in mtf if (sc > 0) == is_buy and abs(sc) >= 20]
        if len(aligned) >= 2:
            mtf_block.append(f"⭐ تكررت الصفقة المتوافقة على: {'، '.join(aligned)}")

    conviction_banner = ["⭐⭐ صفقة عالية القناعة ⭐⭐", SEP, ""] if r.get("high_conviction") else []

    return "\n".join([
        SEP,
        f"{head_emoji} {head_text}",
        SEP,
        "",
        *conviction_banner,
        f"⏱️ الفريم: {cfg['timeframe']}",
        f"💰 العملة: {r['symbol']}",
        f"💵 السعر: {price_str}",
        SEP,
        "",
        f"◾ الحالة: ✅ {r['signal']}",
        f"◾ قوة الحجم: {r['vol_strength']}%",
        f"◾ قوة الاتجاه: {r['trend_strength']}%  ({t_label}) {t_emoji}",
        *([f"📐 دايفرجنس إيجابي: {' + '.join(r['div_inds'])} ✅"]
          if r.get("divergence") == "bull" and r.get("div_inds") else []),
        *([f"🌐 اتجاه السوق ({r['market']['ref']}): {r['market']['label']} "
           f"{'✅' if r.get('conviction_parts', {}).get('market_ok', True) else '⚠️ معاكس'}"]
          if r.get("market") else []),
        *([f"📅 متوسط 365 يوم: {'فوقه ✅' if r['yearly']['above'] else 'تحته ⚠️'}"]
          if r.get("yearly") else []),
        *mtf_block,
        SEP,
        "",
        *dca_block,
        *(["", *tp_lines] if dca_block else tp_lines),
        f"🛑 وقف الخسارة: {stop_str}",
        SEP,
        "",
        f"⏰ الوقت: {now}",
        SEP,
        "",
        "💡 إدارة المخاطر سر النجاح",
        "⚠️ تحليل تعليمي — ليس نصيحة مالية",
    ])


# ======================================================================
#  6) التشغيل الرئيسي
# ======================================================================
def run(cfg, watchlist_path, out_dir):
    print("=" * 64)
    print("  بوت البحث عن الصفقات — جارٍ التحليل")
    print("=" * 64)

    parsed = parse_watchlist(watchlist_path)
    targets = []
    if cfg["assets"] in ("all", "stocks"):
        targets += [(it, "stock") for it in parsed["stocks"]]
    if cfg["assets"] in ("all", "crypto"):
        targets += [(it, "crypto") for it in parsed["crypto"]]

    print(f"الأسهم: {len(parsed['stocks'])} | الكريبتو: {len(parsed['crypto'])} | "
          f"رموز سياق: {len(parsed['macro'])} | تم تجاهل: {len(parsed['skipped'])}")

    # --- فلتر اتجاه السوق العام (يُحسب مرة واحدة) ---
    regimes = {}
    if cfg["assets"] in ("all", "crypto"):
        regimes["crypto"] = market_regime("crypto")
    if cfg["assets"] in ("all", "stocks"):
        regimes["stock"] = market_regime("stock")
    for k, rg in regimes.items():
        if rg:
            print(f"🌐 اتجاه السوق ({rg['ref']}): {rg['label']} — {rg['detail']}")
    print(f"سيتم فحص {len(targets)} رمز على إطار {cfg['timeframe']} ...\n")

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = {ex.submit(scan_symbol, it, kind, cfg): it for it, kind in targets}
        for fut in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  ... فُحص {done}/{len(targets)}")
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        print("\n⚠️ لم يتم جلب أي بيانات. تحقق من اتصال الإنترنت أو تثبيت yfinance.")
        _tok = cfg.get("tg_token") or os.environ.get("TELEGRAM_TOKEN")
        _cid = cfg.get("tg_chat") or os.environ.get("TELEGRAM_CHAT_ID")
        if _tok and _cid and not cfg.get("quiet_empty"):
            send_telegram(_tok, _cid,
                f"⚠️ بوت الصفقات: لم يتم جلب أي بيانات للإطار {cfg['timeframe']} ({cfg['assets']}).\n"
                "تحقق من اتصال الإنترنت أو توفر البيانات.")
        return

    # ربط كل نتيجة باتجاه سوقها
    for r in results:
        r["market"] = regimes.get(r["kind"])

    # تصفية حسب نوع الصفقة المطلوب (شراء فقط افتراضياً)
    side = cfg.get("side", "buy")
    if side == "buy":
        results = [r for r in results if r["score"] > 0]
    elif side == "sell":
        results = [r for r in results if r["score"] < 0]

    # فلتر اتجاه السوق (اختياري - hard): استبعاد الصفقات المعاكسة للسوق
    if cfg.get("market_filter"):
        before = len(results)
        def aligned(r):
            mk = r.get("market")
            if mk is None:
                return True
            return mk["bullish"] if r["score"] > 0 else not mk["bullish"]
        results = [r for r in results if aligned(r)]
        print(f"فلتر اتجاه السوق مفعّل: استُبعد {before - len(results)} صفقة معاكسة، بقيت {len(results)}.")

    # تصفية اختيارية: الصفقات ذات الدايفرجنس المؤكّد فقط
    if cfg.get("require_divergence"):
        want = "bull" if side != "sell" else "bear"
        results = [r for r in results if r.get("divergence") == want]
        print(f"تصفية الدايفرجنس مفعّلة: بقيت {len(results)} صفقة.")

    # ترتيب: الأقوى إشارةً (قيمة مطلقة للدرجة)
    results.sort(key=lambda x: abs(x["score"]), reverse=True)
    shown = [r for r in results if abs(r["score"]) >= cfg["min_score"]][:cfg["top"]]

    # ---- طباعة الملخص ----
    print("\n" + "=" * 64)
    print(f"  أفضل {len(shown)} فرصة (درجة ≥ {cfg['min_score']})")
    print("=" * 64)
    for r in shown:
        arrow = "🟢" if r["score"] > 0 else "🔴"
        print(f"\n{arrow} {r['symbol']:<12} [{r['kind']}]  "
              f"الإشارة: {r['signal']}  |  الدرجة: {r['score']:+d}")
        print(f"   السعر: {r['price']:<12g}  RSI: {r['rsi']}  "
              f"وقف: {r['stop']:g}  هدف: {r['target']:g}  (عائد/مخاطرة {r['rr']})")
        print(f"   الأسباب: " + "؛ ".join(r["reasons"][:5]))

    # ---- حفظ التقارير ----
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(out_dir, f"signals_{ts}.csv")
    def _tp(r, i, key):
        tps = r.get("targets") or []
        return tps[i][key] if i < len(tps) else ""

    df_out = pd.DataFrame([{
        "الرمز": r["symbol"], "النوع": r["kind"], "الإشارة": r["signal"],
        "الدرجة": r["score"], "السعر": r["price"], "RSI": r["rsi"],
        "قوة_الحجم%": r.get("vol_strength"), "قوة_الاتجاه%": r.get("trend_strength"),
        "وقف_الخسارة": r["stop"],
        "هدف1": _tp(r, 0, "price"), "هدف1%": _tp(r, 0, "pct"),
        "هدف2": _tp(r, 1, "price"), "هدف2%": _tp(r, 1, "pct"),
        "هدف3": _tp(r, 2, "price"), "هدف3%": _tp(r, 2, "pct"),
        "الأسباب": " | ".join(r["reasons"]),
    } for r in results])
    df_out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n✅ حُفظ التقرير الكامل ({len(results)} رمز): {csv_path}")

    # ---- إرسال إلى تيليجرام (إن وُجدت الإعدادات) ----
    token = cfg.get("tg_token") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = cfg.get("tg_chat") or os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        if shown:
            # تأكيد متعدد الفريمات للصفقات المعروضة فقط (توفيراً للوقت)
            print("  ... فحص تأكيد الفريمات المتعددة للصفقات المختارة")
            for r in shown:
                try:
                    r["mtf"] = multi_tf_check(r["symbol"], r["kind"])
                except Exception:
                    r["mtf"] = []
                try:
                    r["yearly"] = yearly_ma_status(r["symbol"], r["kind"])
                except Exception:
                    r["yearly"] = None
                evaluate_conviction(r)
            # الصفقات عالية القناعة أولاً ثم الأقوى درجةً
            shown.sort(key=lambda x: (x.get("high_conviction", False), abs(x["score"])),
                       reverse=True)
            hc = sum(1 for r in shown if r.get("high_conviction"))
            print(f"  ⭐ صفقات عالية القناعة: {hc}/{len(shown)}")
            sent = 0
            # تحميل الصفقات الحالية لتفادي التكرار
            state_path = cfg.get("state_path", TRADES_FILE)
            trades = load_trades(state_path)
            open_syms = {t["symbol"] for t in trades if t["status"] == "open"}
            # منع تكرار التنبيه: نرسل فقط الإشارات الجديدة (غير المفتوحة أصلاً)
            new_signals = [r for r in shown if r["symbol"] not in open_syms]
            for r in new_signals:
                if send_telegram(token, chat_id, format_signal_card(r, cfg)):
                    sent += 1
                    # حفظ الصفقة للمتابعة
                    if r.get("targets"):
                        trades.append(make_trade(r, cfg))
                        open_syms.add(r["symbol"])
                time.sleep(0.6)   # تفادي حدود إرسال تيليجرام
            save_trades(trades, state_path)
            if new_signals:
                print(f"📲 أُرسلت {sent} بطاقة صفقة جديدة إلى تيليجرام، وحُفظت للمتابعة.")
            else:
                print("لا صفقات جديدة — كل الإشارات الحالية مُنبَّه بها مسبقاً (لا تكرار).")
        elif not cfg.get("quiet_empty"):
            send_telegram(token, chat_id, "🤖 بوت الصفقات: لا توجد فرص تتجاوز الحد المطلوب الآن.")
            print("📲 أُرسل تنبيه (لا فرص) إلى تيليجرام.")
        else:
            print("لا فرص جديدة الآن — كُتمت رسالة الفراغ (وضع الفحص المتكرر).")

    print("\n⚠️ تذكير: أداة تحليل تعليمية فقط — ليست نصيحة مالية. القرار والمسؤولية عليك.")


# ======================================================================
#  7) الاختبار التاريخي (Backtest) — قياس أداء الاستراتيجية على الماضي
# ======================================================================
#
# المبدأ: نمشي شمعةً بشمعة. عند كل شمعة نحلّل البيانات حتى تلك الشمعة فقط
# (بدون look-ahead). إذا ظهرت إشارة مؤهّلة نفتح صفقة افتراضية بسعر إغلاق
# الشمعة، ثم نحاكي الشموع التالية لنرى أيهما يتحقق أولاً: الوقف أم الأهداف.
# نقيس النتيجة بوحدات المخاطرة (R): ربح/خسارة كل صفقة ÷ مخاطرتها.
#
# نحسب سيناريوهين لنفس الدخولات:
#   A) بدون إدارة : نمسك حتى الهدف الأخير أو الوقف.
#   B) مع إدارة    : عند الهدف الأول نجني 50% وننقل الوقف لنقطة الدخول،
#                    والباقي يركض للهدف الأخير أو يخرج عند نقطة الدخول.
# الافتراض المحافظ: لو لمست الشمعة الوقف والهدف معاً، نعتبر الوقف ضُرب أولاً.

def _simulate_trade(df, i, entry, stop, targets, direction, hold, manage, cost=0.0):
    """يحاكي مصير صفقة فُتحت عند الشمعة i. يرجع (R, نتيجة) أو None.
    cost: تكلفة ذهاب-وإياب كنسبة من القيمة (عمولة+انزلاق)، تُطرح بوحدات R."""
    n = len(df)
    risk = abs(entry - stop)
    if risk <= 0 or not targets:
        return None
    # تكلفة الصفقة بوحدات R: كلما ضاق الوقف، زاد وزن العمولة نسبياً
    cost_r = cost * entry / risk if cost else 0.0
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tp1, tp_final = targets[0], targets[-1]

    stop_cur = stop
    part = 1.0          # الجزء المتبقّي من الصفقة
    realized = 0.0      # الربح/الخسارة المحقّق بوحدات R
    tp1_done = False
    last_c = entry
    # سياسة الخروج المُدارة: إغلاق الصفقة **بالكامل عند الهدف الأول** (بلا جني جزئي
    # على أهداف متعددة وبلا نقل وقف للتعادل) — أبسط وأوضح بطلب بو محمد.
    def hit_stop(px_lo, px_hi):
        return px_lo <= stop_cur if direction == 1 else px_hi >= stop_cur

    def hit(level, px_lo, px_hi):
        return px_hi >= level if direction == 1 else px_lo <= level

    for j in range(i + 1, min(i + 1 + hold, n)):
        lo, hi, last_c = low[j], high[j], close[j]
        # 1) الوقف أولاً (محافظ) — الجزء المتبقّي يخرج عند الوقف الجاري
        if hit_stop(lo, hi):
            realized += part * direction * (stop_cur - entry) / risk
            return realized - cost_r, ("be_stop" if tp1_done else "stop")
        # 2) الهدف — إغلاق كامل عند الهدف الأول (مُدار) أو الأخير (خام)
        if manage:
            if hit(targets[0], lo, hi):       # إغلاق 100% عند الهدف الأول
                realized += part * direction * (targets[0] - entry) / risk
                return realized - cost_r, "target"
        else:
            if hit(tp_final, lo, hi):
                realized += part * direction * (tp_final - entry) / risk
                return realized - cost_r, "target"
    # 3) خروج زمني عند آخر إغلاق متاح
    realized += part * direction * (last_c - entry) / risk
    return realized - cost_r, "time"


def build_regime_series(kind):
    """يبني سلسلة اتجاه السوق التاريخية (يومي): BTC للكريبتو، SPY للأسهم.
    يرجع dict فيه مصفوفة تواريخ مرتبة وراية صعود لكل يوم، أو None."""
    if kind == "crypto":
        df = fetch_binance("BTCUSDT", "1d", 500)
    else:
        df = fetch_stock("SPY", "1d", "2y")
    if df is None or len(df) < 60:
        return None
    close = df["close"]
    ema50 = ema(close, 50)
    ema200 = ema(close, 200) if len(df) >= 200 else ema(close, 100)
    bullish = (close > ema50) & (ema50 >= ema200 * 0.99)
    dates = pd.to_datetime(df["date"]).dt.normalize().values.astype("datetime64[D]")
    return {"dates": dates, "bullish": bullish.values.astype(bool)}


def regime_bullish_at(reg, date):
    """حالة اتجاه السوق كما كانت بتاريخ معيّن (as-of). يرجع True/False أو None."""
    if reg is None:
        return None
    try:
        d = np.datetime64(pd.to_datetime(date).normalize(), "D")
    except Exception:
        return None
    idx = int(np.searchsorted(reg["dates"], d, side="right")) - 1
    if idx < 0:
        return None
    return bool(reg["bullish"][idx])


def anchored_vwap_last(df, anchor):
    """VWAP مثبّت (Anchored) عند بداية الأسبوع/الشهر، قيمته عند آخر شمعة.
    anchor: 'W' أسبوعي (يُصفّر الإثنين) أو 'M' شهري (يُصفّر أول الشهر).
    يُحسب من البيانات اليومية: مجموع (السعر النموذجي × الحجم) ÷ مجموع الحجم."""
    if df is None or len(df) < 2:
        return None
    d = pd.to_datetime(df["date"])
    last = d.iloc[-1]
    if anchor == "D":
        start = last.normalize()                       # بداية اليوم (مفيد على 4h/1h)
    elif anchor == "W":
        start = (last - pd.Timedelta(days=int(last.weekday()))).normalize()
    else:  # 'M'
        start = last.normalize().replace(day=1)
    mask = (d >= start).values
    sub = df[mask]
    if len(sub) == 0:
        return None
    tp = (sub["high"] + sub["low"] + sub["close"]) / 3.0
    vol = sub["volume"]
    denom = float(vol.sum())
    if denom <= 0:
        return None
    return float((tp * vol).sum() / denom)


def supply_demand_ok(df, atr, direction, lookback=80, body_mult=1.5, vol_mult=1.2):
    """يتحقق إن كان السعر الحالي عند منطقة طلب (شراء) أو عرض (بيع) طازجة.
    منطقة الطلب = قاعدة (شمعة قبل) تلاها اندفاع صاعد قوي (جسم كبير + حجم مرتفع
    = اختلال توازن). تبقى طازجة ما لم يُكسر قاعها لاحقاً. يرجع True/False/None."""
    n = len(df)
    if n < 25 or atr <= 0:
        return None
    o = df["open"].values; c = df["close"].values
    h = df["high"].values; l = df["low"].values; v = df["volume"].values
    seg = slice(max(0, n - lookback), n)
    body = np.abs(c - o)
    avg_body = float(np.mean(body[seg])) or 1e-9
    avg_vol = float(np.mean(v[seg])) or 1e-9
    price = float(c[-1])
    band = 0.5 * atr
    start = max(2, n - lookback)
    # نمشي من الأحدث للأقدم ونرجع فور إيجاد منطقة طازجة قرب السعر
    for j in range(n - 2, start - 1, -1):
        strong = body[j] >= body_mult * avg_body and v[j] >= vol_mult * avg_vol
        if not strong:
            continue
        if direction == 1 and c[j] > o[j]:          # اندفاع صاعد -> منطقة طلب
            base_lo, base_hi = float(l[j - 1]), float(h[j - 1])
            broken = bool(np.any(l[j + 1:n - 1] < base_lo)) if j + 1 < n - 1 else False
            if not broken and (base_lo - band) <= price <= (base_hi + band):
                return True
        elif direction == -1 and c[j] < o[j]:        # اندفاع هابط -> منطقة عرض
            base_lo, base_hi = float(l[j - 1]), float(h[j - 1])
            broken = bool(np.any(h[j + 1:n - 1] > base_hi)) if j + 1 < n - 1 else False
            if not broken and (base_lo - band) <= price <= (base_hi + band):
                return True
    return False


def fetch_binance_paged(symbol, interval, total, limit=1000):
    """يجلب أكثر من 1000 شمعة بالتقسيم (paging) رجوعاً عبر endTime.
    يُستخدم للتحقّق خارج العيّنة على فترات أقدم."""
    total = int(total)
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tqav", "ignore"]
    frames, end_time, fetched, guard = [], None, 0, 0
    while fetched < total and guard < 25:
        guard += 1
        n_req = min(limit, total - fetched)
        params = {"symbol": symbol, "interval": interval, "limit": n_req}
        if end_time is not None:
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
        df = pd.DataFrame(data, columns=cols)
        for cc in ["open", "high", "low", "close", "volume"]:
            df[cc] = pd.to_numeric(df[cc], errors="coerce")
        df["date"] = pd.to_datetime(df["close_time"], unit="ms")
        frames.append(df[["date", "open", "high", "low", "close", "volume", "open_time"]])
        fetched += len(data)
        end_time = int(data[0][0]) - 1            # قبل أقدم شمعة في هذه الدفعة
        if len(data) < n_req:
            break
    if not frames:
        return None
    allf = (pd.concat(frames).drop_duplicates("open_time").sort_values("open_time")
            .dropna().reset_index(drop=True))
    return allf[["date", "open", "high", "low", "close", "volume"]]


def _simulate_dca(df, i0, direct_entry, dca_levels, stop, targets, hold, manage, cost):
    """محاكاة صفقة بدخول مباشر ثم DCA على مستويات فيبوناتشي.
    T0 = دخول مباشر عند التأكيد، ثم تُملأ شرائح DCA كلما هبط السعر للمستوى.
    R محسوبة على متوسط الدخول مقابل الوقف. الافتراض: شرائح متساوية الحجم."""
    n = len(df)
    if direct_entry <= stop or not targets:
        return None
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    tp1, tpf = targets[0], targets[-1]
    filled = [direct_entry]; nxt = 0
    tp1_done = False; realized = 0.0; part = 1.0
    cur_stop = stop; last_c = direct_entry

    def rv(px, avg):
        return (px - avg) / (avg - stop) if (avg - stop) > 0 else 0.0

    for j in range(i0 + 1, min(i0 + 1 + hold, n)):
        lo, hi, last_c = low[j], high[j], close[j]
        while nxt < len(dca_levels) and lo <= dca_levels[nxt]:   # ملء شرائح DCA
            filled.append(dca_levels[nxt]); nxt += 1
        avg = sum(filled) / len(filled)
        cost_r = cost * avg / (avg - stop) if (avg - stop) > 0 else 0.0
        if lo <= cur_stop:                                       # الوقف أولاً (محافظ)
            realized += part * rv(cur_stop, avg)
            return realized - cost_r, ("be_stop" if tp1_done else "stop")
        if manage:
            if not tp1_done and hi >= tp1:
                realized += 0.5 * rv(tp1, avg); part = 0.5; tp1_done = True; cur_stop = avg
            if tp1_done and hi >= tpf:
                realized += part * rv(tpf, avg)
                return realized - cost_r, "target"
        else:
            if hi >= tpf:
                realized += part * rv(tpf, avg)
                return realized - cost_r, "target"
    avg = sum(filled) / len(filled)
    cost_r = cost * avg / (avg - stop) if (avg - stop) > 0 else 0.0
    realized += part * rv(last_c, avg)
    return realized - cost_r, "time"


def _bt_fetch_df(sym, kind, cfg):
    """يجلب بيانات رمز للـbacktest مع دعم الجلب المقسّم وإزاحة خارج العيّنة.
    يدعم كاش اختياري (cfg['_df_cache']) لإعادة استخدام نفس البيانات عبر عدّة
    تشغيلات (مثل walk-forward الذي يكرّر نفس الرموز بإعدادات مختلفة) — يلغي
    الجلب المكرّر ويتفادى خنق مصدر البيانات (rate-limit)."""
    bars = cfg.get("bt_bars", 365)
    offset = int(cfg.get("bt_offset", 0))
    cache = cfg.get("_df_cache")
    key = (sym, kind, cfg.get("timeframe"), bars, offset)
    if cache is not None and key in cache:
        return cache[key]
    if kind == "crypto":
        need = bars + 1000 + offset      # إحماء كافٍ لمتوسط 200 على 4h (≈800 شمعة 1h)
        if need > 1000:
            df = fetch_binance_paged(sym, BINANCE_INTERVAL[cfg["timeframe"]], need)
        else:
            df = fetch_binance(sym, BINANCE_INTERVAL[cfg["timeframe"]], min(need, 1000))
    else:
        df = fetch_stock(sym, YF_INTERVAL[cfg["timeframe"]], "2y")
    if offset > 0 and df is not None and len(df) > offset + 120:
        df = df.iloc[:len(df) - offset].reset_index(drop=True)
    if cache is not None and df is not None:
        cache[key] = df
    return df


def backtest_symbol_reversal(item, kind, cfg):
    """استراتيجية الانعكاس الزخمي:
    RSI(21)<20 (تشبّع بيعي) → نتابع حتى RSI(21)>80 (تشبّع شرائي) →
    ننتظر ارتداداً يصنع قاعاً أعلى من قاع الموجة ثم التفاتاً صعوديّاً → دخول.
    الوقف تحت القاع الأعلى، أهداف 1R/2R/3R."""
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 40)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    os_th = cfg.get("rsi_os", 20.0)
    ob_th = cfg.get("rsi_ob", 80.0)

    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 120:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    rsi21 = rsi(df["close"], 21).values
    atrs = atr(df, 14).values
    # فلتر المتوسط 200 على 4h عند التأكيد (للساعة فقط): إغلاق الساعة فوق متوسط 200 المحسوب على 4h
    ma200_ob = cfg.get("ma200_ob") and cfg.get("timeframe") == "1h"
    if ma200_ob:
        s4 = df.set_index("date")["close"].resample("4h").last().dropna()
        sma4 = s4.rolling(200).mean().dropna().reset_index()
        sma4.columns = ["date", "ma"]
        sma200 = (pd.merge_asof(df[["date"]].copy(), sma4, on="date")["ma"].values
                  if len(sma4) else np.full(n, np.nan))
    else:
        sma200 = None
    dca_fib = cfg.get("dca_fib")

    warmup = 60
    start = max(warmup, n - bars)
    trades = []

    # حالات الآلة: 0=ننتظر تشبّع بيعي | 1=ننتظر تشبّع شرائي | 2=ننتظر القاع الأعلى
    state = 0
    ref_low = None      # أدنى قاع خلال موجة (بيعي→شرائي)
    peak = None         # قمة التشبّع الشرائي (مرجع)
    pull_low = None     # أدنى قاع في الارتداد بعد التشبّع الشرائي

    i = start
    while i < n - 1:
        r = rsi21[i]
        if np.isnan(r):
            i += 1
            continue

        if state == 0:
            if r < os_th:
                state = 1
                ref_low = low[i]
        elif state == 1:
            ref_low = min(ref_low, low[i])
            if r > ob_th:
                ok_trend = True
                if ma200_ob:                       # شرط: الإغلاق فوق متوسط 200 عند التشبّع الشرائي
                    m = sma200[i]
                    ok_trend = (not np.isnan(m)) and close[i] > m
                if ok_trend and dca_fib:
                    # دخول مباشر عند التأكيد ثم DCA على ارتدادات فيبو للموجة (قاع→قمة)
                    peak = high[i]
                    imp = peak - ref_low
                    atrv = atrs[i] if not np.isnan(atrs[i]) else close[i] * 0.02
                    if imp > 0:
                        direct = float(close[i])
                        dca_levels = [peak - rr * imp for rr in (0.382, 0.5, 0.618, 0.786)]
                        stp = float(ref_low - 0.5 * atrv)
                        tps = [round(ref_low + m * imp, 8) for m in (1.272, 1.618, 2.0)]
                        a = _simulate_dca(df, i, direct, dca_levels, stp, tps, hold, False, cost)
                        b = _simulate_dca(df, i, direct, dca_levels, stp, tps, hold, True, cost)
                        if a and b:
                            trades.append({
                                "symbol": sym, "kind": kind, "side": "buy", "bar": i,
                                "date": str(df["date"].iloc[i])[:10], "score": 0,
                                "entry": direct, "stop": stp,
                                "R_plain": round(a[0], 3), "out_plain": a[1],
                                "R_managed": round(b[0], 3), "out_managed": b[1],
                            })
                            i += hold
                    state = 0
                    ref_low = peak = pull_low = None
                    continue
                elif ok_trend:
                    state = 2
                    peak = high[i]
                    pull_low = None
                else:
                    state = 0                       # الإشارة تحت المتوسط 200 → إعداد مرفوض
                    ref_low = None
        elif state == 2:
            if high[i] > peak:                  # القمة ما زالت تصعد → حدّثها وأعد تتبّع الارتداد
                peak = high[i]
                pull_low = None
            elif pull_low is None or low[i] < pull_low:
                pull_low = low[i]               # نتتبّع أدنى قاع بعد القمة (نهاية التصحيح)
            atrv = atrs[i] if not np.isnan(atrs[i]) else close[i] * 0.02
            turn_up = pull_low is not None and close[i] > high[i - 1] and close[i] < peak
            entered = False
            if turn_up:
                imp = peak - ref_low                          # موجة الدفع (قاع→قمة)
                retr = (peak - pull_low) / imp if imp > 0 else 0.0   # عمق التصحيح بالفيبو
                in_zone = cfg.get("fib_lo", 0.382) <= retr <= cfg.get("fib_hi", 0.786)
                if in_zone:
                    entry = float(close[i])
                    stop = float(pull_low - 0.5 * atrv)
                    risk = entry - stop
                    corr = peak - pull_low                    # موجة التصحيح (للأهداف)
                    # أهداف: امتداد فيبو لموجة التصحيح فوق القمة
                    tps = [round(pull_low + m * corr, 8) for m in (1.272, 1.618, 2.618)]
                    if risk > 0 and tps[0] > entry:
                        a = _simulate_trade(df, i, entry, stop, tps, 1, hold, manage=False, cost=cost)
                        b = _simulate_trade(df, i, entry, stop, tps, 1, hold, manage=True, cost=cost)
                        if a and b:
                            trades.append({
                                "symbol": sym, "kind": kind, "side": "buy", "bar": i,
                                "date": str(df["date"].iloc[i])[:10], "score": 0,
                                "entry": entry, "stop": stop, "fib_retr": round(retr, 3),
                                "R_plain": round(a[0], 3), "out_plain": a[1],
                                "R_managed": round(b[0], 3), "out_managed": b[1],
                            })
                            entered = True
                    # سواء دخلنا أو لا، انتهى هذا الإعداد عند ظهور الالتفات في المنطقة
                    state = 0
                    ref_low = peak = pull_low = None
                    if entered:
                        i += hold
                        continue
            if state == 2 and low[i] < ref_low:    # كسر قاع الموجة قبل الدخول → إلغاء
                state = 0
                ref_low = peak = pull_low = None
        i += 1
    return trades


def _simulate_direct_trail_div(df, i0, direct_entry, dca_levels, initial_stop, hold,
                               cost, rsi_arr, atr_arr, trail, buf=0.25, arm=0.0,
                               use_div=True):
    """دخول سوقي مباشر عند i0 + DCA أسفل الدخول، مع وقف متحرك تحت كل تصحيح
    (trail=True) وخروج كامل عند دايفرجنس سلبي، وإلا ضرب الوقف أو انتهاء المدة.
    buf = مضاعف ATR لمسافة الوقف تحت التصحيح. arm = لا يُفعَّل التتبّع إلا بعد ربح
    عائم ≥ arm×المخاطرة (0 = فوري). يرجع (R, outcome) أو None."""
    n = len(df)
    if direct_entry <= initial_stop:
        return None
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    risk0 = direct_entry - initial_stop
    filled = [direct_entry]
    nxt = 0
    cur_stop = initial_stop
    best_ph = None
    armed = (arm <= 0)
    end = min(i0 + 1 + hold, n)

    def _avg():
        return sum(filled) / len(filled)

    for j in range(i0 + 1, end):
        lo = low[j]
        while nxt < len(dca_levels) and lo <= dca_levels[nxt]:
            filled.append(dca_levels[nxt]); nxt += 1
        avg = _avg()
        cost_r = cost * avg / risk0
        if lo <= cur_stop:
            return (cur_stop - avg) / risk0 - cost_r, "trail_stop"
        if not armed and (high[j] - avg) >= arm * (avg - initial_stop):
            armed = True
        k = j - 2
        if k - 2 >= i0:
            piv_low = (low[k] <= low[k - 1] and low[k] <= low[k - 2]
                       and low[k] <= low[k + 1] and low[k] <= low[k + 2])
            piv_high = (high[k] >= high[k - 1] and high[k] >= high[k - 2]
                        and high[k] >= high[k + 1] and high[k] >= high[k + 2])
            atrk = atr_arr[k] if not np.isnan(atr_arr[k]) else avg * 0.01
            if trail and armed and piv_low:
                new_stop = low[k] - buf * atrk
                if new_stop > cur_stop and new_stop < close[j]:
                    cur_stop = new_stop
            if use_div and piv_high:
                rh = rsi_arr[k]
                if best_ph is not None and high[k] > best_ph[0] and rh < best_ph[1]:
                    return (close[j] - avg) / risk0 - cost_r, "divergence"
                if best_ph is None or high[k] > best_ph[0]:
                    best_ph = (high[k], rh)

    avg = _avg()
    cost_r = cost * avg / risk0
    return (close[end - 1] - avg) / risk0 - cost_r, "time"


def _simulate_fib_ladder(df, i_lock, fib_levels, initial_stop, hold, cost,
                         rsi_arr, atr_arr, trail, wait, buf=0.25, arm=0.0,
                         use_div=True):
    """دخول سلّمي عند مستويات فيبوناتشي التصحيحية (لا دخول سوقي مباشر):
      • تُملأ الشريحة كلما هبط السعر إلى مستوى فيبو (الأعلى = أقل تصحيح يُملأ أولاً).
      • يبدأ الإمساك من أول تعبئة؛ لو لم يُلمس أعلى مستوى خلال نافذة الانتظار → لا صفقة.
      • وقف متحرك يرتفع تحت كل تصحيح (trail=True)، وخروج كامل عند دايفرجنس سلبي،
        وإلا ضرب الوقف (الابتدائي/المتحرك) أو انتهاء مدة الإمساك.
    fib_levels: قائمة تنازلية (0.382 أعلى → 0.786 أدنى). يرجع (R, outcome, j0) أو None.
    R على متوسط الدخول مقابل (المتوسط − الوقف الابتدائي)."""
    n = len(df)
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    top = fib_levels[0]
    j0 = None                                        # أول شمعة تلمس أعلى مستوى فيبو
    for j in range(i_lock + 1, min(i_lock + 1 + wait, n)):
        if low[j] <= top:
            j0 = j
            break
    if j0 is None:
        return None

    filled = []
    nxt = 0
    cur_stop = initial_stop
    best_ph = None
    armed = (arm <= 0)
    end = min(j0 + 1 + hold, n)

    def _avg():
        return sum(filled) / len(filled) if filled else top

    for j in range(j0, end):
        lo = low[j]
        while nxt < len(fib_levels) and lo <= fib_levels[nxt]:    # ملء شرائح الفيبو
            filled.append(fib_levels[nxt]); nxt += 1
        avg = _avg()
        risk = avg - initial_stop
        if risk <= 0:
            return None
        cost_r = cost * avg / risk
        if lo <= cur_stop:                                        # الوقف أولاً (محافظ)
            return (cur_stop - avg) / risk - cost_r, "trail_stop", j0
        if not armed and (high[j] - avg) >= arm * risk:           # تفعيل التتبّع بعد ربح عائم
            armed = True

        k = j - 2                                                 # شمعة محورية مؤكَّدة الآن
        if k - 2 >= j0:
            piv_low = (low[k] <= low[k - 1] and low[k] <= low[k - 2]
                       and low[k] <= low[k + 1] and low[k] <= low[k + 2])
            piv_high = (high[k] >= high[k - 1] and high[k] >= high[k - 2]
                        and high[k] >= high[k + 1] and high[k] >= high[k + 2])
            atrk = atr_arr[k] if not np.isnan(atr_arr[k]) else avg * 0.01
            if trail and armed and piv_low:                      # ارفع الوقف تحت التصحيح
                new_stop = low[k] - buf * atrk
                if new_stop > cur_stop and new_stop < close[j]:
                    cur_stop = new_stop
            if use_div and piv_high:                             # افحص الدايفرجنس السلبي
                rh = rsi_arr[k]
                if best_ph is not None and high[k] > best_ph[0] and rh < best_ph[1]:
                    return (close[j] - avg) / risk - cost_r, "divergence", j0
                if best_ph is None or high[k] > best_ph[0]:
                    best_ph = (high[k], rh)

    avg = _avg()
    risk = avg - initial_stop
    if risk <= 0:
        return None
    cost_r = cost * avg / risk
    return (close[end - 1] - avg) / risk - cost_r, "time", j0


def backtest_symbol_osob(item, kind, cfg):
    """استراتيجية «الدخول الفيبوناتشي بعد الموجة + خروج بالدايفرجنس» (osob):

    الإعداد لكل رمز:
      1) RSI(21) ينزل تحت عتبة البيع (20) — قاع/موجة هابطة.
      2) نتابع حتى يتجاوز RSI(21) عتبة الشراء (80) — موجة دفع صاعدة مؤكَّدة؛
         نحتفظ بأعمق قاع (ref_low) وأعلى قمة (peak) للموجة.
      3) حين تنتهي الموجة (RSI<80) نثبّت القمة ونضع سلّم دخول عند ارتدادات فيبو
         peak−{0.382,0.5,0.618,0.786}·imp. الدخول يتحقق عند لمس هذه المستويات (DCA)
         — لا دخول سوقي مباشر.
    الإدارة: وقف متحرك يرتفع تحت كل تصحيح + خروج كامل عند دايفرجنس سلبي (ولو تجاوزنا
      الأهداف) أو ضرب الوقف. الأهداف الفيبوناتشية مرجعية فقط — لا بيع عندها.
    عمود out_plain = بوقف ثابت | out_managed = بوقف متحرك (كلاهما يخرج بالدايفرجنس)."""
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 40)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    os_th = cfg.get("rsi_os", 20.0)
    ob_th = cfg.get("rsi_ob", 80.0)
    # 1h/15m: دخول سلّمي فيبوناتشي | 1d/4h: دخول سوقي مباشر + DCA فيبو
    # --force-direct يفرض الدخول المباشر على أي إطار (مثل 1h يصير كـ4h)
    ladder_mode = (cfg.get("timeframe") in ("1h", "15m")) and not cfg.get("force_direct")
    trend_filter = cfg.get("trend_filter", False)
    trail_buf = cfg.get("trail_buf", 0.25)
    trail_arm = cfg.get("trail_arm", 0.0)
    use_div = not cfg.get("no_div", False)

    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 120:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    rsi21 = rsi(df["close"], 21).values
    atrs = atr(df, 14).values
    # فلتر الاتجاه: إغلاق فوق متوسط 200 وقت التثبيت.
    # --htf-trend يحسبه على فريم أعلى (1h→4h، 15m→1h، 4h/1d→1d) لفلتر اتجاه أقوى.
    if trend_filter:
        if cfg.get("htf_trend"):
            htf = {"1h": "4h", "15m": "1h", "4h": "1D", "1d": "1D"}.get(cfg.get("timeframe"), "1D")
            s = df.set_index("date")["close"].resample(htf).last().dropna()
            sm = s.rolling(200).mean().dropna().reset_index()
            sm.columns = ["date", "ma"]
            sma200 = (pd.merge_asof(df[["date"]].copy(), sm, on="date")["ma"].values
                      if len(sm) else np.full(n, np.nan))
        else:
            sma200 = df["close"].rolling(200).mean().values
    else:
        sma200 = None

    warmup = 60
    start = max(warmup, n - bars)
    trades = []

    state = 0           # 0=ننتظر تشبّع بيعي | 1=ننتظر تشبّع شرائي | 2=داخل التشبّع الشرائي
    ref_low = None
    peak = None

    i = start
    while i < n - 1:
        r = rsi21[i]
        if np.isnan(r):
            i += 1
            continue

        if state == 0:
            if r < os_th:
                state = 1
                ref_low = low[i]
                peak = high[i]
        elif state == 1:
            ref_low = min(ref_low, low[i])
            peak = max(peak, high[i])
            if r > ob_th:
                state = 2
        elif state == 2:
            peak = max(peak, high[i])
            if r < ob_th:                        # انتهت موجة التشبّع الشرائي → ثبّت المرجع
                imp = peak - ref_low
                trend_ok = (not trend_filter) or (
                    sma200 is not None and not np.isnan(sma200[i]) and close[i] > sma200[i])
                if imp > 0 and trend_ok:
                    atrv = atrs[i] if not np.isnan(atrs[i]) else close[i] * 0.02
                    fib = [round(peak - rr * imp, 8) for rr in (0.382, 0.5, 0.618, 0.786)]
                    if ladder_mode:
                        # 1h/15m: دخول سلّمي عند ارتدادات فيبو (لا دخول سوقي)
                        stp = float(ref_low - 0.5 * atrv)
                        a = _simulate_fib_ladder(df, i, fib, stp, hold, cost, rsi21, atrs,
                                                 trail=False, wait=hold,
                                                 buf=trail_buf, arm=trail_arm, use_div=use_div)
                        b = _simulate_fib_ladder(df, i, fib, stp, hold, cost, rsi21, atrs,
                                                 trail=True, wait=hold,
                                                 buf=trail_buf, arm=trail_arm, use_div=use_div)
                        if a and b:
                            j0 = b[2]
                            trades.append({
                                "symbol": sym, "kind": kind, "side": "buy",
                                "mode": "ladder", "lock_bar": i, "fill_bar": j0,
                                "date": str(df["date"].iloc[j0])[:10], "score": 0,
                                "entry_ref": fib[0], "stop": round(stp, 8),
                                "R_plain": round(a[0], 3), "out_plain": a[1],
                                "R_managed": round(b[0], 3), "out_managed": b[1],
                            })
                            i = j0 + hold
                            state = 0
                            ref_low = peak = None
                            continue
                    else:
                        # دخول سوقي مباشر + سلّم DCA بارتدادات فيبو أسفل الدخول
                        # (نفس بناء الإشارة الحيّة عبر _fib_dca_ladder لضمان التطابق)
                        entry = float(close[i])
                        dca = _fib_dca_ladder(entry, peak, ref_low, imp)
                        deepest = min(dca) if dca else ref_low
                        stp = float(min(ref_low, deepest) - 0.5 * atrv)
                        if entry > stp:
                            if cfg.get("tp1_exit"):
                                # خروج كامل عند الهدف الأول (أهداف فيبو امتدادية للموجة)
                                tps = [round(ref_low + mm * imp, 8)
                                       for mm in (1.272, 1.618, 2.0)]
                                a = _simulate_trade(df, i, entry, stp, tps, 1, hold,
                                                    manage=False, cost=cost)
                                b = _simulate_trade(df, i, entry, stp, tps, 1, hold,
                                                    manage=True, cost=cost)
                            else:
                                a = _simulate_direct_trail_div(df, i, entry, dca, stp, hold,
                                                               cost, rsi21, atrs, trail=False,
                                                               buf=trail_buf, arm=trail_arm,
                                                               use_div=use_div)
                                b = _simulate_direct_trail_div(df, i, entry, dca, stp, hold,
                                                               cost, rsi21, atrs, trail=True,
                                                               buf=trail_buf, arm=trail_arm,
                                                               use_div=use_div)
                            if a and b:
                                trades.append({
                                    "symbol": sym, "kind": kind, "side": "buy",
                                    "mode": "direct", "lock_bar": i, "fill_bar": i,
                                    "date": str(df["date"].iloc[i])[:10], "score": 0,
                                    "entry_ref": round(entry, 8), "stop": round(stp, 8),
                                    "R_plain": round(a[0], 3), "out_plain": a[1],
                                    "R_managed": round(b[0], 3), "out_managed": b[1],
                                })
                                i += hold
                                state = 0
                                ref_low = peak = None
                                continue
                state = 0
                ref_low = peak = None
        i += 1
    return trades


def backtest_symbol_trendwave(item, kind, cfg):
    """استراتيجية مستقلة «trendwave» — الإعداد الرابح الموحّد على كل الفريمات:

      • الإعداد: RSI(21) ينزل تحت 20 ثم يتجاوز 80 (موجة دفع صاعدة مؤكَّدة).
      • الدخول: سوقي مباشر عند نهاية الموجة + تعديل بمستويات فيبو تصحيحية (DCA).
      • فلتر الاتجاه: إغلاق فوق متوسط 200 على *فريم أعلى* (1h→4h، 15m→1h، 4h/1d→1d).
      • الخروج: إغلاق كامل عند الهدف الأول (أهداف فيبو امتدادية) أو وقف الخسارة — بلا تتبّع ولا دايفرجنس.

    مبنيّة فوق محرّك osob لكنها مُقدَّمة كاستراتيجية مستقلة بإعداداتها المثبّتة.
    نتائج الباك-تست الحقيقية (Binance): رابحة على 1d و4h و1h."""
    c = dict(cfg)
    c["force_direct"] = True      # دخول مباشر على كل الفريمات (بما فيها 1h/15m)
    c["no_div"] = True            # بلا خروج دايفرجنس
    c["tp1_exit"] = True          # خروج كامل عند الهدف الأول (بدل الوقف المتحرك)
    c["trend_filter"] = True      # فلتر اتجاه مفعّل
    c["htf_trend"] = True         # من فريم أعلى
    c.setdefault("trail_buf", 0.5)
    c.setdefault("trail_arm", 1.0)
    return backtest_symbol_osob(item, kind, c)


def backtest_symbol_os_multi(item, kind, cfg):
    """استراتيجية «تكرار التشبّع البيعي بعد موجة شرائية مكتملة» (os-multi):

    التسلسل لكل رمز:
      1) تكتمل موجة تشبّع شرائي: RSI(21) يتجاوز عتبة الشراء (80) ثم يعود تحتها
         (نهاية الموجة). نحتفظ بأعلى قمة بلغتها الموجة كمرجع (peak).
      2) بعد اكتمال الموجة نعدّ نزولات التشبّع البيعي: كل هبوط لـ RSI(21) تحت
         عتبة البيع (20) يُحتسب «مرة»، ولا تُحتسب المرة التالية إلا بعد عودة
         RSI فوق العتبة (نزول مستقل). نتابع أعمق قاع خلال هذه النزولات (ref_low).
      3) عند بلوغ العدد المطلوب (os_touches: 15m=3، 1h=2) ندخل مباشرةً عند
         إغلاق تلك الشمعة. الفيبو/الأهداف بنفس معادلات الدخول المباشر الحالية:
           imp        = peak - ref_low
           DCA        = peak - {0.382,0.5,0.618,0.786}·imp
           الوقف      = أسفل أعمق مستوى دخول مُدرَج (− 0.5·ATR)
           الأهداف    = ref_low + {1.272,1.618,2.0}·imp
      لو ظهرت موجة شرائية جديدة (RSI>80) أثناء العدّ، يُعاد ضبط الإعداد عليها.
    """
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 40)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    os_th = cfg.get("rsi_os", 20.0)
    ob_th = cfg.get("rsi_ob", 80.0)
    need = int(cfg.get("os_touches", 2))

    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 120:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    rsi21 = rsi(df["close"], 21).values
    atrs = atr(df, 14).values

    warmup = 60
    start = max(warmup, n - bars)
    trades = []

    # ob_done=False: ننتظر موجة شرائية تكتمل | True: نعدّ نزولات التشبّع البيعي
    ob_done = False
    in_ob = False
    ob_peak = None      # أعلى قمة بلغتها الموجة الشرائية (مرجع الفيبو الأعلى)
    touches = 0         # عدد نزولات التشبّع البيعي المستقلة بعد اكتمال الموجة
    armed = True        # جاهز لعدّ نزول جديد (RSI فوق عتبة البيع حالياً)
    ref_low = None      # أعمق قاع خلال النزولات

    i = start
    while i < n - 1:
        r = rsi21[i]
        if np.isnan(r):
            i += 1
            continue

        if not ob_done:
            if r > ob_th:                       # داخل موجة تشبّع شرائي
                in_ob = True
                ob_peak = high[i] if ob_peak is None else max(ob_peak, high[i])
            elif in_ob:                          # RSI عاد تحت 80 → اكتملت الموجة
                ob_done = True
                in_ob = False
                touches = 0
                armed = True
                ref_low = None
            i += 1
            continue

        # ob_done == True: مرحلة عدّ نزولات التشبّع البيعي
        if r > ob_th:                            # موجة شرائية جديدة → أعد الإعداد عليها
            ob_done = False
            in_ob = True
            ob_peak = high[i]
            touches = 0
            armed = True
            ref_low = None
            i += 1
            continue

        ob_peak = high[i] if ob_peak is None else max(ob_peak, high[i])
        if r < os_th:                            # داخل منطقة تشبّع بيعي
            ref_low = low[i] if ref_low is None else min(ref_low, low[i])
            if armed:
                touches += 1
                armed = False
        else:                                    # عاد فوق عتبة البيع → النزول التالي يُعدّ
            armed = True

        if touches >= need and ref_low is not None and ob_peak is not None:
            peak = ob_peak
            imp = peak - ref_low
            if imp > 0:
                entry = float(close[i])
                atrv = atrs[i] if not np.isnan(atrs[i]) else close[i] * 0.02
                dca_levels = [peak - rr * imp for rr in (0.382, 0.5, 0.618, 0.786)]
                stp = float(ref_low - 0.5 * atrv)
                stp = min(stp, round(min(dca_levels) - 0.5 * atrv, 8))
                tps = [round(ref_low + m * imp, 8) for m in (1.272, 1.618, 2.0)]
                a = _simulate_dca(df, i, entry, dca_levels, stp, tps, hold, False, cost)
                b = _simulate_dca(df, i, entry, dca_levels, stp, tps, hold, True, cost)
                if a and b:
                    trades.append({
                        "symbol": sym, "kind": kind, "side": "buy", "bar": i,
                        "date": str(df["date"].iloc[i])[:10], "score": 0,
                        "touches": need, "entry": entry, "stop": round(stp, 8),
                        "R_plain": round(a[0], 3), "out_plain": a[1],
                        "R_managed": round(b[0], 3), "out_managed": b[1],
                    })
                    i += hold
            # سواء دخلنا أو لا، انتهى هذا الإعداد → ابحث عن موجة شرائية جديدة
            ob_done = False
            in_ob = False
            ob_peak = None
            touches = 0
            armed = True
            ref_low = None
            continue
        i += 1
    return trades


# ======================================================================
#  استراتيجيات كلاسيكية معروفة (لمقارنتها عبر بوّابة walk-forward)
#  وقف موحّد 2×ATR لجعل R قابلاً للمقارنة بين الاستراتيجيات.
# ======================================================================
def _sim_long(df, i0, entry, stop, exit_fn, hold, cost):
    """صفقة شراء: وقف ثابت + خروج بإشارة exit_fn(j) أو انتهاء المدة.
    يرجع (R, outcome, exit_bar) حيث R = (الخروج − الدخول)/(الدخول − الوقف) − تكلفة."""
    n = len(df)
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    risk = entry - stop
    if risk <= 0:
        return None
    cost_r = cost * entry / risk
    end = min(i0 + 1 + hold, n)
    for j in range(i0 + 1, end):
        if low[j] <= stop:
            return (stop - entry) / risk - cost_r, "stop", j
        if exit_fn(j):
            return (close[j] - entry) / risk - cost_r, "signal", j
    return (close[end - 1] - entry) / risk - cost_r, "time", end - 1


def backtest_symbol_donchian(item, kind, cfg):
    """اختراق قناة Donchian (نظام السلاحف/Turtle): دخول عند اختراق أعلى قمة
    لـ don_entry شمعة سابقة + فلتر اتجاه (فوق متوسط 200)، خروج عند الإغلاق دون
    أدنى قاع لـ don_exit شمعة، ووقف 2×ATR. تتبّع اتجاه كلاسيكي موثّق."""
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 60)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    en = int(cfg.get("don_entry", 20))
    ex = int(cfg.get("don_exit", 10))
    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 220 + en:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    a = atr(df, 14).values
    sma200 = df["close"].rolling(200).mean().values
    hh = pd.Series(high).rolling(en).max().shift(1).values     # أعلى قمة سابقة
    ll = pd.Series(low).rolling(ex).min().shift(1).values      # أدنى قاع سابق
    warmup = max(220, en + 5)
    start = max(warmup, n - bars)
    trades = []
    i = start
    while i < n - 1:
        if (not np.isnan(hh[i]) and not np.isnan(sma200[i]) and not np.isnan(a[i])
                and high[i] > hh[i] and close[i] > sma200[i]):
            entry = float(close[i])
            stop = float(entry - 2.0 * a[i])
            r = _sim_long(df, i, entry, stop,
                          lambda j: (not np.isnan(ll[j])) and close[j] < ll[j],
                          hold, cost)
            if r:
                trades.append({
                    "symbol": sym, "kind": kind, "side": "buy", "date": str(df["date"].iloc[i])[:10],
                    "entry_ref": round(entry, 8), "stop": round(stop, 8),
                    "R_plain": round(r[0], 3), "out_plain": r[1],
                    "R_managed": round(r[0], 3), "out_managed": r[1],
                })
                i = r[2] + 1
                continue
        i += 1
    return trades


def backtest_symbol_ema_cross(item, kind, cfg):
    """تقاطع المتوسطات المتحرّكة (Golden Cross): دخول عند تقاطع EMA(fast) فوق
    EMA(slow)، خروج عند التقاطع العكسي، ووقف 2×ATR. تتبّع اتجاه بسيط وصلب."""
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 120)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    fast = int(cfg.get("ema_fast", 50))
    slow = int(cfg.get("ema_slow", 200))
    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < slow + 20:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    a = atr(df, 14).values
    ef = ema(df["close"], fast).values
    es = ema(df["close"], slow).values
    warmup = slow + 5
    start = max(warmup, n - bars)
    trades = []
    i = start
    while i < n - 1:
        if (not np.isnan(ef[i - 1]) and not np.isnan(es[i - 1]) and not np.isnan(a[i])
                and ef[i] > es[i] and ef[i - 1] <= es[i - 1]):     # تقاطع صعودي
            entry = float(close[i])
            stop = float(entry - 2.0 * a[i])
            r = _sim_long(df, i, entry, stop, lambda j: ef[j] < es[j], hold, cost)
            if r:
                trades.append({
                    "symbol": sym, "kind": kind, "side": "buy", "date": str(df["date"].iloc[i])[:10],
                    "entry_ref": round(entry, 8), "stop": round(stop, 8),
                    "R_plain": round(r[0], 3), "out_plain": r[1],
                    "R_managed": round(r[0], 3), "out_managed": r[1],
                })
                i = r[2] + 1
                continue
        i += 1
    return trades


def backtest_symbol_rsi2(item, kind, cfg):
    """ارتداد RSI(2) لـ Larry Connors: في اتجاه صاعد (فوق متوسط 200) ادخل عند
    هبوط RSI(2) تحت العتبة (تشبّع بيعي قصير)، واخرج عند إغلاق فوق متوسط 5،
    ووقف 2.5×ATR. ارتداد قصير المدى مشهور."""
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 15)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    buy = float(cfg.get("rsi2_buy", 10.0))
    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 220:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    a = atr(df, 14).values
    r2 = rsi(df["close"], 2).values
    sma200 = df["close"].rolling(200).mean().values
    sma5 = df["close"].rolling(5).mean().values
    warmup = 205
    start = max(warmup, n - bars)
    trades = []
    i = start
    while i < n - 1:
        if (not np.isnan(r2[i]) and not np.isnan(sma200[i]) and not np.isnan(a[i])
                and close[i] > sma200[i] and r2[i] < buy):
            entry = float(close[i])
            stop = float(entry - 2.5 * a[i])
            r = _sim_long(df, i, entry, stop,
                          lambda j: (not np.isnan(sma5[j])) and close[j] > sma5[j],
                          hold, cost)
            if r:
                trades.append({
                    "symbol": sym, "kind": kind, "side": "buy", "date": str(df["date"].iloc[i])[:10],
                    "entry_ref": round(entry, 8), "stop": round(stop, 8),
                    "R_plain": round(r[0], 3), "out_plain": r[1],
                    "R_managed": round(r[0], 3), "out_managed": r[1],
                })
                i = r[2] + 1
                continue
        i += 1
    return trades


def backtest_symbol(item, kind, cfg):
    """يفتح صفقات افتراضية على تاريخ رمز واحد ويرجع قائمة صفقات مغلقة."""
    sym = item["symbol"]
    reg = (cfg.get("_regime") or {}).get(kind) if cfg.get("market_filter") else None
    bars = cfg.get("bt_bars", 365)
    hold = cfg.get("bt_hold", 40)
    min_score = cfg["min_score"]
    side = cfg.get("side", "buy")

    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 120:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    warmup = 60
    start = max(warmup, n - bars)

    trades = []
    i = start
    while i < n - 1:
        sub = df.iloc[max(0, i - 219):i + 1]
        r = analyze(sub, tp_method=cfg.get("tp_method", "fib"))
        if not r:
            i += 1
            continue
        is_buy = r["score"] > 0
        ok_side = (side == "both") or (side == "buy" and is_buy) or (side == "sell" and not is_buy)
        # فلتر اتجاه السوق: امنع الشراء في سوق هابط والبيع في سوق صاعد (تاريخياً)
        if ok_side and reg is not None:
            mb = regime_bullish_at(reg, df["date"].iloc[i])
            if mb is not None and ((is_buy and not mb) or (not is_buy and mb)):
                ok_side = False
        # فلتر VWAP المثبّت: للشراء يجب أن يكون السعر فوق VWAP (والعكس للبيع)
        if ok_side and (cfg.get("vwap_w") or cfg.get("vwap_m") or cfg.get("vwap_d")):
            pnow = r["price"]
            if cfg.get("vwap_d"):                     # يومي — ذو معنى على 4h/1h فقط
                vd = anchored_vwap_last(sub, "D")
                if vd is not None and ((is_buy and pnow < vd) or (not is_buy and pnow > vd)):
                    ok_side = False
            if ok_side and cfg.get("vwap_w"):
                vw = anchored_vwap_last(sub, "W")
                if vw is not None and ((is_buy and pnow < vw) or (not is_buy and pnow > vw)):
                    ok_side = False
            if ok_side and cfg.get("vwap_m"):
                vm = anchored_vwap_last(sub, "M")
                if vm is not None and ((is_buy and pnow < vm) or (not is_buy and pnow > vm)):
                    ok_side = False
        # فلتر العرض/الطلب: ندخل فقط إن كان السعر عند منطقة طلب/عرض طازجة
        if ok_side and cfg.get("sd"):
            dirn = 1 if is_buy else -1
            sdok = supply_demand_ok(sub, float(r.get("atr") or 0.0), dirn)
            if sdok is False:
                ok_side = False
        if ok_side and abs(r["score"]) >= min_score:
            direction = 1 if is_buy else -1
            entry = r["price"]
            tps = [t["price"] for t in (r.get("targets") or [])]
            cost = cfg.get("cost", 0.0)
            simA = _simulate_trade(df, i, entry, r["stop"], tps, direction, hold, manage=False, cost=cost)
            simB = _simulate_trade(df, i, entry, r["stop"], tps, direction, hold, manage=True, cost=cost)
            if simA and simB:
                trades.append({
                    "symbol": sym, "kind": kind, "side": "buy" if is_buy else "sell",
                    "bar": i, "date": str(df["date"].iloc[i])[:10],
                    "score": r["score"], "entry": entry, "stop": r["stop"],
                    "R_plain": round(simA[0], 3), "out_plain": simA[1],
                    "R_managed": round(simB[0], 3), "out_managed": simB[1],
                })
                # تقدّم زمني بمقدار فترة الإمساك لتفادي صفقات متداخلة على نفس الرمز
                i += hold
                continue
        i += 1
    return trades


def backtest_symbol_rsi_cross(item, kind, cfg):
    """باك-تست استراتيجية الزخم RSI80 — مطابق لـ detect_rsi_cross_signal:
    دخول فور تجاوز RSI(21) عتبة التشبّع صعوداً، عند إغلاق الشمعة.
    الوقف = الدخول − (مضاعف × ATR14)، والأهداف بامتدادات فيبوناتشي
    (1.272/1.618/2.618) على الموجة الصاعدة الأخيرة (~20 شمعة).
    يرجع صفقات بنفس صيغة باقي دوال الباك-تست (R_plain/R_managed...)."""
    sym = item["symbol"]
    hold = cfg.get("bt_hold", 40)
    bars = cfg.get("bt_bars", 365)
    cost = cfg.get("cost", 0.0)
    ob = cfg.get("rsi_ob", 80.0)
    stop_mult = cfg.get("bt_stop_mult", 1.5)

    df = _bt_fetch_df(sym, kind, cfg)
    if df is None or len(df) < 120:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    low = df["low"].values
    r = rsi(df["close"], 21).values
    a = atr(df, 14).values

    warmup = 30
    start = max(warmup, n - bars)
    trades = []
    i = start
    while i < n - 1:
        if np.isnan(r[i]) or np.isnan(r[i - 1]) or not (r[i] >= ob and r[i - 1] < ob):
            i += 1
            continue
        entry = float(close[i])
        atrv = a[i] if not np.isnan(a[i]) else entry * 0.02
        lo_win = float(np.min(low[max(0, i - 20):i + 1]))
        imp = entry - lo_win
        if imp <= 0:
            i += 1
            continue
        targets = [round(lo_win + ext * imp, 8) for ext in (1.272, 1.618, 2.618)]
        stop = entry - stop_mult * atrv
        if targets[0] <= entry or entry - stop <= 0:
            i += 1
            continue
        simA = _simulate_trade(df, i, entry, stop, targets, 1, hold, manage=False, cost=cost)
        simB = _simulate_trade(df, i, entry, stop, targets, 1, hold, manage=True, cost=cost)
        if simA and simB:
            trades.append({
                "symbol": sym, "kind": kind, "side": "buy", "bar": i,
                "date": str(df["date"].iloc[i])[:10], "score": round(float(r[i]), 1),
                "entry": entry, "stop": round(stop, 8),
                "R_plain": round(simA[0], 3), "out_plain": simA[1],
                "R_managed": round(simB[0], 3), "out_managed": simB[1],
            })
            i += hold
            continue
        i += 1
    return trades


def _stats(rs, outs):
    """يحسب مقاييس الأداء من قائمة عوائد R وقائمة النتائج."""
    n = len(rs)
    if n == 0:
        return None
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # أقصى تراجع وأطول سلسلة خسائر على منحنى رأس المال (بوحدات R)
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if x <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    stopped = sum(1 for o in outs if o in ("stop",))
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "expectancy": round(sum(rs) / n, 3),
        "total_R": round(sum(rs), 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "max_dd_R": round(max_dd, 1),
        "max_consec_losses": max_streak,
        "stopped_pct": round(stopped / n * 100, 1),
    }


def _print_stats(title, st):
    if not st:
        print(f"\n{title}: لا صفقات.")
        return
    print(f"\n{SEP}\n  {title}\n{SEP}")
    print(f"  عدد الصفقات        : {st['n']}")
    print(f"  نسبة الربح         : {st['win_rate']}%")
    print(f"  التوقّع (متوسط R)  : {st['expectancy']:+}  ← الأهم (>0 = رابح)")
    print(f"  إجمالي R           : {st['total_R']:+}")
    print(f"  معامل الربح        : {st['profit_factor']}  (>1 رابح، >1.5 جيد)")
    print(f"  متوسط الرابحة/الخاسرة: {st['avg_win']:+} / {st['avg_loss']:+}")
    print(f"  أقصى تراجع (R)     : -{st['max_dd_R']}")
    print(f"  أطول سلسلة خسائر   : {st['max_consec_losses']}")
    print(f"  نسبة ضرب الوقف     : {st['stopped_pct']}%")


def run_backtest(cfg, watchlist_path, out_dir):
    print("=" * 64)
    print("  الاختبار التاريخي (Backtest) — قياس أداء الاستراتيجية")
    print("=" * 64)
    parsed = parse_watchlist(watchlist_path)
    targets = []
    if cfg["assets"] in ("all", "stocks"):
        targets += [(it, "stock") for it in parsed["stocks"]]
    if cfg["assets"] in ("all", "crypto"):
        targets += [(it, "crypto") for it in parsed["crypto"]]
    print(f"رموز للاختبار: {len(targets)} | الإطار: {cfg['timeframe']} | "
          f"شموع: {cfg.get('bt_bars')} | إمساك: {cfg.get('bt_hold')} شمعة | "
          f"الحد الأدنى للدرجة: {cfg['min_score']}")
    if cfg.get("bt_offset"):
        print(f"🔭 تحقّق خارج العيّنة: مُستبعَد أحدث {cfg['bt_offset']} شمعة (اختبار فترة أقدم)")

    # بناء سلسلة اتجاه السوق التاريخية إن كان الفلتر مفعّلاً
    if cfg.get("market_filter"):
        reg = {}
        kinds = {kind for _, kind in targets}
        for k in kinds:
            reg[k] = build_regime_series(k)
        cfg["_regime"] = reg
        ok = [k for k, v in reg.items() if v is not None]
        print(f"🌐 فلتر اتجاه السوق: مفعّل (BTC/SPY) — جاهز لـ: {', '.join(ok) or 'لا شيء'}")
    if cfg.get("cost"):
        print(f"💸 تكلفة الصفقة (عمولة+انزلاق): {cfg['cost']*100:.2f}% ذهاباً وإياباً")
    vwf = [n for n, on in (("يومي", cfg.get("vwap_d")), ("أسبوعي", cfg.get("vwap_w")),
                           ("شهري", cfg.get("vwap_m"))) if on]
    if vwf:
        print(f"📈 فلتر VWAP المثبّت: مفعّل ({' + '.join(vwf)}) — شراء فوق VWAP فقط")
    if cfg.get("sd"):
        print("🟦 فلتر العرض/الطلب: مفعّل — دخول عند منطقة طازجة فقط")
    print()

    if cfg.get("trendwave"):
        bt_fn = backtest_symbol_trendwave
        _tb = cfg.get("trail_buf", 0.5); _ta = cfg.get("trail_arm", 1.0)
        if _tb == 0.25: _tb = 0.5
        if _ta == 0.0: _ta = 1.0
        print("🌟 استراتيجية مستقلة: trendwave (الإعداد الرابح الموحّد)")
        print(f"   RSI21<{cfg.get('rsi_os',20.0):.0f} → >{cfg.get('rsi_ob',80.0):.0f} → دخول سوقي مباشر + DCA فيبو")
        print(f"   🔎 فلتر اتجاه من فريم أعلى | خروج: وقف متحرك فقط ({_tb}×ATR، مؤجّل {_ta}×المخاطرة)")
    elif cfg.get("osob"):
        bt_fn = backtest_symbol_osob
        _direct = cfg.get("force_direct") or cfg.get("timeframe") in ("1d", "4h")
        _md = "دخول سوقي مباشر + DCA فيبو" if _direct else "سلّم دخول فيبوناتشي (1h/15m)"
        if cfg.get("no_div"):
            _md += " | بدون خروج دايفرجنس"
        print(f"🎯 الاستراتيجية: دخول فيبوناتشي بعد الموجة + خروج بالدايفرجنس (osob)")
        print(f"   RSI21<{cfg.get('rsi_os',20.0):.0f} → >{cfg.get('rsi_ob',80.0):.0f} → {_md}")
        _ex = "وقف متحرك فقط" if cfg.get("no_div") else "وقف متحرك + خروج دايفرجنس"
        print(f"   🪜 DCA على 0.382/0.5/0.618/0.786 | الخروج: {_ex}")
        _tf = ("مفعّل (متوسط 200 على فريم أعلى)" if cfg.get("htf_trend")
               else "مفعّل (فوق متوسط 200)") if cfg.get("trend_filter") else "معطّل"
        print(f"   🔎 فلتر الاتجاه: {_tf} | مسافة الوقف: {cfg.get('trail_buf',0.25)}×ATR | "
              f"تفعيل التتبّع بعد: {cfg.get('trail_arm',0.0)}×المخاطرة")
    elif cfg.get("os_multi"):
        bt_fn = backtest_symbol_os_multi
        nt = int(cfg.get("os_touches", 2))
        print(f"🎯 الاستراتيجية: تكرار التشبّع البيعي بعد موجة شرائية مكتملة (os-multi)")
        print(f"   موجة RSI21>{cfg.get('rsi_ob',80.0):.0f} تكتمل → ثم {nt} نزولات تحت {cfg.get('rsi_os',20.0):.0f} → دخول مباشر")
        print(f"   🪜 الفيبو: أعمق قاع→أعلى قمة | DCA 0.382/0.5/0.618/0.786 | أهداف 1.272/1.618/2.0")
    elif cfg.get("donchian"):
        bt_fn = backtest_symbol_donchian
        print(f"🐢 الاستراتيجية: اختراق قناة Donchian (Turtle) — اختراق {int(cfg.get('don_entry',20))} قمة + فلتر متوسط 200")
        print(f"   خروج: إغلاق دون {int(cfg.get('don_exit',10))} قاع | وقف 2×ATR")
    elif cfg.get("ema_cross"):
        bt_fn = backtest_symbol_ema_cross
        print(f"📈 الاستراتيجية: تقاطع المتوسطات EMA{int(cfg.get('ema_fast',50))}/{int(cfg.get('ema_slow',200))} (Golden Cross)")
        print("   خروج: التقاطع العكسي | وقف 2×ATR")
    elif cfg.get("rsi2"):
        bt_fn = backtest_symbol_rsi2
        print(f"🔄 الاستراتيجية: ارتداد RSI(2) Connors — فوق متوسط 200 + RSI2<{cfg.get('rsi2_buy',10.0):.0f}")
        print("   خروج: إغلاق فوق متوسط 5 | وقف 2.5×ATR")
    elif cfg.get("rsi_cross"):
        bt_fn = backtest_symbol_rsi_cross
        print(f"🎯 الاستراتيجية: زخم RSI80 (تجاوز RSI21 عتبة {cfg.get('rsi_ob',80.0):.0f} صعوداً → دخول)")
        print(f"   وقف = {cfg.get('bt_stop_mult',1.5)}×ATR | أهداف امتدادات فيبو 1.272/1.618/2.618")
    elif cfg.get("strategy") == "reversal":
        bt_fn = backtest_symbol_reversal
        print("🎯 الاستراتيجية: انعكاس زخمي (RSI21<20 → >80 → قاع أعلى → دخول)")
    else:
        bt_fn = backtest_symbol
    if cfg.get("strategy") == "reversal" and not cfg.get("rsi_cross"):
        if cfg.get("ma200_ob"):
            if cfg.get("timeframe") == "1h":
                print("   ➕ شرط: إغلاق فوق متوسط 200 (على 4h) عند التشبّع الشرائي")
            else:
                print("   ⚠️ شرط المتوسط 200 يُطبَّق على 1h فقط — مُتجاهَل هنا")
        if cfg.get("dca_fib"):
            print("   🪜 الدخول: مباشر عند التأكيد + DCA على فيبو 0.382/0.5/0.618/0.786")

    all_trades = []
    done = 0
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = {ex.submit(bt_fn, it, kind, cfg): it for it, kind in targets}
        for fut in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  ... اختُبر {done}/{len(targets)} رمز")
            try:
                all_trades.extend(fut.result() or [])
            except Exception:
                pass

    if not all_trades:
        print("\n⚠️ لم تُفتح أي صفقة افتراضية. جرّب خفض --min-score أو تحقّق من البيانات.")
        return

    rA = [t["R_plain"] for t in all_trades]
    oA = [t["out_plain"] for t in all_trades]
    rB = [t["R_managed"] for t in all_trades]
    oB = [t["out_managed"] for t in all_trades]
    stA = _stats(rA, oA)
    stB = _stats(rB, oB)

    _print_stats("بدون إدارة (مسك حتى الهدف/الوقف)", stA)
    _print_stats("مع إدارة (جني 50% عند الهدف الأول + وقف عند الدخول)", stB)

    # خلاصة المقارنة
    if stA and stB:
        print(f"\n{SEP}\n  الخلاصة\n{SEP}")
        better = "مع الإدارة" if stB["expectancy"] >= stA["expectancy"] else "بدون إدارة"
        print(f"  التوقّع: بدون={stA['expectancy']:+} مقابل إدارة={stB['expectancy']:+}  → الأفضل: {better}")
        print(f"  أقصى تراجع: بدون=-{stA['max_dd_R']}R مقابل إدارة=-{stB['max_dd_R']}R")
        verdict = "النظام رابح إحصائياً ✅" if max(stA["expectancy"], stB["expectancy"]) > 0 \
            else "النظام خاسر إحصائياً ❌ — يحتاج تعديلاً قبل الاعتماد عليه"
        print(f"  الحكم: {verdict}")

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(out_dir, f"backtest_{ts}.csv")
    pd.DataFrame(all_trades).to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n✅ حُفظت كل الصفقات الافتراضية ({len(all_trades)}): {csv_path}")
    print("\n⚠️ نتائج تاريخية افتراضية — لا تضمن الأداء المستقبلي. تحليل تعليمي فقط.")


# ======================================================================
#  7.5) تحقّق Walk-Forward (اختبار خارج العيّنة المتدرّج ضد الـoverfitting)
# ======================================================================
def _wf_lbl(key):
    """تسمية مقروءة لمفتاح الإعداد (tuple أو نص)."""
    if isinstance(key, tuple):
        return "×".join(str(x) for x in key)
    return str(key)


def _wf_strategy_spec(cfg):
    """يرجع (الاسم، دالة الباك-تست، شبكة الإعدادات). كل عنصر شبكة = (مفتاح, dict معاملات).
    الإعداد الأول هو الافتراضي (للمقارنة in-sample والاحتياط)."""
    if cfg.get("donchian"):
        grid = [(("don", 20, 10), {"don_entry": 20, "don_exit": 10}),
                (("don", 55, 20), {"don_entry": 55, "don_exit": 20}),
                (("don", 20, 5),  {"don_entry": 20, "don_exit": 5})]
        return "Donchian (Turtle)", backtest_symbol_donchian, grid
    if cfg.get("ema_cross"):
        grid = [(("ema", 50, 200), {"ema_fast": 50, "ema_slow": 200}),
                (("ema", 20, 100), {"ema_fast": 20, "ema_slow": 100}),
                (("ema", 20, 50),  {"ema_fast": 20, "ema_slow": 50})]
        return "EMA Cross (Golden)", backtest_symbol_ema_cross, grid
    if cfg.get("rsi2"):
        grid = [(("rsi2", 10), {"rsi2_buy": 10.0}),
                (("rsi2", 5),  {"rsi2_buy": 5.0}),
                (("rsi2", 15), {"rsi2_buy": 15.0})]
        return "RSI(2) Connors", backtest_symbol_rsi2, grid
    # الافتراضي: trendwave/osob — شبكة الوقف المتحرك
    grid = [((b, a), {"trail_buf": b, "trail_arm": a})
            for b in (0.25, 0.5, 0.75) for a in (0.0, 1.0)]
    fn = backtest_symbol_trendwave if cfg.get("trendwave") else backtest_symbol_osob
    return ("trendwave" if cfg.get("trendwave") else "osob"), fn, grid


def _wf_expectancy(trades):
    rs = [t["R_managed"] for t in trades]
    return (sum(rs) / len(rs)) if rs else None


def _walkforward_oos(trades_by_combo, folds, default_key, min_is=20):
    """النواة القابلة للاختبار: تقسّم الزمن إلى نوافذ، وفي كل نافذة OOS تختار
    الإعداد صاحب أعلى توقّع على IS (كل ما قبلها) ثم تجمع صفقاته في OOS فقط.

    trades_by_combo: dict[(buf,arm) -> list[trade]] حيث كل trade فيه
      'date' (YYYY-MM-DD) و'R_managed' و'out_managed'.
    يرجع (oos_all, fold_rows)."""
    all_dates = [pd.Timestamp(t["date"]) for c in trades_by_combo.values()
                 for t in c if t.get("date")]
    if not all_dates:
        return [], []
    dmin, dmax = min(all_dates), max(all_dates)
    span = (dmax - dmin)
    bounds = [dmin + span * i / (folds + 1) for i in range(folds + 2)]

    oos_all, fold_rows = [], []
    for fi in range(1, folds + 1):
        oos_start, oos_end = bounds[fi], bounds[fi + 1]
        last = (fi == folds)
        best_key, best_exp = None, None
        for key, tr in trades_by_combo.items():
            is_tr = [t for t in tr if pd.Timestamp(t["date"]) < oos_start]
            if len(is_tr) < min_is:
                continue
            e = _wf_expectancy(is_tr)
            if e is not None and (best_exp is None or e > best_exp):
                best_exp, best_key = e, key
        if best_key is None:
            best_key = default_key
        chosen = trades_by_combo.get(best_key, [])
        oos = [t for t in chosen if oos_start <= pd.Timestamp(t["date"])
               and (pd.Timestamp(t["date"]) <= oos_end if last
                    else pd.Timestamp(t["date"]) < oos_end)]
        for t in oos:
            tt = dict(t)
            tt["wf_fold"] = fi
            tt["wf_combo"] = _wf_lbl(best_key)
            oos_all.append(tt)
        st = _stats([t["R_managed"] for t in oos], [t["out_managed"] for t in oos])
        fold_rows.append({
            "fold": fi, "from": str(oos_start)[:10], "to": str(oos_end)[:10],
            "combo": _wf_lbl(best_key),
            "n": st["n"] if st else 0,
            "expectancy": st["expectancy"] if st else None,
            "pf": st["profit_factor"] if st else None,
        })
    return oos_all, fold_rows


def run_walkforward(cfg, watchlist_path, out_dir):
    """تحقّق walk-forward: لكل نافذة اختبار (OOS) نختار أفضل إعداد من البيانات
    السابقة لها (IS) ثم نطبّقه على النافذة الجديدة فقط. النتيجة المجمّعة على
    كل نوافذ OOS هي الحكم الصادق خارج العيّنة ضد الـoverfitting."""
    print("=" * 64)
    print("  تحقّق Walk-Forward — اختبار خارج العيّنة المتدرّج")
    print("=" * 64)
    name, bt_fn, grid = _wf_strategy_spec(cfg)
    parsed = parse_watchlist(watchlist_path)
    targets = []
    if cfg["assets"] in ("all", "stocks"):
        targets += [(it, "stock") for it in parsed["stocks"]]
    if cfg["assets"] in ("all", "crypto"):
        targets += [(it, "crypto") for it in parsed["crypto"]]
    folds = int(cfg.get("wf_folds", 5))
    default_key = grid[0][0]
    print(f"الاستراتيجية: {name} | رموز: {len(targets)} | الإطار: {cfg['timeframe']} | "
          f"شموع: {cfg.get('bt_bars')} | نوافذ OOS: {folds} | إعدادات الشبكة: {len(grid)}")
    print("الشبكة: " + ", ".join(_wf_lbl(k) for k, _ in grid))
    print()

    # كاش مشترك: نجلب بيانات كل رمز مرة واحدة (الإعداد الأول) ونعيد استخدامها
    # في باقي الإعدادات — يلغي الجلب المكرّر ويتفادى خنق المصدر (rate-limit).
    cfg["_df_cache"] = {}
    trades_by_combo = {}
    for gi, (key, params) in enumerate(grid, 1):
        c = dict(cfg)                     # يشارك نفس مرجع _df_cache
        c.update(params)
        comb = []
        with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
            futs = [ex.submit(bt_fn, it, kind, c) for it, kind in targets]
            for fut in as_completed(futs):
                try:
                    comb.extend(fut.result() or [])
                except Exception:
                    pass
        trades_by_combo[key] = comb
        cached = len(cfg["_df_cache"])
        print(f"  [{gi}/{len(grid)}] إعداد {_wf_lbl(key)}: {len(comb)} صفقة"
              + (f"  (بيانات مُخزّنة لـ {cached} رمز)" if gi == 1 else ""))

    # تحذير: عدد صفقات ضئيل ⇒ البيانات لم تُجمَع (غالباً خنق المصدر) لا حكم على الاستراتيجية
    total_def = len(trades_by_combo.get(default_key, []))
    if total_def < 30:
        print(f"\n⚠️ تحذير: إجمالي صفقات الإعداد الافتراضي = {total_def} فقط — قليل جداً!")
        print("   إمّا أن البيانات لم تُجلَب (خنق المصدر) أو أن الإشارة نادرة على هذا الإطار.")
        print("   جرّب باك-تست عادياً أو إطاراً أصغر/إشارة أكثر تكراراً.")

    oos_all, fold_rows = _walkforward_oos(trades_by_combo, folds, default_key)
    if not fold_rows:
        print("\n⚠️ لا صفقات كافية لبناء النوافذ.")
        return

    print(f"\n{SEP}\n  نتائج كل نافذة (OOS فقط)\n{SEP}")
    for r in fold_rows:
        print(f"  نافذة {r['fold']} [{r['from']}→{r['to']}] إعداد {r['combo']}: "
              f"{r['n']} صفقة، توقّع {r['expectancy']}، PF {r['pf']}")

    st_oos = _stats([t["R_managed"] for t in oos_all], [t["out_managed"] for t in oos_all])
    _print_stats(f"الإجمالي خارج العيّنة — {name} (الحكم الصادق)", st_oos)

    full_def = trades_by_combo.get(default_key, [])
    st_is = _stats([t["R_managed"] for t in full_def], [t["out_managed"] for t in full_def])
    if st_is and st_oos:
        n_pos = sum(1 for r in fold_rows if (r["expectancy"] or 0) > 0)
        print(f"\n{SEP}\n  فجوة الباك-تست مقابل خارج العيّنة\n{SEP}")
        print(f"  in-sample (كامل، {_wf_lbl(default_key)}): توقّع {st_is['expectancy']:+}، PF {st_is['profit_factor']}")
        print(f"  out-of-sample (walk-forward): توقّع {st_oos['expectancy']:+}، PF {st_oos['profit_factor']}")
        print(f"  نوافذ موجبة: {n_pos}/{len(fold_rows)}")
        ok = st_oos["expectancy"] > 0 and n_pos >= max(1, (len(fold_rows) + 1) // 2)
        print(f"  الحكم: " + ("رابح خارج العيّنة ✅ — الإيدج صامد عبر الزمن" if ok
                              else "غير صامد خارج العيّنة ❌ — الأداء غالباً overfitting/حظّ فترة"))

    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    if oos_all:
        p = os.path.join(out_dir, f"walkforward_{ts}.csv")
        pd.DataFrame(oos_all).to_csv(p, index=False, encoding="utf-8-sig")
        print(f"\n✅ حُفظت صفقات OOS ({len(oos_all)}): {p}")
    print("\n⚠️ نتائج تاريخية افتراضية — لا تضمن المستقبل. تحليل تعليمي فقط.")


# ======================================================================
#  8) التنبيه الحيّ لاستراتيجية الانعكاس (يرسل الإشارة فور إغلاق الشمعة)
# ======================================================================
def reversal_label(cfg):
    """اسم الاستراتيجية لعرضه في رسالة تيليجرام للتمييز."""
    tf = cfg.get("timeframe", "?")
    if cfg.get("trendwave"):
        return f"trendwave · {tf}"
    if cfg.get("rsi_cross"):
        return f"RSI{int(cfg.get('rsi_ob', 80.0))} · {tf}"
    return f"انعكاس {tf} " + ("DCA" if cfg.get("dca_fib") else "كلاسيكي")


def detect_rsi_cross_signal(df, cfg):
    """إشارة شراء بالزخم: تتحقق فور تجاوز RSI(21) خط الـ80 صعوداً
    عند آخر شمعة *مغلقة* في df (المُستبعَد منه الشمعة الجارية).
    الوقف يعتمد على ATR، والأهداف بمضاعفات المخاطرة، مع مستويات
    دخول فيبوناتشي على الموجة الصاعدة الأخيرة. يرجع dict أو None."""
    ob = cfg.get("rsi_ob", 80.0)
    if df is None or len(df) < 30:
        return None
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    low = df["low"].values
    r = rsi(df["close"], 21).values
    a = atr(df, 14).values
    i = n - 1
    if np.isnan(r[i]) or np.isnan(r[i - 1]):
        return None
    # تجاوز خط 80 صعوداً: الشمعة المغلقة ≥ 80 والسابقة < 80
    if not (r[i] >= ob and r[i - 1] < ob):
        return None
    entry = float(close[i])
    atrv = a[i] if not np.isnan(a[i]) else entry * 0.02
    stop = float(entry - 1.5 * atrv)
    # الموجة الصاعدة الأخيرة: قاع آخر ~20 شمعة → الدخول
    lo_win = float(np.min(low[max(0, i - 20):i + 1]))
    imp = entry - lo_win
    if imp > 0:
        # الأهداف بامتدادات فيبوناتشي على الموجة (القاع + نسبة × المدى)
        targets = [round(lo_win + ext * imp, 8) for ext in (1.272, 1.618, 2.618)]
        # مستويات دخول فيبوناتشي على ارتدادات نفس الموجة
        fib_e = [round(entry - rr * imp, 8) for rr in (0.236, 0.382, 0.5)]
    else:
        # احتياط (موجة غير صالحة): أهداف بمضاعفات المخاطرة
        risk = entry - stop
        targets = [round(entry + m * risk, 8) for m in (1.0, 2.0, 3.0)]
        fib_e = []
    # الوقف يجب أن يقع تحت أعمق مستوى دخول مُدرَج، وإلا فالسلّم بلا معنى
    # (يُملأ آخر دخول بعد ضرب الوقف). نخفض الوقف عند الحاجة بهامش 0.5×ATR.
    if fib_e:
        stop = min(stop, round(min(fib_e) - 0.5 * atrv, 8))
    return {"entry": entry, "stop": stop, "targets": targets,
            "dca": None, "fib_entries": fib_e, "rsi": round(float(r[i]), 1)}


def detect_reversal_signal(df, cfg):
    """يكشف إن كانت إشارة دخول انعكاسية تتحقق عند آخر شمعة *مغلقة* في df.
    df يجب أن يكون قد استُبعدت منه الشمعة الجارية. يرجع dict أو None."""
    os_th = cfg.get("rsi_os", 20.0)
    ob_th = cfg.get("rsi_ob", 80.0)
    dca_fib = cfg.get("dca_fib")
    if df is None or len(df) < 60:
        return None
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    rsi21 = rsi(df["close"], 21).values
    atrs = atr(df, 14).values
    ma200_ob = cfg.get("ma200_ob") and cfg.get("timeframe") == "1h"
    if ma200_ob:
        s4 = df.set_index("date")["close"].resample("4h").last().dropna()
        sma4 = s4.rolling(200).mean().dropna().reset_index()
        sma4.columns = ["date", "ma"]
        sma200 = (pd.merge_asof(df[["date"]].copy(), sma4, on="date")["ma"].values
                  if len(sma4) else np.full(n, np.nan))
    else:
        sma200 = None

    state = 0
    ref_low = peak = pull_low = None
    last_sig = None
    i = 1
    while i < n:
        r = rsi21[i]
        if np.isnan(r):
            i += 1
            continue
        if state == 0:
            if r < os_th:
                state = 1
                ref_low = low[i]
        elif state == 1:
            ref_low = min(ref_low, low[i])
            if r > ob_th:
                ok = True
                if ma200_ob:
                    m = sma200[i]
                    ok = (not np.isnan(m)) and close[i] > m
                if ok and dca_fib:
                    peak = high[i]
                    imp = peak - ref_low
                    atrv = atrs[i] if not np.isnan(atrs[i]) else close[i] * 0.02
                    if imp > 0:
                        entry = float(close[i])
                        stp = float(ref_low - 0.5 * atrv)
                        dca = [round(peak - rr * imp, 8) for rr in (0.382, 0.5, 0.618, 0.786)]
                        # الوقف تحت أعمق مستوى دخول مُدرَج
                        stp = min(stp, round(min(dca) - 0.5 * atrv, 8))
                        tps = [round(ref_low + mm * imp, 8) for mm in (1.272, 1.618, 2.0)]
                        last_sig = (i, {"entry": entry, "stop": stp, "targets": tps, "dca": dca})
                    state = 0
                    ref_low = peak = pull_low = None
                elif ok:
                    state = 2
                    peak = high[i]
                    pull_low = None
                else:
                    state = 0
                    ref_low = None
        elif state == 2:
            if high[i] > peak:
                peak = high[i]
                pull_low = None
            elif pull_low is None or low[i] < pull_low:
                pull_low = low[i]
            atrv = atrs[i] if not np.isnan(atrs[i]) else close[i] * 0.02
            turn_up = pull_low is not None and close[i] > high[i - 1] and close[i] < peak
            if turn_up:
                imp = peak - ref_low
                retr = (peak - pull_low) / imp if imp > 0 else 0.0
                if cfg.get("fib_lo", 0.382) <= retr <= cfg.get("fib_hi", 0.786):
                    entry = float(close[i])
                    stp = float(pull_low - 0.5 * atrv)
                    corr = peak - pull_low
                    tps = [round(pull_low + mm * corr, 8) for mm in (1.272, 1.618, 2.618)]
                    # مستويات الدخول على فيبوناتشي (ارتدادات الموجة الصاعدة ref_low→peak)
                    fib_e = ([round(peak - rr * imp, 8) for rr in (0.382, 0.5, 0.618, 0.786)]
                             if imp > 0 else [])
                    # الوقف يجب أن يقع تحت أعمق مستوى دخول مُدرَج، وإلا تُملأ
                    # الدخولات الأعمق بعد ضرب الوقف (سلّم بلا معنى).
                    if fib_e:
                        stp = min(stp, round(min(fib_e) - 0.5 * atrv, 8))
                    last_sig = (i, {"entry": entry, "stop": stp, "targets": tps,
                                    "dca": None, "retr": round(retr, 3),
                                    "fib_entries": fib_e})
                    state = 0
                    ref_low = peak = pull_low = None
            if state == 2 and low[i] < ref_low:
                state = 0
                ref_low = peak = pull_low = None
        i += 1

    if last_sig and last_sig[0] == n - 1:     # الإشارة عند آخر شمعة مغلقة فقط
        return last_sig[1]
    return None


def _fib_dca_ladder(entry, peak, ref_low, imp):
    """سلّم دخول DCA بارتدادات فيبوناتشي لموجة الدفع (ref_low→peak)، تحت الدخول فقط.
    المستويات الأساسية: 0.382/0.5/0.618/0.786/0.886 من القمة. إن وقع الدخول عميقاً
    (أقل من 3 مستويات تحته) يُكمَّل السلّم بارتدادات فيبو بين الدخول وقاع الموجة،
    لضمان سلّم متعدّد المستويات دائماً بدل قيمة واحدة. يرجع قائمة تنازلية (الأقرب أولاً)."""
    if imp <= 0:
        return []
    fib = [round(peak - rr * imp, 8) for rr in (0.382, 0.5, 0.618, 0.786, 0.886)]
    dca = [x for x in fib if x < entry]                 # ارتدادات الموجة تحت الدخول
    if len(dca) < 3:                                    # دخول عميق → أكمل السلّم
        span = entry - ref_low
        if span > 0:
            for rr in (0.236, 0.382, 0.5, 0.618, 0.786):
                lvl = round(entry - rr * span, 8)
                if ref_low < lvl < entry:
                    dca.append(lvl)
    return sorted(set(dca), reverse=True)[:5]


def detect_trendwave_signal(df, cfg):
    """إشارة دخول حيّة لاستراتيجية trendwave عند آخر شمعة *مغلقة*:
    RSI(21)<20 ثم >80 (موجة دفع) → بعد خفوت الموجة *ننتظر تشكُّل قاع محوري مؤكَّد*
    (شمعة أدنى من شمعتين على كل جانب) ثم نُصدر الإشارة، بشرط الإغلاق فوق متوسط 200
    على *فريم أعلى* (1h→4h، 15m→1h، 4h/1d→يومي).
    لا دخول مباشر: سلّم دخول من القاع حتى ارتداد 0.236 للحركة (القمة→القاع)،
    والوقف تحت القاع المؤكَّد، والأهداف ارتداد فيبو تصحيحي 0.382 ثم 0.5.
    يرجع dict أو None."""
    os_th = cfg.get("rsi_os", 20.0)
    ob_th = cfg.get("rsi_ob", 80.0)
    if df is None or len(df) < 60:
        return None
    df = df.reset_index(drop=True)
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    r = rsi(df["close"], 21).values
    a = atr(df, 14).values
    # فلتر الاتجاه من فريم أعلى
    htf = {"1h": "4h", "15m": "1h", "4h": "1D", "1d": "1D"}.get(cfg.get("timeframe"), "1D")
    s = df.set_index("date")["close"].resample(htf).last().dropna()
    sm = s.rolling(200).mean().dropna().reset_index()
    sm.columns = ["date", "ma"]
    sma = (pd.merge_asof(df[["date"]].copy(), sm, on="date")["ma"].values
           if len(sm) else np.full(n, np.nan))

    # آلة الحالات: نلتقط آخر موجة دفع «خَفَتت» (RSI تجاوز 80 ثم عاد تحته).
    # خلافاً للنسخة القديمة، لا ندخل فور خفوت الموجة، بل ننتظر تشكُّل القاع.
    state = 0
    ref_low = peak = None
    peak_idx = None
    faded = None                                # (peak_idx, ref_low, peak) لآخر موجة خفتت
    for i in range(1, n):
        ri = r[i]
        if np.isnan(ri):
            continue
        if state == 0:
            if ri < os_th:
                state = 1; ref_low = low[i]; peak = high[i]; peak_idx = i
        elif state == 1:
            if low[i] < ref_low:
                ref_low = low[i]
            if high[i] > peak:
                peak = high[i]; peak_idx = i
            if ri > ob_th:
                state = 2
        elif state == 2:
            if high[i] > peak:
                peak = high[i]; peak_idx = i
            if ri < ob_th:                     # خفوت الموجة → نبدأ انتظار القاع
                faded = (peak_idx, ref_low, peak)
                state = 0; ref_low = peak = None; peak_idx = None

    if not faded:
        return None
    pk_idx, rl, pk = faded

    # ننتظر تشكُّل «قاع محوري مؤكَّد» بعد القمة: شمعة قاعها أدنى من شمعتين على كل جانب.
    # التأكيد يحتاج شمعتين بعد القاع، فالقاع المؤكَّد حديثاً يقع عند المؤشر n-3.
    piv = n - 3
    if piv <= pk_idx + 1:                       # لا قاع مؤكَّد بعد القمة بعد
        return None
    seg = low[piv - 2:piv + 3]                  # نافذة 5 شموع حول القاع
    if len(seg) < 5 or float(low[piv]) != float(np.nanmin(seg)):
        return None                             # الشمعة piv ليست قاعاً محورياً مؤكَّداً

    tr_low = float(low[piv])
    m = sma[n - 1]
    if np.isnan(m) or close[n - 1] <= m:        # فلتر الاتجاه عند شمعة الإشارة
        return None
    imp = pk - rl
    drop = pk - tr_low                          # حركة التصحيح (القمة → القاع)
    if imp <= 0 or drop <= 0 or tr_low >= pk:
        return None

    atrv = a[n - 1] if not np.isnan(a[n - 1]) else tr_low * 0.02
    c = float(close[n - 1])
    # سلّم دخول فيبو من السعر الحالي نزولاً نحو القاع المؤكَّد (شراء على إعادة اختبار
    # القاع) — لا دخول مباشر مفرد. كلها فوق الوقف وتحت السعر الحالي.
    span = c - tr_low if c > tr_low else 0.5 * atrv
    dca = sorted({round(c - rr * span, 8) for rr in (0.236, 0.5, 0.786)}, reverse=True)
    if not dca:
        return None
    avg_entry = round(sum(dca) / len(dca), 8)
    stop = round(tr_low - 0.5 * atrv, 8)        # الوقف تحت القاع المؤكَّد
    # الأهداف = ارتداد فيبو التصحيحي للحركة (القمة → القاع): 0.382 ثم 0.5
    targets = [round(tr_low + 0.382 * drop, 8), round(tr_low + 0.5 * drop, 8)]
    if targets[0] <= avg_entry or stop >= avg_entry:
        return None
    return {"entry": avg_entry, "stop": stop, "dca": dca, "targets": targets,
            "rsi": round(float(r[n - 1]), 1), "peak": round(pk, 8),
            "trough": round(tr_low, 8)}


TRACK_FILE = "tracked_signals.json"

# إعدادات إدارة الصفقة الحيّة (مطابقة لإعداد trendwave الرابح في الباك-تست)
TRAIL_BUF = 0.5     # مسافة الوقف المتحرك تحت القاع المحوري = 0.5×ATR
TRAIL_ARM = 1.0     # لا يُفعَّل التتبّع إلا بعد ربح عائم ≥ 1×المخاطرة


def _tp_split_for(n_targets):
    """نِسَب الإغلاق الجزئي المقترَحة لكل هدف (جني ربح تدريجي).
    50% عند الهدف الأول ثم توزيع الباقي بالتساوي على بقية الأهداف."""
    if n_targets <= 0:
        return []
    if n_targets == 1:
        return [100]
    rest = round(50 / (n_targets - 1))
    split = [50] + [rest] * (n_targets - 1)
    split[-1] = 100 - sum(split[:-1])      # ضمان أن المجموع = 100%
    return split


def track_signal(sig, label, cfg, message_id, path=TRACK_FILE):
    """يخزّن إشارة مُرسَلة لمتابعتها لاحقاً (أهداف/وقف/وقف متحرك) والرد على رسالتها.
    يُنظّف الإشارات الأقدم من 14 يوماً."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    data = {k: v for k, v in data.items()
            if isinstance(v, dict) and v.get("created", "") >= cutoff}
    key = f"{label}|{sig['symbol']}|{sig.get('bar_ts')}"
    is_tw = bool(cfg.get("trendwave"))
    # جني الربح الجزئي + الوقف للتعادل: لعائلة الانعكاس فقط (trendwave يُدار بالوقف المتحرك)
    tp_split = None if is_tw else _tp_split_for(len(sig["targets"]))
    data[key] = {
        "symbol": sig["symbol"],
        "label": label,
        "timeframe": cfg.get("timeframe"),
        "message_id": message_id,
        "entry": sig["entry"],
        "stop": sig["stop"],            # الوقف الابتدائي (يبقى ثابتاً للمرجع)
        "init_stop": sig["stop"],
        "cur_stop": sig["stop"],        # الوقف الجاري (يرتفع مع التتبّع)
        "last_alert_stop": sig["stop"], # آخر مستوى وقف أُبلغ عنه (منع التكرار)
        "armed": False,                 # هل تفعّل الوقف المتحرك (بعد 1×المخاطرة)؟
        "targets": sig["targets"],
        "tp_split": tp_split,
        "is_trendwave": is_tw,
        "breakeven_done": False,
        "bar_ts": sig.get("bar_ts"),
        "last_bar": sig.get("bar_ts"),  # آخر شمعة مغلقة عولِجت
        "hits": [],
        "stopped": False,
        "hi_seen": sig["entry"],
        "lo_seen": sig["entry"],
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _advance_trade(df, tr):
    """يُحدّث حالة صفقة مُتابَعة بناءً على الشموع المُغلقة الجديدة فقط.
    يطبّق: (1) جني ربح جزئي عند كل هدف + نقل الوقف للتعادل بعد الهدف الأول
    (عائلة الانعكاس)، (2) وقف متحرك يرتفع تحت كل قاع محوري مؤكَّد − 0.5×ATR
    بعد ربح عائم ≥ 1×المخاطرة (مطابق لإعداد trendwave الرابح).
    يُعدّل tr مباشرةً ويُرجع قائمة أحداث (نصوص جاهزة للإرسال). الترتيب الزمني محفوظ."""
    fmt = _fmt_price
    events = []
    df = df.reset_index(drop=True)
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    dates = df["date"]
    atr_arr = atr(df, 14).values

    entry = tr["entry"]
    init_stop = tr.get("init_stop", tr["stop"])
    risk = entry - init_stop
    if risk <= 0:
        return events
    targets = tr["targets"]
    tp_split = tr.get("tp_split")          # None لـ trendwave
    sym = tr["symbol"]

    # نقطة البداية: أول شمعة مغلقة تلَت آخر شمعة عولِجت
    try:
        last_bar = pd.Timestamp(tr.get("last_bar") or tr.get("bar_ts"))
    except Exception:
        last_bar = None
    if last_bar is not None:
        idxs = [i for i in range(len(df)) if dates.iloc[i] > last_bar]
    else:
        idxs = list(range(len(df)))
    if not idxs:
        return events

    cur_stop = tr.get("cur_stop", init_stop)
    armed = tr.get("armed", False)

    # ── إدارة trendwave: جني 50% عند الهدف الأول + رفع الوقف لمتوسط الدخول،
    #    ثم جني 50% المتبقية وإغلاق عند الهدف الثاني. (لا وقف متحرك ولا أهداف إضافية)
    if tr.get("is_trendwave"):
        for j in idxs:
            # (أ) ضرب الوقف أولاً — بالمستوى الجاري (الابتدائي قبل الهدف1، التعادل بعده)
            if low[j] <= cur_stop:
                if cur_stop >= entry:              # بعد الهدف1: خروج بلا خسارة على المتبقّي
                    events.append(f"➖ {sym} — خروج عند التعادل على المتبقّي (بعد جني 50%)\n"
                                  f"السعر: {fmt(cur_stop)}  (0.00%)")
                else:                              # قبل الهدف1: وقف خسارة كامل
                    events.append(f"🛑 {sym} — ضرب وقف الخسارة\n"
                                  f"السعر: {fmt(cur_stop)}")
                tr["stopped"] = True
                tr["cur_stop"] = cur_stop
                tr["last_bar"] = str(dates.iloc[j])
                tr["hi_seen"] = max(tr.get("hi_seen", entry), float(high[j]))
                tr["lo_seen"] = min(tr.get("lo_seen", entry), float(low[j]))
                return events

            # (ب) الهدف الأول → جني 50% + رفع الوقف لمتوسط الدخول (تعادل)
            if 1 not in tr["hits"] and high[j] >= targets[0]:
                tr["hits"].append(1)
                gain = ((targets[0] - entry) / entry * 100) if entry else 0.0
                cur_stop = entry                   # الوقف = متوسط الدخول
                events.append(f"🎯 {sym} — تحقق الهدف الأول ✅ — جني 50% ورفع الوقف "
                              f"لمتوسط الدخول (تعادل)\n"
                              f"السعر: {fmt(targets[0])}  (+{gain:.2f}%)")

            # (ج) الهدف الثاني → جني الـ50% المتبقية وإغلاق الصفقة
            if len(targets) > 1 and 1 in tr["hits"] and 2 not in tr["hits"] \
                    and high[j] >= targets[1]:
                tr["hits"].append(2)
                gain2 = ((targets[1] - entry) / entry * 100) if entry else 0.0
                events.append(f"🏁 {sym} — تحقق الهدف الثاني ✅✅ — جني 50% وإغلاق الصفقة\n"
                              f"السعر: {fmt(targets[1])}  (+{gain2:.2f}%)")
                tr["stopped"] = True
                tr["cur_stop"] = cur_stop
                tr["last_bar"] = str(dates.iloc[j])
                tr["hi_seen"] = max(tr.get("hi_seen", entry), float(high[j]))
                tr["lo_seen"] = min(tr.get("lo_seen", entry), float(low[j]))
                return events

            tr["hi_seen"] = max(tr.get("hi_seen", entry), float(high[j]))
            tr["lo_seen"] = min(tr.get("lo_seen", entry), float(low[j]))

        tr["cur_stop"] = cur_stop
        tr["last_bar"] = str(dates.iloc[idxs[-1]])
        # أبلغ مرّة واحدة عن رفع الوقف للتعادل بعد الهدف الأول
        eps = abs(cur_stop) * 1e-6
        if cur_stop > tr.get("last_alert_stop", init_stop) + eps:
            events.append(f"📈 {sym} — رُفع الوقف لمتوسط الدخول (تعادل)\n"
                          f"الوقف الجديد: {fmt(cur_stop)}")
            tr["last_alert_stop"] = cur_stop
        return events

    for j in idxs:
        # (أ) ضرب الوقف (بالمستوى السابق) أولاً — يُنهي الصفقة.
        #     يُفحص قبل أي رفع لهذه الشمعة حتى لا يُطبَّق التعادل/التتبّع على شمعته نفسها.
        if low[j] <= cur_stop:
            if cur_stop > entry:                   # خروج بربح عبر الوقف المتحرك
                g = (cur_stop - entry) / entry * 100 if entry else 0.0
                events.append(f"✅ {sym} — خروج بالوقف المتحرك (تأمين ربح)\n"
                              f"السعر: {fmt(cur_stop)}  (+{g:.2f}%)")
            elif cur_stop >= entry:                # خروج عند التعادل
                events.append(f"➖ {sym} — خروج عند التعادل\n"
                              f"السعر: {fmt(cur_stop)}  (0.00%)")
            else:                                  # وقف خسارة
                events.append(f"🛑 {sym} — ضرب وقف الخسارة\n"
                              f"السعر: {fmt(cur_stop)}")
            tr["stopped"] = True
            tr["cur_stop"] = cur_stop
            tr["last_bar"] = str(dates.iloc[j])
            tr["hi_seen"] = max(tr.get("hi_seen", entry), float(high[j]))
            tr["lo_seen"] = min(tr.get("lo_seen", entry), float(low[j]))
            return events

        # (ب) الهدف — **إغلاق كامل عند الهدف الأول لكل الاستراتيجيات** (بلا جني جزئي
        #     ولا أهداف متعددة ولا وقف متحرك). الصفقة: هدف أول أو وقف خسارة فقط.
        if 1 not in tr["hits"] and high[j] >= targets[0]:
            tr["hits"].append(1)
            gain = ((targets[0] - entry) / entry * 100) if entry else 0.0
            events.append(f"🎯 {sym} — تحقق الهدف الأول ✅ — أغلق الصفقة بالكامل\n"
                          f"السعر: {fmt(targets[0])}  (+{gain:.2f}%)")
            tr["stopped"] = True
            tr["cur_stop"] = cur_stop
            tr["last_bar"] = str(dates.iloc[j])
            tr["hi_seen"] = max(tr.get("hi_seen", entry), float(high[j]))
            tr["lo_seen"] = min(tr.get("lo_seen", entry), float(low[j]))
            return events

        tr["hi_seen"] = max(tr.get("hi_seen", entry), float(high[j]))
        tr["lo_seen"] = min(tr.get("lo_seen", entry), float(low[j]))

    tr["cur_stop"] = cur_stop
    tr["armed"] = armed
    tr["last_bar"] = str(dates.iloc[idxs[-1]])

    # (هـ) أبلغ عن رفع الوقف المتحرك مرة واحدة عند تغيّر مستواه فعلياً
    eps = abs(cur_stop) * 1e-6
    if cur_stop > tr.get("last_alert_stop", init_stop) + eps:
        locked = ((cur_stop - entry) / entry * 100) if entry else 0.0
        extra = (f"  (مؤمّن +{locked:.2f}%)" if cur_stop > entry else
                 "  (تعادل)" if abs(cur_stop - entry) <= eps else "")
        events.append(f"📈 {sym} — ارفع الوقف المتحرك\n"
                      f"الوقف الجديد: {fmt(cur_stop)}{extra}")
        tr["last_alert_stop"] = cur_stop

    return events


def monitor_tracked_signals(cfg, path=TRACK_FILE):
    """يتابع الإشارات المُرسَلة؛ يدير كل صفقة حيّاً (وقف متحرك + جني ربح جزئي)
    ويرسل كل حدث رداً على رسالة الصفقة الأصلية في تيليجرام (reply_to_message_id)."""
    token = cfg.get("tg_token") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = cfg.get("tg_chat") or os.environ.get("TELEGRAM_CHAT_ID")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        print("[متابعة] لا توجد إشارات مُتابَعة.")
        return
    if not isinstance(data, dict) or not data:
        print("[متابعة] لا توجد إشارات مُتابَعة.")
        return

    changed = False
    active = [(k, v) for k, v in data.items()
              if not v.get("stopped")
              and len(v.get("hits", [])) < len(v.get("targets", []))]
    print(f"[متابعة] إشارات نشطة: {len(active)}")

    for key, tr in active:
        tf = tr.get("timeframe", "4h")
        # شموع كافية لإعادة بناء القيعان المحورية و ATR منذ آخر معالجة
        need = {"15m": 400, "1h": 240, "4h": 120, "1d": 60}.get(tf, 120)
        df = fetch_binance(tr["symbol"], BINANCE_INTERVAL.get(tf, "4h"), need)
        if df is None or len(df) < 16:
            continue
        df = df.iloc[:-1]                  # استبعاد الشمعة الجارية (غير المغلقة)
        if len(df) < 16:
            continue
        mid = tr.get("message_id")
        events = _advance_trade(df, tr)
        for txt in events:
            changed = True
            if token and chat_id:
                send_telegram(token, chat_id, txt, reply_to=mid)
                time.sleep(0.4)
            print(f"  📲 {tr['symbol']}: {txt.splitlines()[0]}")

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    print("[متابعة] انتهت.")


def format_reversal_card(sig, cfg, label):
    """بطاقة تيليجرام لإشارة انعكاس حيّة، مُعنونة باسم الاستراتيجية."""
    tf = cfg.get("timeframe", "?")
    now = datetime.now().strftime("%H:%M:%S")
    fmt = _fmt_price
    is_tw = bool(cfg.get("trendwave"))
    entry = sig["entry"]
    stop = sig["stop"]
    levels = sig.get("dca") or sig.get("fib_entries") or []
    if is_tw:
        # trendwave: لا دخول مباشر — الدخول (entry) هو نفسه متوسط سلّم الفيبو
        avg_entry = entry
        risk_ref = entry
    else:
        # عند وجود سلّم دخول، المخاطرة تُقاس من متوسط الدخول (لا الدخول المباشر)
        all_entries = [entry] + list(levels)
        avg_entry = sum(all_entries) / len(all_entries) if all_entries else entry
        risk_ref = avg_entry if levels else entry
    risk_pct = ((risk_ref - stop) / risk_ref * 100) if risk_ref else 0.0
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    head = ("🌟 إشارة trendwave" if cfg.get("trendwave")
            else "🟢 اختراق RSI صعودي" if cfg.get("rsi_cross") else "🟢 انعكاس صعودي")
    lines = [f"{head} — {label}",
             f"💎 {sig['symbol']} · ⏱️ {tf}"]
    if sig.get("rsi") is not None:
        if cfg.get("trendwave"):
            _note = "ارتداد بعد تأكيد القاع + فلتر اتجاه"
        elif cfg.get("rsi_cross"):
            _note = f"تجاوز {int(cfg.get('rsi_ob', 80.0))}"
        else:
            _note = "ارتداد"
        lines.append(f"📈 RSI(21): {sig['rsi']} ({_note})")
    if sig.get("ml_prob") is not None:
        lines.append(f"🤖 ثقة الفلتر التعلّمي (موجات+MACD 4C): {sig['ml_prob']*100:.0f}%")
    lines.append("")

    # الدخول + مستويات الدخول على فيبوناتشي
    if is_tw and sig.get("dca"):
        # trendwave: سلّم دخول فيبو فقط (لا دخول مباشر)، والمتوسط هو مرجع الصفقة
        lines.append("🪜 مستويات الدخول (فيبوناتشي):")
        for k, lv in enumerate(sig["dca"], 1):
            lines.append(f"   {k}) {fmt(lv)}")
        lines.append(f"   ⚖️ متوسط الدخول: {fmt(avg_entry)}")
    elif sig.get("dca"):
        lines.append(f"📍 الدخول المباشر: {fmt(entry)}")
        lines.append("🪜 مستويات الدخول (فيبوناتشي):")
        for k, lv in enumerate(sig["dca"], 1):
            lines.append(f"   {k}) {fmt(lv)}")
        lines.append(f"   ⚖️ متوسط الدخول: {fmt(avg_entry)}")
    else:
        retr = sig.get("retr")
        suffix = f"  (ارتداد فيبو {retr})" if retr is not None else ""
        lines.append(f"📍 الدخول: {fmt(entry)}{suffix}")
        if sig.get("fib_entries"):
            lines.append("🪜 مستويات الدخول (فيبوناتشي):")
            for k, lv in enumerate(sig["fib_entries"], 1):
                lines.append(f"   {k}) {fmt(lv)}")
            lines.append(f"   ⚖️ متوسط الدخول: {fmt(avg_entry)}")

    lines.append(f"🛑 الوقف: {fmt(stop)}  (−{risk_pct:.2f}%)")
    lines += ["", "🎯 الأهداف:"]
    for k, t in enumerate(sig["targets"]):
        gain = ((t - entry) / entry * 100) if entry else 0.0
        n = nums[k] if k < len(nums) else f"{k + 1})"
        lines.append(f"{n} {fmt(t)}  (+{gain:.2f}%)")

    lines += ["",
              f"⚖️ المخاطرة لكل صفقة: {risk_pct:.2f}% من "
              f"{'متوسط الدخول' if levels else 'الدخول'}",
              f"⏰ {now}", "",
              "⚠️ تحليل تعليمي — ليس نصيحة مالية"]
    return "\n".join(lines)


def live_reversal_scan(cfg, watchlist_path, state_path):
    """يفحص القائمة على الإطار/الاستراتيجية المحدّدة، ويرسل إشارات الدخول الجديدة
    (عند آخر شمعة مغلقة) إلى تيليجرام مع منع التكرار."""
    token = cfg.get("tg_token") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = cfg.get("tg_chat") or os.environ.get("TELEGRAM_CHAT_ID")
    label = reversal_label(cfg)
    parsed = parse_watchlist(watchlist_path)
    targets = parsed["crypto"]      # الاستراتيجية مُتحقَّقة على الكريبتو
    print(f"[{label}] فحص {len(targets)} عملة ...")

    alerted = load_trades(state_path) if os.path.exists(state_path) else {}
    if not isinstance(alerted, dict):
        alerted = {}
    need = 1000 if cfg.get("timeframe") in ("1h", "15m") else 320
    if cfg.get("trendwave"):
        detector = detect_trendwave_signal
        # فلتر الفريم الأعلى يحتاج 200 شمعة على الفريم الأعلى → شموع أكثر
        tw_need = {"15m": 1200, "1h": 1200, "4h": 1300, "1d": 400}.get(cfg.get("timeframe"), 1000)
    elif cfg.get("rsi_cross"):
        detector = detect_rsi_cross_signal
    else:
        detector = detect_reversal_signal

    def work(item):
        sym = item["symbol"]
        if cfg.get("trendwave"):
            df = fetch_binance_paged(sym, BINANCE_INTERVAL[cfg["timeframe"]], tw_need)
        else:
            df = fetch_binance(sym, BINANCE_INTERVAL[cfg["timeframe"]], min(need, 1000))
        if df is None or len(df) < 60:
            return None
        df = df.iloc[:-1].reset_index(drop=True)  # استبعاد الشمعة الجارية (غير المغلقة)
        sig = detector(df, cfg)
        if sig:
            sig["symbol"] = sym
            sig["bar_ts"] = str(df["date"].iloc[-1])
            # فلتر التعلّم الآلي: يقرأ الموجات + MACD 4C وسياق السوق ويرشّح
            if cfg.get("ml_filter"):
                try:
                    import ml_filter
                    ok, prob, thr = ml_filter.passes_filter(
                        df, len(df) - 1, "crypto", cfg.get("side", "buy"))
                    if prob is not None:
                        sig["ml_prob"] = round(prob, 3)
                        if not ok:
                            return None                # أُسقطت إشارة ضعيفة الاحتمال
                except Exception:
                    pass                               # عند أي خطأ لا نكسر الفحص
        return sig

    found = []
    with ThreadPoolExecutor(max_workers=cfg.get("workers", 8)) as ex:
        for fut in as_completed([ex.submit(work, it) for it in targets]):
            try:
                s = fut.result()
            except Exception:
                s = None
            if s:
                found.append(s)

    sent = 0
    for s in found:
        key = f"{label}|{s['symbol']}|{s['bar_ts']}"
        if alerted.get(key):
            continue
        if token and chat_id:
            pid = register_pending_signal(s, label, cfg)
            markup = {"inline_keyboard": [[
                {"text": "📝 افتح صفقة ورقية", "callback_data": f"o|{pid}"},
                {"text": "📊 المتتبّع", "url": DASHBOARD_URL},
            ]]}
            mid = send_telegram(token, chat_id, format_reversal_card(s, cfg, label),
                                reply_markup=markup)
            # خزّن الإشارة للمتابعة والرد على رسالتها عند الهدف/الوقف
            if mid:
                track_signal(s, label, cfg, mid)
            time.sleep(0.5)
        alerted[key] = True
        sent += 1
        print(f"  📲 {label}: {s['symbol']} @ {s['bar_ts']}")
    save_trades(alerted, state_path)
    print(f"[{label}] إشارات جديدة مُرسَلة: {sent}")


def build_argparser():
    p = argparse.ArgumentParser(description="بوت البحث عن الصفقات")
    p.add_argument("--mode", choices=["scan", "monitor", "yearly", "backtest", "reversal", "trackmon"],
                   default="scan",
                   help="scan | monitor | yearly | backtest | reversal: تنبيه انعكاس حيّ | "
                        "trackmon: متابعة الإشارات والرد على رسالتها عند الهدف/الوقف")
    p.add_argument("--bt-bars", type=int, default=365,
                   help="عدد الشموع الأخيرة للاختبار التاريخي (افتراضي 365)")
    p.add_argument("--bt-hold", type=int, default=40,
                   help="أقصى عدد شموع لإمساك الصفقة الافتراضية (افتراضي 40)")
    p.add_argument("--bt-offset", type=int, default=0,
                   help="استبعاد أحدث N شمعة لاختبار فترة أقدم (تحقّق خارج العيّنة)")
    p.add_argument("--walk-forward", action="store_true",
                   help="(backtest) تحقّق walk-forward: اختيار أفضل إعداد على IS واختباره على OOS فقط")
    p.add_argument("--wf-folds", type=int, default=5,
                   help="عدد نوافذ الاختبار خارج العيّنة في walk-forward (افتراضي 5)")
    p.add_argument("--strategy", choices=["score", "reversal"], default="score",
                   help="score: النظام متعدد العوامل | reversal: انعكاس RSI الزخمي")
    p.add_argument("--ma200-confirm", action="store_true",
                   help="(انعكاس، 1h فقط) اشتراط إغلاق فوق متوسط 200 (على 4h) عند التشبّع الشرائي")
    p.add_argument("--dca-fib", action="store_true",
                   help="(انعكاس) دخول مباشر عند التأكيد ثم DCA على ارتدادات فيبوناتشي")
    p.add_argument("--cost", type=float, default=0.0,
                   help="تكلفة الصفقة ذهاباً وإياباً كنسبة (مثلاً 0.002 = 0.2%% عمولة+انزلاق)")
    p.add_argument("--rsi-ob", type=float, default=80.0,
                   help="عتبة التشبّع الشرائي لاستراتيجية الزخم RSI (افتراضي 80؛ جرّب 70/75)")
    p.add_argument("--rsi-os", type=float, default=20.0,
                   help="عتبة التشبّع البيعي (افتراضي 20)")
    p.add_argument("--os-multi", action="store_true",
                   help="(backtest) استراتيجية: موجة تشبّع شرائي مكتملة ثم تكرار التشبّع "
                        "البيعي N مرة ثم دخول مباشر بفيبو DCA")
    p.add_argument("--os-touches", type=int, default=2,
                   help="(os-multi) عدد نزولات التشبّع البيعي المطلوبة للدخول (15m=3، 1h=2)")
    p.add_argument("--trendwave", action="store_true",
                   help="استراتيجية مستقلة (الإعداد الرابح): دخول مباشر + DCA فيبو + فلتر "
                        "اتجاه من فريم أعلى + وقف متحرك 0.5×ATR مؤجّل 1R، بلا دايفرجنس. كل الفريمات")
    p.add_argument("--donchian", action="store_true",
                   help="(backtest) استراتيجية كلاسيكية: اختراق قناة Donchian (Turtle)")
    p.add_argument("--don-entry", type=int, default=20, help="(donchian) قمة الاختراق")
    p.add_argument("--don-exit", type=int, default=10, help="(donchian) قاع الخروج")
    p.add_argument("--ema-cross", action="store_true",
                   help="(backtest) استراتيجية كلاسيكية: تقاطع EMA (Golden Cross)")
    p.add_argument("--ema-fast", type=int, default=50, help="(ema-cross) المتوسط السريع")
    p.add_argument("--ema-slow", type=int, default=200, help="(ema-cross) المتوسط البطيء")
    p.add_argument("--rsi2", action="store_true",
                   help="(backtest) استراتيجية كلاسيكية: ارتداد RSI(2) لـ Connors")
    p.add_argument("--rsi2-buy", type=float, default=10.0, help="(rsi2) عتبة الدخول")
    p.add_argument("--osob", action="store_true",
                   help="(backtest) استراتيجية: تشبّع بيعي→شرائي ثم سلّم دخول عند ارتدادات "
                        "فيبو (DCA)، وقف متحرك تحت كل تصحيح، خروج عند دايفرجنس سلبي")
    p.add_argument("--trend-filter", action="store_true",
                   help="(osob) دخول فقط إذا كان الإغلاق فوق متوسط 200 على نفس الإطار")
    p.add_argument("--htf-trend", action="store_true",
                   help="(osob) احسب فلتر متوسط 200 على فريم أعلى (1h→4h، 15m→1h) لفلتر أقوى")
    p.add_argument("--trail-buf", type=float, default=0.25,
                   help="(osob) مضاعف ATR لمسافة الوقف المتحرك تحت التصحيح (افتراضي 0.25؛ أكبر = أوسع)")
    p.add_argument("--trail-arm", type=float, default=0.0,
                   help="(osob) لا يُفعَّل الوقف المتحرك إلا بعد ربح عائم ≥ هذا×المخاطرة (0=فوري؛ جرّب 1.0)")
    p.add_argument("--force-direct", action="store_true",
                   help="(osob) فرض الدخول السوقي المباشر على أي إطار (يجعل 1h/15m مثل 4h)")
    p.add_argument("--no-divergence", action="store_true",
                   help="(osob) تعطيل الخروج عند الدايفرجنس السلبي (الخروج بالوقف المتحرك فقط)")
    p.add_argument("--bt-stop-mult", type=float, default=1.5,
                   help="مضاعف ATR للوقف في باك-تست الزخم RSI80 (افتراضي 1.5؛ جرّب 2.0)")
    p.add_argument("--watchlist", help="مسار ملف الـ watchlist (مطلوب في وضع scan)")
    p.add_argument("--state", default=TRADES_FILE, help="ملف حفظ الصفقات المفتوحة")
    p.add_argument("--assets", choices=["all", "crypto", "stocks"], default=DEFAULTS["assets"])
    p.add_argument("--side", choices=["buy", "sell", "both"], default=DEFAULTS["side"],
                   help="نوع الصفقات: buy (افتراضي) / sell / both")
    p.add_argument("--targets", choices=["fib", "atr"], default=DEFAULTS["tp_method"],
                   help="طريقة الأهداف: fib امتداد فيبوناتشي (افتراضي) / atr مضاعفات ATR")
    p.add_argument("--require-divergence", action="store_true",
                   help="إرسال الصفقات ذات الدايفرجنس المؤكّد فقط")
    p.add_argument("--market-filter", action="store_true",
                   help="استبعاد الصفقات المعاكسة لاتجاه السوق العام (BTC/SPY)")
    p.add_argument("--supply-demand", action="store_true",
                   help="شرط مناطق العرض/الطلب (دخول عند منطقة طازجة فقط)")
    p.add_argument("--vwap-daily", action="store_true",
                   help="شرط VWAP مثبّت يومياً (ذو معنى على 4h/1h فقط)")
    p.add_argument("--vwap-weekly", action="store_true",
                   help="شرط VWAP مثبّت أسبوعياً (شراء فقط فوقه)")
    p.add_argument("--vwap-monthly", action="store_true",
                   help="شرط VWAP مثبّت شهرياً (شراء فقط فوقه)")
    p.add_argument("--dca", action="store_true",
                   help="عرض سلّم دخول DCA (4 مستويات فيبوناتشي) ومتوسط الدخول")
    p.add_argument("--quiet-empty", action="store_true",
                   help="عدم إرسال رسالة عند عدم وجود فرص جديدة (للفحص المتكرر)")
    p.add_argument("--timeframe", choices=["1d", "4h", "1h", "15m"], default=DEFAULTS["timeframe"])
    p.add_argument("--rsi-cross", action="store_true",
                   help="(reversal) إشارة شراء فور تجاوز RSI(21) خط الـ80 — استراتيجية زخم")
    p.add_argument("--ml-filter", action="store_true",
                   help="فلتر التعلّم الآلي: يرشّح الإشارات حسب الموجات + MACD 4C وسياق السوق (يتطلّب ml_model.joblib)")
    p.add_argument("--top", type=int, default=DEFAULTS["top"])
    p.add_argument("--min-score", type=int, default=DEFAULTS["min_score"])
    p.add_argument("--workers", type=int, default=DEFAULTS["workers"])
    p.add_argument("--output-dir", default=".")
    p.add_argument("--telegram-token", default=None,
                   help="توكن بوت تيليجرام (أو متغيّر البيئة TELEGRAM_TOKEN)")
    p.add_argument("--telegram-chat-id", default=None,
                   help="معرّف المحادثة (أو متغيّر البيئة TELEGRAM_CHAT_ID)")
    return p


if __name__ == "__main__":
    args = build_argparser().parse_args()
    cfg = {
        "timeframe": args.timeframe, "lookback": DEFAULTS["lookback"],
        "top": args.top, "min_score": args.min_score,
        "workers": args.workers, "assets": args.assets, "side": args.side,
        "tp_method": args.targets, "require_divergence": args.require_divergence,
        "market_filter": args.market_filter, "dca": args.dca,
        "vwap_d": args.vwap_daily, "sd": args.supply_demand,
        "vwap_w": args.vwap_weekly, "vwap_m": args.vwap_monthly,
        "quiet_empty": args.quiet_empty,
        "tg_token": args.telegram_token, "tg_chat": args.telegram_chat_id,
        "state_path": args.state,
        "bt_bars": args.bt_bars, "bt_hold": args.bt_hold, "cost": args.cost,
        "bt_offset": args.bt_offset, "strategy": args.strategy,
        "ma200_ob": args.ma200_confirm, "dca_fib": args.dca_fib,
        "rsi_cross": args.rsi_cross,
        "rsi_ob": args.rsi_ob, "rsi_os": args.rsi_os, "bt_stop_mult": args.bt_stop_mult,
        "os_multi": args.os_multi, "os_touches": args.os_touches,
        "osob": args.osob, "trend_filter": args.trend_filter,
        "trail_buf": args.trail_buf, "trail_arm": args.trail_arm,
        "force_direct": args.force_direct, "no_div": args.no_divergence,
        "htf_trend": args.htf_trend, "trendwave": args.trendwave,
        "walk_forward": args.walk_forward, "wf_folds": args.wf_folds,
        "donchian": args.donchian, "don_entry": args.don_entry, "don_exit": args.don_exit,
        "ema_cross": args.ema_cross, "ema_fast": args.ema_fast, "ema_slow": args.ema_slow,
        "rsi2": args.rsi2, "rsi2_buy": args.rsi2_buy,
        "ml_filter": args.ml_filter,
    }
    if args.trendwave:        # الإعداد الرابح المثبّت
        if args.trail_buf == 0.25:
            cfg["trail_buf"] = 0.5
        if args.trail_arm == 0.0:
            cfg["trail_arm"] = 1.0
    if args.mode == "monitor":
        monitor(cfg, args.state)
    elif args.mode == "trackmon":
        monitor_tracked_signals(cfg)
    elif args.mode == "reversal":
        if not args.watchlist:
            sys.exit("⚠️ وضع reversal يتطلب --watchlist")
        live_reversal_scan(cfg, args.watchlist, args.state)
    elif args.mode == "backtest":
        if not args.watchlist:
            sys.exit("⚠️ وضع backtest يتطلب --watchlist")
        if cfg.get("walk_forward"):
            run_walkforward(cfg, args.watchlist, args.output_dir)
        else:
            run_backtest(cfg, args.watchlist, args.output_dir)
    elif args.mode == "yearly":
        if not args.watchlist:
            sys.exit("⚠️ وضع yearly يتطلب --watchlist")
        scan_yearly_crosses(cfg, args.watchlist)
    else:
        if not args.watchlist:
            sys.exit("⚠️ وضع scan يتطلب --watchlist")
        run(cfg, args.watchlist, args.output_dir)
