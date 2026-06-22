#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper.py — متتبّع الصفقات الورقية (paper trading) للبوت.

لا يحرّك أموالاً. يسجّل الصفقات التي تختارها يدوياً بالضغط على زر تيليجرام،
ثم يتابعها على بيانات الأسعار الحيّة حتى الهدف/الوقف، ويحسب النتيجة بالـ R،
ويكتب كل شيء في ملف تقرأه لوحة الويب على GitHub Pages.

الأوضاع:
  auto     — يسجّل تلقائياً كل إشارة معلّقة كصفقة ورقية (بلا ضغط زر).
  poll     — يلتقط ضغطات زر "افتح صفقة ورقية" عبر getUpdates ويفتح الصفقات.
  monitor  — يتابع الصفقات المفتوحة، يطبّق إدارة (جني جزئي + breakeven)،
             يغلقها عند اكتمالها، ويرسل إشعاراً.
  export   — ينسخ سجل الصفقات إلى docs/ للوحة ويحسب الإحصائيات.
  report   — يرسل ملخص الأداء إلى تيليجرام (للأمر /report أو يدوياً).

الإدارة (هجينة مُتحقَّقة بالباك-تست — الأفضل توازناً): مسك كامل بلا جني جزئي.
عند بلوغ الهدف الأول يُرفع الوقف إلى سعر الدخول (تعادل)، ثم وقف متحرّك هيكلي
تحت أدنى قاع في نافذة TRAIL_W ناقص TRAIL_BUF×ATR مع كل صعود، والخروج فوراً
عند تشكّل دايفرجنس سلبي على RSI(21). النتيجة بالـ R = (سعر الخروج − الدخول) / المخاطرة.

⚠️ أداة تحليل تعليمية ونتائج افتراضية — ليست نصيحة مالية.
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime

import numpy as np
import requests

# إعادة استخدام أدوات البوت الأساسية
from trading_bot import (
    fetch_binance, send_telegram, _fmt_price,
    BINANCE_INTERVAL, PENDING_FILE, DASHBOARD_URL,
    rsi, atr, detect_divergence,
)

# إدارة هجينة (مُتحقَّقة بالباك-تست، الأفضل توازناً): تعادل عند الهدف الأول →
# وقف متحرّك هيكلي تحت أدنى قاع في نافذة TRAIL_W ناقص TRAIL_BUF×ATR → خروج عند دايفرجنس سلبي.
TRAIL_W = 10        # نافذة القاع للوقف المتحرّك
TRAIL_BUF = 1.0     # حاجز ATR تحت القاع
DIV_LOOKBACK = 60   # نافذة كشف الدايفرجنس السلبي

_DASH_BTN = {"inline_keyboard": [[{"text": "📊 افتح المتتبّع", "url": DASHBOARD_URL}]]}

PAPER_FILE = "paper_trades.json"
OFFSET_FILE = "tg_offset.json"
# ملف بيانات اللوحة (تقرأه index.html في جذر المستودع عبر GitHub Pages)
DATA_FILE = "paper_data.json"

# إدارة الصفقة: نِسب الجني عند الأهداف الثلاثة، ونقل الوقف للتعادل بعد الأول
TP_FRACTIONS = [0.5, 0.25, 0.25]
SEP = "━━━━━━━━━━━━━━━━━━"


# ── أدوات تخزين ────────────────────────────────────────────────────────────
def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _creds():
    return (os.environ.get("TELEGRAM_TOKEN"),
            os.environ.get("TELEGRAM_CHAT_ID"))


# ── poll: فتح الصفقات من ضغطات الأزرار ─────────────────────────────────────
def _answer_callback(token, cq_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                      data={"callback_query_id": cq_id, "text": text,
                            "show_alert": False}, timeout=10)
    except Exception as e:
        print(f"⚠️ answerCallbackQuery: {e}")


