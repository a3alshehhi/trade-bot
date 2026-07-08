#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
مقارنة أداء البوتات الثلاثة (الرئيسي / عرض-طلب+تشبّع / عرض-طلب+دايفرجنس) — طلب بو محمد 2026-07-08.

يقرأ دفاتر التنفيذ (Bybit + Binance) لكل بوت، يحسب إحصائية الصفقات المغلقة (سيقان، فوز%،
صافي USDT، مجموع R، متوسط R، معامل الربح PF) وعدد المراكز المفتوحة، ثم يرسل جدول مقارنة
إلى تيليجرام. للقراءة فقط — لا يعدّل أي حالة. (نتائج testnet/demo، ليست حقيقية.)
"""
import os
import json
import urllib.request
import urllib.parse

# (اسم العرض، دفاتر بايبت+بايننس، ملفات المراكز بايبت+بايننس)
BOTS = [
    ("الرئيسي (عرض/طلب)",
     ["sd_ledger.json", "sd_ledger_binance.json"],
     ["sd_positions.json", "sd_positions_binance.json"]),
    ("عرض/طلب+تشبّع",
     ["sd_ledger_os21.json", "sd_ledger_os21_binance.json"],
     ["sd_pos_os21.json", "sd_pos_os21_binance.json"]),
    ("عرض/طلب+دايفرجنس",
     ["sd_ledger_div.json", "sd_ledger_div_binance.json"],
     ["sd_pos_div.json", "sd_pos_div_binance.json"]),
]


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def stats(ledgers):
    """يجمع سيقان دفاتر عدّة ويحسب الإحصائية."""
    legs = []
    for p in ledgers:
        d = _load(p, [])
        if isinstance(d, list):
            legs.extend(d)
    n = len(legs)
    pnl = sum(float(x.get("pnl_usdt", 0) or 0) for x in legs)
    wins = sum(1 for x in legs if float(x.get("pnl_usdt", 0) or 0) > 0)
    rs = [float(x["r"]) for x in legs if x.get("r") is not None]
    tot_r = sum(rs)
    avg_r = tot_r / len(rs) if rs else 0.0
    pos_sum = sum(float(x.get("pnl_usdt", 0) or 0) for x in legs if float(x.get("pnl_usdt", 0) or 0) > 0)
    neg_sum = abs(sum(float(x.get("pnl_usdt", 0) or 0) for x in legs if float(x.get("pnl_usdt", 0) or 0) < 0))
    pf = (pos_sum / neg_sum) if neg_sum > 0 else (float("inf") if pos_sum > 0 else 0.0)
    win_pct = (wins / n * 100) if n else 0.0
    return dict(n=n, win_pct=win_pct, pnl=pnl, tot_r=tot_r, avg_r=avg_r, pf=pf, n_r=len(rs))


def open_count(pos_files):
    total = 0
    for p in pos_files:
        d = _load(p, {})
        if isinstance(d, dict):
            total += len(d)
        elif isinstance(d, list):
            total += len(d)
    return total


def _fmt_pf(pf):
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_report():
    lines = ["📊 مقارنة أداء البوتات (صفقات مغلقة · testnet/demo)", "➖➖➖➖➖➖➖➖➖"]
    for name, ledgers, pos_files in BOTS:
        s = stats(ledgers)
        opn = open_count(pos_files)
        if s["n"] == 0:
            lines.append(f"🤖 {name}\nلا صفقات مغلقة بعد · مفتوحة={opn}")
        else:
            lines.append(
                f"🤖 {name}\n"
                f"سيقان={s['n']} · فوز {s['win_pct']:.0f}% · صافي {s['pnl']:+.2f} USDT\n"
                f"مجموع {s['tot_r']:+.2f}R · متوسط {s['avg_r']:+.2f}R · PF={_fmt_pf(s['pf'])} · مفتوحة={opn}"
            )
        lines.append("➖➖➖➖➖➖➖➖➖")
    lines.append("ℹ️ نتائج تجريبية (Bybit Testnet + Binance Demo) — ليست حقيقية.")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", os.environ.get("TG_TOKEN", ""))
    chat = os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_CHAT", ""))
    if not token or not chat:
        print("(no telegram creds — طباعة فقط)")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
        print("✅ أُرسل إلى تيليجرام")
    except Exception as ex:
        print("تعذّر الإرسال:", ex)


if __name__ == "__main__":
    report = build_report()
    print(report)
    send_telegram(report)
