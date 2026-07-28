# -*- coding: utf-8 -*-
"""اختبار وحدة للوقف المتدرّج (staged_trail) — النسخة المعدّلة 2026-07-26، بيانات صناعية."""
import staged_trail as st


def _mk():
    """هبوط تصحيحي (قاع ~98) ثم تعافٍ يصنع قمماً صاعدة حتى الهدف 112 (دخول ~100)."""
    closes, highs, lows = [], [], []
    p = 112.0
    for _ in range(40):
        p -= 0.35
        closes.append(p); highs.append(p + 0.3); lows.append(p - 0.3)
    ups = [98.5, 99.4, 100.5, 101.6, 102.6, 103.4,
           102.9, 102.6, 103.0,
           104.2, 105.5, 106.8, 108.2, 109.6, 111.0, 112.5]
    for p in ups:
        closes.append(p); highs.append(p + 0.4); lows.append(p - 0.3)
    return highs, lows, closes


def main():
    highs, lows, closes = _mk()
    entry, tp1 = 100.0, 112.0

    # (كامل) بلوغ الهدف → المرحلة 3: الوقف فوق المتوسط +0.5%
    t, s, note = st.compute_staged_stop(highs, lows, closes, entry, tp1)
    print(f"[كامل] stage={s} target={t:.4f}  {note}")
    assert s == 3, f"متوقّع المرحلة 3، وصلنا {s}"
    assert abs(t - entry * 1.005) < 1e-6, "المرحلة 3 = المتوسط +0.5%"

    # (قبل الهدف) بعد قمة أعلى من قمة → المرحلة 2: الوقف تحت أقرب قاع −0.5% (أقل من الدخول)
    cut = len(closes) - 2
    t2, s2, n2 = st.compute_staged_stop(highs[:cut], lows[:cut], closes[:cut], entry, tp1)
    print(f"[قبل الهدف] stage={s2} target={t2:.4f}  {n2}")
    assert s2 == 2, f"متوقّع المرحلة 2 قبل الهدف، وصلنا {s2}"
    assert t2 < entry, "المرحلة 2 يجب أن تكون تحت الدخول (تحت أقرب قاع)"
    assert t2 < 98.5, "تحت قاع التصحيح −0.5%"

    # الثابت الأهم: انتهاء التصحيح وحده لا يحرّك الوقف (أي مرحلة==1 ⇐ target None)
    stage1_seen = False
    for c in range(3, len(closes) + 1):
        tc, sc, _ = st.compute_staged_stop(highs[:c], lows[:c], closes[:c], entry, tp1)
        if sc == 1:
            stage1_seen = True
            assert tc is None, f"المرحلة 1 يجب ألا تحرّك الوقف (target={tc} عند القطع {c})"
    print(f"[بوابة] لوحظت المرحلة 1 (انتهاء تصحيح بلا تحريك): {stage1_seen}")
    assert stage1_seen, "يجب أن تمرّ لحظة انتهاء تصحيح دون تحريك الوقف"

    # أول تحريك فعلي للوقف = المرحلة 2 (قمة أعلى)، لا عند انتهاء التصحيح
    first_move = next(
        (st.compute_staged_stop(highs[:c], lows[:c], closes[:c], entry, tp1) + (c,)
         for c in range(3, len(closes) + 1)
         if st.compute_staged_stop(highs[:c], lows[:c], closes[:c], entry, tp1)[0] is not None),
        None)
    print(f"[أول تحريك] عند القطع {first_move[3]} = المرحلة {first_move[1]} target={first_move[0]:.4f}")
    assert first_move[1] == 2, "أول تحريك للوقف يجب أن يكون عند قمة أعلى (المرحلة 2)"

    # أثناء التصحيح البحت (قبل البوابة) → لا شيء
    t0, s0, _ = st.compute_staged_stop(highs[:35], lows[:35], closes[:35], entry, tp1)
    print(f"[هبوط فقط] stage={s0} target={t0}")
    assert s0 == 0 and t0 is None, "أثناء التصحيح لا يتحرّك الوقف إطلاقاً"

    # بوابة الوسم
    assert st.applies_to("العرض/الطلب")
    assert st.applies_to("الفيواب الأسبوعي · BTC")
    assert not st.applies_to("عرض/طلب+دايفرجنس")

    _test_hard_exit()
    print("\n✅ كل الاختبارات نجحت — البوابة + قمة أعلى→دعم + هدف→+0.5% + الخروج الفوري صحيحة.")