def _open_trade_from_signal(pid, sig):
    """يبني سجل صفقة ورقية من إشارة معلّقة."""
    entry = float(sig["entry"])
    stop = float(sig["stop"])
    risk = entry - stop
    return {
        "id": pid,
        "symbol": sig["symbol"],
        "label": sig.get("label", ""),
        "strategy": sig.get("strategy", "classic"),
        "timeframe": sig.get("timeframe", "4h"),
        "side": "buy",
        "entry": entry,
        "stop": stop,
        "stop_orig": stop,
        "targets": [float(t) for t in sig["targets"]],
        "risk": risk,
        "opened_at": datetime.now().isoformat(timespec="seconds"),
        "status": "open",
        "hits": [],
        "breakeven": False,
        "remaining": 1.0,
        "realized_pct": 0.0,   # الربح/الخسارة المحقَّق حتى الآن بالنسبة المئوية
        "result_pct": None,    # النتيجة النهائية بالنسبة المئوية
        "exit_price": None,    # سعر الخروج الفعلي
        "events": [],
        "last_ts": sig.get("bar_ts"),
        "closed_at": None,
    }


def poll():
    """يقرأ التحديثات من تيليجرام: يفتح صفقات عند ضغط الزر، ويردّ على /report و/trades."""
    token, chat_id = _creds()
    if not token or not chat_id:
        print("⚠️ لا توجد بيانات تيليجرام — تخطّي poll")
        return

    offset = _load_json(OFFSET_FILE, {}).get("offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 0,
                                 "allowed_updates": json.dumps(
                                     ["callback_query", "message"])},
                         timeout=20)
        updates = r.json().get("result", []) if r.status_code == 200 else []
    except Exception as e:
        print(f"⚠️ getUpdates: {e}")
        return

    pending = _load_json(PENDING_FILE, {})
    trades = _load_json(PAPER_FILE, [])
    existing_ids = {t["id"] for t in trades}
    opened = 0
    max_uid = offset - 1

    for up in updates:
        max_uid = max(max_uid, up.get("update_id", max_uid))

        # 1) ضغط زر "افتح صفقة ورقية"
        cq = up.get("callback_query")
        if cq and str(cq.get("data", "")).startswith("o|"):
            pid = cq["data"][2:]
            sig = pending.get(pid)
            if not sig:
                _answer_callback(token, cq["id"], "⌛ انتهت صلاحية الإشارة")
            elif pid in existing_ids:
                _answer_callback(token, cq["id"], "✓ الصفقة مفتوحة مسبقاً")
            else:
                tr = _open_trade_from_signal(pid, sig)
                trades.append(tr)
                existing_ids.add(pid)
                opened += 1
                _answer_callback(token, cq["id"],
                                 f"✅ فُتحت صفقة ورقية: {tr['symbol']}")
                send_telegram(token, chat_id, _format_open_card(tr),
                              reply_markup=_DASH_BTN)
            continue

        # 2) أوامر نصية
        msg = up.get("message") or {}
        text = str(msg.get("text", "")).strip().lower()
        if text.startswith("/report"):
            send_telegram(token, chat_id, _format_report(trades))
        elif text.startswith("/trades"):
            send_telegram(token, chat_id, _format_open_list(trades))

    _save_json(PAPER_FILE, trades)
    _save_json(OFFSET_FILE, {"offset": max_uid + 1})
    print(f"[poll] تحديثات: {len(updates)} | صفقات جديدة: {opened}")


# ── auto: تسجيل كل الإشارات تلقائياً (بلا ضغط زر) ───────────────────────────
def auto_open():
    """يفتح تلقائياً كل إشارة معلّقة لم تُسجَّل بعد كصفقة ورقية.
    يضمن تجميع نتائج كل الإشارات أمامياً بلا حاجة للضغط على الزر."""
    token, chat_id = _creds()
    pending = _load_json(PENDING_FILE, {})
    if not isinstance(pending, dict) or not pending:
        print("[auto] لا إشارات معلّقة")
        return
    trades = _load_json(PAPER_FILE, [])
    existing_ids = {t["id"] for t in trades}
    opened = []
    for pid, sig in sorted(pending.items()):
        if pid in existing_ids:
            continue
        if not isinstance(sig, dict) or sig.get("entry") is None or not sig.get("targets"):
            continue
        try:
            tr = _open_trade_from_signal(pid, sig)
        except Exception as e:
            print(f"⚠️ auto {sig.get('symbol')}: {e}")
            continue
        # تجاهل الإشارات بمخاطرة غير صالحة (entry<=stop)
        if tr["risk"] <= 0:
            continue
        trades.append(tr)
        existing_ids.add(pid)
        opened.append(tr)
    _save_json(PAPER_FILE, trades)
    print(f"[auto] صفقات مُسجّلة تلقائياً: {len(opened)}")
    # رسالة ملخّص واحدة (تفادي إغراق تيليجرام برسالة لكل صفقة)
    if opened and token and chat_id:
        names = "، ".join(f"{t['symbol']}({t['timeframe']})" for t in opened[:12])
        more = f" +{len(opened) - 12}" if len(opened) > 12 else ""
        msg = "\n".join([
            SEP, f"📝 سُجّلت {len(opened)} صفقة ورقية تلقائياً للمتابعة", SEP,
            names + more, "", "سأتابعها وأبلغك عند الهدف/الوقف.",
            SEP, "⚠️ تتبّع ورقي تعليمي — ليس نصيحة مالية"])
        send_telegram(token, chat_id, msg, reply_markup=_DASH_BTN)


