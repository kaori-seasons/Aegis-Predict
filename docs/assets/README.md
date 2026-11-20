# SOP 与预测图形资产

## README 顶部：跨行业端到端 SOP

[动态 GIF](industry-sop.gif) · [静态 SVG](industry-sop.svg) · [静态 PNG](industry-sop.png) · [八步分镜](industry-sop-contact.png) · [结构化源](industry-sop.json) · [步骤表](industry-sop-steps.md) · [生成元数据](industry-sop.meta.json)

新版按四条岗位泳道展示电商/商贸、海外仓/3PL、货代/承运协同的八个步骤：接入、核对、预测、补货、审批发布、发运收货、双端库存结算、成熟复盘。每段标明负责人、页面、操作、交接、放行条件与异常去向。业务循环在07结算关闭后重新开始，08成熟评分独立等待，不阻塞下一轮。

这是一份流程说明，不是实际店铺或承运商录屏，不含虚构收益、处理时长或发运数量。图中区分系统内的事实/预测/审批/预占/对账与系统外的真实授权、WMS作业、订舱和运输。真实执行仍需单独完成开关、来源/对账、模型和实际试点验收。

- **规格：**1600×1150px，55秒，无限循环，默认4fps，约0.85MB。
- **阅读：**首帧就是完整概览；后续八段逐步点亮。暂停/减少动态时可读PNG、SVG或分镜。
- **可维护：**`industry-sop.json` 是岗位与文字的单一数据源。生成脚本校验画布及区域文字边界、步骤数、GIF帧/总时长，并写出源文件摘要。
- **同步：**`--update-docs` 只更新 README 和行业手册中 `BEGIN/END INDUSTRY_SOP` 标记之间的步骤表，不重写其他正文。

仅作者工具需要Pillow，不增加生产运行依赖，也不分发宿主字体：

```bash
python3 -m venv /tmp/aegis-gif-venv
/tmp/aegis-gif-venv/bin/pip install Pillow==13.0.0
/tmp/aegis-gif-venv/bin/python scripts/render_industry_sop.py --update-docs
```

默认检测macOS Hiragino Sans GB或Linux Noto Sans CJK，可显式指定 `--font /path/to/CJK-font.ttc`、`--bold-index INDEX`，动画帧率可用 `--fps 2` 到 `--fps 8` 调整，总阅读时长保持55秒。光栅文件无需读者安装字体；SVG会使用读者系统的中文字体。

变更后检查[全图](industry-sop.png)和[分镜](industry-sop-contact.png)，再核对[行业手册](../24-industry-sop-and-readme-guide.md)中的岗位权限、影子/执行、累计量和结算条件。[图文验收记录](../verification/industry-sop-docs.json)只说明本次图形与文档检查，既有应用测试/容器证据仍对应其原代码交付。

---

# 默认 v2 预测内部流程

README模型详情中的旧GIF展示默认v2的预测过程（候选v3的训练内选模见[模型评估文档](../19-forecast-model-evaluation.md)），不是项目能力宣传图。使用普通流程框、判断框和箭头；七个步骤逐段点亮，右侧演算同一组固定销量数据。

静态图：[SVG](aegis-workflow.svg) / [PNG](aegis-workflow.png)。GIF为1540×1240px、42秒循环；可打开静态矢量图放大阅读。

## 与当前实现一致的流程

1. **规范有效需求**：订单行或日销量经标识映射、状态过滤、版本去重与权威来源选择，避免多来源重复累计。
2. **日历对齐与数据门禁**：按SKU×仓库形成日序列，只使用仓库当地已完成的日期，分别保留缺失和缺货删失标记。检查覆盖、时效和样本等条件，异常返回隔离/补齐/重放；固定截止日并冻结输入、策略与周期，记录摘要。
3. **只用过去观察填补**：真实零销量保留。缺失或缺货删失日，有历史有效观察时取“当日已知销量”和“此前最多28次有效观察均值”的较大值；没有此前观察时沿用当日值。填补值不作为新的有效观察加入均值窗口，不读取未来值。
4. **确定性模型分流**：有效观察少于7天走类别均值冷启动；否则，处理后的非零天数比例小于50%走TSB；其余走阻尼周季节模型，历史长度≥28天时才加入周季节项，否则仅用近期水平。低样本规划仍受上游数据门禁约束。模型函数具备冷启动分支，不表示任意低样本都能发布执行计划。
5. **计算需求分位**：输出日均需求、波动、预测周期、P50/P80/P95及启发式置信分数。分位采用正态近似，不是经过校准的履约保证；置信分数不是命中率。
6. **独立历史回测**：从同一冻结历史取训练前缀，至少28天起训、预测后7天，按7天非重叠窗口滚动。含缺失/缺货删失的测试窗整折排除。报告WAPE、偏差和过去28天均值基线；回测不自动择优换模。
7. **结果输出**：预测页/冻结计划明细展示需求结果，效果评估页展示分组回测。库存、提前期、约束分配、审批与实物执行属于下游补货过程，本图不展开。

代码依据：[来源观察](../../src/aegis/integration/observations.py)、[规划组装](../../src/aegis/planning/assembler.py)、[冻结输入](../../src/aegis/planning/lifecycle.py)、[预测函数](../../src/aegis/planning/forecast.py)、[历史回测](../../src/aegis/planning/backtest.py)。这张v2图不覆盖v3/v4/v5的选型、计划外部信号、经营滞后或新品群组路径；相应能力以当前交付文档为准。

## 图中的实际演算

`forecast-example.json`保存完整固定样例、处理后序列、预测输出和逐折回测结果。生成脚本显式指定v2版本，调用 `forecast()`、v2的 `legacy_uncensor()` 与配对 `rolling_backtest()`，不手写预测值或回测指标。

- SKU S / 仓库W：2026-09-01至2026-11-09，共70天，其中65天有效观察、3天缺失、2天缺货删失；有效观察含4个真实零销量日。
- 预测未来14天，当前分流结果为 `damped-seasonal`。图中输出和指标从函数运行结果读取，展示值适当四舍五入。
- 正态近似需求分位：P50=153、P80=165、P95=177。
- 6个候选回测窗口中2个有效，其余含缺失或删失被排除。本样例预测WAPE约8.47%，均值基线约7.39%；按实际结果展示，不声称样例模型优于基线。
- 这是演示数据，不代表真实商家销量、线上准确率或收益。

## 复生成

仅作者工具依赖Pillow；不改变生产运行依赖，也不分发宿主字体文件。

```bash
python3 -m venv /tmp/aegis-gif-venv
/tmp/aegis-gif-venv/bin/pip install Pillow==13.0.0
/tmp/aegis-gif-venv/bin/python scripts/render_workflow_gif.py
```

字体默认检测macOS Hiragino Sans GB或Linux Noto Sans CJK；其他环境用 `--font /path/to/CJK-font.ttc`，可用 `--bold-index INDEX` 指定字体集字重。当前交付的中文已光栅化到GIF/PNG，无需读者安装字体。

`aegis-workflow.json`记录尺寸、时长、帧数和预测/回测源码摘要。修改算法后应重新生成，并检查各分镜的箭头、文字和计算结果。
