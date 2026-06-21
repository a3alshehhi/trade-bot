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

الإدارة (مطابِقة لما تم التحقق منه خارج العيّنة): دخول كامل، ثم عند الهدف الأول
يُجنى 50% ويُنقل الوقف إلى نقطة الدخول (breakeven)، 25% عند الهدف الثاني،
و25% عند الثالث. النتيجة بالـ R = مجموع (الجزء × عائده).

⚠️ أداة تحليل تعليمية ونتائج افتراضية — ليست نصيحة مالية.
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime

import requests

# إعادة استخدام أدوات البوت الأساسية
from trading_bot import (
    fetch_binance, send_telegram, _fmt_price,
    BINANCE_INTERVAL, PENDING_FILE, DASHBOARD_URL,
)

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
        "realized_R": 0.0,
        "result_R": None,
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
    sym = tr["symbol"]
    tf = tr["timeframe"]
    df = fetch_binance(sym, BINANCE_INTERVAL.get(tf, "4h"), 300)
    if df is None or len(df) < 2:
        return
    df = df.iloc[:-1]  # الشموع المغلقة فقط (نستبعد الجارية)

    last_ts = tr.get("last_ts")
    bars = df
    if last_ts:
        bars = df[df["date"].astype(str) > str(last_ts)]
    if bars.empty:
        return

    entry, risk = tr["entry"], tr["risk"]
    if risk <= 0:
        return
    targets = tr["targets"]

    for _, bar in bars.iterrows():
        if tr["status"] != "open":
            break
        hi, lo = float(bar["high"]), float(bar["low"])
        bts = str(bar["date"])

        # 1) فحص الوقف أولاً (تحفّظياً)
        eff_stop = entry if tr["breakeven"] else tr["stop_orig"]
        if lo <= eff_stop:
            R = (eff_stop - entry) / risk
            tr["realized_R"] += tr["remaining"] * R
            ev = "be_sl" if tr["breakeven"] else "sl"
            tr["events"].append({"ts": bts, "type": ev,
                                 "price": eff_stop, "R": round(R, 3)})
            tr["remaining"] = 0.0
            tr["status"] = "closed"
            tr["last_ts"] = bts
            _close_trade(tr, ev, eff_stop, token, chat_id)
            break

        # 2) فحص الأهداف بالترتيب
        for i in (1, 2, 3):
            if i in tr["hits"] or i > len(targets):
                continue
            if hi >= targets[i - 1]:
                R_i = (targets[i - 1] - entry) / risk
                frac = TP_FRACTIONS[i - 1]
                tr["realized_R"] += frac * R_i
                tr["remaining"] = max(0.0, tr["remaining"] - frac)
                tr["hits"].append(i)
                tr["breakeven"] = True
                tr["stop"] = entry
                tr["events"].append({"ts": bts, "type": f"tp{i}",
                                     "price": targets[i - 1], "R": round(R_i, 3)})
                _notify_event(tr, i, targets[i - 1], R_i, token, chat_id)
                if i == 3 or tr["remaining"] <= 1e-9:
                    tr["status"] = "closed"
                    _close_trade(tr, f"tp{i}", targets[i - 1], token, chat_id)
        tr["last_ts"] = bts

    if tr["status"] == "closed" and tr["result_R"] is None:
        tr["result_R"] = round(tr["realized_R"], 3)
        tr["closed_at"] = datetime.now().isoformat(timespec="seconds")


def _close_trade(tr, ev, price, token, chat_id):
    tr["result_R"] = round(tr["realized_R"], 3)
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


def _notify_event(tr, i, price, R, token, chat_id):
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
        f"📊 عائد هذا الجزء: {R:+.2f}R",
        f"📈 المحقَّق حتى الآن: {tr['realized_R']:+.2f}R",
        SEP,
    ])
    send_telegram(token, chat_id, msg)


def _format_close_card(tr):
    f = _fmt_price
    res = tr["result_R"] if tr["result_R"] is not None else tr["realized_R"]
    verdict = "ربح ✅" if res > 0 else ("تعادل ⚪" if abs(res) < 1e-6 else "خسارة 🔴")
    hit_txt = "، ".join(f"هدف {h}" for h in tr["hits"]) or "لا شيء"
    lines = [SEP, "🏁 أُغلقت الصفقة الورقية", SEP, "",
             f"💰 {tr['symbol']} — {_strat_name(tr)}",
             f"🟢 الدخول: {f(tr['entry'])}",
             f"✅ الأهداف المتحققة: {hit_txt}",
             f"📊 النتيجة النهائية: {res:+.2f}R — {verdict}",
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
                     f"| محقَّق {t['realized_R']:+.2f}R")
    return "\n".join(lines)


# ── الإحصائيات والتقرير ────────────────────────────────────────────────────
def compute_stats(trades):
    closed = [t for t in trades if t.get("status") == "closed"
              and t.get("result_R") is not None]
    n = len(closed)
    out = {"open": sum(1 for t in trades if t.get("status") == "open"),
           "closed": n, "win_rate": 0.0, "profit_factor": 0.0,
           "total_R": 0.0, "avg_R": 0.0, "max_drawdown_R": 0.0}
    if n == 0:
        return out
    rs = [t["result_R"] for t in closed]
    wins = [r for r in rs if r > 1e-9]
    losses = [r for r in rs if r < -1e-9]
    gp = sum(wins)
    gl = -sum(losses)
    out["win_rate"] = round(len(wins) / n * 100, 1)
    out["profit_factor"] = round(gp / gl, 2) if gl > 0 else float("inf")
    out["total_R"] = round(sum(rs), 2)
    out["avg_R"] = round(sum(rs) / n, 3)
    # أقصى تراجع على منحنى رأس المال
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    out["max_drawdown_R"] = round(mdd, 2)
    return out


def _format_report(trades):
    s = compute_stats(trades)
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    lines = [SEP, "📊 تقرير الصفقات الورقية", SEP, "",
             f"📂 مفتوحة: {s['open']}  |  🏁 مغلقة: {s['closed']}",
             f"🎯 نسبة الفوز: {s['win_rate']}%",
             f"💹 معامل الربح: {pf}",
             f"📈 إجمالي العائد: {s['total_R']:+.2f}R",
             f"📐 متوسط الصفقة: {s['avg_R']:+.3f}R",
             f"📉 أقصى تراجع: {s['max_drawdown_R']:.2f}R"]
    # تفصيل لكل استراتيجية
    by = {}
    for t in trades:
        if t.get("status") == "closed" and t.get("result_R") is not None:
            by.setdefault(_strat_name(t), []).append(t["result_R"])
    if by:
        lines += ["", "— حسب الاستراتيجية —"]
        for name, rs in by.items():
            wr = round(sum(1 for r in rs if r > 0) / len(rs) * 100)
            lines.append(f"• {name}: {len(rs)} صفقة | فوز {wr}% | "
                         f"{sum(rs):+.2f}R")
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
