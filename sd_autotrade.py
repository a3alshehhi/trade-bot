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

import bybit_exec
try:
    import binance_exec
except Exception:
    binance_exec = None

bx = bybit_exec          # المنصّة النشطة حالياً (تُبدَّل لكل منصّة داخل الدورة)

# ── إعدادات ──────────────────────────────────────────────────────────────────
RISK_PCT = float(os.environ.get("SD_RISK_PCT", "0.005"))     # 0.5% لكل صفقة (غير مستخدم عند تثبيت القيمة)
ORDER_USD = float(os.environ.get("SD_ORDER_USD", "300"))     # قيمة ثابتة لكل أمر شراء (USDT)
MAX_CONCURRENT = int(os.environ.get("SD_MAX_POS", "5"))       # حد المراكز المتزامنة

# ── إعدادات خاصّة ببايننس (2026-07-03، بطلب بو محمد) ──────────────────────────
# بايننس Demo = *أوامر شراء فقط* بقيمة 100$ لكل أمر (لا بيع/لا إدارة خروج).
# قيم بايبت تبقى كما هي (300$، إدارة 50/50 كاملة).
BINANCE_ORDER_USD = float(os.environ.get("BINANCE_ORDER_USD", "100"))
BINANCE_BUY_ONLY = os.environ.get("BINANCE_BUY_ONLY", "1") == "1"   # 1 = شراء فقط
# مع الشراء فقط لا تُغلق المراكز، فنرفع حدّ المراكز حتى لا يتجمّد بعد 5.
BINANCE_MAX_POS = int(os.environ.get("BINANCE_MAX_POS", "1000"))
FEE_RATE = 0.001                                              # عمولة تقديرية للطرف الواحد
POS_PATH = os.environ.get("SD_POS", "sd_positions.json")
LEDGER_PATH = os.environ.get("SD_LEDGER", "sd_ledger.json")
EXEC_PATH = os.environ.get("SD_EXECUTED", "sd_executed.json")
LAST_PATH = os.environ.get("SD_LAST_ENTRY", "sd_last_entry.json")  # آخر دخول لكل عملة (تهدئة)
TRACK_PATH = os.environ.get("SD_TRACK", "tracked_signals.json")
MAX_SIGNAL_AGE_H = float(os.environ.get("SD_MAX_SIGNAL_AGE_H", "3"))  # لا تنفّذ إشارات أقدم من كذا
COOLDOWN_H = float(os.environ.get("SD_COOLDOWN_H", "12"))  # لا تعِد فتح نفس العملة قبل مرور كذا ساعة

# البوتات المسموح بتنفيذها آلياً (تُطابق حقل label في tracked_signals.json).
# تشمل: العرض/الطلب + عائلة RSI70/الانعكاس + trendwave. "*" = الكل.
_LABELS_ENV = os.environ.get("SD_LABELS", "*").strip()

SEP = "━━━━━━━━━━━━━━━━━━"

# ── دعم منصّات متعدّدة (بايبت + بايننس) ───────────────────────────────────────
# كل منصّة لها ملفات حالة منفصلة: بايبت يبقي الأسماء الأصلية، وبايننس يأخذ لاحقة
# "_binance" حتى لا تختلط الصفقات. تُبدَّل الأسماء عبر _use_exchange داخل الدورة.
_POS_BASE, _LEDGER_BASE = POS_PATH, LEDGER_PATH
_EXEC_BASE, _LAST_BASE = EXEC_PATH, LAST_PATH
EX_NAME = "bybit"        # اسم المنصّة النشطة (للرسائل)


def _use_exchange(module, suffix, name):
    """يبدّل المنصّة النشطة وملفات حالتها (بايبت=أسماء أصلية، غيره=لاحقة)."""
    global bx, EX_NAME, POS_PATH, LEDGER_PATH, EXEC_PATH, LAST_PATH
    bx = module
    EX_NAME = name

    def _s(n):
        if not suffix:
            return n
        base, ext = n.rsplit(".", 1)
        return f"{base}_{suffix}.{ext}"

    POS_PATH, LEDGER_PATH = _s(_POS_BASE), _s(_LEDGER_BASE)
    EXEC_PATH, LAST_PATH = _s(_EXEC_BASE), _s(_LAST_BASE)


