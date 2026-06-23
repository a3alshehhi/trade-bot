"""تحقّق أن الاستراتيجيات الكلاسيكية الثلاث تفتح صفقات على بيانات مُصطنعة مناسبة
(بلا شبكة، عبر حقن كاش _bt_fetch_df) — للتأكد أن المحرّكات سليمة قبل التشغيل الحقيقي."""
import numpy as np, pandas as pd
import trading_bot as tb


def frame(close):
    close = np.asarray(close, float)
    n = len(close)
    dates = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": 1000.0})


def run(name, flag_cfg, close):
    df = frame(close)
    bars = 1400
    cfg = {"timeframe": "4h", "bt_bars": bars, "bt_hold": 60, "cost": 0.0,
           "_df_cache": {("S", "crypto", "4h", bars, 0): df}}
    cfg.update(flag_cfg)
    fn = {"donchian": tb.backtest_symbol_donchian,
          "ema_cross": tb.backtest_symbol_ema_cross,
          "rsi2": tb.backtest_symbol_rsi2}[name]
    trades = fn({"symbol": "S", "raw": "S"}, "crypto", cfg)
    print(f"{name}: {len(trades)} صفقة" +
          (f" | مثال: دخول {trades[0]['date']} R={trades[0]['R_managed']} ({trades[0]['out_managed']})"
           if trades else ""))
    assert len(trades) >= 1, f"❌ {name} لم يفتح أي صفقة على بيانات مناسبة!"
    # تحقّق أن R منطقي (ضمن نطاق معقول)
    for t in trades:
        assert -3 <= t["R_managed"] <= 20, f"R غير منطقي: {t['R_managed']}"
    return trades


# اتجاه صاعد طويل مع تذبذب: مناسب لـ Donchian و EMA cross (اختراقات + تقاطع)
up = 200 + np.cumsum(np.r_[np.ones(700)*0.15, np.sin(np.linspace(0, 30, 700))*1.5 + 0.15])
run("donchian", {"donchian": True, "don_entry": 20, "don_exit": 10}, up)
run("ema_cross", {"ema_cross": True, "ema_fast": 20, "ema_slow": 50}, up)

# اتجاه صاعد حادّ مع نقرات هبوط قصيرة تبقى فوق متوسط 200: مناسب لـ RSI(2)
base = np.linspace(100, 460, 1400)       # ميل حادّ ⇒ متوسط 200 أدنى بكثير
series = base.copy()
for k in range(300, 1380, 90):           # نقرتا هبوط متتاليتان ثم تعافٍ
    series[k] = series[k - 1] - 7
    series[k + 1] = series[k] - 7
rsi_series = series
run("rsi2", {"rsi2": True, "rsi2_buy": 10.0}, rsi_series)

print("\n✅ المحرّكات الثلاثة سليمة: كلٌّ يفتح صفقات على البيانات المناسبة، وR ضمن نطاق منطقي.")
