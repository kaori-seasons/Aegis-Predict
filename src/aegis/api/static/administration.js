/* Governance views use the same tenant session, CSRF and mutation queue as the workspace. */
'use strict';
const adminViews={};
const roleNames={TENANT_ADMIN:'租户管理员',INTEGRATION_ADMIN:'数据接入管理员',PLANNER:'计划员',APPROVER:'审批员',VIEWER:'只读成员'};
const adminLabels={open:'待处理',in_progress:'处理中',resolved:'已解决',pending:'待映射',confirmed:'已确认',auto:'自动匹配'};
const adminBadge=v=>`<span class="badge ${['resolved','confirmed','auto'].includes(v)?'good':'warn'}">${esc(adminLabels[v]||v)}</span>`;
const filtersFor=page=>adminViews[page] ||= {q:'',status:'',entity:'',offset:0};
function adminFilters(page){const f=filtersFor(page);return `<form id="admin-filter" class="admin-filters"><label>搜索<input name="q" value="${esc(f.q)}" placeholder="${page==='users'?'邮箱或姓名':page==='mappings'?'外部标识或来源':'对账证据说明'}" maxlength="160"></label>${page!=='users'?`<label>状态<select name="status"><option value="">全部</option>${(page==='mappings'?['pending','confirmed','auto']:['mismatch','matched']).map(v=>`<option value="${v}" ${f.status===v?'selected':''}>${esc(adminLabels[v]||labels[v])}</option>`).join('')}</select></label>`:''}${page==='mappings'?`<label>类型<select name="entity"><option value="">全部</option><option value="sku" ${f.entity==='sku'?'selected':''}>商品</option><option value="location" ${f.entity==='location'?'selected':''}>仓库</option></select></label>`:''}<button type="submit">查询</button></form>`;}
function pagination(page){const f=filtersFor(page),data=f.data;return `<div class="pagination"><small>共 ${number(data.total)} 条 · 当前 ${data.items.length?f.offset+1:0}–${data.items.length?Math.min(f.offset+data.items.length,data.total):0}</small><div><button class="secondary" data-admin-prev ${f.offset===0?'disabled':''}>上一页</button><button class="secondary" data-admin-next ${f.offset+50>=data.total?'disabled':''}>下一页</button></div></div>`;}
async function adminPage(page){
  const permission=page==='users'?'user:manage':page==='mappings'?'integration:connection:view':'data:view';
  if(!can(permission))return empty('当前账号没有此管理页面的访问权限。');
  const f=filtersFor(page);const params=new URLSearchParams({limit:50,offset:f.offset,q:f.q});if(f.status)params.set('status',f.status);if(f.entity)params.set('entity',f.entity);
  const request=f.request=(f.request||0)+1;
  const data=await api((page==='users'?'/api/v1/users':'/api/v1/workspace/'+(page==='reconciliation'?'reconciliations':'mappings'))+'?'+params);
  if(request!==f.request)throw Error('筛选已更新');
  f.data=data;
  if(page==='users'){const roles=await api('/api/v1/roles');if(request!==f.request)throw Error('筛选已更新');f.roles=roles;return usersPage(f);}
  if(page==='mappings')return mappingsPage(f);
  return reconciliationPage(f);
}
function reconciliationPage(f){return panel('独立源对账',`<p class="help">每个批次显示最新检查。处置状态与对账结果分开记录；关闭差异必须关联最新通过的同源复核，处置说明不能跳过发布门禁。</p>${adminFilters('reconciliation')}${table(['批次 / 数据流','结果与差异','处置状态','操作'],f.data.items.map(r=>`<tr><td>${esc(r.stream)}<small>${esc(r.batch_id)}</small><small>${esc(state.connections.find(c=>c.id===r.connection_id)?.name||r.connection_id)}</small></td><td>${badge(r.status)}<small>缺失 ${r.details.missing_count} · 多出 ${r.details.extra_count} · 变化 ${r.details.changed_count}</small></td><td>${r.case?adminBadge(r.case.status):r.status==='mismatch'?adminBadge('open'):'—'}${r.case?`<small>版本 ${r.case.revision} · ${esc(r.assignee_label||'未分配')}</small>`:''}</td><td><button data-check="${esc(r.id)}">查看差异</button>${(r.status==='mismatch'||r.case)?`<button class="secondary" data-disposition="${esc(r.id)}" data-permission="integration:connection:edit">处置</button>`:''}</td></tr>`).join(''))}${pagination('reconciliation')}`);}
function mappingsPage(f){return panel('外部标识映射',`<p class="help">将来源系统的商品或仓库标识对应到本租户的有效主数据。确认后最多将该来源500条未映射隔离记录重新排队；Worker再次规范化，仍有异常会重新隔离。已确认映射的历史事实不能直接改指其他商品。</p>${adminFilters('mappings')}${table(['来源 / 外部标识','类型','标准代码','状态','操作'],f.data.items.map(r=>`<tr><td>${esc(r.external_id)}<small>${esc(r.source_system)}</small></td><td>${r.entity==='sku'?'商品':'仓库'}</td><td>${esc(r.target_code||'待选择')}</td><td>${adminBadge(r.status)}</td><td>${r.status==='pending'?`<button data-map="${esc(r.id)}" data-permission="integration:mapping:resolve">确认映射</button>`:'—'}</td></tr>`).join(''))}${pagination('mappings')}`)+panel('隔离处理',`<p class="help">映射后可前往数据源管理检查隔离队列。来源量大于500条时，分批重放；系统不会自动接受数量或质量异常。</p><button class="secondary" data-page="sources">查看隔离与同步</button>`);}
function usersPage(f){return panel('工作空间成员',`<div class="panel-head"><p class="help">角色按权限并集生效；修改成员会撤销其全部旧会话。最后一位有效租户管理员不能停用或降权。</p><button data-user-create>新增成员</button></div>${adminFilters('users')}${table(['成员','角色与数据范围','状态','操作'],f.data.items.map(r=>`<tr><td>${esc(r.display_name||r.email)}<small>${esc(r.email)}${r.id===state.me.user_id?' · 当前账号':''}</small><small>Google ${r.google_bound?'已绑定':'未绑定'}</small></td><td>${r.roles.map(x=>esc(roleNames[x]||x)).join('、')}<small>${r.location_scope===null?'全部地点':esc(r.location_scope.join('、')||'无地点权限')}</small></td><td>${r.is_active?badge('active'):badge('disabled')}</td><td><button data-user-edit="${esc(r.id)}">编辑权限</button><button class="secondary" data-user-revoke="${esc(r.id)}">撤销会话</button>${!r.google_bound?`<button class="text-button" data-user-bind="${esc(r.id)}">绑定Google</button>`:''}</td></tr>`).join(''))}${pagination('users')}`)+panel('角色权限',table(['角色','权限'],Object.entries(f.roles).map(([key,perms])=>`<tr><td>${esc(roleNames[key]||key)}</td><td class="permission-list">${perms.map(esc).join(' · ')}</td></tr>`).join('')));}
function roleFields(roles){return `<fieldset><legend>角色（可多选）</legend><div class="role-options">${Object.entries(roleNames).map(([key,label])=>`<label class="check"><input type="checkbox" name="roles" value="${key}" ${roles.includes(key)?'checked':''}>${esc(label)}</label>`).join('')}</div></fieldset>`;}
function memberAction(user=null){
  const fields=`${user?`<p class="help">${esc(user.email)} · 保存后该成员需重新登录。</p>`:`<div class="form-grid"><label>邮箱<input type="email" name="email" required maxlength="320"></label><label>Google subject（可稍后绑定）<input name="google_subject" maxlength="255" autocomplete="off"></label></div>`}<label>显示名称<input name="display_name" maxlength="200" value="${esc(user?.display_name||'')}"></label>${roleFields(user?.roles||['VIEWER'])}<label>地点范围<select name="scope_mode"><option value="all" ${user?.location_scope===null?'selected':''}>全部地点</option><option value="limited" ${user?.location_scope!==null&&user?'selected':''}>指定地点</option></select></label><label>指定地点代码（逗号分隔）<input name="scope_codes" value="${esc(user?.location_scope?.join(',')||'')}" placeholder="例如 W,US-LAX"></label><p class="help">当前业务工作台要求全部地点范围，指定地点成员暂不能使用业务页面。租户管理员必须拥有全部地点。</p>${user?`<label class="check"><input type="checkbox" name="is_active" ${user.is_active?'checked':''}>启用此成员</label>${note('权限变更依据')}`:state.providers.default==='password'?'<label>初始密码<input type="password" name="password" minlength="12" autocomplete="new-password" required></label>':'<p class="help">Google模式不创建可使用的本地密码；绑定经可信核验取得的subject后才可登录。系统不发送邀请邮件。</p>'}`;
  action(user?'编辑成员权限':'新增工作空间成员',fields,async d=>{
    const roles=[...$('action-form').querySelectorAll('[name=roles]:checked')].map(x=>x.value);if(!roles.length)throw Error('至少选择一个角色');
    const scope=d.scope_mode==='all'?null:d.scope_codes.split(',').map(x=>x.trim()).filter(Boolean);
    const body={roles,location_scope:scope,display_name:d.display_name};
    if(user)await api('/api/v1/users/'+user.id,{method:'PATCH',body:JSON.stringify({...body,is_active:d.is_active==='on',expected_updated_at:user.updated_at,note:d.note})});
    else await post('/api/v1/users',{...body,email:d.email,...(d.password?{password:d.password}:{}),...(d.google_subject?{google_subject:d.google_subject}:{})});
  });
}
async function checkAction(id,edit=false){
  try{
    const data=await api('/api/v1/data/reconciliations/'+id),r=data.check,c=data.case;
    if(edit){
      const assignees=await api('/api/v1/workspace/reconciliation-assignees');
      action('处置对账差异',`<p class="help">批次 ${esc(r.batch_id)}。独立清单修正后可复核当前批次；源数据有误时先在源系统修正并重新同步。关闭时需填写通过检查的ID，系统会校验来源、范围和时间窗口。</p><div class="form-grid"><label>处置状态<select name="status"><option value="in_progress" ${!c||c.status==='in_progress'?'selected':''}>处理中</option><option value="open" ${c?.status==='open'?'selected':''}>待处理</option><option value="resolved" ${c?.status==='resolved'?'selected':''}>已解决（需通过复核）</option></select></label><label>负责人<select name="assignee_id"><option value="">未分配</option>${options(assignees,x=>x.id,x=>x.email,c?.assignee_id||'')}</select></label></div><label>通过复核的检查ID<input name="resolution_check_id" value="${esc(data.history.find(x=>x.status==='matched')?.id||'')}" placeholder="关闭时必填"></label>${note('差异原因与处理依据')}`,d=>post(`/api/v1/data/batches/${r.batch_id}/disposition`,{...d,expected_revision:c?.revision||0,assignee_id:d.assignee_id||null,resolution_check_id:d.resolution_check_id||null}));
      return;
    }
    const samples=r.details.samples||[];
    action('对账差异明细',`<div class="metrics">${metric('源记录',number(r.details.source_count))}${metric('本地记录',number(r.details.local_count))}${metric('缺失 / 多出',`${r.details.missing_count} / ${r.details.extra_count}`)}${metric('内容变化',number(r.details.changed_count))}</div><p>${badge(r.status)}</p><p class="help">${esc(r.evidence)} · ${esc(r.checked_by)} · ${esc(r.created_at)}</p>${samples.length?table(['外部标识 / 类型','源记录','本地记录'],samples.map(x=>`<tr><td>${esc(x.external_id)}<small>${esc(({missing:'本地缺失',extra:'本地多出',changed:'内容或版本变化'})[x.kind])}</small><small>${esc(x.changed_fields.join('、'))}</small></td><td><pre>${esc(JSON.stringify(x.source,null,2))}</pre></td><td><pre>${esc(JSON.stringify(x.local,null,2))}</pre></td></tr>`).join('')):`<pre>${esc(JSON.stringify({missing:r.details.missing,extra:r.details.extra,changed:r.details.changed},null,2))}</pre>`}<p class="help">每类最多20条内容预览；仅展示库存、需求等允许字段，其他字段隐藏。历史检查可能只有键列表（每类最多100条）。</p><details><summary>范围与摘要</summary><pre>${esc(JSON.stringify({scope:r.details.scope,source_digest:r.source_digest,local_digest:r.local_digest},null,2))}</pre></details><h3>处置记录</h3>${data.notes.length?data.notes.map(n=>`<p class="help">${esc(n.created_at)} · ${esc(n.actor_label)}<br>${esc(adminLabels[n.after?.status]||n.after?.status||'重新打开')} · ${esc(n.after?.note||'')}</p>`).join(''):empty('暂无处置记录')}<h3>最近20次复核</h3>${table(['检查ID','结果','时间'],data.history.map(h=>`<tr><td>${esc(h.id)}</td><td>${badge(h.status)}</td><td>${esc(h.created_at)}</td></tr>`).join(''))}<div class="actions"><button type="button" data-manifest="${esc(r.batch_id)}" data-permission="integration:connection:edit">上传修正清单并复核</button><button type="button" class="secondary" data-disposition="${esc(r.id)}" data-permission="integration:connection:edit">记录处置</button></div>`,()=>{});
    $('action-form').querySelector('[type=submit]').hidden=true;enforcePermissions();
  }catch(error){message(error.message,true);}
}
function bindAdmin(page){
  const f=filtersFor(page);
  $('admin-filter')?.addEventListener('submit',e=>{e.preventDefault();Object.assign(f,Object.fromEntries(new FormData(e.currentTarget)),{offset:0});showPage(page);});
}
async function mappingAction(id){
  const row=adminViews.mappings.data.items.find(r=>r.id===id);
  action('确认主数据映射',`<p class="help">${esc(row.source_system)} / ${esc(row.external_id)} → ${row.entity==='sku'?'商品':'仓库'}。确认只影响后续规范化及本次重放。</p><label>搜索标准代码<input id="target-query" maxlength="160"></label><button type="button" class="secondary" id="target-search">查找标准代码</button><label>选择有效主数据<select name="target_code" id="target-options" required><option value="">请先查找</option></select></label>${note('映射确认依据')}`,async d=>{const result=await post(`/api/v1/mappings/${id}/resolve`,{target_code:d.target_code,expected_updated_at:row.updated_at,note:d.note});return `映射已确认，${result.requeued}条记录重新排队，等待Worker规范化。`;});
  $('target-search').onclick=async()=>{try{const rows=await api('/api/v1/workspace/catalog/search?'+new URLSearchParams({entity:row.entity,q:$('target-query').value}));$('target-options').innerHTML=rows.length?options(rows,x=>x.code,x=>`${x.code} · ${x.name||''}`):'<option value="">没有匹配的有效主数据</option>';}catch(error){message(error.message,true);}};
}
document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b||b.disabled)return;
  const f=filtersFor(state.page);
  if(b.hasAttribute('data-admin-prev')){f.offset=Math.max(0,f.offset-50);showPage(state.page);}
  if(b.hasAttribute('data-admin-next')){f.offset+=50;showPage(state.page);}
  if(b.dataset.check||b.dataset.disposition){$('action-dialog').close();checkAction(b.dataset.check||b.dataset.disposition,!!b.dataset.disposition);}
  if(b.dataset.map)mappingAction(b.dataset.map);
  if(b.hasAttribute('data-user-create'))memberAction();
  if(b.dataset.userEdit)memberAction(adminViews.users.data.items.find(r=>r.id===b.dataset.userEdit));
  if(b.dataset.userRevoke)action('撤销成员全部会话',note('撤销原因'),d=>post(`/api/v1/users/${b.dataset.userRevoke}/revoke-sessions`,d));
  if(b.dataset.userBind)action('绑定Google身份',`<p class="help">仅使用经可信流程核验的Google subject，不按邮箱自动合并账户。该操作不会发送邀请。</p><label>Google subject<input name="subject" required maxlength="255" autocomplete="off"></label>`,d=>post(`/api/v1/users/${b.dataset.userBind}/google-identity`,d));
});
