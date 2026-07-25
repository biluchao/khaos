# =============================================================================
# KHAOS 量化交易系统 - Makefile (华尔街机构级)
# 适用于 2000 美金至万亿美金账户，支持 4K 中文界面。
# 兼容 Linux、macOS、Windows (WSL)
# =============================================================================

# 项目根目录
PROJECT_ROOT := $(shell pwd)
# Python 虚拟环境
VENV := $(PROJECT_ROOT)/.venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
# 前端目录
FRONTEND_DIR := $(PROJECT_ROOT)/frontend
NPM := npm
NPX := npx
# Docker 镜像
IMAGE_NAME ?= khaos
IMAGE_TAG ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "latest")
# 服务名称 (systemd)
SERVICE_NAME ?= khaos
# 生产环境配置
CONFIG_FILE ?= config/default.yaml

# 强制使用 bash 作为 shell 以获得跨平台一致性
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
# 出错后删除目标文件，避免损坏的产物
.DELETE_ON_ERROR:
# 所有目标都是伪目标，不生成同名文件
.PHONY: help install install-python install-frontend build run run-prod run-worker test test-coverage lint lint-python lint-frontend lint-fix format format-python format-frontend clean db-migrate db-downgrade db-create-migration check deploy setup docker-build docker-up docker-down restart status logs shell backup restore security audit outdated freeze monitor

help: ## 显示帮助信息
	@echo "KHAOS 量化交易系统 - 可用命令:"
	@echo ""
	@echo "  make install           安装所有依赖 (Python + 前端)"
	@echo "  make install-python    仅安装 Python 依赖"
	@echo "  make install-frontend  仅安装前端依赖"
	@echo "  make build             构建前端资源"
	@echo "  make run               启动开发服务器 (API)"
	@echo "  make run-prod          生产模式启动 (gunicorn + 静态文件)"
	@echo "  make run-worker        启动后台任务处理器 (Celery)"
	@echo "  make test              运行全部测试"
	@echo "  make test-coverage     运行测试并生成覆盖率报告"
	@echo "  make lint              执行代码静态检查 (Python + 前端)"
	@echo "  make lint-fix          自动修复部分 lint 问题"
	@echo "  make format            使用 black 和 prettier 格式化所有代码"
	@echo "  make clean             清理构建产物和缓存"
	@echo "  make db-migrate        执行数据库迁移 (升级)"
	@echo "  make db-downgrade      回滚数据库到上一个版本 (需确认)"
	@echo "  make db-create-migration 创建新的迁移文件 (需指定 message)"
	@echo "  make check             运行所有检查 (lint + test + build)"
	@echo "  make deploy            部署到生产环境 (需 sudo)"
	@echo "  make setup             初始化开发环境 (venv, pre-commit)"
	@echo "  make docker-build      构建 Docker 镜像"
	@echo "  make docker-up         使用 Docker Compose 启动全部服务"
	@echo "  make docker-down       停止 Docker Compose 服务"
	@echo "  make restart           重启 systemd 服务"
	@echo "  make status            查看 systemd 服务状态"
	@echo "  make logs              查看服务日志"
	@echo "  make shell             进入 Python 虚拟环境交互模式"
	@echo "  make backup            备份数据库与配置文件"
	@echo "  make restore           从最新备份恢复"
	@echo "  make security          运行安全扫描 (bandit + npm audit)"
	@echo "  make audit             查看依赖漏洞"
	@echo "  make outdated          检查过时的依赖"
	@echo "  make freeze            导出当前依赖到 requirements.txt"
	@echo "  make monitor           启动系统监控面板 (htop + prometheus)"
	@echo ""

# ---------- 安装 ----------
install: install-python install-frontend

install-python: $(VENV)
	@echo "安装 Python 依赖..."
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements.txt --no-cache-dir
	@echo "Python 依赖安装完成。"

$(VENV):
	@echo "创建 Python 虚拟环境..."
	python3 -m venv $(VENV)
	@echo "虚拟环境创建于 $(VENV)"

install-frontend:
	@if [ ! -f "$(FRONTEND_DIR)/package.json" ]; then echo "错误: 未找到 frontend/package.json"; exit 1; fi
	cd $(FRONTEND_DIR) && $(NPM) ci --prefer-offline --no-audit

# ---------- 构建 ----------
build: install-frontend
	@echo "构建前端..."
	cd $(FRONTEND_DIR) && $(NPM) run build
	@echo "前端构建完成。"

# ---------- 运行 ----------
run: ensure-venv
	@echo "启动开发服务器 (API)..."
	PYTHONUNBUFFERED=1 LC_ALL=zh_CN.UTF-8 $(PYTHON) main.py --config $(CONFIG_FILE)

run-prod: ensure-venv
	@echo "启动生产服务器 (Gunicorn)..."
	@# 验证 gunicorn 是否安装
	@if ! $(PYTHON) -c "import gunicorn" 2>/dev/null; then echo "错误: 缺少 gunicorn，请运行 'pip install gunicorn'"; exit 1; fi
	PYTHONUNBUFFERED=1 LC_ALL=zh_CN.UTF-8 $(VENV)/bin/gunicorn api.app:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --access-logfile /var/log/khaos/access.log --error-logfile /var/log/khaos/error.log

