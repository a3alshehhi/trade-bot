#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست مقارنة فلتر الاتجاه لبوت العرض/الطلب — طلب بو محمد 2026-07-07.

الهدف: عزل أثر «فلتر الاتجاه» فقط (السعر فوق المتوسط) مع تثبيت كل شيء آخر،
ومقارنة ثلاث حالات على نفس الصفقات بالضبط لكل فريم:
  • الحالي  : المتوسط الأُسّي المستخدَم حياً (EMA365 على 15m، EMA200 على 1h)
  • EMA200  : متوسط أُسّي بطول 200 على الفريمين
  • SMA200  : متوسط بسيط بطول 200 على الفريمين

المنطق يعيد استخدام دوال sd_bot الحيّة (نفس المناطق، نفس الدخول DCA، نفس إدارة 50/50،
نفس الرسوم) فلا يتغيّر بين الحالات إلا شرط قبول الصفقة حسب المتوسط. هذا يجعل المقارنة
نظيفة: أي فرق في النتيجة سببه فلتر الاتجاه وحده.

يُشغَّل على GitHub Actions (الساندبوكس المحلي محجوب عن Binance). الفريم يُمرَّر عبر
SD_ENTRY_TF/SD_HTF مثل بقية أوضاع sd_bot.
"""
import os
import sys
import math
import time

import sd_bot as S


def sma(arr, n):
    """متوسط بسيط بنافذة n (نفس طول المصفوفة، NaN قبل اكتمال النافذة)."""
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
    cur_len = CFG["ema_len"]  # 365 على 15m / 200 على 1h (كما بناها sd_bot من الفريم)
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    groups = {"current": [], "ema200": [], "sma200": []}
    n_setups = 0        # إجمالي الصفقات المؤهلة (بعد كل الفلاتر عدا الاتجاه)
    n_symbols = 0

    print(f"trend-filter backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | "
          f"hold={hold} | current=EMA{cur_len} | max_ema_dist={CFG['max_ema_dist']}",
          flush=True)

    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = S.setup_features(s, d1, d4)
            ema_cur = S.ema(c, cur_len)
            ema_200 = S.ema(c, 200)
            sma_200 = sma(c, 200)
            n_symbols += 1

            for st in setups:
                f = st["f"]
                # ── نفس فلاتر backtest() ما عدا فلتر الاتجاه (emaRel) ──
                if f["htf"] < 0:                                    # سياق الفريم الأعلى غير هابط
                    continue
                if CFG["require_choch"] and not f["choch"]:
                    continue
                if CFG["require_ob_after_os"] and not f["rsiObOs"]:
                    continue
                if CFG["require_confirm"] and not f["confirm"]:
                    continue
                if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
                    continue

                tch = st["touch"]
                avg = S._dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"],
                                     h, l, c, tch, hold)
                r = S._sim_5050(avg, st["stop"], st["tp1"], st["tp2"], h, l, c, tch, hold)
                if r is None:
                    continue
                n_setups += 1

                cpx = c[tch]
                for name, series in (("current", ema_cur), ("ema200", ema_200), ("sma200", sma_200)):
                    m = series[tch]
                    if not (m and math.isfinite(m)):
                        continue
                    rel = (cpx - m) / m
                    if rel <= 0:                                   # السعر يجب أن يكون فوق المتوسط
                        continue
                    if CFG["max_ema_dist"] and rel > CFG["max_ema_dist"]:  # قريب لا متمدّد
                        continue
                    groups[name].append(r)
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    # ── التقرير ──
    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols} · صفقات مؤهلة (قبل فلتر الاتجاه)={n_setups}", flush=True)
    order = [("الحالي (EMA%d)" % cur_len, "current"),
             ("EMA200", "ema200"),
             ("SMA200", "sma200")]
    for label, key in order:
        print(f"  • {label:<16} : {S._stats(groups[key])}", flush=True)
    return {tf: {k: S._stats(v) for k, v in groups.items()}}


if __name__ == "__main__":
    run_frame()
