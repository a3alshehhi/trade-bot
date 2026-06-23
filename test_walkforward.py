"""اختبار نواة walk-forward: اختيار أفضل إعداد على IS وجمع OOS فقط، وعدم التسريب."""
import trading_bot as tb


def mk(date, r):
    return {"symbol": "X", "date": date, "R_managed": r, "out_managed":
            ("target" if r > 0 else "trail_stop")}


# سيناريو: إعدادان. A رابح في النصف الأول فقط (حظّ فترة)، B رابح باستمرار.
# نتوقّع أن walk-forward يختار B في النوافذ المتأخرة فيعطي حكماً صادقاً.
A = ([mk(f"2026-0{1+i//28}-{1+i%28:02d}", +1.0) for i in range(60)]   # يناير-فبراير ربح
     + [mk(f"2026-0{3+i//28}-{1+i%28:02d}", -1.0) for i in range(60)])  # مارس-أبريل خسارة
B = ([mk(f"2026-0{1+i//28}-{1+i%28:02d}", +0.3) for i in range(120)])   # ربح ثابت طوال الفترة

tbc = {(0.5, 1.0): A, (0.75, 1.0): B}
oos, folds = tb._walkforward_oos(tbc, folds=4, default_key=(0.5, 1.0), min_is=10)

print("=== نوافذ OOS ===")
for f in folds:
    print(f"  نافذة {f['fold']} [{f['from']}→{f['to']}] إعداد {f['combo']} "
          f"n={f['n']} توقّع={f['expectancy']} PF={f['pf']}")

# تحقّقات
combos_used = {f["combo"] for f in folds}
print("\nالإعدادات المختارة:", combos_used)

# 1) لا تسريب: مجموع صفقات OOS عبر النوافذ = عدد صفقات OOS الكلي (بلا تكرار/فجوات داخلية)
total_oos = sum(f["n"] for f in folds)
print("إجمالي صفقات OOS عبر النوافذ:", total_oos, "| oos_all:", len(oos))
assert total_oos == len(oos), "عدم تطابق العدّ بين النوافذ والإجمالي"

# 2) كل صفقة OOS تحمل وسم نافذتها وإعدادها
assert all("wf_fold" in t and "wf_combo" in t for t in oos), "وسم مفقود"

# 3) التكيّف: النافذة الأخيرة تتحوّل إلى B (الأكثر صموداً) بعد انهيار A
assert folds[-1]["combo"] == "0.75×1.0", \
    f"يُفترض التحوّل إلى B في النافذة الأخيرة، الفعلي: {folds[-1]['combo']}"
print("✅ المُحدِّد تحوّل إلى B في النافذة الأخيرة بعد ما انهار A")

# 4) الصدق: walk-forward يكشف نافذة OOS خاسرة على الأقل (ما ينخدع بفترة A الذهبية)
assert any((f["expectancy"] or 0) < 0 for f in folds), \
    "يُفترض أن تكشف إحدى نوافذ OOS خسارة (فضح الـoverfitting)"
print("✅ نافذة OOS واحدة على الأقل خاسرة — كُشف هشاشة A خارج العيّنة")

# 5) لا تسريب زمني: كل صفقة OOS تقع داخل حدود نافذتها
import pandas as pd
fb = {f["fold"]: (pd.Timestamp(f["from"]), pd.Timestamp(f["to"])) for f in folds}
for t in oos:
    lo, hi = fb[t["wf_fold"]]
    assert lo <= pd.Timestamp(t["date"]) <= hi, "صفقة خارج حدود نافذتها (تسريب)"
print("✅ كل صفقة داخل حدود نافذتها — لا تسريب زمني")

st = tb._stats([t["R_managed"] for t in oos], [t["out_managed"] for t in oos])
print("\nالتوقّع الإجمالي OOS:", st["expectancy"], "| PF:", st["profit_factor"])
print("✅ كل اختبارات walk-forward نجحت")
