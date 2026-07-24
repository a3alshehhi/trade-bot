#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_regime.py — يبني ملف نظام السوق (regime.json) متعدد الفريمات لبوت الفيواب.

القاعدة (بو محمد 2026-07-25، شرط رئيسي):
  • الاتجاه يُقاس ببنية القمم/القيعان (لا EMA):
      BTC صاعد   = قمم أعلى + قيعان أعلى (HH/HL)
      USDT.D هابط = قمم أدنى + قيعان أدنى (LH/LL)  ← سيولة تدخل الألت (مؤاتٍ للشراء)
  • يُحلَّل على 4 فريمات: 15m / 1h / 4h / 1d (15m الأهم لأن الدخول يومي).
  • «الفريم مؤاتٍ» = (BTC صاعد) و(USDT.D هابط) على ذلك الفريم.
  • قرار الدخول (في الباك-تست): أغلبية ≥3 من 4 فريمات مؤاتية.

المصادر:
  • BTC (BTCUSDT) من binance.vision — مجاني ودقيق على كل الفريمات.
  • استحواذ USDT (CRYPTOCAP:USDT.D) من TradingView عبر tvdatafeed (شبكة السحابة).

المخرجات: regime.json = { "meta": {...}, "15m": {"t":[ms], "btc":[+1/-1/0], "usdtd":[+1/-1/0]}, ... }
  +1 = صاعد ، -1 = هابط ، 0 = محايد/غير مؤكّد (يُحمل آخر تصنيف مؤكّد).
"""
import json, time, sys, math
import requests

TFS = ["15m", "1h", "4h", "1d"]
BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
# عدد الشموع المطلوب لكل فريم (يكفي للباك-تست + إحماء بنية القمم)
BARS = {"15m": 5000, "1h": 2500, "4h": 1200, "1d": 400}
PIVOT = {"15m": 3, "1h": 3, "4h": 2, "1d": 2}   # نصف عرض الفراكتال لكشف المحور لكل فريم


# ───────────────────────── جلب BTC من binance.vision ─────────────────────────
def fetch_binance(symbol, interval, need):
    """يجلب need شمعة (تقريباً) بالترقيم للخلف عبر endTime. يعيد dict t/o/h/l/c/v."""
    all_rows, end_time = [], None
    pages = math.ceil(need / 1000) + 1
    for _ in range(pages):
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        data = None
        for base in BINANCE_BASES:
            try:
                r = requests.get(f"{base}/api/v3/klines", params=params, timeout=15)
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
        raise RuntimeError(f"فشل جلب {symbol} {interval} من binance.vision")
    m = {row[0]: row for row in all_rows}
    rows = sorted(m.values(), key=lambda x: x[0])
    return dict(t=[int(r[0]) for r in rows],
                h=[float(r[2]) for r in rows], l=[float(r[3]) for r in rows],
                c=[float(r[4]) for r in rows])


# ───────────────────────── جلب USDT.D من TradingView ─────────────────────────
def fetch_usdtd(interval, need):
    """يجلب CRYPTOCAP:USDT.D عبر tvdatafeed. يعيد dict t(ms)/h/l/c."""
    from tvDatafeed import TvDatafeed, Interval
    tvmap = {"15m": Interval.in_15_minute, "1h": Interval.in_1_hour,
             "4h": Interval.in_4_hour, "1d": Interval.in_daily}
    tv = TvDatafeed()   # جلسة مجهولة (بدون تسجيل دخول)
    df = tv.get_hist(symbol="USDT.D", exchange="CRYPTOCAP",
                     interval=tvmap[interval], n_bars=min(need, 5000))
    if df is None or len(df) == 0:
        raise RuntimeError(f"فشل جلب USDT.D {interval} من TradingView")
    t = [int(ts.timestamp() * 1000) for ts in df.index]
    return dict(t=t, h=list(df["high"]), l=list(df["low"]), c=list(df["close"]))


# ───────────────────────── بنية القمم/القيعان ─────────────────────────
def _pivots(h, l, k):
    """محاور فراكتال: قمة عند i إذا h[i] أعلى من k شمعات كل جهة؛ قاع بالعكس.
    يُؤكَّد المحور بعد k شمعات (لا نظرة مستقبلية عند الاستخدام)."""
    n = len(h); ph = [False]*n; pl = [False]*n
    for i in range(k, n-k):
        if all(h[i] >= h[i-j] for j in range(1, k+1)) and all(h[i] > h[i+j] for j in range(1, k+1)):
            ph[i] = True
        if all(l[i] <= l[i-j] for j in range(1, k+1)) and all(l[i] < l[i+j] for j in range(1, k+1)):
            pl[i] = True
    return ph, pl


def structure_labels(t, h, l, k):
    """يعيد تصنيفاً لكل شمعة: +1 صاعد (HH/HL) ، -1 هابط (LH/LL) ، 0 محايد.
    يُسنَد التصنيف من لحظة تأكيد المحور (i+k) فصاعداً لتفادي النظرة المستقبلية."""
    n = len(h); ph, pl = _pivots(h, l, k)
    lab = [0]*n
    last_hi = prev_hi = None
    last_lo = prev_lo = None
    cur = 0
    # نمرّ زمنياً؛ المحور عند i يصبح معلوماً عند i+k
    events = []
    for i in range(n):
        if ph[i]: events.append((i+k, "H", h[i]))
        if pl[i]: events.append((i+k, "L", l[i]))
    events.sort()
    ei = 0
    for bar in range(n):
        while ei < len(events) and events[ei][0] <= bar:
            _, kind, val = events[ei]; ei += 1
            if kind == "H":
                prev_hi, last_hi = last_hi, val
            else:
                prev_lo, last_lo = last_lo, val
            if last_hi is not None and prev_hi is not None and last_lo is not None and prev_lo is not None:
                if last_hi > prev_hi and last_lo > prev_lo:
                    cur = 1
                elif last_hi < prev_hi and last_lo < prev_lo:
                    cur = -1
                # غير ذلك: نحمل آخر تصنيف (cur كما هو)
        lab[bar] = cur
    return lab


# ───────────────────────── البناء ─────────────────────────
def build():
    out = {"meta": {"built_ts": int(time.time()*1000),
                    "rule": "BTC HH/HL(+1) & USDT.D LH/LL(-1); favorable TF = btc==+1 & usdtd==-1; enter if >=3/4 TFs",
                    "tfs": TFS}}
    for tf in TFS:
        print(f"[regime] fetching BTC {tf} ...", flush=True)
        b = fetch_binance("BTCUSDT", tf, BARS[tf])
        print(f"[regime] fetching USDT.D {tf} ...", flush=True)
        u = fetch_usdtd(tf, BARS[tf])
        btc_lab = structure_labels(b["t"], b["h"], b["l"], PIVOT[tf])
        usd_lab = structure_labels(u["t"], u["h"], u["l"], PIVOT[tf])
        out[tf] = {"t": b["t"], "btc": btc_lab,
                   "usdtd_t": u["t"], "usdtd": usd_lab}
        print(f"[regime] {tf}: btc {len(btc_lab)} bars, usdtd {len(usd_lab)} bars", flush=True)
    with open("regime.json", "w") as f:
        json.dump(out, f)
    print("[regime] wrote regime.json", flush=True)


if __name__ == "__main__":
    build()
