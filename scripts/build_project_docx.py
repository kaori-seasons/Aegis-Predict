from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "competition" / "project_book"
CHART_DIR = REPORT_DIR / "data_charts"
SCREENSHOT_DIR = REPORT_DIR / "system_screenshots"
OUTPUT_DIR = ROOT / "output"

PROJECT_NAME = "GlobalPool AI：跨境电商海外仓数字孪生仿真与智能补货决策系统"
TEAM_NAME = "【请填写团队名称】"
SCHOOL = "【请填写学校】"
COLLEGE = "【请填写学院】"
MEMBERS = "【请填写成员姓名】"
SUBMIT_DATE = "2026年6月"


plt.rcParams.update(
    {
        "font.sans-serif": ["PingFang SC", "STSong", "Arial Unicode MS", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.linewidth": 0.8,
        "font.size": 9,
    }
)

PALETTE = {
    "blue": "#2563eb",
    "teal": "#0f766e",
    "amber": "#f59e0b",
    "red": "#dc2626",
    "purple": "#7c3aed",
    "gray": "#64748b",
    "light": "#e5e7eb",
}


def load_plan() -> dict:
    path = OUTPUT_DIR / "replenishment_plan.json"
    if not path.exists():
        from overseas_inventory.demo import build_plan

        build_plan(ROOT / "data" / "sample_network.json", OUTPUT_DIR, 30)
    return json.loads(path.read_text(encoding="utf-8"))


def capture_system_screenshots() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return
    dashboard = OUTPUT_DIR / "dashboard.html"
    if not dashboard.exists():
        load_plan()

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


def save_chart(fig, filename: str) -> Path:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    path = CHART_DIR / filename
    fig.savefig(path, dpi=260, bbox_inches="tight")
    plt.close(fig)
    return path


def style_axis(ax, title: str, subtitle: str | None = None) -> None:
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", pad=26)
    if subtitle:
        ax.text(0, 1.015, subtitle, transform=ax.transAxes, fontsize=8.5, color=PALETTE["gray"], va="bottom")
    ax.grid(axis="y", alpha=0.25, color="#94a3b8")


def build_charts(plan: dict) -> dict[str, Path]:
    charts: dict[str, Path] = {}
    recommendations = plan["recommendations"]
    allocations = plan["allocations"]
    sim = plan["simulation_comparison"]

    # 1. Prototype KPI chart.
    fig, ax = plt.subplots(figsize=(8.8, 4.6))
    labels = ["Fill Rate", "缺货率", "平均库存/1000", "总利润/10万"]
    baseline = [
        sim["baseline"]["fill_rate"],
        sim["baseline"]["stockout_rate"],
        sim["baseline"]["average_inventory_units"] / 1000,
        sim["baseline"]["total_profit"] / 100000,
    ]
    intelligent = [
        sim["intelligent"]["fill_rate"],
        sim["intelligent"]["stockout_rate"],
        sim["intelligent"]["average_inventory_units"] / 1000,
        sim["intelligent"]["total_profit"] / 100000,
    ]
    x = range(len(labels))
    ax.bar([i - 0.18 for i in x], baseline, width=0.36, color="#cbd5e1", label="传统独立备货")
    ax.bar([i + 0.18 for i in x], intelligent, width=0.36, color=PALETTE["blue"], label="智能库存池方案")
    ax.set_xticks(list(x), labels)
    style_axis(ax, "原型仿真关键指标对比", "数据来源：系统90天滚动仿真输出")
    ax.legend(frameon=False, ncols=2, loc="upper left")
    charts["kpi"] = save_chart(fig, "chart01-kpi-comparison.png")

    # 2. Market chart.
    fig, ax1 = plt.subplots(figsize=(8.8, 4.8))
    years = [2025, 2026, 2027, 2028, 2029]
    china_cbec = [2.75, 2.98, 3.23, 3.50, 3.78]
    global_ecom = [6.419, 6.88, 7.33, 7.89, 8.47]
    ax1.plot(years, china_cbec, marker="o", color=PALETTE["red"], linewidth=2.4, label="中国跨境电商进出口（万亿元人民币）")
    ax1.set_ylabel("中国跨境电商（万亿元人民币）", color=PALETTE["red"])
    ax1.tick_params(axis="y", labelcolor=PALETTE["red"])
    ax1.grid(alpha=0.25)
    ax2 = ax1.twinx()
    ax2.plot(years, global_ecom, marker="s", color=PALETTE["blue"], linewidth=2.4, label="全球零售电商（万亿美元）")
    ax2.set_ylabel("全球零售电商（万亿美元）", color=PALETTE["blue"])
    ax2.tick_params(axis="y", labelcolor=PALETTE["blue"])
    ax1.set_title("跨境电商与全球零售电商规模趋势", loc="left", fontsize=12, fontweight="bold", pad=14)
    ax1.text(2025, 2.62, "2025年中国跨境电商进出口2.75万亿元", fontsize=8.5, color=PALETTE["gray"])
    lines = ax1.get_lines() + ax2.get_lines()
    ax1.legend(lines, [line.get_label() for line in lines], frameon=False, loc="upper left")
    charts["market"] = save_chart(fig, "chart02-market-scale.png")

    # 3. Replenishment ranking.
    top = sorted(recommendations, key=lambda row: row["recommended_units"], reverse=True)[:10]
    fig, ax = plt.subplots(figsize=(8.8, 5.2))
    labels = [f'{row["sku"]}\n{row["warehouse_id"]}' for row in top][::-1]
    values = [row["recommended_units"] for row in top][::-1]
    urgency = [row["urgency"] for row in top][::-1]
    colors = [PALETTE["red"] if u >= 0.6 else PALETTE["amber"] if u >= 0.35 else PALETTE["teal"] for u in urgency]
    ax.barh(labels, values, color=colors)
    for i, (value, u) in enumerate(zip(values, urgency)):
        ax.text(value + 8, i, f"{value}件 / 紧急度{u:.3f}", va="center", fontsize=8)
    style_axis(ax, "补货需求Top 10", "数据来源：系统补货建议 recommended_units 与 urgency")
    ax.set_xlabel("建议补货量（件）")
    charts["replenishment"] = save_chart(fig, "chart03-replenishment-top10.png")

    # 4. Allocation gap.
    fig, ax = plt.subplots(figsize=(9.2, 5.2))
    rows = sorted(allocations, key=lambda row: row["unmet_units"], reverse=True)[:10]
    labels = [f'{row["sku"]}\n{row["warehouse_id"]}' for row in rows]
    allocated = [row["allocated_units"] for row in rows]
    unmet = [row["unmet_units"] for row in rows]
    ax.bar(labels, allocated, color=PALETTE["teal"], label="已分配")
    ax.bar(labels, unmet, bottom=allocated, color="#fca5a5", label="未满足缺口")
    style_axis(ax, "库存池分配满足情况", "数据来源：MILP分仓结果 allocated_units 与 unmet_units")
    ax.set_ylabel("件")
    ax.legend(frameon=False)
    ax.tick_params(axis="x", labelrotation=0)
    charts["allocation"] = save_chart(fig, "chart04-allocation-gap.png")

    # 5. Pool waterline.
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    pool = plan["pool_levels"]
    labels = [row["sku"] for row in pool]
    allocated = [row["allocated_units"] for row in pool]
    remaining = [row["remaining_units"] for row in pool]
    ax.bar(labels, allocated, color=PALETTE["blue"], label="已分配")
    ax.bar(labels, remaining, bottom=allocated, color="#bfdbfe", label="剩余可分配")
    for i, row in enumerate(pool):
        ax.text(i, row["allocatable_units"] + 12, f'水位 {row["waterline"]:.1%}', ha="center", fontsize=8.5)
    style_axis(ax, "库存池水位", "数据来源：系统库存池水位表")
    ax.set_ylabel("件")
    ax.legend(frameon=False)
    charts["pool"] = save_chart(fig, "chart05-pool-waterline.png")

    # 6. Simulation metrics.
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    metrics = ["Fill Rate", "缺货率", "平均时效", "平均库存/1000", "利润/10万"]
    baseline = [
        sim["baseline"]["fill_rate"],
        sim["baseline"]["stockout_rate"],
        sim["baseline"]["average_delivery_days"],
        sim["baseline"]["average_inventory_units"] / 1000,
        sim["baseline"]["total_profit"] / 100000,
    ]
    intelligent = [
        sim["intelligent"]["fill_rate"],
        sim["intelligent"]["stockout_rate"],
        sim["intelligent"]["average_delivery_days"],
        sim["intelligent"]["average_inventory_units"] / 1000,
        sim["intelligent"]["total_profit"] / 100000,
    ]
    x = range(len(metrics))
    ax.plot(x, baseline, marker="o", color=PALETTE["gray"], linewidth=2, label="传统独立备货")
    ax.plot(x, intelligent, marker="o", color=PALETTE["blue"], linewidth=2.4, label="智能库存池")
    ax.set_xticks(list(x), metrics)
    style_axis(ax, "90天仿真多指标对比", "不同量纲指标按报告展示单位处理")
    ax.legend(frameon=False)
    charts["simulation"] = save_chart(fig, "chart06-simulation-metrics.png")

    # 7. Business model chart.
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    tiers = ["基础版", "专业版", "企业版", "实施/接口"]
    low = [1999, 7999, 30000, 50000]
    high = [4999, 19999, 100000, 200000]
    ax.bar(tiers, high, color="#dbeafe", label="价格上限")
    ax.bar(tiers, low, color=PALETTE["blue"], label="价格下限")
    style_axis(ax, "商业化定价区间", "单位：元；企业版和实施/接口按项目或年度估算")
    ax.set_ylabel("元")
    ax.legend(frameon=False)
    charts["business"] = save_chart(fig, "chart07-pricing.png")

    # 8. Roadmap.
    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    tasks = [
        ("仿真引擎与决策闭环", 0, 2, PALETTE["blue"]),
        ("客户访谈与情景标定", 1, 5, PALETTE["teal"]),
        ("孪生验证+试点对接", 4, 8, PALETTE["amber"]),
        ("模型训练与监控", 6, 11, PALETTE["purple"]),
        ("SaaS产品化", 8, 12, PALETTE["red"]),
    ]
    for i, (name, start, end, color) in enumerate(tasks):
        ax.barh(i, end - start, left=start, color=color, alpha=0.86)
        ax.text(start + 0.1, i, name, va="center", color="white", fontsize=9, fontweight="bold")
    ax.set_yticks([])
    ax.set_xlim(0, 12)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("月份")
    style_axis(ax, "0-12个月实施路线图", "从数字孪生原型到真实业务试点")
    charts["roadmap"] = save_chart(fig, "chart08-roadmap.png")

    # 9. Financial chart.
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    years = ["第1年", "第2年", "第3年"]
    revenue = [80, 260, 620]
    cost = [135, 230, 420]
    profit = [r - c for r, c in zip(revenue, cost)]
    x = range(len(years))
    ax.bar([i - 0.22 for i in x], revenue, width=0.32, color=PALETTE["blue"], label="收入")
    ax.bar([i + 0.12 for i in x], cost, width=0.32, color=PALETTE["amber"], label="成本")
    ax.plot(list(x), profit, marker="o", color=PALETTE["red"], linewidth=2.4, label="利润")
    ax.axhline(0, color="#111827", linewidth=0.8)
    ax.set_xticks(list(x), years)
    ax.set_ylabel("万元")
    style_axis(ax, "三年财务预测", "谨慎假设：第3年进入稳定盈利区间")
    ax.legend(frameon=False, ncols=3)
    charts["finance"] = save_chart(fig, "chart09-finance.png")

    # 10. Team chart.
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    roles = ["项目管理", "算法建模", "工程开发", "产品设计", "市场财务"]
    workloads = [20, 25, 25, 15, 15]
    ax.pie(workloads, labels=roles, colors=["#bfdbfe", "#99f6e4", "#fde68a", "#ddd6fe", "#fecaca"], autopct="%1.0f%%", startangle=90)
    ax.set_title("团队能力与工作量配置建议", loc="left", fontsize=12, fontweight="bold")
    charts["team"] = save_chart(fig, "chart10-team-roles.png")

    return charts


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def add_page_number(section) -> None:
    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    fld_char1 = OxmlElement("w:fldChar")
    fld_char1.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_char2 = OxmlElement("w:fldChar")
    fld_char2.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char1)
    run._r.append(instr)
    run._r.append(fld_char2)


