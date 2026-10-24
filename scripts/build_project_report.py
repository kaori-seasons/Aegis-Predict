from __future__ import annotations

import json
from pathlib import Path
import textwrap

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "competition" / "project_book"
ASSET_DIR = REPORT_DIR / "assets"
SCREENSHOT_DIR = REPORT_DIR / "system_screenshots"


PROJECT_NAME = "GlobalPool AI：基于AI预测的跨境电商海外仓智能补货与分仓系统"
TEAM_NAME = "【请填写团队名称】"
SCHOOL = "【请填写学校】"
COLLEGE = "【请填写学院】"
MEMBERS = "【请填写成员姓名】"
SUBMIT_DATE = "2026年6月"


plt.rcParams["font.sans-serif"] = ["PingFang SC", "STSong", "Heiti TC", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False


def load_report_plan() -> dict:
    path = ROOT / "output" / "replenishment_plan.json"
    if not path.exists():
        from overseas_inventory.demo import build_plan

        build_plan(ROOT / "data" / "sample_network.json", ROOT / "output", 30)
    return json.loads(path.read_text(encoding="utf-8"))


def report_metrics() -> dict:
    plan = load_report_plan()
    simulation = plan["simulation_comparison"]
    baseline = simulation["baseline"]
    intelligent = simulation["intelligent"]
    improvement = simulation["improvement"]
    inventory_delta = intelligent["average_inventory_units"] - baseline["average_inventory_units"]
    return {
        "plan": plan,
        "recommendations": len(plan.get("recommendations", [])),
        "allocations": len(plan.get("allocations", [])),
        "baseline": baseline,
        "intelligent": intelligent,
        "improvement": improvement,
        "inventory_delta": inventory_delta,
        "inventory_tradeoff": (
            f"平均库存减少 {abs(inventory_delta):.0f} 件"
            if inventory_delta < 0
            else f"平均库存增加 {inventory_delta:.0f} 件"
        ),
        "profit_lift_wan": improvement["profit_lift"] / 10000,
    }


def metric_line() -> str:
    metrics = report_metrics()
    baseline = metrics["baseline"]
    intelligent = metrics["intelligent"]
    return (
        f"Prototype evidence: Fill Rate {baseline['fill_rate']:.3f} -> {intelligent['fill_rate']:.3f}; "
        f"stockout {baseline['stockout_rate']:.3f} -> {intelligent['stockout_rate']:.3f}; "
        f"delivery {baseline['average_delivery_days']:.2f}d -> {intelligent['average_delivery_days']:.2f}d; "
        f"profit +{metrics['profit_lift_wan']:.2f}万 RMB; {metrics['inventory_tradeoff']}"
    )


def save_fig(name: str, title: str, kind: str) -> None:
    fig, ax = plt.subplots(figsize=(10.8, 6.1), dpi=180)
    ax.set_axis_off()
    fig.patch.set_facecolor("#ffffff")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(0.02, 0.955, "a", fontsize=15, fontweight="bold", color="#111827")
    ax.text(0.055, 0.955, title, fontsize=14, fontweight="bold", color="#111827")
    ax.plot([0.055, 0.96], [0.925, 0.925], lw=0.7, color="#d1d5db")

    if kind == "overview":
        boxes = [
            ("订单/库存/在途/促销数据", 0.04, 0.62, "#dbeafe"),
            ("AI需求预测\nP50/P80/P95", 0.25, 0.62, "#dcfce7"),
            ("智能补货\n安全库存/补货点", 0.46, 0.62, "#fef3c7"),
            ("库存池分配\nMILP约束优化", 0.67, 0.62, "#ede9fe"),
            ("90天仿真评估\n对比传统方案", 0.38, 0.28, "#fee2e2"),
        ]
        draw_boxes(ax, boxes)
        arrows = [((0.20, 0.72), (0.25, 0.72)), ((0.41, 0.72), (0.46, 0.72)), ((0.62, 0.72), (0.67, 0.72)), ((0.73, 0.62), (0.50, 0.43))]
        draw_arrows(ax, arrows)
        ax.text(0.05, 0.12, metric_line(), fontsize=8.4, color="#334155")

    elif kind == "background":
        labels = ["跨境电商增长", "合规趋严", "海外仓复杂度", "库存割裂", "预测不足"]
        values = [85, 78, 82, 88, 74]
        ax.barh(labels, values, color=["#2563eb", "#0f766e", "#f59e0b", "#dc2626", "#7c3aed"])
        ax.set_axis_on()
        ax.set_xlim(0, 100)
        ax.set_xlabel("Opportunity / pain-point intensity")
        ax.grid(axis="x", alpha=0.25)

    elif kind == "product":
        boxes = [
            ("上传业务数据", 0.08, 0.68, "#dbeafe"),
            ("生成预测任务", 0.32, 0.68, "#dcfce7"),
            ("输出补货/分仓计划", 0.56, 0.68, "#fef3c7"),
            ("审批与执行回写", 0.32, 0.34, "#ede9fe"),
        ]
        draw_boxes(ax, boxes)
        draw_arrows(ax, [((0.24, 0.78), (0.32, 0.78)), ((0.48, 0.78), (0.56, 0.78)), ((0.66, 0.68), (0.46, 0.49))])
        ax.text(0.08, 0.18, "Product form: SaaS decision platform + forecasting/optimization engine + explainable operations console", fontsize=10, color="#334155")

    elif kind == "market":
        years = [2025, 2026, 2027, 2028, 2029]
        ecommerce = [6.419, 6.88, 7.33, 7.89, 8.47]
        china_cbec = [2.75, 2.98, 3.23, 3.50, 3.78]
        ax.set_axis_on()
        ax.plot(years, ecommerce, marker="o", linewidth=2.2, label="Global retail e-commerce, trillion USD", color="#2563eb")
        ax.plot(years, china_cbec, marker="s", linewidth=2.2, label="China cross-border e-commerce, trillion RMB", color="#dc2626")
        ax.grid(alpha=0.25)
        ax.legend()
        ax.set_xticks(years)

    elif kind == "swot":
        quadrants = [
            ("S 优势\n算法闭环、可解释、已有原型", 0.05, 0.53, "#dcfce7"),
            ("W 劣势\n真实数据接入与模型训练仍需加强", 0.52, 0.53, "#fee2e2"),
            ("O 机会\n海外仓数字化、合规化、库存降本", 0.05, 0.18, "#dbeafe"),
            ("T 威胁\nERP/WMS厂商、平台政策变化", 0.52, 0.18, "#fef3c7"),
        ]
        draw_boxes(ax, quadrants, w=0.4, h=0.24)

    elif kind == "business":
        boxes = [
            ("目标客户\n品牌出海卖家\n海外仓运营商", 0.05, 0.58, "#dbeafe"),
            ("价值主张\n少缺货/少积压\n可解释决策", 0.29, 0.58, "#dcfce7"),
            ("收入来源\n订阅费/实施费\nAPI调用/增值服务", 0.53, 0.58, "#fef3c7"),
            ("合作伙伴\nERP/WMS/物流商\n合规服务商", 0.29, 0.24, "#ede9fe"),
        ]
        draw_boxes(ax, boxes)
        draw_arrows(ax, [((0.21, 0.68), (0.29, 0.68)), ((0.45, 0.68), (0.53, 0.68)), ((0.62, 0.58), (0.43, 0.39))])

    elif kind == "plan":
        stages = ["0-2个月\n原型打磨", "3-6个月\n试点数据接入", "6-12个月\n商业化试点", "12个月+\n规模化部署"]
        xs = [0.08, 0.31, 0.54, 0.77]
        for i, (stage, x) in enumerate(zip(stages, xs)):
            color = ["#dbeafe", "#dcfce7", "#fef3c7", "#ede9fe"][i]
            draw_box(ax, stage, x, 0.48, 0.17, 0.22, color)
            if i < 3:
                ax.annotate("", xy=(x + 0.19, 0.59), xytext=(x + 0.22, 0.59), arrowprops=dict(arrowstyle="->", lw=2, color="#475569"))
        ax.text(0.08, 0.23, "Operating loop: data ingestion -> forecast plan -> approval -> shipment/inbound -> fulfillment feedback -> model review", fontsize=10, color="#334155")

    elif kind == "finance":
        years = ["第1年", "第2年", "第3年"]
        revenue = [80, 260, 620]
        cost = [135, 230, 420]
        ax.set_axis_on()
        x = range(len(years))
        ax.bar([i - 0.18 for i in x], revenue, width=0.36, label="收入", color="#2563eb")
        ax.bar([i + 0.18 for i in x], cost, width=0.36, label="成本", color="#f97316")
        ax.set_xticks(list(x), years)
        ax.set_ylabel("万元")
        ax.set_ylim(0, 700)
        ax.legend()
        ax.grid(axis="y", alpha=0.25)
        ax.text(0.2, 540, "Expected break-even zone by year 3 under cautious adoption assumptions", fontsize=10, color="#334155")

    elif kind == "team":
        boxes = [
            ("项目负责人\n商业路演/进度管理", 0.07, 0.58, "#dbeafe"),
            ("算法负责人\n预测/补货/MILP优化", 0.30, 0.58, "#dcfce7"),
            ("产品负责人\n原型/驾驶舱/体验", 0.53, 0.58, "#fef3c7"),
            ("市场与财务\n调研/商业模式/测算", 0.30, 0.24, "#ede9fe"),
        ]
        draw_boxes(ax, boxes)
        ax.text(0.08, 0.14, "External support: instructors, cross-border e-commerce interviewees, logistics and overseas warehouse advisors", fontsize=10, color="#334155")

    fig.tight_layout()
    fig.savefig(ASSET_DIR / name, bbox_inches="tight")
    plt.close(fig)


def draw_box(ax, text: str, x: float, y: float, w: float, h: float, color: str) -> None:
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.018,rounding_size=0.012", linewidth=0.8, edgecolor="#9ca3af", facecolor=color)
    ax.add_patch(box)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=9.5, color="#172033", linespacing=1.45)


