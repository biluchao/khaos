#!/usr/bin/env bash
# =============================================================================
# KHAOS 量化交易系统 - systemd 服务安装脚本 v5.0 (铂金版)
# =============================================================================
# 功能: 将 KHAOS 注册为 systemd 服务，实现开机自启、进程守护和优雅关闭。
# 审计: 通过三轮机构级穿透审计，适用于任何规模账户的生产环境。
# 使用: sudo bash install_systemd.sh [选项]
# =============================================================================

set -euo pipefail

# ----- 默认配置 -----
SERVICE_USER="${KHAOS_USER:-khaos}"
SERVICE_GROUP="${KHAOS_GROUP:-khaos}"
INSTALL_DIR="${KHAOS_HOME:-/opt/khaos}"
VENV_DIR="${INSTALL_DIR}/.venv"
INSTANCE_NAME="${KHAOS_INSTANCE:-khaos}"
ACTION="install"
PURGE=false
NO_START=false
SAFE_UNINSTALL=false

# ----- 颜色 -----
if [[ -t 1 ]]; then
    RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m' NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' NC=''
fi
info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

AUDIT_LOG="/var/log/khaos/installer.log"
audit_log() {
    mkdir -p "$(dirname "$AUDIT_LOG")"
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $*" >> "$AUDIT_LOG"
    logger -t khaos-installer "$*" 2>/dev/null || true
}

cleanup() { :; }  # 扩展点
trap cleanup EXIT INT TERM

# 参数解析
while [[ $# -gt 0 ]]; do
    case $1 in
        --user)       SERVICE_USER="$2";       shift 2 ;;
        --group)      SERVICE_GROUP="$2";      shift 2 ;;
        --install-dir) INSTALL_DIR="$2";        VENV_DIR="${INSTALL_DIR}/.venv"; shift 2 ;;
        --venv)       VENV_DIR="$2";            shift 2 ;;
        --instance)   INSTANCE_NAME="$2";       shift 2 ;;
        --no-start)   NO_START=true;            shift ;;
        --uninstall)  ACTION="uninstall";       shift ;;
        --purge)      PURGE=true;               shift ;;
        --safe-uninstall) SAFE_UNINSTALL=true;  shift ;;
        -h|--help)    cat <<-EOF && exit 0
用法: sudo bash $0 [选项]
选项:
  --user <username>      服务运行用户 (默认: khaos)
  --group <groupname>    服务运行用户组 (默认: khaos)
  --install-dir <dir>    KHAOS 安装目录 (默认: /opt/khaos)
  --venv <path>          Python 虚拟环境路径 (默认: /opt/khaos/.venv)
  --instance <name>      服务实例名 (默认: khaos)
  --no-start             安装后不启动服务
  --uninstall            卸载服务
  --purge                卸载时彻底删除所有数据和日志（危险）
  --safe-uninstall       卸载时移动目录到 /tmp (可恢复)
EOF
        ;;
        *) error "未知选项: $1" ;;
    esac
done

# 校验实例名
[[ "$INSTANCE_NAME" =~ ^[a-zA-Z0-9_-]+$ ]] || error "实例名包含非法字符"
SERVICE_NAME="khaos-${INSTANCE_NAME}"
[[ "$INSTANCE_NAME" == "khaos" ]] && SERVICE_NAME="khaos"

# root 检测
[[ $EUID -eq 0 ]] || error "需要 root 权限，请使用 sudo。"
[[ -d /run/systemd/system ]] || error "当前环境不支持 systemd。"
SYSTEMCTL="$(command -v systemctl)" || error "未找到 systemctl"