# ── monitor: متابعة وإغلاق الصفقات ─────────────────────────────────────────
def monitor():
    """يتابع الصفقات المفتوحة على الشموع المغلقة الجديدة، يطبّق الإدارة، ويغلق."""
    token, chat_id = _creds()
    trades = _load_json(PAPER_FILE, [])
    if not trades:
        print("[monitor] لا صفقات")
        return
    open_trades = [t for t in trades if t.get("status") == "open"]
    print(f"[monitor] صفقات مفتوحة: {len(open_trades)}")

    for tr in open_trades:
        try:
            _update_trade(tr, token, chat_id)
        except Exception as e:
            print(f"⚠️ متابعة {tr['symbol']}: {e}")

    _save_json(PAPER_FILE, trades)
    export()  # حدّث نسخة اللوحة


def _update_trade(tr, token, chat_id):
    """إدارة هجينة: تعادل عند الهدف الأول → وقف متحرّك هيكلي → خروج عند دايفرجنس سلبي.
    مسك كامل بلا جني جزئي. الوقف المتحرّك يُحفظ في tr['stop'] ويستمر عبر التشغيلات."""
    sym = tr["symbol"]
    tf = tr["timeframe"]
    df = fetch_binance(sym, BINANCE_INTERVAL.get(tf, "4h"), 300)
    if df is None or len(df) < 30:
        return
    df = df.iloc[:-1].reset_index(drop=True)   # الشموع المغلقة فقط

    entry, risk = tr["entry"], tr["risk"]
    if risk <= 0:
        return
    targets = tr["targets"]
    tp1 = targets[0] if targets else None
    low = df["low"].values
    high = df["high"].values
    rsi21 = rsi(df["close"], 21).values
    atrs = atr(df, 14).values
    last_ts = tr.get("last_ts")
    cur = float(tr.get("stop", tr["stop_orig"]))   # الوقف الفعّال الحالي

    for p in range(len(df)):
        if tr["status"] != "open":
            break
        bts = str(df["date"].iloc[p])
        if last_ts and bts <= str(last_ts):
            continue
        lo = float(low[p]); hi = float(high[p]); c = float(df["close"].iloc[p])

        # 1) فحص الوقف أولاً (تحفّظياً)
        if lo <= cur:
            pct = _pct(entry, cur)
            tr["realized_pct"] = pct
            tr["exit_price"] = cur
            ev = ("be_sl" if abs(cur - entry) < 1e-9
                  else ("trail_sl" if cur > tr["stop_orig"] + 1e-9 else "sl"))
            tr["events"].append({"ts": bts, "type": ev, "price": cur, "pct": round(pct, 3)})
            tr["remaining"] = 0.0
            tr["stop"] = cur
            tr["status"] = "closed"
            tr["last_ts"] = bts
            _close_trade(tr, ev, cur, token, chat_id)
            break

        # 2) تعادل عند الهدف الأول (نقل الوقف لسعر الدخول)
        if not tr["breakeven"] and tp1 is not None and hi >= tp1:
            tr["breakeven"] = True
            cur = max(cur, entry)
            if 1 not in tr["hits"]:
                tr["hits"].append(1)
            tr["events"].append({"ts": bts, "type": "tp1_be",
                                 "price": tp1, "pct": round(_pct(entry, tp1), 3)})
            _notify_be(tr, tp1, token, chat_id)

        # 3) بعد التعادل: وقف متحرّك هيكلي + خروج عند دايفرجنس سلبي
        if tr["breakeven"]:
            w0 = max(0, p - TRAIL_W + 1)
            swing = float(np.min(low[w0:p + 1]))
            av = float(atrs[p]) if not np.isnan(atrs[p]) else entry * 0.02
            cand = float(swing - TRAIL_BUF * av)
            if cand > cur:
                cur = cand
                tr["events"].append({"ts": bts, "type": "trail", "price": round(cur, 8)})
            if detect_divergence(low[:p + 1], high[:p + 1], rsi21[:p + 1],
                                 lookback=DIV_LOOKBACK) == "bear":
                pct = _pct(entry, c)
                tr["realized_pct"] = pct
                tr["exit_price"] = c
                tr["events"].append({"ts": bts, "type": "bear_div",
                                     "price": c, "pct": round(pct, 3)})
                tr["remaining"] = 0.0
                tr["stop"] = cur
                tr["status"] = "closed"
                tr["last_ts"] = bts
                _close_trade(tr, "bear_div", c, token, chat_id)
                break

        tr["stop"] = cur
        tr["last_ts"] = bts

    if tr["status"] == "closed" and tr.get("result_pct") is None:
        tr["result_pct"] = round(tr["realized_pct"], 3)
        tr["closed_at"] = datetime.now().isoformat(timespec="seconds")


