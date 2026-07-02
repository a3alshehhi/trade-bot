#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bybit_exec.py — منفّذ صفقات Spot على Bybit Testnet (حساب افتراضي).

يربط إشارات البوت بحساب تجريبي حقيقي على Bybit عبر واجهة v5 API مع توقيع HMAC،
بدون أي مكتبات إضافية (يعتمد على requests فقط). يعمل على Testnet (أموال وهمية)
أو Mainnet إذا غُيّر BYBIT_ENV — والافتراضي Testnet للأمان.

الأوضاع (CLI):
  test       — فحص الاتصال + التوقيع: يطبع وقت الخادم ورصيد الحساب.
  balance    — يعرض أرصدة العملات (USDT + أي حيازات) والقيمة الإجمالية.
  buy        — أمر شراء سوق اختباري:  buy --symbol BTCUSDT --usdt 100
  sell       — أمر بيع سوق:          sell --symbol BTCUSDT --qty 0.001  (أو --all)
  positions  — يعرض الحيازات الحالية والأوامر المفتوحة.
  sync       — يرسل ملخص الحساب (الرصيد + الحيازات) إلى تيليجرام.

المفاتيح تُقرأ من متغيّرات البيئة:
  BYBIT_API_KEY, BYBIT_API_SECRET   (إلزامية لكل ما عدا فحص الوقت العام)
  BYBIT_ENV = testnet | mainnet     (الافتراضي testnet)

محلياً: ضع المفاتيح في ملف bybit_keys.env (غير مرفوع لـGit) ويُحمَّل تلقائياً.

⚠️ أداة تعليمية على حساب افتراضي. على Testnet لا تتحرّك أموال حقيقية.
   لا تستخدم مفاتيح حساب حقيقي هنا. ليست نصيحة مالية.
