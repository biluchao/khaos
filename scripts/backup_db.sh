#!/usr/bin/env bash
# =============================================================================
# KHAOS 量化交易系统 - 数据库备份脚本 v6.0 (华尔街旗舰终极版)
# =============================================================================
# 功能: 工业级数据库备份，支持 SQLite / PostgreSQL，压缩、加密、远程多协议
#       传输、完整性校验、深度恢复测试、指标上报、审计追踪。
# 适用: 100 美金至万亿美金账户，4K 中文界面运维。
# 配置: 可通过 /opt/khaos/config/backup.conf 预定义，支持多环境。
# 使用: sudo bash backup_db.sh [选项]
# 要求: bash >= 4.2, openssl >= 1.1.0, aws cli (可选), pigz (可选)
# =============================================================================

set -Eeuo pipefail

# ----- 全局只读常量 -----------------------------------------------------------
readonly SCRIPT_NAME="${0##*/}"
readonly SCRIPT_VERSION="6.0.0"
readonly TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# ----- 可配置默认值 (环境变量优先，可通过配置文件覆盖) ------------------------
db_type="${DB_TYPE:-sqlite}"
sqlite_db_path="${SQLITE_DB_PATH:-/opt/khaos/data/khaos.db}"
pg_dbname="${PG_DBNAME:-khaos}"
pg_host="${PG_HOST:-localhost}"
pg_port="${PG_PORT:-5432}"
pg_user="${PG_USER:-khaos}"
backup_dir="${BACKUP_DIR:-/opt/khaos/backups}"
retention_days="${RETENTION_DAYS:-30}"
enable_compress="${ENABLE_COMPRESS:-true}"
enable_encrypt="${ENABLE_ENCRYPT:-false}"
encryption_cipher="${ENCRYPTION_CIPHER:-aes-256-gcm}"
encrypt_pass="${KHAOS_BACKUP_PASS:-}"
remote_url="${REMOTE_URL:-}"
notify_hook="${KHAOS_WEBHOOK_URL:-}"
heartbeat_url="${KHAOS_HEARTBEAT_URL:-}"
dry_run="${DRY_RUN:-false}"
force_unlock="${FORCE_UNLOCK:-false}"
restore_test="${RESTORE_TEST:-false}"
deep_restore_test="${DEEP_RESTORE_TEST:-false}"  # 深度恢复测试（需要额外环境）
config_file="${CONFIG_FILE:-/opt/khaos/config/backup.conf}"
delete_local_after_upload="${DELETE_LOCAL_AFTER_UPLOAD:-false}"
metrics_push_url="${METRICS_PUSH_URL:-}"
backup_type="${BACKUP_TYPE:-full}"
language="${LANGUAGE:-zh_CN}"
instance_name="${INSTANCE_NAME:-main}"
s3_endpoint="${S3_ENDPOINT:-}"
s3_region="${S3_REGION:-}"
pre_backup_hook="${PRE_BACKUP_HOOK:-}"
post_backup_hook="${POST_BACKUP_HOOK:-}"
gzip_level="${GZIP_LEVEL:-6}"
ssh_port="${SSH_PORT:-22}"
exclude_table_data="${EXCLUDE_TABLE_DATA:-}"

# ----- 内部路径 ---------------------------------------------------------------
readonly lock_file="/var/run/khaos/backup.lock"
readonly log_file="/var/log/khaos/backup.log"
readonly error_log_file="/var/log/khaos/backup_error.log"
readonly min_disk_space_mb=200
readonly tmp_dir_base="${backup_dir}/tmp"

# 临时目录和文件
tmp_dir=""
pass_file=""
lock_fd=""
lock_type=""   # "flock" or "dir"

