#!/usr/bin/env bash
# =============================================================================
# KHAOS 系统健康检查 v3.0 (华尔街机构级生产标准)
# 功能: 深度检查系统与策略核心组件，支持文本/JSON输出，邮件/Pushgateway告警。
# 用法: bash health_check.sh [选项]
# =============================================================================

set -euo pipefail

# ----- 安全基础设置 -----
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LANG=C LC_ALL=C
umask 027

# ----- 默认配置 (可由 /etc/khaos/healthcheck.conf 覆盖) -----
SERVICE_NAME="${KHAOS_SERVICE_NAME:-khaos}"
API_HOST="${KHAOS_API_HOST:-127.0.0.1}"
API_PORT="${KHAOS_API_PORT:-8000}"
HEALTH_ENDPOINT="${KHAOS_HEALTH_ENDPOINT:-/health}"
TIMEOUT_SEC="${KHAOS_TIMEOUT_SEC:-5}"
DB_FILE="${KHAOS_DB_FILE:-/opt/khaos/data/khaos.db}"
EXCHANGE_URLS=(${KHAOS_EXCHANGE_URLS:-https://api.binance.com/api/v3/ping})
LOCK_FILE="/var/run/khaos/healthcheck.lock"
LOG_FILE=""
EMAIL_RECIPIENT=""
PUSH_GATEWAY=""
NO_COLOR=false
JSON_MODE=false
QUIET_MODE=false
SCRIPT_VERSION="3.0"

# ----- 颜色 -----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
if ! [[ -t 1 ]]; then NO_COLOR=true; fi

# ----- 锁文件 -----
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "另一个健康检查实例正在运行，退出。" >&2; exit 1; }
trap "rm -f '$LOCK_FILE'" EXIT

# ----- 辅助函数 -----
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g'; }
info() { if ! $QUIET_MODE; then echo -e "${NO_COLOR:+$1}"; fi; }
status_ok() { STATUS_RESULTS["$1"]="ok"; info "  ${NO_COLOR:+[✓] }$2"; }
status_warn() { STATUS_RESULTS["$1"]="warn"; ((ISSUES_FOUND++)); info "  ${NO_COLOR:+[!] }$2"; OVERALL_STATUS="degraded"; }
status_error() { STATUS_RESULTS["$1"]="error"; ((ISSUES_FOUND++)); OVERALL_STATUS="unhealthy"; info "  ${NO_COLOR:+[✗] }$2"; }

# ----- 参数解析 -----
while [[ $# -gt 0 ]]; do
    case $1 in
        --json) JSON_MODE=true ;;
        --quiet) QUIET_MODE=true ;;
        --no-color) NO_COLOR=true ;;
        --config) shift; source "$1" || { echo "配置文件错误: $1" >&2; exit 1; } ;;
        --log-file) shift; LOG_FILE="$1" ;;
        --email) shift; EMAIL_RECIPIENT="$1" ;;
        --push-gateway) shift; PUSH_GATEWAY="$1" ;;
        --version) echo "$SCRIPT_VERSION"; exit 0 ;;
        -h|--help) cat <<EOF; exit 0 ;;
用法: $0 [选项]
选项:
  --json              JSON 格式输出
  --quiet             只输出错误
  --no-color          禁用彩色输出
  --config FILE       指定配置文件
  --log-file FILE     同时输出到日志文件
  --email ADDR        发送邮件报告
  --push-gateway URL  推送到 Prometheus Pushgateway
  --version           显示版本
  -h, --help          显示帮助
EOF
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
    shift
done
$JSON_MODE && QUIET_MODE=true

# ----- 邮件发送 -----
send_email() {
    if [[ -n "$EMAIL_RECIPIENT" ]]; then
        mail -s "KHAOS 健康检查报告" "$EMAIL_RECIPIENT" < "$1"
    fi
}

# ----- Pushgateway 推送 -----
push_to_gateway() {
    if [[ -n "$PUSH_GATEWAY" ]]; then
        local job="khaos_healthcheck"
        for key in "${!STATUS_RESULTS[@]}"; do
            local val=0
            case ${STATUS_RESULTS[$key]} in
                ok) val=1; warn_val=0; err_val=0 ;;
                warn) val=0; warn_val=1; err_val=0 ;;
                error) val=0; warn_val=0; err_val=1 ;;
            esac
            echo "khaos_healthcheck{component=\"$key\",type=\"ok\"} $val" | curl -s --data-binary @- "$PUSH_GATEWAY/metrics/job/$job" >/dev/null 2>&1 || true
        done
    fi
}

# ----- 核心检查函数 -----
check_dependencies() {
    info "检查核心依赖..."
    local missing=0
    for cmd in systemctl curl df awk pgrep sqlite3 timeout bc ss ip dig; do
        command -v "$cmd" &>/dev/null || { status_warn "dep_$cmd" "缺少命令: $cmd"; missing=1; }
    done
    [[ $missing -eq 0 ]] && status_ok "deps" "核心依赖满足"
}

