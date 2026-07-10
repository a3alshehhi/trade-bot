#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست شرط «دايفرجنس إيجابي (صعودي) على هيستوجرام MACD 4C» كبوابة دخول — طلب بو محمد 2026-07-08.
(بديل عن بوابة التشبّع البيعي التي أُسقِطت لأنها تُصفّر العيّنة.)

الفكرة: نعزل أثر الدايفرجنس الصعودي وحده مع تثبيت كل شيء آخر كما في الحيّ (نفس فلاتر الدخول،
DCA، إدارة 50/50، الرسوم، نفس المناطق)، ونقارن على نفس الصفقات لكل فريم:
  • بدون الشرط (أساس) : الدخول الحيّ كما هو
  • div              : يوجد دايفرجنس صعودي على هيستوجرام MACD قبل بداية موجة الـCHoCH
  • 4c              : حالة MACD 4C صاعدة عند شمعة الدخول (الهيستوجرام يرتفع: حالة ∈ {-1, 2})
  • div+4c          : الشرطان معاً

تعريف الدايفرجنس الصعودي «العادي» (regular bullish):
  نأخذ آخر قاعين محوريين (pivot lows) للسعر داخل نافذة [j-lookback, j] قبل بدء الموجة.
  دايفرجنس صعودي = السعر صنع قاعاً أدنى (l[b] < l[a]) بينما هيستوجرام MACD صنع قاعاً أعلى
  (hist[b] > hist[a]) — أي الزخم الهبوطي يخفت رغم هبوط السعر ⇒ بوادر انعكاس صعودي.

فلتر الاتجاه قابل للاختيار عبر SD_TREND_MA (live/smaN/emaN) — نفس آلية os21.
يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance). الفريم عبر SD_ENTRY_TF/SD_HTF،
النافذة عبر SD_OS_LOOKBACK.
"""
import os
import math
import time

import sd_bot as S


def sma(arr, n):
    out = [float("nan")] * len(arr)
    s = 0.0
    for i, x in enumerate(arr):
        s += x
        if i >= n:
            s -= arr[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def parse_trend_ma():
    raw = os.environ.get("SD_TREND_MA", "live").strip().lower()
    if raw in ("", "live"):
        return ("live", 0)
    kind = "sma" if raw.startswith("sma") else ("ema" if raw.startswith("ema") else None)
    try:
        n = int(raw[3:])
    except ValueError:
        n = 0
    if kind and n > 0:
        return (kind, n)
    return ("live", 0)


def bullish_divergence(lows_idx, l, hist, lo, hi, c=None, ma=None):
    """دايفرجنس صعودي عادي بين آخر قاعين محوريين في [lo, hi]:
       قاع سعر أدنى + قاع هيستوجرام أعلى.
       شرط إضافي اختياري (طلب بو محمد): إن مُرِّر (c, ma) وجب أن يكون القاعان
       مغلقين تحت المتوسط (السعر تحت SMA50) لاعتماد الدايفرجنس."""
    pts = [x for x in lows_idx if lo <= x <= hi and math.isfinite(hist[x])]
    if len(pts) < 2:
        return False
    a, b = pts[-2], pts[-1]          # الأقدم، ثم الأحدث
    if not ((l[b] < l[a]) and (hist[b] > hist[a])):
        return False
    if c is not None and ma is not None:
        if not (math.isfinite(ma[a]) and math.isfinite(ma[b])
                and c[a] < ma[a] and c[b] < ma[b]):
            return False
    return True


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    lookback = CFG["os_lookback"]        # نافذة البحث عن الدايفرجنس قبل الموجة
    max_dist = CFG["max_ema_dist"]
    ma_kind, ma_len = parse_trend_ma()
    ma_label = "live-EMA" if ma_kind == "live" else f"{ma_kind.upper()}{ma_len}"
    div_below_ma = int(os.environ.get("SD_DIV_BELOW_MA", 50))  # شرط: قاعا الدايفرجنس تحت SMA(n)؛ 0=تعطيل
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    groups = {"base": [], "div": [], "m4c": [], "div4c": []}
    n_symbols = 0

    print(f"div backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | "
          f"نافذة={lookback} | فلتر اتجاه={ma_label} | dist={max_dist}", flush=True)

    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = S.setup_features(s, d1, d4)
            # ── مؤشّرات الدايفرجنس على نفس أسعار الإغلاق ──
            piv, _ = S.structure(h, l, c, CFG["pivL"], CFG["pivR"])
            lows_idx = [p[0] for p in piv if p[2] == "L"]
            _, _, hist = S.macd(c)
            # ── سلسلة فلتر الاتجاه المختار ──
            ma = None
            if ma_kind == "sma":
                ma = sma(c, ma_len)
            elif ma_kind == "ema":
                ma = S.ema(c, ma_len)
            # سلسلة SMA لشرط «الدايفرجنس تحت المتوسط» (SD_DIV_BELOW_MA؛ 0 = تعطيل)
            div_ma = sma(c, div_below_ma) if div_below_ma else None
            n_symbols += 1

            for st in setups:
                f = st["f"]
                tch = st["touch"]
                # ── فلتر الاتجاه ──
                if ma is None:
                    rel = f["emaRel"]
                else:
                    m = ma[tch]
                    if not math.isfinite(m) or m <= 0:
                        continue
                    rel = (c[tch] - m) / m
                if rel <= 0:
                    continue
                if max_dist and rel > max_dist:
                    continue
                # ── بقيّة فلاتر الدخول الحيّة ──
                if f["htf"] < 0:
                    continue
                if CFG["require_choch"] and not f["choch"]:
                    continue
                if CFG["require_ob_after_os"] and not f["rsiObOs"]:
                    continue
                if CFG["require_confirm"] and not f["confirm"]:
                    continue
                if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
                    continue

                avg = S._dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"],
                                     h, l, c, tch, hold)
                r = S._sim_5050(avg, st["stop"], st["tp1"], st["tp2"], h, l, c, tch, hold)
                if r is None:
                    continue
                groups["base"].append(r)

                # ── الدايفرجنس الصعودي + حالة 4C عند الدخول ──
                j = st["created"]
                lo = max(1, j - lookback)
                div = bullish_divergence(lows_idx, l, hist, lo, j, c, div_ma)
                st4 = S.macd4c_state(hist, tch)
                c4 = st4 in (-1, 2)          # الهيستوجرام يرتفع (زخم صاعد)
                if div:
                    groups["div"].append(r)
                if c4:
                    groups["m4c"].append(r)
                if div and c4:
                    groups["div4c"].append(r)
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    # ── التقرير ──
    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) — فلتر اتجاه {ma_label} ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols} · صفقات خط الأساس={len(groups['base'])}", flush=True)
    order = [("بدون الشرط (أساس)", "base"),
             ("دايفرجنس صعودي", "div"),
             ("MACD 4C صاعد", "m4c"),
             ("دايفرجنس + 4C", "div4c")]
    for label, key in order:
        print(f"  • {label:<20} : {S._stats(groups[key])}", flush=True)
    return {tf: {k: S._stats(v) for k, v in groups.items()}}


if __name__ == "__main__":
    run_frame()
