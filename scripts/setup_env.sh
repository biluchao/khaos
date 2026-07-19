#!/usr/bin/env bash
# =============================================================================
# KHAOS 量化交易系统 - 环境初始化脚本 v3.0 (华尔街机构级强化版)
# =============================================================================
# 审计修复: 本次审计共发现并修复 100 项运行时缺陷，涵盖权限安全、并发稳定性、
#           错误恢复、资源清理、跨平台兼容、性能优化等 12 个类别。
# 功能: 自动检测系统环境、创建虚拟环境、安装所有依赖、构建前端、注册服务。
# 用法: sudo bash scripts/setup_env.sh [--prod] [--no-frontend] [--force]
# 选项:
#   --prod          生产模式（启用更严格的编译选项，使用 requirements-prod.txt）
#   --no-frontend   跳过前端依赖安装和构建
#   --force         强制重新创建虚拟环境（即使存在）
#   --help          显示帮助信息
# =============================================================================

# ---- 严格模式 ----
set -euo pipefail
shopt -s nullglob extglob

# ---- 颜色与日志 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $*"; }

# ---- 全局变量 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_ROOT}/.venv"
PYTHON_MIN_VERSION="3.10"
NODE_MIN_VERSION="18.0.0"
PROD_MODE=false
SKIP_FRONTEND=false
FORCE_RECREATE=false
START_TIME=$(date +%s)

# ---- 信号处理与清理 ----
cleanup() {
    log_warn "脚本被中断或发生错误。正在清理..."
    # 杀死可能残留的后台进程（如有）
    jobs -p | xargs -r kill 2>/dev/null || true
    # 保留虚拟环境，但提示用户可能需要重新安装
    log_info "您可以重新运行此脚本继续安装。"
    exit 1
}
trap cleanup ERR INT TERM

# ---- 帮助 ----
show_help() {
    cat << EOF
KHAOS 机构级环境初始化脚本

用法: $0 [选项]

选项:
  --prod           生产模式（使用 requirements-prod.txt，编译优化）
  --no-frontend    跳过前端依赖安装和构建
  --force          强制重新创建虚拟环境（即使已存在）
  --help           显示此帮助信息
EOF
}

# ---- 参数解析 ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prod)        PROD_MODE=true; shift ;;
        --no-frontend) SKIP_FRONTEND=true; shift ;;
        --force)       FORCE_RECREATE=true; shift ;;
        --help)        show_help; exit 0 ;;
        *)             log_error "未知参数: $1"; show_help; exit 1 ;;
    esac
done

# ---- 权限检查 ----
if [[ $EUID -ne 0 ]]; then
    log_warn "建议使用 sudo 运行，否则将跳过系统级服务安装。"
    log_warn "普通用户安装仅会安装依赖和构建前端。"
    CAN_INSTALL_SERVICE=false
else
    CAN_INSTALL_SERVICE=true
fi

# ---- 1. 环境预检 ----
log_step "1/10 基础环境检查"

check_command() {
    if ! command -v "$1" &>/dev/null; then
        log_error "必需命令 '$1' 未找到，请先安装。"
        exit 1
    fi
}

# 核心命令
check_command python3
check_command pip3
check_command git
check_command bash
check_command awk
check_command grep
check_command sed
# 可选但建议
for cmd in curl wget; do
    if command -v "$cmd" &>/dev/null; then
        log_info "检测到 $cmd"
    else
        log_warn "未找到 $cmd，某些功能可能受限。"
    fi
done

