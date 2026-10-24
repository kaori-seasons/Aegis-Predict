import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve('competition/project_intro_ppt');
const templatePath = '/Users/calm/.agents/skills/guizang-ppt-skill/assets/template-swiss.html';
const outPath = path.join(root, 'index.html');
const metrics = JSON.parse(fs.readFileSync(path.join(root, 'project_metrics.json'), 'utf8'));
const m = metrics.dashboard.metrics;
const model = metrics.plan_summary.model_pipeline;
const comparison = metrics.simulation_compare.comparison;
const cmp = comparison.improvement;
const baseline = comparison.baseline;
const intelligent = comparison.intelligent;
const recCount = metrics.plan_summary.recommendation_count;
const allocationCount = metrics.plan_summary.allocation_count;
const skuCount = metrics.plan_summary.sku_count;
const warehouseCount = metrics.plan_summary.warehouse_count;
const rollingCount = metrics.forecast.rolling_summary.length;
const inboundCount = metrics.replenishment.in_transit_shipments.length;
const orders = metrics.realtime_orders.orders.length;
const scenarioLabel = metrics.simulation_compare.simulation.scenario_label || '黑五促销';
const clock = (metrics.dom_summary.clock || '').replace('已暂停 / ', '').replace(`${scenarioLabel} / `, '');
const fmt = new Intl.NumberFormat('zh-CN');
const pct = (v) => `${Math.round(Number(v) * 1000) / 10}%`;
const money = (v) => fmt.format(Math.round(Number(v)));

const img = (src, slot, cls = 'frame-img r-16x10 fit-contain') => `<figure class="${cls}" data-image-slot="${slot}"><img data-image-slot="${slot}" src="${src}" alt="${slot}" /></figure>`;

