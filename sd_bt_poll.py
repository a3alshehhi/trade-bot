#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست فجوة اللقطة (polling gap) — طلب بو محمد 2026-07-09.

الاكتشاف: الإدارة الحيّة في sd_autotrade.py تقرأ bx.last_price (لقطة واحدة كل 15 دقيقة)،
بينما الباك-تست يمشي على قمم/قيعان الشموع. فتُفوَّت ذيول +1R/هدف1/الوقف اللحظية. تسليح +1R
موجود حيّاً (السطر 530) لكنه لا يُطلَق لأن اللقطة لا ترى الذيل.

هذا السكربت يشغّل *نفس آلة حالة الخروج الحيّة بالضبط* (وقف → هدف1 50% + قفل LOCK_R →
هدف2 → تسليح +1R قبل الهدف1 + تتبّع price−R) تحت ثلاثة أوضاع لكل صفقة:

  close   = كل المشغّلات والملء على إغلاق الشمعة c[i]  (محاكاة last_price لحظة الفحص — الواقع الحالي)
  armhigh = كشف تسليح +1R عبر قمة الشمعة h[i]، لكن كل الملء (وقف/هدف/بيع) سوقيّ على c[i]
            (المسار (أ): يقرأ القمة ليُسلّح الوقف مبكراً ثم يبيع سوقياً على النزول — قابل للتحقيق بلا أوامر راكدة)
  hl      = المشغّلات الصاعدة والملء على القمة h[i] والوقف على القاع l[i]  (سقف نظري يتطلّب أوامر راكدة)

الفروق: armhigh−close = المكسب القابل للتحقيق فعلاً؛ hl−armhigh = الجزء الذي يتطلّب أوامر راكدة.
الدخول مثبّت (نفس setup_features + الفلاتر + DCA).
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
    """يحاكي آلة حالة الخروج الحيّة (sd_autotrade._manage_one) شمعةً شمعة تحت ثلاثة أوضاع.
       الترتيب داخل كل فحص مطابق للحيّ: وقف → هدف1 → هدف2 → تسليح/تتبّع. الناتج بوحدات R.
       الملء واقعيّ (سوقيّ على c[i]) في close/armhigh؛ وعند مستوى الأمر في hl فقط."""
    R = avg - stop
    if R <= 0:
        return None
    if (tp1 - avg) / R <= 0:
        return None
    hl = (mode == "hl")
    end = min(len(c), tch + hold)
    sl = stop
    tp1_done = False
    armed = False
    realized = 0.0
    for i in range(tch, end):
        cpx = c[i]
        stop_px = l[i] if hl else cpx            # كشف الوقف
        up = h[i] if hl else cpx                 # كشف الهدف
        arm_px = h[i] if mode in ("armhigh", "hl") else cpx   # كشف تسليح +1R
        # (1) الوقف أولاً — الملء عند مستوى الوقف في hl، وسوقيّ (c[i]) خلاف ذلك
        if stop_px <= sl:
            fill = sl if hl else cpx
            frac = 0.5 if tp1_done else 1.0
            return realized + frac * ((fill - avg) / R)
        # (2) الهدف الأول: بيع 50% + قفل
        if not tp1_done and up >= tp1:
            fill = tp1 if hl else cpx
            realized += 0.5 * ((fill - avg) / R)
            tp1_done = True
            armed = True
            lock = avg + LOCK_R * R
            if (tp1 - avg) / R <= LOCK_R:
                lock = avg
            sl = max(sl, lock)
        # (3) الهدف الثاني للنصف الباقي
        if tp1_done and up >= tp2:
            fill = tp2 if hl else cpx
            return realized + 0.5 * ((fill - avg) / R)
        # (4) تسليح +1R قبل الهدف1 (يُكتشف بالقمة في armhigh/hl) + تتبّع
        if not armed and arm_px >= avg + R:
            armed = True
            lock = avg + LOCK_R * R
            if lock > sl:
                sl = lock
        if armed:
            trail = (h[i] if hl else cpx) - R    # التتبّع سوقيّ (c[i]) خارج hl
            if trail > sl:
                sl = trail
    fill = c[end - 1]                            # إغلاق زمني (سوقيّ)
    frac = 0.5 if tp1_done else 1.0
    return realized + frac * ((fill - avg) / R)


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
    for mode, name in (("close", "close (last_price الحالي)"),
                       ("armhigh", "armhigh (كشف+1R بالقمة)"),
                       ("hl", "hl (سقف نظري)")):
        rs = []
        for (avg, stop, tp1, tp2, h, l, c, tch) in trades:
            r = sim_live(avg, stop, tp1, tp2, h, l, c, tch, hold, mode)
            if r is not None:
                rs.append(r)
        res[mode] = rs
        print(f"  {name:26s}: {S._stats(rs)}", flush=True)
    achievable = sum(res["armhigh"]) - sum(res["close"])
    ceiling = sum(res["hl"]) - sum(res["armhigh"])
    print(f"\n  المكسب القابل للتحقيق (armhigh − close): {achievable:+.1f}R  ← المسار (أ)",
          flush=True)
    print(f"  الجزء الذي يتطلّب أوامر راكدة (hl − armhigh): {ceiling:+.1f}R",
          flush=True)


if __name__ == "__main__":
    run_frame()