def _mk_rollover():
    """تصحيح هابط، ثم تعافٍ يصنع بوابة خضراء (لم يصل الهدف 112)، ثم تراجع يقلب
    الهيستوجرام أحمر وآخر إغلاق ~99. القمة القصوى ~106 (أقل من tp1)."""
    closes, highs, lows = [], [], []
    p = 112.0
    for _ in range(40):                    # التصحيح الهابط
        p -= 0.35
        closes.append(p); highs.append(p + 0.3); lows.append(p - 0.3)
    ups = [98.6, 99.6, 100.8, 102.0, 103.2, 104.4, 105.4, 106.0]   # تعافٍ (بوابة)
    for p in ups:
        closes.append(p); highs.append(p + 0.4); lows.append(p - 0.3)
    downs = [105.2, 104.0, 102.6, 101.2, 100.2, 99.4, 99.0]        # تراجع → هيستوجرام أحمر
    for p in downs:
        closes.append(p); highs.append(p + 0.3); lows.append(p - 0.4)
    return highs, lows, closes


def _test_hard_exit():
    highs, lows, closes = _mk_rollover()
    entry, tp1 = 100.0, 112.0
    hist = st.macd_hist(closes)
    assert hist[-1] < 0, "التهيئة: يجب أن يكون الهيستوجرام أحمر في آخر شمعة"
    assert max(highs) < tp1, "التهيئة: يجب ألا يبلغ السعر الهدف الأول"

    # الحالة الأساسية: تحت الفيواب (wvwap=103 > آخر إغلاق ~99) + أحمر + قبل الهدف → خروج
    ex, note = st.hard_exit(highs, lows, closes, entry, tp1, wvwap=103.0)
    print(f"[خروج فوري] should_exit={ex}  {note}")
    assert ex, "متوقّع خروج فوري (تحت الفيواب + أحمر + قبل الهدف)"

    # فوق الفيواب → لا خروج
    ex2, _ = st.hard_exit(highs, lows, closes, entry, tp1, wvwap=90.0)
    assert not ex2, "فوق الفيواب: لا خروج فوري"

    # الهدف تحقّق → لا خروج فوري (تُدار 50/50)
    ex3, _ = st.hard_exit(highs, lows, closes, entry, tp1=95.0, wvwap=103.0)
    assert not ex3, "بعد بلوغ الهدف: لا خروج فوري"

    # تصحيح هابط بحت (بلا بوابة) + الاشتراط مفعّل → لا خروج
    dh, dl, dc = highs[:40], lows[:40], closes[:40]
    ex4, _ = st.hard_exit(dh, dl, dc, entry, tp1, wvwap=200.0)
    assert not ex4, "بلا بوابة (تصحيح بحت) والاشتراط مفعّل: لا خروج فوري"

    # الميزة معطّلة عبر العلم → لا خروج مهما تحقّقت الشروط
    _saved = st.HARD_EXIT
    st.HARD_EXIT = False
    ex5, _ = st.hard_exit(highs, lows, closes, entry, tp1, wvwap=103.0)
    st.HARD_EXIT = _saved
    assert not ex5, "STAGED_HARD_EXIT=0 يجب أن يُلغي الميزة"
    print("[خروج فوري] كل الحالات (فوق/تحت/هدف/بلا بوابة/معطّل) صحيحة")


if __name__ == "__main__":
    main()