# 清理函数
cleanup() {
    # 释放锁
    if [[ "$lock_type" == "flock" && -n "${lock_fd:-}" ]]; then
        flock -u "$lock_fd" 2>/dev/null || true
    elif [[ "$lock_type" == "dir" ]]; then
        rmdir "$lock_file" 2>/dev/null || true
    fi
    # 删除密码文件
    if [[ -n "${pass_file:-}" && -f "$pass_file" ]]; then
        shred -u "$pass_file" 2>/dev/null || rm -f "$pass_file"
    fi
    # 清理临时目录
    if [[ -n "${tmp_dir:-}" && -d "$tmp_dir" ]]; then
        rm -rf "$tmp_dir" 2>/dev/null
    fi
    # 清理可能残留的子进程 (pg_dump 等)
    if [[ -n "${backup_pids:-}" ]]; then
        kill -9 $backup_pids 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'cleanup; exit 1' SIGTERM SIGINT SIGHUP

# ----- 中英文提示 ------------------------------------------------------------
case "$language" in
    zh_CN)
        info_prefix="信息"
        warn_prefix="警告"
        error_prefix="错误"
        ;;
    *)
        info_prefix="INFO"
        warn_prefix="WARN"
        error_prefix="ERROR"
        ;;
esac

# 颜色
if [[ -t 1 ]]; then
    red='\033[0;31m'; green='\033[0;32m'; yellow='\033[1;33m'; nc='\033[0m'
else
    red=''; green=''; yellow=''; nc=''
fi
info()  { echo -e "${green}[${info_prefix}]${nc}  $(date '+%F %T') $$ $*" | tee -a "$log_file"; }
warn()  { echo -e "${yellow}[${warn_prefix}]${nc}  $(date '+%F %T') $$ $*" | tee -a "$log_file" >&2; }
error() { echo -e "${red}[${error_prefix}]${nc} $(date '+%F %T') $$ $*" | tee -a "$log_file" >&2; exit 1; }

# 记录错误到单独文件
log_error() {
    echo "$(date '+%F %T') $$ $*" >> "$error_log_file"
}

# ----- 帮助 -------------------------------------------------------------------
usage() {
    cat << EOF
KHAOS 数据库备份脚本 v${SCRIPT_VERSION}
用法: $0 [选项]

环境变量:
  KHAOS_BACKUP_PASS      加密密码
  KHAOS_WEBHOOK_URL      通知 Webhook
  KHAOS_HEARTBEAT_URL    健康检查心跳 URL
  KHAOS_METRICS_URL      指标推送地址

选项:
  --db-type sqlite|postgres  数据库类型 (默认: $db_type)
  --db-path <路径>           SQLite 数据库路径 (可逗号分隔多个)
  --pg-dbname <库名>         PostgreSQL 数据库名 (可逗号分隔)
  --pg-host <主机>           PG 主机
  --pg-port <端口>           PG 端口
  --pg-user <用户>           PG 用户
  --backup-dir <目录>        备份输出目录
  --retention-days <天数>    本地保留天数 (默认 30)
  --compress                 启用压缩 (默认)
  --gzip-level <1-9>         压缩等级 (默认 6)
  --encrypt                  启用加密
  --encryption-pass <密码>   加密密码 (不推荐，请使用环境变量)
  --remote-url <URL>         远程目标 (s3://, scp://, rsync://)
  --s3-endpoint <URL>        S3 兼容存储端点 (MinIO 等)
  --s3-region <区域>         S3 区域
  --ssh-port <端口>          SCP 端口 (默认 22)
  --delete-local-after-upload 远程传输后删除本地副本
  --backup-type full|incr    备份类型 (默认 full)
  --instance-name <名称>     实例标识 (默认 main)
  --config-file <文件>       配置文件路径
  --restore-test             备份后测试恢复
  --deep-restore-test        深度恢复测试（需额外环境）
  --pre-backup-hook <脚本>   备份前执行的钩子
  --post-backup-hook <脚本>  备份完成后执行的钩子
  --dry-run                  模拟运行
  --quiet                    静默模式 (仅错误输出)
  --unlock                   清除锁文件
  --lang zh_CN|en            语言 (默认 zh_CN)
  -h, --help                 帮助
EOF
    exit 0
}

# ----- 依赖检查 ---------------------------------------------------------------
check_deps() {
    local deps=(gzip sqlite3 pg_dump openssl curl find xargs)
    [[ "$enable_compress" == "true" ]] && deps+=(gzip)
    for cmd in "${deps[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            error "缺少依赖: $cmd"
        fi
    done
    if [[ -n "$remote_url" ]]; then
        case "$remote_url" in
            s3://*)
                if ! command -v aws &>/dev/null; then error "远程S3传输需要AWS CLI"; fi
                ;;
            scp://*)
                if ! command -v scp &>/dev/null; then error "SCP传输需要scp命令"; fi
                ;;
            rsync://*)
                if ! command -v rsync &>/dev/null; then error "rsync传输需要rsync命令"; fi
                ;;
            *) error "不支持的远程协议: $remote_url" ;;
        esac
    fi
    if [[ "$enable_compress" == "true" ]]; then
        if command -v pigz &>/dev/null; then
            readonly compress_cmd="pigz"
        else
            readonly compress_cmd="gzip"
        fi
    fi
}

