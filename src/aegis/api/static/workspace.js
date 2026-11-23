/* Tenant workspace: cookies + CSRF only. Every action goes through the production API. */
'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = value => new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2}).format(Number(value || 0));
const titles = {overview:'仓网总览',forecast:'销量预测',replenishment:'补货队列',allocation:'分仓方案',evaluation:'效果评估',sources:'数据源管理',reconciliation:'对账差异',mappings:'主数据映射',users:'成员与权限',forecastops:'预测与模型',bundles:'套组追溯',risks:'库存风险',collaboration:'运营协同',commerce:'经营信号与新品',experts:'新品专家评审'};
const labels = {queued:'待计算',solving:'计算中',pending_review:'待审核',released:'已发布',closed:'已关闭',discarded:'已丢弃',failed:'失败',cancelled:'已取消',proposed:'待审核',approved:'已批准',rejected:'已驳回',confirmed:'单据已创建',writeback_failed:'回写失败',complete:'完整',landing:'读取中',landed:'待规范化',quarantined:'待处理',active:'启用',paused:'暂停',disabled:'停用',auth_failed:'授权失效',succeeded:'成功',running:'运行中',partial:'部分完成',shadow:'影子',execution:'执行',matched:'对账一致',mismatch:'对账差异',held:'预占中',consumed:'已发运',dead:'待人工处理',pending:'待发送',sent_unconfirmed:'查单确认中'};
const state = {page:Object.hasOwn(titles,location.hash.slice(1))?location.hash.slice(1):'overview',me:null,providers:null,plan:null,plans:[],catalog:{locations:[],skus:[]},overview:null,ready:null,connections:[],policies:[],sources:null,forecast:null,evaluation:null,execution:null,evaluationHorizon:null,busy:false,dirty:false,render:0};
const can = p => state.me?.permissions.includes(p);
const badge = value => `<span class="badge ${['approved','complete','matched','closed','succeeded'].includes(value)?'good':['failed','dead','mismatch','quarantined','auth_failed'].includes(value)?'bad':'warn'}">${esc(labels[value] || value)}</span>`;
const metric = (label,value,hint='') => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(hint)}</small></div>`;
const empty = text => `<div class="empty">${esc(text)}</div>`;
const panel = (title,body,extra='') => `<section class="panel ${extra}"><h3>${esc(title)}</h3>${body}</section>`;
const options = (rows,value,label,selected='') => rows.map(r=>`<option value="${esc(value(r))}" ${String(value(r))===String(selected)?'selected':''}>${esc(label(r))}</option>`).join('');
const locationName = id => {const r=state.catalog.locations.find(x=>x.id===id);return r?(r.name||r.code):id;};
const skuName = code => state.catalog.skus.find(x=>x.code===code)?.name || code;
const planLines = () => state.plan?.lines || [];
function message(text,error=false){$('message').textContent=text;$('message').classList.toggle('hidden',!text);$('message').classList.toggle('error',error);}
async function api(path,options={}){
  const headers=new Headers(options.headers || {});
  if(options.body && !(options.body instanceof FormData)) headers.set('content-type','application/json');
  if(options.method && !['GET','HEAD'].includes(options.method)){
    const name=state.providers?.csrf_cookie_name || 'aegis_csrf';
    const cookie=document.cookie.split('; ').find(x=>x.startsWith(name+'='));
    headers.set('x-csrf-token',cookie?decodeURIComponent(cookie.slice(name.length+1)):'');
  }
  const response=await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  const data=await response.json().catch(()=>({}));
  if(response.status===401){state.me=null;state.plan=null;state.connections=[];$('workspace').classList.add('hidden');location.replace('/login');throw Error('会话已过期，请重新登录');}
  if(!response.ok)throw Error(data.error?.message || (data.error?.issues||data.detail||[]).map?.(issue=>`${(issue.loc||[]).filter(k=>k!=='body').join('.')}：${issue.msg}`).join('；') || (response.status===403?'当前账号没有此操作权限':`请求失败 (${response.status})`));
  return data;
}
const post = (path,body={}) => api(path,{method:'POST',body:JSON.stringify(body)});
function enforcePermissions(){
  document.querySelectorAll('[data-permission]').forEach(node=>{node.hidden=!can(node.dataset.permission);});
  document.querySelector('.sidebar [data-page="sources"]').hidden=!can('integration:connection:view');
}
async function perform(task,success){
  if(state.busy)return;
  state.busy=true;$('workspace').setAttribute('aria-busy','true');document.querySelectorAll('button[type="submit"]').forEach(b=>b.disabled=true);
  try{const result=await task();document.querySelectorAll('dialog[open]').forEach(d=>d.close());await load();if(success)message(typeof success==='function'?success(result):success);}
  catch(error){message(error.message,true);if(document.querySelector('dialog[open]')){let p=document.querySelector('dialog[open] .field-error');if(!p){p=document.createElement('p');p.className='field-error';p.setAttribute('role','alert');document.querySelector('dialog[open] form').append(p);}p.textContent=error.message;}}
  finally{state.busy=false;$('workspace').setAttribute('aria-busy','false');document.querySelectorAll('button[type="submit"]').forEach(b=>b.disabled=false);}
}
function action(title,fields,submit){
  $('action-form').querySelector('[type=submit]').hidden=false;$('action-title').textContent=title;$('action-fields').innerHTML=fields;$('action-form').querySelector('.field-error')?.remove();
  $('action-form').onsubmit=e=>{e.preventDefault();const values=Object.fromEntries(new FormData(e.currentTarget));perform(()=>submit(values),result=>typeof result==='string'?result:'操作已记录。');};
  $('action-dialog').showModal();
}
const note = (label='操作说明') => `<label>${esc(label)}<textarea name="note" maxlength="2000" required></textarea></label>`;
function table(headers,rows,cls=''){return `<div class="table-wrap ${cls}" tabindex="0" role="region" aria-label="${esc(headers.join('、'))}"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows || `<tr><td colspan="${headers.length}">暂无记录</td></tr>`}</tbody></table></div>`;}
function shell(){
  $('workspace').dataset.planStatus=state.plan?.status||'none';
  $('tenant-name').textContent=state.overview?.tenant.name || '当前工作空间';$('member-email').textContent=state.me.email;
  $('environment').textContent=state.providers.platform_data_mode==='mock'?'Mock 工作空间':'真实接口模式';
  $('context-label').textContent=`${state.overview.sku_count} 个商品 · ${state.overview.warehouse_count} 个目的仓 · ${state.overview.tenant.slug}`;
  const mixed=state.overview.stock.some(x=>x.is_mock)&&state.overview.stock.some(x=>!x.is_mock);
  $('source-badge').textContent=mixed?'存在混合来源 · 发布受门禁约束':state.overview.stock.some(x=>x.is_mock)?'Mock 合成来源':'权威来源数据';
  $('review-count').textContent=number(planLines().filter(x=>x.status==='proposed'&&x.final_units>0).length);
  $('readiness-summary').textContent=state.ready?.data_ready?'当前数据检查通过':'数据尚未就绪';
  $('data-blockers').replaceChildren(...(state.ready?.data_blockers || []).slice(0,5).map(issue=>{const li=document.createElement('li');li.textContent=typeof issue==='string'?issue:issue.message||issue.code;return li;}));
  $('execution-policy').textContent=state.providers.execution_enabled?`执行已启用；外部回写${state.providers.writeback_enabled?'已启用':'关闭'}。`:'执行开关关闭，可生成影子计划。';
  $('plan-select').innerHTML=state.plans.length?options(state.plans,r=>r.id,r=>`${labels[r.mode]||r.mode} · ${labels[r.status]||r.status} · ${r.created_at.slice(0,16).replace('T',' ')}`,state.plan?.id):'<option value="">暂无计划</option>';
  $('plan-status').textContent=state.plan?`${labels[state.plan.status]||state.plan.status} · ${state.plan.horizon_days} 天`:'先完成数据准备，再生成计划';
  $('release-plan').disabled=!state.plan || state.plan.mode!=='execution' || state.plan.status!=='pending_review' || !state.providers.execution_enabled;
  $('close-plan').disabled=state.plan?.status!=='released';
  $('discard-plan').disabled=!['queued','solving','pending_review'].includes(state.plan?.status);
  $('default-policy').classList.toggle('hidden',state.policies.some(x=>x.is_active));
  $('new-plan').disabled=!state.policies.some(x=>x.is_active)||state.plans.some(x=>['queued','solving','pending_review','released'].includes(x.status));
  enforcePermissions();
}
async function load(){
  state.dirty=false;
  state.me=await api('/api/v1/auth/me');
  const values=await Promise.all([api('/api/v1/workspace/overview'),api('/api/v1/workspace/catalog'),api('/api/v1/plans?limit=100'),api('/api/v1/policies'),api('/api/v1/data/pilot-readiness'),can('integration:connection:view')?api('/api/v1/connections?limit=500'):Promise.resolve([])]);
  [state.overview,state.catalog,state.plans,state.policies,state.ready,state.connections]=values;
  const chosen=state.plan?.id || new URL(location.href).searchParams.get('plan') || state.plans[0]?.id;
  state.plan=chosen?await api('/api/v1/plans/'+encodeURIComponent(chosen)):null;
  state.sources=null;state.execution=null;state.evaluation=null;state.forecast=null;
  shell();$('loading').classList.add('hidden');$('workspace').classList.remove('hidden');await showPage(state.page,false);
}
async function showPage(page,focus=true){
  if(!Object.hasOwn(titles,page))return;
  state.page=page;const management=['sources','reconciliation','mappings','users',...opsPages].includes(page);$('workspace').classList.toggle('management-mode',management);$('management-nav').classList.toggle('hidden',!management);const version=++state.render;
  $('page-title').textContent=titles[page];$('section-title').textContent=titles[page];
  document.querySelectorAll('[data-page]').forEach(b=>(b.dataset.page===page||(management&&!opsPages.includes(page)&&b.closest('.sidebar')&&b.dataset.page==='sources')||(opsPages.includes(page)&&b.closest('.sidebar')&&b.dataset.page==='forecastops'))?b.setAttribute('aria-current','page'):b.removeAttribute('aria-current'));
  const url=new URL(location.href);url.hash=page;if(state.plan)url.searchParams.set('plan',state.plan.id);else url.searchParams.delete('plan');history.replaceState(null,'',url);
  $('page-content').innerHTML='<p class="muted">正在读取…</p>';
  try{
    let html='';
    if(page==='overview')html=overviewPage();
    if(page==='replenishment')html=replenishmentPage();
    if(page==='forecast')html=forecastPage();
    if(page==='allocation'){state.execution=state.plan?await api(`/api/v1/workspace/plans/${state.plan.id}/execution`):null;html=allocationPage();}
    if(page==='evaluation'){state.evaluation=state.plan?.input_digest?await api(`/api/v1/workspace/plans/${state.plan.id}/evaluation?horizon=${state.evaluationHorizon||state.plan.horizon_days}`):null;html=evaluationPage();}
    if(page==='sources'){await loadSources();html=sourcesPage();}
    if(['reconciliation','mappings','users'].includes(page))html=await adminPage(page);
    if(opsPages.includes(page))html=await decisionPage(page);
    if(version!==state.render)return;
    $('page-content').innerHTML=html;enforcePermissions();
    if(page==='forecast'&&planLines().length)await loadForecast();
    if(page==='sources')bindSourceForms();
    if(['reconciliation','mappings','users'].includes(page))bindAdmin(page);
    if(focus){$('section-title').focus({preventScroll:true});window.scrollTo(0,0);}
  }catch(error){if(version===state.render)$('page-content').innerHTML=empty(error.message);}
}
function overviewPage(){
  const stock=state.overview.stock.filter(x=>x.kind!=='pool');
  const risk=[...planLines()].sort((a,b)=>b.urgency-a.urgency).slice(0,5);
  const totals=state.overview.stock.reduce((a,s)=>{a.on_hand+=s.on_hand;a.in_transit+=s.in_transit;return a;},{on_hand:0,in_transit:0});
  const stories=stock.map(x=>`<button type="button" class="warehouse-story" data-location="${esc(x.code)}"><span><b>${esc(x.code.split('-').pop())}</b></span>${esc(x.name)}<small>${x.is_mock?'Mock 来源':'权威来源'}</small></button>`).join('');
  return `<div class="stories">${stories||'<p class="muted">尚未批准库存来源，请先前往数据源管理。</p>'}</div><div class="metrics">${metric('权威快照在库',number(totals.on_hand),state.overview.inventory_complete?'件 · 已配置来源的完整批次':'覆盖未完成，不代表完整仓网')}${metric('权威快照在途',number(totals.in_transit),'件 · 与PO需关联防重')}${metric('建议补货',number(planLines().reduce((n,x)=>n+x.recommended_units,0)),'件 · 当前冻结计划')}${metric('待审核',number(planLines().filter(x=>x.status==='proposed'&&x.final_units>0).length),'条 · 当前计划')}</div>${panel('需要关注的库存',table(['商品 / 目的仓','库存位置','建议补货','紧急度'],risk.map(x=>`<tr><td>${esc(skuName(x.sku_code))}<small>${esc(x.location_code)}</small></td><td class="num">${number(x.inventory_position)}</td><td class="num">${number(x.recommended_units)}</td><td>${number(x.urgency*100)}%</td></tr>`).join('')),'feed')}${panel('仓库库存概况',table(['仓库','在库','在途','完整批次'],stock.map(x=>`<tr><td>${esc(x.name)}<small>${esc(x.code)}</small></td><td class="num">${number(x.on_hand)}</td><td class="num">${number(x.in_transit)}</td><td>${badge('complete')}<small>${esc(x.batch_id.slice(0,8))}</small></td></tr>`).join('')))}<p class="help">库存为最新权威批次；建议与风险来自所选计划的冻结输入。两者时间口径可能不同。</p>`;
}
function forecastPage(){
  if(!planLines().length)return empty('暂无已计算的计划。先完成数据准备并生成计划。');
  return panel('销量预测',`<div class="filters"><label>商品 × 仓库<select id="forecast-line">${options(planLines(),r=>r.id,r=>`${skuName(r.sku_code)} / ${r.location_code}`)}</select></label><label>预测周期<select id="forecast-horizon">${(planLines()[0]?.detail?.forecast?.horizons?Object.keys(planLines()[0].detail.forecast.horizons).map(Number):[7,14,30]).sort((a,b)=>a-b).map(h=>`<option value="${h}" ${h===30?'selected':''}>${h}天</option>`).join('')}</select></label></div><div id="forecast-result"></div>`)+panel('计划明细',table(['商品','目的仓','日均需求','建议','原因'],planLines().map(x=>`<tr><td>${esc(skuName(x.sku_code))}</td><td>${esc(x.location_code)}</td><td class="num">${number(x.daily_rate)}</td><td class="num">${number(x.recommended_units)}</td><td>${esc(x.reason)}</td></tr>`).join('')));
}
async function loadForecast(){
  const id=$('forecast-line')?.value;if(!id)return;const horizon=$('forecast-horizon').value;const plan=state.plan.id;
  $('forecast-result').textContent='正在读取冻结需求…';
  try{const data=await api(`/api/v1/workspace/plans/${plan}/forecast?line_id=${id}&horizon=${horizon}`);if(state.page!=='forecast'||$('forecast-line')?.value!==id||$('forecast-horizon')?.value!==horizon||state.plan.id!==plan)return;
    const f=data.forecast;const points=data.history.units;const max=Math.max(1,...points);const path=points.map((v,i)=>`${i?'L':'M'}${(20+i/Math.max(1,points.length-1)*620).toFixed(1)},${(155-v/max*130).toFixed(1)}`).join(' ');
    $('forecast-result').innerHTML=`<div class="metrics">${metric('P50需求',number(f.p50_units),`${horizon}天 · 件`)}${metric('P80需求',number(f.p80_units),data.quantile_method||'件 · 正态近似')}${metric('P95需求',number(f.p95_units),data.quantile_method||'件 · 正态近似')}${metric('库存位置',number(data.inventory_position),'件 · 冻结快照')}</div><h3>历史需求 · 最近${points.length}天</h3><svg class="chart" viewBox="0 0 660 180" role="img" aria-label="冻结历史需求趋势"><line x1="20" y1="155" x2="640" y2="155"/><path d="${path}"/><text x="20" y="175">较早</text><text x="600" y="175">截止日</text></svg><p class="help">缺失${f.missing_days}天、缺货删失${f.censored_days}天。图中原始零值应结合观察标记解释；预测会按因果规则处理缺失/删失。分位数为近似值，不等于已校准的履约承诺。</p><details><summary>模型与输入证据</summary><p class="help">${esc(f.model_name)} · ${esc(f.model_version||'seasonal-tsb-v2')} · ${esc(f.selection?.reason||'')} · 截止 ${esc(data.cutoff_at)} · 输入摘要 ${esc(data.input_digest)}</p></details>`;
  }catch(error){if($('forecast-result'))$('forecast-result').textContent=error.message;}
}
function replenishmentPage(){
  if(!state.plan)return empty('尚未生成补货计划。');
  const editable=state.plan.status==='pending_review';
  return panel('补货审批',table(['商品 / 目的仓','建议','分配','最终','状态','操作'],planLines().map(x=>`<tr><td>${esc(skuName(x.sku_code))}<small>${esc(x.location_code)} · ${esc(x.reason)}</small></td><td data-label="建议" class="num">${number(x.recommended_units)}</td><td data-label="分配" class="num">${number(x.allocated_units)}</td><td data-label="最终" class="num">${number(x.final_units)}</td><td>${badge(x.status)}</td><td>${editable?`<button type="button" data-decision="${esc(x.id)}" data-approve="true" data-permission="plan:approve">批准</button><button type="button" class="secondary" data-decision="${esc(x.id)}" data-approve="false" data-permission="plan:approve">驳回</button><button type="button" class="text-button" data-adjust="${esc(x.id)}" data-permission="plan:adjust">调整数量</button>`:'—'}</td></tr>`).join(''),'approval-table'))+`<p class="help">当前为${esc(labels[state.plan.mode])}计划。影子计划可以审核和复核，不会创建预占或外部单据。调整数量后以服务端重新校验结果为准。</p>`;
}
function allocationPage(){
  if(!state.plan)return empty('尚未生成分仓方案。');
  const rows=state.execution?.reservations||[];
  return panel('分仓方案',table(['商品','目的仓','分配数量','运输方式','提前期'],planLines().map(x=>`<tr><td>${esc(skuName(x.sku_code))}</td><td>${esc(x.location_code)}</td><td class="num">${number(x.final_units)}</td><td>${esc(x.transport_mode||'—')}</td><td>${number(x.lead_time_days)}天</td></tr>`).join('')))+panel('库存预占与实物执行',rows.length?rows.map(({reservation:r,sku,location,latest:e,settlement,candidate_batches})=>`<article class="execution-item"><h4>${esc(skuName(sku))} / ${esc(location)}</h4>${badge(r.status)}<p>预占 ${number(r.units)} · 累计发运 ${number(r.consumed_units)} · 累计取消 ${number(r.released_units)} · 库存已结算 ${number(r.settled_units)}</p><p>${e?`累计收货 ${number(e.received)} · 源版本 ${e.source_version} · ${e.is_final?'最终执行':'执行中'} · ${esc(e.external_ref)}`:'尚无实物执行回读。单据创建成功不代表发运或收货。'}</p><p>${settlement?'当前执行观察已完成库存结算':'当前观察尚未完成库存结算'}</p><div class="actions"><button type="button" data-execution="${esc(r.id)}" data-permission="plan:release" ${e?.is_final?'disabled':''}>记录累计执行</button><button type="button" class="secondary" data-settle="${esc(r.id)}" data-permission="plan:release" ${!e||settlement?'disabled':''}>对账并结算库存</button></div></article>`).join(''):empty('发布执行计划后才会创建库存预占。'))+panel('外部单据创建状态',table(['单据类型','状态','外部编号'],(state.execution?.commands||[]).map(x=>`<tr><td>${esc(x.command_type)}</td><td>${badge(x.status)}</td><td>${esc(x.external_ref||'等待回读')}</td></tr>`).join('')));
}
function evaluationPage(){
  const e=state.evaluation;if(!e)return empty('计划尚未冻结输入，无法评估。');
  if(e.method==='independent-point-in-time')return panel('独立实绩评估',`<p class="help">此计划使用不可变独立预测，展示其最新冻结实绩评估。未成熟、缺失与删失窗口不计算收益。回到预测与模型可以查看标签修订和比较证据。</p>${e.report?table(['模型 / 周期','样本','MAE','WAPE','相对均值改善'],Object.entries(e.report.metrics).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${v.count}</td><td>${number(v.mae)}</td><td>${v.wape===null?'—':number(v.wape*100)+'%'}</td><td>${v.skill_vs_mean28===null?'—':number(v.skill_vs_mean28*100)+'%'}</td></tr>`).join('')):empty('实绩尚未成熟，等待评分。')}<button data-page="forecastops">查看预测与模型</button>`);
  const m=e.metrics.overall,b=e.metrics.baseline;const pct=v=>v==null?'不适用':number(v*100)+'%';
  const names={adaptive:'训练内选模 v3',mean_28:'28天均值 · 基线',mean_7:'7天均值',mean_56:'56天均值',seasonal_naive_7:'上周同日',weekday_mean_8:'近8周同日均值',ses_02:'指数平滑',damped_trend_28:'阻尼趋势',tsb:'TSB',seasonal_blend:'固定季节混合',legacy_v2:'原固定分流 v2',market_ridge:'市场信号 · 固定回归',market_adaptive:'市场信号 · 训练内门禁 v4'};
  const modelRows=Object.entries(e.models||{}).map(([key,v])=>{const p=v.vs_baseline;return `<tr><td>${esc(names[key]||key)}${key===e.primary_model?'<small>所选计划的模型版本</small>':''}</td><td>${pct(v.overall.wape)}</td><td>${number(v.overall.mae)}</td><td>${p.skill==null?(p.paired_folds?'基线误差为0':'无有效折'):pct(p.skill)}</td><td>${p.wins} / ${p.ties} / ${p.losses}</td><td>${pct(v.overall.p80_coverage)} / ${pct(v.overall.p95_coverage)}</td></tr>`;}).join('');
  const marketLabels={market_training_prefix_validation_gain:'训练内改善通过',market_not_clearly_better:'改善证据不足',insufficient_market_coverage:'信号覆盖不足',insufficient_market_inner_folds:'历史验证窗口不足',missing_market_calendar:'缺少可核验日历',no_market_variation:'信号无有效变化'};
  const marketPanel=Object.keys(e.market_comparison||{}).length?panel('市场信号接入前后',table(['方案','相对v3误差改善','胜 / 平 / 负'],Object.entries(e.market_comparison).map(([key,p])=>`<tr><td>${esc(names[key])}</td><td>${p.skill==null?'不适用':pct(p.skill)}</td><td>${p.wins} / ${p.ties} / ${p.losses}</td></tr>`).join(''))+`<p class="help">与未使用市场信号的v3在同一批留出窗比较。${esc(Object.entries(e.market_selection_counts||{}).map(([k,v])=>`${marketLabels[k]||k} ${v}折`).join('；'))}。回填信号不会提前生效，信号不足时沿用v3。</p>`):'';
  const rows=Object.entries(e.metrics).filter(([key])=>!['overall','baseline'].includes(key)&&!key.startsWith('series:'));
  return `<div class="filters"><label>回测预测周期<select id="evaluation-horizon">${[...new Set([7,14,30,e.horizon])].sort((a,b)=>a-b).map(h=>`<option value="${h}" ${h===e.horizon?'selected':''}>${h}天</option>`).join('')}</select></label></div><div class="metrics">${metric('计划模型 WAPE',pct(m?.wape),'外层滚动留出集')}${metric('均值基线 WAPE',pct(b?.wape),'同一批有效折')}${metric('计划模型偏差',pct(m?.bias),'正值代表高估')}${metric('有效折数',number(m?.folds),'含零销量测试窗')}</div>${panel('模型比较 · 同折配对',table(['模型','WAPE','MAE / 件','相对均值误差改善','胜 / 平 / 负','P80 / P95覆盖率'],modelRows))}<p class="help">误差改善为正代表优于28天均值，为负代表退化；基线误差为0时不计算比例。模型选择只使用每个外层训练集内部的历史验证窗，外层结果仅作评估，不会替换已冻结计划。${esc(e.model_version)} · ${esc(e.evaluation_version)}</p>${marketPanel}${panel('训练内选模与样本覆盖',`<p class="help">v3外层预测选择计数：${esc(JSON.stringify(e.selection_counts||{}))}。排除窗口：${esc(JSON.stringify(e.skipped||{}))}。</p><p class="help">无可核验的共享日历时不计算ABC分组。P80/P95为正态近似，覆盖率是留出集诊断，不是履约承诺。</p>`)}${panel('冻结计划结果',table(['指标','数值'],Object.entries(e.summary||{}).map(([k,v])=>`<tr><td>${esc(({allocated_units:'计划分配件数',recommended_units:'建议件数',unmet_units:'未满足目标件数',total_cost:'规划目标成本',dispatch_cost:'发运成本',shortage_penalty:'缺口惩罚'})[k]||k)}</td><td class="num">${number(v)}</td></tr>`).join('')))}${panel('计划模型分组回测',table(['分组','WAPE','偏差','折数'],rows.map(([key,v])=>`<tr><td>${esc(key)}</td><td>${pct(v.wape)}</td><td>${pct(v.bias)}</td><td>${number(v.folds)}</td></tr>`).join('')))}<p class="help">所选计划冻结历史：${e.min_train}天起训、${e.horizon}天留出窗，每序列最多最近${e.max_folds_per_series}折。市场模型按每个内外层起点筛选当时已发布、已接收、已可用的修订；需求事实的历史修订尚未重建。规划成本不是实际经营利润。</p>`;
}

