#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست «كسر بنية داخلية» (MSB) كزناد دخول أسرع بديل عن اختراق قمة الـCHoCH الخارجية
— طلب بو محمد 2026-07-10.

الفكرة: عند سؤاله "شو الأسرع لمعرفة كسر البنية وتغيير الاتجاه؟" اقتُرحت فكرة SMC الشائعة:
بنية خارجية (القمم/القيعان الكبيرة، pivL=3/pivR=3 — نفس ما يستخدمه البوت الحيّ لتأكيد
CHoCH وصلاحية المنطقة) مقابل بنية داخلية (قمم/قيعان صغيرة بفراكتال أضيق، مثلاً pivL=1/pivR=1)
تُستخدم كزناد دخول أبكر ضمن نفس الموجة، مع إبقاء صلاحية المنطقة/الاتجاه بالبنية الخارجية.

المقارنة (على نفس المناطق، نفس فلاتر الدخول الحيّة، نفس الوقف/الأهداف/الإدارة 50/50):
  • أساس (خارجي) : الدخول الحيّ كما هو — اختراق قمة الـCHoCH الخارجية (pivL/pivR كبيرة)
  • MSB (داخلي)  : الدخول عند أول كسر بنية داخلية صعودية بعد بداية الموجة (pivL_in/pivR_in صغيرة)
                   نفس قاع الموجة (خارجي) للوقف، وأهداف فيبو مبنية على leg_high الجديد عند لحظة
                   الدخول الداخلي (peak حتى تلك اللحظة).
يُقاس أيضاً "bars_saved" = فرق التوقيت بالشموع بين الدخولين على نفس المنطقة (موجب = MSB أبكر).

فلتر الاتجاه قابل للاختيار عبر SD_TREND_MA، فراكتال البنية الداخلية عبر SD_MSB_PIVL/SD_MSB_PIVR
(الافتراضي 1/1). يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance).
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


def any_break_high_levels(c, piv, R):
    """يعيد dict: {مؤشّر شمعة → سعر القمة المحورية المكسورة صعوداً} — أي كسر بنية صعودي
    (بلا شرط الانعكاس CHoCH: BOS أو CHoCH كلاهما يُحتسب). هذا هو "MSB" حين يُطبَّق على
    بنية داخلية بفراكتال ضيق (pivL_in/pivR_in صغيرة)."""
    ref_h = None
    pidx = 0
    out = {}
    for i in range(len(c)):
        while pidx < len(piv) and piv[pidx][0] + R <= i:
            p = piv[pidx]
            pidx += 1
            if p[2] == "H":
                ref_h = p
        if ref_h and c[i] > ref_h[1]:
            out[i] = ref_h[1]
            ref_h = None
    return out


