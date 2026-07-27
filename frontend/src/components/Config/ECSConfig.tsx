// =============================================================================
// KHAOS 确定性增强引擎配置组件 (机构级 v2.0)
// 功能: 调整四大维度权重，实时预览，保存到后端。具备完善的错误处理、
//       异步保护、无障碍支持及性能优化。
// =============================================================================

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  Card,
  Switch,
  Slider,
  Button,
  message,
  Typography,
  Space,
  Divider,
  Row,
  Col,
  Statistic,
  Tooltip,
} from 'antd';
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons';
import { getECSConfig, updateECSConfig } from '../../utils/api';

const { Title, Text } = Typography;

// ---------------------------------------------------------------------------
// 类型定义
// ---------------------------------------------------------------------------
type DimensionKey = 'time_and_stats' | 'cross_market' | 'microstructure' | 'adaptive';

interface DimensionWeights {
  time_and_stats: number;
  cross_market: number;
  microstructure: number;
  adaptive: number;
}

interface ECSConfigResponse {
  enabled: boolean;
  dimension_weights: DimensionWeights;
}

const DIMENSION_LABELS: Record<DimensionKey, string> = {
  time_and_stats: '时间与统计',
  cross_market: '跨市场与宏观',
  microstructure: '微观结构深度',
  adaptive: '自适应与进化',
};

const DIMENSION_TOOLTIPS: Record<DimensionKey, string> = {
  time_and_stats: '时段质量、路径平滑度、分时成交量分布',
  cross_market: '关联品种协同、稳定币资金流、恐惧与贪婪',
  microstructure: '订单簿恢复力、大单方向、持仓爆仓、BPI/TakerFlow',
  adaptive: '模块可信度在线学习、对手盘冲击、凯利仓位',
};

const MAX_PERCENT = 100;
const MIN_PERCENT = 0;
const DEFAULT_PERCENT_WEIGHTS: DimensionWeights = {
  time_and_stats: 25,
  cross_market: 10,
  microstructure: 40,
  adaptive: 25,
};

// 网络请求超时时间
const REQUEST_TIMEOUT_MS = 8000;
// 保存防抖延迟
const SAVE_DEBOUNCE_MS = 600;

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------
function normalizeWeights(weights: DimensionWeights): DimensionWeights {
  const sum = Object.values(weights).reduce((s, v) => s + v, 0);
  if (sum === 0) return { ...DEFAULT_PERCENT_WEIGHTS };
  const normalized: any = {};
  for (const key of Object.keys(weights) as DimensionKey[]) {
    normalized[key] = Math.round((weights[key] / sum) * 100);
  }
  return normalized as DimensionWeights;
}

function decimalToPercent(weights: DimensionWeights): DimensionWeights {
  const percent: any = {};
  for (const key of Object.keys(weights) as DimensionKey[]) {
    percent[key] = Math.round(weights[key] * 100);
  }
  return normalizeWeights(percent); // 确保总和为100
}

function percentToDecimal(weights: DimensionWeights): DimensionWeights {
  const decimal: any = {};
  for (const key of Object.keys(weights) as DimensionKey[]) {
    decimal[key] = weights[key] / 100;
  }
  return decimal as DimensionWeights;
}

