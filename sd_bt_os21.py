#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست شرط «تشبّع بيعي على RSI(21)» كشرط دخول رئيسي — طلب بو محمد 2026-07-07.

الفكرة: لا تُقبل الصفقة إلا إذا كان RSI(21) قد نزل إلى التشبّع البيعي (≤ عتبة، مطابق للحيّ 30)
مرّة واحدة أو أكثر داخل نافذة قبل بداية موجة الـCHoCH (os_lookback شمعة). نعزل أثر هذا الشرط
وحده مع تثبيت كل شيء آخر (نفس فلاتر الدخول الحيّة، نفس المناطق، دخول DCA، إدارة 50/50، الرسوم)،
ونقارن على نفس الصفقات بالضبط لكل فريم:
  • بدون الشرط : خط الأساس = الدخول الحيّ كما هو اليوم (لا اشتراط تشبّع بيعي)
    • تشبّع ≥1   : يُشترط نزول RSI21 للتشبّع البيعي مرّة+ في النافذة قبل الموجة
      • تشبّع ≥2   : يُشترط نزوله للتشبّع البيعي مرّتين+ (نوبتان منفصلتان)
        • تشبّع ≥3   : ثلاث نوبات+

        «النوبة» = هبوط جديد إلى ≤ العتبة (انتقال من فوق العتبة/NaN إلى ≤ العتبة)، فلا تُحتسب
        شمعتان متتاليتان تحت العتبة نوبتين.

        يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance). الفريم عبر SD_ENTRY_TF/SD_HTF،
        والعتبة عبر SD_RSI21_OS (=30 مطابقة للحيّ)، والنافذة عبر SD_OS_LOOKBACK.
        """
import os
import math
import time

import sd_bot as S


def count_os_dips(rs_en, lo, hi, thr):
      """عدد نوبات التشبّع البيعي المنفصلة في [lo, hi] على RSI: انتقالات جديدة إلى ≤ thr."""
      dips = 0
      prev_below = False
      for x in range(lo, hi + 1):
                v = rs_en[x]
                below = math.isfinite(v) and v <= thr
                if below and not prev_below:
                              dips += 1
                          prev_below = below
            return dips


def run_frame():
      CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    thr = CFG["rsi21_os"]            # عتبة التشبّع البيعي على RSI21 (=30 مطابقة للحيّ)
    lookback = CFG["os_lookback"]    # نافذة البحث قبل بداية الموجة
    rsi_len = CFG["rsi_entry_len"]   # طول RSI للدخول (=21)
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    groups = {"base": [], "ge1": [], "ge2": [], "ge3": []}
    n_symbols = 0

    print(f"os21 backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | "
                    f"RSI{rsi_len}≤{thr:.0f} | نافذة={lookback} | ema_dist={CFG['max_ema_dist']}",
                    flush=True)

    for s in basket:
              try:
                            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
                            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
                            if not d1 or not d4 or len(d1["c"]) < 800:
                                              continue
                                          setups, h, l, c = S.setup_features(s, d1, d4)
                            rs_en = S.rsi(c, rsi_len)
                            n_symbols += 1

                  for st in setups:
                                    f = st["f"]
                                    # ── نفس فلاتر الدخول الحيّة (backtest) بالضبط — الاتجاه مثبّت على الحيّ ──
                                    if f["emaRel"] <= 0:                              # فوق فلتر الاتجاه الحيّ (EMA)
                                        continue
                                                      if CFG["max_ema_dist"] and f["emaRel"] > CFG["max_ema_dist"]:
                                                                            continue
                                                                        if f["htf"] < 0:                                  # سياق الفريم الأعلى غير هابط
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
                                                                                                            groups["base"].append(r)

                                                                                          # ── عدّ نوبات التشبّع البيعي على RSI21 في النافذة قبل بداية الموجة ──
                                                                                          j = st["created"]
                                                                                          lo = max(1, j - lookback)
                                                                                          dips = count_os_dips(rs_en, lo, j, thr)
                                                                                          if dips >= 1:
                                                                                                                groups["ge1"].append(r)
                                                                                                            if dips >= 2:
                                                                                                                                  groups["ge2"].append(r)
                                                                                                                              if dips >= 3:
                                                                                                                                                    groups["ge3"].append(r)
                                                                          except Exception as ex:
                                                                                      print("skip", s, ex, flush=True)
                                                                                  time.sleep(0.03)

                                                                              # ── التقرير ──
                                                                              print("", flush=True)
                                                                              print(f"════ نتائج الفريم {tf} (سياق {htf}) ════", flush=True)
                                                                              print(f"رموز مُحلَّلة={n_symbols} · صفقات خط الأساس={len(groups['base'])}", flush=True)
                                                                              order = [(f"بدون الشرط (أساس)", "base"),
                                                                                       (f"تشبّع ≥1 (RSI{rsi_len}≤{thr:.0f})", "ge1"),
                                                                                       (f"تشبّع ≥2", "ge2"),
                                                                                       (f"تشبّع ≥3", "ge3")]
                                                                              for label, key in order:
                                                                                        print(f"  • {label:<22} : {S._stats(groups[key])}", flush=True)
                                                                                    return {tf: {k: S._stats(v) for k, v in groups.items()}}


                                                                          if __name__ == "__main__":
                                                                                run_frame()