def _close_trade(tr, ev, price, token, chat_id):
    if tr.get("exit_price") is None:
        tr["exit_price"] = price
    tr["result_pct"] = round(tr["realized_pct"], 3)
    tr["closed_at"] = datetime.now().isoformat(timespec="seconds")
    if token and chat_id:
        send_telegram(token, chat_id, _format_close_card(tr))


# ── بطاقات تيليجرام ────────────────────────────────────────────────────────
def _strat_name(tr):
    return tr.get("label") or ("DCA" if tr.get("strategy") == "dca" else "كلاسيكي")


def _format_open_card(tr):
    f = _fmt_price
    lines = [SEP, "📝 فُتحت صفقة ورقية (متابعة)", SEP, "",
             f"💰 العملة: {tr['symbol']}",
             f"🧭 الاستراتيجية: {_strat_name(tr)}",
             f"⏱️ الفريم: {tr['timeframe']}",
             f"🟢 الدخول: {f(tr['entry'])}",
             f"🛑 الوقف: {f(tr['stop'])}"]
    for k, t in enumerate(tr["targets"], 1):
        lines.append(f"🎯 الهدف {k}: {f(t)}")
    lines += ["", "سأتابعها وأبلغك عند الهدف أو الوقف.",
              SEP, "⚠️ تتبّع ورقي تعليمي — ليس نصيحة مالية"]
    return "\n".join(lines)


def _notify_be(tr, price, token, chat_id):
    """إشعار بلوغ الهدف الأول ونقل الوقف للتعادل (بدء الإدارة الهجينة)."""
    if not (token and chat_id):
        return
    f = _fmt_price
    msg = "\n".join([
        SEP, "🎯 تحقق الهدف الأول ✅ — رفع الوقف للتعادل", SEP, "",
        f"💰 {tr['symbol']} — {_strat_name(tr)}",
        f"🟢 الدخول: {f(tr['entry'])}",
        f"💵 السعر: {f(price)}",
        "🔒 الوقف الآن = الدخول (صفقة بلا خسارة)",
        "🪜 سأرفع الوقف تحت كل تصحيح، وأخرج عند دايفرجنس سلبي.",
        SEP,
    ])
    send_telegram(token, chat_id, msg)


def _notify_event(tr, i, price, pct, token, chat_id):
    if not (token and chat_id):
        return
    f = _fmt_price
    head = {1: "🎯 تحقق الهدف الأول ✅ (جني 50% + نقل الوقف للتعادل)",
            2: "🎯 تحقق الهدف الثاني ✅✅ (جني 25%)",
            3: "🏆 تحقق الهدف الثالث ✅✅✅ (إغلاق)"}[i]
    msg = "\n".join([
        SEP, head, SEP, "",
        f"💰 {tr['symbol']} — {_strat_name(tr)}",
        f"🟢 الدخول: {f(tr['entry'])}",
        f"💵 السعر: {f(price)}",
        f"📊 عائد هذا الجزء: {pct:+.2f}%",
        f"📈 المحقَّق حتى الآن: {tr['realized_pct']:+.2f}%",
        SEP,
    ])
    send_telegram(token, chat_id, msg)


