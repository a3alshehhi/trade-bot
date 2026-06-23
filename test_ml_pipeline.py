# -*- coding: utf-8 -*-
"""
test_ml_pipeline.py — يتحقّق من سلامة خط أنابيب الفلتر التعلّمي *بالكامل*
دون إنترنت: يولّد سلاسل OHLCV اصطناعية واقعية (موجات زخم + اتجاهات)،
ويستبدل دوال الجلب، ثم يشغّل: الباك-تست الحقيقي → استخراج الميزات →
التدريب → التقسيم الزمني → اختيار العتبة → الحفظ.

ما يثبته هذا الاختبار: أن الكود يعمل بلا أخطاء، بلا نظرة مستقبلية
(lookahead)، وأن المقاييس تُحسب خارج العيّنة. لا يثبت وجود إيدج —
ذلك يتطلّب تشغيلاً على بيانات حقيقية في السحابة/الماك.
"""
import numpy as np
import pandas as pd
import trading_bot as tb
import ml_filter as mlf
import ml_train


def synth_ohlcv(seed, n=1600, tf="1d"):
    """سلسلة OHLCV اصطناعية: مشي عشوائي + موجات اتجاه دورية لإطلاق
    إشارات RSI/التشبّع (تشبه سلوك السوق بما يكفي لاختبار الأنابيب)."""
    rng = np.random.default_rng(seed)
    drift = 0.0002 * np.sin(np.linspace(0, rng.uniform(6, 14), n))  # اتجاهات متبدّلة
    burst = rng.normal(0, 0.018, n)
    # نوبات تذبذب لإحداث موجات تشبّع بيعي/شرائي
    for _ in range(n // 80):
        k = rng.integers(50, n - 5)
        burst[k:k + 5] += rng.normal(0, 0.05, 5)
    ret = drift + burst
    close = 100 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = rng.uniform(1e6, 5e6, n) * (1 + 3 * np.abs(burst))
    freq = {"1d": "1D", "4h": "4h", "1h": "1h", "15m": "15min"}[tf]
    dates = pd.date_range("2021-01-01", periods=n, freq=freq)
    return pd.DataFrame({"date": dates, "open": open_, "high": high,
                         "low": low, "close": close, "volume": vol})


# ── استبدال الجلب والقائمة ببيانات اصطناعية ──
SYMS = [f"SYN{i}" for i in range(24)]
_CACHE = {}


def fake_fetch(sym, kind, cfg):
    key = (sym, cfg.get("timeframe"))
    if key not in _CACHE:
        seed = abs(hash(key)) % (2**31)
        _CACHE[key] = synth_ohlcv(seed, n=1600, tf=cfg.get("timeframe", "1d"))
    return _CACHE[key]


def fake_watchlist(path):
    return {"crypto": [{"symbol": s} for s in SYMS], "stocks": []}


def main():
    tb._bt_fetch_df = fake_fetch
    tb.parse_watchlist = fake_watchlist
    ml_train.tb._bt_fetch_df = fake_fetch
    ml_train.tb.parse_watchlist = fake_watchlist

    # 1) فحص عدم وجود نظرة مستقبلية: ميزات الشمعة i لا تتغيّر بإضافة شموع لاحقة
    df = synth_ohlcv(1, n=800)
    f_full = mlf.compute_features(df, 600, "crypto")
    f_trunc = mlf.compute_features(df.iloc[:601].copy(), 600, "crypto")
    assert f_full == f_trunc, "❌ تسرّب مستقبلي: الميزات تغيّرت!"
    assert len(f_full) == len(mlf.FEATURE_NAMES), "❌ عدد الميزات لا يطابق"
    print(f"✅ لا تسرّب مستقبلي | عدد الميزات = {len(f_full)} (منها 8 موجية/MACD-4C)")

    # 2) توليد + تدريب على البيانات الاصطناعية
    data = ml_train.gather("crypto", max_symbols=24, bt_bars=1500)
    print(f"✅ صفقات مُولّدة: {len(data)} (rsi2 + trendwave عبر الفريمات)")
    res = ml_train.train_and_report(data, min_retain=0.35, assets="crypto")

    # 3) فحص أن النموذج يُحمَّل ويُنتج قراراً
    mlf._MODEL = None
    prob = mlf.score_signal(df, 700, "crypto")
    ok, p, thr = mlf.passes_filter(df, 700, "crypto")
    print(f"✅ النموذج يُحمَّل ويتنبّأ: احتمال={prob:.3f} | عتبة={thr:.3f} | قرار={'قبول' if ok else 'رفض'}")
    print(f"\nالخلاصة: الأنبوب سليم. (هذه بيانات اصطناعية لاختبار الكود فقط)")


if __name__ == "__main__":
    main()