def _enabled_exchanges():
    """قائمة المنصّات المُتاحة للتنفيذ (بايبت دائماً، بايننس إن توفّر الملف)."""
    xs = [(bybit_exec, "", "bybit")]
    if binance_exec is not None:
        xs.append((binance_exec, "binance", "binance"))
    return xs


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
        print(f"autotrade[{EX_NAME}]: لا توجد مفاتيح — التنفيذ متوقّف.")
        return False
    if bx.ENV == "mainnet" and os.environ.get("SD_ALLOW_MAINNET") != "1":
        print(f"autotrade[{EX_NAME}]: mainnet ممنوع بلا SD_ALLOW_MAINNET=1 — متوقّف.")
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
    notional = ORDER_USD                            # قيمة ثابتة لكل أمر شراء (USDT)

    filt = bx.instrument_filters(sym)
    if not filt:                                    # الزوج غير مُدرَج للتداول (Spot/Demo)
        print(f"autotrade[{EX_NAME}]: {sym} غير متاح للتداول على المنصّة — تخطّي")
        return False
    min_amt = float(filt.get("minOrderAmt") or 5)
    avail = bx.wallet_balance()["coins"].get("USDT", {}).get("amount", 0.0)
    notional = min(notional, avail * 0.98)

    # سقف كمية أمر السوق (يمنع خطأ التجاوز): إن كانت القيمة تُنتج كمية أساس
    # تتجاوز الحد الأقصى، نخفّض القيمة لتبقى تحت السقف.
    px = bx.last_price(sym) or entry
    max_mkt_qty = float(filt.get("maxMktOrderQty") or 0)
    max_amt = float(filt.get("maxOrderAmt") or 0)
    if max_mkt_qty > 0 and px > 0:
        notional = min(notional, max_mkt_qty * px * 0.98)
    if max_amt > 0:
        notional = min(notional, max_amt * 0.98)

    if notional < min_amt:
        print(f"autotrade[{EX_NAME}]: {sym} حجم {notional:.2f} < الحد الأدنى {min_amt} — تخطّي")
        return False

    base = sym.replace("USDT", "")
    try:
        before = bx.coin_qty(base)
        bx.market_buy(sym, round(notional, 2))
        after = bx.coin_qty(base)
    except Exception as ex:
        print(f"autotrade[{EX_NAME}]: فشل شراء {sym} —", ex)
        return False
    qty = max(after - before, 0.0)
    if qty <= 0:
        print(f"autotrade[{EX_NAME}]: {sym} لم تُرصد كمية بعد الشراء — تخطّي")
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
        f"{SEP}\n🟢 دخول تجريبي [{EX_NAME}] — {sym} · {tf}  [{label}]\n{SEP}\n"
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
        print(f"autotrade[{EX_NAME}]: تعذّر جلب الرصيد —", ex)
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
    """يقرأ tracked_signals.json (حيث تكتب كل البوتات: العرض/الطلب، RSI70/الانعكاس,
    trendwave) ويفتح صفقة تجريبية لكل إشارة *طازجة* لم تُنفّذ بعد.
    شراء فقط (long): يتخطّى أي إعداد هدفه تحت الدخول (لا بيع على المكشوف في Spot)."""
    if not is_enabled():
        return
    data = _load(TRACK_PATH, {})
    if not isinstance(data, dict) or not data:
        return
    positions = load_positions()
    executed = _load_executed()
    last_entry = _load(LAST_PATH, {})            # عملة -> آخر وقت دخول (لفترة التهدئة)
    if not isinstance(last_entry, dict):
        last_entry = {}
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
        # فترة التهدئة: لا تعِد فتح نفس العملة قبل مرور COOLDOWN_H ساعة من آخر دخول
        last_ts = last_entry.get(sym)
        if last_ts:
            try:
                since_h = (now - dt.datetime.fromisoformat(last_ts)).total_seconds() / 3600
                if since_h < COOLDOWN_H:
                    print(f"autotrade[{EX_NAME}]: {sym} في فترة تهدئة "
                          f"({since_h:.1f}h < {COOLDOWN_H}h) — تخطّي")
                    continue
            except Exception:
                pass
        # الحداثة: تجاهل الإشارات القديمة (كي لا يُنفّذ سجلّ متراكم عند أول تشغيل)
        created = tr.get("created", "")
        try:
            age_h = (now - dt.datetime.fromisoformat(created)).total_seconds() / 3600
        except Exception:
            age_h = 0
        if age_h > MAX_SIGNAL_AGE_H:
            continue
        if len(positions) >= MAX_CONCURRENT:
            print(f"autotrade[{EX_NAME}]: بلغ حدّ المراكز ({MAX_CONCURRENT})")
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
            last_entry[sym] = now.isoformat(timespec="seconds")   # ابدأ التهدئة
            opened += 1
    if opened:
        _save(EXEC_PATH, sorted(executed)[-1000:])
        _save(LAST_PATH, last_entry)


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
        try:
            changed = _manage_one(sym, positions) or changed
        except Exception as ex:
            # فشل رمز واحد (LOT_SIZE، رصيد، شبكة…) يجب ألا يوقف بقية المراكز
            # ولا خطوة فتح الصفقات الجديدة في نفس الدورة.
            print(f"manage[{EX_NAME}]: تخطّي {sym} بسبب خطأ —", ex)
            continue

    if changed:
        _save(POS_PATH, positions)


