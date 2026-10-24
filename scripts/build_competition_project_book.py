from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from PIL import Image
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from competition.project_book.polished_paragraphs import (  # noqa: E402
    FIG_1_AFTER,
    FIG_1_BEFORE,
    FIG_2_AFTER,
    FIG_2_BEFORE,
    FIG_3_AFTER,
    FIG_3_BEFORE,
    FIG_4_AFTER,
    FIG_4_BEFORE,
    FIG_5_AFTER,
    FIG_5_BEFORE,
    FIG_6_AFTER,
    FIG_6_BEFORE,
    FIG_7_AFTER,
    FIG_7_BEFORE,
    FIG_8_AFTER,
    FIG_8_BEFORE,
    FIG_9_AFTER,
    FIG_9_BEFORE,
    FIG_10_AFTER,
    FIG_10_BEFORE,
    FIG_11_AFTER,
    FIG_11_BEFORE,
    FIG_12_AFTER,
    FIG_12_BEFORE,
    FIG_13_BEFORE,
    FIG_13_AFTER,
    FIG_14_AFTER,
    FIG_14_BEFORE,
    FIG_SWOT_AFTER,
    FIG_SWOT_BEFORE,
    SECTION_1,
    SECTION_2_1,
    SECTION_2_2,
    SECTION_3_1,
    SECTION_3_2,
    SECTION_3_3,
    SECTION_3_4,
    SECTION_3_5,
    SECTION_3_6,
    SECTION_4,
    SECTION_5,
    SECTION_6,
    SECTION_7,
    SECTION_8,
    SECTION_9,
    TABLE_1_AFTER,
    TABLE_1_BEFORE,
    TABLE_3_AFTER,
    TABLE_3_BEFORE,
)

PROJECT_NAME = "GlobalPool AI：跨境电商海外仓数字孪生仿真与智能补货决策系统"
COURSE_NAME = "国际企业综合模拟"
TEAM_NAME = "CLS"
SCHOOL = "湖南农业大学"
COLLEGE = "商学院"
MEMBERS = "梁思成、何英賢、万梓鹏、苏涛、颜酉津"
MEMBER_ROLES = [
    ("梁思成", "项目统筹、路演、材料整合", "项目统筹、路演、材料整合"),
    ("何英賢", "PPT、演讲稿", "PPT制作、演讲稿撰写"),
    ("万梓鹏", "现场演讲", "负责答辩现场演讲"),
    ("苏涛", "前端、文档与财务", "前端看板、项目书、SWOT、财务与PPT"),
    ("颜酉津", "算法、后端与测试", "算法与数据、补货与分仓、后端接口、实时仿真、测试部署"),
]
SUBMIT_DATE = "2026年6月"

INK = RGBColor(31, 41, 55)
MUTED = RGBColor(85, 95, 110)
BLUE = RGBColor(37, 99, 235)
REPORT_DIR = ROOT / "competition" / "project_book"
ASSET_DIR = REPORT_DIR / "assets"
PRINT_COVER_SRC = ASSET_DIR / "globalpool-cover-print.jpg"
PRINT_COVER_A4 = ASSET_DIR / "globalpool-cover-print-a4.png"
SCREENSHOT_DIR = REPORT_DIR / "system_screenshots"
CHART_DIR = REPORT_DIR / "data_charts"
OUTPUT_DIR = ROOT / "output"

plt.rcParams.update(
    {
        "font.sans-serif": ["PingFang SC", "STSong", "Arial Unicode MS", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
    }
)


def load_plan() -> dict:
    return json.loads((OUTPUT_DIR / "replenishment_plan.json").read_text(encoding="utf-8"))


def build_charts(_: dict) -> dict[str, Path]:
    ensure_custom_charts()
    return {
        "kpi": CHART_DIR / "chart01-kpi-comparison.png",
        "market": CHART_DIR / "chart02-market-scale.png",
        "replenishment": CHART_DIR / "chart03-replenishment-top10.png",
        "allocation": CHART_DIR / "chart04-allocation-gap.png",
        "pool": CHART_DIR / "chart05-pool-waterline-readable.png",
        "simulation": CHART_DIR / "chart06-simulation-metrics.png",
        "business": CHART_DIR / "chart07-pricing.png",
        "roadmap": CHART_DIR / "chart08-roadmap.png",
        "finance": CHART_DIR / "chart09-finance.png",
        "team": CHART_DIR / "chart10-team-roles.png",
        "swot": CHART_DIR / "chart11-swot-strategy-map.png",
    }


def wrap_cn(text: str, width: int = 18) -> str:
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False, replace_whitespace=False))


