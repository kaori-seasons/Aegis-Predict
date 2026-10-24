from __future__ import annotations

import json


def render_dashboard_html(plan: dict) -> str:
    data = json.dumps(plan, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>3C 配件海外仓数字孪生运营看板</title>
  <style>
    :root {{
      --ink: #1f2933;
      --muted: #667085;
      --line: #d9e2ec;
      --panel: #ffffff;
      --bg: #f6f8fb;
      --green: #147d64;
      --amber: #b76e00;
      --red: #c93d2e;
      --blue: #2f6fed;
      --teal: #15807c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 18px 24px 14px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 750;
      letter-spacing: 0;
    }}
    .subtitle {{ margin-top: 4px; color: var(--muted); max-width: 980px; }}
    main {{ padding: 18px 24px 28px; max-width: 1440px; margin: 0 auto; }}
    .tabs {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }}
    .tab {{
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      padding: 8px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 650;
    }}
    .tab.active {{
      border-color: var(--blue);
      color: var(--blue);
      background: #edf4ff;
    }}
    section {{ display: none; }}
    section.active {{ display: block; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ font-size: 26px; font-weight: 780; margin-top: 3px; }}
    .metric .delta {{ margin-top: 2px; font-size: 12px; color: var(--green); }}
    .layout {{
      display: grid;
      grid-template-columns: 1.15fr .85fr;
      gap: 14px;
      align-items: start;
    }}
    h2 {{ margin: 0 0 10px; font-size: 18px; letter-spacing: 0; }}
    h3 {{ margin: 0 0 8px; font-size: 15px; letter-spacing: 0; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 9px 8px;
      text-align: left;
      vertical-align: middle;
      overflow-wrap: anywhere;
    }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 700; }}
    td.num, th.num {{ text-align: right; }}
    .risk {{
      display: inline-flex;
      min-width: 56px;
      justify-content: center;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
    }}
    .risk.high {{ background: #fff0ed; color: var(--red); }}
    .risk.mid {{ background: #fff7e6; color: var(--amber); }}
    .risk.low {{ background: #e9f7f3; color: var(--green); }}
    .warehouse-map {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .warehouse {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
      min-height: 118px;
    }}
    .warehouse .name {{ font-weight: 780; font-size: 16px; }}
    .warehouse .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 10px; }}
    .bar {{
      height: 10px;
      background: #edf1f5;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 8px;
    }}
    .bar span {{ display: block; height: 100%; background: var(--teal); }}
    .note {{ color: var(--muted); margin-top: 10px; }}
    .split-3 {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .viz-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .viz-grid.two {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .viz-card {{
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 188px;
    }}
    .hbar-list {{ display: grid; gap: 9px; }}
    .hbar-row {{ display: grid; grid-template-columns: minmax(150px, 1.25fr) 2fr 64px; gap: 8px; align-items: center; }}
    .hbar-label {{ font-weight: 700; overflow-wrap: anywhere; }}
    .hbar-track {{ height: 12px; border-radius: 999px; background: #eef2f7; overflow: hidden; }}
    .hbar-fill {{ display: block; height: 100%; border-radius: inherit; background: var(--blue); }}
    .hbar-value {{ text-align: right; color: var(--muted); font-size: 12px; }}
    .heat-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
    .heat-cell {{ border: 1px solid var(--line); border-radius: 8px; padding: 9px; background: #f8fafc; }}
    .heat-cell strong {{ display: block; font-size: 13px; }}
    .heat-cell span {{ display: block; color: var(--muted); font-size: 12px; }}
    .paired-bars {{ display: grid; gap: 12px; }}
    .paired-row {{ display: grid; grid-template-columns: 92px 1fr; gap: 10px; align-items: center; }}
    .paired-label {{ font-weight: 700; }}
    .paired-track {{ display: grid; gap: 4px; }}
    .paired-track span {{ display: block; height: 10px; border-radius: 999px; background: #d8e2ef; }}
    .paired-track span.after {{ background: var(--teal); }}
    .legend {{ display: flex; gap: 12px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 10px; }}
    .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 999px; margin-right: 5px; vertical-align: -1px; }}
    .sparkline {{ width: 100%; height: 116px; display: block; }}
    .waterline {{
      height: 12px;
      background: #edf1f5;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 6px;
    }}
    .waterline span {{
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--teal), var(--blue));
    }}
    .map-stage {{
      position: relative;
      min-height: 440px;
      border: 1px solid #1e3850;
      border-radius: 8px;
      background: radial-gradient(circle at 78% 38%, rgba(70,182,200,.22), transparent 48%), linear-gradient(135deg, #071321, #0b2433 52%, #07111d);
      overflow: hidden;
    }}
    .map-stage::before {{
      content: "";
      position: absolute;
      inset: 0;
      background-image: url('/assets/world-land.svg');
      background-position: center;
      background-repeat: no-repeat;
      background-size: 100% 100%;
      opacity: .9;
      filter: drop-shadow(0 5px 4px rgba(2,10,18,.34));
      z-index: 0;
    }}
    .map-stage::after {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(90deg, rgba(171,205,235,.1) 1px, transparent 1px),
        linear-gradient(0deg, rgba(171,205,235,.1) 1px, transparent 1px);
      background-size: 120px 72px;
      z-index: 1;
    }}
    .static-map-routes {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      z-index: 2;
    }}
    .static-map-routes path {{
      fill: none;
      stroke-linecap: round;
      opacity: .82;
    }}
    .static-map-routes .glow {{ opacity: .2; }}
    .map-hub {{
      position: absolute;
      z-index: 4;
      width: 132px;
      padding: 8px 10px;
      border-radius: 8px;
      background: rgba(8,18,31,.88);
      border: 1px solid rgba(194,219,248,.18);
      color: #fff;
      font-weight: 800;
      transform: translate(-50%, 20px);
      box-shadow: 0 14px 26px rgba(0,18,38,.28);
    }}
    .map-hub span {{ display: block; color: #9db6d2; font-size: 11px; font-weight: 700; }}
    .map-node {{
      position: absolute;
      z-index: 5;
      width: 168px;
      min-height: 92px;
      padding: 10px;
      border: 1px solid rgba(194,219,248,.18);
      border-radius: 8px;
      background: rgba(8,18,31,.86);
      color: #f8fbff;
      box-shadow: 0 14px 28px rgba(0,18,38,.28);
      transform: translate(-50%, -50%);
    }}
    .map-node strong {{ display: block; margin-bottom: 2px; }}
    .map-node small {{ color: #a7bed8; display: block; }}
    .approval-card {{
      border: 1px solid var(--line);
      border-left: 4px solid var(--blue);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
    }}
    .approval-card strong {{ display: block; font-size: 15px; }}
    .approval-card .meta {{ color: var(--muted); margin-top: 4px; }}
    @media (max-width: 900px) {{
      main {{ padding: 14px; }}
      .grid, .layout, .warehouse-map, .split-3, .viz-grid, .viz-grid.two, .heat-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 20px; }}
      .metric .value {{ font-size: 22px; }}
      .hbar-row, .paired-row {{ grid-template-columns: 1fr; }}
      .hbar-value {{ text-align: left; }}
      .map-node {{ position: static; width: auto; transform: none; margin: 10px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>3C 配件海外仓数字孪生运营看板</h1>
    <div class="subtitle">15 个具体 SKU × 美国/德国/英国/澳洲海外仓 × 180 天模拟销量，用于展示 AI 预测、全球库存池和分仓优化闭环。</div>
  </header>
  <main>
    <nav class="tabs" aria-label="dashboard tabs">
      <button class="tab active" data-tab="pool">库存池总览</button>
      <button class="tab" data-tab="forecast">AI 需求预测</button>
      <button class="tab" data-tab="alerts">缺货预警</button>
      <button class="tab" data-tab="replenishment">补货优化</button>
      <button class="tab" data-tab="approval">补货审批</button>
      <button class="tab" data-tab="allocation">分仓履约</button>
      <button class="tab" data-tab="waterline">库存池水位</button>
      <button class="tab" data-tab="network">仓网地图</button>
      <button class="tab" data-tab="simulation">仿真评估</button>
    </nav>

    <section id="pool" class="active"></section>
    <section id="forecast"></section>
    <section id="alerts"></section>
    <section id="replenishment"></section>
    <section id="approval"></section>
    <section id="allocation"></section>
    <section id="waterline"></section>
    <section id="network"></section>
    <section id="simulation"></section>
  </main>
  <script id="plan-data" type="application/json">{data}</script>
  <script>
    const plan = JSON.parse(document.getElementById('plan-data').textContent);
    const fmt = new Intl.NumberFormat('zh-CN');
    const recommendations = plan.recommendations;
    const allocations = plan.allocations;
    const sim = plan.simulation_comparison;
    const alerts = plan.stockout_alerts || [];
    const approvals = plan.approval_queue || [];
    const poolLevels = plan.pool_levels || [];
    const warehouseMap = plan.warehouse_map || [];
    const displayNames = plan.display_names || {{}};
    const skuCatalog = displayNames.skus || {{}};
    const warehouseCatalog = displayNames.warehouses || {{}};
    const countryCatalog = {{US: '美国', DE: '德国', UK: '英国', AU: '澳洲'}};
    const regionCatalog = {{'North America': '北美', Europe: '欧洲', Oceania: '大洋洲'}};

    function skuName(sku) {{
      return skuCatalog[sku] || sku;
    }}
    function warehouseName(warehouseId) {{
      return warehouseCatalog[warehouseId] || warehouseId;
    }}
    function sourceName(source) {{
      return source === 'GLOBAL-POOL' ? '深圳中心库存池' : warehouseName(source);
    }}
    function countryRegion(row) {{
      return `${{countryCatalog[row.country] || row.country}} / ${{regionCatalog[row.region] || row.region}}`;
    }}

    function riskClass(value) {{
      if (value >= 0.65) return 'high';
      if (value >= 0.35) return 'mid';
      return 'low';
    }}
    function riskText(value) {{
      if (value >= 0.65) return '红色';
      if (value >= 0.35) return '黄色';
      return '绿色';
    }}
    function table(headers, rows) {{
      return `<table><thead><tr>${{headers.map(h => `<th class="${{h.cls || ''}}">${{h.label}}</th>`).join('')}}</tr></thead><tbody>${{rows.join('')}}</tbody></table>`;
    }}
    function valueOf(value) {{
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }}
    function hbarChart(title, rows, formatter = value => fmt.format(value)) {{
      const max = Math.max(1, ...rows.map(row => valueOf(row.value)));
      const bars = rows.map(row => {{
        const width = Math.max(3, Math.min(100, valueOf(row.value) / max * 100));
        const color = row.color || 'var(--blue)';
        return `<div class="hbar-row">
          <div class="hbar-label">${{row.label}}</div>
          <div class="hbar-track"><span class="hbar-fill" style="width:${{width}}%; background:${{color}}"></span></div>
          <div class="hbar-value">${{formatter(row.value)}}</div>
        </div>`;
      }}).join('');
      return `<div class="viz-card"><h3>${{title}}</h3><div class="hbar-list">${{bars || '<p class="note">暂无数据</p>'}}</div></div>`;
    }}
    function heatCells(rows) {{
      return rows.map(row => {{
        const risk = valueOf(row.risk);
        const background = risk >= 0.65 ? '#fff0ed' : risk >= 0.35 ? '#fff7e6' : '#e9f7f3';
        const border = risk >= 0.65 ? '#f2b8af' : risk >= 0.35 ? '#f0cf87' : '#9fd7c6';
        return `<div class="heat-cell" style="background:${{background}}; border-color:${{border}}">
          <strong>${{row.label}}</strong>
          <span>风险 ${{risk.toFixed(2)}} / 缺口 ${{fmt.format(row.unmet || 0)}}</span>
        </div>`;
      }}).join('');
    }}
    function pairedBars(title, rows, lowerIsBetter = false) {{
      const max = Math.max(1, ...rows.flatMap(row => [valueOf(row.before), valueOf(row.after)]));
      const body = rows.map(row => {{
        const beforeWidth = Math.max(3, Math.min(100, valueOf(row.before) / max * 100));
        const afterWidth = Math.max(3, Math.min(100, valueOf(row.after) / max * 100));
        const lower = row.lowerIsBetter ?? lowerIsBetter;
        const direction = lower ? valueOf(row.before) - valueOf(row.after) : valueOf(row.after) - valueOf(row.before);
        const mark = direction >= 0 ? '改善' : '回落';
        return `<div class="paired-row">
          <div class="paired-label">${{row.label}}</div>
          <div class="paired-track">
            <span style="width:${{beforeWidth}}%"></span>
            <span class="after" style="width:${{afterWidth}}%"></span>
            <small class="note">${{row.before}} -> ${{row.after}}，${{mark}}</small>
          </div>
        </div>`;
      }}).join('');
      return `<div class="viz-card"><h3>${{title}}</h3><div class="paired-bars">${{body}}</div><div class="legend"><span><i style="background:#d8e2ef"></i>传统</span><span><i style="background:var(--teal)"></i>智能库存池</span></div></div>`;
    }}
    function sparklineChart(title, rows) {{
      const max = Math.max(1, ...rows.flatMap(row => [valueOf(row.p50_units), valueOf(row.p80_units), valueOf(row.p95_units)]));
      const series = rows.slice(0, 8);
      const step = series.length > 1 ? 300 / (series.length - 1) : 300;
      function points(field) {{
        return series.map((row, index) => {{
          const x = 18 + index * step;
          const y = 100 - (valueOf(row[field]) / max * 82);
          return `${{x}},${{y}}`;
        }}).join(' ');
      }}
      const labels = series.map((row, index) => {{
        const x = 18 + index * step;
        return `<text x="${{x}}" y="112" text-anchor="middle" font-size="9" fill="#667085">${{skuName(row.sku)}}</text>`;
      }}).join('');
      return `<div class="viz-card"><h3>${{title}}</h3>
        <svg class="sparkline" viewBox="0 0 340 120" role="img" aria-label="${{title}}">
          <polyline points="${{points('p95_units')}}" fill="none" stroke="#c93d2e" stroke-width="2.5" stroke-linecap="round"/>
          <polyline points="${{points('p80_units')}}" fill="none" stroke="#2f6fed" stroke-width="2.5" stroke-linecap="round"/>
          <polyline points="${{points('p50_units')}}" fill="none" stroke="#15807c" stroke-width="2.5" stroke-linecap="round"/>
          ${{labels}}
        </svg>
        <div class="legend"><span><i style="background:#15807c"></i>P50</span><span><i style="background:#2f6fed"></i>P80</span><span><i style="background:#c93d2e"></i>P95</span></div>
      </div>`;
    }}
    function poolView() {{
      const byWarehouse = {{}};
      recommendations.forEach(row => {{
        byWarehouse[row.warehouse_id] ??= {{need: 0, position: 0, risk: 0, count: 0}};
        byWarehouse[row.warehouse_id].need += row.recommended_units;
        byWarehouse[row.warehouse_id].position += row.inventory_position;
        byWarehouse[row.warehouse_id].risk += row.urgency;
        byWarehouse[row.warehouse_id].count += 1;
      }});
      const warehouses = Object.entries(byWarehouse).map(([id, row]) => {{
        const avgRisk = row.risk / row.count;
        const width = Math.max(8, Math.min(100, Math.round((1 - avgRisk) * 100)));
        return `<div class="warehouse">
          <div class="name">${{warehouseName(id)}}</div>
          <div class="meta">风险状态 <span class="risk ${{riskClass(avgRisk)}}">${{riskText(avgRisk)}}</span></div>
          <div>库存位置：${{fmt.format(row.position)}} 件</div>
          <div>建议补货：${{fmt.format(row.need)}} 件</div>
          <div class="bar"><span style="width:${{width}}%"></span></div>
        </div>`;
      }}).join('');
      const warehouseRisk = Object.entries(byWarehouse).map(([id, row]) => ({{
        label: warehouseName(id),
        value: Number((row.risk / row.count).toFixed(3)),
        color: (row.risk / row.count) >= 0.65 ? 'var(--red)' : (row.risk / row.count) >= 0.35 ? 'var(--amber)' : 'var(--green)'
      }})).sort((a, b) => b.value - a.value);
      const poolUsage = poolLevels.map(row => ({{
        label: skuName(row.sku),
        value: row.waterline,
        color: row.waterline >= 0.8 ? 'var(--red)' : row.waterline >= 0.5 ? 'var(--amber)' : 'var(--teal)'
      }})).sort((a, b) => b.value - a.value);
      const simRows = [
        {{label:'Fill Rate', before: sim.baseline.fill_rate, after: sim.intelligent.fill_rate}},
        {{label:'总利润', before: sim.baseline.total_profit, after: sim.intelligent.total_profit}},
        {{label:'平均库存', before: sim.baseline.average_inventory_units, after: sim.intelligent.average_inventory_units}},
      ];
      document.getElementById('pool').innerHTML = `
        <div class="grid">
          <div class="metric"><div class="label">预测周期</div><div class="value">${{plan.horizon_days}}天</div><div class="delta">滚动计划输入</div></div>
          <div class="metric"><div class="label">补货建议</div><div class="value">${{recommendations.length}}</div><div class="delta">SKU-仓维度</div></div>
          <div class="metric"><div class="label">缺货预警</div><div class="value">${{alerts.length}}</div><div class="delta">按紧急度与缺口排序</div></div>
          <div class="metric"><div class="label">Fill Rate 提升</div><div class="value">+${{sim.improvement.fill_rate_lift_pct}}pct</div><div class="delta">对比传统独立备货</div></div>
        </div>
        <div class="viz-grid">
          ${{hbarChart('仓库风险排行', warehouseRisk, value => value.toFixed(2))}}
          ${{hbarChart('库存池使用率', poolUsage, value => `${{Math.round(value * 100)}}%`)}}
          ${{pairedBars('核心指标对比', simRows)}}
        </div>
        <div class="layout">
          <div class="panel"><h2>全球库存池风险地图</h2><div class="warehouse-map">${{warehouses}}</div></div>
          <div class="panel"><h2>模型与数据</h2><p>${{plan.model_pipeline.active_model}}</p><p class="note">${{plan.model_pipeline.feature_sources.join(' / ')}}</p><p class="note">${{plan.model_pipeline.notes}}</p></div>
        </div>`;
    }}
    function forecastView() {{
      const topForecast = recommendations.slice().sort((a, b) => b.p80_units - a.p80_units).slice(0, 8);
      const demandBars = topForecast.map(row => ({{
        label: `${{skuName(row.sku)}} / ${{warehouseName(row.warehouse_id)}}`,
        value: row.p80_units,
        color: row.urgency >= 0.65 ? 'var(--red)' : 'var(--blue)'
      }}));
      const rows = recommendations.slice(0, 10).map(row => `<tr>
        <td>${{skuName(row.sku)}}</td><td>${{warehouseName(row.warehouse_id)}}</td><td class="num">${{row.daily_rate}}</td>
        <td class="num">${{fmt.format(row.p80_units)}}</td><td class="num">${{fmt.format(row.p95_units)}}</td>
        <td><span class="risk ${{riskClass(row.urgency)}}">${{riskText(row.urgency)}}</span></td>
      </tr>`);
      document.getElementById('forecast').innerHTML = `<div class="viz-grid two">
        ${{sparklineChart('需求分位走势 Top 8', topForecast)}}
        ${{hbarChart('P80 需求排行', demandBars)}}
      </div><div class="panel"><h2>AI 需求预测</h2>${{table([
        {{label:'SKU'}}, {{label:'海外仓'}}, {{label:'日均预测', cls:'num'}}, {{label:'P80 需求', cls:'num'}}, {{label:'P95 需求', cls:'num'}}, {{label:'风险'}}
      ], rows)}}</div>`;
    }}
    function alertsView() {{
      const heat = alerts.slice(0, 12).map(row => ({{
        label: `${{skuName(row.sku)}} / ${{warehouseName(row.warehouse_id)}}`,
        risk: row.urgency,
        unmet: row.unmet_units
      }}));
      const unmetBars = alerts.slice(0, 8).map(row => ({{
        label: `${{skuName(row.sku)}} / ${{warehouseName(row.warehouse_id)}}`,
        value: row.unmet_units,
        color: row.severity === '红色' ? 'var(--red)' : 'var(--amber)'
      }}));
      const rows = alerts.map(row => `<tr>
        <td>${{skuName(row.sku)}}</td><td>${{warehouseName(row.warehouse_id)}}</td><td><span class="risk ${{row.severity === '红色' ? 'high' : 'mid'}}">${{row.severity}}</span></td>
        <td class="num">${{row.urgency}}</td><td class="num">${{fmt.format(row.unmet_units)}}</td><td>${{row.message}}</td>
      </tr>`);
      document.getElementById('alerts').innerHTML = `<div class="viz-grid two">
        <div class="viz-card"><h3>风险热力单元</h3><div class="heat-grid">${{heatCells(heat) || '<p class="note">暂无预警</p>'}}</div></div>
        ${{hbarChart('缺口数量排行', unmetBars)}}
      </div><div class="panel"><h2>缺货预警</h2>${{table([
        {{label:'SKU'}}, {{label:'海外仓'}}, {{label:'等级'}}, {{label:'紧急度', cls:'num'}}, {{label:'缺口', cls:'num'}}, {{label:'处理建议'}}
      ], rows)}}</div>`;
    }}
    function replenishmentView() {{
      const recBars = recommendations.slice().sort((a, b) => b.recommended_units - a.recommended_units).slice(0, 8).map(row => ({{
        label: `${{skuName(row.sku)}} / ${{warehouseName(row.warehouse_id)}}`,
        value: row.recommended_units,
        color: row.approval_required ? 'var(--amber)' : 'var(--teal)'
      }}));
      const inventoryPressure = recommendations.slice().sort((a, b) => b.urgency - a.urgency).slice(0, 8).map(row => ({{
        label: `${{skuName(row.sku)}} / ${{warehouseName(row.warehouse_id)}}`,
        value: Math.max(0, row.reorder_point - row.inventory_position),
        color: row.urgency >= 0.65 ? 'var(--red)' : 'var(--blue)'
      }}));
      const rows = recommendations.map(row => `<tr>
        <td>${{skuName(row.sku)}}</td><td>${{warehouseName(row.warehouse_id)}}</td><td class="num">${{fmt.format(row.inventory_position)}}</td>
        <td class="num">${{fmt.format(row.reorder_point)}}</td><td class="num">${{fmt.format(row.target_stock)}}</td>
        <td class="num">${{fmt.format(row.recommended_units)}}</td><td>${{row.approval_required ? '待审批' : '自动通过'}}</td><td>${{row.reason}}</td>
      </tr>`);
      document.getElementById('replenishment').innerHTML = `<div class="viz-grid two">
        ${{hbarChart('建议补货量 Top 8', recBars)}}
        ${{hbarChart('补货点缺口压力', inventoryPressure)}}
      </div><div class="panel"><h2>补货优化结果</h2>${{table([
        {{label:'SKU'}}, {{label:'海外仓'}}, {{label:'库存位置', cls:'num'}}, {{label:'再订货点', cls:'num'}}, {{label:'目标库存', cls:'num'}}, {{label:'建议补货', cls:'num'}}, {{label:'审批'}}, {{label:'原因'}}
      ], rows)}}</div>`;
    }}
    function approvalView() {{
      const cards = approvals.map(row => `<div class="approval-card">
        <strong>${{skuName(row.sku)}} -> ${{warehouseName(row.warehouse_id)}}</strong>
        <div class="meta">建议 ${{fmt.format(row.recommended_units)}} 件，已分配 ${{fmt.format(row.allocated_units)}} 件，${{row.transport_mode}}，预计毛利 ${{fmt.format(row.estimated_margin)}}</div>
        <div class="meta">${{row.reason}}</div>
      </div>`).join('');
      document.getElementById('approval').innerHTML = `<div class="panel"><h2>补货审批队列</h2><div class="split-3">${{cards || '<p class="note">当前没有需要人工审批的补货单。</p>'}}</div></div>`;
    }}
    function allocationView() {{
      const transportTotals = {{}};
      allocations.forEach(row => {{
        const modes = String(row.transport_mode || '未分配').split('+').filter(mode => mode && mode !== '未分配');
        const share = modes.length ? row.allocated_units / modes.length : 0;
        modes.forEach(mode => transportTotals[mode] = (transportTotals[mode] || 0) + share);
      }});
      const transportRows = Object.entries(transportTotals).map(([label, value]) => ({{
        label,
        value,
        color: label === 'AIR' ? 'var(--amber)' : 'var(--teal)'
      }}));
      const allocationRows = allocations.slice().sort((a, b) => b.allocated_units - a.allocated_units).slice(0, 8).map(row => ({{
        label: `${{skuName(row.sku)}} / ${{warehouseName(row.warehouse_id)}}`,
        value: row.allocated_units,
        color: row.unmet_units > 0 ? 'var(--amber)' : 'var(--blue)'
      }}));
      const rows = allocations.map(row => `<tr>
        <td>${{skuName(row.sku)}}</td><td>${{warehouseName(row.warehouse_id)}}</td><td>${{sourceName(row.source)}}</td>
        <td>${{row.transport_mode}}</td><td class="num">${{fmt.format(row.allocated_units)}}</td><td class="num">${{fmt.format(row.unmet_units)}}</td>
        <td class="num">${{row.lead_time_days}}天</td><td class="num">${{row.priority_score}}</td><td>${{row.approval_status}}</td><td>${{row.optimizer}}</td><td>${{row.capacity_note}}</td>
      </tr>`);
      document.getElementById('allocation').innerHTML = `<div class="viz-grid two">
        ${{hbarChart('运输方式分配', transportRows)}}
        ${{hbarChart('分仓数量 Top 8', allocationRows)}}
      </div><div class="panel"><h2>库存池分仓与履约决策</h2>${{table([
        {{label:'SKU'}}, {{label:'目标仓'}}, {{label:'来源'}}, {{label:'运输'}}, {{label:'分配数量', cls:'num'}}, {{label:'缺口', cls:'num'}}, {{label:'到仓', cls:'num'}}, {{label:'优先级', cls:'num'}}, {{label:'审批'}}, {{label:'优化器'}}, {{label:'约束说明'}}
      ], rows)}}</div>`;
    }}
    function waterlineView() {{
      const cards = poolLevels.map(row => `<div class="metric">
        <div class="label">${{skuName(row.sku)}}</div>
        <div class="value">${{fmt.format(row.remaining_units)}}</div>
        <div class="delta">剩余 / 可分配：${{fmt.format(row.allocatable_units)}}，已分配 ${{fmt.format(row.allocated_units)}}</div>
        <div class="waterline"><span style="width:${{Math.max(4, Math.min(100, row.waterline * 100))}}%"></span></div>
      </div>`).join('');
      const usageRows = poolLevels.map(row => ({{
        label: skuName(row.sku),
        value: row.allocated_units,
        color: row.remaining_units <= row.allocatable_units * 0.2 ? 'var(--red)' : 'var(--teal)'
      }})).sort((a, b) => b.value - a.value);
      const rows = poolLevels.map(row => `<tr><td>${{skuName(row.sku)}}</td><td class="num">${{fmt.format(row.available_units)}}</td><td class="num">${{fmt.format(row.reserved_units)}}</td><td class="num">${{fmt.format(row.allocatable_units)}}</td><td class="num">${{fmt.format(row.allocated_units)}}</td><td class="num">${{fmt.format(row.remaining_units)}}</td></tr>`);
      document.getElementById('waterline').innerHTML = `<div class="grid">${{cards}}</div><div class="viz-grid two">${{hbarChart('库存池已分配量', usageRows)}}<div class="viz-card"><h3>水位判读</h3><p class="note">红色代表剩余库存低于可分配库存 20%，蓝绿色代表仍有可调度余量。这里适合给采购和调拨审批快速判断哪些 SKU 接近耗尽。</p></div></div><div class="panel"><h2>库存池水位明细</h2>${{table([
        {{label:'SKU'}}, {{label:'可用库存', cls:'num'}}, {{label:'保留库存', cls:'num'}}, {{label:'可分配', cls:'num'}}, {{label:'已分配', cls:'num'}}, {{label:'剩余', cls:'num'}}
      ], rows)}}</div>`;
    }}
    function networkView() {{
      const project = (longitude, latitude) => ({{
        x: Math.max(2, Math.min(98, ((Number(longitude) + 180) / 360) * 100)),
        y: Math.max(6, Math.min(94, ((84 - Number(latitude)) / 168) * 100)),
      }});
      const hub = project(114.0579, 22.5431);
      const routeColor = row => row.risk >= 0.65 ? '#c93d2e' : row.risk >= 0.35 ? '#b76e00' : '#15807c';
      const routes = warehouseMap.map((row, index) => {{
        const point = project(row.longitude, row.latitude);
        const lift = row.warehouse_id === 'AU-SYD' ? 18 : row.warehouse_id === 'US-LAX' ? -22 : -12 - index * 2;
        const d = `M ${{hub.x}} ${{hub.y}} C ${{hub.x - 12}} ${{hub.y + lift}}, ${{point.x + (hub.x - point.x) * .35}} ${{point.y + lift}}, ${{point.x}} ${{point.y}}`;
        return `<path class="glow" d="${{d}}" stroke="${{routeColor(row)}}" stroke-width="3.4"/><path d="${{d}}" stroke="${{routeColor(row)}}" stroke-width="1.4"/>`;
      }}).join('');
      const nodes = warehouseMap.map(row => {{
        const point = project(row.longitude, row.latitude);
        return `<div class="map-node" style="left:${{point.x}}%; top:${{point.y}}%">
          <strong>${{warehouseName(row.warehouse_id)}}</strong>
          <small>${{countryRegion(row)}}</small>
          <small>入仓 ${{fmt.format(row.planned_inbound)}} 件</small>
          <small>仓容 ${{Math.round(row.usage_rate * 100)}}%，风险 ${{row.risk}}</small>
        </div>`;
      }}).join('');
      const hubNode = `<div class="map-hub" style="left:${{hub.x}}%; top:${{hub.y}}%">深圳中心库存池<span>中心供应池</span></div>`;
      document.getElementById('network').innerHTML = `<div class="panel"><h2>仓网地图</h2><div class="map-stage"><svg class="static-map-routes" viewBox="0 0 100 100" preserveAspectRatio="none">${{routes}}</svg>${{hubNode}}${{nodes}}</div><p class="note">世界陆地底图使用本地 Natural Earth 1:110m 边界，按经纬度定位仓库、中心库存池和补货航线。</p></div>`;
    }}
    function simulationView() {{
      const b = sim.baseline;
      const s = sim.intelligent;
      const rows = [
        ['订单满足率', b.fill_rate, s.fill_rate, `+${{sim.improvement.fill_rate_lift_pct}}pct`],
        ['缺货率', b.stockout_rate, s.stockout_rate, `-${{sim.improvement.stockout_rate_reduction_pct}}pct`],
        ['平均配送时效', `${{b.average_delivery_days}}天`, `${{s.average_delivery_days}}天`, `-${{sim.improvement.delivery_days_reduction}}天`],
        ['平均库存', `${{b.average_inventory_units}}件`, `${{s.average_inventory_units}}件`, `-${{sim.improvement.inventory_reduction_units}}件`],
        ['总利润', b.total_profit, s.total_profit, `+${{sim.improvement.profit_lift}}`],
      ].map(row => `<tr><td>${{row[0]}}</td><td class="num">${{row[1]}}</td><td class="num">${{row[2]}}</td><td class="num">${{row[3]}}</td></tr>`);
      const kpiPairs = [
        {{label:'Fill Rate', before: b.fill_rate, after: s.fill_rate}},
        {{label:'缺货率', before: b.stockout_rate, after: s.stockout_rate, lowerIsBetter:true}},
        {{label:'配送时效', before: b.average_delivery_days, after: s.average_delivery_days, lowerIsBetter:true}},
        {{label:'平均库存', before: b.average_inventory_units, after: s.average_inventory_units, lowerIsBetter:true}},
      ];
      const liftRows = [
        {{label:'Fill Rate', value: sim.improvement.fill_rate_lift_pct, color:'var(--green)'}},
        {{label:'缺货率下降', value: sim.improvement.stockout_rate_reduction_pct, color:'var(--green)'}},
        {{label:'时效缩短', value: sim.improvement.delivery_days_reduction, color:'var(--teal)'}},
        {{label:'库存下降', value: sim.improvement.inventory_reduction_units, color:'var(--teal)'}},
      ];
      document.getElementById('simulation').innerHTML = `<div class="viz-grid two">
        ${{pairedBars('仿真 KPI 对比', kpiPairs)}}
        ${{hbarChart('改善贡献', liftRows)}}
      </div><div class="panel"><h2>90天滚动仿真评估</h2>${{table([
        {{label:'指标'}}, {{label:'传统独立备货', cls:'num'}}, {{label:'智能库存池方案', cls:'num'}}, {{label:'改善', cls:'num'}}
      ], rows)}}<p class="note">${{sim.interpretation}}</p></div>`;
    }}

    poolView(); forecastView(); alertsView(); replenishmentView(); approvalView(); allocationView(); waterlineView(); networkView(); simulationView();
    document.querySelectorAll('.tab').forEach(button => {{
      button.addEventListener('click', () => {{
        document.querySelectorAll('.tab').forEach(item => item.classList.remove('active'));
        document.querySelectorAll('section').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        document.getElementById(button.dataset.tab).classList.add('active');
      }});
    }});
  </script>
</body>
</html>"""
