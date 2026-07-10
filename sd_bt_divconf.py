#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست «دخول عند شمعة تأكّد الدايفرجنس الإيجابي» — طلب بو محمد 2026-07-10، بعد إسقاط
تجربة MSB (بنية داخلية) لأنها فشلت في السرعة والأداء.

الفكرة الجديدة: بدل ما ننتظر تكوّن منطقة عرض/طلب كاملة + CHoCH + اختراق قمّته (دخول متأخر
بعيد عن القاع)، نجرّب الدخول **مباشرة عند لحظة تأكّد الدايفرجنس نفسها** — أي عند أول شمعة
يصبح فيها القاع المحوري الثاني (الأحدث) مؤكَّداً هيكلياً (بعد pivR شمعة يمين)، بغضّ النظر عن
أي منطقة/CHoCH لاحقة. هذا نظام مستقل تماماً عن بنية العرض/الطلب (`demand_zones`/CHoCH) —
اختبار مباشر لسؤال: هل الدخول القريب من القاع (لحظة تأكّد الدايفرجنس) أفضل من الدخول
المتأخر بعد الاختراق؟

تعريف الدايفرجنس نفسه **مطابق تماماً** لما هو مستخدم بالحيّ (`sd_bot.py`/`sd_bt_div.py`):
آخر قاعين محوريين للسعر (بنية خارجية pivL/pivR — نفس الافتراضي 3/3 المستخدم لصلاحية
المناطق)، قاع سعر أدنى + قاع هيستوجرام MACD أعلى، وبشرط «تحت SMA(n)» عبر SD_DIV_BELOW_MA
(الافتراضي 50) — بالضبط كما أُصلح ورُفع اليوم.

**زناد الدخول:** شمعة تأكّد القاع الثاني = index(القاع) + pivR (أول شمعة يصير فيها القاع
معروفاً هيكلياً كقاع محوري). الدخول = إغلاق تلك الشمعة (لا اشتراط شمعة صعودية إضافية —
تفسير حرفي لطلب بو محمد).

**الوقف:** القاع الثاني نفسه − stop_buf×ATR (نفس معامل الوقف المستخدم بالحيّ).
**الأهداف (فيبو دائماً، لا R-multiples):** ساق الهبوط = آخر قمة محورية قبل القاع الأول →
القاع الثاني، وامتدادات 1.272/1.618/2.0/2.618 لهذه الساق فوق الدخول (أول امتدادين صالحين).

**ملاحظة تصميم:** هذا النظام **لا** يشترط فلتر «فوق المتوسط» (SD_TREND_MA) كبقية الأنظمة،
لأن الدايفرجنس بحكم شرط «تحت SMA(n)» يعني السعر غالباً تحت المتوسط لحظة التأكّد — تطبيق
شرط «فوقه» هنا يُصفّر العيّنة عملياً. الفلتر الوحيد المُبقى: السياق الأعلى (htf) غير هابط.

يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance). الفريم عبر SD_ENTRY_TF/SD_HTF.
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


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    stop_buf = CFG["stop_buf_atr"]
    tp2_ext = CFG["tp2_ext"]
    pivL, pivR = CFG["pivL"], CFG["pivR"]           # نفس البنية الخارجية المستخدمة لصلاحية المناطق
    div_below_ma = int(os.environ.get("SD_DIV_BELOW_MA", "50"))
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    rs = []
    n_symbols = 0
    n_candidates = 0   # قاعان محوريان يحقّقان الدايفرجنس (قبل فلاتر htf/الوقف/الهدف)

    print(f"divconf backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | "
          f"pivL={pivL}/pivR={pivR} | تحت SMA{div_below_ma}", flush=True)

    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            h, l, c, t = d1["h"], d1["l"], d1["c"], d1["t"]
            a = S.atr(h, l, c, CFG["atr_len"])
            piv, _ = S.structure(h, l, c, pivL, pivR)
            lows_idx = [p[0] for p in piv if p[2] == "L"]
            highs = [(p[0], p[1]) for p in piv if p[2] == "H"]     # مرتّبة تصاعدياً (piv مرتّبة)
            _, _, hist = S.macd(c)
            div_ma = sma(c, div_below_ma) if div_below_ma else None
            hb = S.htf_bias_fn(d4)
            n_symbols += 1

            for k in range(1, len(lows_idx)):
                a_idx, b_idx = lows_idx[k - 1], lows_idx[k]     # الأقدم، ثم الأحدث
                if not (math.isfinite(hist[a_idx]) and math.isfinite(hist[b_idx])):
                    continue
                if not (l[b_idx] < l[a_idx] and hist[b_idx] > hist[a_idx]):
                    continue
                if div_ma is not None:
                    if not (math.isfinite(div_ma[a_idx]) and math.isfinite(div_ma[b_idx])
                            and c[a_idx] < div_ma[a_idx] and c[b_idx] < div_ma[b_idx]):
                        continue
                n_candidates += 1

                tch = b_idx + pivR          # شمعة تأكّد القاع الثاني = زناد الدخول
                if tch >= len(c):
                    continue
                if hb(t[tch]) < 0:          # السياق الأعلى غير هابط (الفلتر الوحيد المُبقى)
                    continue

                prior_hi = None             # آخر قمة محورية قبل القاع الأول (ساق الهبوط)
                for (pi, pp) in highs:
                    if pi < a_idx:
                        prior_hi = pp
                    else:
                        break
                if prior_hi is None:
                    continue

                leg_low = l[b_idx]
                span = prior_hi - leg_low
                if span <= 0:
                    continue
                entry = c[tch]
                atch = a[tch] or span
                stop = leg_low - stop_buf * atch
                if not (entry - stop > 0):
                    continue

                exts = [leg_low + m * span for m in (1.272, tp2_ext, 2.0, 2.618)]
                above = [x for x in exts if x > entry]
                if len(above) >= 2:
                    tp1, tp2 = above[0], above[1]
                elif len(above) == 1:
                    tp1, tp2 = above[0], entry + span
                else:
                    continue

                r = S._sim_5050(entry, stop, tp1, tp2, h, l, c, tch, hold)
                if r is not None:
                    rs.append(r)
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) — دخول عند تأكّد الدايفرجنس ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols} · مرشّحو الدايفرجنس (قبل فلاتر htf/الوقف/الهدف)={n_candidates}",
          flush=True)
    print(f"  • دخول عند تأكّد الدايفرجنس : {S._stats(rs)}", flush=True)
    return {tf: S._stats(rs)}


if __name__ == "__main__":
    run_frame()
