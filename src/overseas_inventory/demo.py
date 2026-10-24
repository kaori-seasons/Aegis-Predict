from __future__ import annotations

import argparse
import json
from pathlib import Path

from .allocation import allocate_from_global_pool
from .dashboard import render_dashboard_html
from .models import dataclass_to_dict, load_network
from .replenishment import build_replenishment_recommendations
from .simulation import compare_strategies


WAREHOUSE_DISPLAY_NAMES = {
    "US-LAX": "美国洛杉矶仓",
    "DE-FRA": "德国法兰克福仓",
    "UK-BHX": "英国伯明翰仓",
    "AU-SYD": "澳洲悉尼仓",
}


def build_plan(data_path: Path, output_dir: Path, horizon_days: int) -> dict:
    network = load_network(data_path)
    recommendations = build_replenishment_recommendations(network, horizon_days=horizon_days)
    allocations = allocate_from_global_pool(network, recommendations)
    comparison = compare_strategies(network, days=90, planning_interval_days=7, horizon_days=horizon_days)

    plan = {
        "project": "面向 3C 配件跨境电商的海外仓智能补货与分仓系统",
        "strategy": "围绕 USB-C 快充线、手机支架、耳机保护壳、平板保护壳、USB-C 拓展坞和笔记本支架，建立 SKU × 国家 × 海外仓 × 日期的预测、补货、分仓和仿真闭环。",
        "horizon_days": horizon_days,
        "model_pipeline": build_model_pipeline(network),
        "display_names": build_display_names(network),
        "simulation_inputs": build_simulation_inputs(network),
        "constraint_summary": build_constraint_summary(network, recommendations, allocations),
        "pool_levels": build_pool_levels(network, allocations),
        "stockout_alerts": build_stockout_alerts(recommendations, allocations),
        "approval_queue": build_approval_queue(network, recommendations, allocations),
        "warehouse_map": build_warehouse_map(network, recommendations, allocations),
        "recommendations": dataclass_to_dict(recommendations),
        "allocations": dataclass_to_dict(allocations),
        "simulation_comparison": dataclass_to_dict(comparison),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "replenishment_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "replenishment_plan.md").write_text(render_markdown(plan), encoding="utf-8")
    (output_dir / "dashboard.html").write_text(render_dashboard_html(plan), encoding="utf-8")
    return plan


def build_display_names(network) -> dict:
    return {
        "skus": {sku: item.name for sku, item in network.skus.items()},
        "warehouses": {
            warehouse_id: WAREHOUSE_DISPLAY_NAMES.get(warehouse_id, f"{warehouse.country} {warehouse.region}仓")
            for warehouse_id, warehouse in network.warehouses.items()
        },
    }


def build_model_pipeline(network) -> dict:
    training_samples = sum(max(0, len(series.daily_units) - 14) for series in network.demand_history)
    return {
        "active_model": "LightGBM/XGBoost/RandomForest ML ensemble",
        "feature_sources": [
            "订单销量",
            "滞后销量",
            "滚动均值",
            "波动率",
            "趋势",
            "星期周期",
            "广告投放指数",
            "促销提升系数",
            "季节指数",
            "平台活动",
            "价格指数",
        ],
        "signal_count": len(network.market_signals),
        "training_samples": training_samples,
        "notes": "比赛版使用 180 天 3C 配件 SKU×海外仓销量数据训练树模型集成；部署环境会优先启用 LightGBM、XGBoost 和 scikit-learn RandomForest，本地缺包时自动使用内置随机森林兜底。分仓层使用 scipy.optimize.milp 做整数规划约束求解。",
    }


def build_simulation_inputs(network) -> dict:
    return {
        "skus": {
            sku: {
                "name": item.name,
                "category": item.category,
                "unit_margin": item.unit_margin,
                "service_class": item.service_class,
            }
            for sku, item in network.skus.items()
        },
        "warehouses": {
            warehouse_id: {
                "country": warehouse.country,
                "region": warehouse.region,
                "handling_capacity_per_day": warehouse.handling_capacity_per_day,
                "last_mile_score": warehouse.last_mile_score,
            }
            for warehouse_id, warehouse in network.warehouses.items()
        },
        "market_signals": {
            f"{signal.sku}||{signal.warehouse_id}": {
                "platform": signal.platform,
                "ad_spend_index": signal.ad_spend_index,
                "promotion_lift": signal.promotion_lift,
                "seasonal_index": signal.seasonal_index,
                "platform_event_lift": signal.platform_event_lift,
                "price_index": signal.price_index,
            }
            for signal in network.market_signals.values()
        },
    }


def build_constraint_summary(network, recommendations, allocations) -> dict:
    allocated_by_sku: dict[str, int] = {}
    allocated_by_warehouse: dict[str, int] = {}
    for row in allocations:
        allocated_by_sku[row.sku] = allocated_by_sku.get(row.sku, 0) + row.allocated_units
        allocated_by_warehouse[row.warehouse_id] = allocated_by_warehouse.get(row.warehouse_id, 0) + row.allocated_units

    procurement = []
    for sku, constraint in network.procurement_constraints.items():
        planned = allocated_by_sku.get(sku, 0)
        capital_used = planned * constraint.unit_purchase_cost
        procurement.append(
            {
                "sku": sku,
                "supplier": constraint.supplier,
                "planned_units": planned,
                "available_units": constraint.available_units,
                "min_order_qty": constraint.min_order_qty,
                "capital_used": round(capital_used, 2),
                "capital_budget": constraint.capital_budget,
                "status": "资金需复核" if capital_used > constraint.capital_budget else "可执行",
            }
        )

    warehouses = []
    for warehouse_id, constraint in network.warehouse_constraints.items():
        current_position = sum(
            item.available_position for item in network.inventory.values() if item.warehouse_id == warehouse_id
        )
        planned = allocated_by_warehouse.get(warehouse_id, 0)
        usage_after_plan = current_position + planned
        warehouses.append(
            {
                "warehouse_id": warehouse_id,
                "capacity_units": constraint.capacity_units,
                "current_position": current_position,
                "planned_inbound": planned,
                "usage_after_plan": usage_after_plan,
                "usage_rate": round(usage_after_plan / max(1, constraint.capacity_units), 3),
                "storage_cost_per_unit_day": constraint.storage_cost_per_unit_day,
                "max_daily_inbound_units": constraint.max_daily_inbound_units,
            }
        )

    transport = []
    for row in allocations:
        transport.append(
            {
                "sku": row.sku,
                "warehouse_id": row.warehouse_id,
                "mode": row.transport_mode,
                "units": row.allocated_units,
                "lead_time_days": row.lead_time_days,
                "status": row.capacity_note,
                "optimizer": row.optimizer,
                "optimizer_status": row.optimizer_status,
            }
        )

    return {"procurement": procurement, "warehouses": warehouses, "transport": transport}


def build_pool_levels(network, allocations) -> list[dict]:
    allocated_by_sku: dict[str, int] = {}
    for row in allocations:
        allocated_by_sku[row.sku] = allocated_by_sku.get(row.sku, 0) + row.allocated_units

    levels = []
    for sku, pool in network.global_supply_pool.items():
        allocated = allocated_by_sku.get(sku, 0)
        levels.append(
            {
                "sku": sku,
                "available_units": pool.available_units,
                "reserved_units": pool.reserved_units,
                "allocatable_units": pool.allocatable_units,
                "allocated_units": allocated,
                "remaining_units": max(0, pool.allocatable_units - allocated),
                "waterline": round(allocated / max(1, pool.allocatable_units), 3),
            }
        )
    return levels


def build_stockout_alerts(recommendations, allocations) -> list[dict]:
    allocation_by_key = {(row.sku, row.warehouse_id): row for row in allocations}
    alerts = []
    for row in recommendations:
        allocation = allocation_by_key.get((row.sku, row.warehouse_id))
        unmet_units = allocation.unmet_units if allocation else row.recommended_units
        if row.urgency >= 0.55 or unmet_units > 0:
            severity = "红色" if row.urgency >= 0.65 or unmet_units >= 100 else "黄色"
            alerts.append(
                {
                    "sku": row.sku,
                    "warehouse_id": row.warehouse_id,
                    "severity": severity,
                    "urgency": row.urgency,
                    "unmet_units": unmet_units,
                    "message": "高缺货风险，建议优先审批补货" if severity == "红色" else "存在缺口，建议滚动观察",
                }
            )
    return sorted(alerts, key=lambda item: (item["severity"] == "红色", item["urgency"]), reverse=True)


def build_approval_queue(network, recommendations, allocations) -> list[dict]:
    allocation_by_key = {(row.sku, row.warehouse_id): row for row in allocations}
    queue = []
    for row in recommendations:
        allocation = allocation_by_key.get((row.sku, row.warehouse_id))
        if not row.approval_required and not (allocation and allocation.approval_status == "待审批"):
            continue
        sku = network.skus[row.sku]
        allocated_units = allocation.allocated_units if allocation else 0
        queue.append(
            {
                "sku": row.sku,
                "warehouse_id": row.warehouse_id,
                "recommended_units": row.recommended_units,
                "allocated_units": allocated_units,
                "estimated_margin": round(allocated_units * sku.unit_margin, 2),
                "transport_mode": allocation.transport_mode if allocation else "待定",
                "status": allocation.approval_status if allocation else "待审批",
                "reason": row.reason,
            }
        )
    return queue


def build_warehouse_map(network, recommendations, allocations) -> list[dict]:
    allocated_by_warehouse: dict[str, int] = {}
    risk_by_warehouse: dict[str, list[float]] = {}
    for row in allocations:
        allocated_by_warehouse[row.warehouse_id] = allocated_by_warehouse.get(row.warehouse_id, 0) + row.allocated_units
    for row in recommendations:
        risk_by_warehouse.setdefault(row.warehouse_id, []).append(row.urgency)

    nodes = []
    for warehouse_id, warehouse in network.warehouses.items():
        constraint = network.warehouse_constraints.get(warehouse_id)
        current_position = sum(
            item.available_position for item in network.inventory.values() if item.warehouse_id == warehouse_id
        )
        planned = allocated_by_warehouse.get(warehouse_id, 0)
        avg_risk = sum(risk_by_warehouse.get(warehouse_id, [0])) / max(1, len(risk_by_warehouse.get(warehouse_id, [])))
        capacity = constraint.capacity_units if constraint else max(1, current_position + planned)
        nodes.append(
            {
                "warehouse_id": warehouse_id,
                "country": warehouse.country,
                "region": warehouse.region,
                "latitude": warehouse.latitude,
                "longitude": warehouse.longitude,
                "current_position": current_position,
                "planned_inbound": planned,
                "usage_rate": round((current_position + planned) / max(1, capacity), 3),
                "risk": round(avg_risk, 3),
                "last_mile_score": warehouse.last_mile_score,
            }
        )
    return nodes


def render_markdown(plan: dict) -> str:
    display_names = plan.get("display_names", {})
    sku_names = display_names.get("skus", {})
    warehouse_names = display_names.get("warehouses", {})

    def sku_name(sku: str) -> str:
        return sku_names.get(sku, sku)

    def warehouse_name(warehouse_id: str) -> str:
        return warehouse_names.get(warehouse_id, warehouse_id)

    def source_name(source: str) -> str:
        return "深圳中心库存池" if source == "GLOBAL-POOL" else warehouse_name(source)

    lines = [
        "# 智能补货与分仓决策计划",
        "",
        f"- 项目: {plan['project']}",
        f"- 策略: {plan['strategy']}",
        f"- 预测周期: {plan['horizon_days']} 天",
        "",
        "## 闭环定位",
        "",
        "本原型收缩到 3C 配件跨境卖家场景，不做跨境电商后台大杂烩。系统围绕 15 个具体 SKU、美国/德国/英国/澳洲四个海外仓和深圳中心库存池，建立 AI 预测、补货优化、分仓决策和仿真评估闭环。",
        "",
        "## 预测模型与外部信号",
        "",
        f"- 当前模型: {plan['model_pipeline']['active_model']}",
        f"- 外部信号: {', '.join(plan['model_pipeline']['feature_sources'])}",
        f"- 样例信号条数: {plan['model_pipeline']['signal_count']}",
        "",
        "## 补货建议",
        "",
        "| SKU | 海外仓 | 模型 | 日均预测 | P80需求 | P95需求 | 库存位置 | 补货点 | 目标库存 | 建议补货 | 审批 | 紧急度 | 原因 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |",
    ]

    for row in plan["recommendations"]:
        row_display = {**row, "sku": sku_name(row["sku"]), "warehouse_id": warehouse_name(row["warehouse_id"])}
        lines.append(
            "| {sku} | {warehouse_id} | {model_name} | {daily_rate} | {p80_units} | {p95_units} | "
            "{inventory_position} | {reorder_point} | {target_stock} | {recommended_units} | "
            "{approval_required} | {urgency} | {reason} |".format(**row_display)
        )

    lines.extend(
        [
            "",
            "## 库存池分配结果",
            "",
            "| SKU | 目标仓 | 来源 | 运输 | 分配数量 | 未满足缺口 | 预计到仓 | 优先级 | 审批 | 优化器 | 求解状态 | 仓容约束 | 决策说明 |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in plan["allocations"]:
        row_display = {
            **row,
            "sku": sku_name(row["sku"]),
            "warehouse_id": warehouse_name(row["warehouse_id"]),
            "source": source_name(row["source"]),
        }
        lines.append(
            "| {sku} | {warehouse_id} | {source} | {transport_mode} | {allocated_units} | {unmet_units} | "
            "{lead_time_days}天 | {priority_score} | {approval_status} | {optimizer} | {optimizer_status} | {capacity_note} | {decision_note} |".format(**row_display)
        )

    lines.extend(
        [
            "",
            "## 库存池水位",
            "",
            "| SKU | 可用库存 | 保留库存 | 可分配 | 已分配 | 剩余 | 水位 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in plan["pool_levels"]:
        row_display = {**row, "sku": sku_name(row["sku"])}
        lines.append(
            "| {sku} | {available_units} | {reserved_units} | {allocatable_units} | "
            "{allocated_units} | {remaining_units} | {waterline} |".format(**row_display)
        )

    lines.extend(
        [
            "",
            "## 缺货预警与审批队列",
            "",
            "| SKU | 海外仓 | 严重度 | 紧急度 | 缺口 | 提示 |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in plan["stockout_alerts"]:
        row_display = {**row, "sku": sku_name(row["sku"]), "warehouse_id": warehouse_name(row["warehouse_id"])}
        lines.append(
            "| {sku} | {warehouse_id} | {severity} | {urgency} | {unmet_units} | {message} |".format(**row_display)
        )

    comparison = plan["simulation_comparison"]
    baseline = comparison["baseline"]
    intelligent = comparison["intelligent"]
    improvement = comparison["improvement"]
    lines.extend(
        [
            "",
            "## 90天滚动仿真对比",
            "",
            "| 指标 | 传统独立备货 | 智能库存池方案 | 改善方向 |",
            "| --- | ---: | ---: | ---: |",
            f"| Fill Rate | {baseline['fill_rate']} | {intelligent['fill_rate']} | +{improvement['fill_rate_lift_pct']}pct |",
            f"| 缺货率 | {baseline['stockout_rate']} | {intelligent['stockout_rate']} | -{improvement['stockout_rate_reduction_pct']}pct |",
            f"| 平均配送时效 | {baseline['average_delivery_days']}天 | {intelligent['average_delivery_days']}天 | -{improvement['delivery_days_reduction']}天 |",
            f"| 平均库存 | {baseline['average_inventory_units']}件 | {intelligent['average_inventory_units']}件 | -{improvement['inventory_reduction_units']}件 |",
            f"| 总利润 | {baseline['total_profit']} | {intelligent['total_profit']} | +{improvement['profit_lift']} |",
            "",
            f"结论：{comparison['interpretation']}",
        ]
    )

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run overseas warehouse replenishment demo.")
    parser.add_argument("--data", type=Path, default=Path("data/sample_network.json"))
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--horizon-days", type=int, default=30)
    args = parser.parse_args()

    plan = build_plan(args.data, args.output, args.horizon_days)
    comparison = plan["simulation_comparison"]
    print(f"Generated {len(plan['recommendations'])} recommendations and {len(plan['allocations'])} allocations.")
    print(
        "Simulation: fill rate "
        f"{comparison['baseline']['fill_rate']} -> {comparison['intelligent']['fill_rate']}, "
        f"stockout {comparison['baseline']['stockout_rate']} -> {comparison['intelligent']['stockout_rate']}."
    )
    print(f"Markdown plan: {args.output / 'replenishment_plan.md'}")
    print(f"Dashboard: {args.output / 'dashboard.html'}")


if __name__ == "__main__":
    main()
