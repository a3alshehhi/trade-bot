#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت «سحابة المتوسطات» (MA Cloud) — بوت منفصل مستقل تماماً — لا يشارك ملفات حالة أو دفاتر
مع أي بوت آخر في المشروع (لا sd_bot ولا hunter_bot).
==============================================================================
٥ متوسطات كما طلب بو محمد:
  ١) EMA20 على القمم (High)   ٢) EMA20 على القيعان (Low)   → سحابة سريعة
  ٣) EMA100 على القمم (High)  ٤) EMA100 على القيعان (Low)  → سحابة بطيئة (اتجاه متوسط)
  ٥) SMA300 على الإغلاق (Close)                              → فلتر النظام العام (bull/bear)

المنطق (اقتراح محلل فني):
  • فلتر النظام: الإغلاق فوق/تحت SMA300.
  • تأكيد الاتجاه المتوسط: سحابة EMA100 بالكامل فوق/تحت SMA300 (ليس مجرد لمسة).
  • الزناد: ارتداد داخل/عبر سحابة EMA20 ثم إغلاق يعيد الاختراق باتجاه الترند (شراء الارتداد
    لا مطاردة الاختراق).
  • الوقف: تحت/فوق قاع/قمة الارتداد نفسه أو حافة سحابة EMA100 (الأبعد أماناً، الأقرب فعلياً).
  • الأهداف: فيبوناتشي امتداد 1.272 / 1.618 لطول الموجة الدافعة الأخيرة (لا R-multiples ولا ATR،
    قاعدة المشروع الثابتة).
  • الخروج: إدارة 50/50 (جني 50% عند الهدف الأول + نقل الوقف للتعادل، 50% الباقية عند الهدف
    الثاني) + خروج إجباري إذا انكسرت سحابة EMA100 هيكلياً (انكسار الاتجاه المتوسط نفسه).

الأوضاع:
  python ma_cloud_bot.py backtest   # باك-تست حقيقي على بيانات فعلية (15m/1h/4h افتراضياً)
  python ma_cloud_bot.py scan       # فحص آخر شمعة مغلقة وإرسال أفضل الإشارات لتيليجرام

