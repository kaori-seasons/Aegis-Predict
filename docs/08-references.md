# 参考依据

## 跨境物流不确定性

- 世界银行 Logistics Performance Index 官方页面：<https://lpi.worldbank.org/>
- World Bank LPI 2.0 介绍页：<https://lpi.worldbank.org/lpi-2-0>

可用于说明：跨境供应链越来越依赖货运追踪、速度、连接性和可靠性等数据化指标，海外仓补货需要把物流不确定性纳入模型。

## 预测 + 优化 + 仿真方向

- JD.com 智能供应链库存分配方向相关论文检索：<https://arxiv.org/search/?query=JD.com+inventory+allocation+forecasting+optimization+simulation&searchtype=all>

可用于说明：供应链比赛项目不能停留在销量预测，还需要把预测接到库存分配、补货优化和仿真评估。

## 外部变量与需求预测

- XGBoost 零售库存预测相关论文检索：<https://arxiv.org/search/?query=XGBoost+retail+inventory+demand+forecasting+holidays&searchtype=all>

可用于说明：节假日、促销、星期、价格、异常等外部变量可以作为需求预测特征；比赛原型可先用趋势加权预测，后续升级到 XGBoost/LightGBM。

## 本项目使用方式

本项目没有把这些研究直接复制为复杂模型，而是采用适合 4-8 周比赛开发周期的路线：

```text
可解释预测基线 -> P80/P95需求 -> 补货优化 -> 库存池分仓 -> 滚动仿真对比
```

这样既能讲清楚算法，也能在比赛现场演示业务效果。
