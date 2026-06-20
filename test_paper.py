#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""اختبارات منطق إدارة الصفقة الورقية (جني جزئي + breakeven) وحساب R.
شغّلها محلياً: python test_paper.py"""
import pandas as pd
import paper


def _mk(bars):  # bars: [(date, high, low), ...]
    return pd.DataFrame([{"date": d, "open": h, "high": h, "low": l,
                          "close": (h + l) / 2, "volume": 1} for d, h, l in bars])


def _run(name, bars, expect):
    # تُضاف شمعة "جارية" أخيرة تُستبعَد داخل _update_trade عبر iloc[:-1]
    df = _mk(bars + [("9999", 200, 200)])
    paper.fetch_binance = lambda *a, **k: df
    tr = paper._open_trade_from_signal("T1", {
        "symbol": "TST", "label": "انعكاس 4h كلاسيكي", "strategy": "classic",
        "timeframe": "4h", "entry": 100.0, "stop": 90.0,
        "targets": [110.0, 120.0, 130.0], "bar_ts": "0000"})
    paper._update_trade(tr, None, None)
    got = tr["result_R"]
    ok = (got is not None and abs(got - expect) < 1e-6) or (got is None and expect is None)
    print(f"{'PASS' if ok else 'FAIL'} {name}: result={got} expect={expect} "
          f"hits={tr['hits']} status={tr['status']}")
    assert ok, name


if __name__ == "__main__":
    # risk = 100-90 = 10 ؛ R_i = (target_i-100)/10 → 1,2,3
    _run("كل الأهداف", [("1", 111, 101), ("2", 121, 109), ("3", 131, 118)],
         0.5 * 1.0 + 0.25 * 2.0 + 0.25 * 3.0)            # = 1.75R
    _run("وقف خسارة كامل", [("1", 101, 89)], -1.0)
    _run("هدف أول ثم تعادل", [("1", 111, 101), ("2", 105, 100)], 0.5 * 1.0)  # 0.5R
    _run("لا تزال مفتوحة", [("1", 105, 96)], None)
    print("\n✅ كل الاختبارات نجحت")
