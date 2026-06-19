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
from datetime import datetime
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
BINANCE_INTERVAL = {"1d": "1d", "4h": "4h", "1h": "1h"}
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
def send_telegram(token, chat_id, text):
    """يرسل رسالة إلى تيليجرام. يقسّم الرسائل الطويلة (حد 4096 حرفاً)."""
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for i in range(0, len(text), 3500):
        chunk = text[i:i + 3500]
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": chunk,
                                         "disable_web_page_preview": True}, timeout=15)
            if r.status_code != 200:
                ok = False
                print(f"⚠️ تيليجرام: {r.status_code} {r.text[:200]}")
        except Exception as e:
            ok = False
            print(f"⚠️ تعذّر الإرسال إلى تيليجرام: {e}")
    return ok


SEP = "━━━━━━━━━━━━━━━━━━"


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

def _simulate_trade(df, i, entry, stop, targets, direction, hold, manage):
    """يحاكي مصير صفقة فُتحت عند الشمعة i. يرجع (R, نتيجة) أو None."""
    n = len(df)
    risk = abs(entry - stop)
    if risk <= 0 or not targets:
        return None
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tp1, tp_final = targets[0], targets[-1]

    stop_cur = stop
    part = 1.0          # الجزء المتبقّي من الصفقة
    realized = 0.0      # الربح/الخسارة المحقّق بوحدات R
    tp1_done = False
    last_c = entry

    def hit_stop(px_lo, px_hi):
        return px_lo <= stop_cur if direction == 1 else px_hi >= stop_cur

    def hit(level, px_lo, px_hi):
        return px_hi >= level if direction == 1 else px_lo <= level

    for j in range(i + 1, min(i + 1 + hold, n)):
        lo, hi, last_c = low[j], high[j], close[j]
        # 1) الوقف أولاً (محافظ)
        if hit_stop(lo, hi):
            realized += part * direction * (stop_cur - entry) / risk
            return realized, ("be_stop" if tp1_done else "stop")
        # 2) الأهداف
        if manage:
            if not tp1_done and hit(tp1, lo, hi):
                realized += 0.5 * direction * (tp1 - entry) / risk
                part = 0.5
                tp1_done = True
                stop_cur = entry          # نقل الوقف لنقطة الدخول
            if tp1_done and hit(tp_final, lo, hi):
                realized += part * direction * (tp_final - entry) / risk
                return realized, "target"
        else:
            if hit(tp_final, lo, hi):
                realized += part * direction * (tp_final - entry) / risk
                return realized, "target"
    # 3) خروج زمني عند آخر إغلاق متاح
    realized += part * direction * (last_c - entry) / risk
    return realized, "time"


def backtest_symbol(item, kind, cfg):
    """يفتح صفقات افتراضية على تاريخ رمز واحد ويرجع قائمة صفقات مغلقة."""
    sym = item["symbol"]
    bars = cfg.get("bt_bars", 365)
    hold = cfg.get("bt_hold", 40)
    min_score = cfg["min_score"]
    side = cfg.get("side", "buy")

    if kind == "crypto":
        df = fetch_binance(sym, BINANCE_INTERVAL[cfg["timeframe"]], min(bars + 220, 1000))
    else:
        df = fetch_stock(sym, YF_INTERVAL[cfg["timeframe"]], "2y")
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
        if ok_side and abs(r["score"]) >= min_score:
            direction = 1 if is_buy else -1
            entry = r["price"]
            tps = [t["price"] for t in (r.get("targets") or [])]
            simA = _simulate_trade(df, i, entry, r["stop"], tps, direction, hold, manage=False)
            simB = _simulate_trade(df, i, entry, r["stop"], tps, direction, hold, manage=True)
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
          f"الحد الأدنى للدرجة: {cfg['min_score']}\n")

    all_trades = []
    done = 0
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = {ex.submit(backtest_symbol, it, kind, cfg): it for it, kind in targets}
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


def build_argparser():
    p = argparse.ArgumentParser(description="بوت البحث عن الصفقات")
    p.add_argument("--mode", choices=["scan", "monitor", "yearly", "backtest"], default="scan",
                   help="scan: بحث | monitor: متابعة | yearly: المتوسط السنوي | backtest: اختبار تاريخي")
    p.add_argument("--bt-bars", type=int, default=365,
                   help="عدد الشموع الأخيرة للاختبار التاريخي (افتراضي 365)")
    p.add_argument("--bt-hold", type=int, default=40,
                   help="أقصى عدد شموع لإمساك الصفقة الافتراضية (افتراضي 40)")
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
    p.add_argument("--dca", action="store_true",
                   help="عرض سلّم دخول DCA (4 مستويات فيبوناتشي) ومتوسط الدخول")
    p.add_argument("--quiet-empty", action="store_true",
                   help="عدم إرسال رسالة عند عدم وجود فرص جديدة (للفحص المتكرر)")
    p.add_argument("--timeframe", choices=["1d", "4h", "1h"], default=DEFAULTS["timeframe"])
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
        "quiet_empty": args.quiet_empty,
        "tg_token": args.telegram_token, "tg_chat": args.telegram_chat_id,
        "state_path": args.state,
        "bt_bars": args.bt_bars, "bt_hold": args.bt_hold,
    }
    if args.mode == "monitor":
        monitor(cfg, args.state)
    elif args.mode == "backtest":
        if not args.watchlist:
            sys.exit("⚠️ وضع backtest يتطلب --watchlist")
        run_backtest(cfg, args.watchlist, args.output_dir)
    elif args.mode == "yearly":
        if not args.watchlist:
            sys.exit("⚠️ وضع yearly يتطلب --watchlist")
        scan_yearly_crosses(cfg, args.watchlist)
    else:
        if not args.watchlist:
            sys.exit("⚠️ وضع scan يتطلب --watchlist")
        run(cfg, args.watchlist, args.output_dir)
