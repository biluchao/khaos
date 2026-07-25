# =============================================================================
# KHAOS 量化交易系统 Dockerfile v2 (华尔街机构级终版)
# 适用于 100 美金至万亿美金账户，支持 4K 中文界面。
# 构建命令: docker build --build-arg KHAOS_VERSION=$(git describe) .
# =============================================================================

ARG KHAOS_VERSION=unknown
ARG PYTHON_VERSION=3.11.9
ARG NODE_VERSION=18.20.4

# ---------- 阶段 1: 前端构建 ----------
FROM node:${NODE_VERSION}-alpine AS frontend-builder
ARG KHAOS_VERSION
ARG NODE_VERSION

ENV NODE_ENV=production

# 安装前端构建所需的最小工具（如 node-gyp 需要 python 和 make）
RUN apk add --no-cache python3 make g++

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --audit=false --prefer-offline --ignore-scripts

COPY frontend/ ./
RUN npm run build && npm cache clean --force

# ---------- 阶段 2: 后端依赖构建 ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS backend-builder
ARG PYTHON_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=0

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
# 启用哈希验证，确保供应链安全
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --timeout 120 --retries 3 --require-hashes -r requirements.txt

# 快速验证关键依赖
RUN python -c "import fastapi; print('FastAPI OK')"

# 卸载 pip 和 setuptools 以减少攻击面
RUN pip uninstall -y pip setuptools

# ---------- 阶段 3: 最终镜像 ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG KHAOS_VERSION
ARG PYTHON_VERSION

LABEL maintainer="KHAOS Engineering" \
      version="${KHAOS_VERSION}" \
      description="KHAOS 量化交易系统 - 机构级多策略交易引擎"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=1 \
    PYTHONFAULTHANDLER=0 \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    TZ=Asia/Shanghai

# 安装运行时必要工具并固定 tini 版本
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini=0.19.0-1 \
    tzdata \
    fonts-wqy-zenhei \
    && echo "Asia/Shanghai" > /etc/timezone \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/* \
    # 清理不必要的时区文件
    && find /usr/share/zoneinfo -mindepth 1 -maxdepth 1 ! -name 'Asia' ! -name 'Etc' -exec rm -rf {} + \
    && find /usr/share/zoneinfo/Asia -mindepth 1 ! -name 'Shanghai' -exec rm -rf {} +

# 创建非 root 用户，无登录 shell
RUN groupadd --gid 1000 khaos \
    && useradd --uid 1000 --gid 1000 --no-log-init --no-create-home --shell /usr/sbin/nologin khaos

WORKDIR /app

# 从构建阶段复制虚拟环境（已卸载 pip）
COPY --from=backend-builder --chown=khaos:khaos /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制后端代码
COPY --chown=khaos:khaos main.py .
COPY --chown=khaos:khaos config/ ./config/
COPY --chown=khaos:khaos core/ ./core/
COPY --chown=khaos:khaos adapters/ ./adapters/
COPY --chown=khaos:khaos services/ ./services/
COPY --chown=khaos:khaos api/ ./api/
COPY --chown=khaos:khaos evolution/ ./evolution/
COPY --chown=khaos:khaos scripts/ ./scripts/

# 复制前端构建产物
COPY --from=frontend-builder --chown=khaos:khaos /app/frontend/dist ./frontend/dist

# 创建运行时目录并写入版本文件
RUN mkdir -p /app/data /app/logs /app/backups \
    && echo "${KHAOS_VERSION}" > /app/version.txt \
    && chown -R khaos:khaos /app

USER khaos

EXPOSE 8000/tcp

# 使用 Python 内置工具进行健康检查，减少依赖
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 使用 tini 作为 init 进程，正确处理 SIGTERM
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["python", "main.py", "--config", "config/default.yaml"]