async function loadSources(){
  const [markets,templates,batches,authorities,quarantine,jobs,checks,revisions]=await Promise.all([api('/api/v1/integration/markets'),api('/api/v1/integration/templates'),api('/api/v1/data/batches?limit=100'),api('/api/v1/data/authorities'),api('/api/v1/quarantine?limit=100'),api('/api/v1/sync-runs?limit=100'),api('/api/v1/data/reconciliations?limit=100'),api('/api/v1/data/market-signal-revisions?limit=100')]);
  state.sources={markets,templates,batches,authorities,quarantine,jobs,checks,revisions};
}
function sourcesPage(){
  const s=state.sources;
  const revisionTable=rows=>table(['来源 / 商品 / 仓库','目标日 / 修订','发布 / 接收 / 可用（UTC）'],rows.map(r=>`<tr><td>${esc(r.source_system)}<small>${esc(r.sku)} / ${esc(r.location)} · ${esc(r.platform)}</small></td><td>${esc(r.signal_date)}<small>v${r.revision} · ${r.is_deleted?'已撤回':r.signal_kind==='planned'?'计划信号':'实际指标'}</small></td><td>${esc(r.published_at)}<small>${esc(r.received_at)}</small><small>${esc(r.available_at)}</small></td></tr>`).join(''),'market-revisions');
  const connections=state.connections.map(c=>`<tr><td>${esc(c.name)}<small>${esc(c.kind)} · ${c.is_mock?'Mock':'真实接口'}</small></td><td>${badge(c.status)}<small>${esc(c.last_error||'')}</small></td><td><button data-sync="${esc(c.id)}" data-permission="integration:sync:run">同步</button>${c.is_mock&&['mock_amazon','mock_tiktok'].includes(c.kind)?`<button data-authorize="${esc(c.id)}" data-permission="integration:connection:edit">Mock授权</button>`:''}<button class="secondary" data-connection-status="${esc(c.id)}" data-status="${c.status==='active'?'paused':'active'}" data-permission="integration:connection:edit">${c.status==='active'?'暂停':'恢复'}</button><button class="text-button" data-secrets="${esc(c.id)}" data-permission="integration:connection:edit">配置密钥</button></td></tr>`).join('');
  const connOptions=options(state.connections,x=>x.id,x=>`${x.name} (${x.is_mock?'Mock':'真实'})`);
  return panel('数据源',table(['连接','状态','操作'],connections)+`<details data-permission="integration:connection:edit"><summary>新建连接</summary><form id="connection-form"><div class="form-grid"><label>名称<input name="name" required maxlength="120"></label><label>类型<select name="kind" id="connection-kind"><option value="mock_amazon">Amazon Mock</option><option value="mock_tiktok">TikTok Shop Mock</option><option value="mock_market">市场信号 Mock</option><option value="mock_commerce">经营指标 Mock</option><option value="mock_public_market">公开信号 Mock</option><option value="public_market">免费公开市场信号</option><option value="file_csv">CSV / XLSX</option><option value="generic_rest">通用 REST</option></select></label><label>市场<select name="market" id="connection-market"></select></label><label>账户 / 店铺编号<input name="account" value="MOCK-ACCOUNT" maxlength="40"></label><label>目标仓代码<input name="location" required placeholder="例如 US-LAX"></label><label>起始日期<input name="initial" type="date" value="${new Date(Date.now()-90*86400000).toISOString().slice(0,10)}" required></label><label class="wide">连接配置（公开订阅、Mock 或 REST）<textarea name="config" placeholder='{"base_url":"https://…","streams":{…}}'></textarea></label></div><button type="submit">创建连接</button></form></details>`)+panel('导入文件',`<form id="upload-form" data-permission="integration:connection:edit"><div class="form-grid"><label>CSV连接<select name="connection" required>${options(state.connections.filter(x=>x.kind==='file_csv'),x=>x.id,x=>x.name)}</select></label><label>数据流<select name="stream">${Object.keys(s.templates).map(k=>`<option>${esc(k)}</option>`).join('')}</select></label><label class="wide">CSV / XLSX文件<input type="file" name="file" accept=".csv,.xlsx" required></label></div><button type="submit">上传文件并请求同步</button></form>${state.uploadReceipt?`<p class="help">最近上传文件版本（独立清单使用）</p><pre>${esc(JSON.stringify(state.uploadReceipt,null,2))}</pre>`:""}<details><summary>字段契约</summary><pre>${esc(JSON.stringify(s.templates,null,2))}</pre></details>`)+panel('市场信号修订 · 最近100条',`<p class="help">发布时间来自供应商，接收及可用时间由服务器记录。历史回填不会提前生效；仅配置了来源的v4计划使用信号，既有v2/v3不改变。</p>`+revisionTable(s.revisions.slice(0,10))+(s.revisions.length>10?`<details><summary>展开其余${s.revisions.length-10}条修订</summary>${revisionTable(s.revisions.slice(10))}</details>`:''))+panel('权威来源规则',table(['领域 / 渠道','地点','来源','操作'],s.authorities.map(r=>`<tr><td>${esc(r.domain)}<small>${esc(r.channel)}</small></td><td>${esc(locationName(r.location_id))}</td><td>${esc(state.connections.find(c=>c.id===r.connection_id)?.name||r.connection_id)}<small>${esc(r.stream)}</small></td><td><button data-remove-authority="${esc(r.id)}" class="text-button" data-permission="integration:connection:edit">移除</button></td></tr>`).join(''))+`<details data-permission="integration:connection:edit"><summary>批准来源</summary><form id="authority-form"><div class="form-grid"><label>领域<select name="domain" id="authority-domain"><option value="inventory">库存</option><option value="demand">需求</option><option value="inbound">未收采购</option></select></label><label>地点<select name="location_id">${options(state.catalog.locations,r=>r.id,r=>r.name||r.code)}</select></label><label>连接<select name="connection_id">${connOptions}</select></label><label>数据流<select name="stream" id="authority-stream"></select></label><label>渠道（总量用 *）<input name="channel" value="*" required></label><label>订单覆盖起日（需求订单流）<input name="coverage_start" type="date"></label><label>需求数量口径<select name="quantity_basis"><option value="">按数据流默认</option><option value="base_units">基础件 · 不再展开</option><option value="sellable_units">销售件 · 按 BOM 展开</option></select></label><label>在途与PO口径<select name="inbound_mode"><option value="linked">有单据关联</option><option value="disjoint">已核实完全不重叠</option></select></label><label class="wide">批准依据<textarea name="note" required maxlength="2000"></textarea></label></div><button type="submit">批准来源规则</button></form></details>`)+panel('最近100个批次与源对账',table(['数据流 / 来源','完整性','最新对账','操作'],s.batches.map(b=>{const check=s.checks.find(c=>c.batch_id===b.id);const conn=state.connections.find(c=>c.id===b.connection_id);return `<tr><td>${esc(b.stream)}<small>${esc(conn?.name||b.connection_id)} · ${number(b.record_count)}条</small><small>${esc(b.id)}</small></td><td>${badge(b.status)}<small>${esc(b.error||'')}</small></td><td>${check?badge(check.status):'待对账'}</td><td><button class="text-button" data-batch-details="${esc(b.id)}">覆盖范围</button>${b.status==='complete'?`${['mock_amazon','mock_tiktok'].includes(conn?.kind)?`<button data-mock-reconcile="${esc(b.id)}" data-permission="integration:connection:edit">Mock源对账</button>`:''}<button class="secondary" data-manifest="${esc(b.id)}" data-permission="integration:connection:edit">上传独立清单</button>`:''}</td></tr>`;}).join('')))+panel('同步任务',table(['任务','状态','读取 / 隔离'],s.jobs.map(j=>`<tr><td>${esc(j.id.slice(0,8))}<small>${esc(j.mode)}</small></td><td>${badge(j.status)}<small>${esc(j.error||'')}</small></td><td>${number(j.records_read)} / ${number(j.records_quarantined)}</td></tr>`).join('')))+panel('隔离记录',table(['数据流','原因','操作'],s.quarantine.map(q=>`<tr><td>${esc(q.stream)}</td><td>${esc(q.reason_code)}<small>${esc(q.detail)}</small></td><td><button data-replay="${esc(q.id)}" data-permission="integration:mapping:resolve">修正后重放</button></td></tr>`).join('')));
}
function bindSourceForms(){
  function markets(){const kind=$('connection-kind').value;const rows=kind==='mock_tiktok'?state.sources.markets.tiktok_shop:state.sources.markets.amazon;$('connection-market').innerHTML=options(rows,r=>r.country,r=>r.country);$('connection-market').disabled=!['mock_amazon','mock_tiktok'].includes(kind);$('connection-form').elements.location.required=['mock_amazon','mock_tiktok','mock_market','mock_commerce'].includes(kind);}
  $('connection-kind').onchange=markets;markets();
  $('connection-form').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.currentTarget));perform(async()=>{
    let config={};
    if(d.kind==='mock_amazon'){const market=state.sources.markets.amazon.find(x=>x.country===d.market);config={seller_id:d.account,region:market.region,marketplace_id:market.marketplace_id,location_code:d.location,initial_since:d.initial+'T00:00:00Z'};}
    if(d.kind==='mock_tiktok')config={shop_id:d.account,shop_cipher:'MOCK-CIPHER-'+d.account,market:d.market,location_code:d.location,initial_since:d.initial+'T00:00:00Z'};
    if(d.kind==='mock_market')config=d.config.trim()?JSON.parse(d.config):{sku:'S',location_code:d.location,platform:'amazon',start_date:d.initial,days:180};
    if(d.kind==='mock_commerce')config=d.config.trim()?JSON.parse(d.config):{sku:'S',location_code:d.location,channel:'amazon',timezone:'America/Los_Angeles',start_date:d.initial,days:180};
    if(['public_market','mock_public_market'].includes(d.kind))config=d.config.trim()?JSON.parse(d.config):{subscriptions:[{provider:'frankfurter',series_key:'USD/EUR'},{provider:'wikimedia',series_key:'en.wikipedia.org|USB-C'},{provider:'govuk_holidays',series_key:'england-and-wales'}],history_days:90};
    if(d.kind==='file_csv')config={directory:state.me.tenant_id};if(d.kind==='generic_rest')config=JSON.parse(d.config);
    await post('/api/v1/connections',{name:d.name,kind:d.kind,config});
  },'连接已创建，请按类型配置授权并同步。');};
  function streams(){const domain=$('authority-domain').value;const values=domain==='inventory'?['inventory_snapshots']:domain==='inbound'?['purchase_orders']:['daily_demand','order_lines'];$('authority-stream').innerHTML=values.map(v=>`<option>${v}</option>`).join('');}
  $('authority-domain').onchange=streams;streams();
  $('authority-form').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.currentTarget));d.coverage_start=d.coverage_start||null;d.quantity_basis=d.quantity_basis||null;perform(()=>post('/api/v1/data/authorities',d),'来源规则已保存。');};
  $('upload-form').onsubmit=e=>{e.preventDefault();const form=new FormData(e.currentTarget);const conn=form.get('connection'),stream=form.get('stream');const body=new FormData();body.append('file',form.get('file'));perform(async()=>{state.uploadReceipt=await api(`/api/v1/connections/${conn}/files/${stream}`,{method:'POST',body});await post(`/api/v1/connections/${conn}/sync`,{streams:[stream]});},'文件已上传，同步已排队。');};
}
function executionAction(id){
  const row=state.execution.reservations.find(x=>x.reservation.id===id),e=row.latest;
  action('记录累计执行',`<p class="help">${esc(row.sku)} / ${esc(row.location)} · 预占 ${number(row.reservation.units)}件。填写源系统累计量，不填写本次增量。</p><div class="form-grid"><label>源版本<input name="source_version" type="number" min="1" value="${(e?.source_version||0)+1}" required></label><label>单据编号<input name="external_ref" value="${esc(e?.external_ref||state.execution.commands.find(x=>x.plan_line_id===row.reservation.plan_line_id)?.external_ref||'')}" maxlength="200" required></label>${['shipped','received','cancelled'].map((key,i)=>`<label>${['累计发运','累计收货','累计取消'][i]}<input name="${key}" type="number" min="0" max="${row.reservation.units}" value="${e?.[key]||0}" required></label>`).join('')}<label>发生时间<input type="datetime-local" name="occurred_at" step="1" required></label></div><label class="check"><input type="checkbox" name="is_final">源系统确认全部收货或取消，标记最终执行</label>${note('源系统回读证据')}`,d=>post(`/api/v1/inventory/reservations/${id}/execution`,{...d,connection_id:row.execution_connection_id,source_version:Number(d.source_version),shipped:Number(d.shipped),received:Number(d.received),cancelled:Number(d.cancelled),is_final:d.is_final==='on',occurred_at:new Date(d.occurred_at).toISOString(),evidence:d.note,note:undefined}));
  const localNow=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,19);
  $('action-form').elements.occurred_at.value=localNow;
}
function settleAction(id){const row=state.execution.reservations.find(x=>x.reservation.id===id);action('结算库存',`<p class="help">先导入执行后的完整库存，并对账清单声明该单据的累计执行量。以下是当前权威候选批次，服务端还会验证SKU、时间与单据计数。</p><label>源仓批次<input name="source_batch_id" value="${esc(row.candidate_batches.source||'')}" required></label><label>目的仓批次<input name="destination_batch_id" value="${esc(row.candidate_batches.destination||'')}" ${row.latest.received?'required':''}></label>${note('库存已反映执行的依据')}`,d=>post(`/api/v1/inventory/reservations/${id}/settle`,{source_batch_id:d.source_batch_id,destination_batch_id:d.destination_batch_id||null,evidence:d.note}));}

