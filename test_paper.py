#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""اختبارات منطق إدارة الصفقة الورقية (هجينة: تعادل + وقف متحرّك) وحساب النتيجة بالنسبة المئوية.
شغّلها محلياً: python test_paper.py

النتيجة = (سعر الخروج − الدخول) ÷ الدخول × 100. الدخول 100 والوقف 90 (مخاطرة 10%).
يُضاف حشو محايد (30 شمعة) لتجاوز حد الـ 30 شمعة الأدنى داخل _update_trade،
وشمعة "جارية" أخيرة تُستبعَد عبر iloc[:-1].
"""
import pandas as pd
import paper

PAD = [(f"p{i:03d}", 101, 100) for i in range(30)]  # حشو لا يلمس الوقف(90) ولا الهدف(110)


def _mk(bars):  # bars: [(date, high, low), ...]
    return pd.DataFrame([{"date": d, "open": h, "high": h, "low": l,
                          "close": (h + l) / 2, "volume": 1} for d, h, l in bars])


def _run(name, ev, expect):
    df = _mk(PAD + ev + [("9999", 200, 200)])
    paper.fetch_binance = lambda *a, **k: df
    tr = paper._open_trade_from_signal("T1", {
        "symbol": "TST", "label": "انعكاس 4h كلاسيكي", "strategy": "classic",
        "timeframe": "4h", "entry": 100.0, "stop": 90.0,
        "targets": [110.0, 120.0, 130.0], "bar_ts": "0000"})
    paper._update_trade(tr, None, None)
    got = tr["result_pct"]
    ok = (got is not None and abs(got - expect) < 1e-6) or (got is None and expect is None)
    print(f"{'PASS' if ok else 'FAIL'} {name}: result={got}% expect={expect} status={tr['status']}")
    assert ok, name


if __name__ == "__main__":
    # وقف خسارة كامل قبل أي هدف: الخروج عند 90 → (90-100)/100 = -10%
    _run("وقف خسارة كامل", [("e1", 101, 89)], -10.0)
    # بلوغ الهدف الأول (رفع الوقف للتعادل) ثم هبوط يضرب التعادل: الخروج عند 100 → 0%
    _run("هدف أول ثم تعادل", [("e1", 111, 101), ("e2", 105, 90)], 0.0)
    # لا وقف ولا انعكاس → الصفقة تبقى مفتوحة (لا نتيجة)
    _run("لا تزال مفتوحة", [("e1", 105, 96)], None)
    print("\n✅ كل الاختبارات نجحت")