def save_support_chart(fig, filename: str) -> Path:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    path = CHART_DIR / filename
    fig.savefig(path, dpi=260, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def build_swot_chart() -> Path:
    fig, ax = plt.subplots(figsize=(10.4, 6.2))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(0.03, 0.95, "表2 SWOT 分析可视化", fontsize=18, fontweight="bold", color="#111827")
    ax.text(0.03, 0.905, "围绕课程原型、程序运行能力和后续试点可行性整理", fontsize=10.5, color="#64748b")

    cards = [
        (
            "S",
            "优势 Strengths",
            "#dff7ec",
            "#047857",
            [
                "场景聚焦海外仓补货与分仓，不泛化成普通后台",
                "已有可运行 Web 程序、五页看板、截图和测试用例",
                "预测、补货、分仓、仿真形成完整闭环",
                "建议能解释触发原因、库存水位、审批状态和约束",
            ],
            0.04,
            0.50,
        ),
        (
            "W",
            "劣势 Weaknesses",
            "#fee2e2",
            "#b91c1c",
            [
                "当前主要使用样例与仿真数据，尚未接入真实企业系统",
                "模型参数还需要真实订单、物流和退货数据持续校准",
                "课程团队资源有限，短期更适合验证模型和流程",
                "多租户、权限、数据库和监控仍处后续产品化阶段",
            ],
            0.52,
            0.50,
        ),
        (
            "O",
            "机会 Opportunities",
            "#dbeafe",
            "#1d4ed8",
            [
                "跨境卖家从直邮转向海外仓，本地履约要求提高",
                "促销、广告和平台活动让库存计划更依赖预测",
                "中小卖家缺少轻量的库存池仿真与试算工具",
                "高校实训可用本程序展示数据驱动供应链决策",
            ],
            0.04,
            0.13,
        ),
        (
            "T",
            "威胁 Threats",
            "#fef3c7",
            "#b45309",
            [
                "ERP/WMS 厂商可能加入类似智能补货功能",
                "平台规则、关税、物流价格变化会改变业务参数",
                "真实客户数据质量差会影响预测可信度",
                "如果缺少试点反馈，商业化论证容易停留在演示层",
            ],
            0.52,
            0.13,
        ),
    ]
    for code, title, fill, edge, points, x, y in cards:
        box = FancyBboxPatch(
            (x, y),
            0.43,
            0.31,
            boxstyle="round,pad=0.018,rounding_size=0.025",
            linewidth=1.4,
            edgecolor=edge,
            facecolor=fill,
        )
        ax.add_patch(box)
        ax.text(x + 0.025, y + 0.245, code, fontsize=34, fontweight="bold", color=edge)
        ax.text(x + 0.10, y + 0.265, title, fontsize=14, fontweight="bold", color="#111827")
        for i, point in enumerate(points):
            ax.text(x + 0.05, y + 0.205 - i * 0.047, f"{i + 1}. {wrap_cn(point, 24)}", fontsize=9.8, color="#334155", va="top")

    ax.plot([0.5, 0.5], [0.10, 0.84], color="#cbd5e1", linewidth=1)
    ax.plot([0.03, 0.97], [0.47, 0.47], color="#cbd5e1", linewidth=1)
    ax.text(0.035, 0.06, "结论：课程阶段应突出“可运行、可解释、可复现实验”；商业化表述以小额试点和真实数据验证为主。", fontsize=10.5, color="#0f172a")
    return save_support_chart(fig, "chart11-swot-strategy-map.png")


def build_realistic_finance_chart() -> Path:
    fig, ax = plt.subplots(figsize=(9.2, 5.2))
    stages = ["课程实训", "完善演示", "小额试点"]
    cash_cost = [0.35, 1.20, 4.20]
    potential_income = [0.00, 0.00, 5.00]
    x = range(len(stages))
    ax.bar([i - 0.18 for i in x], cash_cost, width=0.34, color="#f59e0b", label="预计现金支出")
    ax.bar([i + 0.18 for i in x], potential_income, width=0.34, color="#2563eb", label="可能试点收入")
    ax.set_xticks(list(x), stages)
    ax.set_ylabel("万元")
    ax.set_title("课程实训阶段财务假设", loc="left", fontsize=14, fontweight="bold", pad=18)
    ax.text(0, 5.45, "口径：当前项目是大学生课程模型实训，不默认发生融资、正式商业收入或规模化成本。", fontsize=9.5, color="#64748b")
    ax.grid(axis="y", alpha=0.24)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False, loc="upper left")
    for i, (cost, income) in enumerate(zip(cash_cost, potential_income)):
        ax.text(i - 0.18, cost + 0.08, f"{cost:.2f}", ha="center", fontsize=9)
        ax.text(i + 0.18, income + 0.08, f"{income:.2f}", ha="center", fontsize=9)
    ax.set_ylim(0, 6.0)
    return save_support_chart(fig, "chart09-finance.png")


