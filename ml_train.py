# -*- coding: utf-8 -*-
"""
ml_train.py — يبني بيانات التدريب من نتائج الاستراتيجيتين الحيّتين نفسيهما
(reversal=اختراق RSI70 + trendwave) عبر باك-تست البوت، ثم يدرّب نموذج Gradient
Boosting يتعلّم تمييز الإشارات الرابحة من الخاسرة، ويحفظه في ml_model.joblib.

الصدق المنهجي:
  • الميزات تُحسب من بيانات سابقة فقط (داخل ml_filter.compute_features).
  • التقسيم زمني صارم: تدريب → تحقّق (لاختيار العتبة) → اختبار (للتقرير).
    لا تُختار العتبة ولا تُقاس النتيجة على بيانات رآها النموذج.

التشغيل:
  python3 ml_train.py --assets crypto --max-symbols 60 --bt-bars 1500
"""
import argparse
import sys
import numpy as np
import pandas as pd

import trading_bot as tb
import ml_filter as mlf

# الاستراتيجيتان الحيّتان فقط (مطابقة تماماً لما يشغّله reversal.yml):
#   • reversal  = اختراق RSI(21) عتبة 70 في وضع --mode reversal (backtest_symbol_rsi_cross)
#   • trendwave = موجة RSI 20→80 ثم نهايتها + فلتر فريم أعلى (backtest_symbol_trendwave)
# لا علاقة لها بـ RSI2 الكلاسيكي — كان مجرد اسم تسمية قديم مُضلِّل.
LIVE_SETUPS = [
    # (اسم, دالة الباك-تست, الفريمات, تعديلات cfg)
    ("reversal",  tb.backtest_symbol_rsi_cross, ["15m", "1h", "4h", "1d"],
     {"rsi_cross": True, "rsi_ob": 70.0, "bt_stop_mult": 2.0}),
    ("trendwave", tb.backtest_symbol_trendwave, ["15m", "1h", "4h"],
     {"trendwave": True, "rsi_os": 20.0, "rsi_ob": 80.0,
      "trail_buf": 0.5, "trail_arm": 1.0}),
]


def base_cfg():
    return {
        "timeframe": "1h", "min_score": 0, "assets": "crypto", "side": "buy",
        "bt_bars": 1500, "bt_hold": 40, "cost": 0.002, "bt_offset": 0,
        "rsi_ob": 70.0, "rsi_os": 20.0, "bt_stop_mult": 2.0,
        "trail_buf": 0.5, "trail_arm": 1.0, "force_direct": False,
        "no_div": False, "_df_cache": {},
    }


def gather(assets, max_symbols, bt_bars):
    """يجمع صفقات الاستراتيجيات الحيّة مع ميزاتها وتسمياتها."""
    parsed = tb.parse_watchlist("watchlist.txt")
    targets = []
    if assets in ("all", "stocks"):
        targets += [(it, "stock") for it in parsed["stocks"]]
    if assets in ("all", "crypto"):
        targets += [(it, "crypto") for it in parsed["crypto"]]
    if max_symbols:
        targets = targets[:max_symbols]

    cache = {}
    rows = []
    for name, bt_fn, tfs, over in LIVE_SETUPS:
        for tf in tfs:
            cfg = base_cfg()
            cfg.update(over)
            cfg["timeframe"] = tf
            cfg["bt_bars"] = bt_bars
            cfg["_df_cache"] = cache
            n_sig = 0
            for item, kind in targets:
                sym = item["symbol"] if isinstance(item, dict) else item
                try:
                    df = tb._bt_fetch_df(sym, kind, cfg)
                except Exception:
                    df = None
                if df is None or len(df) < 250:
                    continue
                df = df.reset_index(drop=True)
                try:
                    trades = bt_fn(item, kind, cfg)
                except Exception:
                    trades = []
                for t in trades:
                    bar = t.get("bar", t.get("lock_bar"))
                    if bar is None:
                        continue
                    feats = mlf.compute_features(df, int(bar), kind, t.get("side", "buy"))
                    if feats is None:
                        continue
                    rmg = t.get("R_managed")
                    if rmg is None:
                        continue
                    row = dict(feats)
                    row["_date"] = t.get("date")
                    row["_R"] = float(rmg)
                    row["_label"] = 1 if float(rmg) > 0 else 0
                    row["_strat"] = name
                    row["_tf"] = tf
                    row["_sym"] = sym
                    rows.append(row)
                    n_sig += 1
            print(f"  {name:9s} {tf:3s} → {n_sig} صفقة تراكمياً", flush=True)
    return pd.DataFrame(rows)


def expectancy(rs):
    return float(np.mean(rs)) if len(rs) else 0.0


def winrate(labels):
    return 100.0 * float(np.mean(labels)) if len(labels) else 0.0