const slides = `
<section class="slide accent" data-layout="SWISS-COVER-ASCII" data-animate="hero">
  <div class="canvas-card">
    <canvas class="ascii-bg" aria-hidden="true"></canvas>
    <div class="chrome-min">
      <div class="l">GLOBALPOOL AI · PROJECT INTRO</div>
      <div class="r">REALTIME SANDBOX · 01 / 10</div>
    </div>
    <div style="flex:1;padding:0;display:grid;grid-template-rows:auto 1fr auto;gap:2.6vh">
      <div data-anim="kicker" class="t-meta" style="color:rgba(255,255,255,.78);letter-spacing:.22em">FORECAST · REPLENISHMENT · ALLOCATION · SIMULATION</div>
      <h1 data-anim="title" style="align-self:center;font-family:var(--sans),var(--sans-zh);font-weight:200;font-size:min(9.8vw,17vh);line-height:.94;letter-spacing:-.035em;color:#fff">实时海外仓<br/>经营<span style="font-style:italic;font-weight:300">沙盘</span></h1>
      <div data-anim="bottom" style="display:grid;grid-template-rows:auto auto;gap:1.6vh;border-top:1px solid rgba(255,255,255,.22);padding-top:2vh">
        <div data-anim="lead" class="lead" style="max-width:62ch;color:rgba(255,255,255,.86)">从订单流驱动库存扣减，用机器学习预测需求，用整数规划做库存池分仓，再用仿真评估策略收益。</div>
        <div style="display:flex;justify-content:space-between;align-items:end">
          <div class="t-meta" style="color:rgba(255,255,255,.62)">Captured from project web · ${metrics.captured_at}</div>
          <div class="t-meta" style="color:rgba(255,255,255,.62)">${scenarioLabel} · ${clock}</div>
        </div>
      </div>
    </div>
  </div>
</section>

<section class="slide split" data-layout="S03" data-animate="split-statement">
  <div class="canvas-card">
    <div class="split-half">
      <div class="half b-accent" style="justify-content:space-between">
        <div class="chrome-min"><div class="l">02 / 10</div><div class="r">PROBLEM</div></div>
        <div data-anim="left" style="display:flex;flex-direction:column;gap:2.4vh">
          <div class="t-meta on-dark">CORE TENSION</div>
          <h2 style="font-family:var(--sans),var(--sans-zh);font-weight:200;font-size:min(7vw,12vh);line-height:.95;letter-spacing:-.03em;color:#fff">总库存够，<br/>局部仍缺货。</h2>
        </div>
        <div class="t-body-sm" style="color:rgba(255,255,255,.78);max-width:38ch">跨境业务真正难的不是生成一张补货表，而是在订单波动、运输时效、仓容约束和区域需求差异之间持续做经营决策。</div>
      </div>
      <div class="half b-grey r-border" style="justify-content:space-between">
        <div class="chrome-min"><div class="l">WHY NOW</div><div class="r">SYSTEM VIEW</div></div>
        <div data-anim="right" style="display:grid;grid-template-columns:1fr;gap:2vh">
          ${['实时订单会持续扣减库存，风险不是静态字段，而是随时间变化的状态。','预测必须落到 SKU × 仓库粒度，才能回答“未来会卖多少、哪里先缺”。','补货与分仓要同时考虑安全库存、在途、仓容、采购预算和运输方式。','仿真不是装饰图表，而是用同一批需求检验传统方案与智能方案的差异。'].map((t,i)=>`<div style="display:grid;grid-template-columns:auto 1fr;gap:1.4vw;padding:2.2vh 0;border-top:1px solid var(--border-subtle)"><div style="font-family:var(--sans);font-weight:250;font-size:3.2vw;color:${i===3?'var(--accent)':'var(--ink)'}">0${i+1}</div><p class="t-body" style="margin-top:.4vh;color:var(--text-secondary)">${t}</p></div>`).join('')}
        </div>
      </div>
    </div>
  </div>
</section>

<section class="slide" data-layout="S06" data-animate="bar-grow">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">03 / 10 · DATA SCOPE</div><div class="r">REAL WEB RUN ${metrics.job_id}</div></div>
    <div style="display:grid;grid-template-columns:42% 58%;gap:4vw;align-items:start;margin-top:4vh">
      <div data-anim="text" style="display:flex;flex-direction:column;gap:2.4vh">
        <div class="t-meta">CURRENT PROJECT STATE</div>
        <h2 style="font-family:var(--sans),var(--sans-zh);font-weight:200;font-size:min(5.6vw,9vh);line-height:1;letter-spacing:-.035em">不是上传样例页，<br/>而是实时经营页。</h2>
        <p class="t-body" style="max-width:46ch;color:var(--text-secondary)">网页采集自实时模拟沙盘：${scenarioLabel} 场景推进 ${metrics.sim_steps} 个模拟小时，冻结在 ${clock}。页面与 API 指标来自同一个 run。</p>
      </div>
      <div class="bar-towers" data-anim="bars" style="min-height:54vh">
        <div class="bar-tower"><div class="cap"></div><div class="body-block h-2"><div class="lbl">SKU</div><div class="nb">${skuCount}</div><div class="sub">3C 配件商品</div></div></div>
        <div class="bar-tower"><div class="cap"></div><div class="body-block h-1"><div class="lbl">WAREHOUSE</div><div class="nb">${warehouseCount}</div><div class="sub">美 / 德 / 英 / 澳海外仓</div></div></div>
        <div class="bar-tower"><div class="cap"></div><div class="body-block h-3"><div class="lbl">TRAIN</div><div class="nb">${fmt.format(model.training_samples)}</div><div class="sub">训练样本</div></div></div>
        <div class="bar-tower"><div class="cap"></div><div class="body-block h-4 b-accent"><div class="lbl">SIGNAL</div><div class="nb">${model.signal_count}</div><div class="sub">SKU × 仓市场信号</div></div></div>
      </div>
    </div>
  </div>
</section>

<section class="slide" data-layout="S17" data-animate="system-diagram">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">04 / 10 · SYSTEM LOOP</div><div class="r">OPERATING SANDBOX</div></div>
    <div style="display:grid;grid-template-columns:48% 52%;gap:4vw;align-items:end;margin-top:4vh">
      <h2 style="font-family:var(--sans),var(--sans-zh);font-size:min(5.2vw,8.4vh);font-weight:200;line-height:1;letter-spacing:-.035em">把库存问题变成<br/>可滚动执行的闭环。</h2>
      <p class="t-body" style="color:var(--text-secondary);max-width:52ch">实时订单先改变库存状态，预测层刷新未来需求，补货层输出安全库存与订货建议，库存池分仓层把货分到最该去的位置，最后由仿真层对比策略结果。</p>
    </div>
    <div data-anim="diagram" style="display:grid;grid-template-columns:repeat(5,1fr);gap:1vw;margin-top:6vh;align-items:stretch">
      ${[
        ['01','订单流','扣减库存 / 生成异常'],
        ['02','预测补货','P50 / P80 / P95'],
        ['03','补货决策','安全库存 / 在途抵扣'],
        ['04','库存池分仓','仓容 / 预算 / 运输约束'],
        ['05','仿真对比','传统 vs 智能策略']
      ].map((x,i)=>`<div style="min-height:28vh;background:${i===1||i===3?'var(--accent)':'var(--grey-1)'};color:${i===1||i===3?'var(--accent-on)':'var(--ink)'};padding:2.6vh 1.4vw;display:flex;flex-direction:column;justify-content:space-between"><div class="t-meta" style="color:currentColor;opacity:.72">${x[0]}</div><div><h3 style="font-family:var(--sans),var(--sans-zh);font-weight:400;font-size:max(20px,2vw);line-height:1.1;margin-bottom:1.2vh">${x[1]}</h3><p style="font-family:var(--sans),var(--sans-zh);font-size:max(15px,.95vw);line-height:1.5;opacity:.82">${x[2]}</p></div></div>`).join('')}
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:2vw;margin-top:4vh;border-top:1px solid var(--border-subtle);padding-top:2vh">
      <p class="t-body-sm">实时沙盘：小时级订单与库存变化。</p>
      <p class="t-body-sm">决策引擎：按 6 个模拟小时滚动补货。</p>
      <p class="t-body-sm">仿真评估：用统一需求流比较策略。</p>
    </div>
  </div>
</section>

<section class="slide" data-layout="S11" data-animate="statement">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">05 / 10 · REALTIME CADENCE</div><div class="r">D181 → D182</div></div>
    <div style="display:grid;grid-template-columns:34% 66%;gap:4vw;margin-top:5vh;align-items:start">
      <div>
        <div class="t-meta">LIVE CLOCK</div>
        <h2 style="font-family:var(--sans),var(--sans-zh);font-weight:200;font-size:min(6.6vw,10.4vh);line-height:.93;letter-spacing:-.045em;margin-top:2vh">${clock}</h2>
        <p class="t-body" style="margin-top:2.6vh;color:var(--text-secondary);max-width:34ch">采集场景为 ${scenarioLabel}，推进 ${metrics.sim_steps} 个模拟小时后暂停。页面截图和指标 JSON 都来自同一个冻结状态。</p>
        <div style="margin-top:4vh;border-top:1px solid var(--border-subtle);padding-top:2vh;display:grid;grid-template-columns:50% 50%;gap:1.4vw">
          <div><div class="t-meta">SCENARIO</div><div style="font-family:var(--sans),var(--sans-zh);font-weight:300;font-size:max(22px,2.1vw);margin-top:.8vh">${scenarioLabel}</div></div>
          <div><div class="t-meta">TICKS</div><div style="font-family:var(--sans);font-weight:250;font-size:max(22px,2.1vw);margin-top:.8vh">${metrics.sim_steps}h</div></div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;border-top:1px solid var(--border-subtle)">
        ${[
          ['00','D181 初始化','180 天历史数据训练后的经营基线','HISTORY BASELINE'],
          ['01','订单进入','实时订单流扣减库存，近期订单留存 '+orders+' 条','ORDER STREAM'],
          ['06','补货决策','按 6 个模拟小时刷新一次，当前生成 '+inboundCount+' 批在途补货','REPLENISHMENT'],
          ['24','滚动预测','每日刷新预测摘要，当前跟踪 '+rollingCount+' 个高关注 SKU×仓','FORECAST'],
          ['30','冻结采集','库存 '+fmt.format(m.total_inventory)+' 件，订单满足率 '+pct(m.fill_rate),'CAPTURED STATE']
        ].map((row,i)=>`<div style="display:grid;grid-template-columns:7.2vw 1fr 13vw;gap:2vw;align-items:center;padding:2.25vh 0;border-bottom:1px solid var(--border-subtle)"><div style="font-family:var(--sans);font-weight:200;font-size:min(4.2vw,7vh);line-height:1;color:${i===4?'var(--accent)':'var(--ink)'}">${row[0]}</div><div><div style="font-family:var(--sans),var(--sans-zh);font-weight:400;font-size:max(20px,1.9vw);line-height:1.15;letter-spacing:-.015em;color:${i===4?'var(--accent)':'var(--text-primary)'}">${row[1]}</div><div class="t-body-sm" style="margin-top:.8vh;color:var(--text-secondary)">${row[2]}</div></div><div class="t-meta" style="color:${i===4?'var(--accent)':'var(--text-helper)'};text-align:right">${row[3]}</div></div>`).join('')}
      </div>
    </div>
  </div>
</section>

<section class="slide" data-layout="S08" data-animate="duo-mirror">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">06 / 10 · STRATEGY SHIFT</div><div class="r">FROM STATIC TO OPERATING</div></div>
    <div class="duo-compare">
      <div class="col">
        <div class="col-tag"><span class="num">A</span>STATIC CONTROL</div>
        <div class="col-ttl">表格驱动</div>
        <p class="col-desc">上传数据、生成方案、查看结果。适合说明算法，但不能体现订单流冲击后的经营状态变化。</p>
        <ul class="col-list">
          <li>任务结果更像一次性报表。</li>
          <li>缺货风险来自静态库存与预测。</li>
          <li>用户难以看到决策节奏与反馈。</li>
        </ul>
      </div>
      <div class="vrule"></div>
      <div class="col accent">
        <div class="col-tag"><span class="num">B</span>REALTIME SANDBOX</div>
        <div class="col-ttl">经营驱动</div>
        <p class="col-desc">当前 Web 端以实时经营沙盘为入口：订单、库存、预测、补货、分仓和仿真在同一个运行状态里联动。</p>
        <ul class="col-list">
          <li>小时级订单流持续改变库存。</li>
          <li>每日刷新预测，6 小时刷新补货决策。</li>
          <li>策略结果用仿真对比说明收益与代价。</li>
        </ul>
      </div>
    </div>
  </div>
</section>

<section class="slide" data-layout="S22" data-animate="image-hero">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">07 / 10 · WEB EVIDENCE</div><div class="r">POOL COCKPIT</div></div>
    <div style="position:relative;margin-top:3vh">
      ${img('images/web/01-web-pool.png','s22-hero-21x9','frame-img r-21x9 pos-top')}
      <div style="position:absolute;left:0;top:0;background:var(--paper);padding:2vh 2vw;max-width:34vw;border-right:1px solid var(--border-subtle);border-bottom:1px solid var(--border-subtle)">
        <div class="t-meta">GLOBAL INVENTORY POOL</div>
        <h2 style="font-family:var(--sans),var(--sans-zh);font-weight:300;font-size:max(22px,2.4vw);line-height:1.1;margin-top:1vh">首页直接回答：现在最该处理哪个仓、哪个 SKU？</h2>
      </div>
    </div>
    <div class="kpi-row-4" style="margin-top:3vh">
      <div class="kpi-cell"><div class="lbl">INVENTORY</div><div class="nb">${fmt.format(m.total_inventory)}<span class="unit">件</span></div><div class="note">全局库存池当前水位</div></div>
      <div class="kpi-cell"><div class="lbl">DEMAND 30D</div><div class="nb">${fmt.format(m.demand_30d)}<span class="unit">件</span></div><div class="note">未来 30 天预测需求</div></div>
      <div class="kpi-cell"><div class="lbl">REPLENISH</div><div class="nb">${fmt.format(m.today_replenishment_units)}<span class="unit">件</span></div><div class="note">今日建议补货量</div></div>
      <div class="kpi-cell"><div class="lbl">RISK SKU</div><div class="nb">${m.risk_sku_count}<span class="unit">个</span></div><div class="note">缺货风险 SKU</div></div>
    </div>
  </div>
</section>

<section class="slide" data-layout="S15" data-animate="matrix-fill">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">08 / 10 · FOUR MODULES</div><div class="r">SCREENSHOT MATRIX</div></div>
    <div style="display:grid;grid-template-columns:1fr auto;gap:3vw;align-items:start;margin-top:3vh">
      <div>
        <div class="t-meta">MAIN WORKBENCH</div>
        <h2 style="font-family:var(--sans),var(--sans-zh);font-weight:200;font-size:min(3.6vw,5.8vh);line-height:1;letter-spacing:-.035em">预测、补货、分仓、仿真<br/>在一个运行状态里解释。</h2>
      </div>
      <div class="kpi-thin-sm accent">${recCount}<span class="unit"> rec</span><br/>${allocationCount}<span class="unit"> alloc</span></div>
    </div>
    <div data-anim="modules-grid" style="display:grid;grid-template-columns:repeat(2,1fr);grid-template-rows:repeat(2,auto);gap:1vw;margin-top:2vh">
      <div style="background:var(--grey-1);padding:1vh">${img('images/web/02-web-forecast.png','s15-grid-ui-forecast','frame-img h-18 pos-top')}<div class="t-meta" style="margin-top:.8vh">AI 销量预测 · ${fmt.format(model.training_samples)} training samples</div></div>
      <div style="background:var(--grey-1);padding:1vh">${img('images/web/03-web-replenishment.png','s15-grid-ui-replenishment','frame-img h-18 pos-top')}<div class="t-meta" style="margin-top:.8vh">智能补货 · ${fmt.format(m.today_replenishment_units)} units</div></div>
      <div style="background:var(--grey-1);padding:1vh">${img('images/web/04-web-allocation.png','s15-grid-ui-allocation','frame-img h-18 pos-top')}<div class="t-meta" style="margin-top:.8vh">库存池分仓 · ${allocationCount} allocations</div></div>
      <div style="background:var(--grey-1);padding:1vh">${img('images/web/05-web-simulation.png','s15-grid-ui-simulation','frame-img h-18 pos-top')}<div class="t-meta" style="margin-top:.8vh">仿真对比 · +${cmp.fill_rate_lift_pct}pct fill rate</div></div>
    </div>
  </div>
</section>

<section class="slide" data-layout="S20" data-animate="stacked-ledger">
  <div class="canvas-card">
    <div class="chrome-min"><div class="l">09 / 10 · SIMULATION RESULT</div><div class="r">90-DAY COMPARISON</div></div>
    <div style="display:grid;grid-template-columns:36% 64%;gap:4vw;margin-top:4vh;align-items:start">
      <div>
        <div class="t-meta">TRADITIONAL VS INTELLIGENT</div>
        <h2 style="font-family:var(--sans),var(--sans-zh);font-weight:200;font-size:min(5.8vw,9.2vh);line-height:.98;letter-spacing:-.04em;margin-top:2vh">把策略放进<br/>同一条需求流里跑。</h2>
        <p class="t-body" style="color:var(--text-secondary);margin-top:3vh;max-width:38ch">智能库存池显著提高服务水平和交付速度，但也带来更高平均库存占用。这不是遮掩代价，而是让经营者看到可权衡的收益曲线。</p>
      </div>
      <div data-anim="ledger" style="display:flex;flex-direction:column;border-top:1px solid var(--border-subtle)">
        ${[
          ['订单满足率', `${pct(baseline.fill_rate)} → ${pct(intelligent.fill_rate)}`, `+${cmp.fill_rate_lift_pct}pct`],
          ['缺货率', `${pct(baseline.stockout_rate)} → ${pct(intelligent.stockout_rate)}`, `-${cmp.stockout_rate_reduction_pct}pct`],
          ['平均配送', `${baseline.average_delivery_days}天 → ${intelligent.average_delivery_days}天`, `-${cmp.delivery_days_reduction}天`],
          ['总利润', `${money(baseline.total_profit)} → ${money(intelligent.total_profit)}`, `+${money(cmp.profit_lift)}`],
          ['平均库存', `${fmt.format(Math.round(baseline.average_inventory_units))} → ${fmt.format(Math.round(intelligent.average_inventory_units))}`, '服务水平换库存占用']
        ].map((row,i)=>`<div class="ledger-row" style="display:grid;grid-template-columns:8vw 1fr auto;gap:2vw;align-items:center;padding:2.4vh 0;border-bottom:1px solid var(--border-subtle)"><div class="ledger-num" style="font-family:var(--sans);font-weight:200;font-size:min(4.2vw,7vh);color:${i===0||i===3?'var(--accent)':'var(--ink)'}">0${i+1}</div><div class="ledger-label"><div class="t-meta">${row[0]}</div><div style="font-family:var(--sans),var(--sans-zh);font-weight:300;font-size:max(22px,2.2vw);letter-spacing:-.02em;margin-top:.8vh">${row[1]}</div></div><div class="ledger-icon" style="font-family:var(--mono);font-size:max(14px,1vw);letter-spacing:.12em;color:${i===0||i===3?'var(--accent)':'var(--text-helper)'}">${row[2]}</div></div>`).join('')}
      </div>
    </div>
  </div>
</section>

<section class="slide split" data-layout="SWISS-CLOSING-ASCII" data-animate="split-statement">
  <div class="canvas-card">
    <div class="split-half">
      <div class="half b-accent" style="padding:5.6vh 3.6vw 4.4vh;justify-content:space-between;position:relative;overflow:hidden">
        <canvas class="ascii-bg" aria-hidden="true"></canvas>
        <div class="chrome-min" style="position:relative;z-index:1"><div class="l">10 / 10</div><div class="r">CLOSING</div></div>
        <div data-anim="manifesto" style="display:flex;flex-direction:column;gap:2vh;position:relative;z-index:1">
          <div class="t-meta" style="color:rgba(255,255,255,.78);letter-spacing:.22em">TAKEAWAY</div>
          <h2 style="font-family:var(--sans),var(--sans-zh);font-size:min(6.8vw,11vh);line-height:.96;letter-spacing:-.035em;font-weight:200;color:#fff">让库存先变成<br/>决策，再变成<span style="font-style:italic;font-weight:300">动作</span>。</h2>
          <div style="font-family:var(--sans),var(--sans-zh);font-size:max(16px,1vw);line-height:1.6;color:rgba(255,255,255,.82);font-weight:400;max-width:36ch;margin-top:1.4vh">这个项目的主角不是样例文件，而是持续变化的经营状态。</div>
        </div>
        <div data-anim="signature" style="display:flex;justify-content:space-between;align-items:end;border-top:1px solid rgba(255,255,255,.22);padding-top:2vh;position:relative;z-index:1">
          <div class="t-meta" style="color:rgba(255,255,255,.62)">GLOBALPOOL AI</div>
          <div class="t-meta" style="color:rgba(255,255,255,.62)">END</div>
        </div>
      </div>
      <div class="half" style="padding:5.6vh 3.6vw 4.4vh;justify-content:space-between">
        <div class="chrome-min"><div class="l">PROJECT VALUE</div><div class="r">03 RULES</div></div>
        <div data-anim="rules" style="display:flex;flex-direction:column;gap:0">
          ${[
            ['实时状态是入口','库存、订单、在途、仓容和异常事件共同构成经营沙盘。'],
            ['AI 给出可解释动作','预测不是终点，补货量、运输方式、分仓结果才是执行语言。'],
            ['仿真说明收益边界','用满足率、缺货率、交付时效、利润和库存占用一起评价方案。']
          ].map((row,i)=>`<div style="display:grid;grid-template-columns:auto 1fr;gap:2vw;align-items:start;padding:2.8vh 0;border-top:1px solid var(--border-subtle);${i===2?'border-bottom:2px solid var(--accent)':''}"><div style="font-family:var(--sans);font-weight:200;font-size:min(4.4vw,7.8vh);line-height:.9;color:${i===2?'var(--accent)':'var(--text-primary)'}">0${i+1}</div><div><h3 style="font-family:var(--sans),var(--sans-zh);font-weight:400;font-size:max(18px,1.8vw);line-height:1.2;letter-spacing:-.015em;color:${i===2?'var(--accent)':'var(--text-primary)'};margin-bottom:1vh">${row[0]}</h3><p style="font-family:var(--sans),var(--sans-zh);font-size:max(16px,.94vw);line-height:1.6;color:var(--text-secondary);font-weight:400">${row[1]}</p></div></div>`).join('')}
        </div>
        <div data-anim="foot" class="t-meta" style="color:var(--text-helper);text-align:right">→ END OF PROJECT INTRO</div>
      </div>
    </div>
  </div>
</section>
`;

let html = fs.readFileSync(templatePath, 'utf8');
html = html.replace('<title>[必填] 替换为 PPT 标题 · Deck Title</title>', '<title>GlobalPool AI 实时经营沙盘 · 项目介绍</title>');
const start = html.indexOf('<div id="deck">');
const nav = html.indexOf('\n<div id="nav"></div>', start);
if (start < 0 || nav < 0) throw new Error('Cannot locate deck container in template');
html = html.slice(0, start) + `<div id="deck">\n${slides}\n</div>\n` + html.slice(nav);
fs.writeFileSync(outPath, html, 'utf8');
console.log(outPath);
