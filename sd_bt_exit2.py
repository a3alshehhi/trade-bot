#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست متغيّرات الخروج (تقييد الهدف الأول بعتبة R) — طلب بو محمد 2026-07-09.

الدافع (من تشخيص sd_bt_diag تشغيل #2): على 15m، 72% من الصفقات الخاسرة تعطي ربحاً
عائماً ≥1.0R قبل أن تنعكس إلى وقف كامل −1R. السبب: هدف1 الفيبوناتشي يقع غالباً أبعد
من +1R، فلا يُقفَل نصف ولا يُنقَل الوقف، ثم ينعكس السعر للوقف كاملاً.

هذا السكربت يثبّت الدخول (نفس setup_features + نفس الفلاتر الحيّة + فلتر الاتجاه المختار
+ متوسط DCA) ويغيّر منطق الخروج فقط، ثم يقارن مجموع/توقّع/PF/فوز لعدّة أنظمة:

  base   = الحيّ الحالي: نصف عند هدف1 الفيبوناتشي، قفل +0.3R، الباقي لهدف2.
  cap12  = تقييد الهدف الأول بـ min(هدف1، +1.2R): نصف عند الأقرب، قفل +0.3R، الباقي لهدف2.
  cap10  = تقييد الهدف الأول بـ min(هدف1، +1.0R).
  cap08  = تقييد الهدف الأول بـ min(هدف1، +0.8R).
  be10   = إبقاء الجني عند هدف1، لكن نقل الوقف للتعادل وقائياً حين يبلغ MFE +1.0R (بلا جني).

كلها 50/50، الوقف أولاً تحفّظاً عند التعارض، إغلاق زمني على آخر شمعة. الناتج بوحدات R (خام،
بلا رسوم — المقارنة نسبية والرسوم شبه متساوية). فريم عبر SD_ENTRY_TF/SD_HTF، فلتر عبر SD_TREND_MA.
يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance).
"""
import os
import math
import time

import sd_bot as S

LOCK_R = float(os.environ.get("SD_LOCK_R", "0.3"))


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


def build_ma(c, ma_kind, ma_len):
    if ma_kind == "sma":
        return sma(c, ma_len)
    if ma_kind == "ema":
        return S.ema(c, ma_len)
    return None


def base_filter_ok(f, c, ma, tch, max_dist, CFG):
    if ma is None:
        rel = f["emaRel"]
    else:
        m = ma[tch]
        if not math.isfinite(m) or m <= 0:
            return False
        rel = (c[tch] - m) / m
    if rel <= 0:
        return False
    if max_dist and rel > max_dist:
        return False
    if f["htf"] < 0:
        return False
    if CFG["require_choch"] and not f["choch"]:
        return False
    if CFG["require_ob_after_os"] and not f["rsiObOs"]:
        return False
    if CFG["require_confirm"] and not f["confirm"]:
        return False
    if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
        return False
    return True


def sim(avg, stop, tp1, tp2, h, l, c, tch, hold, cap_R=None, protect_be_R=None):
    """يحاكي إدارة 50/50 مع تقييد اختياري للهدف الأول بعتبة R (cap_R) ونقل تعادل وقائي (protect_be_R).
       الناتج بوحدات R. الوقف أولاً تحفّظاً عند التعارض بنفس الشمعة."""
    R = avg - stop
    if R <= 0:
        return None
    r2 = (tp2 - avg) / R
    # مستوى الهدف الأول الفعلي: الأقرب بين هدف1 الفيبوناتشي و(avg + cap_R·R)
    fp1 = tp1 if cap_R is None else min(tp1, avg + cap_R * R)
    r1 = (fp1 - avg) / R
    if r1 <= 0:
        return None
    end = min(len(c), tch + hold)
    sl = stop
    half = False
    be_done = False
    realized = 0.0
    for i in range(tch, end):
        if l[i] <= sl:                                   # ضُرب الوقف
            return realized + (0.5 if half else 1.0) * ((sl - avg) / R)
        if (protect_be_R is not None and not be_done and not half
                and h[i] >= avg + protect_be_R * R):     # تعادل وقائي قبل الجني
            sl = max(sl, avg)
            be_done = True
        if not half and h[i] >= fp1:                     # جني النصف الأول
            half = True
            realized = 0.5 * r1
            lock = avg + LOCK_R * R
            if r1 <= LOCK_R:                             # هدف قريب جداً → تعادل تفادياً لوقف فوري
                lock = avg
            sl = max(sl, lock)
        if half and h[i] >= tp2:                         # الهدف الثاني للنصف الباقي
            return realized + 0.5 * r2
    X = c[end - 1]                                       # إغلاق زمني
    return realized + (0.5 if half else 1.0) * ((X - avg) / R)


SCHEMES = [
    ("base ", dict(cap_R=None, protect_be_R=None)),
    ("cap12", dict(cap_R=1.2,  protect_be_R=None)),
    ("cap10", dict(cap_R=1.0,  protect_be_R=None)),
    ("cap08", dict(cap_R=0.8,  protect_be_R=None)),
    ("be10 ", dict(cap_R=None, protect_be_R=1.0)),
]


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]; htf = CFG["htf"]; hold = CFG["bt_hold"]
    max_dist = CFG["max_ema_dist"]
    ma_kind, ma_len = parse_trend_ma()
    ma_label = "live-EMA" if ma_kind == "live" else f"{ma_kind.upper()}{ma_len}"
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    print(f"exit2 SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | فلتر اتجاه={ma_label} | LOCK_R={LOCK_R}",
          flush=True)

    trades = []
    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = S.setup_features(s, d1, d4)
            ma = build_ma(c, ma_kind, ma_len)
            for st in setups:
                if not base_filter_ok(st["f"], c, ma, st["touch"], max_dist, CFG):
                    continue
                tch = st["touch"]
                avg = S._dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"],
                                     h, l, c, tch, hold)
                if not (avg - st["stop"] > 0):
                    continue
                trades.append((avg, st["stop"], st["tp1"], st["tp2"], h, l, c, tch))
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    print("", flush=True)
    print(f"════ مقارنة الخروج — الفريم {tf} (سياق {htf}) — فلتر {ma_label} — صفقات={len(trades)} ════",
          flush=True)
    results = {}
    for name, kw in SCHEMES:
        rs = []
        for (avg, stop, tp1, tp2, h, l, c, tch) in trades:
            r = sim(avg, stop, tp1, tp2, h, l, c, tch, hold, **kw)
            if r is not None:
                rs.append(r)
        results[name] = rs
        print(f"  {name} : {S._stats(rs)}", flush=True)

    base_tot = sum(results["base "]) if results.get("base ") else 0.0
    print("", flush=True)
    print("  الفرق عن base (مجموع R):", flush=True)
    for name, _ in SCHEMES:
        if name.strip() == "base":
            continue
        diff = sum(results[name]) - base_tot
        print(f"    {name} : {diff:+.1f}R", flush=True)


if __name__ == "__main__":
    run_frame()
