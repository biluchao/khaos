-- =============================================================================
-- KHAOS 数据库迁移: 002_remove_audit_tables.sql
-- 说明: 根据系统最新决策，移除所有合规审计相关的数据库表。
-- 执行: 此迁移为不可逆操作，执行后将永久删除审计数据。
-- =============================================================================

-- 删除审计事件表
DROP TABLE IF EXISTS audit_events;

-- 删除决策快照表
DROP TABLE IF EXISTS decision_snapshots;

-- 删除意图日志表（被否决信号）
DROP TABLE IF EXISTS intent_logs;

-- 删除链式签名验证记录
DROP TABLE IF EXISTS audit_chain;

-- 删除参数变更审批记录
DROP TABLE IF EXISTS parameter_approvals;

-- 可在此添加其他需要清理的审计相关表...