def build_team_chart() -> Path:
    fig, ax = plt.subplots(figsize=(9.2, 5.6))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(0.03, 0.96, f"{TEAM_NAME} 团队分工结构", fontsize=17, fontweight="bold", color="#111827")
    ax.text(0.03, 0.915, f"{COURSE_NAME} · {SCHOOL}{COLLEGE}", fontsize=10, color="#64748b")
    roles = [
        ("梁思成", "项目统筹 / 路演", "项目统筹、路演、材料整合", "#dbeafe", "#1d4ed8", 0.03, 0.58, 0.30),
        ("何英賢", "PPT / 演讲稿", "PPT制作、演讲稿撰写", "#dcfce7", "#047857", 0.35, 0.58, 0.30),
        ("万梓鹏", "现场演讲", "负责答辩现场演讲", "#fef3c7", "#b45309", 0.67, 0.58, 0.30),
        ("苏涛", "前端 / 文档 / 财务", "前端看板、项目书、SWOT、财务与PPT", "#ede9fe", "#6d28d9", 0.18, 0.20, 0.30),
        ("颜酉津", "算法 / 后端 / 测试", "算法与数据、补货分仓、后端接口、实时仿真、测试部署", "#ffe4e6", "#be123c", 0.52, 0.20, 0.30),
    ]
    for name, title, desc, fill, edge, x, y, width in roles:
        box = FancyBboxPatch(
            (x, y),
            width,
            0.28,
            boxstyle="round,pad=0.018,rounding_size=0.024",
            linewidth=1.2,
            edgecolor=edge,
            facecolor=fill,
        )
        ax.add_patch(box)
        ax.text(x + 0.025, y + 0.205, name, fontsize=15, fontweight="bold", color=edge)
        ax.text(x + 0.025, y + 0.145, title, fontsize=11.5, fontweight="bold", color="#111827")
        ax.text(x + 0.025, y + 0.085, wrap_cn(desc, 16), fontsize=9.5, color="#334155", va="top")
    ax.text(0.05, 0.08, "协作逻辑：产品问题 -> 数据与算法 -> 程序实现 -> 演示文档 -> 复盘修改。", fontsize=10.5, color="#0f172a")
    return save_support_chart(fig, "chart10-team-roles.png")


def ensure_custom_charts() -> None:
    build_swot_chart()
    build_realistic_finance_chart()
    build_team_chart()


def ensure_print_cover() -> Path:
    """将用户提供的封面图放大到 A4 打印分辨率（300 DPI）。"""
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    if not PRINT_COVER_SRC.exists():
        raise FileNotFoundError(f"打印封面源图不存在: {PRINT_COVER_SRC}")
    with Image.open(PRINT_COVER_SRC) as img:
        img = img.convert("RGB")
        if img.size != (2480, 3508):
            img = img.resize((2480, 3508), Image.Resampling.LANCZOS)
        img.save(PRINT_COVER_A4, dpi=(300, 300))
    return PRINT_COVER_A4


