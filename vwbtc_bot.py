#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت «الفيواب الأسبوعي — نسخة BTC»
=================================
نسخة مستقلّة من استراتيجية الفيواب الأسبوعي، تضيف فوق الإشارة الحيّة نفسها:
  1) فلتر نظام السوق (BTC صاعد + استحواذ USDT هابط، بنية قمم/قيعان، أغلبية ≥ SD_REGIME_MIN من 4 فريمات).
  2) «رتّب-ثم-اختر»: يمسح كل العملات (لا أول 60 أبجدياً) ويأخذ أفضل top_n بجودة الإشارة (عائد/مخاطرة).

مهم: هذا الملف **لا يعدّل scan_vwave الرئيسي إطلاقاً** — يعيد استخدام دوال sd_bot الحيّة فقط،
فيبقى بوت الفيواب الرئيسي مطابقاً 100%. العزل (وسم/دفتر/حالة) عبر SD_LABEL/SD_TRACK/SD_STATE
التي تُقرأ في sd_bot عند الاستيراد (تُضبط في reversal.yml قبل تشغيل بايثون).

إشارات فقط — بلا تنفيذ (يُشغَّل بـ SD_EXECUTE=0). يعتمد على regime.json الذي ينتجه fetch_regime.py.
"""
import os, json, bisect, time
from concurrent.futures import ThreadPoolExecutor

import sd_bot
from sd_bot import (fetch_klines, parse_watchlist_crypto, WATCHLIST, CFG,
                    vwave_signal, tp1_too_close, send_telegram,
                    format_message_vwave, track_for_dashboard,
                    load_state, save_state)

REGIME_MIN = int(os.environ.get("SD_REGIME_MIN", "2"))       # أغلبية الفريمات المطلوبة (من 4)
WORKERS    = int(os.environ.get("SD_SCAN_WORKERS", "8"))
REGIME_PATH = os.environ.get("SD_REGIME_FILE", "regime.json")


def regime_favorable():
    """يعيد (عدد الفريمات المؤاتية أو None, نص تفصيلي) عند اللحظة الحالية حسب regime.json.
    الفريم مؤاتٍ = BTC صاعد (+1) و USDT.D هابط (-1). None = تعذّر تحميل النظام (نُكمل بلا حجب)."""
    try:
        now = int(time.time() * 1000)
        rg = json.load(open(REGIME_PATH))
        fav = 0; det = []
        for tf in rg["meta"]["tfs"]:
            b = rg[tf]
            jb = bisect.bisect_right(b["t"], now) - 1
            ju = bisect.bisect_right(b["usdtd_t"], now) - 1
            btc = b["btc"][jb] if jb >= 0 else 0
            usd = b["usdtd"][ju] if ju >= 0 else 0
            ok = (btc == 1 and usd == -1)
            fav += 1 if ok else 0
            det.append(f"{tf}:{'✓' if ok else '✗'}(btc{btc:+d}/usdt{usd:+d})")
        return fav, " ".join(det)
    except Exception as e:
        return None, f"regime unavailable: {e}"


def scan():
    basket = parse_watchlist_crypto(WATCHLIST)        # كل العملات — يكسر الانحياز الأبجدي
    state = load_state(); sent = set(state.get("sent", []))

    fav, det = regime_favorable()
    print(f"[vwbtc] النظام: مؤاتٍ={fav}/4 (الحدّ={REGIME_MIN}) | {det}", flush=True)
    if fav is None:
        print("[vwbtc] بيانات النظام غير متاحة (regime.json) — تخطّي الدورة (نبقى محافظين)", flush=True)
        return []
    if fav < REGIME_MIN:
        print("[vwbtc] النظام غير مؤاتٍ — لا دخول هذه الدورة", flush=True)
        return []

    def _fetch(s):
        try:
            return s, fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
        except Exception:
            return s, None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(_fetch, basket))

    signals = []
    for s, d1 in fetched:
        try:
            if not d1 or len(d1["c"]) < 300:
                continue
            d1 = {k: vv[:-1] for k, vv in d1.items()}       # استبعاد الشمعة الجارية
            sig = vwave_signal(d1)
            if not sig:
                continue
            key = f"{s}:{sig['ts']}"
            if key in sent:
                continue
            if tp1_too_close(sig["levels"][0], sig["tp1"]):
                continue
            levels = [round(p, 8) for p in sig["levels"]]
            entry, stop = levels[0], sig["stop"]
            quality = (sig["tp1"] - entry) / (entry - stop) if entry > stop else 0.0
            signals.append(dict(
                key=key, sym=s, tf=CFG["entry_tf"], ts=sig["ts"],
                entry=entry, stop=round(stop, 8),
                legs=levels, dca_levels=levels, wait_entry=True,
                max_age_h=CFG["wait_max_age_h"],
                tp1=round(sig["tp1"], 8), tp2=round(sig["tp2"], 8),
                os_hits=sig["os_hits"], quality=quality))
        except Exception as ex:
            print("[vwbtc] skip", s, ex, flush=True)

    # رتّب-ثم-اختر: الأعلى عائد/مخاطرة أولاً، ثم أفضل top_n
    signals.sort(key=lambda x: -x["quality"])
    signals = signals[:CFG["top_n"]]

    if signals:
        mid = send_telegram(format_message_vwave(signals))
        track_for_dashboard(signals, mid)
        for sig in signals:
            state.setdefault("sent", []).append(sig["key"])
        save_state(state)
        print(f"[vwbtc] أرسلت {len(signals)} إشارة: " +
              ", ".join(f"{x['sym']}(q={x['quality']:.2f})" for x in signals), flush=True)
    else:
        print("[vwbtc] لا إشارات هذه الدورة", flush=True)
    return signals


if __name__ == "__main__":
    scan()
