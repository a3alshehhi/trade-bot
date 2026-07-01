#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت العرض/الطلب بالذكاء الاصطناعي (v3) — إشارات فقط (لا تنفيذ صفقات)
=====================================================================
يرمّز مدرسة العرض/الطلب (مناطق طلب، كتل أوامر، FVG، اصطياد سيولة، كسر بنية، حجم)
ثم نموذج تعلّم آلي (Logistic Regression) يرشّح الإعدادات، ويرسل أفضلها كإشارة تيليجرام.

إعداد E (محافظ): دخول 1h، سياق 4h، فلتر «السعر فوق EMA200» + «4h غير هابط»، عتبة ML 0.60،
مخاطرة مقترحة 0.5% لكل صفقة وحد 5 مراكز. إدارة خروج: جني 50% عند +1R + تعادل + وقف متحرّك.

الأوضاع:
  python sd_bot.py train   # يبني العيّنات من التاريخ ويدرّب النموذج (sd_model.joblib)
  python sd_bot.py scan    # يفحص آخر شمعة مغلقة ويرسل الإشارات لتيليجرام
  python sd_bot.py both     # تدريب ثم فحص (الافتراضي)

تنبيه: أداة تحليل تعليمية. لا تنفّذ صفقات ولا تحرّك أموالاً. التداول مخاطرة، وليست نصيحة مالية.
"""
import os, sys, time, math, json, datetime as dt
import requests

# ----------------------- إعدادات -----------------------
CFG = dict(
    pivL=3, pivR=3, impK=1.0, base_max_body=0.6, base_max=3,
    atr_len=50, vol_len=200, ema_len=200, react_k=48,
    distal_buf_atr=0.1, ml_threshold=0.60,
    risk_pct=0.005, max_concurrent=5,           # إعداد E
    entry_tf="1h", htf="4h", pages_1h=4, pages_4h=2,
    top_n=8,
)
# ── تجاوز فريم الدخول/السياق عبر البيئة (لتشغيل البوت على كل الفريمات: 15m/1h/4h) ──
# مثال: SD_ENTRY_TF=15m SD_HTF=1h  |  SD_ENTRY_TF=4h SD_HTF=1d
CFG["entry_tf"] = os.environ.get("SD_ENTRY_TF", CFG["entry_tf"])
CFG["htf"]      = os.environ.get("SD_HTF", CFG["htf"])
BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
# ملفات النموذج/الحالة قابلة للتخصيص لكل فريم (لتفادي التضارب بين الفريمات)
MODEL_PATH = os.environ.get("SD_MODEL", "sd_model.joblib")
STATE_PATH = os.environ.get("SD_STATE", "sd_state.json")
WATCHLIST = "watchlist.txt"
MODEL_MAX_AGE_H = 24                  # يعيد التدريب إذا تجاوز عمر النموذج هذا
ML_KEYS = ["strength", "heightATR", "baseVolZ", "touchVolZ", "bos", "fvg", "sweep",
           "htf", "emaRel", "barsToTouch", "hour"]
# أسماء الأسرار نفسها التي يستخدمها workflow الحالي (sd_bot.yml)
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", os.environ.get("TG_TOKEN", ""))
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", os.environ.get("TG_CHAT", ""))

# سجل المتتبّع المشترك: نكتب فيه إشاراتنا لتظهر وتُتابَع في اللوحة مثل بقية البوتات.
# (يتابعها trackmon في reversal.yml كل 15 دقيقة ويُصدّر paper_data.json للوحة)
TRACK_FILE = "tracked_signals.json"
DASH_LABEL = "العرض/الطلب"
_TF_MS = {"1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000,
          "1h": 3600000, "2h": 7200000, "4h": 14400000, "1d": 86400000}

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
                    data = r.json()
                    break
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
def atr(h, l, c, n):
    out = [float("nan")] * len(c); s = 0.0; tr = []
    for i in range(len(c)):
        t = (h[i] - l[i]) if i == 0 else max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        tr.append(t); s += t
        if i >= n: s -= tr[i-n]
        if i >= n-1: out[i] = s / n
    return out

def ema(arr, n):
    k = 2 / (n + 1); out = [float("nan")] * len(arr); prev = None
    for i, x in enumerate(arr):
        prev = x if i == 0 else x * k + prev * (1 - k)
        out[i] = prev
    return out

def vol_z(v, L):
    out = [float("nan")] * len(v)
    for i in range(L - 1, len(v)):
        win = v[i - L + 1:i + 1]
        m = sum(win) / L
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / L)
        out[i] = (v[i] - m) / sd if sd > 0 else 0.0
    return out

def pivots(h, l, L, R):
    piv = []
    for i in range(L, len(h) - R):
        hi = lo = True
        for k in range(1, L + 1):
            if h[i] < h[i-k]: hi = False
            if l[i] > l[i-k]: lo = False
        for k in range(1, R + 1):
            if h[i] < h[i+k]: hi = False
            if l[i] > l[i+k]: lo = False
        if hi: piv.append((i, h[i], "H"))
        if lo: piv.append((i, l[i], "L"))
    piv.sort(key=lambda x: x[0])
    return piv

def structure(h, l, c, L, R):
    piv = pivots(h, l, L, R); events = []
    ref_h = ref_l = None; bias = 0; pidx = 0
    for i in range(len(c)):
        while pidx < len(piv) and piv[pidx][0] + R <= i:
            p = piv[pidx]; pidx += 1
            if p[2] == "H": ref_h = p
            else: ref_l = p
        if ref_h and c[i] > ref_h[1]:
            events.append((i, "up")); bias = 1; ref_h = None
        elif ref_l and c[i] < ref_l[1]:
            events.append((i, "dn")); bias = -1; ref_l = None
    return piv, events

def demand_zones(o, h, l, c, v, a):
    zones = []
    for j in range(2, len(c)):
        A = a[j]
        if not (A and A > 0):
            continue
        body = c[j] - o[j]
        if not (c[j] > o[j] and body > CFG["impK"] * A):
            continue
        base = []; k = j - 1
        while k >= 0 and len(base) < CFG["base_max"] and abs(c[k] - o[k]) < CFG["base_max_body"] * A:
            base.append(k); k -= 1
        if not base:
            continue
        top = max(max(o[x], c[x], h[x]) for x in base)
        bot = min(l[x] for x in base)
        if not (top > bot):
            continue
        zones.append(dict(created=j, proximal=top, distal=bot, height=top - bot,
                          strength=round(body / A, 2)))
    return zones

def htf_bias_fn(d4):
    _, ev = structure(d4["h"], d4["l"], d4["c"], CFG["pivL"], CFG["pivR"])
    pts = [(d4["t"][i], 1 if k == "up" else -1) for i, k in ev]
    def f(ts):
        b = 0
        for t, u in pts:
            if t <= ts: b = u
            else: break
        return b
    return f

# ----------------------- ميزات الإعداد -----------------------
def setup_features(sym, d1, d4):
    o, h, l, c, v, t = d1["o"], d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
    a = atr(h, l, c, CFG["atr_len"]); vz = vol_z(v, CFG["vol_len"]); e200 = ema(c, CFG["ema_len"])
    _, ev = structure(h, l, c, CFG["pivL"], CFG["pivR"])
    bos_up = set(i for i, k in ev if k == "up")
    hb = htf_bias_fn(d4); zones = demand_zones(o, h, l, c, v, a)
    out = []
    for z in zones:
        j = z["created"]; tch = -1
        for i in range(j + 1, len(c)):
            if l[i] <= z["proximal"]:
                tch = i; break
        if tch < 0:
            continue
        R = z["height"]
        if not (R > 0):
            continue
        fvg = 1 if l[j] > h[j-2] else 0
        bos = 1 if (j in bos_up or (j+1) in bos_up or (j+2) in bos_up) else 0
        lo, hi = max(0, j - 30), max(1, j - 5)
        prior_low = min(l[lo:hi]) if hi > lo else l[j]
        sweep = 1 if z["distal"] < prior_low else 0
        ema_rel = (c[tch] - e200[tch]) / e200[tch] if e200[tch] else 0.0
        f = dict(strength=z["strength"], heightATR=round(R / (a[j] or R), 2),
                 baseVolZ=round(vz[j] or 0, 2), touchVolZ=round(vz[tch] or 0, 2),
                 bos=bos, fvg=fvg, sweep=sweep, htf=hb(t[tch]),
                 emaRel=round(ema_rel, 4), barsToTouch=tch - j,
                 hour=dt.datetime.utcfromtimestamp(t[tch] / 1000).hour)
        out.append(dict(sym=sym, created=j, touch=tch, ts=t[tch], f=f,
                        entry=z["proximal"],
                        stop=z["distal"] - CFG["distal_buf_atr"] * (a[tch] or R),
                        height=R))
    return out, h, l, c

# ----------------------- تسمية للتدريب -----------------------
def label_setup(s, h, l, c):
    entry, stop, tch = s["entry"], s["stop"], s["touch"]
    R = entry - stop
    if R <= 0:
        return None
    tgt = entry + s["height"]
    end = min(len(c), tch + CFG["react_k"])
    for i in range(tch, end):
        if l[i] <= stop: return 0
        if h[i] >= tgt: return 1
    return None

# ----------------------- قائمة الكريبتو -----------------------
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

# ----------------------- تدريب النموذج -----------------------
def train(basket=None):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    import joblib
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:40]
    X, Y = [], []
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], CFG["pages_1h"])
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 800:
                continue
            setups, h, l, c = setup_features(s, d1, d4)
            for st in setups:
                y = label_setup(st, h, l, c)
                if y is None:
                    continue
                feat = [st["f"][k] for k in ML_KEYS]
                # تجاهل العيّنات ذات الميزات غير المنتهية (NaN/inf من فترة إحماء ATR/الحجم/المتوسط)
                if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in feat):
                    continue
                X.append(feat); Y.append(y)
        except Exception as ex:
            print("train skip", s, ex)
        time.sleep(0.05)
    if len(X) < 200:
        print("not enough samples:", len(X)); return None
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=1000))
    model.fit(X, Y)
    joblib.dump(dict(model=model, keys=ML_KEYS, trained=len(X),
                     date=dt.date.today().isoformat()), MODEL_PATH)
    print(f"trained on {len(X)} setups, base_win={sum(Y)/len(Y):.3f} -> {MODEL_PATH}")
    return model

# ----------------------- الفحص الحيّ + الإشارات -----------------------
def load_model():
    import joblib
    return joblib.load(MODEL_PATH) if os.path.exists(MODEL_PATH) else None

def model_is_fresh():
    # نعتمد على التاريخ المخزّن داخل النموذج لا وقت الملف (git checkout يعيد ضبط الوقت)
    # فيُعاد التدريب مرة واحدة يومياً (أول تشغيل في يوم جديد).
    if not os.path.exists(MODEL_PATH):
        return False
    try:
        import joblib
        return joblib.load(MODEL_PATH).get("date") == dt.date.today().isoformat()
    except Exception:
        return False

def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {"sent": []}

def save_state(state):
    state["sent"] = state.get("sent", [])[-800:]   # نحتفظ بآخر 800 مفتاح فقط
    try:
        json.dump(state, open(STATE_PATH, "w"))
    except Exception as ex:
        print("state save error", ex)

def scan(basket=None):
    bundle = load_model()
    if not bundle:
        print("no model; run train first"); return []
    model = bundle["model"]
    basket = basket or parse_watchlist_crypto(WATCHLIST)[:60]
    state = load_state(); sent = set(state.get("sent", []))
    signals = []
    for s in basket:
        try:
            d1 = fetch_klines(s, CFG["entry_tf"], 2)
            d4 = fetch_klines(s, CFG["htf"], CFG["pages_4h"])
            if not d1 or not d4 or len(d1["c"]) < 300:
                continue
            setups, h, l, c = setup_features(s, d1, d4)
            last = len(c) - 1
            for st in setups:
                if st["touch"] != last:        # أول لمسة على الشمعة المغلقة الأخيرة فقط
                    continue
                f = st["f"]
                if f["emaRel"] <= 0:           # فلتر E: فوق EMA200
                    continue
                if f["htf"] < 0:               # فلتر E: 4h غير هابط
                    continue
                key = f"{s}:{st['ts']}"          # منع التكرار: نفس اللمسة لا تُرسل مرتين
                if key in sent:
                    continue
                feat = [f[k] for k in ML_KEYS]
                # تخطّي الإعدادات ذات الميزات غير المنتهية (NaN/inf) كي لا ينهار التنبّؤ
                if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in feat):
                    continue
                prob = model.predict_proba([feat])[0][1]
                if prob < CFG["ml_threshold"]:
                    continue
                entry, stop = st["entry"], st["stop"]
                signals.append(dict(key=key, sym=s, prob=round(float(prob), 3),
                    entry=round(entry, 8), stop=round(stop, 8),
                    tp1=round(entry + (entry - stop), 8), ts=st["ts"],
                    reasons=_reasons(f)))
        except Exception as ex:
            print("scan skip", s, ex)
        time.sleep(0.05)
    signals.sort(key=lambda x: x["prob"], reverse=True)
    signals = signals[:CFG["top_n"]]
    if signals:
        mid = send_telegram(format_message(signals))
        track_for_dashboard(signals, mid)        # تظهر في لوحة المتتبّع مثل بقية البوتات
        for sig in signals:
            state.setdefault("sent", []).append(sig["key"])
        save_state(state)
    else:
        print("no signals this scan")
    return signals

def _reasons(f):
    r = []
    if f["sweep"]: r.append("اصطياد سيولة")
    if f["bos"]: r.append("كسر بنية صاعد")
    if f["htf"] > 0: r.append("4h صاعد")
    if f["emaRel"] > 0: r.append("فوق متوسط 200")
    if f["baseVolZ"] >= 1: r.append("حجم قوي عند القاعدة")
    return r or ["منطقة طلب طازجة"]

def format_message(signals):
    lines = ["📊 <b>إشارات العرض/الطلب (v3) — شراء</b>",
             "<i>إعداد E المحافظ · مخاطرة مقترحة 0.5% · حد 5 مراكز</i>", ""]
    for s in signals:
        lines += [
            f"🟢 <b>{s['sym']}</b>  (ثقة {int(s['prob']*100)}%)",
            f"   دخول: {s['entry']}",
            f"   وقف: {s['stop']}",
            f"   هدف1: {s['tp1']}  ← جني 50% + تعادل + وقف متحرّك",
            f"   الأسباب: {'، '.join(s['reasons'])}",
            ""]
    lines.append("⚠️ إشارة تحليلية فقط — أنت تقرّر وتنفّذ. ليست نصيحة مالية.")
    return "\n".join(lines)

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("TG not configured; message:\n", text); return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=15)
        print(f"sent {text.count(chr(0x1F7E2))} signals to telegram")
        return (r.json().get("result") or {}).get("message_id")
    except Exception as ex:
        print("telegram error", ex)
        return None


def track_for_dashboard(signals, message_id, tf=None, path=TRACK_FILE):
    """يسجّل إشارات هذا الفحص في tracked_signals.json بنفس صيغة بقية البوتات،
    فتظهر وتُتابَع في لوحة المتتبّع (إدارة 50/50: هدف1 +1R جني 50%+تعادل، هدف2 +2R).
    لا يمسّ إشارات البوتات الأخرى — يُضيف فقط ويُنظّف إشاراته القديمة (>14 يوماً)."""
    tf = tf or CFG["entry_tf"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    # تنظيف إشارات هذا البوت القديمة فقط (دون المساس بإشارات البوتات الأخرى)
    cutoff = (dt.datetime.now() - dt.timedelta(days=14)).isoformat()
    data = {k: v for k, v in data.items()
            if not (isinstance(v, dict) and v.get("label") == DASH_LABEL
                    and v.get("created", "") < cutoff)}
    added = 0
    for s in signals:
        entry, stop, tp1 = s["entry"], s["stop"], s["tp1"]
        R = entry - stop
        if R <= 0:
            continue
        tp2 = round(entry + 2 * R, 8)
        bar_ts = dt.datetime.utcfromtimestamp(s["ts"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        key = f"{DASH_LABEL}|{s['sym']}|{bar_ts}"
        if key in data:
            continue
        data[key] = {
            "symbol": s["sym"], "label": DASH_LABEL, "timeframe": tf,
            "message_id": message_id,
            "entry": entry, "stop": stop, "init_stop": stop, "cur_stop": stop,
            "last_alert_stop": stop, "armed": False,
            "targets": [tp1, tp2], "tp_split": [50, 50],
            "is_trendwave": False, "mgmt": "5050", "breakeven_done": False,
            "bar_ts": bar_ts, "last_bar": bar_ts,
            "hits": [], "stopped": False, "hi_seen": entry, "lo_seen": entry,
            "created": dt.datetime.now().isoformat(timespec="seconds"),
        }
        added += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"tracked {added} signals to {path}")

# ----------------------- main -----------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode == "train":
        train()
    elif mode == "scan":
        scan()
    else:  # both: يدرّب فقط إذا غاب النموذج أو تجاوز عمره 24 ساعة، ثم يفحص
        if not model_is_fresh():
            print("model missing/stale -> training")
            train()
        else:
            print("model fresh -> skip training")
        scan()