def build_cover_image(anonymous: bool) -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "anonymous" if anonymous else "full"
    path = ASSET_DIR / f"globalpool-a4-cover-{suffix}.png"
    team = "匿名团队" if anonymous else TEAM_NAME
    school = "匿名学校" if anonymous else SCHOOL
    college = "匿名学院" if anonymous else COLLEGE
    members = "匿名成员" if anonymous else MEMBERS

    fig, ax = plt.subplots(figsize=(8.27, 11.69), dpi=300)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.patch.set_facecolor("#f7f8f5")
    ax.set_facecolor("#f7f8f5")

    # Fine editorial grid.
    for x in [i / 20 for i in range(21)]:
        ax.plot([x, x], [0, 1], color="#e4e7dd", linewidth=0.45, zorder=0)
    for y in [i / 28 for i in range(29)]:
        ax.plot([0, 1], [y, y], color="#e4e7dd", linewidth=0.45, zorder=0)

    ax.add_patch(FancyBboxPatch((0.06, 0.06), 0.88, 0.88, boxstyle="round,pad=0,rounding_size=0.012", linewidth=1.1, edgecolor="#172033", facecolor="none"))
    ax.add_patch(FancyBboxPatch((0.09, 0.09), 0.82, 0.82, boxstyle="round,pad=0,rounding_size=0.008", linewidth=0.55, edgecolor="#c3c9bd", facecolor="none"))

    # Global inventory network composition.
    hubs = {
        "深圳中心库存池": (0.50, 0.50, "#002FA7"),
        "US-LAX": (0.25, 0.60, "#0f766e"),
        "DE-FRA": (0.67, 0.67, "#f59e0b"),
        "UK-BHX": (0.73, 0.53, "#dc2626"),
        "AU-SYD": (0.61, 0.34, "#7c3aed"),
    }
    center = hubs["深圳中心库存池"]
    for name, (x, y, color) in hubs.items():
        if name != "深圳中心库存池":
            ax.plot([center[0], x], [center[1], y], color="#002FA7", linewidth=1.8, alpha=0.42, zorder=1)
            for t in [0.30, 0.58, 0.80]:
                ax.scatter(center[0] + (x - center[0]) * t, center[1] + (y - center[1]) * t, s=10, color=color, alpha=0.70, zorder=2)
    for name, (x, y, color) in hubs.items():
        size = 1160 if name == "深圳中心库存池" else 560
        ax.scatter(x, y, s=size, color=color, alpha=0.92, edgecolor="white", linewidth=2.0, zorder=4)
        ax.scatter(x, y, s=size * 1.55, color=color, alpha=0.10, edgecolor="none", zorder=3)
        ax.text(x, y - 0.055, name, ha="center", va="top", fontsize=9.5, color="#172033", fontweight="bold", zorder=5)

    panel = FancyBboxPatch((0.12, 0.67), 0.42, 0.15, boxstyle="round,pad=0.018,rounding_size=0.018", linewidth=0, facecolor="#ffffff", alpha=0.92, zorder=6)
    ax.add_patch(panel)
    ax.text(0.15, 0.785, "GLOBAL INVENTORY POOL", fontsize=8.5, color="#002FA7", fontweight="bold", zorder=7)
    ax.text(0.15, 0.748, "AI 预测  ·  智能补货  ·  库存分配  ·  仿真评估", fontsize=11, color="#172033", fontweight="bold", zorder=7)
    ax.text(0.15, 0.712, "从实时订单流到补货分仓决策的\n跨境电商海外仓数字孪生模型", fontsize=9.8, color="#526070", linespacing=1.5, zorder=7)

    course_label = "匿名课程" if anonymous else COURSE_NAME
    ax.text(0.11, 0.885, "GLOBALPOOL AI", fontsize=12, color="#002FA7", fontweight="bold")
    ax.text(0.89, 0.885, course_label, fontsize=8.5, color="#64748b", ha="right")
    ax.plot([0.11, 0.89], [0.865, 0.865], color="#172033", linewidth=1.0)

    title_lines = ["跨境电商海外仓", "数字孪生仿真与", "智能补货决策系统"]
    for i, line in enumerate(title_lines):
        ax.text(0.11, 0.255 - i * 0.055, line, fontsize=26, fontweight="bold", color="#111827", ha="left", va="center")
    ax.text(0.11, 0.118, f"课程名称：{course_label}", fontsize=10.5, color="#475569")
    ax.text(0.11, 0.095, f"团队名称：{team}", fontsize=11, color="#111827")
    ax.text(0.11, 0.072, f"学校：{school}    学院：{college}", fontsize=10.5, color="#475569")
    ax.text(0.11, 0.049, f"成员姓名：{members}    提交日期：{SUBMIT_DATE}", fontsize=9.8, color="#475569")

    metric_cards = [
        ("15", "SKU"),
        ("4", "OVERSEAS WAREHOUSES"),
        ("180D", "HISTORY DATA"),
        ("90D", "SIMULATION"),
    ]
    for i, (num, label) in enumerate(metric_cards):
        x = 0.59 + (i % 2) * 0.16
        y = 0.205 - (i // 2) * 0.085
        ax.add_patch(FancyBboxPatch((x, y), 0.13, 0.058, boxstyle="round,pad=0.01,rounding_size=0.012", linewidth=0.8, edgecolor="#d7dccf", facecolor="#ffffff", alpha=0.90))
        ax.text(x + 0.015, y + 0.032, num, fontsize=17, fontweight="bold", color="#002FA7", va="center")
        ax.text(x + 0.015, y + 0.011, label, fontsize=5.8, color="#64748b", va="center")

    fig.savefig(path, dpi=300, pad_inches=0)
    plt.close(fig)
    with Image.open(path) as img:
        img = img.resize((2480, 3508), Image.Resampling.LANCZOS)
        img.save(path, dpi=(300, 300))
    return path


def add_page_number(section) -> None:
    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.first_line_indent = Cm(0)
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


def enable_field_update_on_open(doc: Document) -> None:
    settings = doc.settings.element
    existing = settings.find(qn("w:updateFields"))
    if existing is None:
        update_fields = OxmlElement("w:updateFields")
        update_fields.set(qn("w:val"), "true")
        settings.append(update_fields)
    else:
        existing.set(qn("w:val"), "true")


def add_table_of_contents(doc: Document) -> None:
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.first_line_indent = Cm(0)
    title.paragraph_format.space_after = Pt(18)
    run = title.add_run("目录")
    set_font(run, "黑体", "黑体", 18, True)

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.line_spacing = 1.4
    run = p.add_run()

    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = 'TOC \\o "1-3" \\h \\z \\u'
    fld_separate = OxmlElement("w:fldChar")
    fld_separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "目录将在 Word 中自动更新；如未显示，请右键目录并选择更新域。"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")

    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_separate)
    run._r.append(placeholder)
    run._r.append(fld_end)
    doc.add_page_break()


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