def train_and_report(df, min_retain=0.35, assets="crypto"):
    """يدرّب النموذج على إطار صفقات يحمل الميزات + التسمية، ويطبع تقريراً
    صادقاً خارج العيّنة، ويحفظ ml_model.joblib. يرجع dict ملخّص."""
    if df.empty or len(df) < 200:
        raise SystemExit(f"\n⚠️ بيانات غير كافية للتدريب ({len(df)} صفقة).")

    df = df.dropna(subset=["_date"]).copy()
    df["_date"] = pd.to_datetime(df["_date"])
    df = df.sort_values("_date").reset_index(drop=True)

    base_wr = winrate(df["_label"].values)
    base_exp = expectancy(df["_R"].values)
    print(f"\nإجمالي الصفقات: {len(df)} | نسبة الرابحة: {base_wr:.1f}% | "
          f"التوقّع الأساسي: {base_exp:+.3f}R")

    X = df[mlf.FEATURE_NAMES].values.astype(float)
    y = df["_label"].values.astype(int)
    R = df["_R"].values.astype(float)

    # تقسيم زمني صارم 60/20/20
    n = len(df)
    i1, i2 = int(n * 0.60), int(n * 0.80)
    Xtr, ytr = X[:i1], y[:i1]
    Xva, yva, Rva = X[i1:i2], y[i1:i2], R[i1:i2]
    Xte, yte, Rte = X[i2:], y[i2:], R[i2:]

    from sklearn.ensemble import HistGradientBoostingClassifier
    model = HistGradientBoostingClassifier(
        max_depth=3, max_iter=250, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=25,
        early_stopping=True, validation_fraction=0.15, random_state=42,
    )
    model.fit(Xtr, ytr)

    # اختيار العتبة على بيانات التحقّق فقط: نعظّم التوقّع مع إبقاء نسبة صفقات معقولة
    pva = model.predict_proba(Xva)[:, 1]
    best_thr, best_exp = 0.5, -1e9
    for thr in np.linspace(0.30, 0.75, 46):
        keep = pva >= thr
        if keep.sum() < max(15, min_retain * len(pva)):
            continue
        e = expectancy(Rva[keep])
        if e > best_exp:
            best_exp, best_thr = e, float(thr)

    # تقرير صادق على الاختبار (لم يُرَ في التدريب ولا في اختيار العتبة)
    pte = model.predict_proba(Xte)[:, 1]
    keep = pte >= best_thr
    print("\n" + "-" * 64)
    print("  التحقّق خارج العيّنة (مجموعة الاختبار — الأحدث زمنياً)")
    print("-" * 64)
    print(f"  العتبة المختارة (من التحقّق): {best_thr:.3f}")
    print(f"  قبل الفلتر : {len(Rte)} صفقة | ربح {winrate(yte):.1f}% | توقّع {expectancy(Rte):+.3f}R | إجمالي {Rte.sum():+.1f}R")
    if keep.sum():
        print(f"  بعد الفلتر : {int(keep.sum())} صفقة | ربح {winrate(yte[keep]):.1f}% | "
              f"توقّع {expectancy(Rte[keep]):+.3f}R | إجمالي {Rte[keep].sum():+.1f}R")
        print(f"  نسبة الصفقات المُبقاة: {100*keep.mean():.0f}%")
    else:
        print("  بعد الفلتر : لا صفقات فوق العتبة (العتبة مرتفعة جداً).")

    # أهمية الميزات (تبادلية) على الاختبار — لتفسير ما تعلّمه النموذج
    try:
        from sklearn.inspection import permutation_importance
        imp = permutation_importance(model, Xte, yte, n_repeats=8, random_state=0)
        order = np.argsort(imp.importances_mean)[::-1][:8]
        print("\n  أهم الميزات التي اعتمد عليها النموذج:")
        for k in order:
            print(f"    {mlf.FEATURE_NAMES[k]:14s} {imp.importances_mean[k]:+.4f}")
    except Exception:
        pass

    # النموذج النهائي للنشر: يُدرَّب على (تدريب+تحقّق) بالعتبة المختارة
    final = HistGradientBoostingClassifier(
        max_depth=3, max_iter=250, learning_rate=0.05,
        l2_regularization=1.0, min_samples_leaf=25,
        early_stopping=True, validation_fraction=0.15, random_state=42,
    )
    final.fit(X[:i2], y[:i2])

    import joblib
    bundle = {
        "model": final, "threshold": best_thr,
        "feature_names": mlf.FEATURE_NAMES,
        "trained_on": str(pd.Timestamp.now())[:19],
        "n_samples": int(n), "assets": assets,
        "base_winrate": round(base_wr, 1), "base_expectancy": round(base_exp, 3),
    }
    joblib.dump(bundle, mlf.MODEL_PATH)
    print(f"\n✅ حُفظ النموذج: {mlf.MODEL_PATH}")
    print("   ملاحظة صادقة: الفلتر يقلّل الخاسرة بالحذف، لا يخلق إيدج من العدم.")
    return {"threshold": best_thr, "n": int(n),
            "test_exp_before": round(expectancy(Rte), 3),
            "test_exp_after": round(expectancy(Rte[keep]), 3) if keep.sum() else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="crypto", choices=["crypto", "stocks", "all"])
    ap.add_argument("--max-symbols", type=int, default=60)
    ap.add_argument("--bt-bars", type=int, default=1500)
    ap.add_argument("--min-retain", type=float, default=0.35,
                    help="أقل نسبة صفقات يجب الإبقاء عليها عند اختيار العتبة")
    args = ap.parse_args()

    print("=" * 64)
    print("  تدريب الفلتر التعلّمي — يتعلّم من نتائج الاستراتيجيات الحيّة")
    print("=" * 64)
    df = gather(args.assets, args.max_symbols, args.bt_bars)
    train_and_report(df, min_retain=args.min_retain, assets=args.assets)


if __name__ == "__main__":
    main()
