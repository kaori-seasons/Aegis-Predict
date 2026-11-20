"""Render a source-controlled SOP as GIF, PNG, SVG and a Markdown handoff table.

Authoring-only Pillow dependency; no production or browser dependencies.
The diagram describes existing workflows, not an executed merchant demo.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H = 1600, 1150
BG, INK, MUTED, RULE = "#ffffff", "#20252b", "#626b75", "#d8dce0"
BLUE, TINT, AMBER = "#235b91", "#edf4fa", "#996015"
LEFT, COL, TOP, ROW = 198, 169, 310, 90


class Canvas:
    def __init__(self, font, bold_index, svg=False):
        self.im = Image.new("RGB", (W, H), BG)
        self.draw = ImageDraw.Draw(self.im)
        self.path, self.bold_index = str(font), bold_index
        self.elements = [] if svg else None
        self.text_boxes = []

    @lru_cache(maxsize=50)
    def font(self, size, bold=False):
        return ImageFont.truetype(self.path, size, index=self.bold_index if bold else 0)

    def emit(self, s):
        if self.elements is not None:
            self.elements.append(s)

    def rect(self, xy, fill=BG, outline=RULE, width=1):
        self.draw.rectangle(xy, fill=fill, outline=outline, width=width)
        x, y, x2, y2 = xy
        self.emit(
            f'<rect x="{x}" y="{y}" width="{x2 - x}" height="{y2 - y}" fill="{fill}" stroke="{outline}" stroke-width="{width}"/>'
        )

    def line(self, pts, color=RULE, width=1):
        self.draw.line(pts, fill=color, width=width, joint="curve")
        coords = " ".join(f"{x},{y}" for x, y in pts)
        self.emit(
            f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round"/>'
        )

    def arrow(self, pts, color=RULE, width=2):
        self.line(pts, color, width)
        x, y = pts[-1]
        px, py = pts[-2]
        angle = math.atan2(y - py, x - px)
        self.line(
            [
                (x - 7 * math.cos(angle - 0.55), y - 7 * math.sin(angle - 0.55)),
                (x, y),
                (x - 7 * math.cos(angle + 0.55), y - 7 * math.sin(angle + 0.55)),
            ],
            color,
            width,
        )

    def text(self, x, y, value, size=24, color=INK, bold=False, max_width=None):
        font = self.font(size, bold)
        length = self.draw.textlength(value, font=font)
        if max_width is not None and length > max_width:
            raise ValueError(f"text exceeds region ({length:.1f}>{max_width}): {value}")
        bounds = self.draw.textbbox((x, y), value, font=font, anchor="lt")
        if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > W - 32 or bounds[3] > H - 8:
            raise ValueError(f"text exceeds canvas: {value}")
        self.text_boxes.append([*bounds, value])
        self.draw.text((x, y), value, font=font, fill=color, anchor="lt")
        self.emit(
            f'<text x="{x}" y="{y}" dominant-baseline="text-before-edge" font-family="Hiragino Sans GB,Noto Sans CJK SC,Microsoft YaHei,sans-serif" font-size="{size}" font-weight="{600 if bold else 400}" fill="{color}">{html.escape(value)}</text>'
        )

    def wrap(self, x, y, value, width, size=24, line_height=34, max_lines=2, color=INK):
        lines = []
        current = ""
        for ch in value:
            if self.draw.textlength(current + ch, font=self.font(size)) > width:
                lines.append(current)
                current = ch
            else:
                current += ch
        if current:
            lines.append(current)
        if (
            len(lines) == 2
            and self.draw.textlength(lines[1], font=self.font(size)) < width * 0.35
        ):
            # Avoid a lone Chinese character on the second line; prefer a clause boundary.
            candidates = [i + 1 for i, ch in enumerate(value[:-1]) if ch in "；，。"]
            candidates = [
                i
                for i in candidates
                if self.draw.textlength(value[:i], font=self.font(size)) <= width
                and self.draw.textlength(value[i:], font=self.font(size)) <= width
            ]
            if candidates:
                cut = min(
                    candidates,
                    key=lambda i: abs(
                        self.draw.textlength(value[:i], font=self.font(size))
                        - self.draw.textlength(value[i:], font=self.font(size))
                    ),
                )
                lines = [value[:cut], value[cut:]]
        if len(lines) > max_lines:
            raise ValueError(f"text requires {len(lines)} lines: {value}")
        for i, line in enumerate(lines):
            self.text(x, y + line_height * i, line, size, color, max_width=width)


def center(stage):
    return LEFT + COL * (int(stage["id"]) - 0.5), TOP + ROW * (stage["lane"] + 0.5)


def connector(a, b):
    x, y = center(a)
    nx, ny = center(b)
    middle = (x + nx) / 2
    return [(x + 70, y), (middle, y), (middle, ny), (nx - 70, ny)]


def scene(data, font, bold_index, active=None, svg=False):
    c = Canvas(font, bold_index, svg)
    stages = data["stages"]
    c.text(48, 38, "电商、海外仓与物流的日常作业闭环", 42, bold=True, max_width=1330)
    c.text(
        48,
        102,
        "需求与库存接入 → 预测与补货 → 审批与实物执行 → 双端结算与成熟复盘",
        25,
        MUTED,
        max_width=1500,
    )
    c.text(1405, 50, "AEGIS", 28, BLUE, bold=True)
    for i, industry in enumerate(data["industries"]):
        x = 48 + i * 510
        if i:
            c.line([(x - 20, 158), (x - 20, 222)])
        c.text(x, 157, industry["name"], 26, bold=True, max_width=480)
        c.text(x, 196, industry["handoff"], 21, MUTED, max_width=480)
    c.line([(48, 245), (1552, 245)], INK)
    c.text(48, 272, "岗位 / 交接", 22, MUTED)
    for i, stage in enumerate(stages):
        x, _ = center(stage)
        selected = active == i
        c.text(
            x - 55,
            264,
            f"{stage['id']}  {stage['short']}",
            25,
            BLUE if selected else INK,
            bold=selected,
            max_width=140,
        )
    for i, lane in enumerate(data["lanes"]):
        yy = TOP + ROW * i
        c.rect((48, yy, 1552, yy + ROW), "#f7f8f9" if i % 2 == 0 else BG, BG)
        c.text(54, yy + 32, lane, 24, bold=True, max_width=140)
        c.line([(185, yy), (185, yy + ROW)])
    for i in range(9):
        x = LEFT + COL * i
        c.line([(x, TOP), (x, TOP + 4 * ROW)], "#e7e9ec")
    for a, b in zip(stages, stages[1:]):
        c.arrow(connector(a, b), "#9ba4ae", 2)
    # Operational close releases the next cycle without waiting for forecast labels.
    sx, sy = center(stages[6])
    tx, ty = center(stages[0])
    c.arrow([(sx, sy + 32), (sx, 700), (tx, 700), (tx, ty + 32)], MUTED, 2)
    c.rect((420, 682, 1200, 723), BG, BG)
    c.text(
        434,
        688,
        "07 结算关闭后可开下一轮；08 独立等待实绩成熟",
        24,
        MUTED,
        max_width=770,
    )
    for i, stage in enumerate(stages):
        x, y = center(stage)
        selected = active == i
        c.rect(
            (x - 70, y - 33, x + 70, y + 33),
            TINT if selected else BG,
            BLUE if selected else "#74808c",
            3 if selected else 1,
        )
        c.text(
            x - 59,
            y - 23,
            stage["node"][0],
            23,
            BLUE if selected else INK,
            bold=selected,
            max_width=122,
        )
        c.text(x - 59, y + 9, stage["node"][1], 19, MUTED, max_width=122)
    c.line([(48, 744), (1552, 744)], INK)
    if active is None:
        c.text(
            48, 770, "先完成数据与影子验证，再受控执行", 35, bold=True, max_width=1500
        )
        c.text(
            48,
            827,
            "系统内：冻结事实、预测证据、人工审批、库存预占、对账和审计。",
            27,
            max_width=1500,
        )
        c.text(
            48,
            875,
            "系统外：真实商家授权、仓内实物作业、订舱及运输由对应平台完成。",
            27,
            max_width=1500,
        )
        c.text(
            48,
            946,
            "作业顺序  01–04 准备与规划  /  05 审批发布  /  06–07 实物闭环  /  08 成熟复盘",
            25,
            BLUE,
            max_width=1500,
        )
        c.text(
            48,
            1000,
            "出现差异先修源数据；单据创建不等于发运完成；库存结算不等于财务结算。",
            25,
            AMBER,
            max_width=1500,
        )
    else:
        stage = stages[active]
        c.text(
            48, 768, f"{stage['id']}  {stage['title']}", 35, bold=True, max_width=1480
        )
        c.text(48, 813, f"负责人：{stage['owner']}", 22, MUTED, max_width=1480)
        c.text(48, 843, f"入口：{stage['page']}", 22, MUTED, max_width=1480)
        c.text(48, 878, "操作", 22, MUTED)
        c.wrap(112, 875, stage["action"], 660, 25, 35, 2)
        c.text(820, 878, "交接", 22, MUTED)
        c.wrap(884, 875, stage["output"], 660, 25, 35, 2)
        c.text(48, 959, "放行", 23, BLUE, bold=True)
        c.wrap(112, 957, stage["gate"], 1430, 24, 32, 1, BLUE)
        c.text(48, 1003, "异常", 23, AMBER, bold=True)
        c.wrap(112, 1001, stage["failure"], 1430, 24, 32, 1, AMBER)
        c.text(48, 1055, "触发：" + stage["trigger"], 22, MUTED, max_width=1450)
    c.line([(48, 1092), (1552, 1092)])
    c.text(
        48,
        1110,
        "作业示意；默认 Mock / 影子。真实授权、执行开关、逐仓对账与模型验收需分别完成。",
        22,
        MUTED,
        max_width=1500,
    )
    return c


def markdown_table(data):
    lines = [
        "| 步骤 / 触发 | 负责人 / 页面 | 操作与交接 | 放行与异常处理 |",
        "| --- | --- | --- | --- |",
    ]
    for s in data["stages"]:
        lines.append(
            f"| **{s['id']} {s['title']}**<br>{s['trigger']} | {s['owner']}<br>「{s['page']}」 | {s['action']}<br>**交接：**{s['output']} | **放行：**{s['gate']}<br>**异常：**{s['failure']} |"
        )
    return "\n".join(lines) + "\n"


def point(path, t):
    length = [math.dist(a, b) for a, b in zip(path, path[1:])]
    distance = t * sum(length)
    for a, b, n in zip(path, path[1:], length):
        if distance <= n:
            k = distance / n if n else 0
            return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        distance -= n
    return path[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=ROOT / "docs/assets/industry-sop.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets")
    parser.add_argument("--font", type=Path)
    parser.add_argument("--bold-index", type=int)
    parser.add_argument("--fps", type=int, default=4)
    parser.add_argument(
        "--update-docs",
        action="store_true",
        help="Update only the marked SOP tables in README and the industry handbook",
    )
    args = parser.parse_args()
    if not 2 <= args.fps <= 8:
        parser.error("fps must be2–8")
    choices = [
        args.font,
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    font = next((p for p in choices if p and p.exists()), None)
    if font is None:
        parser.error("pass --font /path/to/CJK-font.ttc")
    bold = (
        args.bold_index
        if args.bold_index is not None
        else 2
        if "Hiragino" in font.name
        else 0
    )
    data = json.loads(args.source.read_text())
    stages = data["stages"]
    assert [s["id"] for s in stages] == [f"{i:02}" for i in range(1, 9)]
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    poster = scene(data, font, bold, svg=True)
    poster.im.save(out / "industry-sop.png", optimize=True)
    title = "电商、海外仓与物流端到端SOP"
    desc = "八步岗位泳道：接入、核对、预测、补货、审核发布、发运收货、双端结算、成熟复盘。执行默认关闭；结算后可开启下一轮，独立成熟评分不阻塞业务。"
    (out / "industry-sop.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc"><title id="title">{title}</title><desc id="desc">{desc}</desc><rect width="{W}" height="{H}" fill="white"/>'
        + "".join(poster.elements)
        + "</svg>"
    )
    boards = [scene(data, font, bold, i) for i in range(8)]
    sheet = Image.new("RGB", (400, 288 * 9), BG)
    for i, c in enumerate([poster, *boards]):
        sheet.paste(c.im.resize((400, 288)), (0, i * 288))
    palette = sheet.quantize(colors=112, method=Image.Quantize.MEDIANCUT)
    # Reserve exact semantic colors. A text-heavy median-cut palette can otherwise
    # merge rare blue/amber highlights into gray and tint the white background.
    from PIL import ImageColor

    colors = [
        BG,
        INK,
        MUTED,
        RULE,
        BLUE,
        TINT,
        AMBER,
        "#f7f8f9",
        "#74808c",
        "#9ba4ae",
        "#e7e9ec",
    ]
    entries = palette.getpalette()
    for i, color in enumerate(colors, 112):
        entries[3 * i : 3 * i + 3] = ImageColor.getrgb(color)
    palette.putpalette(entries)
    # Pillow's coarse RGB lookup can send pure white to a nearby gray even when
    # exact white is present. Restore that bucket to the reserved white entry.
    white_bucket = (
        Image.new("RGB", (1, 1), BG)
        .quantize(palette=palette, dither=Image.Dither.NONE)
        .getpixel((0, 0))
    )
    remap = list(range(256))
    remap[white_bucket] = 112

    def quantize(im):
        return im.quantize(palette=palette, dither=Image.Dither.NONE).point(remap)

    frames = [quantize(poster.im)]
    durations = [3000]
    for i, board in enumerate(boards):
        for j in range(6 * args.fps):
            im = board.im.copy()
            draw = ImageDraw.Draw(im)
            t = (j + 1) / (6 * args.fps)
            if i:
                x, y = point(connector(stages[i - 1], stages[i]), t)
                draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=BLUE)
            else:
                draw.rectangle((LEFT, TOP, LEFT + int(COL * t), TOP + 2), fill=BLUE)
            draw.rectangle((1225, 1067, 1225 + int(327 * t), 1070), fill=BLUE)
            frames.append(quantize(im))
            durations.append(
                round((j + 1) * 100 / args.fps) * 10 - round(j * 100 / args.fps) * 10
            )
    frames.append(frames[0])
    durations.append(4000)
    target = out / "industry-sop.gif"
    frames[0].save(
        target,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )
    table = markdown_table(data)
    (out / "industry-sop-steps.md").write_text(table)
    if args.update_docs:
        begin, end = "<!-- BEGIN INDUSTRY_SOP -->", "<!-- END INDUSTRY_SOP -->"
        for document in (
            ROOT / "README.md",
            ROOT / "docs/24-industry-sop-and-readme-guide.md",
        ):
            text = document.read_text()
            if text.count(begin) != 1 or text.count(end) != 1:
                raise ValueError(f"expected exactly one SOP region: {document}")
            head, tail = text.split(begin)
            _, tail = tail.split(end)
            document.write_text(head + begin + "\n" + table + end + tail)
    # Overview and each step are independently inspectable, without waiting for animation.
    contacts = Image.new("RGB", (800 * 2, 575 * 4), BG)
    for i, c in enumerate(boards):
        contacts.paste(c.im.resize((800, 575)), ((i % 2) * 800, (i // 2) * 575))
    contacts.save(out / "industry-sop-contact.png", optimize=True)
    with Image.open(target) as gif:
        actual_duration = sum(
            gif.seek(i) or gif.info["duration"] for i in range(gif.n_frames)
        )
        assert actual_duration == 55000, (actual_duration, gif.n_frames)
        assert gif.size == (W, H) and gif.info.get("loop") == 0
        count = gif.n_frames
    sources = [
        args.source,
        Path(__file__),
        ROOT / "src/aegis/planning/lifecycle.py",
        ROOT / "src/aegis/decision/forecasting.py",
        ROOT / "src/aegis/decision/protection.py",
        ROOT / "src/aegis/inventory/execution.py",
    ]
    metadata = {
        "schema": "industry-sop-render-v1",
        "width": W,
        "height": H,
        "duration_ms": actual_duration,
        "frames": count,
        "bytes": target.stat().st_size,
        "fps": args.fps,
        "loop": 0,
        "content": "workflow illustration; no fabricated merchant metrics or UI recording",
        "stages": [s["title"] for s in stages],
        "layout_checks": {
            "canvas_bounds": "passed",
            "regional_text_bounds": "passed",
            "step_count": 8,
            "text_blocks": len(poster.text_boxes)
            + sum(len(c.text_boxes) for c in boards),
        },
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
            if p.exists()
        },
    }
    (out / "industry-sop.meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