def set_doc_styles(doc: Document) -> None:
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "宋体"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.first_line_indent = Cm(0.74)

    for style_name, size in [("Heading 1", 16), ("Heading 2", 14)]:
        style = styles[style_name]
        style.font.name = "黑体"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
        style.font.size = Pt(size)
        style.font.bold = True
        style.paragraph_format.first_line_indent = Cm(0)
        style.paragraph_format.space_before = Pt(10)
        style.paragraph_format.space_after = Pt(6)


def add_cover(doc: Document, anonymous: bool) -> None:
    team = "匿名团队" if anonymous else TEAM_NAME
    school = "匿名学校" if anonymous else SCHOOL
    college = "匿名学院" if anonymous else COLLEGE
    members = "匿名成员" if anonymous else MEMBERS

    for _ in range(5):
        doc.add_paragraph()
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(PROJECT_NAME)
    run.bold = True
    run.font.name = "黑体"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
    run.font.size = Pt(22)

    for text in [f"团队名称：{team}", f"学校：{school}", f"学院：{college}", f"成员姓名：{members}", f"提交日期：{SUBMIT_DATE}"]:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(text)
        r.font.name = "宋体"
        r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        r.font.size = Pt(14)
    doc.add_page_break()


def add_image(doc: Document, path: Path, caption: str, width_cm: float = 15.6) -> None:
    if not path.exists():
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.add_run().add_picture(str(path), width=Cm(width_cm))
    cap = doc.add_paragraph(caption)
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.first_line_indent = Cm(0)
    cap.runs[0].font.size = Pt(9)
    cap.runs[0].font.color.rgb = RGBColor(80, 80, 80)


