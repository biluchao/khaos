-- =============================================================================
-- KHAOS 数据库初始迁移脚本 v2.0 (华尔街机构级强化)
-- 功能: 创建核心业务表，包含金融级精度、约束、索引及运维特性。
-- 兼容: SQLite / PostgreSQL (通过 Alembic 适配，已注释差异)
-- 注意: 金融数据统一使用 DECIMAL 或 NUMERIC，避免 REAL 精度丢失。
-- =============================================================================

BEGIN TRANSACTION;

-- ---------------------------------------------------------------------------
-- 1. 交易品种信息
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    min_qty DECIMAL(18,8) NOT NULL DEFAULT 0.0 CHECK(min_qty >= 0),
    step_size DECIMAL(18,8) NOT NULL DEFAULT 0.0 CHECK(step_size >= 0),
    tick_size DECIMAL(18,8) NOT NULL DEFAULT 0.0 CHECK(tick_size >= 0),
    min_notional DECIMAL(18,8) NOT NULL DEFAULT 0.0 CHECK(min_notional >= 0),
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- 修复: 增加交易所原始精度字段
    price_precision INTEGER DEFAULT 2,
    qty_precision INTEGER DEFAULT 3
);

-- ---------------------------------------------------------------------------
-- 2. K线数据 (行情核心)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS klines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL CHECK(interval IN ('1m','3m','5m','15m','30m','1h','2h','4h','6h','8h','12h','1d','3d','1w')),
    open_time BIGINT NOT NULL,
    close_time BIGINT NOT NULL,
    open DECIMAL(18,8) NOT NULL,
    high DECIMAL(18,8) NOT NULL,
    low DECIMAL(18,8) NOT NULL,
    close DECIMAL(18,8) NOT NULL,
    volume DECIMAL(18,8) NOT NULL CHECK(volume >= 0),
    quote_volume DECIMAL(18,8) DEFAULT 0.0,
    trades INTEGER DEFAULT 0 CHECK(trades >= 0),
    is_synthetic BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, interval, open_time)
);
-- 修复: 增加覆盖索引加速常见查询
CREATE INDEX IF NOT EXISTS idx_klines_symbol_interval_time 
    ON klines(symbol, interval, open_time DESC);
CREATE INDEX IF NOT EXISTS idx_klines_close_time ON klines(close_time);

-- ---------------------------------------------------------------------------
-- 3. 订单记录
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    order_type TEXT NOT NULL CHECK(order_type IN ('market','limit','stop_market','stop_limit','oco')),
    quantity DECIMAL(18,8) NOT NULL CHECK(quantity > 0),
    price DECIMAL(18,8),
    stop_price DECIMAL(18,8),
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','partially_filled','filled','canceled','rejected','expired')),
    executed_qty DECIMAL(18,8) DEFAULT 0.0 CHECK(executed_qty >= 0),
    avg_fill_price DECIMAL(18,8),
    commission DECIMAL(18,8) DEFAULT 0.0,
    commission_asset TEXT,
    strategy_tag TEXT,
    reduce_only BOOLEAN DEFAULT FALSE,
    post_only BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- 修复: 约束：限价单必须提供价格，市价单则不需要
    CHECK( (order_type IN ('limit','stop_limit','oco') AND price IS NOT NULL) OR 
           (order_type IN ('market','stop_market') AND price IS NULL) )
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_time ON orders(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- ---------------------------------------------------------------------------
-- 4. 成交明细
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    fill_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    price DECIMAL(18,8) NOT NULL CHECK(price > 0),
    qty DECIMAL(18,8) NOT NULL CHECK(qty > 0),
    commission DECIMAL(18,8) DEFAULT 0.0,
    commission_asset TEXT,
    fill_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(fill_time DESC);

-- ---------------------------------------------------------------------------
-- 5. 当前持仓
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long','short')),
    quantity DECIMAL(18,8) NOT NULL CHECK(quantity > 0),
    avg_entry_price DECIMAL(18,8) NOT NULL CHECK(avg_entry_price > 0),
    unrealized_pnl DECIMAL(18,8) DEFAULT 0.0,
    realized_pnl DECIMAL(18,8) DEFAULT 0.0,
    stop_loss_price DECIMAL(18,8),
    take_profit_price DECIMAL(18,8),
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, direction)
);

-- ---------------------------------------------------------------------------
-- 6. 持仓快照 (绩效归因)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long','short')),
    quantity DECIMAL(18,8) NOT NULL,
    avg_entry_price DECIMAL(18,8) NOT NULL,
    unrealized_pnl DECIMAL(18,8) NOT NULL,
    mark_price DECIMAL(18,8) NOT NULL,
    snapshot_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pos_snap_time ON position_snapshots(snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_pos_snap_symbol ON position_snapshots(symbol, snapshot_time DESC);

-- ---------------------------------------------------------------------------
-- 7. 信号日志 (精简版)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT NOT NULL,
    direction TEXT CHECK(direction IN ('LONG','SHORT','NONE')),
    action TEXT NOT NULL CHECK(action IN ('OPEN','CLOSE','REDUCE','ADD','NO_ACTION')),
    price DECIMAL(18,8),
    probability DECIMAL(5,4) CHECK(probability >= 0 AND probability <= 1),
    module TEXT,
    escape_score DECIMAL(5,4),
    resonance_strength DECIMAL(5,4),
    reject_reason TEXT,
    strategy_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_module ON signals(module, timestamp DESC);

-- ---------------------------------------------------------------------------
-- 8. 参数版本
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS param_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- ---------------------------------------------------------------------------
-- 9. 进化任务
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evolution_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL CHECK(task_type IN ('bapo','rl','meta','gan','online')),
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed','approved','rolled_back')),
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMP,
    result_json TEXT,
    max_drawdown DECIMAL(5,4),
    sharpe DECIMAL(6,2),
    is_applied BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_evolution_status ON evolution_runs(status);

-- ---------------------------------------------------------------------------
-- 10. 系统检查点 (恢复)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    state_json TEXT NOT NULL,
    kline_time BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(component, kline_time)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_component_time 
    ON checkpoints(component, kline_time DESC);

-- ---------------------------------------------------------------------------
-- 初始默认参数版本
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO param_versions (version, params_json, description) 
VALUES ('v2.0.0', '{}', 'Institutional grade initial parameters');

-- PostgreSQL 特定建议 (注释)
-- ALTER TABLE orders ALTER COLUMN created_at SET DEFAULT now();
-- CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

COMMIT;
