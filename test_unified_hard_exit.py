# -*- coding: utf-8 -*-
"""اختبار مرحلة الخروج الفوري داخل السجل الموحّد (simulate_real) — بيانات صناعية، بلا شبكة."""
from datetime import datetime, timezone
import unified_log as ul

STEP = 900_000  # 15m
# إثنين 00:00 UTC (مرساة أسبوع ISO) لضمان تجمّع الفيواب على أسبوع واحد
BASE = int(datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _series():
    """تصحيح هابط ثم تعافٍ (بوابة خضراء) ثم تراجع يقلب الهيستوجرام أحمر وإغلاق ~99."""
    closes, highs, lows = [], [], []
    p = 112.0
    for _ in range(40):
        p -= 0.35
        closes.append(p); highs.append(p + 0.3); lows.append(p - 0.3)
    for p in [98.6, 99.6, 100.8, 102.0, 103.2, 104.4, 105.4, 106.0]:
        closes.append(p); highs.append(p + 0.4); lows.append(p - 0.3)
    for p in [105.2, 104.0, 102.6, 101.2, 100.2, 99.4, 99.0]:
        closes.append(p); highs.append(p + 0.3); lows.append(p - 0.4)
    bars = []
    for k in range(len(closes)):
        o = closes[k - 1] if k else closes[0]
        bars.append([BASE + k * STEP, o, highs[k], lows[k], closes[k], 1000.0])
    return bars


BARS = _series()


def main():
    # نُثبّت الشموع الصناعية بدل جلب الشبكة (klines يُرجع نفس السلسلة أياً كان start)
    ul.klines = lambda symbol, interval, start_ms, limit=1000: BARS
    entry, stop, targets = 100.0, 95.0, [112.0, 116.0]
    start_ms = BARS[0][0]

    # (1) وسم مشمول بالخروج الفوري → يجب أن تُغلق الصفقة بسبب «خروج فوري»
    r = ul.simulate_real("XUSDT", "15m", entry, stop, targets, start_ms, 100.0,
                         label="العرض/الطلب")
    print(f"[العرض/الطلب] status={r['status']} reason={r['exit_reason']} "
          f"exit={r['exit_price']} net%={r['net_pct']}")
    assert r["status"] == "closed", "متوقّع إغلاق الصفقة"
    assert r["exit_reason"] == "خروج فوري (تحت الفيواب+أحمر)", "متوقّع سبب الخروج الفوري"
    assert any(e["type"] == "hard_exit" for e in r["events"]), "متوقّع حدث hard_exit"

    # (2) وسم غير مشمول (SD Stable) → لا خروج فوري (تبقى مفتوحة: لا وقف/هدف تحقّق)
    r2 = ul.simulate_real("XUSDT", "15m", entry, stop, targets, start_ms, 100.0,
                          label="SD Stable")
    print(f"[SD Stable] status={r2['status']} reason={r2['exit_reason']}")
    assert not any(e["type"] == "hard_exit" for e in r2["events"]), \
        "الوسوم غير المشمولة يجب ألا تُطبّق الخروج الفوري"

    print("\n✅ الخروج الفوري داخل السجل الموحّد يعمل على البوتات المشمولة فقط.")


if __name__ == "__main__":
    main()
