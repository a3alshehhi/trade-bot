#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sd_autotrade.py — تنفيذ آلي لإشارات بوت العرض/الطلب على حساب Bybit *تجريبي*.
=============================================================================
يربط إشارات sd_bot بحساب Bybit Testnet (أموال وهمية) عبر bybit_exec.py، فيدخل
الصفقات ويديرها ويخرج منها آليّاً — «مثل محلّل محترف» لكن دون أي مال حقيقي، حتى
نجمع سجلّ أداء حقيقياً لأسابيع قبل أي قرار بمال فعلي.

إدارة الخروج (50/50) لكل صفقة:
  • حجم الصفقة يُحسب بالمخاطرة: خطر = 0.5% من رأس المال ÷ مسافة الوقف.
  • الهدف الأول (+1R): بيع 50% + نقل الوقف إلى نقطة الدخول (تعادل).
  • بعد الهدف الأول: وقف متحرّك يقفل الأرباح (يرتفع مع السعر بمقدار 1R).
  • الهدف الثاني (+2R): إغلاق ما تبقّى.
  • الوقف: إغلاق كامل المتبقّي.

حواجز الأمان (مهمّة):
  • لا يعمل إلا إذا SD_EXECUTE=1 صراحةً.
  • يرفض العمل على mainnet إلا إذا SD_ALLOW_MAINNET=1 (افتراضي: testnet فقط).
  • Spot شراء فقط (لا رافعة، لا بيع على المكشوف).

الأوضاع (CLI):
  python sd_autotrade.py manage    # إدارة المراكز المفتوحة فقط
  python sd_autotrade.py status    # عرض المراكز المفتوحة + ملخّص السجلّ