def msb_entry(z, h, l, c, a, low_idx, internal_hi, stop_buf, use_dca, dca_fibs, tp2_ext):
    """نظير _entry_plan لكن الزناد = أول كسر بنية داخلية بعد بداية الموجة، بدل اختراق
    قمة الـCHoCH الخارجية. الوقف/الأهداف بنفس منطق البوت الحيّ (قاع الموجة الخارجية،
    امتدادات فيبو لساق leg_low→leg_high عند لحظة الدخول)."""
    j = z["created"]
    leg_low = S._wave_low(z, l, low_idx)
    run_high = max(z["proximal"], h[j])
    tch = -1
    entry = leg_high = 0.0
    for i in range(j + 1, len(c)):
        if h[i - 1] > run_high:
            run_high = h[i - 1]
        if i in internal_hi:
            tch = i
            leg_high = max(run_high, h[i])
            entry = internal_hi[i]
            break
    if tch < 0:
        return None
    atch = a[tch] or (leg_high - leg_low)
    stop = leg_low - stop_buf * atch
    if not (entry - stop > 0):
        return None
    span = leg_high - leg_low
    if use_dca:
        ladder = [leg_high - lv * span for lv in dca_fibs]
        legs = [entry] + [p for p in ladder if stop < p < entry]
    else:
        legs = [entry]
    _exts = [leg_low + m * span for m in (1.272, tp2_ext, 2.0, 2.618)]
    _above = [x for x in _exts if x > entry]
    tp1, tp2 = (_above[0], _above[1]) if len(_above) >= 2 else (entry + 0.618 * span, entry + span)
    return dict(tch=tch, entry=entry, stop=stop, tp1=tp1, tp2=tp2, legs=legs)


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    max_dist = CFG["max_ema_dist"]
    ma_kind, ma_len = parse_trend_ma()
    ma_label = "live-EMA" if ma_kind == "live" else f"{ma_kind.upper()}{ma_len}"
    pivL_in = int(os.environ.get("SD_MSB_PIVL", "1"))
    pivR_in = int(os.environ.get("SD_MSB_PIVR", "1"))
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    base_rs, msb_rs = [], []
    bars_saved = []       # touch_خارجي − touch_داخلي على نفس المنطقة (كلا الدخولين صالحان)
    n_symbols = 0
    n_base_only = n_msb_only = n_both = 0

    print(f"msb backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | "
          f"فلتر اتجاه={ma_label} | بنية داخلية pivL={pivL_in} pivR={pivR_in}", flush=True)

    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = S.setup_features(s, d1, d4)
            o = d1["o"]; v = d1["v"]
            a = S.atr(h, l, c, CFG["atr_len"])
            piv_ext, _ = S.structure(h, l, c, CFG["pivL"], CFG["pivR"])
            low_idx = [p[0] for p in piv_ext if p[2] == "L"]     # قيعان محورية خارجية (لقاع الموجة)
            piv_in, _ = S.structure(h, l, c, pivL_in, pivR_in)
            internal_hi = any_break_high_levels(c, piv_in, pivR_in)
            zones = S.demand_zones(o, h, l, c, v, a)
            ma = None
            if ma_kind == "sma":
                ma = sma(c, ma_len)
            elif ma_kind == "ema":
                ma = S.ema(c, ma_len)
            n_symbols += 1

            for z in zones:
                # نفس فلاتر صلاحية المنطقة المستخدمة بالدخول الحيّ (عبر مطابقة setups بنفس created)
                st = next((x for x in setups if x["created"] == z["created"]), None)
                if st is None:
                    continue
                f = st["f"]
                tch_ext = st["touch"]
                if ma is None:
                    rel = f["emaRel"]
                else:
                    m = ma[tch_ext]
                    if not math.isfinite(m) or m <= 0:
                        continue
                    rel = (c[tch_ext] - m) / m
                if rel <= 0 or (max_dist and rel > max_dist):
                    continue
                if f["htf"] < 0:
                    continue
                if CFG["require_choch"] and not f["choch"]:
                    continue
                if CFG["require_confirm"] and not f["confirm"]:
                    continue
                if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
                    continue

                # ── الأساس (خارجي): نفس دخول setup_features الحيّ ──
                avg_b = S._dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"],
                                        h, l, c, tch_ext, hold)
                r_b = S._sim_5050(avg_b, st["stop"], st["tp1"], st["tp2"], h, l, c, tch_ext, hold) \
                    if (avg_b is not None and avg_b - st["stop"] > 0) else None
                if r_b is not None:
                    base_rs.append(r_b)

                # ── MSB (داخلي): زناد دخول بديل ──
                plan = msb_entry(z, h, l, c, a, low_idx, internal_hi, CFG["stop_buf_atr"],
                                  CFG["use_dca"], CFG["dca_fibs"], CFG["tp2_ext"])
                r_m = None
                if plan is not None:
                    avg_m = S._dca_average(plan["legs"], plan["stop"], plan["tp1"],
                                            h, l, c, plan["tch"], hold)
                    if avg_m is not None and avg_m - plan["stop"] > 0:
                        r_m = S._sim_5050(avg_m, plan["stop"], plan["tp1"], plan["tp2"],
                                           h, l, c, plan["tch"], hold)
                        if r_m is not None:
                            msb_rs.append(r_m)

                if r_b is not None and r_m is not None:
                    n_both += 1
                    bars_saved.append(tch_ext - plan["tch"])
                elif r_b is not None:
                    n_base_only += 1
                elif r_m is not None:
                    n_msb_only += 1
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) — فلتر اتجاه {ma_label} — "
          f"بنية داخلية pivL={pivL_in}/pivR={pivR_in} ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols}", flush=True)
    print(f"  • أساس (خارجي CHoCH)      : {S._stats(base_rs)}", flush=True)
    print(f"  • MSB (بنية داخلية)       : {S._stats(msb_rs)}", flush=True)
    print(f"  مطابقة على نفس المنطقة: كلاهما صالح={n_both} · أساس فقط={n_base_only} · "
          f"MSB فقط={n_msb_only}", flush=True)
    if bars_saved:
        avg_saved = sum(bars_saved) / len(bars_saved)
        earlier = sum(1 for x in bars_saved if x > 0) / len(bars_saved) * 100
        print(f"  توقيت الدخول (على المطابقة، +يعني MSB أبكر): متوسط={avg_saved:+.1f} شمعة · "
              f"MSB أبكر في {earlier:.0f}% من الحالات", flush=True)
    else:
        print("  لا مطابقة كافية لقياس فرق التوقيت.", flush=True)
    return {tf: {"base": S._stats(base_rs), "msb": S._stats(msb_rs)}}


if __name__ == "__main__":
    run_frame()