document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b||b.disabled)return;
  if(b.hasAttribute('data-close'))return b.closest('dialog').close();
  if(b.dataset.page)return showPage(b.dataset.page);
  if(b.dataset.location){const line=planLines().find(x=>x.location_code===b.dataset.location);showPage('forecast').then(()=>{if(line&&$('forecast-line')){$('forecast-line').value=line.id;loadForecast();}});return;}
  if(b.classList.contains('logout'))return perform(()=>post('/api/v1/auth/logout').then(()=>location.replace('/login')));
  if(b.dataset.decision)return action(b.dataset.approve==='true'?'批准补货建议':'驳回补货建议',note('审核说明'),d=>post(`/api/v1/plans/${state.plan.id}/lines/${b.dataset.decision}/decision`,{approve:b.dataset.approve==='true',note:d.note}));
  if(b.dataset.adjust){const line=planLines().find(x=>x.id===b.dataset.adjust);return action('调整补货数量',`<label>最终数量<input type="number" min="0" name="units" value="${line.final_units}" required></label>${note('调整原因')}`,d=>api(`/api/v1/plans/${state.plan.id}/lines/${line.id}`,{method:'PATCH',body:JSON.stringify({units:Number(d.units),note:d.note,pin:true})}));}
  if(b.dataset.authorize)return perform(()=>post(`/api/v1/connections/${b.dataset.authorize}/mock-authorize`),'Mock授权已记录。');
  if(b.dataset.sync)return perform(()=>post(`/api/v1/connections/${b.dataset.sync}/sync`),'同步任务已排队，请等待Worker并刷新。');
  if(b.dataset.connectionStatus)return perform(()=>api(`/api/v1/connections/${b.dataset.connectionStatus}`,{method:'PATCH',body:JSON.stringify({status:b.dataset.status})}),'连接状态已更新。');
  if(b.dataset.secrets)return action('配置连接密钥',`<p class="help">密钥由服务端加密保存，不会回显。提交后将替换该连接的密钥对象。</p><label>密钥对象<textarea name="secrets" autocomplete="off" spellcheck="false" required></textarea></label>`,d=>api(`/api/v1/connections/${b.dataset.secrets}/secrets`,{method:'PUT',body:JSON.stringify({secrets:JSON.parse(d.secrets)})}));
  if(b.dataset.removeAuthority)return action('移除权威来源',note('移除原因'),d=>api(`/api/v1/data/authorities/${b.dataset.removeAuthority}`,{method:'DELETE',body:JSON.stringify(d)}));
  if(b.dataset.mockReconcile)return perform(async()=>{const body=await api(`/api/v1/data/batches/${b.dataset.mockReconcile}/mock-source-manifest`);const result=await post(`/api/v1/data/batches/${b.dataset.mockReconcile}/reconcile`,body);if(result.status!=='matched')throw Error('已记录对账差异，请核对来源。');},'独立Mock源对账通过。');
  if(b.dataset.manifest){action('上传独立源清单',`<p class="help">从源系统独立导出的JSON清单，需包含source_rows、scope和evidence；结算时还需execution_refs。最大10MB。</p><label>清单文件<input id="manifest-file" type="file" accept=".json" required></label>`,async()=>{const file=$('manifest-file').files[0];if(file.size>10000000)throw Error('清单超过10MB');const result=await post(`/api/v1/data/batches/${b.dataset.manifest}/reconcile`,JSON.parse(await file.text()));if(result.status!=='matched')throw Error('已保存对账差异，请核对缺失、多出和内容变化。');});return;}
  if(b.dataset.batchDetails){action('批次覆盖范围',`<pre>${esc(JSON.stringify(state.sources.batches.find(x=>x.id===b.dataset.batchDetails)?.scope,null,2))}</pre>`,()=>{});$('action-form').querySelector('[type=submit]').hidden=true;return;}
  if(b.dataset.replay)return action('重放隔离记录',`<p class="help">请先修正主数据或源记录。本操作不会自动接受质量异常。</p>${note('处理说明')}`,d=>post('/api/v1/quarantine/replay',{ids:[b.dataset.replay],accept_quality:false,note:d.note}));
  if(b.dataset.execution)return executionAction(b.dataset.execution);
  if(b.dataset.settle)return settleAction(b.dataset.settle);
});
document.addEventListener('input',event=>{if(event.target.closest('form'))state.dirty=true;});
document.addEventListener('change',event=>{if(event.target.id==='evaluation-horizon'){state.evaluationHorizon=Number(event.target.value);showPage('evaluation',false);}if(event.target.closest('form'))state.dirty=true;if(['forecast-line','forecast-horizon'].includes(event.target.id))loadForecast();});
$('plan-select').onchange=()=>perform(async()=>{state.evaluationHorizon=null;state.plan=await api('/api/v1/plans/'+$('plan-select').value);});
$('refresh').onclick=()=>perform(async()=>{},'数据已刷新。');
$('default-policy').onclick=()=>perform(()=>post('/api/v1/policies',{}),'默认策略已创建。');
$('new-plan').onclick=()=>{
  $('writeback-select').innerHTML='<option value="">人工协同，不创建外部单据</option>'+options(state.connections.filter(x=>Object.keys(x.config.writeback?.commands||{}).length),x=>x.id,x=>x.name);
  $('writeback-select').disabled=!state.providers.writeback_enabled;
  $('new-plan-form').elements.mode.querySelector('[value=execution]').disabled=!state.providers.execution_enabled;
  $('new-plan-dialog').showModal();
};
$('new-plan-form').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.currentTarget));perform(async()=>{state.evaluationHorizon=null;state.plan=await post('/api/v1/plans',{horizon_days:Number(d.horizon_days),mode:d.mode,writeback_connection_id:d.writeback_connection_id||null});},'计划已排队，等待计算Worker。');};
$('release-plan').onclick=()=>action('发布已批准建议',`<p class="help">发布将重新校验来源、数据完整性、独立对账与可用库存，并创建预占。${$('ack-stale').checked?'本次明确接受过期数据风险。':''}</p>${note('发布确认说明')}`,d=>post(`/api/v1/plans/${state.plan.id}/release`,{acknowledge_stale:$('ack-stale').checked,note:d.note}));
$('close-plan').onclick=()=>action('关闭已结算计划',note('关闭依据'),d=>post(`/api/v1/plans/${state.plan.id}/actions/close`,d));
$('discard-plan').onclick=()=>action('结束当前计划',note('结束原因'),d=>post(`/api/v1/plans/${state.plan.id}/actions/${state.plan.status==='pending_review'?'discard':'cancel'}`,d));
async function initialize(){try{state.providers=await api('/api/v1/auth/providers');state.me=await api('/api/v1/auth/me');if(can('forecast:expert:respond')&&!can('data:view')){location.replace('/expert');return;}await load();}catch(error){message(error.message,true);$('loading').classList.add('hidden');}}
initialize();
setInterval(async()=>{if(!state.me||state.busy||document.hidden)return;try{await api('/api/v1/auth/me');if(!state.dirty&&!document.querySelector('dialog[open]')&&(['queued','solving'].includes(state.plan?.status)||(state.page==='sources'&&state.sources?.jobs.some(x=>['queued','running'].includes(x.status))&&!document.activeElement.matches('input,select,textarea')&&!document.querySelector('dialog[open]'))))await load();}catch(error){message(error.message,true);}},5000);

document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.me&&!state.busy)api('/api/v1/auth/me').catch(error=>message(error.message,true));});
