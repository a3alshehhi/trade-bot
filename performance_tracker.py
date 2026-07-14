#!/usr/bin/env python3
"""
متتبّع أداء البوتات — يقرأ من GitHub API، يحسب الإحصائيات، يرسل Telegram
"""
import os
import json
import requests
from datetime import datetime, timedelta
from collections import defaultdict
import sys

# GitHub API
GITHUB_API = "https://api.github.com"
REPO_OWNER = "a3alshehhi"
REPO_NAME = "trade-bot"
GH_TOKEN = os.environ.get("GH_TOKEN", "")

# Telegram
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# خريطة البوتات وملفات ledger الخاصة بها
BOTS_LEDGERS = {
    "العرض/الطلب (SD)": {
        "label": "sd",
        "ledger": "sd_ledger.json",
        "positions": "sd_positions.json",
        "emoji": "🟢",
    },
    "عرض/طلب+تشبّع": {
        "label": "os21",
        "ledger": "sd_ledger_os21.json",
        "positions": "sd_pos_os21.json",
        "emoji": "🟡",
    },
    "عرض/طلب+دايفرجنس": {
        "label": "div",
        "ledger": "sd_ledger_div.json",
        "positions": "sd_pos_div.json",
        "emoji": "🟠",
    },
    "تريند ويف 1h": {
        "label": "tw_1h",
        "ledger": "tw_ledger_1h.json",
        "positions": "tw_pos_1h.json",
        "emoji": "🔵",
    },
    "تريند ويف 4h (vwap_w)": {
        "label": "tw_vwapw",
        "ledger": "tw_ledger_vwapw.json",
        "positions": "tw_pos_vwapw.json",
        "emoji": "🟣",
    },
    "تريند ويف 4h (vwap_m)": {
        "label": "tw_vwapm",
        "ledger": "tw_ledger_vwapm.json",
        "positions": "tw_pos_vwapm.json",
        "emoji": "⚫",
    },
    "صياد (Hunter)": {
        "label": "hunter",
        "ledger": None,  # Hunter قد تكون بنية مختلفة
        "positions": None,
        "emoji": "🔴",
    },
}

def get_file_from_github(filepath):
    """جلب ملف من GitHub API"""
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{filepath}"
    headers = {"Authorization": f"token {GH_TOKEN}"} if GH_TOKEN else {}

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if "content" in data:
            import base64
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content)
    except Exception as e:
        print(f"Error fetching {filepath}: {e}")
    return None

def calculate_stats(trades):
    """حساب الإحصائيات من قائمة الصفقات"""
    if not trades or len(trades) == 0:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "profit_factor": 0.0,
            "avg_r": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
        }

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]

    total_wins = sum(t.get("pnl", 0) for t in wins)
    total_losses = abs(sum(t.get("pnl", 0) for t in losses))

    pnls = [t.get("pnl", 0) for t in trades]
    r_multiples = [t.get("r_multiple", 0) for t in trades if t.get("r_multiple")]

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(trades) if trades else 0.0,
        "profit_factor": total_wins / total_losses if total_losses > 0 else (float('inf') if total_wins > 0 else 0.0),
        "avg_r": sum(r_multiples) / len(r_multiples) if r_multiples else 0.0,
        "max_win": max(pnls) if pnls else 0.0,
        "max_loss": min(pnls) if pnls else 0.0,
    }

def filter_trades_by_period(trades, days=None):
    """تصفية الصفقات حسب الفترة الزمنية"""
    if not days or not trades:
        return trades

    cutoff = datetime.now() - timedelta(days=days)
    filtered = []

    for t in trades:
        try:
            created = t.get("created")
            if isinstance(created, str):
                # حاول صيغ مختلفة
                for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                    try:
                        dt = datetime.strptime(created[:19], fmt)
                        if dt >= cutoff:
                            filtered.append(t)
                        break
                    except ValueError:
                        continue
        except Exception:
            pass

    return filtered

def fetch_all_bots_data():
    """جلب بيانات جميع البوتات"""
    bots_data = {}

    for bot_name, bot_info in BOTS_LEDGERS.items():
        ledger_file = bot_info.get("ledger")
        if not ledger_file:
            continue

        trades = get_file_from_github(ledger_file) or {}
        if isinstance(trades, dict) and "trades" in trades:
            trades = trades["trades"]
        elif not isinstance(trades, list):
            trades = []

        bots_data[bot_name] = {
            "info": bot_info,
            "trades": trades,
        }

    return bots_data