تنبيه: أداة تحليل تعليمية. لا تنفّذ صفقات بأموال حقيقية. التداول مخاطرة، وليست نصيحة مالية.
"""
import os, sys, time, math, json, datetime as dt
import requests

# ----------------------- إعدادات الاستراتيجية -----------------------
CFG = dict(
    ema20_len=20,
    ema100_len=100,
    sma300_len=300,
    pullback_lookback=6,     # عدد الشموع للبحث عن لمسة سحابة EMA20 قبل الإغلاق المُعيد للاختراق
    swing_lookback=40,       # نافذة تحديد الموجة الدافعة (للأهداف بالفيبوناتشي)
    fib_ext1=1.272,
    fib_ext2=1.618,
    stop_buf_pct=0.001,      # بافر صغير خلف الوقف الهيكلي (0.1%)
    max_ema_dist=0.05,       # فلتر تجنّب المطاردة: بُعد الإغلاق عن حافة سحابة EMA20 عند الإشارة
    pages=5,                 # صفحات جلب البيانات (كل صفحة ≈ 1000 شمعة)
    bt_hold=200,             # أقصى شموع لإمساك الصفقة بالباك-تست
    bt_symbols=40,
    bt_frames="15m,1h,4h",   # فريمات الباك-تست (فريم واحد لكل قيمة — لا حاجة لسياق أعلى منفصل)
    top_n=5,
)
CFG["bt_frames"]  = os.environ.get("MC_BT_FRAMES", CFG["bt_frames"])
CFG["bt_symbols"] = int(os.environ.get("MC_BT_SYMBOLS", CFG["bt_symbols"]))
CFG["bt_hold"]    = int(os.environ.get("MC_BT_HOLD", CFG["bt_hold"]))
CFG["pages"]      = int(os.environ.get("MC_BT_PAGES", CFG["pages"]))  # لتمديد فترة الباك-تست (كل صفحة ≈1000 شمعة)
CFG["entry_tf"]   = os.environ.get("MC_TF", "1h")   # فريم وضع scan الحي

BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
WATCHLIST = "watchlist.txt"
STATE_PATH = os.environ.get("MC_STATE", "ma_cloud_state.json")
DASH_LABEL = "سحابة المتوسطات"
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", os.environ.get("TG_TOKEN", ""))
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_CHAT", ""))


# ----------------------- جلب البيانات -----------------------
def fetch_klines(symbol, interval, pages=2):
    all_rows, end_time = [], None
    for _ in range(pages):
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        data = None
        for base in BINANCE_BASES:
            try:
                r = requests.get(f"{base}/api/v3/klines", params=params, timeout=12)
                if r.status_code == 200 and r.json():
                    data = r.json(); break
            except Exception:
                continue
        if not data:
            break
        all_rows = data + all_rows
        end_time = data[0][0] - 1
        if len(data) < 1000:
            break
    if not all_rows:
        return None
    m = {row[0]: row for row in all_rows}
    rows = sorted(m.values(), key=lambda x: x[0])
    return dict(
        t=[r[0] for r in rows], o=[float(r[1]) for r in rows], h=[float(r[2]) for r in rows],
        l=[float(r[3]) for r in rows], c=[float(r[4]) for r in rows], v=[float(r[5]) for r in rows])


# ----------------------- مؤشرات -----------------------
def ema(arr, n):
    k = 2 / (n + 1); out = [float("nan")] * len(arr); prev = None
    for i, x in enumerate(arr):
        prev = x if i == 0 else x * k + prev * (1 - k)
        out[i] = prev
    return out

def sma(arr, n):
    out = [float("nan")] * len(arr); s = 0.0
    for i, x in enumerate(arr):
        s += x
        if i >= n:
            s -= arr[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


# ----------------------- منطق سحابة المتوسطات -----------------------
def find_setup(sym, d):
    """يبحث عن Setup عند آخر شمعة مغلقة في d (الطرف الأخير من كل مصفوفة).
       يعيد dict للإشارة (long/short) أو None."""
    h, l, c = d["h"], d["l"], d["c"]
    n = len(c)
    need = CFG["sma300_len"] + CFG["swing_lookback"] + 5
    if n < need:
        return None

    e20h = ema(h, CFG["ema20_len"])
    e20l = ema(l, CFG["ema20_len"])
    e100h = ema(h, CFG["ema100_len"])
    e100l = ema(l, CFG["ema100_len"])
    s300 = sma(c, CFG["sma300_len"])
    i = n - 1
    vals = (e20h[i], e20l[i], e100h[i], e100l[i], s300[i], e20h[i - 1], e20l[i - 1])
    if not all(v is not None and math.isfinite(v) for v in vals):
        return None

    lb = CFG["pullback_lookback"]; sw = CFG["swing_lookback"]

    # ---- LONG: نظام صاعد (فوق SMA300) + سحابة EMA100 بالكامل فوق SMA300 ----
    if c[i] > s300[i] and e100l[i] > s300[i]:
        touched = any(l[k] <= e20h[k] for k in range(max(0, i - lb), i))
        reclaim = c[i - 1] <= e20h[i - 1] and c[i] > e20h[i]
        if touched and reclaim:
            dist = (c[i] - e20h[i]) / e20h[i]
            if 0 <= dist <= CFG["max_ema_dist"]:
                lo_i = min(range(max(0, i - sw), i + 1), key=lambda k: l[k])
                hi_i = max(range(lo_i, i + 1), key=lambda k: h[k])
                leg = h[hi_i] - l[lo_i]
                if leg > 0 and hi_i > lo_i:
                    pull_low = min(l[k] for k in range(hi_i, i + 1))
                    stop = min(pull_low, e100l[i]) * (1 - CFG["stop_buf_pct"])
                    entry = c[i]
                    if entry > stop:
                        return dict(sym=sym, dir="long", entry=entry, stop=stop,
                                    tp1=entry + CFG["fib_ext1"] * leg,
                                    tp2=entry + CFG["fib_ext2"] * leg,
                                    leg=leg, idx=i)

    # ---- SHORT: نظام هابط (تحت SMA300) + سحابة EMA100 بالكامل تحت SMA300 ----
    if c[i] < s300[i] and e100h[i] < s300[i]:
        touched = any(h[k] >= e20l[k] for k in range(max(0, i - lb), i))
        reclaim = c[i - 1] >= e20l[i - 1] and c[i] < e20l[i]
        if touched and reclaim:
            dist = (e20l[i] - c[i]) / e20l[i]
            if 0 <= dist <= CFG["max_ema_dist"]:
                hi_i = max(range(max(0, i - sw), i + 1), key=lambda k: h[k])
                lo_i = min(range(hi_i, i + 1), key=lambda k: l[k])
                leg = h[hi_i] - l[lo_i]
                if leg > 0 and lo_i > hi_i:
                    pull_high = max(h[k] for k in range(lo_i, i + 1))
                    stop = max(pull_high, e100h[i]) * (1 + CFG["stop_buf_pct"])
                    entry = c[i]
                    if stop > entry:
                        return dict(sym=sym, dir="short", entry=entry, stop=stop,
                                    tp1=entry - CFG["fib_ext1"] * leg,
                                    tp2=entry - CFG["fib_ext2"] * leg,
                                    leg=leg, idx=i)
    return None


# ----------------------- محاكاة الخروج: إدارة 50/50 + كسر هيكلي لسحابة EMA100 -----------------------
def _sim_5050_long(entry, stop, tp1, tp2, h, l, c, e100l, tch, hold):
    R = entry - stop
    if R <= 0:
        return None
    end = min(len(c), tch + 1 + hold)
    sl = stop; took1 = False; realized = 0.0
    for i in range(tch + 1, end):
        if math.isfinite(e100l[i]) and c[i] < e100l[i]:          # انكسار هيكلي لسحابة EMA100
            frac = 1.0 - (0.5 if took1 else 0.0)
            return realized + frac * ((c[i] - entry) / R)
        if l[i] <= sl:
            frac = 1.0 - (0.5 if took1 else 0.0)
            return realized + frac * ((sl - entry) / R)
        if not took1 and h[i] >= tp1:
            took1 = True; realized += 0.5 * ((tp1 - entry) / R); sl = entry
        if took1 and h[i] >= tp2:
            realized += 0.5 * ((tp2 - entry) / R)
            return realized
    frac = 1.0 - (0.5 if took1 else 0.0)
    return realized + frac * ((c[end - 1] - entry) / R)

def _sim_5050_short(entry, stop, tp1, tp2, h, l, c, e100h, tch, hold):
    R = stop - entry
    if R <= 0:
        return None
    end = min(len(c), tch + 1 + hold)
    sl = stop; took1 = False; realized = 0.0
    for i in range(tch + 1, end):
        if math.isfinite(e100h[i]) and c[i] > e100h[i]:          # انكسار هيكلي لسحابة EMA100
            frac = 1.0 - (0.5 if took1 else 0.0)
            return realized + frac * ((entry - c[i]) / R)
        if h[i] >= sl:
            frac = 1.0 - (0.5 if took1 else 0.0)
            return realized + frac * ((entry - sl) / R)
        if not took1 and l[i] <= tp1:
            took1 = True; realized += 0.5 * ((entry - tp1) / R); sl = entry
        if took1 and l[i] <= tp2:
            realized += 0.5 * ((entry - tp2) / R)
            return realized
    frac = 1.0 - (0.5 if took1 else 0.0)
    return realized + frac * ((entry - c[end - 1]) / R)

def _stats(rs):
    if not rs:
        return "لا صفقات"
    n = len(rs); wins = [x for x in rs if x > 0]; losses = [x for x in rs if x <= 0]
    wr = len(wins) / n * 100
    gp = sum(wins); gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else float("inf")
    exp = sum(rs) / n
    return f"صفقات={n} · فوز={wr:.1f}% · توقّع={exp:+.3f}R · PF={pf:.2f} · مجموع={sum(rs):+.1f}R"


# ----------------------- Watchlist -----------------------
def parse_watchlist_crypto(path):
    try:
        raw = open(path, encoding="utf-8").read()
    except Exception:
        return []
    toks = []
    for line in raw.splitlines():
        if "\t" in line:
            line = line.split("\t", 1)[1]
        toks += [x.strip() for x in line.split(",") if x.strip()]
    seen, uniq = set(), []
    for tok in toks:
        if tok.startswith("#") or ":" not in tok:
            continue
        exch, sym = tok.split(":", 1); sym = sym.strip().upper()
        if exch.upper() in {"BINANCE", "BYBIT", "MEXC", "BINANCEUS"} and sym.endswith("USDT"):
            if sym not in seen:
                seen.add(sym); uniq.append(sym)
    return uniq


# ----------------------- باك-تست حقيقي (متعدد الفريمات) -----------------------
def backtest_frame(tf, basket):
    hold = CFG["bt_hold"]
    need = CFG["sma300_len"] + CFG["swing_lookback"] + 5
    rs_long, rs_short = [], []
    n_sig_long = n_sig_short = 0
    n_symbols = 0
    for s in basket:
        try:
            d = fetch_klines(s, tf, CFG["pages"])
            if not d or len(d["c"]) < need + hold + 10:
                continue
            n_symbols += 1
            h, l, c = d["h"], d["l"], d["c"]
            e100l_full = ema(l, CFG["ema100_len"])
            e100h_full = ema(h, CFG["ema100_len"])
            N = len(c)
            step = 3
            for cut in range(need, N - hold, step):
                sub = {k: d[k][:cut + 1] for k in ("t", "o", "h", "l", "c", "v")}
                sig = find_setup(s, sub)
                if not sig:
                    continue
                if sig["dir"] == "long":
                    r = _sim_5050_long(sig["entry"], sig["stop"], sig["tp1"], sig["tp2"],
                                        h, l, c, e100l_full, cut, hold)
                    if r is not None:
                        rs_long.append(r); n_sig_long += 1
                else:
                    r = _sim_5050_short(sig["entry"], sig["stop"], sig["tp1"], sig["tp2"],
                                         h, l, c, e100h_full, cut, hold)
                    if r is not None:
                        rs_short.append(r); n_sig_short += 1
        except Exception as ex:
            print("bt skip", s, tf, ex, flush=True)
        time.sleep(0.03)

    print(f"\n════ فريم {tf} ════", flush=True)
    print(f"رموز مُحلَّلة={n_symbols}", flush=True)
    print(f"  LONG  : إشارات={n_sig_long} · {_stats(rs_long)}", flush=True)
    print(f"  SHORT : إشارات={n_sig_short} · {_stats(rs_short)}", flush=True)
    print(f"  الكل  : {_stats(rs_long + rs_short)}", flush=True)
    return {"long": rs_long, "short": rs_short}

def backtest(basket=None):
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:CFG["bt_symbols"]]
    frames = [x.strip() for x in CFG["bt_frames"].split(",") if x.strip()]
    print(f"باك-تست بوت سحابة المتوسطات (EMA20H/L + EMA100H/L + SMA300) | "
          f"فريمات={frames} | {len(basket)} رمز | hold={CFG['bt_hold']}", flush=True)
    all_res = {}
    for tf in frames:
        all_res[tf] = backtest_frame(tf, basket)
    return all_res


# ----------------------- الحالة (منع تكرار الإشارات في scan) -----------------------
def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {"sent": []}

def save_state(state):
    state["sent"] = state.get("sent", [])[-800:]
    try:
        json.dump(state, open(STATE_PATH, "w"))
    except Exception as ex:
        print("state save error", ex)


# ----------------------- تيليجرام -----------------------
def _fmt(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"

def format_message(signals):
    now = dt.datetime.now().strftime("%H:%M:%S")
    tf = CFG["entry_tf"]
    sep = "\n➖➖➖➖➖➖➖➖➖\n"
    blocks = []
    for i, s in enumerate(signals[:CFG["top_n"]], 1):
        arrow = "🟢 شراء" if s["dir"] == "long" else "🔴 بيع"
        blocks.append(
            f"{i}️⃣ <b>{s['sym']}</b> — {arrow}\n"
            f"دخول: {_fmt(s['entry'])}\n"
            f"وقف: {_fmt(s['stop'])}\n"
            f"هدف1: {_fmt(s['tp1'])}  ·  هدف2: {_fmt(s['tp2'])}"
        )
    header = f"☁️ <b>سحابة المتوسطات — {DASH_LABEL}</b>\nفريم {tf} · {now}"
    return header + sep + sep.join(blocks)

def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("تيليجرام غير مضبوط، تخطّي الإرسال"); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                       data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as ex:
        print("telegram error", ex)


# ----------------------- scan حي -----------------------
def scan(basket=None, send=True):
    basket = basket or parse_watchlist_crypto(WATCHLIST)
    tf = CFG["entry_tf"]
    state = load_state()
    sent = set(state.get("sent", []))
    signals = []
    for s in basket:
        try:
            d = fetch_klines(s, tf, CFG["pages"])
            if not d or len(d["c"]) < CFG["sma300_len"] + CFG["swing_lookback"] + 5:
                continue
            sig = find_setup(s, d)
            if not sig:
                continue
            key = f"{s}:{tf}:{sig['dir']}:{sig['idx']}"
            if key in sent:
                continue
            signals.append(sig); sent.add(key)
        except Exception as ex:
            print("scan skip", s, ex)
        time.sleep(0.03)

    if signals and send:
        send_telegram(format_message(signals))
    state["sent"] = list(sent)
    save_state(state)
    print(f"scan تم: {len(signals)} إشارة جديدة (فريم {tf})")
    return signals


# ----------------------- main -----------------------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "backtest":
        backtest()
    else:
        scan()

if __name__ == "__main__":
    main()