"""
import os
import sys
import json
import time
import hmac
import hashlib
import argparse

import requests

# ── تحميل المفاتيح من ملف محلي إن وُجد (KEY=VALUE في كل سطر) ─────────────────
def _load_env_file(path="bybit_keys.env"):
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
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_env_file()

# ── الإعدادات ───────────────────────────────────────────────────────────────
ENV = os.environ.get("BYBIT_ENV", "testnet").lower()
BASE_URL = ("https://api-testnet.bybit.com" if ENV != "mainnet"
            else "https://api.bybit.com")
API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
RECV_WINDOW = "10000"
TIMEOUT = 20

SEP = "━━━━━━━━━━━━━━━━━━"


# ── توقيع v5 ─────────────────────────────────────────────────────────────────
def _ts():
    return str(int(time.time() * 1000))


def _sign(timestamp, payload):
    """التوقيع = HMAC_SHA256(secret, ts + apiKey + recvWindow + payload)."""
    pre = timestamp + API_KEY + RECV_WINDOW + payload
    return hmac.new(API_SECRET.encode(), pre.encode(), hashlib.sha256).hexdigest()


def _headers(timestamp, sign):
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": sign,
        "Content-Type": "application/json",
    }


def _require_keys():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "⚠️ لا توجد مفاتيح. ضع BYBIT_API_KEY و BYBIT_API_SECRET في bybit_keys.env "
            "أو في متغيّرات البيئة. راجع BYBIT_SETUP_ar.md.")


def _get(path, params=None):
    """طلب GET موثّق."""
    _require_keys()
    params = params or {}
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    ts = _ts()
    sign = _sign(ts, qs)
    url = f"{BASE_URL}{path}" + (f"?{qs}" if qs else "")
    r = requests.get(url, headers=_headers(ts, sign), timeout=TIMEOUT)
    return _parse(r)


def _post(path, body):
    """طلب POST موثّق."""
    _require_keys()
    payload = json.dumps(body, separators=(",", ":"))
    ts = _ts()
    sign = _sign(ts, payload)
    url = f"{BASE_URL}{path}"
    r = requests.post(url, headers=_headers(ts, sign), data=payload, timeout=TIMEOUT)
    return _parse(r)


def _parse(r):
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"رد غير متوقّع ({r.status_code}): {r.text[:300]}")
    if data.get("retCode") not in (0, None):
        raise RuntimeError(f"Bybit خطأ {data.get('retCode')}: {data.get('retMsg')}")
    return data.get("result", data)


# ── واجهات عامة ──────────────────────────────────────────────────────────────
def server_time():
    """وقت خادم Bybit (عام، بلا توقيع) — لاختبار الوصول الشبكي."""
    r = requests.get(f"{BASE_URL}/v5/market/time", timeout=TIMEOUT)
    return r.json()


def last_price(symbol):
    """آخر سعر تداول لزوج Spot."""
    r = requests.get(f"{BASE_URL}/v5/market/tickers",
                     params={"category": "spot", "symbol": symbol}, timeout=TIMEOUT)
    lst = r.json().get("result", {}).get("list", [])
    return float(lst[0]["lastPrice"]) if lst else None


def wallet_balance():
    """أرصدة الحساب الموحّد (UNIFIED). يرجع dict: coin -> {amount, usd_value}."""
    res = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    out = {"total_usd": 0.0, "coins": {}}
    for acct in res.get("list", []):
        out["total_usd"] = float(acct.get("totalEquity") or 0)
        for c in acct.get("coin", []):
            amt = float(c.get("walletBalance") or 0)
            if amt <= 0:
                continue
            out["coins"][c["coin"]] = {
                "amount": amt,
                "usd_value": float(c.get("usdValue") or 0),
            }
    return out


def coin_qty(coin):
    """كمية عملة معيّنة في المحفظة."""
    return wallet_balance()["coins"].get(coin, {}).get("amount", 0.0)


def instrument_filters(symbol):
    """قيود الكمية/السعر للزوج: خطوة الكمية ودقّة، أدنى قيمة أمر."""
    r = requests.get(f"{BASE_URL}/v5/market/instruments-info",
                     params={"category": "spot", "symbol": symbol}, timeout=TIMEOUT)
    lst = r.json().get("result", {}).get("list", [])
    if not lst:
        return {}
    f = lst[0]
    lot = f.get("lotSizeFilter", {})
    return {
        "basePrecision": lot.get("basePrecision"),
        "minOrderQty": lot.get("minOrderQty"),
        "minOrderAmt": lot.get("minOrderAmt"),
        "quotePrecision": lot.get("quotePrecision"),
    }


def _round_step(qty, step):
    """تقريب الكمية لأسفل وفق خطوة الدقّة (مثل 0.000001)."""
    if not step:
        return qty
    from decimal import Decimal, ROUND_DOWN
    q = Decimal(str(qty)).quantize(Decimal(str(step)), rounding=ROUND_DOWN)
    return float(q)


def market_buy(symbol, usdt):
    """شراء سوق بقيمة USDT محدّدة (marketUnit=quoteCoin). يرجع نتيجة الأمر."""
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": str(usdt),
        "marketUnit": "quoteCoin",
    }
    return _post("/v5/order/create", body)


def market_sell(symbol, base_qty):
    """بيع سوق لكمية من العملة الأساس (marketUnit=baseCoin)."""
    f = instrument_filters(symbol)
    qty = _round_step(base_qty, f.get("basePrecision"))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "qty": str(qty),
        "marketUnit": "baseCoin",
    }
    return _post("/v5/order/create", body)


def open_orders(symbol=None):
    """الأوامر المفتوحة (Spot)."""
    params = {"category": "spot"}
    if symbol:
        params["symbol"] = symbol
    return _get("/v5/order/realtime", params).get("list", [])


# ── عرض ──────────────────────────────────────────────────────────────────────
def _fmt(v):
    v = float(v)
    return f"{v:.8f}".rstrip("0").rstrip(".") if 0 < v < 1 else f"{v:,.2f}"


def cmd_test():
    print(f"🌐 البيئة: {ENV}  ({BASE_URL})")
    t = server_time()
    print(f"⏱️  وقت الخادم: retCode={t.get('retCode')} "
          f"time={t.get('result', {}).get('timeSecond')}")
    bal = wallet_balance()
    print(f"✅ التوقيع يعمل. إجمالي الحساب ≈ {_fmt(bal['total_usd'])} USD")
    print(f"   عملات: {', '.join(bal['coins'].keys()) or '—'}")


def cmd_balance():
    bal = wallet_balance()
    print(f"💰 إجمالي الحساب ≈ {_fmt(bal['total_usd'])} USD  ({ENV})")
    for coin, d in sorted(bal["coins"].items(),
                          key=lambda x: -x[1]["usd_value"]):
        print(f"   {coin:<8} {_fmt(d['amount']):>16}   ≈ {_fmt(d['usd_value'])} USD")


def cmd_buy(symbol, usdt):
    px = last_price(symbol)
    print(f"🛒 شراء سوق {symbol} بقيمة {usdt} USDT (السعر الحالي ≈ {_fmt(px)})")
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
    holdings = {c: d for c, d in bal["coins"].items() if c != "USDT"}
    print(f"📊 الحيازات ({ENV}):")
    if not holdings:
        print("   لا توجد حيازات (نقد فقط).")
    for coin, d in holdings.items():
        print(f"   {coin:<8} {_fmt(d['amount']):>16}  ≈ {_fmt(d['usd_value'])} USD")
    oo = open_orders()
    print(f"📋 أوامر مفتوحة: {len(oo)}")
    for o in oo:
        print(f"   {o.get('symbol')} {o.get('side')} {o.get('orderType')} "
              f"qty={o.get('qty')} @ {o.get('price')}")


def cmd_sync():
    """يرسل ملخص الحساب إلى تيليجرام."""
    try:
        from trading_bot import send_telegram
    except Exception:
        send_telegram = None
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    bal = wallet_balance()
    lines = [SEP, f"🤖 حساب Bybit التجريبي ({ENV})", SEP,
             f"💰 الإجمالي ≈ {_fmt(bal['total_usd'])} USD", ""]
    for coin, d in sorted(bal["coins"].items(), key=lambda x: -x[1]["usd_value"]):
        lines.append(f"• {coin}: {_fmt(d['amount'])}  ≈ {_fmt(d['usd_value'])} USD")
    oo = open_orders()
    lines += ["", f"📋 أوامر مفتوحة: {len(oo)}", "",
              "⚠️ حساب افتراضي تعليمي — ليست نصيحة مالية."]
    msg = "\n".join(lines)
    print(msg)
    if send_telegram and token and chat:
        send_telegram(token, chat, msg)
        print("📨 أُرسل إلى تيليجرام.")
    else:
        print("ℹ️ لم يُرسل (لا توجد بيانات تيليجرام).")


def main():
    ap = argparse.ArgumentParser(description="منفّذ Bybit Testnet (Spot)")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("test")
    sub.add_parser("balance")
    sub.add_parser("positions")
    sub.add_parser("sync")
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
        elif args.mode == "sync":
            cmd_sync()
        elif args.mode == "buy":
            cmd_buy(args.symbol, args.usdt)
        elif args.mode == "sell":
            cmd_sell(args.symbol, args.qty, args.all)
    except (RuntimeError, SystemExit) as e:
        print(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