check_systemd_service() {
    info "检查 systemd 服务..."
    if command -v systemctl &>/dev/null; then
        local active=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo inactive)
        local enabled=$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || echo disabled)
        [[ $active == active ]] && status_ok "service" "运行中, 自启: $enabled" || status_error "service" "状态: $active"
    else
        status_warn "service" "systemctl 不可用"
    fi
}

check_api_health() {
    info "检查 API 端点..."
    local url="http://${API_HOST}:${API_PORT}${HEALTH_ENDPOINT}"
    local code body
    code=$(curl -sS --connect-timeout 2 --max-time "$TIMEOUT_SEC" -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || code="000"
    if [[ $code == 200 ]]; then
        body=$(curl -sS --connect-timeout 2 --max-time "$TIMEOUT_SEC" "$url")
        local status=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
        [[ -n $status && $status != "ok" ]] && status_warn "api" "状态: $status" || status_ok "api" "正常 (200)"
    else
        status_error "api" "HTTP $code"
    fi
}

check_exchange_connections() {
    info "检查交易所连接..."
    for url in "${EXCHANGE_URLS[@]}"; do
        # URL 白名单校验
        [[ "$url" =~ ^https:// ]] || { status_warn "exch_url" "非法URL: $url"; continue; }
        local code=$(curl -sS --connect-timeout 2 --max-time "$TIMEOUT_SEC" -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || code="000"
        [[ $code == 200 ]] && status_ok "exch_${url##*/}" "可达" || status_warn "exch_${url##*/}" "HTTP $code"
    done
}

check_process() {
    info "检查策略进程..."
    local pid=$(pgrep -f "python.*main.py" 2>/dev/null | head -1) || true
    if [[ -n $pid ]]; then
        if [[ -f /proc/$pid/cmdline ]]; then
            local rss=$(ps -o rss= -p $pid 2>/dev/null)
            status_ok "process" "PID $pid, RSS ${rss:-?}KB"
        else
            status_warn "process" "PID $pid 无效"
        fi
    else
        status_warn "process" "未检测到 main.py 进程"
    fi
}

check_database() {
    info "检查数据库..."
    if [[ -f "$DB_FILE" ]]; then
        if [[ -r "$DB_FILE" && -w "$DB_FILE" ]]; then
            if command -v sqlite3 &>/dev/null; then
                local integrity=$(timeout "$TIMEOUT_SEC" sqlite3 "$DB_FILE" "PRAGMA integrity_check;" 2>&1) || integrity="fail"
                [[ $integrity == "ok" ]] && status_ok "db" "数据库正常" || status_error "db" "损坏: $integrity"
            else
                status_ok "db" "文件可读写 (无sqlite3)"
            fi
        else
            status_error "db" "权限错误"
        fi
    else
        status_warn "db" "数据库文件不存在"
    fi
}

check_disk() {
    info "检查磁盘/Inode..."
    while IFS= read -r line; do
        read -r _ _ _ usage mount <<< "$line"
        usage=${usage%\%}
        if [[ $usage -gt 95 ]]; then status_error "disk_$mount" "$mount 使用 ${usage}%"
        elif [[ $usage -gt 80 ]]; then status_warn "disk_$mount" "$mount 使用 ${usage}%"
        else status_ok "disk_$mount" "$mount ${usage}%"
        fi
    done < <(df -P | awk 'NR>1 {print $1, $5, $6}')
    # Inode
    while IFS= read -r line; do
        read -r _ _ _ iuse mount <<< "$line"
        iuse=${iuse%\%}
        [[ $iuse -gt 90 ]] && status_warn "inode_$mount" "$mount Inode ${iuse}%"
    done < <(df -iP | awk 'NR>1 {print $1, $5, $6}')
}

check_memory_swap() {
    info "检查内存/Swap..."
    local total=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
    local avail=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
    local swap_total=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
    local swap_free=$(awk '/SwapFree/ {print $2}' /proc/meminfo)
    local pct=$((100 - avail * 100 / total))
    [[ $pct -gt 95 ]] && status_error "memory" "内存 ${pct}%" || [[ $pct -gt 85 ]] && status_warn "memory" "内存 ${pct}%" || status_ok "memory" "内存 ${pct}%"
    if [[ $swap_total -gt 0 ]]; then
        local swap_pct=$((100 - swap_free * 100 / swap_total))
        [[ $swap_pct -gt 50 ]] && status_warn "swap" "Swap ${swap_pct}%" || status_ok "swap" "Swap ${swap_pct}%"
    fi
}

check_cpu() {
    info "检查 CPU..."
    local load=$(awk '{print $1}' /proc/loadavg)
    local cores=$(nproc)
    local pct
    if command -v bc &>/dev/null; then pct=$(echo "scale=1; $load/$cores*100" | bc)
    else pct=$((load * 100 / cores))
    fi
    [[ ${pct%.*} -gt 90 ]] && status_error "cpu" "负载 ${pct}%" || [[ ${pct%.*} -gt 75 ]] && status_warn "cpu" "负载 ${pct}%" || status_ok "cpu" "负载 ${pct}%"
    # 频率
    if [[ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ]]; then
        local freq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)
        status_ok "cpu_freq" "当前频率 $((freq/1000)) MHz"
    fi
}

check_time_sync() {
    info "检查时间同步..."
    if command -v chronyc &>/dev/null; then
        local offset=$(chronyc tracking | awk '/Last offset/ {print $4}') || offset=""
        if [[ -n $offset ]]; then
            local abs=${offset#-}
            if (( $(echo "$abs > 0.5" | bc -l) )); then status_warn "ntp" "偏差 ${offset}s"
            else status_ok "ntp" "偏差 ${offset}s"
            fi
        else status_warn "ntp" "无法获取偏差"
        fi
    else status_warn "ntp" "chronyc 未安装"
    fi
}

check_network() {
    info "检查网络接口..."
    local found=0
    for iface in eth0 ens3 ens33; do
        if ip link show "$iface" &>/dev/null; then
            found=1
            if ip link show "$iface" | grep -q "state UP"; then status_ok "net_$iface" "UP"
            else status_warn "net_$iface" "DOWN"
            fi
        fi
    done
    [[ $found -eq 0 ]] && status_warn "network" "未检测到物理接口"
    # DNS
    if command -v dig &>/dev/null; then
        dig +short +time=2 google.com &>/dev/null && status_ok "dns" "DNS 正常" || status_warn "dns" "DNS 解析失败"
    fi
    # TCP 重传率 (简化)
    if [[ -f /proc/net/snmp ]]; then
        local retrans=$(awk '/Tcp:/ {print $13}' /proc/net/snmp)
        status_ok "tcp_retrans" "TCP 重传段: ${retrans:-?}"
    fi
}

check_zombies() {
    local count=$(ps -eo stat | grep -c 'Z' 2>/dev/null || echo 0)
    if [[ $count -gt 10 ]]; then status_warn "zombies" "僵尸进程: $count"
    elif [[ $count -gt 0 ]]; then status_ok "zombies" "少量僵尸: $count"
    else status_ok "zombies" "无僵尸进程"
    fi
}

check_fd_usage() {
    if [[ -f /proc/sys/fs/file-nr ]]; then
        read -r curr max _ < /proc/sys/fs/file-nr
        local pct=$((curr * 100 / max))
        [[ $pct -gt 80 ]] && status_warn "fd" "文件描述符使用 ${pct}%" || status_ok "fd" "FD 使用 ${pct}%"
    fi
}

# ----- 系统信息 -----
system_info() {
    info "系统: $(uname -a)"
    info "运行时间: $(uptime -p)"
    info "健康检查版本: $SCRIPT_VERSION"
}

# ----- 主流程 -----
main() {
    declare -gA STATUS_RESULTS
    ISSUES_FOUND=0
    OVERALL_STATUS="healthy"

    system_info
    check_dependencies
    check_systemd_service
    check_api_health
    check_process
    check_exchange_connections
    check_database
    check_disk
    check_memory_swap
    check_cpu
    check_time_sync
    check_network
    check_zombies
    check_fd_usage

    # 输出
    if $JSON_MODE; then
        echo "{"
        echo "  \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
        echo "  \"overall_status\": \"$OVERALL_STATUS\","
        echo "  \"issues_found\": $ISSUES_FOUND,"
        echo "  \"checks\": {"
        local first=true
        for k in "${!STATUS_RESULTS[@]}"; do
            $first && first=false || echo ","
            printf "    \"%s\": \"%s\"" "$k" "${STATUS_RESULTS[$k]}"
        done
        echo ""
        echo "  }"
        echo "}" | tee "${LOG_FILE:-/dev/null}"
    else
        local report=""
        report+="============================================\n"
        if [[ $OVERALL_STATUS == "healthy" ]]; then
            report+="${GREEN}系统状态: 健康${NC} (0 问题)\n"
        elif [[ $OVERALL_STATUS == "degraded" ]]; then
            report+="${YELLOW}系统状态: 降级${NC} (${ISSUES_FOUND} 警告)\n"
        else
            report+="${RED}系统状态: 不健康${NC} (${ISSUES_FOUND} 错误)\n"
        fi
        echo -e "$report" | tee "${LOG_FILE:-/dev/null}"
    fi

    # 邮件
    if [[ -n "$EMAIL_RECIPIENT" && -n "${LOG_FILE:-}" ]]; then
        send_email "$LOG_FILE"
    fi

    # Pushgateway
    if [[ -n "$PUSH_GATEWAY" ]]; then
        push_to_gateway
    fi

    case $OVERALL_STATUS in
        healthy) exit 0 ;;
        degraded) exit 1 ;;
        unhealthy) exit 2 ;;
    esac
}

main "$@"
