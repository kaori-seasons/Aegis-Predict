# 页面数据字段表

## 1. 首页：全球库存池看板

| 模块 | 字段 | 说明 |
| --- | --- | --- |
| 核心指标 | total_inventory | 全球总库存件数 |
| 核心指标 | risk_sku_count | 缺货风险 SKU 数 |
| 核心指标 | demand_30d | 预计未来 30 天需求 |
| 核心指标 | fill_rate | 平均订单满足率 |
| 核心指标 | turnover_days | 库存周转天数 |
| 核心指标 | today_replenishment_units | 今日建议补货量 |
| 仓库地图 | warehouse_id, warehouse_name, latitude, longitude | 仓库地理位置 |
| 仓库地图 | status, risk_score, current_inventory, planned_inbound | 仓库状态和补货量 |
| 风险排行 | sku, warehouse_id, current_inventory, days_of_supply, risk_level | 风险 SKU 排名 |
| 实时订单流 | simulated_time, country, sku, quantity, warehouse_id, stockout_units | 实时订单与缺货事件 |

## 2. 预测页：AI 销量预测

| 模块 | 字段 | 说明 |
| --- | --- | --- |
| 筛选器 | sku, country, warehouse_id, horizon | SKU、国家、仓库、预测周期 |
| 预测曲线 | date_index, history_units, p50_units, p80_units, p95_units, safety_stock | 历史销量和未来预测区间 |
| 模型摘要 | model_name, training_samples, feature_sources, signal_count | 模型和特征来源 |
| 影响因素 | factor, impact, direction | 影响预测上涨或下降的原因 |
| 解释文本 | explanation | 可解释预测说明 |

## 3. 补货页：智能补货建议

| 字段 | 说明 |
| --- | --- |
| sku, warehouse_id | 商品和目标仓 |
| current_inventory | 当前可用库存 |
| inbound_units | 在途库存 |
| forecast_30d | 未来 30 天预测需求 |
| safety_stock | 安全库存 |
| recommended_units | 建议补货量 |
| transport_mode | 海运、空运或跨仓调拨 |
| priority | 高、中、低 |
| formula | 补货计算过程 |

## 4. 分仓页：库存分配方案

| 字段 | 说明 |
| --- | --- |
| sku | 待分配 SKU |
| warehouse_id | 目标海外仓 |
| forecast_demand | 预测需求 |
| current_inventory | 当前库存 |
| shortage_gap | 缺口 |
| allocated_units | 建议分配量 |
| source | 深圳中心仓或跨仓来源 |
| transport_mode | 运输方式 |
| reason | 分配原因 |

## 5. 仿真评估页：方案对比

| 字段 | 说明 |
| --- | --- |
| baseline_stockout_rate, intelligent_stockout_rate | 传统和智能方案缺货率 |
| baseline_fill_rate, intelligent_fill_rate | 传统和智能方案订单满足率 |
| baseline_inventory_cost, intelligent_inventory_cost | 库存成本 |
| baseline_delivery_days, intelligent_delivery_days | 平均配送时效 |
| turnover_days | 库存周转天数 |
| simulation_clock, tick, scenario | 实时仿真状态 |
| in_transit_shipments | 在途补货任务 |

## 建议接口

| 接口 | 作用 |
| --- | --- |
| `/api/dashboard` | 首页核心指标、仓库地图、风险排行、实时订单 |
| `/api/warehouses` | 海外仓库存状态 |
| `/api/orders/realtime` | 实时订单流 |
| `/api/forecast?sku=xxx&warehouse_id=xxx` | 销量预测数据 |
| `/api/replenishment` | 补货建议 |
| `/api/allocation` | 分仓方案 |
| `/api/simulation/compare` | 传统方案和智能方案对比 |

