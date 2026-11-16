"""Draw the actual prediction pipeline and a reproducible numerical example.

Authoring dependency: Pillow. Forecast and backtest numbers come from production code.
No browser captures, external artwork, or system fonts are bundled.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import math
import sys
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import fmean
from types import SimpleNamespace
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from aegis.planning.forecast import MODEL_VERSION, forecast, uncensor
from aegis.planning.backtest import rolling_backtest
from overseas_inventory.models import DemandSeries

W,H=1540,1240
INK,MUTED,LINE,BLUE='#252525','#666666','#a9a9a9','#235b91'
BG,SOFT,TINT,GREEN='#ffffff','#f5f5f5','#f1f6fb','#39745b'
STAGES=['读取并规范需求','对齐日历、检查质量并冻结','仅用历史处理缺失与缺货','按规则选择模型','计算未来需求分位','用历史留出窗回测','输出预测与评估结果']
class Canvas:
    def __init__(self, font, bold_index=0, svg=False):
        self.im=Image.new('RGB',(W,H),BG);self.d=ImageDraw.Draw(self.im)
        self.font_path=font;self.bold_index=bold_index;self.svg=[] if svg else None

    @lru_cache(maxsize=128)
    def font(self,size,bold=False,brand=False):
        if brand:
            path=Path('/System/Library/Fonts/Supplemental/Georgia Italic.ttf')
            if path.exists():return ImageFont.truetype(str(path),size)
        return ImageFont.truetype(self.font_path,size,index=self.bold_index if bold else 0)

    def add(self,value):
        if self.svg is not None:self.svg.append(value)

    def box(self,xy,fill=None,outline=None,r=0,width=1):
        self.d.rounded_rectangle(xy,radius=r,fill=fill,outline=outline,width=width)
        x,y,x2,y2=xy
        self.add(f'<rect x="{x}" y="{y}" width="{x2-x}" height="{y2-y}" rx="{r}" fill="{fill or "none"}" stroke="{outline or "none"}" stroke-width="{width}"/>')

    def line(self,pts,fill=LINE,width=2):
        self.d.line(pts,fill=fill,width=width,joint='curve')
        self.add(f'<polyline points="{" ".join(f"{x},{y}" for x,y in pts)}" fill="none" stroke="{fill}" stroke-width="{width}" stroke-linejoin="round"/>')

    def dot(self,x,y,r=4,fill=BLUE):
        self.d.ellipse((x-r,y-r,x+r,y+r),fill=fill)
        self.add(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}"/>')

    def text(self,x,y,value,size=20,fill=INK,bold=False,brand=False):
        font=self.font(size,bold,brand)
        bounds=self.d.textbbox((0,0),value,font=font)
        if x<0 or y<0 or x+(bounds[2]-bounds[0])>W-20 or y+(bounds[3]-bounds[1])>H:
            raise ValueError(f'text outside canvas: {value}')
        self.d.text((x,y),value,font=font,fill=fill,anchor='lt')
        family='Georgia,serif' if brand else 'Hiragino Sans GB,Noto Sans CJK SC,Microsoft YaHei,sans-serif'
        self.add(f'<text x="{x}" y="{y}" dominant-baseline="text-before-edge" font-family="{family}" font-size="{size}" font-weight="{600 if bold else 400}" fill="{fill}" font-style="{"italic" if brand else "normal"}">{html.escape(value)}</text>')

    def arrow(self,pts,color=LINE,width=2):
        self.line(pts,color,width)
        x,y=pts[-1];px,py=pts[-2];a=math.atan2(y-py,x-px)
        self.line([(x-6*math.cos(a-.55),y-6*math.sin(a-.55)),(x,y),(x-6*math.cos(a+.55),y-6*math.sin(a+.55))],color,width)

    def pill(self,x,y,label,width=108,color=INK,bg=SOFT):
        self.box((x,y,x+width,y+36),fill=bg,r=6)
        self.text(x+12,y+8,label,17,color)


def example():
    values=[(8,10,11,9,13,17,15)[i%7] for i in range(70)]
    missing=[i in (18,39,62) for i in range(70)]
    stockout=[i in (30,54) for i in range(70)]
    for i in (18,39,62,10,27,43,65):values[i]=0
    values[30],values[54]=2,1
    series=DemandSeries('S','W',values,stockout,missing)
    repaired,observed=uncensor(series)
    prediction=forecast(series,14)
    # rolling_backtest only reads these two attributes; no database or made-up metrics.
    network=SimpleNamespace(demand_history=[series],skus={'S':SimpleNamespace(category='sample')})
    report=rolling_backtest(network)
    start=date(2026,9,1)
    return {'series':series,'repaired':repaired,'forecast':prediction.to_dict(),'backtest':report,
        'dates':[(start+timedelta(days=i)).isoformat() for i in range(70)],
        'observed':observed,'nonzero_ratio':sum(v>0 for v in repaired)/len(repaired),
        'level':.6*fmean(repaired[-14:])+.4*fmean(repaired[-56:]),
        'seasonal':fmean(fmean(repaired[j::7][-8:]) for j in ((len(repaired)+d)%7 for d in range(14)))}


def rect_node(c,xy,title,body='',active=False,title_size=23,body_size=18):
    x,y,x2,y2=xy
    c.box(xy,TINT if active else BG,BLUE if active else INK,width=2 if active else 1)
    for value,size in [(title,title_size),(body,body_size)]:
        if c.d.textlength(value,font=c.font(size))>x2-x-24:
            raise ValueError('node label too wide: '+value)
    c.text(x+14,y+10,title,title_size,BLUE if active else INK)
    if body:c.text(x+14,y+42,body,body_size,MUTED)


def table(c,headers,rows,positions,y=248,row_height=40):
    left,right=1040,1492
    for x,label in zip(positions,headers):c.text(x,y,label,19,MUTED)
    c.line([(left,y+30),(right,y+30)],INK,1)
    for i,row in enumerate(rows):
        yy=y+44+i*row_height
        for x,value in zip(positions,row):c.text(x,yy,str(value),20)
        c.line([(left,yy+29),(right,yy+29)],'#dedede',1)


def example_panel(c,stage,e):
    f=e['forecast'];s=e['series'];r=e['backtest'];m=r['metrics']['overall'];b=r['metrics']['baseline']
    c.text(1040,151,'演算样例：SKU S / 仓库 W',24)
    c.text(1040,191,'固定合成销量；不是线上经营数据',18,MUTED)
    if stage==0:
        indices=[9,10,18,30,39,69]
        rows=[(e['dates'][i],s.daily_units[i],'缺失' if s.missing_days[i] else '缺货删失' if s.stockout_days[i] else '有效观察') for i in indices]
        table(c,['日期','原始量','标记'],rows,[1040,1206,1300])
        c.text(1040,574,'0 销量可能是真实零，也可能是缺失。',19)
        c.text(1040,610,'这里只展示部分行；样例共70天。',18,MUTED)
    elif stage==1:
        rows=[('日历范围','2026-09-01 至 11-09'),('预测起日','2026-11-10'),('历史长度','70天'),('有效观察','65天（含4个真实零）'),('缺失 / 缺货','3天 / 2天'),('预测周期','未来14天')]
        table(c,['序列属性','取值'],rows,[1040,1210])
        c.text(1040,570,'冻结后固定本次输入，按摘要核验。',19)
        c.text(1040,610,'真实规划另有库存、时效等质量门禁。',18,MUTED)
    elif stage==2:
        rows=[(e['dates'][i][5:],str(s.daily_units[i]),f"{e['repaired'][i]:.2f}",'缺失' if s.missing_days[i] else '缺货') for i in (18,30,39,54,62)]
        table(c,['日期','原始','处理后','原因'],rows,[1040,1160,1260,1388])
        c.text(1040,530,'真实零销量 0 → 0，保留原值。',20)
        c.text(1040,570,'只读取该日之前的有效观察。',19)
        c.text(1040,608,'填补值不变成新的实测观察。',19)
        c.text(1040,648,'展示值已四舍五入；模型使用完整精度。',17,MUTED)
    elif stage==3:
        rows=[('有效观察','65天，满足 ≥7'),('处理后非零率',f"{e['nonzero_ratio']:.2%}"),('选择模型','damped-seasonal'),('近期水平',f"{e['level']:.6f}"),('周季节项',f"{e['seasonal']:.6f}"),('日均需求',f"{f['daily_rate']:.6f}")]
        table(c,['分流与计算','本样例'],rows,[1040,1210])
        c.text(1040,570,'水平 = 0.6×近14天 + 0.4×近56天均值',17,MUTED)
        c.text(1040,610,'日需求 = 0.7×水平 + 0.3×周季节项',18)
        c.text(1040,650,'仅当前缀长度≥28天时加入周季节项。',17,MUTED)
    elif stage in (4,-1):
        table(c,['14天需求预测','生产函数实际输出'],[
            ('日均需求',f"{f['daily_rate']:.6f}"),('日波动',f"{f['volatility']:.6f}"),
            ('置信分数',str(f['confidence']))],[1040,1210])
        c.text(1040,422,'未来14天累计需求（件）',18,MUTED)
        for i,key in enumerate(('p50_units','p80_units','p95_units')):
            yy=460+i*54;units=f[key];c.text(1040,yy,key[:3].upper(),21)
            c.line([(1110,yy+13),(1110+units*1.6,yy+13)],BLUE,7)
            c.text(1420,yy,str(units),22)
        c.text(1040,644,'分位使用正态近似，不是履约承诺。',18,MUTED)
        c.text(1040,682,'置信分数是启发式指标，不是命中率。',17,MUTED)
    elif stage==5:
        total_folds=len(range(28,70-7+1,7))
        table(c,['滚动留出评估','本样例'],[
            ('训练 / 测试窗','≥28天 / 7天'),('候选 / 有效折数',f"{total_folds} / {m['folds']}"),
            ('排除无效窗',str(total_folds-m['folds'])),('预测 WAPE',f"{m['wape']:.2%}"),
            ('均值基线 WAPE',f"{b['wape']:.2%}"),('偏差',f"{m['bias']:+.2%}")],[1040,1246])
        c.text(1040,570,'样例中预测WAPE高于均值基线。',19)
        c.text(1040,610,'每折仅以前缀训练，不读取未来值。',18)
        c.text(1040,648,'测试窗含缺失/缺货删失时整折排除。',17,MUTED)
    else:
        c.text(1040,249,'forecast() 输出节选',20)
        lines=['{',f'  "model_name": "{f["model_name"]}",',f'  "horizon_days": {f["horizon_days"]},',
            f'  "daily_rate": {f["daily_rate"]},',f'  "p50_units": {f["p50_units"]},',
            f'  "p80_units": {f["p80_units"]},',f'  "p95_units": {f["p95_units"]}', '}']
        for i,line in enumerate(lines):c.text(1040,292+i*33,line,19)
        c.text(1040,600,'预测页读取同一计划的冻结历史。',19)
        c.text(1040,640,'补货另行结合库存、提前期和约束。',18,MUTED)


def scene(font,bold_index,stage,e,svg=False):
    c=Canvas(font,bold_index,svg)
    c.text(48,28,'销量预测流程',34)
    c.text(1070,40,'当前模型：'+MODEL_VERSION,18,MUTED)
    c.text(48,82,'历史销量 → 日需求序列 → 缺失/缺货处理 → 模型分流 → 需求分位；历史回测独立进行',22)
    c.line([(48,125),(1492,125)],INK,1)
    c.line([(1018,142),(1018,1138)],'#d6d6d6',1)
    # A conventional process diagram: rectangles, a quality decision, explicit branches.
    rect_node(c,(184,151,474,214),'平台订单行','Amazon / TikTok（Mock）',stage==0,22,18)
    rect_node(c,(544,151,836,214),'ERP / CSV 日销量','已规范化的历史销量',stage==0,22,18)
    for x in (329,690):c.arrow([(x,216),(x,220),(510,220),(510,232)],INK,1)
    rect_node(c,(184,232,836,300),'1. 规范为有效需求','ID映射 · 有效订单状态 · 版本去重 · 权威来源',stage==0)
    c.arrow([(510,302),(510,333)],INK,1)
    rect_node(c,(184,333,836,401),'2. 按 SKU × 仓库对齐完整日历','截至仓库当地昨日；分别保留 missing / stockout 标记',stage==1)
    c.arrow([(510,403),(510,420)],INK,1)
    points=[(510,420),(640,465),(510,510),(380,465),(510,420)]
    c.d.polygon(points,fill=TINT if stage==1 else BG)
    c.add(f'<polygon points="{" ".join(f"{x},{y}" for x,y in points)}" fill="{TINT if stage==1 else BG}"/>')
    c.line(points,BLUE if stage==1 else INK,2 if stage==1 else 1)
    c.text(470,442,'质量门禁',20)
    c.text(480,475,'通过？',20)
    c.text(665,452,'覆盖 / 时效 / 样本',18,MUTED)
    c.arrow([(378,465),(268,465)],INK,1);c.text(309,438,'否',18)
    rect_node(c,(48,432,266,500),'隔离 / 补齐 / 重放','修正后重新检查',False,21,18)
    c.arrow([(122,430),(122,267),(182,267)],INK,1)
    c.text(112,527,'异常不作为真实零',17,MUTED)
    c.arrow([(510,512),(510,546)],INK,1);c.text(528,519,'是',18)
    rect_node(c,(184,546,836,608),'3. 固定截止日，冻结本次输入','历史、策略、周期固定；保存并核验输入摘要',stage==1)
    c.arrow([(510,610),(510,644)],INK,1)
    rect_node(c,(184,644,836,714),'4. 仅用过去观察处理缺失 / 缺货','有历史时：max(已知销量, 前28次有效观察均值)',stage==2,23,18)
    # The implementation is an if/elif/else router, not automatic model search.
    for x in (187,505,829):c.arrow([(510,716),(510,750),(x,750),(x,792)],INK,1)
    model_boxes=[(48,792,326,904),(360,792,650,904),(684,792,974,904)]
    rows=[('5a. 类别均值冷启动','有效观察 < 7','用类别日均需求作先验*'),
          ('5b. TSB 间歇模型','否则，非零天数占比 <50%','发生概率 × 正销量规模'),
          ('5c. 阻尼周季节模型','否则；≥28天加入周季节项','近期水平 / 季节项：0.7 / 0.3')]
    for i,(xy,row) in enumerate(zip(model_boxes,rows)):
        rect_node(c,xy,row[0],row[1],stage==3 and i==2,21,17)
        c.text(xy[0]+14,xy[1]+79,row[2],17,MUTED)
        c.arrow([(xy[0]+(xy[2]-xy[0])/2,906),(xy[0]+(xy[2]-xy[0])/2,925),(510,925),(510,944)],INK,1)
    rect_node(c,(184,944,836,1040),'6. 输出日均需求、波动与需求分位','μ = 日需求 × 天数；σ = 日波动 × √天数',stage==4,23,19)
    c.text(200,1016,'P50 = ceil(μ)；P80 / P95 使用正态近似',17,MUTED)
    c.arrow([(510,1042),(510,1072)],INK,1)
    rect_node(c,(184,1072,836,1138),'7. 预测页 / 冻结计划明细','结果再供补货模块使用；本图不展开审批与执行',stage==6,23,18)
    # Parallel historical evaluation starts at the same frozen input, not at future actuals.
    c.arrow([(838,577),(999,577),(999,836),(1040,836)],BLUE if stage==5 else MUTED,2 if stage==5 else 1)
    c.text(854,594,'同一冻结历史',16,MUTED)
    c.text(1040,754,'独立评估：滚动回测',23,BLUE if stage==5 else INK)
    rect_node(c,(1040,806,1492,866),'历史前缀训练 → 预测后7天','至少28天起训；窗口每次向后移动7天',stage==5,22,17)
    c.arrow([(1266,868),(1266,889)],INK,1)
    rect_node(c,(1040,889,1492,941),'测试窗必须无缺失 / 无缺货删失','',stage==5,21)
    c.arrow([(1266,943),(1266,964)],INK,1)
    rect_node(c,(1040,964,1492,1016),'计算 WAPE / 偏差 / 均值基线','',stage==5,21)
    c.arrow([(1266,1018),(1266,1039)],INK,1)
    rect_node(c,(1040,1039,1492,1103),'效果评估页','按总体 / 品类 / ABC / 仓库分组',stage==6,22,17)
    c.text(1040,1124,'回测用于评估，不会自动择优换模。',17,MUTED)
    example_panel(c,stage,e)
    c.line([(48,1165),(1492,1165)],INK,1)
    c.text(48,1182,'* 冷启动是模型支持分支；低样本规划仍受数据门禁约束。分位不是已校准的履约承诺。',17,MUTED)
    c.text(48,1213,'当前：'+('完整预测流程与样例结果' if stage<0 else f'{stage+1}/7 '+STAGES[stage]),17,BLUE)
    return c


PATHS=[
    [[(329,216),(329,220),(510,220),(510,232)],[(510,302),(510,333)]],
    [[(510,403),(510,420)],[(510,512),(510,546)]],
    [[(510,610),(510,644)]],
    [[(510,716),(510,750),(829,750),(829,792)]],
    [[(829,906),(829,925),(510,925),(510,944)]],
    [[(838,577),(999,577),(999,836),(1040,836)],[(1266,868),(1266,889)]],
    [[(510,1042),(510,1072)],[(1266,1018),(1266,1039)]]
]


def point_on_path(points,t):
    lengths=[math.dist(a,b) for a,b in zip(points,points[1:])];distance=t*sum(lengths)
    for a,b,length in zip(points,points[1:],lengths):
        if distance<=length:
            k=distance/length if length else 0;return a[0]+(b[0]-a[0])*k,a[1]+(b[1]-a[1])*k
        distance-=length
    return points[-1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'docs/assets')
    parser.add_argument('--font',type=Path)
    parser.add_argument('--bold-index',type=int,default=None)
    parser.add_argument('--fps',type=int,default=8)
    args=parser.parse_args()
    if not 2<=args.fps<=15:parser.error('fps must be between 2 and 15')
    choices=[args.font,Path('/System/Library/Fonts/Hiragino Sans GB.ttc'),Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
    font=next((p for p in choices if p and p.exists()),None)
    if not font:parser.error('pass --font /path/to/CJK-font.ttc')
    bold_index=args.bold_index if args.bold_index is not None else 2 if 'Hiragino' in font.name else 0
    out=args.output;out.mkdir(parents=True,exist_ok=True);e=example()
    assert e['forecast']['model_name']=='damped-seasonal' and e['observed']==65
    poster=scene(str(font),bold_index,-1,e,svg=True)
    poster.im.save(out/'aegis-workflow.png',optimize=True)
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc"><title id="title">当前生产实现的销量预测流程</title><desc id="desc">原始销量、规范需求、完整日历与质量门禁、冻结输入、只用历史处理缺失和缺货、按样本量与非零比例分流、需求分位输出。冻结历史另行滚动回测，不自动择优换模。右侧固定合成样例由生产函数计算。</desc><rect width="{W}" height="{H}" fill="{BG}"/>'+''.join(poster.svg)+'</svg>'
    (out/'aegis-workflow.svg').write_text(svg,encoding='utf-8')
    bases=[scene(str(font),bold_index,i,e).im for i in range(7)]
    sample=Image.new('RGB',(770,620*7))
    for i,base in enumerate(bases):sample.paste(base.resize((770,620)),(0,620*i))
    palette=sample.quantize(colors=128,method=Image.Quantize.MEDIANCUT)
    frames=[];durations=[]
    for stage,base in enumerate(bases):
        count=6*args.fps
        for i in range(count):
            t=i/max(1,count-1);im=base.copy();draw=ImageDraw.Draw(im)
            for path in PATHS[stage]:
                x,y=point_on_path(path,t);draw.ellipse((x-4,y-4,x+4,y+4),fill=BLUE)
            # A thin time cursor gives readers six seconds per processing step.
            draw.rectangle((840,1223,840+int(652*t),1225),fill=BLUE)
            frames.append(im.quantize(palette=palette,dither=Image.Dither.NONE))
            durations.append(round((i+1)*100/args.fps)*10-round(i*100/args.fps)*10)
    gif=out/'aegis-workflow.gif'
    frames[0].save(gif,save_all=True,append_images=frames[1:],duration=durations,loop=0,optimize=True,disposal=1)
    with Image.open(gif) as image:
        total=sum(image.seek(i) or image.info['duration'] for i in range(image.n_frames))
        assert image.n_frames==7*6*args.fps and total==42000
        metadata={'focus':'actual forecasting pipeline','width':W,'height':H,'frames':image.n_frames,'duration_ms':total,
                  'bytes':gif.stat().st_size,'loop':0,'stages':STAGES,'model_version':MODEL_VERSION,'example':'fixed synthetic 70-day series, 14-day horizon',
                  'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'src/aegis/planning/forecast.py',ROOT/'src/aegis/planning/backtest.py']}}
    (out/'aegis-workflow.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    numerical={k:v for k,v in e.items() if k!='series'}
    numerical['series']={'sku':'S','warehouse_id':'W','daily_units':e['series'].daily_units,'missing_days':e['series'].missing_days,'stockout_days':e['series'].stockout_days}
    (out/'forecast-example.json').write_text(json.dumps(numerical,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(metadata,ensure_ascii=False))


if __name__=='__main__':main()