⚠️ أداة تعليمية على حساب افتراضي. ليست نصيحة مالية.
"""
import os
import json
import datetime as dt

import bybit_exec as bx

# ── إعدادات ──────────────────────────────────────────────────────────────────
RISK_PCT = float(os.environ.get("SD_RISK_PCT", "0.005"))     # 0.5% لكل صفقة
MAX_CONCURRENT = int(os.environ.get("SD_MAX_POS", "5"))       # حد المراكز المتزامنة
FEE_RATE = 0.001                                              # عمولة تقديرية للطرف الواحد
POS_PATH = os.environ.get("SD_POS", "sd_positions.json")
LEDGER_PATH = os.environ.get("SD_LEDGER", "sd_ledger.json")
EXEC_PATH = os.environ.get("SD_EXECUTED", "sd_executed.json")
TRACK_PATH = os.environ.get("SD_TRACK", "tracked_signals.json")
MAX_SIGNAL_AGE_H = float(os.environ.get("SD_MAX_SIGNAL_AGE_H", "3"))  # لا تنفّذ إشارات أقدم من كذا

# البوتات المسموح بتنفيذها آلياً (تُطابق حقل label في tracked_signals.json).
# تشمل: العرض/الطلب + عائلة RSI70/الانعكاس + trendwave. "*" = الكل.
_LABELS_ENV = os.environ.get("SD_LABELS", "*").strip()

SEP = "━━━━━━━━━━━━━━━━━━"


# ── تخزين الحالة ─────────────────────────────────────────────────────────────
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        print("save error", path, ex)


def load_positions():
    d = _load(POS_PATH, {})
    return d if isinstance(d, dict) else {}


def load_ledger():
    d = _load(LEDGER_PATH, [])
    return d if isinstance(d, list) else []


# ── تيليجرام (يعيد استخدام مُرسِل sd_bot) ────────────────────────────────────
def _notify(text):
    print(text)
    try:
        from sd_bot import send_telegram
        send_telegram(text)
    except Exception as ex:
        print("notify skip", ex)


# ── حواجز التفعيل ────────────────────────────────────────────────────────────
def is_enabled():
    """التنفيذ مُعطّل ما لم يُطلب صراحةً، ومقيّد بـ testnet ما لم يُسمح غير ذلك."""
    if os.environ.get("SD_EXECUTE") != "1":
        return False
    if not bx.API_KEY or not bx.API_SECRET:
        print("autotrade: لا توجد مفاتيح Bybit — التنفيذ متوقّف.")
        return False
    if bx.ENV == "mainnet" and os.environ.get("SD_ALLOW_MAINNET") != "1":
        print("autotrade: mainnet ممنوع بلا SD_ALLOW_MAINNET=1 — التنفيذ متوقّف.")
        return False
    return True


# ── تنسيق ────────────────────────────────────────────────────────────────────
def _fmt(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


# ── فتح مركز واحد (منطق مشترك للتنفيذ المباشر والمبني على المتتبّع) ───────────
def _open_position(sym, tf, entry, stop, tp1, tp2, prob, label, positions, equity):
    """يحسب الحجم بالمخاطرة، يشتري سوقاً، ويسجّل المركز. يرجع True عند النجاح."""
    R = entry - stop
    if R <= 0 or tp1 <= entry:                     # long فقط: وقف تحت الدخول وهدف فوقه
        return False
    stop_pct = R / entry
    notional = (equity * RISK_PCT) / stop_pct      # الحجم بالـ USDT من المخاطرة
    notional = min(notional, equity / MAX_CONCURRENT)   # لا تتجاوز حصّة المركز

    filt = bx.instrument_filters(sym)
    min_amt = float(filt.get("minOrderAmt") or 5)
    avail = bx.wallet_balance()["coins"].get("USDT", {}).get("amount", 0.0)
    notional = min(notional, avail * 0.98)
    if notional < min_amt:
        print(f"autotrade: {sym} حجم {notional:.2f} < الحد الأدنى {min_amt} — تخطّي")
        return False

    base = sym.replace("USDT", "")
    try:
        before = bx.coin_qty(base)
        bx.market_buy(sym, round(notional, 2))
        after = bx.coin_qty(base)
    except Exception as ex:
        print(f"autotrade: فشل شراء {sym} —", ex)
        return False
    qty = max(after - before, 0.0)
    if qty <= 0:
        print(f"autotrade: {sym} لم تُرصد كمية بعد الشراء — تخطّي")
        return False

    positions[sym] = {
        "symbol": sym, "tf": tf, "label": label, "prob": prob,
        "entry": entry, "init_stop": stop, "stop": stop, "R": R,
        "tp1": tp1, "tp2": tp2 if tp2 and tp2 > tp1 else entry + 2 * R,
        "qty": qty, "qty_open": qty, "tp1_done": False,
        "opened_ts": _now(),
    }
    _save(POS_PATH, positions)
    _notify(
        f"{SEP}\n🟢 دخول تجريبي — {sym} · {tf}  [{label}]\n{SEP}\n"
        f"📍 الدخول ≈ {_fmt(entry)}\n🛑 الوقف {_fmt(stop)}  (−{stop_pct*100:.2f}%)\n"
        f"🎯 هدف1 {_fmt(positions[sym]['tp1'])} · هدف2 {_fmt(positions[sym]['tp2'])}\n"
        f"📦 الكمية {_fmt(qty)} {base} (≈ {_fmt(notional)} USDT)\n"
        + (f"🤖 ثقة الفلتر {int((prob or 0)*100)}%\n" if prob else "")
        + "⚠️ حساب تجريبي — ليست نصيحة مالية."
    )
    return True


def _get_equity():
    try:
        eq = bx.wallet_balance()["total_usd"]
    except Exception as ex:
        print("autotrade: تعذّر جلب الرصيد —", ex)
        return 0.0
    return eq


def execute_signals(signals):
    """تنفيذ مباشر لقائمة إشارات sd_bot (اختياري؛ المسار الأساسي عبر المتتبّع)."""
    if not is_enabled() or not signals:
        return
    positions = load_positions()
    equity = _get_equity()
    if equity <= 0:
        return
    for sig in signals:
        sym = sig["sym"]
        if sym in positions or len(positions) >= MAX_CONCURRENT:
            continue
        entry, stop = float(sig["entry"]), float(sig["stop"])
        _open_position(sym, sig.get("tf", ""), entry, stop,
                       entry + (entry - stop), entry + 2 * (entry - stop),
                       sig.get("prob"), "العرض/الطلب", positions, equity)


# ── التنفيذ المبني على المتتبّع المشترك (يغطّي كل البوتات) ───────────────────
def _labels_allowed(label):
    if _LABELS_ENV == "*" or not _LABELS_ENV:
        return True
    return any(label == x.strip() for x in _LABELS_ENV.split("،") if x.strip()) or \
           any(label == x.strip() for x in _LABELS_ENV.split(",") if x.strip())


def _load_executed():
    d = _load(EXEC_PATH, [])
    return set(d) if isinstance(d, list) else set()


def execute_from_tracker():
    """يقرأ tracked_signals.json (حيث تكتب كل البوتات: العرض/الطلب، RSI70/الانعكاس،
    trendwave) ويفتح صفقة تجريبية لكل إشارة *طازجة* لم تُنفّذ بعد.
    شراء فقط (long): يتخطّى أي إعداد هدفه تحت الدخول (لا بيع على المكشوف في Spot)."""
    if not is_enabled():
        return
    data = _load(TRACK_PATH, {})
    if not isinstance(data, dict) or not data:
        return
    positions = load_positions()
    executed = _load_executed()
    equity = _get_equity()
    if equity <= 0:
        return
    now = dt.datetime.now()
    # الأقدم أولاً حتى نحترم ترتيب ظهور الإشارات ضمن حدّ المراكز
    items = sorted(data.items(), key=lambda kv: (kv[1] or {}).get("created", ""))
    opened = 0
    for key, tr in items:
        if not isinstance(tr, dict):
            continue
        sym = tr.get("symbol")
        label = tr.get("label", "")
        if not sym or not str(sym).endswith("USDT"):
            continue
        if not _labels_allowed(label):
            continue
        if tr.get("stopped") or tr.get("hits"):        # طازجة فقط: لم تتحرّك بعد
            continue
        ekey = f"{label}|{sym}|{tr.get('bar_ts')}"
        if ekey in executed or sym in positions:       # منع التكرار / صفقة لكل رمز
            continue
        # الحداثة: تجاهل الإشارات القديمة (كي لا يُنفّذ سجلّ متراكم عند أول تشغيل)
        created = tr.get("created", "")
        try:
            age_h = (now - dt.datetime.fromisoformat(created)).total_seconds() / 3600
        except Exception:
            age_h = 0
        if age_h > MAX_SIGNAL_AGE_H:
            continue
        if len(positions) >= MAX_CONCURRENT:
            print(f"autotrade: بلغ حدّ المراكز ({MAX_CONCURRENT})")
            break
        try:
            entry = float(tr["entry"])
            stop = float(tr.get("init_stop", tr.get("stop")))
        except Exception:
            continue
        targets = tr.get("targets") or []
        if not targets:
            continue
        tp1 = float(targets[0])
        tp2 = float(targets[1]) if len(targets) > 1 else 0.0
        if _open_position(sym, tr.get("timeframe", ""), entry, stop, tp1, tp2,
                          tr.get("prob"), label or "إشارة", positions, equity):
            executed.add(ekey)
            opened += 1
    if opened:
        _save(EXEC_PATH, sorted(executed)[-1000:])


# ── إدارة المراكز المفتوحة ───────────────────────────────────────────────────
def _sell(sym, qty):
    """بيع سوق لكمية، مع تقريبها لخطوة الزوج. يرجع الكمية المُقرّبة أو 0."""
    filt = bx.instrument_filters(sym)
    q = bx._round_step(qty, filt.get("basePrecision"))
    if q <= 0:
        return 0.0
    bx.market_sell(sym, q)
    return q


def _record_exit(pos, qty, price, reason):
    """يسجّل ساق خروج في السجلّ ويرجع الربح/الخسارة بالـ USDT (تقديري بعد العمولة)."""
    entry = pos["entry"]
    gross = qty * (price - entry)
    fees = qty * (entry + price) * FEE_RATE          # عمولة الدخول والخروج تقديراً
    pnl = gross - fees
    ledger = load_ledger()
    ledger.append({
        "symbol": pos["symbol"], "tf": pos.get("tf", ""),
        "entry": round(entry, 8), "exit": round(price, 8),
        "qty": round(qty, 8), "pnl_usdt": round(pnl, 4),
        "pnl_pct": round((price - entry) / entry * 100, 3),
        "reason": reason, "closed_ts": _now(),
    })
    _save(LEDGER_PATH, ledger)
    return pnl


def manage_open_positions():
    """يفحص كل مركز مفتوح ويطبّق آلة الحالة 50/50 (وقف/هدف1/تتبّع/هدف2)."""
    if not is_enabled():
        return
    positions = load_positions()
    if not positions:
        return
    changed = False
    for sym in list(positions.keys()):
        pos = positions[sym]
        try:
            price = bx.last_price(sym)
        except Exception as ex:
            print(f"manage: تعذّر جلب سعر {sym} —", ex)
            continue
        if not price:
            continue
        R, entry = pos["R"], pos["entry"]

        # (1) الوقف أولاً — حماية رأس المال
        if price <= pos["stop"]:
            sold = _sell(sym, pos["qty_open"])
            reason = "تعادل/تتبّع" if pos["tp1_done"] else "وقف خسارة"
            pnl = _record_exit(pos, sold or pos["qty_open"], price, reason)
            del positions[sym]; changed = True
            _notify(f"🛑 خروج {sym} @ {_fmt(price)} ({reason}) — "
                    f"ربح/خسارة الساق ≈ {_fmt(pnl)} USDT")
            continue

        # (2) الهدف الأول — بيع 50% + تعادل
        if not pos["tp1_done"] and price >= pos["tp1"]:
            half = pos["qty_open"] * 0.5
            sold = _sell(sym, half)
            if sold > 0:
                pnl = _record_exit(pos, sold, price, "هدف1 (50%)")
                pos["qty_open"] -= sold
                pos["tp1_done"] = True
                pos["stop"] = entry                    # نقل الوقف للتعادل
                changed = True
                _notify(f"🎯 هدف1 {sym} @ {_fmt(price)} — جني 50% "
                        f"(≈ {_fmt(pnl)} USDT) + نقل الوقف للتعادل")
            continue

        # (3) الهدف الثاني — إغلاق المتبقّي
        if pos["tp1_done"] and price >= pos["tp2"]:
            sold = _sell(sym, pos["qty_open"])
            pnl = _record_exit(pos, sold or pos["qty_open"], price, "هدف2")
            del positions[sym]; changed = True
            _notify(f"🏁 هدف2 {sym} @ {_fmt(price)} — إغلاق كامل "
                    f"(≈ {_fmt(pnl)} USDT)")
            continue

        # (4) وقف متحرّك بعد الهدف الأول (يقفل الأرباح، لا ينزل أبداً)
        if pos["tp1_done"]:
            trail = price - R
            if trail > pos["stop"]:
                pos["stop"] = trail
                changed = True

    if changed:
        _save(POS_PATH, positions)


# ── عرض ──────────────────────────────────────────────────────────────────────
def cmd_status():
    positions = load_positions()
    ledger = load_ledger()
    print(f"{SEP}\n📊 المراكز المفتوحة: {len(positions)}\n{SEP}")
    for sym, p in positions.items():
        state = "بعد هدف1 (تتبّع)" if p["tp1_done"] else "قبل هدف1"
        print(f"  {sym:<10} دخول {_fmt(p['entry'])}  وقف {_fmt(p['stop'])}  "
              f"كمية {_fmt(p['qty_open'])}  [{state}]")
    if ledger:
        pnl = sum(x["pnl_usdt"] for x in ledger)
        wins = sum(1 for x in ledger if x["pnl_usdt"] > 0)
        print(f"{SEP}\n📒 سيقان مُغلقة: {len(ledger)} · رابحة {wins} · "
              f"صافي ≈ {_fmt(pnl)} USDT\n{SEP}")
    else:
        print("📒 السجلّ فارغ (لا خروج بعد).")


def run_cycle():
    """دورة كاملة: أدر المراكز المفتوحة أولاً، ثم افتح الإشارات الطازجة."""
    manage_open_positions()
    execute_from_tracker()


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "manage":
        manage_open_positions()
    elif mode == "run":
        run_cycle()
    else:
        cmd_status()
