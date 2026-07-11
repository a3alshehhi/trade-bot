#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست مقارنة: «الدخول المباشر عند الدايفرجنس» مقابل «اختراق القمة (الوضع الحالي)»
— طلب بو محمد 2026-07-12.

الخلفية: بوت «عرض/طلب+دايفرجنس» الحيّ يكتشف دايفرجنساً صعودياً تحت SMA50، لكنه لا يدخل
عنده — بل ينتظر اختراق قمة الـCHoCH (entry_mode=breakout) فيدخل *فوق*، متأخّراً، ويطارد
الحركة (سبب دخول ANKRUSDT عند 0.00395 بدل مستوى الإشارة 0.00383).

هذه الأداة تعزل *توقيت/سعر الدخول* فقط: لكل حدث دايفرجنس صعودي (قاعان محوريان: قاع سعر
أدنى + قاع هيستوجرام MACD أعلى، وكلاهما مغلق تحت SMA(n))، نثبّت **نفس** الوقف و**نفس**
أهداف الفيبو، ونقارن ذراعين:

  • DIRECT   (المقترح): الدخول سوقاً عند إغلاق شمعة تأكيد الدايفرجنس (b + pivR) — تحت المتوسط.
  • BREAKOUT (الحالي) : الدخول عند كسر السعر القمة المحورية السابقة ph (بديل «قمة الـCHoCH»).

المستويات المشتركة (كلها من الفيبو، ومعروفة وقت الدخول — بلا نظر للمستقبل):
  قاع الدايفرجنس = l[b] ، القمة المرجعية ph = آخر قمة محورية قبل b ، المدى span = ph − l[b]
  الوقف = l[b] − stop_buf×ATR ، هدف1 = l[b] + 1.272×span ، هدف2 = l[b] + tp2_ext×span

يُبلّغ عن إحصاء كل ذراع منفصلاً + مجموعة مقترنة (الأحداث التي فعّلت الذراعين معاً) للمقارنة
صفقة-بصفقة. الإدارة 50/50 عبر S._sim_5050 (R خام بلا رسوم، مطابق لـ sd_bt_div).

يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance). الفريم عبر SD_ENTRY_TF/SD_HTF،
حجم السلّة عبر SD_BASKET، عتبة «تحت المتوسط» عبر SD_DIV_BELOW_MA، نافذة الاختراق عبر
SD_BRK_WINDOW.
"""
import os
import math
import time

import sd_bot as S


def _prior_pivot_high(piv, b_idx):
    """آخر قمة محورية (H) قبل قاع الدايفرجنس b — تُستخدم كقمة مرجعية للاختراق ولمدى الفيبو.
    يعيد (idx, price) أو None."""
    best = None
    for (pi, pp, pt) in piv:
        if pt == "H" and pi < b_idx:
            best = (pi, pp)
        elif pi >= b_idx:
            break
    return best


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    lookback = CFG["os_lookback"]          # نافذة إيجاد الدايفرجنس (شموع)
    pivR = CFG["pivR"]
    stop_buf = CFG["stop_buf_atr"]
    tp2_ext = CFG["tp2_ext"]
    div_below_ma = int(os.environ.get("SD_DIV_BELOW_MA", 50))   # 0 = تعطيل شرط تحت المتوسط
    brk_window = int(os.environ.get("SD_BRK_WINDOW", str(hold)))  # أقصى شموع لانتظار الاختراق
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    direct, breakout, pair_d, pair_b = [], [], [], []
    n_symbols = 0
    n_events = 0

    print(f"divnow backtest | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | "
          f"نافذة_دايفرجنس={lookback} | تحت SMA{div_below_ma} | نافذة_اختراق={brk_window}",
          flush=True)

    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            if not d1 or len(d1["c"]) < 800:
                continue
            h, l, c = d1["h"], d1["l"], d1["c"]
            a = S.atr(h, l, c, CFG["atr_len"])
            piv, _ = S.structure(h, l, c, CFG["pivL"], pivR)
            lows = [p[0] for p in piv if p[2] == "L"]
            _, _, hist = S.macd(c)
            ma = S.sma(c, div_below_ma) if div_below_ma else None
            n_symbols += 1

            # نمرّ على أزواج القيعان المحورية المتتالية (الأقدم a ثم الأحدث b)
            for k in range(1, len(lows)):
                a_, b_ = lows[k - 1], lows[k]
                if b_ - a_ > lookback:                       # القاعان بعيدان — خارج النافذة
                    continue
                if not math.isfinite(hist[a_]) or not math.isfinite(hist[b_]):
                    continue
                # دايفرجنس صعودي عادي: قاع سعر أدنى + قاع هيستوجرام أعلى
                if not (l[b_] < l[a_] and hist[b_] > hist[a_]):
                    continue
                # شرط «تحت المتوسط»: القاعان مغلقان تحت SMA(n)
                if ma is not None:
                    if not (math.isfinite(ma[a_]) and math.isfinite(ma[b_])
                            and c[a_] < ma[a_] and c[b_] < ma[b_]):
                        continue

                t0 = b_ + pivR                               # شمعة تأكيد القاع b (بلا نظر للمستقبل)
                if t0 >= len(c):
                    continue
                ph = _prior_pivot_high(piv, b_)              # قمة مرجعية للاختراق ومدى الفيبو
                if ph is None:
                    continue
                ph_idx, ph_px = ph
                if ph_idx + pivR > t0:                       # القمة المرجعية لم تتأكّد بعد وقت الدخول
                    continue

                span = ph_px - l[b_]
                if span <= 0:
                    continue
                atr0 = a[t0] if (a[t0] and a[t0] > 0) else span * 0.1
                stop = l[b_] - stop_buf * atr0               # مشترك للذراعين
                tp1 = l[b_] + 1.272 * span                   # مشترك — فيبو
                tp2 = l[b_] + tp2_ext * span                 # مشترك — فيبو (1.618)
                n_events += 1

                got_d = got_b = False
                rd = rb = None

                # ── الذراع DIRECT: دخول سوق عند إغلاق t0 (تحت المتوسط) ──
                ent_d = c[t0]
                if ent_d > stop and tp1 > ent_d:
                    rd = S._sim_5050(ent_d, stop, tp1, tp2, h, l, c, t0, hold)
                    if rd is not None:
                        direct.append(rd); got_d = True

                # ── الذراع BREAKOUT: انتظر كسر ph (أو يُبطل عند ضرب الوقف أولاً) ──
                if tp1 > ph_px and ph_px > stop:
                    end = min(len(c), t0 + brk_window)
                    for i in range(t0, end):
                        if l[i] <= stop:                     # انهار للوقف قبل الاختراق → لا صفقة
                            break
                        if h[i] >= ph_px:                    # اخترق القمة → دخول اختراق
                            rb = S._sim_5050(ph_px, stop, tp1, tp2, h, l, c, i, hold)
                            if rb is not None:
                                breakout.append(rb); got_b = True
                            break

                if got_d and got_b:                          # الحدث فعّل الذراعين → مقترن
                    pair_d.append(rd); pair_b.append(rb)
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    # ── التقرير ──
    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) — الدخول المباشر مقابل الاختراق ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols} · أحداث دايفرجنس صالحة={n_events}", flush=True)
    print(f"  • DIRECT (المقترح — دخول عند الدايفرجنس) : {S._stats(direct)}", flush=True)
    print(f"  • BREAKOUT (الحالي — اختراق القمة)        : {S._stats(breakout)}", flush=True)
    print("", flush=True)
    print(f"── المقارنة المقترنة (أحداث فعّلت الذراعين معاً؛ نفس الوقف/الأهداف) ──", flush=True)
    print(f"  • DIRECT  (مقترن) : {S._stats(pair_d)}", flush=True)
    print(f"  • BREAKOUT(مقترن) : {S._stats(pair_b)}", flush=True)
    if pair_d and pair_b:
        diff = (sum(pair_d) / len(pair_d)) - (sum(pair_b) / len(pair_b))
        print(f"  • فرق التوقّع (DIRECT − BREAKOUT) = {diff:+.3f}R على {len(pair_d)} صفقة مقترنة",
              flush=True)
    return {tf: {"direct": S._stats(direct), "breakout": S._stats(breakout),
                 "pair_direct": S._stats(pair_d), "pair_breakout": S._stats(pair_b)}}


if __name__ == "__main__":
    run_frame()
