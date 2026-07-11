#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست «بوابة الحجم» لبوت العرض/الطلب — طلب بو محمد 2026-07-11.

الفكرة: بوت العرض/الطلب أصلاً يحسب z-score للحجم عند شمعة الدخول (touchVolZ)
وعند قاعدة المنطقة (baseVolZ)، لكنه لا يستخدمهما كفلتر. هذا السكربت يعزل أثر
«بوابة الحجم» وحدها: يبني نفس صفقات البوت الحيّة (نفس المناطق، نفس الدخول DCA،
نفس إدارة 50/50، نفس الرسوم)، ثم يصنّفها حسب عتبة الحجم ويقارن التوقّع.

يقارن على نفس الصفقات بالضبط لكل فريم:
  • بلا بوابة              (كل الصفقات — خط الأساس)
  • touchVolZ ≥ 1 / 1.5 / 2 / 3   (قفزة حجم على شمعة الدخول)
  • baseVolZ  ≥ 1 / 2            (حجم عالٍ عند تكوّن قاعدة العرض/الطلب)
  • مركّب: touchVolZ≥2 و baseVolZ≥1

هذا يجعل المقارنة نظيفة: أي فرق في النتيجة سببه بوابة الحجم وحدها، لأن كل
شيء آخر مُثبَّت. الذاكرة من v2 تلمّح أن الحافة تظهر عند z≥3‑4 وعلى الفريمات ≥1h
بينما 15m ضوضاء — هذا الباك-تست يتحقّق من ذلك على منطق العرض/الطلب الحالي.

يُشغَّل على GitHub Actions (الساندبوكس المحلي محجوب عن Binance). الفريم يُمرَّر
عبر SD_ENTRY_TF/SD_HTF مثل بقية أوضاع sd_bot.
"""
import os
import time

import sd_bot as S


# عتبات البوابة: (اسم للعرض، مفتاح الميزة، القيمة الدنيا)
GATES = [
    ("بلا بوابة",         None,          None),
    ("touchVolZ ≥ 1",     "touchVolZ",   1.0),
    ("touchVolZ ≥ 1.5",   "touchVolZ",   1.5),
    ("touchVolZ ≥ 2",     "touchVolZ",   2.0),
    ("touchVolZ ≥ 3",     "touchVolZ",   3.0),
    ("baseVolZ ≥ 1",      "baseVolZ",    1.0),
    ("baseVolZ ≥ 2",      "baseVolZ",    2.0),
]


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    groups = {name: [] for name, _, _ in GATES}
    groups["مركّب touchZ≥2 & baseZ≥1"] = []
    n_setups = 0
    n_symbols = 0

    print(f"volume-gate backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold}",
          flush=True)

    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = S.setup_features(s, d1, d4)
            n_symbols += 1

            for st in setups:
                f = st["f"]
                # ── نفس فلاتر backtest() الحيّة بالكامل (بلا تغيير) ──
                if f["emaRel"] <= 0 or f["htf"] < 0:
                    continue
                if CFG["max_ema_dist"] and f["emaRel"] > CFG["max_ema_dist"]:
                    continue
                if CFG["require_choch"] and not f["choch"]:
                    continue
                if CFG["require_ob_after_os"] and not f["rsiObOs"]:
                    continue
                if CFG["require_confirm"] and not f["confirm"]:
                    continue
                if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
                    continue

                # نفس محاكاة الدخول/الإدارة الحيّة (DCA + 50/50)
                tch = st["touch"]
                avg = S._dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"],
                                     h, l, c, tch, hold)
                r = S._sim_5050(avg, st["stop"], st["tp1"], st["tp2"], h, l, c, tch, hold)
                if r is None:
                    continue
                n_setups += 1

                tvz = f.get("touchVolZ", 0.0) or 0.0
                bvz = f.get("baseVolZ", 0.0) or 0.0

                # توزيع الصفقة على المجموعات التي تجتاز عتبتها
                for name, key, thr in GATES:
                    if key is None:
                        groups[name].append(r)
                    else:
                        val = tvz if key == "touchVolZ" else bvz
                        if val >= thr:
                            groups[name].append(r)
                if tvz >= 2.0 and bvz >= 1.0:
                    groups["مركّب touchZ≥2 & baseZ≥1"].append(r)
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    # ── التقرير ──
    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols} · صفقات مؤهلة (خط الأساس)={n_setups}", flush=True)
    order = [name for name, _, _ in GATES] + ["مركّب touchZ≥2 & baseZ≥1"]
    for name in order:
        rs = groups[name]
        share = (f" · نسبة الاحتفاظ={len(rs)/n_setups*100:.0f}%") if n_setups else ""
        print(f"  • {name:<26} : {S._stats(rs)}{share}", flush=True)
    return {tf: {name: S._stats(groups[name]) for name in order}}


if __name__ == "__main__":
    run_frame()
