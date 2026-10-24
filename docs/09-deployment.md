# 比赛网页端部署说明

## 目标

把比赛原型部署成学生可直接使用的网页端：打开浏览器，上传 3C 配件跨境电商仓网数据 JSON，系统自动完成需求预测、MILP 补货优化、库存池分仓、仿真评估，并按 5 页展示可视化、审批和执行回写。

## 本地运行

```bash
PYTHONPATH=src python -m overseas_inventory.competition_web --host 127.0.0.1 --port 8000
```

访问：

```text
http://127.0.0.1:8000
```

网页端能力：

- 登录比赛控制台，默认演示密码为 `globalpool`。
- 上传 `data/sample_network.json` 或同结构真实 JSON。
- 查看首页、销量预测、补货建议、分仓方案和评估对比 5 个页面。
- 查看仓库风险、库存池水位、分仓运输和核心仿真指标。
- 审批补货单。
- 回写执行结果。
- 下载静态驾驶舱、JSON 计划和 Markdown 报告。

## Docker 部署

构建镜像：

```bash
docker build -t globalpool-ai .
```

镜像构建时会安装 `.[ml]` 依赖，默认包含 LightGBM、XGBoost 和 scikit-learn RandomForest。若本地直接运行且没有这些包，预测模块会自动使用内置随机森林兜底，功能仍可运行。

运行容器：

```bash
docker run --rm -p 8000:8000 -v "$PWD/runs:/app/runs" globalpool-ai
```

如需修改演示密码：

```bash
docker run --rm -p 8000:8000 \
  -e GLOBALPOOL_ADMIN_PASSWORD=your-password \
  -v "$PWD/runs:/app/runs" \
  globalpool-ai
```

访问：

```text
http://127.0.0.1:8000
```

使用 Compose：

```bash
docker compose up --build
```

Compose 会把本地 `runs/` 挂载到容器 `/app/runs`，便于保留上传任务、审批记录和执行回写。

## 输出文件

每次上传会在 `runs/{job_id}/` 生成：

- `input_network.json`
- `metadata.json`
- `dashboard.html`
- `replenishment_plan.md`
- `replenishment_plan.json`
- `operations_state.json`

## 数据要求

最小必需字段：

- `service_level_z`
- `skus`
- `warehouses`
- `lanes`
- `global_supply_pool`
- `inventory`
- `demand_history`

增强字段：

- `market_signals`
- `transport_options`
- `warehouse_constraints`
- `procurement_constraints`

当前样例已内置 15 个 3C 配件 SKU、4 个海外仓和 180 天模拟销量。推荐直接复制 `data/sample_network.json` 的结构，再替换为真实订单、广告、促销、季节、平台活动、价格、库存和仓网资料。