async function fetchWithTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timeoutId: NodeJS.Timeout;
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(() => reject(new Error('Request timeout')), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timeoutId));
}

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------
const ECSConfig: React.FC = () => {
  const [enabled, setEnabled] = useState<boolean>(true);
  const [weights, setWeights] = useState<DimensionWeights>(DEFAULT_PERCENT_WEIGHTS);
  const [originalWeights, setOriginalWeights] = useState<DimensionWeights | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // 用于取消异步操作
  const mountedRef = useRef(true);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, []);

  // 加载配置
  const fetchConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchWithTimeout(getECSConfig(), REQUEST_TIMEOUT_MS);
      if (!mountedRef.current) return;

      // 类型守卫
      if (!response || typeof response.enabled !== 'boolean' || !response.dimension_weights) {
        throw new Error('Invalid response format');
      }

      setEnabled(response.enabled);
      const percent = decimalToPercent(response.dimension_weights);
      setWeights(percent);
      setOriginalWeights(percent);
    } catch (err: any) {
      const msg = err?.message === 'Request timeout' ? '加载超时' : '加载ECS配置失败';
      if (mountedRef.current) {
        setError(msg);
        message.error(msg);
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // 保存配置（带防抖）
  const performSave = useCallback(async (data: { enabled: boolean; dimension_weights: DimensionWeights }) => {
    setSaving(true);
    setError(null);
    try {
      await fetchWithTimeout(updateECSConfig(data), REQUEST_TIMEOUT_MS);
      if (mountedRef.current) {
        message.success('确定性增强配置已更新');
        setOriginalWeights({ ...weights });
      }
    } catch (err: any) {
      const msg = err?.message === 'Request timeout' ? '保存超时' : '保存配置失败';
      if (mountedRef.current) {
        setError(msg);
        message.error(msg);
      }
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }, [weights]);

  const handleSave = useCallback(() => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      const decimal = percentToDecimal(weights);
      performSave({ enabled, dimension_weights: decimal });
    }, SAVE_DEBOUNCE_MS);
  }, [enabled, weights, performSave]);

  // 滑块变化
  const handleSliderChange = useCallback((dim: DimensionKey, value: number) => {
    setWeights(prev => {
      // 确保值是有效数字
      if (typeof value !== 'number' || isNaN(value)) return prev;
      const clamped = Math.min(MAX_PERCENT, Math.max(MIN_PERCENT, Math.round(value)));
      return { ...prev, [dim]: clamped };
    });
  }, []);

  // 重置
  const handleReset = useCallback(() => {
    if (originalWeights) {
      setWeights({ ...originalWeights });
    }
  }, [originalWeights]);

  // 计算总和
  const totalPercent = useMemo(() => {
    return Object.values(weights).reduce((sum, v) => sum + (isNaN(v) ? 0 : v), 0);
  }, [weights]);

  // 是否已修改
  const isModified = useMemo(() => {
    if (!originalWeights) return false;
    return Object.keys(weights).some(
      dim => weights[dim as DimensionKey] !== originalWeights[dim as DimensionKey]
    );
  }, [weights, originalWeights]);

  // 错误状态显示
  const errorAlert = error ? (
    <Text type="danger" style={{ marginBottom: 8, display: 'block' }}>
      {error}
    </Text>
  ) : null;

  return (
    <Card
      title={
        <Space>
          <Title level={4} style={{ margin: 0 }}>
            确定性增强引擎 (ECS)
          </Title>
          <Switch
            checked={enabled}
            onChange={setEnabled}
            checkedChildren="启用"
            unCheckedChildren="禁用"
            aria-label="切换ECS引擎状态"
          />
        </Space>
      }
      style={{ margin: 24, borderRadius: 8 }}
      extra={
        <Space>
          <Tooltip title="重置到上次保存的值">
            <Button
              icon={<ReloadOutlined />}
              onClick={handleReset}
              disabled={!isModified || loading}
              aria-label="重置权重"
            />
          </Tooltip>
          <Tooltip title="保存配置">
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={handleSave}
              loading={saving}
              disabled={!isModified}
              aria-label="保存权重配置"
            >
              保存
            </Button>
          </Tooltip>
        </Space>
      }
      loading={loading}
    >
      <Text type="secondary">
        拖动滑块调整各维度在入场确定性评分中的占比。总和无需刚好 100%，系统会自动归一化。
      </Text>

      {errorAlert}

      <Divider style={{ margin: '16px 0' }} />

      <Row justify="center" style={{ marginBottom: 16 }}>
        <Statistic
          title="当前权重总和"
          value={totalPercent}
          suffix="%"
          valueStyle={{
            color: totalPercent === 100 ? '#52c41a' : '#faad14',
            fontSize: 20,
          }}
        />
      </Row>

      {Object.keys(weights).map(dim => {
        const dimKey = dim as DimensionKey;
        const value = weights[dimKey];
        return (
          <Row key={dimKey} align="middle" gutter={16} style={{ marginBottom: 16 }}>
            <Col span={24}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                <Text strong>{DIMENSION_LABELS[dimKey]}</Text>
                <Tooltip title={DIMENSION_TOOLTIPS[dimKey]}>
                  <Text type="secondary" style={{ fontSize: 12, cursor: 'help' }}>
                    详情
                  </Text>
                </Tooltip>
              </div>
              <Slider
                min={MIN_PERCENT}
                max={MAX_PERCENT}
                value={value}
                onChange={(val) => handleSliderChange(dimKey, val)}
                marks={{ 0: '0%', 25: '25%', 50: '50%', 75: '75%', 100: '100%' }}
                tipFormatter={(val) => `${val}%`}
                disabled={!enabled}
                aria-label={`${DIMENSION_LABELS[dimKey]} 权重`}
              />
            </Col>
          </Row>
        );
      })}
    </Card>
  );
};

export default ECSConfig;
