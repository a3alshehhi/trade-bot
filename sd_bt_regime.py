#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sd_bt_regime.py — باك-تست فلتر النظام (BTC + استحواذ USDT) + رتّب-ثم-اختر لبوت الفيواب.

يجيب على سؤالين لبو محمد:
  1) هل بوابة النظام (BTC صاعد + USDT.D هابط، بنية قمم/قيعان، أغلبية ≥3/4 فريمات) تحسّن الأداء؟
  2) هل «رتّب-ثم-اختر» (أفضل N بجودة الإشارة) أفضل من «امسح-ثم-املأ» (الترتيب الأبجدي)؟

يقارن خمسة أنماط بنفس الإشارات الخام:
  BASE_exp   : توقّع كل الإشارات (الحافة الخام، بلا قيود خانات)
  BASE_fill  : محفظة N خانات، ملء بالترتيب الأبجدي (يحاكي الحيّ الحالي)
  REGIME_exp : توقّع الإشارات التي يمرّرها فلتر النظام فقط
  RANK       : محفظة N خانات، اختيار الأعلى جودةً (بلا نظام)
  REGIME+RANK: محفظة N خانات، نظام + اختيار الأعلى جودةً (المقترح الكامل)

المخرجات بوحدات R وأيضاً بالدولار عند مخاطرة ثابتة/صفقة (لإظهار إصلاح مشكلة التحجيم).
البيانات: 15m لكل عملة من binance.vision عبر sd_bot.fetch_klines ؛ regime.json من fetch_regime.py.
"""
import os, json, math, bisect
from concurrent.futures import ThreadPoolExecutor

from sd_bot import (fetch_klines, parse_watchlist_crypto, WATCHLIST, CFG,
                    ultimate_rsi, rsi, macd, vwap_weekly)

ENTRY_TF   = os.environ.get("BT_TF", "15m")
PAGES      = int(os.environ.get("BT_PAGES", "6"))        # صفحات الجلب (6×1000 ≈ 62 يوم على 15m)
MAX_SLOTS  = int(os.environ.get("BT_SLOTS", "10"))       # خانات المحفظة (max concurrent)
WAIT_BARS  = int(os.environ.get("BT_WAIT", "12"))        # أقصى شموع لملء الدخول (wait_entry)
HOLD_BARS  = int(os.environ.get("BT_HOLD", "96"))        # أقصى شموع لإمساك الصفقة
FEE        = float(os.environ.get("BT_FEE", str(CFG["fee_rate"])))
RISK_USD   = float(os.environ.get("BT_RISK_USD", "10"))  # مخاطرة ثابتة/صفقة (لتحويل R→$)
MIN_MAJ    = int(os.environ.get("BT_MIN_MAJORITY", "3")) # أغلبية الفريمات المطلوبة
WORKERS    = int(os.environ.get("BT_WORKERS", "8"))


# ───────────────────────── كل إشارات الفيواب عبر التاريخ ─────────────────────────
def vwave_signals_all(d1):
    """نفس آلة حالات vwave_signal لكن تجمع كل الإشارات المكتملة عبر التاريخ (لا آخر شمعة فقط)."""
    h, l, c, v, t = d1["h"], d1["l"], d1["c"], d1["v"], d1["t"]
    n = len(c)
    if n < 200:
        return []
    rs = rsi(c, CFG["rsi_entry_len"]) if CFG["vw_rsi"] == "classic" else ultimate_rsi(c, CFG["rsi_entry_len"])
    _, _, hist = macd(c)
    vw = vwap_weekly(t, h, l, c, v)
    OS, OB = CFG["vw_os"], CFG["vw_ob"]
    phase = 0; os_hits = 0
    wave_low = wave_low_i = cross_i = None
    sigs = []
    for i in range(1, n):
        if not (math.isfinite(rs[i]) and math.isfinite(vw[i]) and math.isfinite(hist[i]) and math.isfinite(hist[i-1])):
            continue
        if phase == 0:
            if rs[i] <= OS:
                phase, os_hits = 1, 1; wave_low, wave_low_i, cross_i = l[i], i, None
        elif phase == 1:
            if rs[i] <= OS:
                os_hits += 1
                if c[i] < vw[i]: cross_i = None
            if cross_i is None:
                if l[i] < wave_low: wave_low, wave_low_i = l[i], i
                if c[i] > vw[i]: cross_i = i
            else:
                if l[i] < wave_low:
                    phase, os_hits = 0, 0; wave_low = wave_low_i = cross_i = None
                    if rs[i] <= OS:
                        phase, os_hits = 1, 1; wave_low, wave_low_i = l[i], i
                    continue
                if rs[i] >= OB and c[i] > vw[i]:
                    phase = 2
        else:
            if l[i] < wave_low:
                phase, os_hits = 0, 0; wave_low = wave_low_i = cross_i = None; continue
            if hist[i] < 0 <= hist[i-1]:
                wave_high = max(h[wave_low_i:i+1]); span = wave_high - wave_low
                if span > 0:
                    levels = [wave_high - fb*span for fb in CFG["dca_fibs"]]
                    entry = levels[0]; stop = wave_low
                    tp1 = wave_high; tp2 = wave_low + 1.272*span
                    if entry > stop and tp1 > entry:
                        sigs.append(dict(i=i, ts=t[i], entry=entry, stop=stop,
                                         tp1=tp1, tp2=tp2, os_hits=os_hits,
                                         quality=(tp1-entry)/(entry-stop)))
                phase, os_hits = 0, 0; wave_low = wave_low_i = cross_i = None
    return sigs


def simulate(d1, sig):
    """يملأ الدخول عند levels[0] إذا هبط السعر إليه خلال WAIT_BARS، ثم إدارة 50/50 مع الرسوم.
    يعيد (R, fill_ts, exit_ts) أو None إن لم يُملأ. (نموذج ملء واحد موحّد لكل الأنماط.)"""
    h, l, c, t = d1["h"], d1["l"], d1["c"], d1["t"]
    n = len(c); i0 = sig["i"]
    entry, stop, tp1, tp2 = sig["entry"], sig["stop"], sig["tp1"], sig["tp2"]
    fill = None
    for i in range(i0+1, min(n, i0+1+WAIT_BARS)):
        if l[i] <= entry:
            fill = i; break
    if fill is None:
        return None
    R = entry - stop
    if R <= 0: return None
    r1, r2 = (tp1-entry)/R, (tp2-entry)/R
    def cst(px): return FEE*px/R
    end = min(n, fill+HOLD_BARS)
    sl = stop; half = False; realized = 0.0; res = None
    for i in range(fill, end):
        if l[i] <= sl:
            fr = 0.5 if half else 1.0
            res = realized + fr*((sl-entry)/R) - fr*cst(sl)
            if not half: res -= cst(entry)
            return res, t[fill], t[i]
        if not half and h[i] >= tp1:
            half = True; realized = 0.5*r1 - cst(entry) - 0.5*cst(tp1); sl = entry  # تعادل
        if half and h[i] >= tp2:
            res = realized + 0.5*r2 - 0.5*cst(tp2)
            return res, t[fill], t[i]
    X = c[end-1]; fr = 0.5 if half else 1.0
    res = realized + fr*((X-entry)/R) - fr*cst(X)
    if not half: res -= cst(entry)
    return res, t[fill], t[end-1]


# ───────────────────────── فلتر النظام ─────────────────────────
class Regime:
    def __init__(self, path="regime.json"):
        self.ok = False
        try:
            self.d = json.load(open(path)); self.ok = True
            self.tfs = self.d["meta"]["tfs"]
        except Exception as e:
            print(f"[regime] تعذّر تحميل regime.json ({e}) — سيُعطَّل فلتر النظام", flush=True)

    def _label(self, tf, arr_t_key, arr_key, ts):
        blk = self.d[tf]; ts_arr = blk[arr_t_key]
        j = bisect.bisect_right(ts_arr, ts) - 1
        return blk[arr_key][j] if j >= 0 else 0

    def favorable_count(self, ts):
        """عدد الفريمات المؤاتية: BTC=+1 و USDT.D=-1."""
        cnt = 0
        for tf in self.tfs:
            btc = self._label(tf, "t", "btc", ts)
            usd = self._label(tf, "usdtd_t", "usdtd", ts)
            if btc == 1 and usd == -1:
                cnt += 1
        return cnt

    def passes(self, ts):
        return (not self.ok) or (self.favorable_count(ts) >= MIN_MAJ)


# ───────────────────────── محاكاة المحفظة (خانات محدودة) ─────────────────────────
def portfolio(trades, rank_key, regime=None):
    """trades: قائمة dict فيها fill_ts, exit_ts, R, quality, sym.
    نجمّع حسب دورة الملء (fill_ts) ونملأ الخانات الحرّة؛ rank_key يحدّد ترتيب الأفضلية داخل الدورة.
    regime: إن مُرّر، تُقبل الصفقات المؤاتية فقط."""
    buckets = {}
    for tr in trades:
        if regime is not None and not regime.passes(tr["fill_ts"]):
            continue
        buckets.setdefault(tr["fill_ts"], []).append(tr)
    open_exits = []   # exit_ts للصفقات المفتوحة
    taken = []
    for ft in sorted(buckets):
        open_exits = [x for x in open_exits if x > ft]   # حرّر الخانات المنتهية
        free = MAX_SLOTS - len(open_exits)
        if free <= 0:
            continue
        cands = sorted(buckets[ft], key=rank_key)
        for tr in cands[:free]:
            taken.append(tr); open_exits.append(tr["exit_ts"])
    return taken


def stats(rs):
    if not rs: return dict(n=0)
    n = len(rs); wins = [x for x in rs if x > 0]; loss = [x for x in rs if x <= 0]
    gp = sum(wins); gl = -sum(loss)
    return dict(n=n, win=100*len(wins)/n, exp=sum(rs)/n,
                pf=(gp/gl) if gl > 0 else float("inf"),
                sumR=sum(rs), net=sum(rs)*RISK_USD)


def fmt(name, s):
    if s.get("n", 0) == 0:
        return f"  {name:12s}: لا صفقات"
    return (f"  {name:12s}: n={s['n']:4d} · فوز={s['win']:5.1f}% · توقّع={s['exp']:+.3f}R · "
            f"PF={s['pf']:.2f} · مجموع={s['sumR']:+7.1f}R · صافي(${RISK_USD:.0f}/صفقة)={s['net']:+8.1f}$")


def main():
    basket = parse_watchlist_crypto(WATCHLIST)
    print(f"باك-تست النظام | {len(basket)} عملة | {ENTRY_TF} | خانات={MAX_SLOTS} | "
          f"أغلبية≥{MIN_MAJ}/4 | مخاطرة={RISK_USD}$/صفقة", flush=True)
    reg = Regime()

    def work(s):
        try:
            d1 = fetch_klines(s, ENTRY_TF, PAGES)
            if not d1 or len(d1["c"]) < 300:
                return []
            out = []
            for sig in vwave_signals_all(d1):
                sim = simulate(d1, sig)
                if sim is None:
                    continue
                R, fill_ts, exit_ts = sim
                out.append(dict(sym=s, ts=sig["ts"], fill_ts=fill_ts, exit_ts=exit_ts,
                                R=R, quality=sig["quality"]))
            return out
        except Exception as ex:
            print("skip", s, ex, flush=True); return []

    all_trades = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(work, basket):
            all_trades.extend(res)
    all_trades.sort(key=lambda x: (x["fill_ts"], x["sym"]))
    print(f"إجمالي الإشارات المملوءة: {len(all_trades)}", flush=True)
    if not all_trades:
        print("لا إشارات — تحقّق من الجلب"); return

    # الأنماط
    base_exp = [t["R"] for t in all_trades]
    regime_exp = [t["R"] for t in all_trades if reg.passes(t["fill_ts"])]

    k_scan = lambda tr: tr["sym"]                 # ترتيب أبجدي (يحاكي امسح-ثم-املأ)
    k_rank = lambda tr: -tr["quality"]            # الأعلى جودةً أولاً

    base_fill  = [t["R"] for t in portfolio(all_trades, k_scan)]
    rank_only  = [t["R"] for t in portfolio(all_trades, k_rank)]
    regime_rank = [t["R"] for t in portfolio(all_trades, k_rank, regime=reg)]

    print("\n── النتائج ─────────────────────────────────────────────")
    print(fmt("BASE_exp",    stats(base_exp)))
    print(fmt("BASE_fill",   stats(base_fill)))
    print(fmt("REGIME_exp",  stats(regime_exp)))
    print(fmt("RANK",        stats(rank_only)))
    print(fmt("REGIME+RANK", stats(regime_rank)))
    print("────────────────────────────────────────────────────────")
    print("BASE_fill = الوضع الحالي (أبجدي) · REGIME+RANK = المقترح الكامل. قارن الصافي بالدولار.")

    # حفظ ملخص للقراءة الآلية
    summary = {k: stats(v) for k, v in dict(
        BASE_exp=base_exp, BASE_fill=base_fill, REGIME_exp=regime_exp,
        RANK=rank_only, REGIME_RANK=regime_rank).items()}
    for s in summary.values():
        s["pf"] = None if s.get("pf") == float("inf") else s.get("pf")
    json.dump({"summary": summary, "n_signals": len(all_trades),
               "slots": MAX_SLOTS, "min_majority": MIN_MAJ, "risk_usd": RISK_USD},
              open("regime_bt_result.json", "w"), ensure_ascii=False, indent=2)
    print("wrote regime_bt_result.json", flush=True)


if __name__ == "__main__":
    main()