run-worker: ensure-venv
	@echo "启动后台任务处理器 (Celery)..."
	celery -A services.tasks worker --loglevel=info

# ---------- 测试 ----------
test: ensure-venv
	@echo "运行所有测试..."
	$(PYTHON) -m pytest tests/ -v --tb=short

test-coverage: ensure-venv
	@echo "运行测试并生成覆盖率报告..."
	$(PYTHON) -m pytest tests/ --cov=core --cov=adapters --cov=services --cov=api --cov-report=html --cov-report=term --cov-fail-under=80

# ---------- 代码质量 ----------
lint: lint-python lint-frontend

lint-python: ensure-venv
	@echo "运行 Python flake8..."
	$(PYTHON) -m flake8 core adapters services api --count --statistics

lint-frontend:
	cd $(FRONTEND_DIR) && $(NPM) run lint

lint-fix: ensure-venv
	@echo "自动修复 Python 代码..."
	$(PYTHON) -m black core adapters services api
	@echo "自动修复前端代码..."
	cd $(FRONTEND_DIR) && $(NPM) run lint:fix

format: format-python format-frontend

format-python: ensure-venv
	$(PYTHON) -m black core adapters services api

format-frontend:
	cd $(FRONTEND_DIR) && $(NPM) run format

# ---------- 清理 ----------
clean:
	@echo "清理前端构建..."
	rm -rf $(FRONTEND_DIR)/dist $(FRONTEND_DIR)/build
	@echo "清理 Python 缓存..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "清理测试缓存..."
	rm -rf .pytest_cache htmlcov .coverage
	@echo "清理完成。"

# ---------- 数据库 ----------
db-migrate: ensure-venv
	@echo "执行数据库迁移 (升级)..."
	$(PYTHON) -m alembic upgrade head

db-downgrade: ensure-venv
	@read -p "确认回滚数据库到上一个版本? [y/N] " ans; if [ "$$ans" != "y" ]; then echo "取消。"; exit 1; fi
	$(PYTHON) -m alembic downgrade -1

db-create-migration: ensure-venv
	@if [ -z "$(msg)" ]; then echo "请指定迁移消息: make db-create-migration msg='add user table'"; exit 1; fi
	$(PYTHON) -m alembic revision --autogenerate -m "$(msg)"

# ---------- 综合检查 ----------
check: lint test build
	@echo "所有检查通过。"

# ---------- 部署 ----------
deploy: ensure-venv
	@if [ ! -f scripts/deploy_prod.sh ]; then echo "错误: 部署脚本不存在"; exit 1; fi
	@echo "开始生产环境部署..."
	bash scripts/deploy_prod.sh

# ---------- 开发环境初始化 ----------
setup: $(VENV) install
	@echo "安装 pre-commit 钩子..."
	$(VENV)/bin/pre-commit install
	@echo "初始化完成。请编辑 .env 文件并启动 'make run'。"

# ---------- Docker ----------
docker-build:
	@echo "构建 Docker 镜像: $(IMAGE_NAME):$(IMAGE_TAG)..."
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

# ---------- 服务管理 ----------
restart:
	@echo "重启 KHAOS 服务..."
	-sudo systemctl restart $(SERVICE_NAME)

status:
	@sudo systemctl status $(SERVICE_NAME) --no-pager || true

logs:
	@sudo journalctl -u $(SERVICE_NAME) -f

# ---------- 运维工具 ----------
shell: ensure-venv
	$(PYTHON)

backup: ensure-venv
	@echo "备份数据库与配置文件..."
	mkdir -p backups
	tar czf backups/khaos-backup-$(shell date +%Y%m%d_%H%M%S).tar.gz config/ data/
	@echo "备份完成。"

restore:
	@ls -1t backups/khaos-backup-*.tar.gz | head -1 | read file; \
	if [ -z "$$file" ]; then echo "没有找到备份文件。"; exit 1; fi; \
	read -p "确认从 $$file 恢复? [y/N] " ans; \
	if [ "$$ans" != "y" ]; then echo "取消。"; exit 1; fi; \
	tar xzf "$$file" -C .

security: ensure-venv
	@echo "运行 Python 安全扫描 (bandit)..."
	$(PYTHON) -m bandit -r core adapters services api
	@echo "运行前端安全审计..."
	cd $(FRONTEND_DIR) && $(NPM) audit

audit:
	$(PIP) check
	cd $(FRONTEND_DIR) && $(NPM) audit --audit-level=high

outdated:
	$(PIP) list --outdated
	cd $(FRONTEND_DIR) && $(NPM) outdated

freeze: ensure-venv
	$(PIP) freeze > requirements.txt
	@echo "依赖已冻结到 requirements.txt"

monitor:
	@echo "启动监控面板..."
	# 假设使用 tmux + htop + prometheus，此处仅示例
	htop

# ---------- 内部辅助目标 ----------
ensure-venv:
	@if [ ! -f "$(PYTHON)" ]; then echo "错误: 虚拟环境未找到，请运行 'make setup' 或 'make install'"; exit 1; fi