def _format_close_card(tr):
    f = _fmt_price
    res = tr["result_pct"] if tr.get("result_pct") is not None else tr["realized_pct"]
    verdict = "ربح ✅" if res > 0 else ("تعادل ⚪" if abs(res) < 1e-6 else "خسارة 🔴")
    hit_txt = "، ".join(f"هدف {h}" for h in tr["hits"]) or "لا شيء"
    lines = [SEP, "🏁 أُغلقت الصفقة الورقية", SEP, "",
             f"💰 {tr['symbol']} — {_strat_name(tr)}",
             f"🟢 الدخول: {f(tr['entry'])}"]
    if tr.get("exit_price") is not None:
        lines.append(f"🔚 الخروج: {f(tr['exit_price'])}")
    lines += [f"✅ الأهداف المتحققة: {hit_txt}",
              f"📊 النتيجة النهائية: {res:+.2f}% — {verdict}",
              SEP, "⚠️ نتيجة افتراضية تعليمية — ليست نصيحة مالية"]
    return "\n".join(lines)


def _format_open_list(trades):
    op = [t for t in trades if t.get("status") == "open"]
    if not op:
        return "لا توجد صفقات ورقية مفتوحة حالياً."
    lines = [SEP, f"📂 الصفقات المفتوحة ({len(op)})", SEP]
    for t in op:
        hits = "".join("✅" for _ in t["hits"]) or "—"
        lines.append(f"• {t['symbol']} ({_strat_name(t)}) {hits} "
                     f"| محقَّق {t.get('realized_pct', 0.0):+.2f}%")
    return "\n".join(lines)


# ── الإحصائيات والتقرير ────────────────────────────────────────────────────
def _pct(entry, exit_price):
    """نسبة الربح/الخسارة المئوية لصفقة شراء = (الخروج − الدخول) ÷ الدخول × 100."""
    entry = float(entry)
    if entry <= 0:
        return 0.0
    return (float(exit_price) - entry) / entry * 100.0


def _compound(pcts):
    """العائد التراكمي المركّب لقائمة نِسب مئوية (كأن رأس المال يُعاد استثماره)."""
    eq = 1.0
    for p in pcts:
        eq *= (1.0 + p / 100.0)
    return (eq - 1.0) * 100.0


def _closed_trades(trades):
    return [t for t in trades if t.get("status") == "closed"
            and t.get("result_pct") is not None]


def compute_stats(trades):
    closed = _closed_trades(trades)
    n = len(closed)
    out = {"open": sum(1 for t in trades if t.get("status") == "open"),
           "closed": n, "win_rate": 0.0, "profit_factor": 0.0,
           "total_pct": 0.0, "avg_pct": 0.0, "max_drawdown_pct": 0.0}
    if n == 0:
        return out
    ps = [t["result_pct"] for t in closed]
    wins = [p for p in ps if p > 1e-9]
    losses = [p for p in ps if p < -1e-9]
    gp = sum(wins)
    gl = -sum(losses)
    out["win_rate"] = round(len(wins) / n * 100, 1)
    out["profit_factor"] = round(gp / gl, 2) if gl > 0 else float("inf")
    out["avg_pct"] = round(sum(ps) / n, 2)
    # الإجمالي التراكمي المركّب + أقصى تراجع على منحنى رأس المال (بالنسبة المئوية)
    ordered = sorted(closed, key=lambda t: str(t.get("closed_at") or ""))
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for t in ordered:
        eq *= (1.0 + t["result_pct"] / 100.0)
        peak = max(peak, eq)
        mdd = min(mdd, (eq / peak - 1.0) * 100.0)
    out["total_pct"] = round((eq - 1.0) * 100.0, 2)
    out["max_drawdown_pct"] = round(mdd, 2)
    return out


def _period_keys(t):
    """يُرجع (اليوم، الأسبوع ISO، الشهر) من وقت إغلاق الصفقة."""
    ts = t.get("closed_at") or t.get("opened_at")
    d = datetime.fromisoformat(str(ts))
    iso = d.isocalendar()
    return (d.strftime("%Y-%m-%d"),
            f"{iso[0]}-W{iso[1]:02d}",
            d.strftime("%Y-%m"))


