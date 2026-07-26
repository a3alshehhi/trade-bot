#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_log.py — السجل الموحّد لكل صفقات البوت.

يدمج ثلاثة مصادر في ملف واحد (`unified_data.json`) تقرأه لوحة واحدة
(`docs/unified.html`) بنفس تصميم المتتبّع الورقي الحالي:

  1) بايننس ديمو   — sd_positions_stable_binance.json + sd_ledger_stable_binance.json
  2) بايبت تيستنت  — sd_positions_stable.json        + sd_ledger_stable.json
  3) المتتبّع الورقي — paper_trades.json (كل البوتات الورقية)

كل صفقة تحمل وسماً يبيّن مصدرها (بايننس ديمو / بايبت تيستنت / ورقية).

═══ قاعدة «النمط الحقيقي» (قرار بو محمد 2026-07-26) ═══
كل صفقة — أياً كان مصدرها — يُعاد احتساب نتيجتها على الشموع الحقيقية
بقاعدة **ذيل الشمعة**: إن لمس قاع الشمعة الوقف فالصفقة موقوفة، وإن لمست
قمتها الهدف فالهدف محقَّق. تماماً كأمر وقف/هدف راكد على المنصة.

هذا يُصلح التضارب الذي ظهر في ALICE: المنفّذ يقرأ السعر كل ~15 دقيقة
فيفوّت ذيول الشموع ويسجّل ربحاً، بينما الواقع أن الوقف كان سيُضرب.

الإدارة المطبَّقة (مطابقة للبوت الحيّ): 50/50 —
  • الهدف الأول: بيع 50% + رفع الوقف إلى max(الدخول، الدخول + 0.3R)
  • الهدف الثاني: بيع الباقي
  • الوقف لا ينزل تحت سعر الدخول أبداً بعد الهدف الأول
  • العمولة محتسَبة على الدخول والخروج (FEE_PCT لكل جهة)

الأوضاع:
  build   — يبني unified_data.json من المصادر (الافتراضي)
  verify  — يطبع مقارنة «الواقعي مقابل المسجَّل» لكشف الفجوات

