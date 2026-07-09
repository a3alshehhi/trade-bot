#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست فجوة اللقطة (polling gap) — طلب بو محمد 2026-07-09.

الاكتشاف: الإدارة الحيّة في sd_autotrade.py تقرأ bx.last_price (لقطة واحدة كل 15 دقيقة)،
بينما الباك-تست يمشي على قمم/قيعان الشموع. فتُفوَّت ذيول +1R/هدف1/الوقف اللحظية. تسليح +1R
موجود حيّاً (السطر 530) لكنه لا يُطلَق لأن اللقطة لا ترى الذيل.

هذا السكربت يشغّل *نفس آلة حالة الخروج الحيّة بالضبط* (وقف → هدف1 50% + قفل LOCK_R →
هدف2 → تسليح +1R قبل الهدف1 + تتبّع price−R) تحت وضعين لكل صفقة:

  close = المشغّلات كلها على إغلاق الشمعة c[i]  (محاكاة last_price لحظة الفحص)
  hl    = المشغّلات الصاعدة على القمة h[i] والوقف على القاع l[i]  (رؤية الذيول = الباك-تست)

الفرق بين الوضعين = تكلفة عمى الذيول. الدخول مثبّت (نفس setup_features + الفلاتر + DCA).
الناتج بوحدات R (خام). فريم عبر SD_ENTRY_TF/SD_HTF، فلتر عبر SD_TREND_MA.
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


def sim_live(avg, stop, tp1, tp2, h, l, c, tch, hold, mode):
    """يحاكي آلة حالة الخروج الحيّة (sd_autotrade._manage_one) شمعةً شمعة.
       mode='close' → كل المشغّلات على c[i]؛ mode='hl' → الصاعدة على h[i] والوقف على l[i].
       الترتيب داخل كل فحص مطابق للحيّ: وقف → هدف1 → هدف2 → تسليح/تتبّع. الناتج بوحدات R."""
    R = avg - stop
    if R <= 0:
        return None
    r1 = (tp1 - avg) / R
    r2 = (tp2 - avg) / R
    if r1 <= 0:
        return None
    end = min(len(c), tch + hold)
    sl = stop
    tp1_done = False
    armed = False
    realized = 0.0
    for i in range(tch, end):
        up = h[i] if mode == "hl" else c[i]      # سعر المشغّلات الصاعدة
        dn = l[i] if mode == "hl" else c[i]      # سعر فحص الوقف
        # (1) الوقف أولاً
        if dn <= sl:
            frac = 0.5 if tp1_done else 1.0
            return realized + frac * ((sl - avg) / R)
        # (2) الهدف الأول: بيع 50% + قفل
        if not tp1_done and up >= tp1:
            tp1_done = True
            realized += 0.5 * r1
            lock = avg + LOCK_R * R
            if r1 <= LOCK_R:
                lock = avg
            sl = max(sl, lock)
            armed = True
        # (3) الهدف الثاني للنصف الباقي
        if tp1_done and up >= tp2:
            return realized + 0.5 * r2
        # (4) تسليح +1R قبل الهدف1 + تتبّع price−R
        if not armed and up >= avg + R:
            armed = True
            lock = avg + LOCK_R * R
            if lock > sl:
                sl = lock
        if armed:
            trail = up - R
            if trail > sl:
                sl = trail
    X = c[end - 1]                                # إغلاق زمني
    frac = 0.5 if tp1_done else 1.0
    return realized + frac * ((X - avg) / R)


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]; htf = CFG["htf"]; hold = CFG["bt_hold"]
    max_dist = CFG["max_ema_dist"]
    ma_kind, ma_len = parse_trend_ma()
    ma_label = "live-EMA" if ma_kind == "live" else f"{ma_kind.upper()}{ma_len}"
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    print(f"poll SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | فلتر اتجاه={ma_label} | LOCK_R={LOCK_R}",
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
    print(f"════ فجوة اللقطة — الفريم {tf} (سياق {htf}) — فلتر {ma_label} — صفقات={len(trades)} ════",
          flush=True)
    res = {}
    for mode, name in (("close", "close (last_price)"), ("hl", "hl (يرى الذيول)")):
        rs = []
        for (avg, stop, tp1, tp2, h, l, c, tch) in trades:
            r = sim_live(avg, stop, tp1, tp2, h, l, c, tch, hold, mode)
            if r is not None:
                rs.append(r)
        res[mode] = rs
        print(f"  {name:22s}: {S._stats(rs)}", flush=True)
    gap = sum(res["hl"]) - sum(res["close"])
    print(f"\n  فجوة الذيول (hl − close): {gap:+.1f}R  → كم يخسره الحيّ لأنه لا يرى الذيول",
          flush=True)


if __name__ == "__main__":
    run_frame()
