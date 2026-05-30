#!/bin/zsh
# 每日竞品情报自动扫描
# 用法: nohup scripts/daily_intel_scan.sh &

set -u
APP_DIR="/Users/doi/Desktop/amazon"
LOG_FILE="$APP_DIR/logs/intel_scan_$(date +%Y%m%d).log"
API="http://127.0.0.1:8001"

mkdir -p "$APP_DIR/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== 每日竞品情报扫描开始 ==="

# 检查服务是否运行
if ! curl -s "$API/health" > /dev/null 2>&1; then
    log "❌ 服务未运行，退出"
    exit 1
fi

# Step 1: 着陆页情报采集（Tier 1，前 50 个）
log "Step 1: 采集 Tier 1 着陆页情报..."
RESULT=$(curl -s -X POST "$API/api/intel/full-scan?tier=tier1&workers=4" 2>&1)
LP_OK=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('landing_page_ok',0))" 2>/dev/null)
FB_OK=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('fb_ads_ok',0))" 2>/dev/null)
ALERTS=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('alerts',0))" 2>/dev/null)

log "  着陆页: $LP_OK 成功"
log "  FB Ad: $FB_OK 成功"
log "  告警: $ALERTS 条"

# Step 2: 检查告警
log "Step 2: 检查告警..."
ALERT_DATA=$(curl -s "$API/api/intel/alerts" 2>&1)
ALERT_COUNT=$(echo "$ALERT_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('count',0))" 2>/dev/null)

if [ "$ALERT_COUNT" -gt 0 ]; then
    log "⚠️ 发现 $ALERT_COUNT 条告警:"
    echo "$ALERT_DATA" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for a in d.get('alerts', [])[:10]:
    print(f'  [{a.get(\"severity\",\"\")}] {a.get(\"message\",\"\")}')
" 2>&1 | tee -a "$LOG_FILE"
else
    log "✅ 无告警"
fi

# Step 3: 产品目录刷新（智能模式，前 100 个）
log "Step 3: 产品目录智能刷新..."
REFRESH_RESULT=$(curl -s -X POST "$API/api/dashboard/background-refresh?mode=smart&limit=100&workers=6" 2>&1)
REFRESH_OK=$(echo "$REFRESH_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('started',False))" 2>/dev/null)

if [ "$REFRESH_OK" = "True" ]; then
    log "  产品刷新已启动"
else
    log "  产品刷新未启动（可能已在运行）"
fi

log "=== 每日竞品情报扫描完成 ==="
