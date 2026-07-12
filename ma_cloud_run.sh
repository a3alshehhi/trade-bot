#!/bin/bash

# بوت سحابة المتوسطات — سكريبت تشغيل سريع
# ========================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# إعدادات افتراضية
FRAMES="${MC_BT_FRAMES:-15m,1h,4h}"
SYMBOLS="${MC_BT_SYMBOLS:-40}"
HOLD="${MC_BT_HOLD:-200}"
MODE="${1:-scan}"

echo "════════════════════════════════════════"
echo "بوت سحابة المتوسطات (MA Cloud)"
echo "════════════════════════════════════════"
echo ""

case "$MODE" in
    backtest)
        echo "🔄 تشغيل باك-تست..."
        echo "   الفريمات: $FRAMES"
        echo "   الرموز: $SYMBOLS"
        echo "   الـ hold: $HOLD شمعة"
        echo ""
        MC_BT_FRAMES="$FRAMES" MC_BT_SYMBOLS="$SYMBOLS" MC_BT_HOLD="$HOLD" \
        python3 ma_cloud_bot.py backtest
        ;;
    scan)
        echo "🔍 تشغيل الـ scan الحي..."
        python3 ma_cloud_bot.py scan
        ;;
    *)
        echo "❌ وضع غير معروف: $MODE"
        echo ""
        echo "الأوضاع المتاحة:"
        echo "  ./ma_cloud_run.sh scan      # الـ scan الحي (افتراضي)"
        echo "  ./ma_cloud_run.sh backtest  # الباك-تست على 3 فريمات"
        echo ""
        echo "متغيرات البيئة (للباك-تست):"
        echo "  MC_BT_FRAMES='15m,1h'       # فريمات مخصصة"
        echo "  MC_BT_SYMBOLS=20            # عدد رموز مخصص"
        echo "  MC_BT_HOLD=150              # أقصى hold مخصص"
        echo ""
        echo "مثال:"
        echo "  MC_BT_FRAMES='15m,1h' MC_BT_SYMBOLS=30 ./ma_cloud_run.sh backtest"
        exit 1
        ;;
esac

echo ""
echo "✅ انتهى"