⚠️ أداة تحليل تعليمية ونتائج افتراضية — ليست نصيحة مالية.
"""
import os
import re
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests

# ── إعدادات ────────────────────────────────────────────────────────────────
OUT_FILE = os.environ.get("UNIFIED_OUT", "unified_data.json")

SOURCES = [
    # (المفتاح، الاسم العربي، ملف المراكز المفتوحة، ملف الدفتر المغلق، حجم الساق $)
    ("binance", "بايننس ديمو",
     "sd_positions_stable_binance.json", "sd_ledger_stable_binance.json", 100.0),
    ("bybit", "بايبت تيستنت",
     "sd_positions_stable.json", "sd_ledger_stable.json", 300.0),
]
PAPER_FILE = os.environ.get("UNIFIED_PAPER", "paper_trades.json")

# البوتات الفعّالة فقط (المُشغّلة حالياً) — تُستبعد البوتات الموقوفة (الحوت، trendwave، RSI70…).
# قابلة للضبط عبر UNIFIED_ACTIVE_LABELS (مفصولة بفواصل)؛ فارغة = بلا فلترة.
ACTIVE_LABELS = [x.strip() for x in os.environ.get(
    "UNIFIED_ACTIVE_LABELS",
    # البوتات الشغّالة (طلب بو محمد 2026-07-26): البوت المستقر + الفيواب الأسبوعي
    # (وسمه «العرض/الطلب») + عرض/طلب+دايفرجنس + vwbtc. المستبعد: الحوت + trendwave + الهنتر.
    "SD Stable,العرض/الطلب,عرض/طلب+دايفرجنس,vwbtc_bot"
).split(",") if x.strip()]

def _label_active(lbl):
    return (not ACTIVE_LABELS) or ((lbl or "").strip() in ACTIVE_LABELS)

# إدارة 50/50 (مطابقة للبوت الحيّ)
LOCK_R = float(os.environ.get("SD_LOCK_R", "0.3"))
TP1_FRACTION = 0.5
FEE_PCT = float(os.environ.get("UNIFIED_FEE_PCT", "0.1"))   # عمولة لكل جهة (%)

BINANCE_INTERVAL = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
BARS_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

API_HOSTS = [
    "https://data-api.binance.vision",   # يعمل من خوادم أمريكا (GitHub Actions)
    "https://api.binance.com",
]

_CANDLE_CACHE = {}
NO_NETWORK = False        # يُرفع إن فشل الجلب فنكتفي بالنتائج المسجَّلة


# ── أدوات ──────────────────────────────────────────────────────────────────
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _ts_ms(s):
    """يحوّل نصاً زمنياً (ISO أو 'YYYY-MM-DD HH:MM:SS') إلى ميلي-ثانية UTC."""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    s = re.sub(r"[+-]\d\d:\d\d$", "", s).replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _pct(entry, price):
    return (price - entry) / entry * 100.0 if entry else 0.0


# ── جلب الشموع ─────────────────────────────────────────────────────────────
def klines(symbol, interval, start_ms, limit=1000):
    """شموع حقيقية من باينانس ابتداءً من start_ms. يعيد [[openMs,o,h,l,c],...]."""
    global NO_NETWORK
    if NO_NETWORK:
        return []
    key = (symbol, interval, start_ms)
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]
    for host in API_HOSTS:
        try:
            r = requests.get(
                f"{host}/api/v3/klines",
                params={"symbol": symbol, "interval": interval,
                        "startTime": int(start_ms), "limit": limit},
                timeout=20,
            )
            if r.status_code != 200:
                continue
            raw = r.json()
            out = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])]
                   for k in raw]
            _CANDLE_CACHE[key] = out
            return out
        except Exception:
            continue
    NO_NETWORK = True
    print("⚠️  تعذّر جلب الشموع (شبكة محجوبة) — سيُعتمد على النتائج المسجَّلة.",
          file=sys.stderr)
    return []


# ── محاكاة «النمط الحقيقي» بذيل الشمعة ─────────────────────────────────────
def simulate_real(symbol, tf, entry, stop, targets, start_ms, usd):
    """يعيد نتيجة الصفقة كما لو كانت أوامر وقف/هدف راكدة على المنصة.

    القاعدة: قاع الشمعة يلمس الوقف = وقف. قمتها تلمس الهدف = هدف.
    الوقف يُفحص أولاً داخل نفس الشمعة (الأكثر تحفّظاً وواقعية).
    """
    interval = BINANCE_INTERVAL.get(tf, "1h")
    bars = klines(symbol, interval, start_ms)
    risk = entry - stop
    if risk <= 0 or not bars:
        return None

    tp1 = targets[0] if targets else None
    tp2 = targets[1] if len(targets) > 1 else None

    cur_stop = stop
    remaining = 1.0          # الجزء المتبقّي من المركز
    realized = 0.0           # الربح المحقّق بالنسبة المئوية (مرجَّح بالحصص)
    hits = []
    events = []
    status = "open"
    exit_price = None
    exit_reason = None
    closed_ms = None

    for ts, o, h, l, c in bars:
        # (1) الوقف أولاً — تحفّظاً (داخل الشمعة لا نعرف الترتيب)
        if l <= cur_stop:
            part = _pct(entry, cur_stop) * remaining
            realized += part
            if cur_stop > stop + 1e-12:
                exit_reason = "قفل ربح" if cur_stop > entry + 1e-12 else "تعادل"
            else:
                exit_reason = "وقف خسارة"
            events.append({"ts": _iso(ts), "type": "stop",
                           "price": cur_stop, "pct": round(part, 3)})
            exit_price = cur_stop
            remaining = 0.0
            status = "closed"
            closed_ms = ts
            break

        # (2) الهدف الأول: بيع 50% + رفع الوقف
        if tp1 is not None and 1 not in hits and h >= tp1:
            hits.append(1)
            realized += _pct(entry, tp1) * TP1_FRACTION
            remaining -= TP1_FRACTION
            lock = max(entry, entry + LOCK_R * risk)
            if tp1 <= lock:              # هدف قريب جداً → اكتفِ بالتعادل
                lock = entry
            cur_stop = max(cur_stop, lock)
            events.append({"ts": _iso(ts), "type": "tp1",
                           "price": tp1, "pct": round(_pct(entry, tp1), 3)})

        # (3) الهدف الثاني: بيع الباقي
        if tp2 is not None and 1 in hits and 2 not in hits and h >= tp2:
            hits.append(2)
            realized += _pct(entry, tp2) * remaining
            events.append({"ts": _iso(ts), "type": "tp2",
                           "price": tp2, "pct": round(_pct(entry, tp2), 3)})
            exit_price = tp2
            exit_reason = "هدف 2"
            remaining = 0.0
            status = "closed"
            closed_ms = ts
            break

    # العمولة: دخول (كامل) + خروج (بحسب ما بيع)
    fee = FEE_PCT * (1.0 + (1.0 - remaining))
    net_pct = realized - (fee if status == "closed" or hits else FEE_PCT)

    last_close = bars[-1][4] if bars else None
    unreal_pct = (_pct(entry, last_close) * remaining) if (status == "open" and last_close) else 0.0

    return {
        "status": status,
        "hits": hits,
        "realized_pct": round(realized, 3),
        "net_pct": round(net_pct, 3),
        "result_pct": round(net_pct, 3) if status == "closed" else None,
        "result_r": round(realized / (risk / entry * 100.0), 3) if risk else None,
        "unrealized_pct": round(unreal_pct, 3),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "closed_at": _iso(closed_ms),
        "cur_stop": cur_stop,
        "remaining": round(remaining, 4),
        "pnl_usdt": round(usd * net_pct / 100.0, 4),
        "last_price": last_close,
        "events": events,
    }


# ── قراءة المصادر وتوحيدها ────────────────────────────────────────────────
def _rec(source, source_ar, symbol, label, tf, entry, stop, targets,
         opened_at, bar_ms, usd, side="buy"):
    return {
        "id": f"{source}|{symbol}|{opened_at}",
        "source": source,
        "source_ar": source_ar,
        "symbol": symbol,
        "label": label or "—",
        "side": side,
        "timeframe": tf,
        "entry": entry,
        "stop_orig": stop,
        "stop": stop,
        "targets": targets,
        "opened_at": opened_at,
        "_bar_ms": bar_ms,
        "usd": usd,
        "hits": [],
        "status": "open",
        "closed_at": None,
        "realized_pct": 0.0,
        "result_pct": None,
    }


def collect_exec_positions():
    """المراكز المفتوحة حالياً لدى المنفّذ على المنصتين."""
    out = []
    for src, src_ar, pos_file, _ledger, usd in SOURCES:
        pos = _load(pos_file, {})
        if not isinstance(pos, dict):
            continue
        for sym, p in pos.items():
            entry = float(p.get("avg_entry") or p.get("entry") or 0)
            stop = float(p.get("init_stop") or p.get("stop") or 0)
            if entry <= 0 or stop <= 0:
                continue
            tgs = [t for t in (p.get("tp1"), p.get("tp2")) if t]
            opened = p.get("opened_ts") or p.get("opened") or ""
            out.append(_rec(src, src_ar, sym, p.get("label"), p.get("tf", "1h"),
                            entry, stop, [float(t) for t in tgs],
                            opened, _ts_ms(opened), float(p.get("leg_usd") or usd)))
    return out


def collect_exec_closed():
    """الصفقات المغلقة في دفاتر المنفّذ. تُجمَّع الأسطر الجزئية في صفقة واحدة."""
    out = []
    for src, src_ar, _pos, ledger_file, usd in SOURCES:
        rows = _load(ledger_file, [])
        if not isinstance(rows, list):
            continue
        groups = {}
        for r in rows:
            key = (r.get("symbol"), round(float(r.get("entry") or 0), 10),
                   r.get("tf"), r.get("label"))
            groups.setdefault(key, []).append(r)
        for (sym, entry, tf, label), rs in groups.items():
            if not sym or not entry:
                continue
            rs = sorted(rs, key=lambda x: str(x.get("closed_ts") or ""))
            first_close = str(rs[0].get("closed_ts") or "")
            exec_pct = round(sum(float(x.get("pnl_pct") or 0) *
                                 (0.5 if len(rs) > 1 else 1.0) for x in rs), 3)
            exec_usdt = round(sum(float(x.get("pnl_usdt") or 0) for x in rs), 4)
            rec = _rec(src, src_ar, sym, label, tf or "1h", entry, 0.0, [],
                       first_close, None, usd)
            rec["_exec_only"] = True
            rec["exec_pct"] = exec_pct
            rec["exec_usdt"] = exec_usdt
            rec["exec_reasons"] = [x.get("reason") for x in rs]
            rec["status"] = "closed"
            rec["closed_at"] = str(rs[-1].get("closed_ts") or "")
            rec["result_pct"] = exec_pct
            out.append(rec)
    return out


def collect_paper():
    """صفقات المتتبّع الورقي (كل البوتات).

    يقرأ paper_trades.json، وإن كان فارغاً يرجع إلى paper_data.json
    (ملف اللوحة) لأنه يحوي نفس الصفقات بصيغة مختصرة.
    """
    out = []
    trades = _load(PAPER_FILE, [])
    if isinstance(trades, dict):
        trades = trades.get("trades", [])
    if not trades:
        alt = _load("paper_data.json", {})
        trades = alt.get("trades", []) if isinstance(alt, dict) else []
    for t in trades or []:
        entry = float(t.get("entry") or 0)
        stop = float(t.get("stop_orig") or t.get("stop") or 0)
        if entry <= 0 or stop <= 0:
            continue
        opened = t.get("opened_at") or ""
        rec = _rec("paper", "ورقية", t.get("symbol", ""), t.get("label"),
                   t.get("timeframe", "1h"), entry, stop,
                   [float(x) for x in (t.get("targets") or [])],
                   opened, _ts_ms(opened), float(t.get("usd") or 100.0),
                   side=t.get("side", "buy"))
        rec["exec_pct"] = t.get("result_pct")
        # الحالة المسجَّلة تبقى احتياطاً إن تعذّرت إعادة الاحتساب الواقعي
        rec["status"] = t.get("status", "open")
        rec["closed_at"] = t.get("closed_at")
        rec["hits"] = t.get("hits") or []
        if rec["status"] == "closed":
            rec["result_pct"] = t.get("result_pct")
            rec["realized_pct"] = t.get("realized_pct") or t.get("result_pct") or 0.0
        out.append(rec)
    return out


# ── البناء ─────────────────────────────────────────────────────────────────
def build():
    records = collect_exec_positions() + collect_paper() + collect_exec_closed()

    # إبقاء البوتات الفعّالة فقط (استبعاد الموقوفة: الحوت، trendwave، RSI70…)
    records = [r for r in records if _label_active(r.get("label"))]

    # إزالة التكرار: نفس (المصدر، الرمز، الدخول) — نُبقي الأغنى بياناً
    seen = {}
    for r in records:
        key = (r["source"], r["symbol"], round(float(r["entry"]), 10))
        if key not in seen or (not r.get("_exec_only") and seen[key].get("_exec_only")):
            seen[key] = r
    records = list(seen.values())

    simulated = 0
    for r in records:
        if r.get("_exec_only") or not r.get("_bar_ms") or not r.get("targets"):
            continue
        sim = simulate_real(r["symbol"], r["timeframe"], r["entry"],
                            r["stop_orig"], r["targets"], r["_bar_ms"], r["usd"])
        if not sim:
            continue
        simulated += 1
        r["exec_pct"] = r.get("exec_pct")          # يبقى للمقارنة
        r.update({k: v for k, v in sim.items() if k != "events"})
        r["events"] = sim["events"]
        r["real_rule"] = "wick"

    for r in records:
        r.pop("_bar_ms", None)
        r.pop("_exec_only", None)

    records.sort(key=lambda x: str(x.get("opened_at") or ""), reverse=True)

    closed = [r for r in records if r["status"] == "closed" and r.get("result_pct") is not None]
    wins = [r for r in closed if r["result_pct"] > 0]
    losses = [r for r in closed if r["result_pct"] < 0]
    gross_w = sum(r["result_pct"] for r in wins)
    gross_l = abs(sum(r["result_pct"] for r in losses))

    data = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "rule": "wick",                     # قاعدة الخروج: ذيل الشمعة
        "fee_pct_per_side": FEE_PCT,
        "trades": records,
        "stats": {
            "total": len(records),
            "open": sum(1 for r in records if r["status"] == "open"),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else 0,
            "net_pct": round(sum(r["result_pct"] for r in closed), 2),
            "pf": round(gross_w / gross_l, 2) if gross_l else None,
            "simulated": simulated,
        },
    }
    _save(OUT_FILE, data)
    os.makedirs("docs", exist_ok=True)
    _save(os.path.join("docs", OUT_FILE), data)

    s = data["stats"]
    print(f"✅ {OUT_FILE}: {s['total']} صفقة "
          f"({s['open']} مفتوحة · {s['closed']} مغلقة) · "
          f"أُعيد احتسابها واقعياً: {s['simulated']} · "
          f"فوز {s['win_rate']}% · صافي {s['net_pct']}% · PF {s['pf']}")
    return data


def verify():
    """مقارنة النتيجة الواقعية بالمسجَّلة — لكشف فجوة الـpolling."""
    data = build()
    rows = [r for r in data["trades"]
            if r.get("real_rule") and r.get("exec_pct") is not None
            and r.get("result_pct") is not None]
    print(f"\n{'الرمز':<14}{'المصدر':<14}{'الواقعي':>10}{'المسجَّل':>10}{'الفرق':>10}  السبب")
    print("─" * 78)
    gaps = 0
    for r in sorted(rows, key=lambda x: abs((x["result_pct"] or 0) - (x["exec_pct"] or 0)),
                    reverse=True)[:40]:
        diff = round(r["result_pct"] - r["exec_pct"], 2)
        if abs(diff) > 0.05:
            gaps += 1
        print(f"{r['symbol']:<14}{r['source_ar']:<14}"
              f"{r['result_pct']:>9.2f}%{r['exec_pct']:>9.2f}%{diff:>9.2f}%  "
              f"{r.get('exit_reason') or '—'}")
    print(f"\nصفقات تختلف نتيجتها بين الواقعي والمسجَّل: {gaps} من {len(rows)}")


def main():
    ap = argparse.ArgumentParser(description="السجل الموحّد لصفقات البوت")
    ap.add_argument("mode", nargs="?", default="build", choices=["build", "verify"])
    a = ap.parse_args()
    verify() if a.mode == "verify" else build()


if __name__ == "__main__":
    main()