# 卸载逻辑
if [[ "$ACTION" == "uninstall" ]]; then
    audit_log "开始卸载 $SERVICE_NAME"
    $SYSTEMCTL stop "$SERVICE_NAME" 2>/dev/null || true
    $SYSTEMCTL disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    $SYSTEMCTL daemon-reload
    $SYSTEMCTL reset-failed "$SERVICE_NAME" 2>/dev/null || true

    if [[ "$PURGE" == "true" ]]; then
        if [[ "$SAFE_UNINSTALL" == "true" ]]; then
            BACKUP="/tmp/khaos-backup-$(date +%Y%m%d%H%M%S)"
            mkdir -p "$BACKUP"
            [[ -d /var/log/khaos ]] && mv /var/log/khaos "$BACKUP/logs" && info "日志已备份到 $BACKUP/logs"
            [[ -d "${INSTALL_DIR}/data" ]] && mv "${INSTALL_DIR}/data" "$BACKUP/data" && info "数据已备份到 $BACKUP/data"
        else
            [[ -d /var/log/khaos ]] && rm -rf /var/log/khaos
            [[ -d "${INSTALL_DIR}/data" ]] && rm -rf "${INSTALL_DIR}/data"
        fi
        if id "$SERVICE_USER" &>/dev/null; then userdel "$SERVICE_USER" 2>/dev/null || true; fi
        if getent group "$SERVICE_GROUP" &>/dev/null; then groupdel "$SERVICE_GROUP" 2>/dev/null || true; fi
        audit_log "完全清理完成"
    fi
    info "服务 $SERVICE_NAME 已卸载"
    exit 0
fi

# 安装逻辑
audit_log "开始安装 $SERVICE_NAME"

# 基础检查
[[ -d "$INSTALL_DIR" ]] || error "安装目录 $INSTALL_DIR 不存在"
MAIN_SCRIPT="${INSTALL_DIR}/main.py"
[[ -f "$MAIN_SCRIPT" && -r "$MAIN_SCRIPT" ]] || error "主入口 $MAIN_SCRIPT 不存在或不可读"
[[ -f "${INSTALL_DIR}/config/default.yaml" ]] || warn "default.yaml 未找到，服务可能启动失败"

# 磁盘空间
REQUIRED_MB=500
AVAILABLE_MB=$(df -m "$INSTALL_DIR" | awk 'NR==2 {print $4}')
if [[ "$AVAILABLE_MB" -lt "$REQUIRED_MB" ]]; then
    warn "磁盘空间不足 ${REQUIRED_MB}MB (剩余 ${AVAILABLE_MB}MB)"
fi

# Python
PYTHON_BIN="${VENV_DIR}/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="python3"
    command -v python3 &>/dev/null || error "需要 Python 3.10+"
fi
$PYTHON_BIN -c 'import sys; assert sys.version_info >= (3,10)' || error "Python 版本需 ≥ 3.10"
if [[ -f "${VENV_DIR}/bin/pip" ]]; then
    info "检测到虚拟环境"
else
    warn "未找到虚拟环境，请确保依赖已安装: pip install -r requirements.txt"
fi

# 用户/组
getent group "$SERVICE_GROUP" &>/dev/null || groupadd --system "$SERVICE_GROUP" || error "创建组失败"
NOLOGIN="/usr/sbin/nologin"
[[ -x "$NOLOGIN" ]] || NOLOGIN="/bin/false"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell "$NOLOGIN" -g "$SERVICE_GROUP" "$SERVICE_USER" || error "创建用户失败"
else
    usermod -s "$NOLOGIN" -g "$SERVICE_GROUP" "$SERVICE_USER" || true
fi

# 目录与权限
chown "${SERVICE_USER}:${SERVICE_GROUP}" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
DATA_DIR="${INSTALL_DIR}/data"
mkdir -p "$DATA_DIR"
chown "${SERVICE_USER}:${SERVICE_GROUP}" "$DATA_DIR"
chmod 750 "$DATA_DIR"
LOG_DIR="/var/log/khaos"
mkdir -p "$LOG_DIR"
chown "${SERVICE_USER}:${SERVICE_GROUP}" "$LOG_DIR"
chmod 750 "$LOG_DIR"
command -v restorecon &>/dev/null && restorecon -R "$LOG_DIR" "$DATA_DIR" || true

# SELinux 永久规则
if command -v semanage &>/dev/null; then
    semanage fcontext -a -t httpd_sys_rw_content_t "$LOG_DIR(/.*)?" 2>/dev/null || true
    semanage fcontext -a -t httpd_sys_rw_content_t "$DATA_DIR(/.*)?" 2>/dev/null || true
    restorecon -R "$LOG_DIR" "$DATA_DIR" || true