def _manage_one(sym, positions):
    """يدير مركزاً واحداً؛ يرجع True إن تغيّرت الحالة. يرفع الاستثناءات للمنادي
    الذي يعزلها لكل رمز على حدة."""
    pos = positions[sym]
    changed = False
    try:
        price = bx.last_price(sym)
    except Exception as ex:
        print(f"manage[{EX_NAME}]: تعذّر جلب سعر {sym} —", ex)
        return False
    if not price:
        return False
    R, entry = pos["R"], pos["entry"]

    # (1) الوقف أولاً — حماية رأس المال
    if price <= pos["stop"]:
        sold = _sell(sym, pos["qty_open"])
        reason = "تعادل/تتبّع" if pos["tp1_done"] else "وقف خسارة"
        pnl = _record_exit(pos, sold or pos["qty_open"], price, reason)
        del positions[sym]
        _notify(f"🛑 خروج [{EX_NAME}] {sym} @ {_fmt(price)} ({reason}) — "
                f"ربح/خسارة الساق ≈ {_fmt(pnl)} USDT")
        return True

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
            _notify(f"🎯 هدف1 [{EX_NAME}] {sym} @ {_fmt(price)} — جني 50% "
                    f"(≈ {_fmt(pnl)} USDT) + نقل الوقف للتعادل")
        return changed

    # (3) الهدف الثاني — إغلاق المتبقّي
    if pos["tp1_done"] and price >= pos["tp2"]:
        sold = _sell(sym, pos["qty_open"])
        pnl = _record_exit(pos, sold or pos["qty_open"], price, "هدف2")
        del positions[sym]
        _notify(f"🏁 هدف2 [{EX_NAME}] {sym} @ {_fmt(price)} — إغلاق كامل "
                f"(≈ {_fmt(pnl)} USDT)")
        return True

    # (4) وقف متحرّك بعد الهدف الأول (يقفل الأرباح، لا ينزل أبداً)
    if pos["tp1_done"]:
        trail = price - R
        if trail > pos["stop"]:
            pos["stop"] = trail
            changed = True

    return changed


# ── عرض ──────────────────────────────────────────────────────────────────────
def cmd_status():
    for module, suffix, name in _enabled_exchanges():
        _use_exchange(module, suffix, name)
        positions = load_positions()
        ledger = load_ledger()
        print(f"{SEP}\n📊 [{name}] المراكز المفتوحة: {len(positions)}\n{SEP}")
        for sym, p in positions.items():
            state = "بعد هدف1 (تتبّع)" if p["tp1_done"] else "قبل هدف1"
            print(f"  {sym:<10} دخول {_fmt(p['entry'])}  وقف {_fmt(p['stop'])}  "
                  f"كمية {_fmt(p['qty_open'])}  [{state}]")
        if ledger:
            pnl = sum(x["pnl_usdt"] for x in ledger)
            wins = sum(1 for x in ledger if x["pnl_usdt"] > 0)
            print(f"📒 سيقان مُغلقة: {len(ledger)} · رابحة {wins} · "
                  f"صافي ≈ {_fmt(pnl)} USDT")
        else:
            print("📒 السجلّ فارغ (لا خروج بعد).")


_DEFAULT_ORDER_USD = ORDER_USD          # قيمة بايبت الأصلية (300)
_DEFAULT_MAX_POS = MAX_CONCURRENT       # حدّ بايبت الأصلي (5)


def run_cycle():
    """دورة كاملة على كل منصّة مُفعّلة (بايبت + بايننس):
    بايبت: إدارة 50/50 كاملة ثم فتح إشارات (300$).
    بايننس: *شراء فقط* بقيمة 100$ (لا إدارة/بيع) بطلب بو محمد.
    فشل منصّة لا يوقف الأخرى."""
    global ORDER_USD, MAX_CONCURRENT
    for module, suffix, name in _enabled_exchanges():
        _use_exchange(module, suffix, name)
        try:
            if not is_enabled():
                continue
            if name == "binance":
                ORDER_USD = BINANCE_ORDER_USD          # 100$
                MAX_CONCURRENT = BINANCE_MAX_POS
                if not BINANCE_BUY_ONLY:               # الافتراضي: لا إدارة/بيع
                    manage_open_positions()
                execute_from_tracker()
            else:
                ORDER_USD = _DEFAULT_ORDER_USD          # بايبت 300$
                MAX_CONCURRENT = _DEFAULT_MAX_POS
                manage_open_positions()
                execute_from_tracker()
        except Exception as ex:
            print(f"autotrade[{name}] خطأ:", ex)


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "manage":
        for _m, _s, _n in _enabled_exchanges():
            _use_exchange(_m, _s, _n)
            manage_open_positions()
    elif mode == "run":
        run_cycle()
    else:
        cmd_status()
