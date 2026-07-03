#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
binance_exec.py — منفّذ صفقات Spot على Binance *Demo Mode* (حساب افتراضي).

يربط إشارات البوت بحساب Binance التجريبي (Demo Trading) عبر واجهة Spot v3 API
مع توقيع HMAC، بدون مكتبات إضافية (requests فقط). نمط بايننس للتوقيع يختلف عن
بايبت: التوقيع على *سلسلة الاستعلام* + ترويسة X-MBX-APIKEY.

النطاق (وثائق بايننس الرسمية):
  Demo Mode REST = https://demo-api.binance.com/api   (يبدّل api.binance.com/api)
  المفاتيح تُنشأ من demo.binance.com ← API Management.

الأوضاع (CLI):
  test       — فحص الاتصال + التوقيع: وقت الخادم + رصيد الحساب.
  balance    — أرصدة العملات والقيمة الإجمالية.
  buy        — أمر شراء سوق بقيمة USDT: buy --symbol BTCUSDT --usdt 300
  sell       — أمر بيع سوق:           sell --symbol BTCUSDT --qty 0.001 (أو --all)
  positions  — الحيازات + الأوامر المفتوحة.

المفاتيح من متغيّرات البيئة:
  BINANCE_API_KEY, BINANCE_API_SECRET
  BINANCE_ENV      = demo | mainnet   (الافتراضي demo)
  BINANCE_BASE_URL = بادئة بروكسي اختيارية (مثلاً https://<proxy>.vercel.app/binance)

⚠️ أداة تعليمية على حساب افتراضي (Demo). لا تتحرّك أموال حقيقية. ليست نصيحة مالية.
"""
import os
import sys
import time
import hmac
import hashlib
import argparse
from urllib.parse import urlencode

import requests


# ── تحميل مفاتيح محلية إن وُجدت (KEY=VALUE) ─────────────────────────────────
def _load_env_file(path="binance_keys.env"):
    here = os.path.dirname(os.path.abspath(__file__))
    fp = path if os.path.isabs(path) else os.path.join(here, path)
    if not os.path.exists(fp):
        return
    with open(fp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

# ── الإعدادات ───────────────────────────────────────────────────────────────
ENV = os.environ.get("BINANCE_ENV", "demo").lower()

# بايننس يحجب IP أمريكا (خوادم GitHub) — نمرّر عبر بروكسي أوروبي عند توفّره.
# كل نطاق مرشّح ينتهي بـ/api ثم نضيف /v3/... بعده.
_BASE_CANDIDATES = [os.environ.get("BINANCE_BASE_URL")] + (
    ["https://demo-api.binance.com/api"]
    if ENV != "mainnet"
    else ["https://api.binance.com/api"])


def _pick_base_url():
    for u in _BASE_CANDIDATES:
        if not u:
            continue
        u = u.rstrip("/")
        try:
            r = requests.get(f"{u}/v3/time", timeout=8)
            if r.status_code == 200 and "serverTime" in r.json():
                print(f"binance_exec: النطاق المعتمد → {u}")
                return u
            print(f"binance_exec: {u} مرفوض (HTTP {r.status_code})")
        except Exception as ex:
            print(f"binance_exec: {u} فشل — {type(ex).__name__}")
    print("binance_exec: ⚠️ كل النطاقات محجوبة — سيُستخدم الافتراضي (يتوقع حجب).")
    return [u for u in _BASE_CANDIDATES if u][0].rstrip("/")


BASE_URL = _pick_base_url()
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
RECV_WINDOW = "10000"
TIMEOUT = 20

SEP = "━━━━━━━━━━━━━━━━━━"


# ── توقيع بايننس (HMAC على سلسلة الاستعلام) ──────────────────────────────────
def _require_keys():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "⚠️ لا توجد مفاتيح. ضع BINANCE_API_KEY و BINANCE_API_SECRET في "
            "binance_keys.env أو متغيّرات البيئة.")


def _sign(params):
    """يضيف timestamp/recvWindow، يوقّع سلسلة الاستعلام، ويعيدها موقّعة."""
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig


def _headers():
    return {"X-MBX-APIKEY": API_KEY}


def _parse(r):
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"رد غير متوقّع ({r.status_code}): {r.text[:300]}")
    if isinstance(data, dict) and data.get("code") is not None and data.get("code") != 200:
        # بايننس يرجع {"code":-xxxx,"msg":"..."} عند الخطأ
        if str(data.get("code")).lstrip("-").isdigit() and int(data["code"]) < 0:
            raise RuntimeError(f"Binance خطأ {data.get('code')}: {data.get('msg')}")
    return data


def _signed_get(path, params=None):
    _require_keys()
    q = _sign(params)
    r = requests.get(f"{BASE_URL}{path}?{q}", headers=_headers(), timeout=TIMEOUT)
    return _parse(r)


def _signed_post(path, params=None):
    _require_keys()
    q = _sign(params)
    r = requests.post(f"{BASE_URL}{path}?{q}", headers=_headers(), timeout=TIMEOUT)
    return _parse(r)


# ── واجهات عامة (بلا توقيع) ──────────────────────────────────────────────────
def server_time():
    r = requests.get(f"{BASE_URL}/v3/time", timeout=TIMEOUT)
    return r.json()


def last_price(symbol):
    """آخر سعر لزوج Spot."""
    r = requests.get(f"{BASE_URL}/v3/ticker/price",
                     params={"symbol": symbol}, timeout=TIMEOUT)
    try:
        return float(r.json().get("price"))
    except Exception:
        return None


def instrument_filters(symbol):
    """قيود الزوج: خطوة الكمية، أدنى قيمة أمر (minNotional)، أقصى كمية أمر سوق."""
    r = requests.get(f"{BASE_URL}/v3/exchangeInfo",
                     params={"symbol": symbol}, timeout=TIMEOUT)
    syms = r.json().get("symbols", []) if r.status_code == 200 else []
    if not syms:
        return {}
    filters = {f["filterType"]: f for f in syms[0].get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    mlot = filters.get("MARKET_LOT_SIZE", {})
    notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    return {
        "basePrecision": lot.get("stepSize"),        # خطوة الكمية (للتقريب)
        "minOrderQty": lot.get("minQty"),
        "maxOrderQty": lot.get("maxQty"),
        "maxMktOrderQty": mlot.get("maxQty") or lot.get("maxQty"),
        "minOrderAmt": notional.get("minNotional") or notional.get("notional"),
        "maxOrderAmt": None,                          # بايننس لا يحدّ قيمة أمر السوق نصّاً
    }


def _round_step(qty, step):
    """تقريب الكمية لأسفل وفق خطوة الدقّة (مثل 0.00001)."""
    if not step:
        return qty
    from decimal import Decimal, ROUND_DOWN
    q = Decimal(str(qty)).quantize(Decimal(str(step)), rounding=ROUND_DOWN)
    return float(q)


# ── حساب موثّق ────────────────────────────────────────────────────────────────
def wallet_balance():
    """أرصدة الحساب. يرجع {total_usd, coins:{coin:{amount, usd_value}}}."""
    res = _signed_get("/v3/account")
    out = {"total_usd": 0.0, "coins": {}}
    for b in res.get("balances", []):
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt <= 0:
            continue
        coin = b["asset"]
        if coin in ("USDT", "USDC", "FDUSD", "BUSD"):
            usd = amt
        else:
            px = last_price(coin + "USDT")
            usd = amt * px if px else 0.0
        out["coins"][coin] = {"amount": amt, "usd_value": usd}
        out["total_usd"] += usd
    return out


def coin_qty(coin):
    return wallet_balance()["coins"].get(coin, {}).get("amount", 0.0)


def market_buy(symbol, usdt):
    """شراء سوق بقيمة USDT محدّدة (quoteOrderQty). يرجع نتيجة الأمر."""
    return _signed_post("/v3/order", {
        "symbol": symbol, "side": "BUY", "type": "MARKET",
        "quoteOrderQty": str(usdt),
    })


def market_sell(symbol, base_qty):
    """بيع سوق لكمية من العملة الأساس (quantity)."""
    f = instrument_filters(symbol)
    qty = _round_step(base_qty, f.get("basePrecision"))
    return _signed_post("/v3/order", {
        "symbol": symbol, "side": "SELL", "type": "MARKET",
        "quantity": str(qty),
    })


def open_orders(symbol=None):
    params = {"symbol": symbol} if symbol else {}
    res = _signed_get("/v3/openOrders", params)
    return res if isinstance(res, list) else []


# ── عرض ──────────────────────────────────────────────────────────────────────
def _fmt(v):
    v = float(v)
    return f"{v:.8f}".rstrip("0").rstrip(".") if 0 < v < 1 else f"{v:,.2f}"


def cmd_test():
    print(f"🌐 البيئة: {ENV}  ({BASE_URL})")
    t = server_time()
    print(f"⏱️  وقت الخادم: {t.get('serverTime')}")
    bal = wallet_balance()
    print(f"✅ التوقيع يعمل. إجمالي الحساب ≈ {_fmt(bal['total_usd'])} USD")
    print(f"   عملات: {', '.join(bal['coins'].keys()) or '—'}")


def cmd_balance():
    bal = wallet_balance()
    print(f"💰 إجمالي الحساب ≈ {_fmt(bal['total_usd'])} USD  ({ENV})")
    for coin, d in sorted(bal["coins"].items(), key=lambda x: -x[1]["usd_value"]):
        print(f"   {coin:<8} {_fmt(d['amount']):>16}   ≈ {_fmt(d['usd_value'])} USD")


def cmd_buy(symbol, usdt):
    px = last_price(symbol)
    print(f"🛒 شراء سوق {symbol} بقيمة {usdt} USDT (السعر ≈ {_fmt(px)})")
    res = market_buy(symbol, usdt)
    print(f"✅ تم. رقم الأمر: {res.get('orderId')}")
    return res


def cmd_sell(symbol, qty, sell_all):
    base = symbol.replace("USDT", "")
    if sell_all:
        qty = coin_qty(base)
    if not qty or float(qty) <= 0:
        print(f"⚠️ لا توجد كمية من {base} للبيع.")
        return
    print(f"💸 بيع سوق {qty} {base}")
    res = market_sell(symbol, float(qty))
    print(f"✅ تم. رقم الأمر: {res.get('orderId')}")
    return res


def cmd_positions():
    bal = wallet_balance()
    holdings = {c: d for c, d in bal["coins"].items()
                if c not in ("USDT", "USDC", "FDUSD", "BUSD")}
    print(f"📊 الحيازات ({ENV}):")
    if not holdings:
        print("   لا توجد حيازات (نقد فقط).")
    for coin, d in holdings.items():
        print(f"   {coin:<8} {_fmt(d['amount']):>16}  ≈ {_fmt(d['usd_value'])} USD")
    oo = open_orders()
    print(f"📋 أوامر مفتوحة: {len(oo)}")


def main():
    ap = argparse.ArgumentParser(description="منفّذ Binance Demo (Spot)")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("test")
    sub.add_parser("balance")
    sub.add_parser("positions")
    b = sub.add_parser("buy")
    b.add_argument("--symbol", required=True)
    b.add_argument("--usdt", required=True, type=float)
    s = sub.add_parser("sell")
    s.add_argument("--symbol", required=True)
    s.add_argument("--qty", type=float, default=0)
    s.add_argument("--all", action="store_true")
    args = ap.parse_args()

    try:
        if args.mode == "test":
            cmd_test()
        elif args.mode == "balance":
            cmd_balance()
        elif args.mode == "positions":
            cmd_positions()
        elif args.mode == "buy":
            cmd_buy(args.symbol, args.usdt)
        elif args.mode == "sell":
            cmd_sell(args.symbol, args.qty, args.all)
    except (RuntimeError, SystemExit) as e:
        print(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
