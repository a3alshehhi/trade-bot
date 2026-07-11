#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
باك-تست «بوابة الحجم» لبوت العرض/الطلب — طلب بو محمد 2026-07-11،
وُسِّع 2026-07-12 بفحوص متانة ضد الـ overfitting.

الفكرة: بوت العرض/الطلب أصلاً يحسب z-score للحجم عند شمعة الدخول (touchVolZ)
وعند قاعدة المنطقة (baseVolZ)، لكنه لا يستخدمهما كفلتر. هذا السكربت يعزل أثر
«بوابة الحجم» وحدها: يبني نفس صفقات البوت الحيّة (نفس المناطق، نفس الدخول DCA،
نفس إدارة 50/50، نفس الرسوم)، ثم يصنّفها حسب عتبة الحجم ويقارن التوقّع.

النتائج الأولى (2026-07-11) كانت قوية جداً (تضاعُف التوقّع)، وهذا بالضبط ما
يوجب فحص المتانة: تحسين كبير مع تقليل عنيف للصفقات قد يكون صدفة عيّنة صغيرة.
لذلك أُضيفت ثلاثة فحوص:

  (1) مونوتونية العتبة  — عتبات وسطية (1.25/1.75/2.5) لكشف إن كان التحسّن
       سلساً مع رفع العتبة (حافة حقيقية) أم قفزة بسطل واحد (ضجيج/overfitting).
  (2) holdout بتقسيم الرموز — نقسم السلة لنصفين منفصلين من الرموز (A/B) ونفحص
       إن كانت البوابة الفائزة تتفوق على خط الأساس في النصفين معاً. تفوّق في
       نصف واحد فقط = إفراط في المطابقة.
  (3) تركيبات AND — دمج touchVolZ≥1.5 و baseVolZ≥2 لمعرفة إن كان الدمج يشدّ
       الحافة أم يفرط بالتصفية حتى تنهار العيّنة.

كل مقارنة على نفس الصفقات بالضبط لكل فريم؛ أي فرق سببه بوابة الحجم وحدها لأن
كل شيء آخر مُثبَّت. يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance).
الفريم يُمرَّر عبر SD_ENTRY_TF/SD_HTF مثل بقية أوضاع sd_bot.
"""
import os
import time

import sd_bot as S


# ── عتبات مفردة (مونوتونية): اسم، مفتاح الميزة، القيمة الدنيا ──
# أُضيفت العتبات الوسطية 1.25/1.75/2.5 لفحص سلاسة التحسّن.
GATES = [
    ("بلا بوابة",          None,          None),
    ("touchVolZ ≥ 1",      "touchVolZ",   1.0),
    ("touchVolZ ≥ 1.25",   "touchVolZ",   1.25),
    ("touchVolZ ≥ 1.5",    "touchVolZ",   1.5),
    ("touchVolZ ≥ 1.75",   "touchVolZ",   1.75),
    ("touchVolZ ≥ 2",      "touchVolZ",   2.0),
    ("touchVolZ ≥ 2.5",    "touchVolZ",   2.5),
    ("touchVolZ ≥ 3",      "touchVolZ",   3.0),
    ("baseVolZ ≥ 1",       "baseVolZ",    1.0),
    ("baseVolZ ≥ 1.5",     "baseVolZ",    1.5),
    ("baseVolZ ≥ 2",       "baseVolZ",    2.0),
    ("baseVolZ ≥ 2.5",     "baseVolZ",    2.5),
]

# ── تركيبات AND (دمج الفائزين) ──
# كل عنصر: اسم، دالة شرط تأخذ (tvz, bvz) وتُعيد True/False
COMBOS = [
    ("مركّب touchZ≥1.5 & baseZ≥2",  lambda t, b: t >= 1.5 and b >= 2.0),
    ("مركّب touchZ≥2 & baseZ≥1",    lambda t, b: t >= 2.0 and b >= 1.0),
    ("مركّب touchZ≥1.5 | baseZ≥2",  lambda t, b: t >= 1.5 or b >= 2.0),
]


def _gate_pass(name, key, thr, tvz, bvz):
    if key is None:
        return True
    val = tvz if key == "touchVolZ" else bvz
    return val >= thr


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]
    htf = CFG["htf"]
    hold = CFG["bt_hold"]
    limit = int(os.environ.get("SD_BASKET", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    # نجمع لكل صفقة: (r, tvz, bvz, split) حيث split ∈ {A, B} حسب موقع الرمز في السلة.
    # التقسيم بالتناوب (زوجي/فردي) يمنع سيطرة رمز واحد على نصف بعينه ويوزّع
    # ظروف السوق عبر النصفين بالتساوي.
    trades = []
    n_setups = 0
    n_symbols = 0

    print(f"volume-gate backtest SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold}",
          flush=True)

    for si, s in enumerate(basket):
        split = "A" if si % 2 == 0 else "B"
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
                trades.append((r, tvz, bvz, split))
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    # ── مساعدات التقرير ──
    def collect_single(name, key, thr, subset=None):
        return [r for (r, t, b, sp) in trades
                if (subset is None or sp == subset) and _gate_pass(name, key, thr, t, b)]

    def collect_combo(fn, subset=None):
        return [r for (r, t, b, sp) in trades
                if (subset is None or sp == subset) and fn(t, b)]

    def line(label, rs, denom):
        share = (f" · احتفاظ={len(rs)/denom*100:.0f}%") if denom else ""
        print(f"  • {label:<28} : {S._stats(rs)}{share}", flush=True)

    # ── (0) الجدول الكامل — مونوتونية العتبات ──
    print("", flush=True)
    print(f"════ نتائج الفريم {tf} (سياق {htf}) ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols} · صفقات مؤهلة (خط الأساس)={n_setups}", flush=True)
    print("── (1) مونوتونية العتبات (كل الصفقات) ──", flush=True)
    for name, key, thr in GATES:
        line(name, collect_single(name, key, thr), n_setups)

    # ── (3) تركيبات AND ──
    print("── (3) تركيبات AND/OR للفائزين ──", flush=True)
    for name, fn in COMBOS:
        line(name, collect_combo(fn), n_setups)

    # ── (2) holdout بتقسيم الرموز ──
    # نفحص خط الأساس + بوابتَي الفائز المتوقّعتين (touchVolZ≥1.5 و baseVolZ≥2)
    # على النصفين A و B منفصلين. الحافة الحقيقية تظهر في النصفين معاً.
    print("── (2) holdout بتقسيم الرموز (A زوجي / B فردي) ──", flush=True)
    key_gates = [
        ("بلا بوابة",       None,        None),
        ("touchVolZ ≥ 1.5", "touchVolZ", 1.5),
        ("touchVolZ ≥ 2",   "touchVolZ", 2.0),
        ("baseVolZ ≥ 2",    "baseVolZ",  2.0),
    ]
    for subset in ("A", "B"):
        base_n = len(collect_single("بلا بوابة", None, None, subset))
        print(f"  ▸ النصف {subset} (خط أساس={base_n} صفقة):", flush=True)
        for name, key, thr in key_gates:
            rs = collect_single(name, key, thr, subset)
            line("   " + name, rs, base_n)

    # للعودة البرمجية إن لزم
    return {tf: {name: S._stats(collect_single(name, key, thr))
                 for name, key, thr in GATES}}


if __name__ == "__main__":
    run_frame()
