#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تنظيف مرة-واحدة: يُبقي فقط صفقات/إشارات "العرض/الطلب" بتاريخ >= 2026-07-06،
ويحذف كل ما عداه (البوتات المتوقفة: RSI/trendwave/صيد الارتفاعات، وأي عرض/طلب أقدم من أمس).
يشمل: المفتوحة (positions) والمغلقة (ledger) والمتتبع (tracked_signals).
آمن: يُبقي message_id لكل صفقة مفتوحة مُبقاة (لأننا نُبقي القيد كاملاً).
"""
import json, os

LABEL = "العرض/الطلب"
CUT = "2026-07-06"

def load(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def save(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def is_sd(e):
    return isinstance(e, dict) and e.get("label") == LABEL

def date_ok(s):
    return isinstance(s, str) and s[:10] >= CUT

def clean_array(path, date_field):
    data = load(path)
    if not isinstance(data, list):
        print(f"skip {path}: not a list"); return
    kept = [e for e in data if is_sd(e) and date_ok(e.get(date_field, ""))]
    print(f"{path}: {len(data)} -> {len(kept)} (removed {len(data)-len(kept)})")
    save(path, kept)

def clean_dict(path, date_field, alt_key_date=False):
    data = load(path)
    if not isinstance(data, dict):
        print(f"skip {path}: not a dict"); return
    kept = {}
    for k, e in data.items():
        d = ""
        if isinstance(e, dict):
            d = e.get(date_field, "") or ""
        if not d and alt_key_date:
            parts = k.split("|")
            if len(parts) >= 3:
                d = parts[2]
        if is_sd(e) and date_ok(d):
            kept[k] = e
    print(f"{path}: {len(data)} -> {len(kept)} (removed {len(data)-len(kept)})")
    save(path, kept)

if __name__ == "__main__":
    clean_array("sd_ledger.json", "closed_ts")
    clean_array("sd_ledger_binance.json", "closed_ts")
    clean_dict("sd_positions.json", "opened_ts")
    clean_dict("sd_positions_binance.json", "opened_ts")
    clean_dict("tracked_signals.json", "bar_ts", alt_key_date=True)
    print("done.")