def _pack_period(groups, meta=None):
    """يحوّل قاموس {مفتاح: [نِسب]} إلى صفوف مرتّبة تنازلياً بإحصاء كل فترة."""
    rows = []
    for k in sorted(groups.keys(), reverse=True):
        ps = groups[k]
        wins = sum(1 for p in ps if p > 1e-9)
        row = {"key": k, "trades": len(ps),
               "win_rate": round(wins / len(ps) * 100, 1),
               "return_pct": round(_compound(ps), 2)}
        if meta:
            row.update(meta.get(k, {}))
        rows.append(row)
    return rows


def compute_periods(trades):
    """إحصائيات بالنسبة المئوية: يومية (مع أسبوعها وشهرها)، أسبوعية (مع شهرها)، شهرية."""
    closed = _closed_trades(trades)
    daily, weekly, monthly = {}, {}, {}
    md, mw = {}, {}
    for t in closed:
        try:
            day, week, month = _period_keys(t)
        except Exception:
            continue
        p = t["result_pct"]
        daily.setdefault(day, []).append(p);   md[day] = {"week": week, "month": month}
        weekly.setdefault(week, []).append(p);  mw[week] = {"month": month}
        monthly.setdefault(month, []).append(p)
    return {"daily": _pack_period(daily, md),
            "weekly": _pack_period(weekly, mw),
            "monthly": _pack_period(monthly)}


def _format_report(trades):
    s = compute_stats(trades)
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    lines = [SEP, "📊 تقرير الصفقات الورقية", SEP, "",
             f"📂 مفتوحة: {s['open']}  |  🏁 مغلقة: {s['closed']}",
             f"🎯 نسبة الفوز: {s['win_rate']}%",
             f"💹 معامل الربح: {pf}",
             f"📈 إجمالي العائد (مركّب): {s['total_pct']:+.2f}%",
             f"📐 متوسط الصفقة: {s['avg_pct']:+.2f}%",
             f"📉 أقصى تراجع: {s['max_drawdown_pct']:.2f}%"]
    # تفصيل لكل استراتيجية (بالنسبة المئوية المركّبة)
    by = {}
    for t in _closed_trades(trades):
        by.setdefault(_strat_name(t), []).append(t["result_pct"])
    if by:
        lines += ["", "— حسب الاستراتيجية —"]
        for name, ps in by.items():
            wr = round(sum(1 for p in ps if p > 0) / len(ps) * 100)
            lines.append(f"• {name}: {len(ps)} صفقة | فوز {wr}% | "
                         f"{_compound(ps):+.2f}%")
    # إحصائيات الفترات
    per = compute_periods(trades)

    def _section(title, rows, limit):
        if not rows:
            return []
        out = ["", title]
        for r in rows[:limit]:
            out.append(f"• {r['key']}: {r['trades']} صفقة | "
                       f"فوز {r['win_rate']:.0f}% | {r['return_pct']:+.2f}%")
        return out

    lines += _section("🗓️ شهرياً (السنة)", per["monthly"], 12)
    lines += _section("📅 أسبوعياً (آخر 8 أسابيع)", per["weekly"], 8)
    lines += _section("📆 يومياً (آخر 7 أيام)", per["daily"], 7)
    lines += [SEP, "⚠️ نتائج افتراضية تعليمية — ليست نصيحة مالية"]
    return "\n".join(lines)


def report():
    token, chat_id = _creds()
    trades = _load_json(PAPER_FILE, [])
    if token and chat_id:
        send_telegram(token, chat_id, _format_report(trades))
    print("[report] أُرسل التقرير")


# ── export: نسخة اللوحة ────────────────────────────────────────────────────
def export():
    trades = _load_json(PAPER_FILE, [])
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": compute_stats(trades),
        "periods": compute_periods(trades),
        "trades": trades,
    }
    _save_json(DATA_FILE, payload)
    print(f"[export] كُتب {DATA_FILE} ({len(trades)} صفقة)")


# ── CLI ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="متتبّع الصفقات الورقية")
    ap.add_argument("mode", choices=["poll", "auto", "monitor", "export", "report"])
    args = ap.parse_args()
    {"poll": poll, "auto": auto_open, "monitor": monitor,
     "export": export, "report": report}[args.mode]()


if __name__ == "__main__":
    main()