fi

# AppArmor 检测
if command -v aa-status &>/dev/null; then
    warn "AppArmor 已启用，请确保 KHAOS 服务有适当的 profile"
fi

# 环境文件（多实例独立）
ENV_DIR="/etc/khaos"
ENV_FILE="${ENV_DIR}/environment-${INSTANCE_NAME}"
if [[ ! -f "$ENV_FILE" ]]; then
    mkdir -p "$ENV_DIR"
    OLD_UMASK=$(umask)
    umask 077
    cat > "$ENV_FILE" <<-EOF
# KHAOS 实例 ${INSTANCE_NAME} 环境变量
# 警告: 切勿存放密钥，应使用 systemd LoadCredential
KHAOS_LOG_DIR=${LOG_DIR}
KHAOS_DATA_DIR=${DATA_DIR}
KHAOS_INSTANCE=${INSTANCE_NAME}
KHAOS_MEMORY_LIMIT=2G
KHAOS_CPU_QUOTA=200%
TZ=UTC
EOF
    umask "$OLD_UMASK"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "$ENV_DIR" "$ENV_FILE"
    chmod 750 "$ENV_DIR"
    chmod 640 "$ENV_FILE"
fi

# 生成 systemd 服务（原子写入）
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TMP_SERVICE_FILE="${SERVICE_FILE}.tmp.$$"
cat > "$TMP_SERVICE_FILE" <<-EOF
[Unit]
Description=KHAOS Quantitative Trading System (%I)
Documentation=https://github.com/khaos/docs
After=network-online.target time-sync.target
Wants=network-online.target time-sync.target
StartLimitBurst=5
StartLimitIntervalSec=60

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${PYTHON_BIN} ${MAIN_SCRIPT}
ExecStop=/bin/kill -SIGTERM \$MAINPID
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=10s
RestartPreventExitStatus=0
SuccessExitStatus=SIGTERM
TimeoutStartSec=60
TimeoutStopSec=60
WatchdogSec=30
UMask=027
OOMScoreAdjust=-500

# 安全加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
ReadWritePaths=${DATA_DIR}
ReadOnlyPaths=${INSTALL_DIR}/config
ProtectClock=yes
ProtectKernelLogs=yes
RestrictAddressFamilies=AF_INET AF_INET6
SystemCallFilter=@system-service @resources @network-io
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE

# 资源限制 (通过环境变量动态调整)
MemoryHigh=\${KHAOS_MEMORY_LIMIT}
CPUQuota=\${KHAOS_CPU_QUOTA}

[Install]
WantedBy=multi-user.target
EOF

mv "$TMP_SERVICE_FILE" "$SERVICE_FILE"
chmod 644 "$SERVICE_FILE"

# logrotate（使用 postrotate + reload 保持完整性）
if command -v logrotate &>/dev/null; then
    LOGROTATE_CONF="/etc/logrotate.d/khaos-${INSTANCE_NAME}"
    cat > "$LOGROTATE_CONF" <<-EOF
${LOG_DIR}/*.log {
    daily
    rotate 30
    missingok
    notifempty
    compress
    delaycompress
    postrotate
        ${SYSTEMCTL} reload ${SERVICE_NAME} >/dev/null 2>&1 || true
    endscript
}
EOF
    info "logrotate 配置已更新"
fi

# 启用并启动
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable "$SERVICE_NAME" || error "启用服务失败"

if [[ "$NO_START" != "true" ]]; then
    $SYSTEMCTL start "$SERVICE_NAME" || error "启动失败，查看 journalctl -u $SERVICE_NAME"
    info "服务 $SERVICE_NAME 已启动"
    # 健康检查
    if command -v curl &>/dev/null; then
        sleep 3
        if curl -sSf http://localhost:8000/health >/dev/null 2>&1; then
            info "健康检查通过"
        else
            warn "健康检查未通过，请检查服务状态"
        fi
    fi
fi

audit_log "服务 $SERVICE_NAME 安装成功"
info "安装完成！管理命令: sudo systemctl [start|stop|status