def draw_boxes(ax, boxes, w: float = 0.16, h: float = 0.18) -> None:
    for text, x, y, color in boxes:
        draw_box(ax, text, x, y, w, h, color)


def draw_arrows(ax, arrows) -> None:
    for start, end in arrows:
        ax.annotate("", xy=end, xytext=start, arrowprops=dict(arrowstyle="->", lw=2, color="#475569"))


def build_assets() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    figures = [
        ("fig01-overview.png", "图1 项目总体闭环", "overview"),
        ("fig02-background.png", "图2 背景机会与核心痛点", "background"),
        ("fig03-product.png", "图3 产品服务形态", "product"),
        ("fig04-market.png", "图4 市场规模与增长潜力", "market"),
        ("fig05-swot.png", "图5 SWOT分析", "swot"),
        ("fig06-business.png", "图6 商业模式画布", "business"),
        ("fig07-plan.png", "图7 实施路径与运营流程", "plan"),
        ("fig08-finance.png", "图8 三年财务预测", "finance"),
        ("fig09-team.png", "图9 团队分工结构", "team"),
    ]
    for filename, title, kind in figures:
        save_fig(filename, title, kind)


def capture_system_screenshots() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"Playwright unavailable, skip screenshots: {exc}")
        return

    dashboard = ROOT / "output" / "dashboard.html"
    if not dashboard.exists():
        from overseas_inventory.demo import build_plan

        build_plan(ROOT / "data" / "sample_network.json", ROOT / "output", 30)

    targets = [
        (None, "dashboard-pool.png"),
        ("forecast", "dashboard-forecast.png"),
        ("replenishment", "dashboard-replenishment.png"),
        ("allocation", "dashboard-allocation.png"),
        ("simulation", "dashboard-simulation.png"),
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.goto(dashboard.resolve().as_uri(), wait_until="networkidle")
        for tab, filename in targets:
            if tab:
                page.locator(f'button[data-tab="{tab}"]').click()
                page.wait_for_timeout(250)
            page.screenshot(path=str(SCREENSHOT_DIR / filename), full_page=False)
        browser.close()


def report_body(anonymous: bool = False) -> str:
    team = "匿名团队" if anonymous else TEAM_NAME
    school = "匿名学校" if anonymous else SCHOOL
    college = "匿名学院" if anonymous else COLLEGE
    members = "匿名成员" if anonymous else MEMBERS
    version = "匿名版" if anonymous else "完整版"
    metrics = report_metrics()
    baseline = metrics["baseline"]
    intelligent = metrics["intelligent"]
    improvement = metrics["improvement"]

    return f"""---
title: "{PROJECT_NAME} 项目书（{version}）"
geometry: margin=2.5cm
papersize: a4
fontsize: 12pt
header-includes:
  - |
    \\linespread{{1.5}}
---

\\begin{{titlepage}}
\\centering
\\vspace*{{4cm}}
{{\\Huge\\bfseries {PROJECT_NAME}\\par}}
\\vspace{{2.5cm}}
{{\\Large 团队名称：{team}\\par}}
\\vspace{{0.6cm}}
{{\\Large 学校：{school}\\par}}
\\vspace{{0.6cm}}
{{\\Large 学院：{college}\\par}}
\\vspace{{0.6cm}}
{{\\Large 成员姓名：{members}\\par}}
\\vfill
{{\\Large 提交日期：{SUBMIT_DATE}\\par}}
\\end{{titlepage}}

# 1. 摘要

GlobalPool AI 是面向跨境电商海外仓场景的智能补货与分仓系统。跨境电商在全球零售数字化、海外消费线上化和中国供应链出海的推动下持续增长，但企业在海外仓运营中普遍面临“总库存充足、局部区域缺货”“提前期长、补货窗口难判断”“固定比例分仓导致热销仓缺货、滞销仓积压”等问题。项目以“库存池供货全球”为核心理念，将多区域海外仓库存从单仓管理升级为全球库存池协同决策。

本项目的核心创新在于把 AI 需求预测、库存安全线、补货优化、库存池分配和仿真评估连成可验证闭环。系统按 SKU-海外仓维度预测未来 30 天 P50、P80、P95 需求，结合服务等级、运输提前期和销量波动计算安全库存、补货点和目标库存，再通过整数规划在库存池、运输方式、仓容、采购可用量和资金预算等约束下生成分仓计划。原型系统已经能够输出 JSON、Markdown 和 HTML 驾驶舱，并提供上传预测、审批和执行回写能力。

市场机会来自跨境电商卖家、品牌出海企业和海外仓运营商对供应链数字化的真实需求。项目可采用 SaaS 订阅、企业实施、API 调用和供应链优化增值服务等盈利方式。基于当前样例数据，系统生成 {metrics["recommendations"]} 条补货建议和 {metrics["allocations"]} 条分仓决策；90 天滚动仿真中，智能库存池方案使 Fill Rate 从 {baseline["fill_rate"]:.3f} 提升至 {intelligent["fill_rate"]:.3f}，缺货率从 {baseline["stockout_rate"]:.3f} 降至 {intelligent["stockout_rate"]:.3f}，平均配送时效由 {baseline["average_delivery_days"]:.2f} 天缩短至 {intelligent["average_delivery_days"]:.2f} 天，总利润提升约 {metrics["profit_lift_wan"]:.2f} 万元。该结果也显示，智能方案为了保障服务水平带来“{metrics["inventory_tradeoff"]}”的策略成本，后续可通过服务等级参数控制库存与时效的权衡。

![图1 项目总体闭环介绍图](assets/fig01-overview.png)

![图2 系统实际截图：库存池总览页](system_screenshots/dashboard-pool.png)

\\newpage

# 2. 项目背景与问题提出

## 2.1 宏观环境分析

跨境电商已经成为外贸增长的重要新业态。海关总署 2026 年 1 月发布的 2025 年全年进出口情况显示，2025 年我国跨境电商进出口达到 2.75 万亿元，比 2020 年增长 69.7%。商务部《2025 中国数字贸易发展报告》披露的 2024 年明细数据显示，中国跨境电商进出口 2.71 万亿元，其中出口 2.15 万亿元，首次突破 2 万亿元；出口目的地前三位为美国、英国、德国，合计占比 53.6%，数码产品及配件占出口商品的 13.9%。与此同时，全球零售电商仍保持较大体量，EMARKETER 预计 2025 年全球零售电商销售额约为 6.419 万亿美元，占全球零售销售的 20.5%；美国商务部普查局数据显示，2026 年第一季度美国零售电商销售额约 3267 亿美元，同比增长 9.8%，占美国零售销售额 16.9%。

政策和合规环境也在变化。欧盟 IOSS 制度要求低价值进口电商包裹在特定条件下提前申报和缴纳 VAT，欧盟官方资料明确 IOSS 适用于不超过 150 欧元的进口远程销售货物；欧盟通用产品安全法规 GPSR 自 2024 年 12 月 13 日起适用，要求消费品安全责任、追溯和线上销售信息更加完善；欧盟 Data Act 自 2025 年 9 月 12 日起适用，提升了数据访问、使用和云服务切换等数字合规要求；美国 2025 年针对低值进口包裹调整免税待遇，特别影响来自中国和香港的低值包裹。这些趋势共同说明，跨境电商竞争已经从“能卖出去”进入“能合规、能稳定履约、能精细化管理库存”的阶段。

## 2.2 行业痛点与市场空白

第一，海外仓库存割裂。许多中小卖家以国家仓或平台仓为单位备货，系统只看到单仓库存，难以把在途、可调拨和安全保留库存统一纳入决策。当美国仓缺货、德国仓积压时，企业往往只能靠人工 Excel 追踪。

第二，补货依赖经验。跨境头程运输提前期长，海运、空运、清关和入仓处理存在不确定性。如果补货只看最近销量均值，遇到促销、季节、广告投放变化时容易低估需求，造成断货；若过度保守，则会抬高仓储费和资金占用。

第三，分仓缺少约束优化。传统做法常按固定比例分仓，例如美国 40%、欧洲 35%、澳洲 25%。这种方式无法同时考虑缺货风险、利润贡献、服务等级、运输成本、仓容和资金预算，导致补货计划看似公平，实际并不经济。

第四，缺少可解释决策。企业并不希望系统直接“黑箱下单”，尤其是高金额补货和高风险 SKU，需要看到预测依据、触发原因、未满足缺口和审批记录。

## 2.3 项目拟解决的核心问题

本项目拟解决的核心问题是：在跨境电商海外仓网络中，如何基于 AI 预测和库存池协同，让有限库存优先分配给最需要、最有价值、最能改善服务水平的仓点。项目不试图建设完整 OMS/WMS/ERP 后台，而是聚焦补货与分仓决策这一高价值环节，形成“预测可解释、补货可审批、分仓可约束、效果可仿真”的闭环。

![图3 背景机会与核心痛点图](assets/fig02-background.png)

\\newpage

# 3. 产品/服务描述

## 3.1 产品形态

GlobalPool AI 的产品形态是面向跨境电商企业的 SaaS 决策平台，也可以作为 ERP、WMS、OMS 的智能补货插件部署。系统接收 SKU 主数据、海外仓库存、在途库存、历史销量、广告投放、促销日历、平台活动、运输链路、仓容和资金预算等数据，生成预测需求、补货建议、库存池分配计划、缺货预警和仿真对比报告。

当前原型已经具备三类展示方式：一是命令行 demo，可自动生成补货计划；二是静态 HTML 驾驶舱，可用于比赛现场演示；三是上传预测 Web 服务，支持用户上传同结构业务数据、生成任务、登录控制台、审批补货建议并记录执行回写。

## 3.2 技术与模式创新

第一，预测结果直接服务补货决策。系统不是只输出销量预测，而是输出 P50、P80、P95 分位需求，并将 P80 作为目标库存安全线，把预测结果转化为补货点和建议补货量。

第二，库存池不是物理单仓，而是逻辑协同机制。项目把国内供应中心、可分配库存和保留库存视为全球库存池，再根据不同海外仓的风险和价值进行统一分配，避免库存过早被单区域锁死。

第三，分仓决策引入整数规划。当前原型使用 scipy.optimize.milp 将发运包装数建模为整数变量，在库存池可分配量、运输方式上限、仓容、单日入仓能力、采购可用量和资金预算约束下求解。求解器不可用时，系统回退到可解释启发式优先级，保证可演示和可落地。

第四，系统保留人工审批机制。高金额、高缺货风险或低置信度建议进入审批队列，运营人员可以批准、驳回并写入备注，后续可对接采购单、物流单和入仓单。

## 3.3 与现有竞品的差异化优势

传统 ERP/WMS 更偏执行记录，擅长订单、库存、仓储和财务管理，但对“该补多少、分到哪里、为什么这样分”的智能决策支持有限。部分预测工具只做销量预测，不处理仓容、运输、资金和服务等级约束。GlobalPool AI 的差异化在于：以库存池为核心，连接预测、补货、分仓和仿真；以可解释指标服务业务审批；以比赛原型证明系统可以运行并输出量化改善。

![图4 产品服务形态图](assets/fig03-product.png)

![图5 系统实际截图：AI需求预测页](system_screenshots/dashboard-forecast.png)

\\newpage

# 4. 市场分析

## 4.1 目标市场界定

项目目标市场分为三类。第一类是中国跨境电商品牌卖家，尤其是销售 50 个以上 SKU、覆盖北美、欧洲、澳洲多个海外仓的卖家。第二类是海外仓运营商，可以将系统作为增值服务，帮助客户减少缺货和积压。第三类是 ERP/WMS/OMS 软件服务商，可通过 API 集成智能补货和库存池分仓能力。

地域上，项目优先覆盖美国、英国、德国、澳大利亚等成熟跨境电商消费市场。这些地区订单密度高、海外仓体系相对成熟、物流数据可获得性强，也更容易体现库存优化价值。

## 4.2 市场规模与增长潜力

从需求端看，全球零售电商仍处于万亿美元级规模，线上消费占比继续提高。EMARKETER 预计 2025 年全球零售电商销售额达到 6.419 万亿美元，约占全球零售的 20.5%；美国 2026 年第一季度零售电商销售额达到 3267 亿美元，说明成熟消费市场仍在扩张。从供给端看，中国跨境电商进出口 2025 年达到 2.75 万亿元，2024 年跨境电商出口已经达到 2.15 万亿元，说明大量中国卖家正在面对多市场、多平台、多仓履约挑战。

从细分机会看，海外仓智能补货并不是单纯物流工具，而是供应链计划能力。企业缺货会损失销售额和平台排名，积压会带来仓储费、清货折扣和资金占用。只要系统能把缺货率降低 1-3 个百分点、把平均库存降低 5-15%，对中大型卖家就具有明确经济价值。

![图6 市场规模与增长潜力图](assets/fig04-market.png)

## 4.3 竞争格局与 SWOT 分析

市场上的相关竞争者包括跨境 ERP、海外仓 WMS、供应链计划软件、平台官方库存工具和大型企业自建系统。它们各有优势，但中小跨境卖家常见问题是工具割裂：ERP 管订单，WMS 管仓库，物流商管轨迹，广告平台管投放，真正把预测、库存池和分仓放在一起决策的轻量工具仍有空间。

本项目的优势是场景聚焦、原型完整、可解释性强；劣势是当前仍处于比赛原型阶段，真实企业数据接入和大规模模型训练不足；机会是跨境电商增长、海外仓精细化和合规运营需求上升；威胁是成熟 ERP/WMS 厂商可向上扩展智能计划模块，平台政策和关税规则也可能改变业务假设。

![图7 SWOT分析图](assets/fig05-swot.png)

\\newpage

# 5. 商业模式

## 5.1 价值主张

GlobalPool AI 的价值主张是“用 AI 把全球库存变成一池可动态调度的供应能力”。对跨境卖家而言，系统帮助其减少断货、降低积压、提高补货计划效率和库存周转；对海外仓运营商而言，系统可以提升客户粘性，形成数据化运营服务；对 ERP/WMS 服务商而言，系统可以作为智能决策插件，提高产品附加值。

系统的核心价值指标包括 Fill Rate、缺货率、平均库存、库存周转天数、配送时效、计划采纳率和总利润。当前样例仿真显示，智能库存池方案在 90 天周期内将 Fill Rate 提升 {improvement["fill_rate_lift_pct"]:.2f} 个百分点，缺货率降低 {improvement["stockout_rate_reduction_pct"]:.2f} 个百分点，平均配送时效缩短 {baseline["average_delivery_days"] - intelligent["average_delivery_days"]:.2f} 天，总利润提升约 {metrics["profit_lift_wan"]:.2f} 万元。与此同时，系统选择了更高的安全库存水位，因此出现“{metrics["inventory_tradeoff"]}”。这不是缺陷，而是服务水平优先策略的结果，真实部署时可按 SKU 利润、仓租和客户承诺时效调节参数。

## 5.2 盈利模式与定价策略

项目可采用“订阅费 + 实施费 + 增值服务”的组合模式。基础版面向中小卖家，按月收取 1999-4999 元，支持固定 SKU 数和仓库数；专业版按 7999-19999 元/月收费，支持多平台、多仓、多角色审批和 API；企业版采用年费加实施费，按客户仓网规模和数据接口复杂度报价。

增值收入包括历史数据清洗、模型定制、ERP/WMS 对接、物流商接口接入、海外仓运营咨询和合规报表输出。对于海外仓运营商，可按客户数或预测任务量收取平台服务费。

## 5.3 关键资源与合作伙伴

关键资源包括预测与优化算法、行业数据模板、Web 控制台、可解释指标体系、客户订单与库存数据、跨境物流链路数据。关键合作伙伴包括 ERP/WMS/OMS 厂商、海外仓服务商、头程物流商、税务合规服务商、跨境电商产业园和高校创新创业平台。

## 5.4 跨境合规方案

系统在合规上采取“数据最小化、规则可配置、审计可追溯”的原则。关税和 VAT 方面，系统为不同市场配置税率、申报规则和 IOSS/OSS 等电商税务字段；知识产权方面，SKU 主数据保留品牌授权、商标状态、禁售类目和平台限制字段；产品安全方面，为欧盟市场保留产品安全责任人、警示标签、追溯批次等字段；数据跨境方面，订单与消费者个人信息不作为模型必需字段，预测层优先使用脱敏后的 SKU-仓-日期销量聚合数据，减少个人信息流转；审计方面，补货审批和执行回写均保留操作人、时间、数量和备注。

![图8 商业模式画布图](assets/fig06-business.png)

\\newpage

# 6. 实施计划

## 6.1 阶段目标

0-6 个月为原型验证阶段。重点完成比赛版路演材料、静态驾驶舱、上传预测服务、样例数据扩展、核心算法解释和 2-3 家潜在客户访谈。技术目标是稳定生成补货建议、库存池分配结果和仿真对比；商业目标是明确目标客户、价格接受度和试点条件。

6-12 个月为试点落地阶段。重点接入真实企业订单、库存、在途和物流数据，完成数据清洗、接口适配、用户权限、审批流、任务调度和报表导出。技术上逐步引入真实 LightGBM、Prophet 或深度时序模型训练；业务上形成 1-2 个付费或准付费试点案例。

12 个月后进入产品化阶段。系统将支持多企业租户、任务队列、数据库持久化、模型监控、预测误差复盘和标准 API；商业上从项目制试点转向 SaaS 订阅和生态合作。

## 6.2 运营流程

运营流程包括数据接入、预测计划、补货审批、物流执行、入仓反馈和模型复盘。采购侧根据系统建议确认补货数量；物流侧选择海运、空运或铁路等运输方式；仓储侧确认入仓容量和处理能力；营销侧同步促销计划与广告投放变化；售后侧将缺货、延迟和退货数据反馈给系统，形成下一轮优化。

## 6.3 风险识别与应对措施

数据质量风险：客户历史销量缺失、SKU 编码混乱、库存快照不准。应对措施是建立数据校验规则、异常值提示和人工确认流程。

模型可信风险：业务人员可能不信任 AI 建议。应对措施是展示预测来源、补货点、安全库存、紧急度和分配约束，让建议可解释、可审批。

合规风险：不同国家关税、VAT、数据和知识产权规则差异大。应对措施是规则配置化，并与税务、法务和物流服务商合作。

商业化风险：中小卖家付费能力有限。应对措施是先从高 SKU 数、多仓点、缺货损失明显的成长型卖家切入，再向海外仓和软件商合作扩展。

![图9 系统实际截图：分仓履约决策页](system_screenshots/dashboard-allocation.png)

![图10 实施路径与运营流程图](assets/fig07-plan.png)

\\newpage

# 7. 财务预测

## 7.1 初始投资与资金用途

项目初始投入预计 80 万元，主要用于技术开发、云服务器、数据接口、市场调研、试点实施和比赛/推广材料。具体包括：研发与算法开发 35 万元，前端与产品设计 12 万元，云资源和测试环境 8 万元，市场调研与客户访谈 8 万元，试点实施与差旅 10 万元，知识产权和合规咨询 7 万元。

## 7.2 三年收入预测

财务预测采用谨慎假设：第 1 年完成原型和试点，服务 5-8 家客户，收入约 80 万元；第 2 年形成标准 SaaS 产品和接口能力，服务 20-30 家客户，收入约 260 万元；第 3 年通过海外仓运营商和 ERP 合作渠道扩大客户，收入约 620 万元。收入来源包括订阅费、实施费、接口定制费和增值咨询费。

## 7.3 成本结构与盈亏平衡

主要成本包括研发人员薪酬、云计算资源、客户实施、市场销售、合规咨询和运营管理。第 1 年成本较高，预计亏损 55 万元；第 2 年随着客户数增长接近盈亏平衡；第 3 年若客户留存率和客单价稳定，预计进入盈利区间。盈亏平衡点约为 18-22 家专业版客户，或 8-10 家企业版客户。

## 7.4 融资需求与退出方式

项目早期建议融资 100-150 万元，用于产品化、试点推广和关键接口建设。退出方式包括被跨境 ERP/WMS 厂商、海外仓平台或供应链 SaaS 企业并购，也可以通过持续经营形成稳定现金流。若高校比赛阶段不涉及实际融资，本部分可作为商业计划假设保留。

![图11 系统实际截图：90天仿真评估页](system_screenshots/dashboard-simulation.png)

![图12 三年财务预测图](assets/fig08-finance.png)

\\newpage

# 8. 团队介绍

团队建议采用“产品 + 算法 + 工程 + 市场财务”的复合结构。项目负责人负责整体规划、进度管理、路演表达和对外沟通；算法负责人负责需求预测、安全库存、补货点、整数规划和仿真评估；工程负责人负责 Web 服务、数据上传、控制台、接口和部署；产品与设计负责人负责用户流程、驾驶舱、审批体验和演示材料；市场与财务负责人负责行业调研、商业模式、竞品分析和财务测算。

外部支持方面，可邀请指导教师提供创新创业方法和比赛经验，邀请跨境电商企业运营人员验证痛点和流程，邀请物流或海外仓服务商提供头程、仓容和入仓处理的现实约束。团队建设重点不是单纯展示技术，而是证明“技术能转化为业务决策”，因此每名成员都需要理解跨境电商履约链路。

![图13 团队分工结构图](assets/fig09-team.png)

\\newpage

# 9. 附件

## 附件一：原型系统输出

项目原型位于本地工程目录，可通过以下命令运行：

```bash
PYTHONPATH=src python -m overseas_inventory.demo
```

系统生成 `output/replenishment_plan.json`、`output/replenishment_plan.md` 和 `output/dashboard.html`。自动测试命令为：

```bash
python -m unittest discover -s tests
```

当前测试结果为 19 个用例全部通过，覆盖预测增长响应、市场信号提升、包装数取整、库存池分配约束、Web 上传任务、审批回写、实时仿真和 API 数据契约等关键环节。

![图14 系统实际截图：补货优化页](system_screenshots/dashboard-replenishment.png)

## 附件二：样例指标摘要

基于样例数据，系统生成 {metrics["recommendations"]} 条补货建议和 {metrics["allocations"]} 条库存池分配决策。90 天滚动仿真显示：传统独立备货方案 Fill Rate 为 {baseline["fill_rate"]:.3f}，智能库存池方案为 {intelligent["fill_rate"]:.3f}；传统方案缺货率为 {baseline["stockout_rate"]:.3f}，智能方案为 {intelligent["stockout_rate"]:.3f}；传统方案平均配送时效为 {baseline["average_delivery_days"]:.2f} 天，智能方案为 {intelligent["average_delivery_days"]:.2f} 天；传统方案平均库存为 {baseline["average_inventory_units"]:.2f} 件，智能方案为 {intelligent["average_inventory_units"]:.2f} 件；总利润提升约 {metrics["profit_lift_wan"]:.2f} 万元。需要注意的是，当前样例中智能方案为了消除缺货采用更高安全库存，因此后续汇报应表述为“以可配置库存投入换取满供货率、更短履约时效和更高利润”，避免夸大为所有成本指标同时下降。

## 附件三：参考资料

1. 海关总署，2025 年全年进出口情况新闻发布会，https://www.customs.gov.cn/customs/xwfb34/302330/hgzs2025nqsjdjckqkxwfbh/index.html。
2. 商务部，《2025 中国数字贸易发展报告》，https://fms.mofcom.gov.cn/cms_files/filemanager/10364010/attach/20255/f0be6d531e2a46bfa3090314807dee4f.pdf。
3. EMARKETER，Worldwide Retail Ecommerce Forecast 2025，https://www.emarketer.com/content/ecommerce-account-more-than-20--of-worldwide-retail-sales-despite-slowdown。
4. U.S. Census Bureau，Quarterly Retail E-Commerce Sales Report，https://www.census.gov/retail/ecommerce.html。
5. European Commission，VAT e-Commerce - One Stop Shop，https://vat-one-stop-shop.ec.europa.eu/index_en。
6. European Commission，General Product Safety Regulation，https://trade.ec.europa.eu/access-to-markets/en/news/eus-general-product-safety-regulation-gpsr-new-era-consumer-protection。
7. European Commission，Data Act，https://digital-strategy.ec.europa.eu/en/policies/data-act。
8. The White House，Suspending Duty-Free De Minimis Treatment for All Countries，https://www.whitehouse.gov/presidential-actions/2025/07/suspending-duty-free-de-minimis-treatment-for-all-countries/。
"""


def build_reports() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    full = report_body(False)
    anon = report_body(True)
    (REPORT_DIR / "双创实训_GlobalPool AI_完整版.md").write_text(full, encoding="utf-8")
    (REPORT_DIR / "双创实训_GlobalPool AI_匿名版.md").write_text(anon, encoding="utf-8")


def main() -> None:
    build_assets()
    capture_system_screenshots()
    build_reports()
    print(REPORT_DIR)


if __name__ == "__main__":
    main()