def format_telegram_message(period_name, days=None, dashboard_url=None):
    """تشكيل رسالة Telegram بجداول منظمة"""
    bots_data = fetch_all_bots_data()

    # الرأس
    header = f"<b>📊 تقرير الأداء — {period_name}</b>\n"
    header += f"<i>{datetime.now().strftime('%A %d/%m/%Y %H:%M')}</i>\n\n"

    # رابط الداشبورد
    if dashboard_url:
        header += f"<a href='{dashboard_url}'>🔗 عرض التقرير التفاعلي الكامل</a>\n"
        header += "━" * 40 + "\n\n"

    # جدول البوتات
    table_rows = [
        "<code>بوت                    الصفقات   فوز   الربح       PF    R</code>",
        "━" * 60,
    ]

    total_stats = {
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
    }

    for bot_name, bot_data in sorted(bots_data.items()):
        trades = bot_data["trades"]
        if days:
            trades = filter_trades_by_period(trades, days)

        stats = calculate_stats(trades)
        emoji = bot_data["info"]["emoji"]

        if stats["total"] == 0:
            continue

        total_stats["total_trades"] += stats["total"]
        total_stats["total_wins"] += stats["wins"]
        total_stats["total_pnl"] += stats["total_pnl"]

        pf = f'{stats["profit_factor"]:.2f}' if stats["profit_factor"] != float('inf') else "∞"
        pnl_str = f'{stats["total_pnl"]:.0f}' if abs(stats["total_pnl"]) > 0 else "0"
        pnl_emoji = "🟢" if stats["total_pnl"] >= 0 else "🔴"

        # صف الجدول
        short_name = bot_name[:20].ljust(20)
        row = f"{emoji} {short_name} {str(stats['total']).rjust(4)}   {str(stats['wins']).rjust(3)}   {pnl_emoji}{pnl_str.rjust(7)}  {str(pf).rjust(4)}  {str(stats['avg_r']).rjust(5)}"
        table_rows.append(f"<code>{row}</code>")

    table_rows.append("━" * 60)

    # ملخص عام
    if total_stats["total_trades"] > 0:
        overall_rate = (total_stats["total_wins"] / total_stats["total_trades"] * 100)
        pnl_emoji = "🟢" if total_stats["total_pnl"] >= 0 else "🔴"

        summary = f"\n<b>📈 الملخص</b>\n"
        summary += f"┌─ إجمالي الصفقات: <b>{total_stats['total_trades']}</b> صفقة\n"
        summary += f"├─ الرابحة: <b>{total_stats['total_wins']}</b> | الخاسرة: <b>{total_stats['total_trades'] - total_stats['total_wins']}</b>\n"
        summary += f"├─ نسبة النجاح: <b>{overall_rate:.1f}%</b>\n"
        summary += f"└─ الربح الكلي: {pnl_emoji} <b>{total_stats['total_pnl']:.2f} USDT</b>"
    else:
        summary = "\n<i>لا توجد صفقات في هذه الفترة</i>"

    footer = "\n\n<i>⚠️ تحليل تعليمي فقط — ليس نصيحة مالية</i>"

    return header + "\n".join(table_rows) + summary + footer

def send_telegram(message):
    """إرسال رسالة Telegram"""
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram not configured")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        if r.status_code == 200:
            print(f"✓ Sent to Telegram")
            return True
        else:
            print(f"✗ Telegram error: {r.status_code}")
            return False
    except Exception as e:
        print(f"✗ Telegram error: {e}")
        return False

def main():
    period = sys.argv[1] if len(sys.argv) > 1 else "daily"

    # رابط الداشبورد على GitHub Pages
    dashboard_url = "https://a3alshehhi.github.io/trade-bot/performance_dashboard.html"

    if period == "daily":
        message = format_telegram_message("يومي", days=1, dashboard_url=dashboard_url)
    elif period == "weekly":
        message = format_telegram_message("أسبوعي", days=7, dashboard_url=dashboard_url)
    elif period == "monthly":
        message = format_telegram_message("شهري", days=30, dashboard_url=dashboard_url)
    else:
        message = format_telegram_message("شامل", days=None, dashboard_url=dashboard_url)

    print(message)
    print("\n" + "="*50)
    send_telegram(message)

if __name__ == "__main__":
    main()
