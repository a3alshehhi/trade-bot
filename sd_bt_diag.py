#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تشخيص جودة الدخول للنظام (بدل إضافة فلاتر) — طلب بو محمد 2026-07-08.

يشغّل خط الأساس نفسه (نفس فلاتر الدخول الحيّة + فلتر الاتجاه المختار SD_TREND_MA)، ثم:

(أ) تحليل MAE/MFE:
    لكل صفقة نحسب — بوحدات المخاطرة R (R = متوسط الدخول − الوقف) — على فترة الاحتفاظ الفعلية:
      MAE = أقصى حركة ضدّك (كم R غاصت تحت الدخول قبل أن تشتغل)
      MFE = أقصى حركة معك (كم R ارتفعت فوق الدخول)
    ثم نوزّعها للرابحة والخاسرة لنعرف موضوعياً:
      • هل الرابحة تغوص كثيراً قبل أن تعمل ⇒ الدخول مبكّر (الأفضل انتظار إعادة اختبار)؟
      • هل الخاسرة تعطي ربحاً عائماً قبل أن تفشل ⇒ الهدف بعيد/الإدارة تترك ربحاً؟

(ب) فحص تسريب المستقبل (point-in-time replay):
    لعيّنة من الصفقات نعيد اكتشاف الإعداد على بيانات مقطوعة عند شمعة الدخول فقط
    (لا شيء بعد tch)، ونتحقّق هل يظهر نفس الإعداد بنفس (created, touch, entry).
    نسبة إعادة إنتاج عالية ⇒ الدخول سليم زمنياً بلا تسريب؛ منخفضة ⇒ تسريب مستقبل.

الفريم عبر SD_ENTRY_TF/SD_HTF، فلتر الاتجاه عبر SD_TREND_MA، حجم عيّنة الفحص عبر SD_LEAK_SAMPLE.
يُشغَّل على GitHub Actions (الساندبوكس محجوب عن Binance).
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


def build_ma(c, ma_kind, ma_len):
    if ma_kind == "sma":
        return sma(c, ma_len)
    if ma_kind == "ema":
        return S.ema(c, ma_len)
    return None


