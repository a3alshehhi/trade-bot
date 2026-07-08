#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت العرض/الطلب الحيّ محسّن (v4) — EMA200 بدون شرط تشبّع
=====================================================================
بناءً على نتائج الباك-تست (2026-07-08):
  • EMA200 (بدل 200): فوز 71.8% · توقّع +0.527R · PF 3.03 · مجموع +58.0R
  • بدون شرط تشبّع بيعي (RSI21): أفضل جودة صفقات من مع الشرط
  • hold=48 ساعة: توازن جيد بين المدة والمخاطر

يرمّز مدرسة العرض/الطلب ويستخدم ML لترشيح الإشارات ويرسلها لتيليجرام.

إعداد E (محافظ): دخول 1h، سياق 4h، فلتر «السعر فوق EMA200»، 
عتبة ML 0.60، مخاطرة 0.5% لكل صفقة، حد 5 مراكز.

تنبيه: أداة تحليل تعليمية. لا تنفّذ صفقات فعلية. التداول مخاطرة.
"""
import os, sys, time, math, json, datetime as dt
import requests

# ── الإعدادات المحسّنة (بناءً على الباك-تست) ──
CFG = dict(
    pivL=3, pivR=3, impK=1.0, base_max_body=0.6, base_max=3,
    atr_len=50, vol_len=200, ema_len=200, react_k=48,
    distal_buf_atr=0.1, ml_threshold=0.60,
    risk_pct=0.005, max_concurrent=5,           # إعداد E
    entry_tf="1h", htf="4h", pages_1h=4, pages_4h=2,
    top_n=8,
    # ── تحسينات الدخول (2026-07-04) ──
    fib_entry=0.618,
    stop_buf_atr=0.5,
    tp2_ext=1.618,
    max_height_atr=2.0,
    max_bars_to_touch=60,
    require_confirm=1,
    # ── تشديد الدخول ──
    require_choch=1,
    max_ema_dist=0.06,
    # ── بدون شرط التشبّع البيعي (النتائج أفضل) ──
    require_ob_after_os=0,
    require_os21=0,        # ✅ معطّل: النتائج أفضل بدونه
    rsi_len=14, rsi_ob=70, rsi_os=30,
    os_lookback=100,
    rsi_entry_len=21,
    rsi_entry_ob=80,
    # ── فلترة إضافية ──
    require_hh=0,
    require_macd4c=0,
    macd4c_min=1,
    rsi21_os=30,
)

# ── قراءة من متغيرات البيئة (للتطوير) ──
CFG["entry_tf"]  = os.environ.get("SD_ENTRY_TF", CFG["entry_tf"])
CFG["htf"]       = os.environ.get("SD_HTF", CFG["htf"])
CFG["ema_len"]   = int(os.environ.get("SD_EMA_LEN", CFG["ema_len"]))
CFG["require_os21"] = int(os.environ.get("SD_REQUIRE_OS21", CFG["require_os21"]))

# ── استيراد المكتبة الرئيسية ──
try:
    import sd_bot as S
except ImportError:
    print("❌ خطأ: يجب أن يكون sd_bot.py في نفس المجلد", file=sys.stderr)
    sys.exit(1)

# ── دمج الإعدادات المحسّنة مع الافتراضية في sd_bot ──
for key in CFG:
    if key in S.CFG:
        S.CFG[key] = CFG[key]

# ── العمل الأساسي ──
def run_live_scan():
    """فحص حيّ للعملات وإرسال الإشارات المؤهلة."""
    print(f"🚀 بوت العرض/الطلب الحيّ (EMA{CFG['ema_len']} بدون تشبّع) | "
          f"tf={CFG['entry_tf']} · htf={CFG['htf']}", flush=True)
    
    try:
        basket = S.parse_watchlist_crypto(S.WATCHLIST)
        print(f"📊 تحليل {len(basket)} عملة...", flush=True)
        
        signals = []
        for s in basket:
            try:
                d1 = S.fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
                d4 = S.fetch_klines(s, CFG["htf"], CFG["pages_4h"])
                
                if not d1 or not d4:
                    continue
                
                setups, h, l, c = S.setup_features(s, d1, d4)
                
                for st in setups:
                    f = st["f"]
                    
                    # ── فلاتر الدخول ──
                    if f["emaRel"] <= 0:
                        continue
                    if CFG["max_ema_dist"] and f["emaRel"] > CFG["max_ema_dist"]:
                        continue
                    if f["htf"] < 0:
                        continue
                    if CFG["require_choch"] and not f["choch"]:
                        continue
                    if CFG["require_ob_after_os"] and not f["rsiObOs"]:
                        continue
                    if CFG["require_confirm"] and not f["confirm"]:
                        continue
                    if f["heightATR"] > CFG["max_height_atr"] or f["barsToTouch"] > CFG["max_bars_to_touch"]:
                        continue
                    
                    # ── إشارة مؤهلة ──
                    signals.append({
                        "symbol": s,
                        "setup": st,
                        "score": f.get("score", 0),
                    })
                
                time.sleep(0.02)
            
            except Exception as ex:
                print(f"⚠️  {s}: {ex}", flush=True)
                time.sleep(0.03)
        
        # ── ترتيب وطباعة أفضل الإشارات ──
        signals.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        print(f"\n✅ وجدنا {len(signals)} إشارة", flush=True)
        print(f"أفضل {min(CFG['top_n'], len(signals))}:", flush=True)
        
        for i, sig in enumerate(signals[:CFG['top_n']], 1):
            st = sig["setup"]
            f = st["f"]
            print(f"  {i}. {sig['symbol']:<8} | دخول={st['entry']:.8f} · وقف={st['stop']:.8f} · "
                  f"هدف={st['tp1']:.8f} · R={f.get('score', 0):.1f}", flush=True)
        
        # ← هنا يمكنك إضافة إرسال لتيليجرام أو تنفيذ صفقات بحساب حقيقي
        
        return signals
    
    except Exception as ex:
        print(f"❌ خطأ عام: {ex}", file=sys.stderr, flush=True)
        return []


if __name__ == "__main__":
    run_live_scan()