def set_font(run, name: str = "宋体", east_asia: str | None = None, size: float | None = None, bold: bool | None = None) -> None:
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), east_asia or name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold


def set_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "宋体"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(12)
    normal.font.color.rgb = INK
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.first_line_indent = Cm(0.74)

    for style_name in ["Heading 1", "Heading 2", "Heading 3"]:
        style = doc.styles[style_name]
        style.font.name = "黑体"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
        style.font.size = Pt(14)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.first_line_indent = Cm(0)
        style.paragraph_format.line_spacing = 1.5
        style.paragraph_format.space_before = Pt(10)
        style.paragraph_format.space_after = Pt(6)

    caption = doc.styles.add_style("CaptionCN", 1)
    caption.font.name = "宋体"
    caption._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    caption.font.size = Pt(9)
    caption.font.color.rgb = MUTED
    caption.paragraph_format.first_line_indent = Cm(0)
    caption.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    caption.paragraph_format.line_spacing = 1.2
    caption.paragraph_format.space_after = Pt(8)


def add_cover(doc: Document, anonymous: bool, *, full_bleed: bool = False) -> None:
    cover_path = build_cover_image(anonymous) if anonymous else ensure_print_cover()
    picture_width = Cm(21.0) if full_bleed else Cm(16.0)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.add_run().add_picture(str(cover_path), width=picture_width)