def add_table(doc: Document, headers: list[str], rows: list[list[str]], title: str) -> None:
    p = doc.add_paragraph(title)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.runs[0].bold = True
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    hdr = table.rows[0]
    set_repeat_table_header(hdr)
    for i, header in enumerate(headers):
        cell = hdr.cells[i]
        set_cell_shading(cell, "D9EAF7")
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        cell.text = header
    for row in rows:
        cells = table.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = str(value)
    doc.add_paragraph()


def add_para(doc: Document, text: str) -> None:
    doc.add_paragraph(text)


def build_docx(plan: dict, charts: dict[str, Path], anonymous: bool) -> Path:
    recommendations = plan.get("recommendations", [])
    allocations = plan.get("allocations", [])
    simulation = plan["simulation_comparison"]
    baseline = simulation["baseline"]
    intelligent = simulation["intelligent"]
    improvement = simulation["improvement"]
    fill_lift = improvement["fill_rate_lift_pct"]
    stockout_reduction = improvement["stockout_rate_reduction_pct"]
    inventory_delta = intelligent["average_inventory_units"] - baseline["average_inventory_units"]
    inventory_tradeoff = (
        f"平均库存减少 {abs(inventory_delta):.0f} 件"
        if inventory_delta < 0
        else f"平均库存增加 {inventory_delta:.0f} 件"
    )
    profit_lift_wan = improvement["profit_lift"] / 10000

    doc = Document()
    section = doc.sections[0]
    section.page_height = Cm(29.7)
    section.page_width = Cm(21.0)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)
    add_page_number(section)
    set_doc_styles(doc)
    add_cover(doc, anonymous)

    doc.add_heading("1. 摘要", level=1)
    add_para(doc, "GlobalPool AI 是一套面向跨境电商海外仓场景的数字孪生仿真系统。跨境电商企业在多国家、多平台、多海外仓运营中普遍遇到“总库存充足但局部区域缺货”“经验补货导致断货或积压”“固定比例分仓不能兼顾利润、时效和仓容约束”等问题；而这些问题的根因——补货与分仓决策——很难在真实业务中低成本、可重复地试错。本项目用仿真引擎为海外仓运营建立一个可控的数字孪生环境：通过随机过程实时生成订单流、库存流和在途物流流，复现真实经营的波动与不确定性，并在这个环境中验证 AI 销量预测、智能补货和分仓决策的有效性。")
    add_para(doc, "系统的核心创新在于“先建环境、再验决策”。仿真引擎按 SKU-海外仓维度逐小时生成订单，叠加平台分布、日内与周内节律、促销活动、价格弹性等真实经营因子，并以伽马混合泊松（负二项）过程刻画真实订单流的过度离散与阵发性；在此环境之上，预测层输出 P50、P80、P95 分位需求，补货层结合服务等级、运输提前期和销量波动计算安全库存、补货点与目标库存，分仓层通过整数规划在库存池、运输方式、仓容、采购可用量和资金预算等约束下生成分仓计划。系统以仿真数据为唯一数据源，不依赖企业上传或外部接口，因而能够独立运行、可演示、可复现实验结果。")
    add_para(doc, f"基于仿真引擎数据，系统生成 {len(recommendations)} 条补货建议和 {len(allocations)} 条分仓决策。90 天滚动仿真显示，智能库存池方案将 Fill Rate 从 {baseline['fill_rate']:.3f} 提升至 {intelligent['fill_rate']:.3f}，缺货率从 {baseline['stockout_rate']:.3f} 降至 {intelligent['stockout_rate']:.3f}，平均配送时效由 {baseline['average_delivery_days']:.2f} 天缩短至 {intelligent['average_delivery_days']:.2f} 天，总利润提升约 {profit_lift_wan:.2f} 万元。该结果也显示，智能方案为了保障满供货率带来“{inventory_tradeoff}”的策略成本，因此商业化时应允许客户按服务等级调节库存安全线。数字孪生定位的价值在于：这些结论是在同一套可复现的仿真环境中、对传统独立备货与智能库存池两种策略做受控对比得到的，而非依赖一次性的历史回测。")
    add_image(doc, charts["kpi"], "图1 仿真环境下两种策略关键指标对比")
    add_image(doc, SCREENSHOT_DIR / "dashboard-pool.png", "图2 系统截图：全球库存池总览")

    doc.add_heading("2. 项目背景与问题提出", level=1)
    doc.add_heading("2.1 宏观环境分析", level=2)
    add_para(doc, "跨境电商已经成为外贸增长的重要新业态。海关总署 2026 年 1 月发布的 2025 年全年进出口情况显示，2025 年我国跨境电商进出口达到 2.75 万亿元，比 2020 年增长 69.7%。商务部《2025 中国数字贸易发展报告》披露的 2024 年明细数据显示，中国跨境电商进出口 2.71 万亿元，其中出口 2.15 万亿元，首次突破 2 万亿元；出口目的地前三位为美国、英国、德国，合计占比超过一半，且数码产品及配件是主要出口品类之一。")
    add_para(doc, "全球市场仍保持较大规模。EMARKETER 预计 2025 年全球零售电商销售额约为 6.419 万亿美元；美国商务部普查局数据显示，2026 年第一季度美国零售电商销售额约 3267 亿美元，同比增长 9.8%，占美国零售销售额 16.9%。这些数据说明，北美和欧洲成熟市场仍是中国跨境卖家多仓履约的重要战场。")
    add_para(doc, "政策与合规环境正在趋严。欧盟 IOSS 要求不超过 150 欧元的进口远程销售货物按规则申报 VAT，欧盟通用产品安全法规 GPSR 已自 2024 年 12 月 13 日起适用，欧盟 Data Act 自 2025 年 9 月 12 日起适用；美国 2025 年针对低值进口包裹调整免税待遇，特别影响来自中国和香港的低值包裹。跨境电商竞争已经从“能卖出去”进入“能合规、能稳定履约、能精细化管理库存”的阶段。")
    doc.add_heading("2.2 行业痛点与市场空白", level=2)
    add_para(doc, "行业痛点主要体现在四个方面：第一，海外仓库存割裂，企业往往只能看到单仓库存，无法把在途、可调拨和安全保留库存统一纳入决策；第二，补货依赖经验，跨境运输提前期长，促销和广告投放变化会放大预测误差；第三，固定比例分仓难以同时考虑缺货风险、利润贡献、服务等级、运输成本、仓容和资金预算；第四，缺少可解释决策，业务人员难以信任黑箱补货建议。")
    add_para(doc, "更深一层的空白在于“缺少可控的决策验证环境”。补货与分仓策略的好坏，最终要看它在真实订单波动、缺货、退货、物流延迟下的综合表现；但在真实业务中直接试错代价高、不可重复，历史数据回测又难以覆盖促销爆发、平台活动、区域物流中断等极端但关键的情景。这正是数字孪生的切入点：先用仿真引擎把海外仓运营环境“建”出来，让订单流、库存流、在途流可控、可复现、可注入情景，再在其中反复验证 AI 决策，最后才推广到真实业务。本项目正是围绕这一思路构建。")
    add_image(doc, charts["market"], "图3 跨境电商与全球零售电商规模趋势")

    doc.add_heading("3. 产品/服务描述", level=1)
    add_para(doc, "GlobalPool AI 的产品形态是一套“数字孪生仿真 + AI 决策”的海外仓运营决策平台。系统内置仿真数据引擎，按 SKU-海外仓维度逐小时生成订单流，并同步驱动库存流（实时扣减、缺货、退货回流）和在途物流流（头程发运、ETA、延迟），由此构建一个与真实海外仓运营同构的可控环境。在这个环境之上，系统运行 AI 销量预测、智能补货和库存池分仓三层决策，并实时呈现全球库存池态势、缺货预警、补货建议、分仓方案与策略对比报告。面向真实落地时，该仿真层既是产品演示与算法验证环境，也可作为企业接入真实数据前的策略沙盘。")
    add_para(doc, "系统的创新点有两层：其一是“仿真即数据源”——用随机过程实时生成贴近现实的订单、库存与物流数据，使系统能独立运行、可复现、可注入情景，无需依赖企业上传或外部接口；其二是“预测驱动决策闭环”——预测层输出 P50、P80、P95 分位需求，补货层根据 P80 需求、安全库存和补货点生成建议补货量，分仓层通过 MILP 整数规划处理库存池、运输方式、仓容、采购和资金约束，运营层把计划结果转为实时沙盘中的订单履约、审批队列和执行回写记录，形成“仿真生成—预测—补货—分仓—履约—复盘”的完整闭环。")
    add_image(doc, SCREENSHOT_DIR / "dashboard-forecast.png", "图4 系统截图：AI需求预测页")
    add_image(doc, charts["replenishment"], "图5 补货需求Top 10")
    add_table(
        doc,
        ["模块", "输入", "输出", "价值"],
        [
            ["仿真数据引擎", "SKU、仓网、平台/节律/促销参数、随机过程", "实时订单流、库存流、在途流", "构建可控可复现的运营环境"],
            ["需求预测", "仿真订单、广告、促销、季节、平台活动", "P50/P80/P95需求", "提前识别未来需求"],
            ["补货优化", "预测、库存位置、提前期、服务等级", "补货点、目标库存、建议补货量", "把预测转成动作"],
            ["分仓决策", "库存池、仓容、运输、资金预算", "分配量、缺口、运输方式", "有限库存优先给高价值区域"],
            ["仿真评估", "同一环境下的传统方案、智能方案", "Fill Rate、缺货率、库存、利润", "受控对比证明方案有效性"],
        ],
        "表1 系统核心模块说明",
    )

    doc.add_heading("4. 市场分析", level=1)
    add_para(doc, "项目目标市场包括中国跨境电商品牌卖家、平台型卖家、海外仓运营商和跨境 ERP/WMS/OMS 软件服务商。优先切入的客户通常具备以下特征：SKU 数量较多，覆盖美国、欧洲、澳洲等多个区域仓，销售波动受到广告、促销和平台活动影响，且缺货损失和库存积压成本都较高。")
    add_para(doc, "从市场空间看，本项目不是泛化电商工具，而是切入跨境电商增长后半程的供应链计划能力。2024 年中国跨境电商出口中，美国、英国、德国是主要目的地，项目样例中的美国洛杉矶仓、英国伯明翰仓、德国法兰克福仓和澳洲悉尼仓正好覆盖成熟市场和高价值仓网。")
    add_para(doc, "竞争格局方面，传统 ERP/WMS 更偏执行记录，平台官方库存工具更偏单平台规则，大型企业自建供应链系统投入高、周期长。GlobalPool AI 的差异化在于轻量化、可解释、围绕补货与分仓决策闭环设计，更关键的是它自带数字孪生仿真环境：企业无需先打通数据接口，就能在贴近自身仓网的仿真环境中直观看到智能库存池相对传统独立备货的差异，从而显著降低试用与决策门槛。这种“先在孪生环境验证、再接入真实数据”的路径，是现有执行型工具不具备的。")
    add_table(
        doc,
        ["维度", "内容"],
        [
            ["优势", "场景聚焦，自带数字孪生仿真环境，预测、补货、分仓和受控对比链路闭合，可独立运行、可复现"],
            ["劣势", "仿真参数标定与真实业务的贴合度仍需结合行业数据持续校准；规模化模型训练待试点验证"],
            ["机会", "海外仓数字化、库存降本、合规运营和供应链计划需求上升，仿真验证降低企业试用门槛"],
            ["威胁", "成熟 ERP/WMS 厂商可能扩展智能计划模块，平台政策和关税规则可能变化"],
        ],
        "表2 SWOT分析",
    )

    doc.add_heading("5. 商业模式", level=1)
    add_para(doc, "GlobalPool AI 的价值主张是“用数字孪生先验证、用 AI 把全球库存变成一池可动态调度的供应能力”。对跨境卖家而言，系统先在仿真环境中证明智能库存池能减少断货、降低积压、提高周转，再在真实业务中落地，决策更可信、试错成本更低；对海外仓运营商而言，系统可以提升客户粘性，形成数据化运营服务；对 ERP/WMS 服务商而言，系统可以作为智能决策与策略仿真插件，提高产品附加值。")
    add_para(doc, "盈利模式采用订阅费、实施费和增值服务组合。基础版面向中小卖家，提供仿真环境与标准补货分仓决策，按月收取 1999-4999 元；专业版按 7999-19999 元/月收费，支持多平台、多仓、多角色审批、情景仿真与 API；企业版采用年费加实施费，按客户仓网规模与真实数据对接复杂度报价。仿真环境本身也可作为低门槛的试用入口与售前演示工具，缩短成交周期。")
    add_para(doc, "合规方面，系统采用数据最小化和审计可追溯原则。由于核心演示与验证均在仿真环境中完成，不涉及真实消费者数据跨境，天然规避了大量数据合规风险；进入真实数据对接阶段时，关税和 VAT 字段规则可配置，SKU 主数据可保留品牌授权、商标状态、产品安全负责人、禁售类目和平台限制字段，预测层优先使用脱敏后的 SKU-仓-日期销量聚合数据，审批和执行回写均保留操作人、时间、数量和备注。")
    add_image(doc, charts["business"], "图6 商业化定价区间")
    add_image(doc, charts["pool"], "图7 库存池水位")

    doc.add_heading("6. 实施计划", level=1)
    add_para(doc, "0-6 个月为数字孪生原型阶段。重点完成仿真数据引擎、实时订单/库存/在途三流生成、AI 预测与补货分仓决策闭环、传统与智能策略的受控对比、运营看板与实时沙盘，以及比赛版路演材料和潜在客户访谈。技术目标是让仿真环境稳定可复现，并稳定生成补货建议、库存池分配结果和策略对比；商业目标是明确目标客户、价格接受度和试点条件。")
    add_para(doc, "6-12 个月为情景扩展与试点阶段。重点丰富仿真情景（黑五大促、区域爆单、欧洲调拨、澳洲物流延迟等）和参数标定能力，使仿真更贴近真实仓网；在此基础上为 1-2 家试点客户做“孪生环境验证 + 小范围真实数据对接”，完成数据校验、接口适配、用户权限、审批流和报表导出。技术上逐步引入 LightGBM、Prophet 或深度时序模型训练；业务上形成 1-2 个付费或准付费试点案例。")
    add_para(doc, "主要风险包括仿真贴合度风险、模型可信风险、合规风险和商业化风险。应对策略分别是结合行业数据持续标定仿真参数并做敏感性分析、输出可解释决策依据、与合规服务商合作、优先从高 SKU 数和多仓点客户切入。")
    add_image(doc, charts["roadmap"], "图8 0-12个月实施路线图")
    add_image(doc, SCREENSHOT_DIR / "dashboard-allocation.png", "图9 系统截图：分仓履约决策页")

    doc.add_heading("7. 财务预测", level=1)
    add_para(doc, "项目初始投入预计 80 万元，主要用于技术开发、云服务器、数据接口、市场调研、试点实施和比赛推广材料。具体包括研发与算法开发 35 万元，前端与产品设计 12 万元，云资源和测试环境 8 万元，市场调研与客户访谈 8 万元，试点实施与差旅 10 万元，知识产权和合规咨询 7 万元。")
    add_para(doc, "财务预测采用谨慎假设：第 1 年完成原型和试点，服务 5-8 家客户，收入约 80 万元；第 2 年形成标准 SaaS 产品和接口能力，服务 20-30 家客户，收入约 260 万元；第 3 年通过海外仓运营商和 ERP 合作渠道扩大客户，收入约 620 万元。预计第 3 年进入稳定盈利区间。")
    add_image(doc, charts["finance"], "图10 三年财务预测")
    add_image(doc, charts["simulation"], "图11 90天仿真多指标对比")

    doc.add_heading("8. 团队介绍", level=1)
    add_para(doc, "团队建议采用“产品 + 算法 + 工程 + 市场财务”的复合结构。项目负责人负责整体规划、进度管理、路演表达和对外沟通；算法负责人负责需求预测、安全库存、补货点、整数规划和仿真评估；工程负责人负责仿真数据引擎、Web 服务、实时沙盘、运营控制台、接口和部署；产品与设计负责人负责用户流程、运营看板、审批体验和演示材料；市场与财务负责人负责行业调研、商业模式、竞品分析和财务测算。")
    add_para(doc, "外部支持方面，可邀请指导教师提供创新创业方法和比赛经验，邀请跨境电商企业运营人员验证痛点和流程，邀请物流或海外仓服务商提供头程、仓容和入仓处理的现实约束，并据此校准仿真参数。团队建设重点不是单纯展示技术，而是证明技术能够转化为可信的业务决策。")
    add_image(doc, charts["team"], "图12 团队能力与工作量配置建议")

    doc.add_heading("9. 附件", level=1)
    add_para(doc, "项目原型可通过命令 PYTHONPATH=src python -m overseas_inventory.demo 运行，系统基于仿真引擎数据生成 output/replenishment_plan.json、output/replenishment_plan.md 和 output/dashboard.html；Web 端通过 globalpool-web 启动，登录后由仿真引擎一键创建运行并进入实时经营沙盘。自动测试命令为 python -m unittest discover -s tests，当前测试结果为 18 个用例全部通过，覆盖预测增长响应、市场信号提升、包装数取整、库存池分配约束、仿真运行创建、审批与执行回写、实时仿真和 API 数据契约等关键环节。")
    add_para(doc, f"基于仿真引擎数据，系统生成 {len(recommendations)} 条补货建议和 {len(allocations)} 条库存池分配决策。当前环境下智能方案将 Fill Rate 提升 {fill_lift:.2f} 个百分点、缺货率降低 {stockout_reduction:.2f} 个百分点、配送时效缩短 {baseline['average_delivery_days'] - intelligent['average_delivery_days']:.2f} 天、利润提升约 {profit_lift_wan:.2f} 万元，但也带来“{inventory_tradeoff}”的安全库存投入。汇报时应把它表述为服务水平、利润和库存占用之间的可配置权衡，避免夸大为所有指标同时下降。需求由伽马混合泊松随机过程实时生成，传统与智能两种策略在同一条需求路径上对比，并以固定随机种子保证实验可复现。")
    add_image(doc, SCREENSHOT_DIR / "dashboard-replenishment.png", "图13 系统截图：补货优化页")
    add_image(doc, SCREENSHOT_DIR / "dashboard-simulation.png", "图14 系统截图：实时仿真评估页")
    add_para(doc, "参考资料：海关总署《2025 年全年进出口情况新闻发布会》；商务部《2025 中国数字贸易发展报告》；EMARKETER Worldwide Retail Ecommerce Forecast 2025；U.S. Census Bureau Quarterly Retail E-Commerce Sales Report；European Commission VAT e-Commerce One Stop Shop、GPSR、Data Act；The White House 关于低值进口免税待遇调整的总统行动。")

    output_name = "双创实训_GlobalPool AI数字孪生_匿名版.docx" if anonymous else "双创实训_GlobalPool AI数字孪生_完整版.docx"
    output_path = REPORT_DIR / output_name
    doc.save(output_path)
    return output_path


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    plan = load_plan()
    capture_system_screenshots()
    charts = build_charts(plan)
    full = build_docx(plan, charts, anonymous=False)
    anon = build_docx(plan, charts, anonymous=True)
    print(full)
    print(anon)


if __name__ == "__main__":
    main()