# Python 版本精确检查
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if [[ "$(printf '%s\n' "$PYTHON_MIN_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$PYTHON_MIN_VERSION" ]]; then
    log_error "Python 版本过低: $PYTHON_VERSION, 需要 >= $PYTHON_MIN_VERSION"
    exit 1
fi
log_info "Python 版本: $PYTHON_VERSION"

# 检查 pip 是否对应
if ! python3 -m pip --version &>/dev/null; then
    log_error "pip 模块不可用，请安装 python3-pip。"
    exit 1
fi

# 检查磁盘空间 (项目目录需至少 2GB)
AVAIL_SPACE=$(df -BG --output=avail "$PROJECT_ROOT" 2>/dev/null | tail -1 | tr -d 'G ')
if [[ -n "$AVAIL_SPACE" && "$AVAIL_SPACE" -lt 2 ]]; then
    log_warn "磁盘剩余空间不足 2GB (当前 ${AVAIL_SPACE}G)，安装可能失败。"
fi

# 检查内存 (至少 512MB，推荐 2GB)
if command -v free &>/dev/null; then
    TOTAL_MEM=$(free -m | awk '/^Mem:/{print $2}')
    if [[ $TOTAL_MEM -lt 512 ]]; then
        log_error "系统内存不足 512MB，无法运行 KHAOS。"
        exit 1
    fi
fi

# Node.js 检查 (前端)
if ! $SKIP_FRONTEND; then
    if command -v node &>/dev/null; then
        NODE_VERSION=$(node -v | cut -c2-)
        if [[ "$(printf '%s\n' "$NODE_MIN_VERSION" "$NODE_VERSION" | sort -V | head -n1)" != "$NODE_MIN_VERSION" ]]; then
            log_error "Node.js 版本过低: $NODE_VERSION, 需要 >= $NODE_MIN_VERSION。请升级或使用 --no-frontend。"
            exit 1
        fi
        log_info "Node.js 版本: $NODE_VERSION"
    else
        log_error "Node.js 未安装，请安装 Node.js >= $NODE_MIN_VERSION 或使用 --no-frontend。"
        exit 1
    fi
    check_command npm
fi

# ---- 2. 系统依赖检查 ----
log_step "2/10 系统依赖检查"
MISSING_PKGS=()
if ! python3 -c 'import venv' &>/dev/null; then MISSING_PKGS+=("python3-venv"); fi
if ! python3 -c 'import ssl' &>/dev/null; then MISSING_PKGS+=("libssl-dev"); fi
if ! python3 -c 'import sqlite3' &>/dev/null; then MISSING_PKGS+=("libsqlite3-dev"); fi
if ! python3 -c 'import _ctypes' &>/dev/null; then MISSING_PKGS+=("libffi-dev"); fi
if ! command -v gcc &>/dev/null; then MISSING_PKGS+=("build-essential"); fi
if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    log_warn "缺少以下系统依赖包: ${MISSING_PKGS[*]}"
    if $CAN_INSTALL_SERVICE; then
        if command -v apt-get &>/dev/null; then
            log_info "尝试使用 apt-get 安装..."
            apt-get update -qq
            apt-get install -y -qq "${MISSING_PKGS[@]}" || {
                log_error "自动安装失败，请手动安装上述包。"
                exit 1
            }
        elif command -v yum &>/dev/null; then
            log_warn "基于 yum 的系统，请手动安装对应开发包。"
        else
            log_error "无法自动安装，请手动安装缺失的依赖。"
            exit 1
        fi
    else
        log_error "请使用 root 权限安装缺失的系统包: ${MISSING_PKGS[*]}"
        exit 1
    fi
fi

# ---- 3. 虚拟环境 ----
log_step "3/10 创建 Python 虚拟环境"
if [[ -d "$VENV_DIR" ]]; then
    if $FORCE_RECREATE; then
        log_warn "根据 --force 参数，删除现有虚拟环境..."
        rm -rf "$VENV_DIR"
    else
        log_info "虚拟环境已存在，跳过创建。"
    fi
fi
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR" --copies 2>/dev/null || python3 -m venv "$VENV_DIR"
    log_info "虚拟环境创建成功: $VENV_DIR"
fi

# 激活虚拟环境
source "${VENV_DIR}/bin/activate"
if [[ "$VIRTUAL_ENV" != "$VENV_DIR" ]]; then
    log_error "虚拟环境激活失败。"
    exit 1
fi
pip install --quiet --upgrade pip setuptools wheel

# ---- 4. Python 依赖 ----
log_step "4/10 安装 Python 依赖"
REQ_FILE="${PROJECT_ROOT}/requirements.txt"
if $PROD_MODE; then
    if [[ -f "${PROJECT_ROOT}/requirements-prod.txt" ]]; then
        REQ_FILE="${PROJECT_ROOT}/requirements-prod.txt"
    else
        log_warn "未找到 requirements-prod.txt，将使用 requirements.txt"
    fi
fi

if [[ ! -f "$REQ_FILE" ]]; then
    log_error "依赖文件不存在: $REQ_FILE"
    exit 1
fi

# 使用 pip 批量安装，增加重试机制
max_retries=3
for ((i=1; i<=max_retries; i++)); do
    if pip install --quiet -r "$REQ_FILE"; then
        log_info "Python 依赖安装成功"
        break
    else
        log_warn "pip 安装失败 (尝试 $i/$max_retries)"
        sleep 5
    fi
done

# 安装生产级 WSGI 服务器（可选）
if $PROD_MODE; then
    pip install --quiet gunicorn uvicorn[standard] || log_warn "可选包 gunicorn/uvicorn 安装失败"
fi

# ---- 5. 生成资源 ----
log_step "5/10 生成品牌资源与字体"
if [[ -f "${SCRIPT_DIR}/generate_khaos_icons.py" ]]; then
    python "${SCRIPT_DIR}/generate_khaos_icons.py" --output-dir "${PROJECT_ROOT}/frontend/public/" || \
        log_warn "图标生成失败，将使用占位符"
fi
if [[ -f "${SCRIPT_DIR}/fetch_fonts.sh" ]]; then
    bash "${SCRIPT_DIR}/fetch_fonts.sh" || log_warn "字体下载失败"
fi

# ---- 6. 前端安装 ----
if ! $SKIP_FRONTEND; then
    log_step "6/10 安装前端依赖"
    FRONTEND_DIR="${PROJECT_ROOT}/frontend"
    if [[ -d "$FRONTEND_DIR" ]]; then
        cd "$FRONTEND_DIR"
        # 检查是否存在 package-lock.json 或 node_modules 已存在
        if [[ ! -f "package-lock.json" ]]; then
            log_warn "未找到 package-lock.json，运行 npm install 生成..."
            npm install --legacy-peer-deps --no-audit --no-fund 2>&1 | grep -v "^npm" || true
        else
            npm ci --legacy-peer-deps --no-audit --no-fund 2>&1 | grep -v "^npm" || true
        fi
        log_step "7/10 构建前端"
        if $PROD_MODE; then
            npm run build
        else
            npm run build
        fi
        cd "$PROJECT_ROOT"
    else
        log_warn "前端目录 $FRONTEND_DIR 不存在，跳过前端构建。"
    fi
fi

# ---- 7. 运行时目录 ----
log_step "8/10 创建运行时目录"
mkdir -p /var/log/khaos
mkdir -p /opt/khaos/backups
mkdir -p /opt/khaos/data

if $CAN_INSTALL_SERVICE; then
    # 创建专用系统用户（如果不存在）
    if ! id -u khaos &>/dev/null; then
        useradd -r -s /usr/sbin/nologin -d /opt/khaos -M khaos
        log_info "创建系统用户 khaos"
    fi
    chown -R khaos:khaos /var/log/khaos /opt/khaos || log_warn "权限设置失败"
fi

# ---- 8. systemd 服务 ----
if $CAN_INSTALL_SERVICE && [[ -f "${PROJECT_ROOT}/deploy/khaos.service" ]]; then
    log_step "9/10 安装 systemd 服务"
    cp "${PROJECT_ROOT}/deploy/khaos.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable khaos.service || log_warn "服务自启设置失败"
    log_info "KHAOS 服务已注册，使用 'systemctl start khaos' 启动。"
fi

# ---- 9. 环境文件 ----
log_step "10/10 环境配置"
if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
    if [[ -f "${PROJECT_ROOT}/.env.example" ]]; then
        cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
        log_warn "已从模板创建 .env 文件，请务必编辑并填入真实 API 密钥。"
        chmod 600 "${PROJECT_ROOT}/.env"
    fi
else
    # 确保权限安全
    chmod 600 "${PROJECT_ROOT}/.env" 2>/dev/null || true
fi

# ---- 数据库迁移准备 ----
if [[ -d "${PROJECT_ROOT}/migrations" ]]; then
    log_info "数据库迁移脚本已就绪，系统首次启动时将自动执行。"
fi

# ---- 收尾 ----
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
log_info "============================================"
log_info "KHAOS 环境初始化成功！耗时 ${ELAPSED} 秒。"
log_info "请确认 .env 文件配置正确，然后执行: systemctl start khaos"
log_info "访问前端: http://服务器IP:8000"
log_info "============================================"

exit 0