def pct(sorted_vals, q):
    """المئين q (0..1) من قائمة مرتّبة."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def base_filter_ok(f, c, ma, tch, max_dist, CFG):
    """نفس فلاتر الدخول الحيّة + فلتر الاتجاه المختار. يعيد True إن قُبلت الصفقة."""
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


def walk_mae_mfe(avg, stop, tp1, tp2, h, l, c, tch, hold):
    """يعيد (r, mae_R, mfe_R) على فترة الاحتفاظ الفعلية بمنطق 50/50 نفسه (الوقف أولاً تحفّظاً)."""
    R = avg - stop
    if R <= 0:
        return None
    r1, r2 = (tp1 - avg) / R, (tp2 - avg) / R
    if r1 <= 0:
        return None
    end = min(len(c), tch + hold)
    half = False; sl = stop
    mae = 0.0; mfe = 0.0
    r_out = None
    for i in range(tch, end):
        mae = max(mae, (avg - l[i]) / R)      # أقصى غوص تحت الدخول
        mfe = max(mfe, (h[i] - avg) / R)      # أقصى ارتفاع فوق الدخول
        if l[i] <= sl:
            r_out = 0.5 * r1 if half else -1.0
            break
        if not half and h[i] >= tp1:
            half = True; sl = avg
        if half and h[i] >= tp2:
            r_out = 0.5 * r1 + 0.5 * r2
            break
    if r_out is None:
        r_last = (c[end - 1] - avg) / R
        r_out = (0.5 * r1 + 0.5 * r_last) if half else r_last
    return r_out, mae, mfe


def replay_ok(sym, d1, d4, j0, tch0, entry0, ma_kind, ma_len, max_dist, CFG):
    """يعيد اكتشاف الإعداد على بيانات مقطوعة عند tch0، ويتحقّق من ظهور نفس (created, touch, entry).
       entry0 = سعر دخول الإعداد الأصلي (قبل توسيط DCA). None = عيّنة قصيرة تُتخطّى."""
    cut = tch0 + 1
    if cut < 500:
        return None
    d1t = {k: v[:cut] for k, v in d1.items()}
    cutoff = d1["t"][tch0]
    keep = sum(1 for tt in d4["t"] if tt <= cutoff)
    if keep < 60:
        return None
    d4t = {k: v[:keep] for k, v in d4.items()}
    try:
        setups, h, l, c = S.setup_features(sym, d1t, d4t)
    except Exception:
        return None
    ma = build_ma(c, ma_kind, ma_len)
    for st in setups:
        if st["created"] != j0 or st["touch"] != tch0:
            continue
        if not base_filter_ok(st["f"], c, ma, st["touch"], max_dist, CFG):
            continue
        if abs(st["entry"] - entry0) <= 1e-6 * max(1.0, abs(entry0)):
            return True
    return False


def run_frame():
    CFG = S.CFG
    tf = CFG["entry_tf"]; htf = CFG["htf"]; hold = CFG["bt_hold"]
    max_dist = CFG["max_ema_dist"]
    ma_kind, ma_len = parse_trend_ma()
    ma_label = "live-EMA" if ma_kind == "live" else f"{ma_kind.upper()}{ma_len}"
    limit = int(os.environ.get("SD_BASKET", "40"))
    leak_sample = int(os.environ.get("SD_LEAK_SAMPLE", "40"))
    basket = S.parse_watchlist_crypto(S.WATCHLIST)[:limit]

    print(f"diag SD | tf={tf} htf={htf} | {len(basket)} رمز | hold={hold} | فلتر اتجاه={ma_label}",
          flush=True)

    trades = []          # (sym, j, tch, entry_orig, avg, stop, tp1, tp2)
    store = {}           # sym -> (d1, d4)
    for s in basket:
        try:
            d1 = S.fetch_klines(s, tf, CFG["pages_1h"])
            d4 = S.fetch_klines(s, htf, CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = S.setup_features(s, d1, d4)
            ma = build_ma(c, ma_kind, ma_len)
            store[s] = (d1, d4)
            for st in setups:
                if not base_filter_ok(st["f"], c, ma, st["touch"], max_dist, CFG):
                    continue
                tch = st["touch"]
                avg = S._dca_average(st.get("legs", [st["entry"]]), st["stop"], st["tp1"],
                                     h, l, c, tch, hold)
                if not (avg - st["stop"] > 0):
                    continue
                trades.append((s, st["created"], tch, st["entry"], avg,
                               st["stop"], st["tp1"], st["tp2"]))
        except Exception as ex:
            print("skip", s, ex, flush=True)
        time.sleep(0.03)

    # ── (أ) MAE / MFE ──
    win_mae, win_mfe, los_mfe, los_mae = [], [], [], []
    n_win = n_los = n_scr = 0
    for (s, j, tch, ent0, avg, stop, tp1, tp2) in trades:
        d1, _ = store[s]
        h, l, c = d1["h"], d1["l"], d1["c"]
        res = walk_mae_mfe(avg, stop, tp1, tp2, h, l, c, tch, hold)
        if res is None:
            continue
        r, mae, mfe = res
        if r > 0.05:
            n_win += 1; win_mae.append(mae); win_mfe.append(mfe)
        elif r < -0.05:
            n_los += 1; los_mfe.append(mfe); los_mae.append(mae)
        else:
            n_scr += 1

    def dist(name, vals):
        v = sorted(vals)
        if not v:
            print(f"    {name}: لا بيانات", flush=True); return
        print(f"    {name}: n={len(v)} · وسيط={pct(v,0.5):.2f}R · p75={pct(v,0.75):.2f}R · "
              f"p90={pct(v,0.90):.2f}R · أقصى={v[-1]:.2f}R", flush=True)

    def share(vals, thr):
        return (sum(1 for x in vals if x >= thr) / len(vals) * 100) if vals else float("nan")

    print("", flush=True)
    print(f"════ تشخيص الفريم {tf} (سياق {htf}) — فلتر {ma_label} ════", flush=True)
    print(f"صفقات={len(trades)} · رابحة={n_win} · خاسرة={n_los} · متعادلة={n_scr}", flush=True)
    print("  MAE (غوص ضدّك قبل الحسم):", flush=True)
    dist("الرابحة", win_mae)
    dist("الخاسرة", los_mae)
    print(f"    نسبة الرابحة التي غاصت ≥0.5R={share(win_mae,0.5):.0f}% · ≥0.8R={share(win_mae,0.8):.0f}% · "
          f"≥1.0R={share(win_mae,1.0):.0f}%", flush=True)
    print("  MFE (ارتفاع معك):", flush=True)
    dist("الرابحة", win_mfe)
    dist("الخاسرة", los_mfe)
    print(f"    نسبة الخاسرة التي أعطت ربحاً عائماً ≥0.5R={share(los_mfe,0.5):.0f}% · ≥1.0R={share(los_mfe,1.0):.0f}%",
          flush=True)

    # ── (ب) فحص التسريب ──
    print("  فحص تسريب المستقبل (إعادة تشغيل عند نقطة الدخول):", flush=True)
    elig = [t for t in trades if t[2] >= 500]
    if not elig:
        print("    لا عيّنة كافية (tch<500).", flush=True)
        return
    step = max(1, len(elig) // max(1, leak_sample))
    sample = elig[::step][:leak_sample]
    ok = bad = skip = 0
    for (s, j, tch, ent0, avg, stop, tp1, tp2) in sample:
        d1, d4 = store[s]
        res = replay_ok(s, d1, d4, j, tch, ent0, ma_kind, ma_len, max_dist, CFG)
        if res is None:
            skip += 1
        elif res:
            ok += 1
        else:
            bad += 1
    tested = ok + bad
    rate = (ok / tested * 100) if tested else float("nan")
    print(f"    عيّنة={len(sample)} · مُختبرة={tested} · أُعيد إنتاجها={ok} · فشلت={bad} · "
          f"تُخطّيت={skip} · نسبة الإعادة={rate:.0f}%", flush=True)
    if tested:
        if rate >= 90:
            print("    ⇒ الدخول سليم زمنياً (لا تسريب مادّي).", flush=True)
        elif rate >= 70:
            print("    ⇒ تسريب جزئي محتمل — يستحق فحصاً أعمق.", flush=True)
        else:
            print("    ⇒ تسريب مستقبل مرجّح — النتائج متفائلة.", flush=True)


if __name__ == "__main__":
    run_frame()