# ----- 锁管理 -----------------------------------------------------------------
acquire_lock() {
    mkdir -p "$(dirname "$lock_file")"
    if command -v flock &>/dev/null; then
        exec {lock_fd}>"$lock_file"
        if ! flock -n "$lock_fd"; then
            error "获取锁失败，备份进程可能已在运行。"
        fi
        lock_type="flock"
    else
        if ! mkdir "$lock_file" 2>/dev/null; then
            error "获取锁失败，备份进程可能已在运行。"
        fi
        lock_type="dir"
    fi
}

# ----- 磁盘空间与 inode 检查 -------------------------------------------------
check_disk_space() {
    local dir="$1"
    local avail
    avail=$(LANG=C df -l --output=avail "$dir" 2>/dev/null | tail -1 | awk '{print $1}')
    if [[ -n "$avail" ]] && (( avail < min_disk_space_mb * 1024 )); then
        error "磁盘空间不足 ${min_disk_space_mb}MB (可用: $((avail/1024))MB)，目录: $dir"
    fi
    # inode 检查
    local inode_avail
    inode_avail=$(LANG=C df -li "$dir" 2>/dev/null | tail -1 | awk '{print $4}')
    if [[ -n "$inode_avail" ]] && (( inode_avail < 1000 )); then
        warn "inode 数量不足 (可用: $inode_avail)，可能导致备份失败。"
    fi
}