def add_para(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    r = p.add_run(text)
    set_font(r, "宋体", "宋体", 12)


def add_paras(doc: Document, rows: list[str]) -> None:
    for row in rows:
        add_para(doc, row)


def add_bullets(doc: Document, rows: list[str]) -> None:
    for row in rows:
        p = doc.add_paragraph(style=None)
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.left_indent = Cm(0.74)
        p.paragraph_format.line_spacing = 1.5
        r = p.add_run(row)
        set_font(r, "宋体", "宋体", 12)


def add_table(doc: Document, headers: list[str], rows: list[list[str]], title: str) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    r = p.add_run(title)
    set_font(r, "黑体", "黑体", 12, True)

    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = True
    hdr = table.rows[0]
    set_repeat_table_header(hdr)
    for i, header in enumerate(headers):
        cell = hdr.cells[i]
        set_cell_shading(cell, "E8EEF5")
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        cell.text = header
        for paragraph in cell.paragraphs:
            paragraph.paragraph_format.first_line_indent = Cm(0)
            for run in paragraph.runs:
                set_font(run, "黑体", "黑体", 10.5, True)
    for row in rows:
        cells = table.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = str(value)
            cells[i].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for paragraph in cells[i].paragraphs:
                paragraph.paragraph_format.first_line_indent = Cm(0)
                paragraph.paragraph_format.line_spacing = 1.2
                for run in paragraph.runs:
                    set_font(run, "宋体", "宋体", 10)
    doc.add_paragraph()


def add_figure_with_context(
    doc: Document,
    path: Path,
    caption: str,
    *,
    before: str | None = None,
    after: str | None = None,
    width_cm: float = 15.2,
) -> None:
    if before:
        add_para(doc, before)
    add_captioned_image(doc, path, caption, width_cm=width_cm)
    if after:
        add_para(doc, after)


def add_captioned_image(doc: Document, path: Path, caption: str, width_cm: float = 15.2) -> None:
    if not path.exists():
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Cm(0)
    p.add_run().add_picture(str(path), width=Cm(width_cm))
    cp = doc.add_paragraph(style="CaptionCN")
    cp.add_run(caption)


def plan_stats(plan: dict) -> dict:
    recommendations = plan["recommendations"]
    allocations = plan["allocations"]
    comparison = plan["simulation_comparison"]
    baseline = comparison["baseline"]
    intelligent = comparison["intelligent"]
    return {
        "recommendation_count": len(recommendations),
        "allocation_count": len(allocations),
        "approval_count": len(plan.get("approval_queue", [])),
        "fill_lift": comparison["improvement"]["fill_rate_lift_pct"],
        "stockout_drop": comparison["improvement"]["stockout_rate_reduction_pct"],
        "delivery_drop": baseline["average_delivery_days"] - intelligent["average_delivery_days"],
        "profit_lift": comparison["improvement"]["profit_lift"],
        "baseline": baseline,
        "intelligent": intelligent,
    }


def build_docx(plan: dict, charts: dict[str, Path], anonymous: bool) -> Path:
    stats = plan_stats(plan)
    baseline = stats["baseline"]
    intelligent = stats["intelligent"]
    inventory_delta = intelligent["average_inventory_units"] - baseline["average_inventory_units"]
    inventory_phrase = (
        f"平均库存减少 {abs(inventory_delta):.0f} 件"
        if inventory_delta < 0
        else f"平均库存增加 {inventory_delta:.0f} 件"
    )

    doc = Document()
    cover_section = doc.sections[0]
    cover_section.page_height = Cm(29.7)
    cover_section.page_width = Cm(21.0)
    if anonymous:
        cover_section.top_margin = Cm(2.5)
        cover_section.bottom_margin = Cm(2.5)
        cover_section.left_margin = Cm(2.5)
        cover_section.right_margin = Cm(2.5)
    else:
        cover_section.top_margin = Cm(0)
        cover_section.bottom_margin = Cm(0)
        cover_section.left_margin = Cm(0)
        cover_section.right_margin = Cm(0)

    set_styles(doc)
    enable_field_update_on_open(doc)
    add_cover(doc, anonymous, full_bleed=not anonymous)

    content_section = doc.add_section(WD_SECTION.NEW_PAGE)
    content_section.page_height = Cm(29.7)
    content_section.page_width = Cm(21.0)
    content_section.top_margin = Cm(2.5)
    content_section.bottom_margin = Cm(2.5)
    content_section.left_margin = Cm(2.5)
    content_section.right_margin = Cm(2.5)
    add_page_number(content_section)
    add_table_of_contents(doc)

    doc.add_heading("1. 摘要", level=1)
    section_1_stats = (
        f"样例运行生成 {stats['recommendation_count']} 条补货建议、"
        f"{stats['allocation_count']} 条库存池分配决策，其中 {stats['approval_count']} 条处于待审批状态。"
        f"90 天滚动仿真中，智能库存池方案 Fill Rate 为 {intelligent['fill_rate']:.4f}，"
        f"传统独立备货方案为 {baseline['fill_rate']:.4f}；"
        f"缺货率分别为 {intelligent['stockout_rate']:.4f} 与 {baseline['stockout_rate']:.4f}；"
        f"平均配送时效分别为 {intelligent['average_delivery_days']:.2f} 天和 {baseline['average_delivery_days']:.2f} 天；"
        f"总利润差额约 {stats['profit_lift']:.2f} 元，{inventory_phrase}。"
    )
    add_paras(doc, [p if p is not None else section_1_stats for p in SECTION_1])
    add_figure_with_context(
        doc,
        SCREENSHOT_DIR / "dashboard-pool.png",
        "图1 全球库存池看板运行截图",
        before=FIG_1_BEFORE,
        after=FIG_1_AFTER,
    )
    add_figure_with_context(
        doc,
        charts["kpi"],
        "图2 传统独立备货与智能库存池方案关键指标对比",
        before=FIG_2_BEFORE,
        after=FIG_2_AFTER,
    )

    doc.add_heading("2. 项目背景与问题提出", level=1)
    doc.add_heading("2.1 宏观环境分析", level=2)
    add_paras(doc, SECTION_2_1)
    doc.add_heading("2.2 行业痛点或市场空白", level=2)
    add_paras(doc, SECTION_2_2)
    add_figure_with_context(
        doc,
        charts["market"],
        "图3 跨境电商与全球零售电商市场规模趋势",
        before=FIG_3_BEFORE,
        after=FIG_3_AFTER,
    )

    doc.add_heading("3. 产品/服务描述", level=1)
    doc.add_heading("3.1 产品总体形态与使用对象", level=2)
    add_paras(doc, SECTION_3_1)
    doc.add_heading("3.2 全球库存池看板", level=2)
    add_paras(doc, SECTION_3_2)
    doc.add_heading("3.3 AI 销量预测", level=2)
    add_paras(doc, SECTION_3_3)
    doc.add_heading("3.4 智能补货建议", level=2)
    add_paras(doc, SECTION_3_4)
    doc.add_heading("3.5 库存分配方案", level=2)
    add_paras(doc, SECTION_3_5)
    doc.add_heading("3.6 仿真评估对比", level=2)
    add_paras(doc, SECTION_3_6)
    add_figure_with_context(
        doc,
        SCREENSHOT_DIR / "dashboard-forecast.png",
        "图4 AI 销量预测页运行截图",
        before=FIG_4_BEFORE,
        after=FIG_4_AFTER,
    )
    add_figure_with_context(
        doc,
        SCREENSHOT_DIR / "dashboard-replenishment.png",
        "图5 智能补货建议页运行截图",
        before=FIG_5_BEFORE,
        after=FIG_5_AFTER,
    )
    add_para(doc, TABLE_1_BEFORE)
    add_table(
        doc,
        ["模块", "输入", "输出", "差异化价值"],
        [
            ["数字孪生仿真", "SKU、仓网、市场信号、场景参数、随机订单过程", "实时订单、库存、退货、在途补货和仓库负载", "在样例网络上复现订单与库存事件"],
            ["AI 需求预测", "180 天历史销量、广告、促销、季节、平台、价格", "日均需求、P50/P80/P95、波动率、置信度", "输出分位需求供补货层调用"],
            ["补货策略", "预测、库存位置、提前期、服务等级、箱规", "安全库存、补货点、目标库存、建议补货量", "生成 SKU-仓 补货建议"],
            ["库存池分仓", "全球供应池、补货需求、运输方式、仓容、资金", "分配数量、运输方式、未满足缺口、审批状态", "在约束下求解库存池分配"],
            ["评估对比", "传统独立备货与智能库存池策略", "Fill Rate、缺货率、时效、库存、利润", "固定需求路径上的指标并列比较"],
        ],
        "表1 产品功能模块与输入输出",
    )
    add_para(doc, TABLE_1_AFTER)

    doc.add_heading("4. 市场分析", level=1)
    add_paras(doc, SECTION_4)
    add_figure_with_context(
        doc,
        charts["swot"],
        "表2 SWOT 四象限分析",
        before=FIG_SWOT_BEFORE,
        after=FIG_SWOT_AFTER,
    )
    add_figure_with_context(
        doc,
        charts["replenishment"],
        "图6 补货需求 Top 10",
        before=FIG_6_BEFORE,
        after=FIG_6_AFTER,
    )

    doc.add_heading("5. 商业模式", level=1)
    add_paras(doc, SECTION_5)
    add_figure_with_context(
        doc,
        charts["business"],
        "图7 商业模式与定价区间",
        before=FIG_7_BEFORE,
        after=FIG_7_AFTER,
    )
    add_figure_with_context(
        doc,
        charts["pool"],
        "图8 全球库存池水位",
        before=FIG_8_BEFORE,
        after=FIG_8_AFTER,
    )

    doc.add_heading("6. 实施计划", level=1)
    add_paras(doc, SECTION_6)
    add_figure_with_context(
        doc,
        SCREENSHOT_DIR / "dashboard-allocation.png",
        "图9 库存分配方案页运行截图",
        before=FIG_9_BEFORE,
        after=FIG_9_AFTER,
    )
    add_figure_with_context(
        doc,
        charts["roadmap"],
        "图10 0—12 个月实施路线图",
        before=FIG_10_BEFORE,
        after=FIG_10_AFTER,
    )
    add_para(doc, TABLE_3_BEFORE)
    add_table(
        doc,
        ["阶段", "目标", "主要任务", "验收标准"],
        [
            ["0-3个月", "稳定原型与演示闭环", "代码测试、页面截图、仿真情景、项目书和路演材料", "Web 端可运行，五页截图完整，测试通过"],
            ["3-6个月", "试点准备与客户验证", "访谈、字段映射、仿真参数标定、报价和接口方案", "形成试点客户清单和最小数据模板"],
            ["6-9个月", "真实数据试点", "接入脱敏数据、预测误差监控、审批流、执行回写", "输出真实客户补货建议并记录采纳率"],
            ["9-12个月", "产品化交付", "权限、部署、API、行业参数库和商业化版本", "形成可复制 SaaS/插件交付方案"],
        ],
        "表3 实施计划与验收标准",
    )
    add_para(doc, TABLE_3_AFTER)

    doc.add_heading("7. 财务预测", level=1)
    add_paras(doc, SECTION_7)
    add_figure_with_context(
        doc,
        charts["finance"],
        "图11 课程实训阶段财务假设",
        before=FIG_11_BEFORE,
        after=FIG_11_AFTER,
    )
    add_table(
        doc,
        ["项目", "金额/假设", "说明"],
        [
            ["课程实训现金支出", "0.20-0.50万元", "打印装订、演示材料、少量云资源、调研沟通等"],
            ["校内与开源资源", "不计现金收入", "本地电脑、开源库、课程指导、样例数据和已有代码"],
            ["演示版完善支出", "0.80-1.50万元", "页面优化、脱敏样例整理、基础云服务器、软著或视频材料"],
            ["小额试点收入", "3-8万元/家", "后续若有校企合作，可按一次性诊断和部署服务测算"],
            ["小额试点成本", "3-6万元/家", "数据整理、接口映射、参数标定、测试、沟通和云资源"],
            ["当前结论", "暂不假设规模化盈利", "本项目是课程模型实训，核心是验证原型和业务逻辑"],
        ],
        "表4 财务预测关键假设",
    )
    add_figure_with_context(
        doc,
        SCREENSHOT_DIR / "dashboard-simulation.png",
        "图12 仿真评估对比页运行截图",
        before=FIG_12_BEFORE,
        after=FIG_12_AFTER,
    )
    add_figure_with_context(
        doc,
        charts["simulation"],
        "图13 90 天仿真多指标对比",
        before=FIG_13_BEFORE,
        after=FIG_13_AFTER,
    )

    doc.add_heading("8. 团队介绍", level=1)
    if anonymous:
        add_para(doc, "匿名版隐去团队、学校、学院和成员姓名，仅保留岗位分工与能力结构。")
    else:
        add_para(
            doc,
            f"本项目为“{COURSE_NAME}”课程实训成果。团队名称为 {TEAM_NAME}，学校为 {SCHOOL}，学院为 {COLLEGE}，成员为 {MEMBERS}。",
        )
    add_paras(doc, SECTION_8)
    add_figure_with_context(
        doc,
        charts["team"],
        "图14 CLS 团队分工结构",
        before=FIG_14_BEFORE,
        after=FIG_14_AFTER,
    )
    add_table(
        doc,
        ["成员", "职责方向", "具体完成事项"],
        [[name, role, tasks] for name, role, tasks in MEMBER_ROLES],
        "表5 团队分工建议",
    )

    doc.add_heading("9. 附件", level=1)
    section_9_stats = (
        f"附件四：运行结果摘要。样例运行生成 {stats['recommendation_count']} 条补货建议、"
        f"{stats['allocation_count']} 条库存池分配决策，待审批补货单 {stats['approval_count']} 条。"
        f"仿真对比中，智能库存池方案 Fill Rate 为 {intelligent['fill_rate']:.4f}，"
        f"传统方案为 {baseline['fill_rate']:.4f}；"
        f"缺货率分别为 {intelligent['stockout_rate']:.4f} 与 {baseline['stockout_rate']:.4f}；"
        f"平均配送时效分别为 {intelligent['average_delivery_days']:.2f} 天和 {baseline['average_delivery_days']:.2f} 天。"
        f"上述结果基于当前样例网络及仿真参数配置。"
    )
    add_paras(doc, [p if p is not None else section_9_stats for p in SECTION_9])

    output_name = (
        "双创实训_GlobalPool AI跨境电商海外仓智能补货_匿名团队_匿名版.docx"
        if anonymous
        else f"双创实训_GlobalPool AI跨境电商海外仓智能补货_{TEAM_NAME}_完整版.docx"
    )
    output_path = REPORT_DIR / output_name
    doc.save(output_path)
    return output_path


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    plan = load_plan()
    charts = build_charts(plan)
    full = build_docx(plan, charts, anonymous=False)
    anon = build_docx(plan, charts, anonymous=True)
    print(json.dumps({"full": str(full), "anonymous": str(anon)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