# ----- 加密准备 ---------------------------------------------------------------
prepare_encryption() {
    if [[ "$enable_encrypt" != "true" ]]; then
        return
    fi
    # 密码优先级：命令行 > 环境变量 > 配置文件
    if [[ -z "$encrypt_pass" ]]; then
        encrypt_pass="${KHAOS_BACKUP_PASS:-}"
    fi
    if [[ -z "$encrypt_pass" ]]; then
        error "加密已启用但未提供密码。请设置环境变量 KHAOS_BACKUP_PASS。"
    fi
    # 密码强度警告
    if [[ ${#encrypt_pass} -lt 12 ]]; then
        warn "加密密码长度不足 12 位，建议使用更强的密码。"
    fi
    # 检查 cipher 可用性
    if ! openssl enc -help 2>&1 | grep -q "\-${encryption_cipher}"; then
        warn "OpenSSL 不支持 ${encryption_cipher}，回退至 aes-256-cbc"
        encryption_cipher="aes-256-cbc"
    fi
    # 创建密码临时文件，设置权限遮罩
    umask 077
    pass_file=$(mktemp "${tmp_dir}/.pass.XXXXXX")
    printf '%s' "$encrypt_pass" > "$pass_file"
    chmod 600 "$pass_file"
    # 尽早从环境变量中清除明文密码
    unset encrypt_pass KHAOS_BACKUP_PASS
}

# ----- 预备份钩子 -------------------------------------------------------------
run_hook() {
    local hook_script="$1"
    local stage="$2"
    if [[ -n "$hook_script" ]]; then
        if [[ -x "$hook_script" ]]; then
            info "执行${stage}钩子: $hook_script"
            if ! "$hook_script"; then
                warn "${stage}钩子返回非零状态"
            fi
        else
            warn "${stage}钩子不可执行: $hook_script"
        fi
    fi
}

# ----- 备份 SQLite ------------------------------------------------------------
backup_sqlite() {
    local db_path="$1"
    local base_name="$2"
    local backup_file="$backup_dir/${base_name}.dump"

    [[ ! -f "$db_path" ]] && error "SQLite 数据库不存在: $db_path"
    # 版本检查
    local sqlite_ver
    sqlite_ver=$(sqlite3 --version | awk '{print $1}')
    if ! awk -v ver="$sqlite_ver" 'BEGIN { if (ver < 3.27) exit 1; }'; then
        error "SQLite 版本低于 3.27.0，不支持 VACUUM INTO。"
    fi
    # 先执行 checkpoint
    sqlite3 "$db_path" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    sqlite3 "$db_path" "PRAGMA busy_timeout=5000;"
    if ! sqlite3 "$db_path" "VACUUM INTO '$backup_file'"; then
        log_error "SQLite 备份失败: $db_path"
        return 1
    fi
    echo "$backup_file"
}

# ----- 备份 PostgreSQL --------------------------------------------------------
backup_postgresql() {
    local dbname="$1"
    local base_name="$2"
    local backup_file="$backup_dir/${base_name}.dump"

    export PGPASSWORD="${PGPASSWORD:-}"
    export PGCONNECT_TIMEOUT=10
    if ! pg_isready -h "$pg_host" -p "$pg_port" -U "$pg_user" &>/dev/null; then
        error "PostgreSQL 服务不可达"
    fi
    local extra_args=()
    [[ -n "$exclude_table_data" ]] && extra_args+=("--exclude-table-data=$exclude_table_data")
    # 记录 pg_dump 版本
    pg_dump --version > "${tmp_dir}/pg_version.txt" 2>&1
    if ! pg_dump -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$dbname" \
                --format=custom --file="$backup_file" --verbose --no-password \
                --no-owner --no-acl "${extra_args[@]}" 2>"${tmp_dir}/pg_dump.log"; then
        log_error "pg_dump 失败: $dbname"
        return 1
    fi
    [[ -s "${tmp_dir}/pg_dump.log" ]] && warn "pg_dump 输出: $(tail -n 5 "${tmp_dir}/pg_dump.log")"
    echo "$backup_file"
}

# ----- 后处理 (压缩、加密、校验和、元数据) -----------------------------------
post_process() {
    local file="$1"
    local db_label="$2"   # 用于元数据
    # 压缩
    if [[ "$enable_compress" == "true" ]]; then
        # 避免重复压缩
        if [[ "$file" != *.gz ]]; then
            $compress_cmd -f -"$gzip_level" "$file" || error "压缩失败"
            file="${file}.gz"
        fi
    fi
    # 加密
    if [[ "$enable_encrypt" == "true" ]]; then
        local cipher_opt="-${encryption_cipher}"
        local extra_opt=""
        if openssl enc -help 2>&1 | grep -q "\-pbkdf2"; then
            extra_opt="-pbkdf2"
            if openssl enc -help 2>&1 | grep -q "\-iter"; then
                extra_opt="$extra_opt -iter 10000"
            fi
        fi
        if ! openssl enc $cipher_opt -salt $extra_opt -in "$file" \
                -out "${file}.enc" -pass file:"$pass_file"; then
            error "加密失败"
        fi
        rm -f "$file"
        file="${file}.enc"
    fi
    # 校验和
    local sha_file="${file}.sha256"
    sha256sum "$file" | awk '{print $1}' > "$sha_file"
    chmod 600 "$file" "$sha_file"
    # 生成元数据文件
    local meta_file="${file}.meta"
    cat > "$meta_file" <<EOF
{
    "backup_time": "$(date -Iseconds)",
    "database_type": "$db_type",
    "database_label": "$db_label",
    "instance": "$instance_name",
    "version": "$SCRIPT_VERSION",
    "original_file": "$(basename "$file")",
    "sha256": "$(cat "$sha_file")",
    "size_bytes": $(du -b "$file" | awk '{print $1}')
}
EOF
    chmod 600 "$meta_file"
    echo "$file $sha_file $meta_file"
}

# ----- 远程传输 (使用指数退避和超时) -----------------------------------------
upload_remote() {
    local file="$1"
    local sha_file="$2"
    local meta_file="$3"
    local retries=3
    local delays=(5 10 20)
    local success=false

    for ((i=0; i<retries; i++)); do
        if [[ "$remote_url" == s3://* ]]; then
            local s3_cmd=(aws s3 cp "$file" "$remote_url/$(basename "$file")"
                          --sse AES256 --storage-class STANDARD_IA --only-show-errors
                          --cli-connect-timeout 10 --cli-read-timeout 300)
            [[ -n "$s3_endpoint" ]] && s3_cmd+=(--endpoint-url "$s3_endpoint")
            [[ -n "$s3_region" ]] && s3_cmd+=(--region "$s3_region")
            "${s3_cmd[@]}" && \
            aws s3 cp "$sha_file" "$remote_url/$(basename "$sha_file")" --sse AES256 ${s3_endpoint:+--endpoint-url "$s3_endpoint"} --only-show-errors && \
            aws s3 cp "$meta_file" "$remote_url/$(basename "$meta_file")" --sse AES256 ${s3_endpoint:+--endpoint-url "$s3_endpoint"} --only-show-errors && \
            success=true && break
        elif [[ "$remote_url" == scp://* ]]; then
            local target="${remote_url#scp://}"
            timeout 30 scp -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -P "$ssh_port" \
                   "$file" "$sha_file" "$meta_file" "$target/" && success=true && break
        elif [[ "$remote_url" == rsync://* ]]; then
            timeout 30 rsync -av "$file" "$sha_file" "$meta_file" "$remote_url/" && success=true && break
        fi
        warn "远程传输失败，重试 ($((i+1))/$retries)..."
        sleep "${delays[$i]}"
    done
    if [[ "$success" != "true" ]]; then
        error "远程传输彻底失败，备份中止。"
    fi
    info "远程传输成功。"
}

# ----- 深度恢复测试 (仅 PostgreSQL，恢复到一个临时空库) ---------------------
deep_pg_restore_test() {
    local file="$1"
    local test_db="${PG_DBNAME}_restore_test_${TIMESTAMP}"
    info "深度恢复测试: 恢复到临时数据库 $test_db"
    # 创建临时数据库
    if ! createdb -h "$pg_host" -p "$pg_port" -U "$pg_user" "$test_db" &>/dev/null; then
        warn "无法创建临时数据库 $test_db，跳过深度测试。"
        return
    fi
    if pg_restore -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$test_db" "$file" &>/dev/null; then
        info "深度恢复测试成功。"
        dropdb -h "$pg_host" -p "$pg_port" -U "$pg_user" "$test_db" &>/dev/null
    else
        warn "深度恢复测试失败，但备份文件可能仍可用。"
        dropdb -h "$pg_host" -p "$pg_port" -U "$pg_user" "$test_db" &>/dev/null
    fi
}

# ----- 主流程 -----------------------------------------------------------------
main() {
    local start_time
    start_time=$(date +%s)
    info "KHAOS 备份 v${SCRIPT_VERSION} 启动 (操作者: ${SUDO_USER:-$USER}, 实例: $instance_name)"

    # 参数解析
    local sqlite_paths=()
    local pg_dbnames=()
    local quiet=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --db-type) db_type="$2"; shift 2 ;;
            --db-path) IFS=',' read -ra sqlite_paths <<< "$2"; shift 2 ;;
            --pg-dbname) IFS=',' read -ra pg_dbnames <<< "$2"; shift 2 ;;
            --pg-host) pg_host="$2"; shift 2 ;;
            --pg-port) pg_port="$2"; shift 2 ;;
            --pg-user) pg_user="$2"; shift 2 ;;
            --backup-dir) backup_dir="$2"; shift 2 ;;
            --retention-days) retention_days="$2"; shift 2 ;;
            --compress) enable_compress=true; shift ;;
            --gzip-level) gzip_level="$2"; shift 2 ;;
            --encrypt) enable_encrypt=true; shift ;;
            --encryption-pass) encrypt_pass="$2"; shift 2 ;;
            --remote-url) remote_url="$2"; shift 2 ;;
            --s3-endpoint) s3_endpoint="$2"; shift 2 ;;
            --s3-region) s3_region="$2"; shift 2 ;;
            --ssh-port) ssh_port="$2"; shift 2 ;;
            --delete-local-after-upload) delete_local_after_upload=true; shift ;;
            --backup-type) backup_type="$2"; shift 2 ;;
            --instance-name) instance_name="$2"; shift 2 ;;
            --config-file) config_file="$2"; shift 2 ;;
            --restore-test) restore_test=true; shift ;;
            --deep-restore-test) deep_restore_test=true; shift ;;
            --pre-backup-hook) pre_backup_hook="$2"; shift 2 ;;
            --post-backup-hook) post_backup_hook="$2"; shift 2 ;;
            --dry-run) dry_run=true; shift ;;
            --quiet) quiet=true; shift ;;
            --unlock) force_unlock=true; shift ;;
            --lang) language="$2"; shift 2 ;;
            -h|--help) usage ;;
            *) error "未知选项: $1" ;;
        esac
    done

    if [[ "$quiet" == "true" ]]; then
        exec 1>/dev/null
    fi

    if [[ "$force_unlock" == "true" ]]; then
        rm -f "$lock_file"
        info "锁文件已删除。"
        exit 0
    fi

    # 加载配置
    if [[ -f "$config_file" ]]; then
        # shellcheck source=/dev/null
        source "$config_file"
    fi

    check_deps
    acquire_lock

    mkdir -p "$backup_dir" "$(dirname "$log_file")" "$(dirname "$error_log_file")" "$tmp_dir_base"
    tmp_dir=$(mktemp -d "${tmp_dir_base}/.tmp.XXXXXX")
    [[ -z "$tmp_dir" ]] && error "无法创建临时目录"
    check_disk_space "$backup_dir"
    check_disk_space "$tmp_dir_base"

    prepare_encryption

    if [[ "$dry_run" == "true" ]]; then
        info "模拟运行完成，未产生实际备份。"
        exit 0
    fi

    run_hook "$pre_backup_hook" "备份前"

    # 构建备份列表
    local backups=()
    case "$db_type" in
        sqlite)
            [[ ${#sqlite_paths[@]} -eq 0 ]] && sqlite_paths=("$sqlite_db_path")
            for db in "${sqlite_paths[@]}"; do
                local name="khaos_sqlite_${backup_type}_${TIMESTAMP}_${instance_name}_$(basename "$db" .db)"
                backups+=("$db $name")
            done
            ;;
        postgres)
            [[ ${#pg_dbnames[@]} -eq 0 ]] && pg_dbnames=("$pg_dbname")
            for dbname in "${pg_dbnames[@]}"; do
                local name="khaos_pg_${backup_type}_${TIMESTAMP}_${instance_name}_${dbname}"
                backups+=("$dbname $name")
            done
            ;;
        *) error "不支持的数据库类型: $db_type" ;;
    esac

    local all_files=()
    local failed_dbs=()
    for item in "${backups[@]}"; do
        local target base
        target=$(echo "$item" | awk '{print $1}')
        base=$(echo "$item" | awk '{print $2}')
        info "正在备份: $target"
        local raw_file
        if ! raw_file=$(backup_$db_type "$target" "$base" 2>&1); then
            failed_dbs+=("$target")
            warn "备份失败: $target，继续下一个。"
            continue
        fi
        local processed
        # 传递数据库标签用于元数据
        processed=$(post_process "$raw_file" "$target")
        local final_file sha_file meta_file
        final_file=$(echo "$processed" | awk '{print $1}')
        sha_file=$(echo "$processed" | awk '{print $2}')
        meta_file=$(echo "$processed" | awk '{print $3}')
        all_files+=("$final_file $sha_file $meta_file")

        if [[ -n "$remote_url" ]]; then
            upload_remote "$final_file" "$sha_file" "$meta_file"
            if [[ "$delete_local_after_upload" == "true" ]]; then
                rm -f "$final_file" "$sha_file" "$meta_file"
                info "已删除本地副本: $final_file"
            fi
        fi
    done

    if [[ ${#failed_dbs[@]} -gt 0 ]]; then
        warn "以下数据库备份失败: ${failed_dbs[*]}"
        log_error "备份失败数据库: ${failed_dbs[*]}"
    fi

    # 恢复测试
    if [[ "$restore_test" == "true" ]]; then
        for pair in "${all_files[@]}"; do
            local f=$(echo "$pair" | awk '{print $1}')
            if [[ -f "$f" ]]; then
                info "恢复测试: $f"
                if [[ "$db_type" == "sqlite" ]]; then
                    if ! sqlite3 "$f" "SELECT count(*) FROM sqlite_master;" &>/dev/null; then
                        error "恢复测试失败: $f"
                    fi
                    # gzip 测试
                    if [[ "$f" == *.gz ]]; then
                        if ! gzip -t "$f"; then
                            error "压缩文件损坏: $f"
                        fi
                    fi
                else
                    if ! pg_restore -l "$f" &>/dev/null; then
                        error "pg_restore 测试失败: $f"
                    fi
                fi
            fi
        done
        info "恢复测试完成。"
    fi

    # 深度恢复测试（仅 PG）
    if [[ "$deep_restore_test" == "true" && "$db_type" == "postgres" ]]; then
        for pair in "${all_files[@]}"; do
            local f=$(echo "$pair" | awk '{print $1}')
            if [[ -f "$f" ]]; then
                deep_pg_restore_test "$f"
            fi
        done
    fi

    # 清理旧备份 (低 I/O 优先级)
    ionice -c 2 -n 7 find "$backup_dir" -name "khaos_${db_type}_*" -type f -mtime "+${retention_days}" -delete || true

    run_hook "$post_backup_hook" "备份后"

    # 通知与指标
    local end_time=$(date +%s)
    local duration=$((end_time - start_time))
    local msg="KHAOS 备份完成。耗时 ${duration}s，成功 ${#all_files[@]} 个，失败 ${#failed_dbs[@]} 个。"
    if [[ -n "$notify_hook" ]]; then
        curl -s -X POST "$notify_hook" -H "Content-Type: application/json" \
             -d "{\"text\":\"$msg\"}" --max-redirs 0 || warn "通知发送失败"
    fi
    if [[ -n "$heartbeat_url" ]]; then
        local status="success"
        [[ ${#failed_dbs[@]} -gt 0 ]] && status="partial"
        curl -s "$heartbeat_url?status=$status" || true
    fi
    if [[ -n "$metrics_push_url" ]]; then
        local now_ts=$(date +%s)
        curl -s -X POST "$metrics_push_url" -d "backup_duration_seconds{} $duration $now_ts" || true
        for pair in "${all_files[@]}"; do
            local f=$(echo "$pair" | awk '{print $1}')
            if [[ -f "$f" ]]; then
                local size
                size=$(du -b "$f" | awk '{print $1}')
                curl -s -X POST "$metrics_push_url" -d "backup_size_bytes{} $size $now_ts" || true
            fi
        done
    fi

    info "备份成功结束。"
    [[ ${#failed_dbs[@]} -gt 0 ]] && exit 1
    exit 0
}

main "$@"
