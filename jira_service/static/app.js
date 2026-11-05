const $ = (s) => document.querySelector(s);
const state = {issues: [], status: {}, focus: {}, view: 'overview', filter: 'all', jobFilter:'all', search: '', page: 1, detail: null, detailTab: 'materials', token: '', busy: false};
const escapeHTML = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const E = escapeHTML;
const labels = {quality_pending:'平台日志待就绪',unsupported_host:'日志区域待支持',invalid_response:'平台响应无效',feedback_missing:'未找到唯一反馈',too_large:'日志超过大小限制',invalid_feedback:'反馈标识无效',auth_required:'需连接账号',listing:'查询日志清单',downloading:'下载日志',downloaded:'下载完成',inspecting:'检查包内文件',archiving:'归档到Jira',archived:'已归档Jira',archive_pending:'等待附件核对',archive_failed:'归档未完成',network_error:'采集网络异常',no_logs:'平台未提供日志',analysis_failed:'分析执行失败',preflight:'预检完成 · 待分析',unconfigured:'待配置',pairing:'待绑定',queued:'排队中',running:'执行中',completed:'已完成',failed:'失败',interrupted:'已中断',cancelled:'已取消',draft:'待审阅',rejected:'已退回',approved:'已批准',stale:'已过期',superseded:'旧版本',published:'已发布',publishing:'发布中',publication_unknown:'发布结果待核对',ready:'可用',manual:'浏览器下载 → ZIP 导入',pending:'待接入',plugin:'插件可用',verify:'只读校验',missing:'未安装'};
const jobNames = {sync:'Jira 同步',refresh:'刷新工单',fetch:'下载构建产物',import_logs:'导入日志',import_quality_logs:'质量日志归档与分析',preflight:'材料预处理',source_check:'源码提交核对',analysis:'AI 根因分析',preparation_check:'现场预处理验证'};
const badge = (s) => `<span class="badge ${['completed','approved','published','ready'].includes(s)?'green':['failed','analysis_failed','stale','publication_unknown','missing'].includes(s)?'red':['running','queued','draft','pending','manual'].includes(s)?'orange':'blue'}">${E(labels[s] || s)}</span>`;
const fmt = (v) => v ? new Date(v).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
const size = n => n > 1048576 ? (n/1048576).toFixed(1)+' MB' : (n/1024).toFixed(1)+' KB';
let runConnection=null,runTimer=null,activeRun=null,runSeen=new Set();
const stageNames={quality_collection:'质量日志采集与归档',attachments:'下载与解包',scene_inventory:'现场分段与关联',build:'版本产物',crash_parse:'符号、寄存器与源码',ai_analysis:'AI 分析与草稿'};
const stepNames={quality_collect:'质量后台采集',quality_auth:'质量账号连接',quality_wait:'等待质量日志',quality_upload:'上传Jira附件',reboots:'复位类型与触发链',publication_check:'发布结果核对',images:'截图证据',cancel:'取消任务',review_request:'补充分析要求',stage_start:'阶段开始',stage_done:'阶段完成',stage_reuse:'复用检查点',stage_failed:'阶段失败',scene_inventory:'现场分段',scene_selection:'选择现场',crash:'现场解析',crash_stack:'调用栈',retry:'继续执行',similar:'相似工单',queue:'排队',start:'启动',context:'问题上下文',materials:'材料检查',scope:'分析范围',scan:'日志扫描',evidence:'证据提取',validate:'校验',conclusion:'当前结论',draft:'保存草稿',tool:'工具输出',source:'源码核对',jira:'Jira 读取',logs:'日志导入',fetch:'版本产物',blocked:'缺少材料',complete:'完成',failed:'失败',ai_prepare:'准备证据',ai_call:'调用模型',ai_status:'模型进度',ai_finding:'模型发现',ai_search:'定向检索',ai_assess:'交叉核对',ai_verdict:'根因结论',ai_error:'模型错误',review:'等待人工审批'};
function bindRunButtons(){document.querySelectorAll('[data-run]').forEach(x=>x.onclick=e=>{e.stopPropagation();safe(()=>openRun(x.dataset.run));});}
function renderRunState(run){
 activeRun={...activeRun,...run};
 $('#run-status').innerHTML=badge(activeRun.status)+` <span class="small-note">${E(activeRun.issue_key||'全局任务')} · ${E(activeRun.id.slice(0,8))}</span>`;
 $('#run-error').textContent=activeRun.error||'';
 $('#run-stages').innerHTML=(activeRun.stages||[]).map(s=>`<div class="job-card">${badge(s.status)} ${E(stageNames[s.name]||s.name)} ${s.reused_from?'<small>复用已校验检查点</small>':''}${s.error?`<p class="error-text">${E(s.error)}</p>`:''}</div>`).join('');
 const finished=!['queued','running'].includes(activeRun.status);
 if(finished)$('#run-next').innerHTML='';
 if(activeRun.status==='queued'){
  $('#run-current').textContent='尚未开始 · 前面有 '+(activeRun.ahead_count??0)+' 个任务';
  $('#run-next').innerHTML='<button class="button secondary" id="cancel-queued-run">取消排队任务</button>';
  $('#cancel-queued-run').onclick=()=>safe(async()=>{await api('/runs/'+activeRun.id+'/cancel',{});await openRun(activeRun.id);await refresh();});
 }else if(!finished)$('#run-next').innerHTML='';
 if(finished){$('#run-progress').value=activeRun.status==='completed'?100:0;$('#run-current').textContent=labels[activeRun.status]||activeRun.status;}
 if(activeRun.result){$('#run-result').innerHTML=`<details><summary>查看任务结构化结果</summary><pre>${E(JSON.stringify(activeRun.result,null,2))}</pre></details>`;}
 if(finished&&activeRun.issue_key){
  $('#run-next').innerHTML=`${activeRun.result?.next_run_id?'<button class="button primary" id="run-next-stage">继续查看下一阶段分析 →</button> ':''}<button class="button secondary" id="run-open-issue">打开工单材料与草稿 →</button>${['failed','interrupted'].includes(activeRun.status)&&['analysis','preflight'].includes(activeRun.kind)?'<button class="button primary" id="resume-run">从失败处继续</button><label>重新执行起点 <select id="restart-stage">'+Object.entries(stageNames).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select></label><button class="button secondary" id="restart-run">从所选阶段重跑</button>':''}`;
  if($('#resume-run'))$('#resume-run').onclick=()=>safe(async()=>{const r=await api('/runs/'+activeRun.id+'/retry',{});await openRun(r.run_id);});
  if($('#restart-run'))$('#restart-run').onclick=()=>safe(async()=>{const r=await api('/runs/'+activeRun.id+'/retry',{force_from:$('#restart-stage').value});await openRun(r.run_id);});
  if($('#run-next-stage'))$('#run-next-stage').onclick=()=>safe(()=>openRun(activeRun.result.next_run_id));
  $('#run-open-issue').onclick=()=>safe(async()=>{const key=activeRun.issue_key;$('#run-dialog').close();state.detailTab='draft';await openDetail(key);});
 }
}
function appendTrace(event){
 if(runSeen.has(event.id))return;runSeen.add(event.id);
 let details=event.details;try{if(typeof details==='string')details=JSON.parse(details);}catch{details=null;}
 if(event.step.startsWith('stage_')){const traceRun=activeRun.id;safe(async()=>{const current=await api('/runs/'+traceRun);if(activeRun?.id===traceRun)renderRunState(current);});}
 const div=document.createElement('article');div.className='trace-event';
 div.innerHTML=`<div class="trace-label"><span>${E(stepNames[event.step]||event.step)}</span><time>${new Date(event.created).toLocaleTimeString('zh-CN',{hour12:false})}</time></div><p>${E(event.message)}</p>${details?.text?`<pre class="trace-evidence">${E(details.text)}</pre><small>文件 ${E(details.artifact_id)} · 第 ${E(details.line)} 行</small>`:''}${details?`<details><summary>查看证据与数据</summary><pre>${E(JSON.stringify(details,null,2))}</pre></details>`:''}`;
 $('#run-trace').append(div);
 if(['queued','running'].includes(activeRun?.status)){
  $('#run-current').textContent=event.message;
  if(details?.total&&Number.isFinite(details.current))$('#run-progress').value=100*details.current/details.total;
  else $('#run-progress').removeAttribute('value');
 }
 if($('#run-autoscroll').checked)$('#run-trace').scrollTop=$('#run-trace').scrollHeight;
}
async function openRun(rid){
 const run=await api('/runs/'+encodeURIComponent(rid));
 runConnection?.close();clearInterval(runTimer);runSeen=new Set();activeRun=run;
 $('#run-title').textContent=(jobNames[run.kind]||run.kind)+' · 执行过程';
 $('#run-trace').innerHTML='';$('#run-result').innerHTML='';$('#run-next').innerHTML='';
 $('#run-error').textContent='';$('#run-current').textContent=run.status==='queued'?'等待执行器领取任务':'正在连接执行器';$('#run-progress').removeAttribute('value');
 $('#run-connection').textContent='正在连接实时事件流…';
 $('#run-scope').textContent=run.kind==='analysis'?'AI 根因分析任务：证据评估 → 定向检索 → 候选原因核对 → 待审结论。排队时尚未开始，实际执行步骤以下方记录为准。':run.kind==='preflight'?'当前任务为材料预检；启用 AI 时完成后自动衔接根因分析。':'展示实际执行步骤及工具输出。历史任务也可回看；旧任务的记录粒度取决于执行时的服务版本。';
 if(!$('#run-dialog').open)$('#run-dialog').showModal();
 history.replaceState(null,'','#run='+encodeURIComponent(rid));
 const updateElapsed=()=>{const end=['queued','running'].includes(activeRun.status)?Date.now():new Date(activeRun.updated).getTime();$('#run-elapsed').textContent='任务已历时 '+Math.max(0,Math.floor((end-new Date(activeRun.created).getTime())/1000))+' 秒';};
 renderRunState(run);updateElapsed();runTimer=setInterval(updateElapsed,1000);
 // Start from the durable event history. EventSource reconnects using Last-Event-ID.
 runConnection=new EventSource('/api/runs/'+encodeURIComponent(rid)+'/stream');
 runConnection.addEventListener('snapshot',e=>{renderRunState(JSON.parse(e.data));$('#run-connection').textContent='● 已连接 · 事件实时更新并保存，可关闭后回看';});
 runConnection.addEventListener('trace',e=>appendTrace(JSON.parse(e.data)));
 runConnection.addEventListener('state',e=>renderRunState(JSON.parse(e.data)));
 runConnection.addEventListener('done',()=>{runConnection?.close();$('#run-connection').textContent='✓ 任务已结束 · 当前展示已保存的完整执行记录';updateElapsed();clearInterval(runTimer);safe(refresh);});
 runConnection.onerror=()=>{$('#run-connection').textContent='连接暂时中断，正在自动重连；已保存事件会按游标续传。';};
}
$('#close-run').onclick=()=>$('#run-dialog').close();
$('#run-dialog').addEventListener('close',()=>{runConnection?.close();clearInterval(runTimer);history.replaceState(null,'',location.pathname+location.search);});
window.addEventListener('hashchange',()=>{const params=new URLSearchParams(location.hash.slice(1));if(params.get('view')==='focus'){state.view='focus';render();return;}const rid=params.get('run');if(rid&&(rid!==activeRun?.id||!$('#run-dialog').open))safe(()=>openRun(rid));else if(params.get('issue')){const key=params.get('issue');const tab=['materials','evidence','timeline','draft'].includes(params.get('tab'))?params.get('tab'):'materials';if(!$('#detail-dialog').open||state.detail?.issue.key!==key||state.detailTab!==tab||(params.get('report')||state.detail?.latest_report_id)!==state.detail?.selected_report_id)safe(async()=>{if($('#run-dialog').open)$('#run-dialog').close();state.detailTab=tab;await openDetail(key,params.get('report'));});}});
async function api(path, body, method) {
  const opts = {method: method || (body === undefined ? 'GET' : 'POST'), headers: {}};
  if (opts.method !== 'GET') opts.headers['x-jira-auto-token'] = state.token;
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
  const r = await fetch('/api'+path,opts); const d=await r.json();
  if (!r.ok) throw Error(typeof d.detail==='string'?d.detail:'请求失败，请检查参数');
  return d;
}
function toast(message) {$('#toast').textContent=message;$('#toast').hidden=false;setTimeout(()=>$('#toast').hidden=true,4500);}
function fail(e) {$('#error').textContent=e.message;$('#error').hidden=false;if($('#detail-dialog').open){$('#detail-message').textContent=e.message;$('#detail-message').classList.add('error-text');}}
async function safe(fn) {try {$('#error').hidden=true;await fn();} catch(e){fail(e);}}
async function refresh() {
  if(document.activeElement?.matches('input,textarea') && !$('#detail-dialog').open)return;
  if(state.busy)return;state.busy=true;
  try {
    const [a,b,c]=await Promise.all([api('/issues'),api('/status'),api('/focus')]);state.issues=a.issues;state.status=b;state.focus=c;
    $('.local-pill').textContent=b.lan_mode?'● 局域网访问已启用':'● 仅本机访问';
    $('#review-count').textContent=state.issues.filter(x=>x.reviewable).length;
    $('#poll-note').textContent=`每 ${b.poll_seconds/60} 分钟同步 · 最近更新 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
    $('#clock').textContent=new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'short'});
    render();
  } finally {state.busy=false;}
}
function render(){
  const names={focus:'今日优先处理',overview:'问题总览',reviews:'草稿与审批',activity:'执行记录',integrations:'接入与配置'};
  $('#crumb').textContent=names[state.view];
  $('#page-title').innerHTML=state.view==='overview'?'让问题，走向结论<span>。</span>':E(names[state.view])+'<span>。</span>';
  $('#page-subtitle').textContent=({focus:'每天最多 3 项，按截止日期、优先级和开始日期安排。',overview:'从工单与日志开始，连接版本、源码和每一条分析证据。',reviews:'审阅具体版本的草稿，确认后再决定是否发布。',activity:'每一次下载、校验与执行，都有迹可循。',integrations:'查看本地能力与待接入环节，区分已验证和未实现的能力。'})[state.view];
  document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===state.view));
  if(state.view==='focus') focusView();
  if(state.view==='overview') overview();
  if(state.view==='activity') activity();
  if(state.view==='integrations') integrations();
  if(state.view==='reviews') reviews();
}
function focusMarkup(){
 const f=state.focus||{},items=f.items||[];
 const delivery={scheduled:'09:00 后推送',waiting_sync:'等待 Jira 同步',pending:'等待生成今日名单',queued:'卡片待发送',sending:'卡片发送中',sent:'今日卡片已发送',delivery_unknown:'发送结果待核对，不自动重发',skipped:'今日工单均已完成或转出'};
 return `<section class="panel daily-focus"><div class="panel-head"><div><h2>今日优先处理 <span class="badge green">最多 3 项</span></h2><div class="panel-title-sub">${E(f.day||'')} · 截止日期 → P0 / P1 / P2 → 开始日期</div></div><small>${E(f.card_outdated?'推荐规则已更新 · 今日卡片保留原名单':delivery[f.delivery_state]||'等待同步')}</small></div><div class="focus-items">${items.map((i,n)=>`<article class="focus-item"><div class="focus-rank">${n+1}</div><div class="focus-body"><a href="${E(i.jira_url)}" target="_blank" rel="noreferrer" class="issue-key">${E(i.key)} ↗</a><h3>${E(i.summary)}</h3><p class="focus-reason">${E(i.reason)}</p><div class="focus-dates"><span>截止 <strong>${E(i.due_date||'未设置')}</strong></span><span title="${E(i.priority)}">优先级 <strong>${E(i.priority_label)}</strong> · ${E(i.priority)}</span><span>开始 <strong>${E(i.start_date||'未设置')}</strong></span></div></div><button class="button secondary" data-focus-issue="${E(i.key)}">查看工单</button></article>`).join('')||'<div class="empty">'+(f.locked?'今日名单中的工单已完成、转出或重新排期，明天重新推荐。':f.synced_at?'当前没有已到开始时间的待办工单。':'正在等待 Jira 同步日期和优先级。')+'</div>'}</div><div class="focus-foot"><span>${f.locked?'今日名单已确定；完成或转出后隐藏，次日重排。':'每日 09:00 确定当天名单，当前为预览。'}${f.excluded_overdue_issues?' 已排除 '+E(f.excluded_overdue_issues)+' 个逾期超过半年的工单。':''}${f.remaining_count?' 另有 '+E(f.remaining_count)+' 项待办，按每天 3 项控制工作量。':''}</span><span class="${f.fresh?'':'error-text'}">${f.fresh?'Jira 已同步':'数据待刷新 · 暂不发送提醒'} · ${fmt(f.synced_at)}</span></div></section>`;
}
function bindFocus(){document.querySelectorAll('[data-focus-issue]').forEach(b=>b.onclick=()=>safe(()=>openDetail(b.dataset.focusIssue)));}
function focusView(){
 $('#content').innerHTML=focusMarkup()+`<div class="notice">仅处理你当前名下、尚未完成的 Jira。北京时间每天 09:00 通过飞书机器人发送一张卡片，每天最多推荐 3 个；当天开始、当天截止和逾期会直接写明。开始日期尚未到的任务暂不推荐，已到截止日期的除外。截止日期已过去超过 6 个自然月的工单不参与推荐；恰好满 6 个月仍保留。日期为空时明确显示“未设置”。<br>优先级按 P0～P4 识别；Jira Highest / High / Medium 对应 P0 / P1 / P2，保留 Jira 原始名称。服务离线后当日恢复，会在成功同步后补发一次；结果未知不自动重发。</div>`;
 bindFocus();
}
function overview(){
  const bugs=state.issues.filter(x=>x.type==='Bug');const jobs=state.status.runs||[];
  const stats=[['已跟踪工单',state.issues.length,'名下工单 + 手动导入','◫'],['Bug 问题',bugs.length,'优先关注问题与反馈','◇'],['待人工审阅',state.issues.filter(x=>x.reviewable).length,'结论由你确认后发布','✓'],['执行中任务',state.status.active_count??jobs.filter(x=>['queued','running'].includes(x.status)).length,'点击查看执行过程 →','↗']];
  $('#content').innerHTML=`<div class="stats">${stats.map((x,i)=>`<div class="stat ${i===2?'highlight':''}" ${i===3?'id="active-jobs" role="button" tabindex="0"':''}><div class="stat-label">${x[0]}<span class="stat-icon">${x[3]}</span></div><div class="stat-number">${x[1].toString().padStart(2,'0')}</div><div class="stat-foot">${x[2]}</div></div>`).join('')}</div>
  ${focusMarkup()}<div class="layout"><section class="panel"><div class="panel-head"><div><h2>问题队列 <span class="badge">LIVE</span></h2><div class="panel-title-sub">先收集事实，再形成可审阅的结论</div></div><small>最近更新优先</small></div>
  <div class="toolbar"><div class="filter-group">${[['all','全部'],['analysis','分析列表'],['bug','Bug'],['feedback','质量反馈'],['review','待审阅']].map(x=>`<button class="filter ${state.filter===x[0]?'active':''}" data-filter="${x[0]}">${x[1]}</button>`).join('')}</div><input class="search" aria-label="搜索工单" placeholder="搜索工单号或问题…" value="${E(state.search)}"></div><div class="table-wrap"><table><thead><tr><th>问题 / ISSUE</th><th>设备版本</th><th>JIRA 状态</th><th>分析进度</th><th>更新</th></tr></thead><tbody id="issue-rows"></tbody></table></div><div class="table-bottom"><span id="issue-total"></span><span>点击问题查看材料与分析 ↗</span></div></section>
  <aside class="side-stack"><section class="panel"><div class="panel-head"><h2>工作流</h2><span class="badge green">LOCAL</span></div><div class="side-content">${[['01','接入与收集','Jira 监测、质量反馈链接和日志材料'],['02','准备分析环境','识别版本，获取 Manifest、ELF 与 MAP'],['03','证据与草稿','AI 根因分析 + 可追溯证据'],['04','你的确认',state.status.jira_write?'审阅根因分析，批准后评论':'审阅根因分析，回写未启用']].map(x=>`<div class="progress-step"><span class="step-number">${x[0]}</span><div><strong>${x[1]}</strong><p>${x[2]}</p></div></div>`).join('')}</div></section><section class="panel"><div class="panel-head"><h2>最近动态</h2><button class="text-link" id="view-activity">全部 →</button></div><div class="side-content">${(state.status.events||[]).slice(0,5).map(eventHTML).join('')||'<div class="empty">同步后显示执行记录</div>'}</div></section></aside></div>`;
  bindFocus();
  drawRows();
  document.querySelectorAll('[data-filter]').forEach(x=>x.onclick=()=>{state.filter=x.dataset.filter;state.page=1;overview();});
  $('.search').oninput=e=>{state.search=e.target.value;state.page=1;drawRows();};
  $('#view-activity').onclick=()=>{state.view='activity';render();};
  $('#active-jobs').onclick=()=>{state.view='activity';state.jobFilter='active';render();};
  $('#active-jobs').onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('#active-jobs').click();}};
}
function drawRows(){
 const items=state.issues.filter(i=>(state.filter==='all'||state.filter==='analysis'&&i.in_analysis||state.filter==='bug'&&i.type==='Bug'||state.filter==='feedback'&&i.feedback||state.filter==='review'&&i.reviewable)&&(`${i.key} ${i.summary} ${i.device||''}`).toLowerCase().includes(state.search.toLowerCase()));
 const pages=Math.max(1,Math.ceil(items.length/12));state.page=Math.min(state.page,pages);
 $('#issue-rows').innerHTML=items.slice((state.page-1)*12,state.page*12).map(i=>`<tr data-key="${E(i.key)}" tabindex="0"><td><div class="issue-key">${E(i.key)} <span class="badge">${E(i.type)}</span></div><div class="issue-summary" title="${E(i.summary)}">${E(i.summary)}</div><div class="issue-meta">${i.feedback?'◉ 质量平台反馈':'○ Jira 工单'}</div></td><td><div>${E(i.device||'—')}</div><div class="issue-meta">${E(i.firmware||'版本待确认')}</div></td><td>${badge(i.status)}</td><td>${i.analysis_run&&(['queued','running'].includes(i.analysis_run.status)||!i.report_status)?`<button class="text-link" data-run="${E(i.analysis_run.id)}">${badge(i.analysis_run.status)} 查看过程 →</button>`:i.report_status?badge(i.analysis_state||i.report_status):'<span class="badge">待收集材料</span>'}</td><td class="issue-meta">${fmt(i.updated)}<br><button class="text-link" data-select-analysis="${E(i.key)}">${i.in_analysis?'移出分析列表':'加入分析列表'}</button></td></tr>`).join('')||'<tr><td colspan="5"><div class="empty">没有符合条件的工单<br>可以同步 Jira，或导入一个工单开始。</div></td></tr>';
 document.querySelectorAll('[data-select-analysis]').forEach(b=>b.onclick=e=>{e.stopPropagation();safe(async()=>{const item=state.issues.find(i=>i.key===b.dataset.selectAnalysis);const result=await api('/issues/'+item.key+'/analysis-list',{},item.in_analysis?'DELETE':'POST');await refresh();if(result.run_id){toast(result.queued?'已加入并启动分析':'已加入，打开已有分析记录');await openRun(result.run_id);}});});
 $('#issue-total').innerHTML=`共 ${items.length} 条 · 第 ${state.page}/${pages} 页 <button class="text-link" id="prev-page" ${state.page===1?'disabled':''}>上一页</button> · <button class="text-link" id="next-page" ${state.page===pages?'disabled':''}>下一页</button>`;
 $('#prev-page').onclick=()=>{state.page--;drawRows();};$('#next-page').onclick=()=>{state.page++;drawRows();};
 bindRunButtons();
 document.querySelectorAll('[data-key]').forEach(x=>{x.onclick=()=>safe(()=>openDetail(x.dataset.key));x.onkeydown=e=>{if(e.key==='Enter')x.click();};});
}
function eventHTML(e){return `<div class="event"><div class="event-time">${fmt(e.created)}${e.issue_key?' · '+E(e.issue_key):''}</div><p>${E(e.message)}</p></div>`;}
function activity(){
 const jobs=[...new Map([...(state.status.active_runs||[]),...(state.status.runs||[])].map(r=>[r.id,r])).values()];
 const filtered=jobs.filter(r=>state.jobFilter==='all'||state.jobFilter==='active'&&['queued','running'].includes(r.status)||state.jobFilter==='failed'&&['failed','interrupted'].includes(r.status));
 $('#content').innerHTML=`<div class="notice">点击“查看过程”，持续查看真实步骤、工具输出、扫描进度和证据引用；已结束任务也能回看。AI 根因分析任务会调用模型、检索证据并生成待审结论；材料预检只是前置步骤。</div><section class="panel"><div class="panel-head"><h2>后台任务</h2><small>重启保留历史 · 中断任务需手动重试</small></div><div class="table-wrap"><table class="event-table"><thead><tr><th>任务</th><th>工单</th><th>状态</th><th>更新时间</th><th>结果说明</th><th>过程</th></tr></thead><tbody>${filtered.map(r=>`<tr><td>${E(jobNames[r.kind]||r.kind)}</td><td>${E(r.issue_key||'全局')}</td><td>${badge(r.status)}</td><td>${fmt(r.updated)}</td><td class="error-text">${E(r.error||'—')}</td><td><button class="button secondary" data-run="${E(r.id)}">查看过程 ↗</button></td></tr>`).join('')}</tbody></table></div></section><br><section class="panel"><div class="panel-head"><h2>事件时间线</h2></div><div class="side-content">${(state.status.events||[]).map(eventHTML).join('')||'<div class="empty">尚无执行记录</div>'}</div></section>`;
 if(!filtered.length)$('#content .event-table tbody').innerHTML='<tr><td colspan="6"><div class="empty">当前没有符合条件的任务，可切换到“全部记录”回看历史过程。</div></td></tr>';
 const tabs=document.createElement('div');tabs.className='toolbar';tabs.innerHTML=`<div class="filter-group">${[['all','全部记录'],['active','执行中 / 排队'],['failed','失败 / 中断']].map(x=>`<button class="filter ${state.jobFilter===x[0]?'active':''}" data-job-filter="${x[0]}">${x[1]}</button>`).join('')}</div>`;$('#content .panel-head').after(tabs);
 document.querySelectorAll('[data-job-filter]').forEach(x=>x.onclick=()=>{state.jobFilter=x.dataset.jobFilter;activity();});
 bindRunButtons();
}
function integrations(){
 $('#content').innerHTML=`<div class="notice">当前为单用户版本。飞书审批通过专用机器人长连接接入；质量平台已支持后台采集；自动 repo sync 尚未接入；界面中的“待接入”不会模拟成功。</div><div class="integration-grid">${(state.status.integrations||[]).map(x=>`<div class="integration-card"><h3>${E(x.name)}</h3>${badge(x.state)}<p>${E(x.detail)}</p></div>`).join('')}</div><section class="panel"><div class="panel-head"><h2>执行边界</h2></div><div class="side-content"><p class="small-note">固件源码目录：${E(state.status.source)}<br>${state.status.lan_mode?(state.status.lan_auth?'已监听局域网网卡，远程访问需登录。':'已监听局域网网卡，无需登录。'):'服务仅监听 127.0.0.1。'}日志与构建产物保存在服务的私有 data 目录。<br>Jira 回写：${state.status.jira_write?'已启用，仍需审批指定草稿':'关闭（JA_ALLOW_JIRA_WRITE=1 才能启用）'}<br>插件不能审批或发布评论；发布超时将标记“结果待核对”，不会自动重试。</p></div></section>`;
 const feishu=document.createElement('section');feishu.className='panel';feishu.innerHTML=`<div class="panel-head"><h2>专用 Jira Auto 飞书机器人</h2></div><div class="side-content"><ol><li>在飞书开放平台创建企业自建应用，启用机器人能力。</li><li>在服务源码目录的本机终端运行 <code>.venv/bin/python scripts/configure_feishu.py</code>，按提示输入 App ID 和 Secret。</li><li>事件与回调中启用长连接，添加“接收消息 im.message.receive_v1”和“卡片回传交互 card.action.trigger”；开通机器人发送消息和接收单聊消息权限，发布应用并把自己加入可用范围。</li><li>在飞书搜索并打开该机器人，发送配置脚本显示的绑定口令，系统会自动识别你的 open_id。</li><li>后续 AI 草稿会自动推送完整评论和审批按钮；过长草稿需在网页审阅。私聊“状态”可查看待审数量，“帮助”查看指令。</li></ol><p class="small-note">无需公网回调。凭据保存在本机私有 data 目录，不通过此网页或聊天收集。更改配置后脚本会重启服务。</p></div>`;$('#content').prepend(feishu);
 const quality=state.status.integrations?.find(x=>x.name==='质量平台');
 const guide=document.createElement('section');guide.className='panel quality-guide';guide.innerHTML=`<div class="panel-head"><h2>质量平台后台采集</h2>${badge(quality?.state||'auth_required')}</div><div class="side-content"><p>${E(quality?.detail||'请先连接质量平台账号')}</p><button class="button primary" id="connect-quality">${quality?.state==='ready'?'重新连接账号':'连接质量平台账号'} ↗</button><ol><li>连接一次质量平台账号，后续采集无需保持浏览器打开。</li><li>将反馈工单加入分析列表：后台先读 Jira 附件；缺日志时查询对应反馈、下载原始文件并打包。</li><li>检查包内文件、归档到 Jira 并通过 CLI 核对附件后，继续版本准备和 AI 分析。</li><li>“查看过程”实时记录采集、下载、归档和分析；登录失效会提示重连，平台日志未就绪会有限重试。</li></ol><p class="small-note">连接在当前页面完成。平台“解密日志”仍是独立异步功能；已有原始 ZIP 也可继续手动导入。</p><div class="feedback-links">${state.issues.filter(i=>i.feedback).slice(0,20).map(i=>`<button class="button secondary" data-quality="${E(i.key)}">${E(i.key)} · ${E(i.device||'设备待确认')}</button>`).join('')||'<p class="small-note">尚未发现质量反馈工单，请先同步或导入工单。</p>'}</div></div>`;$('#content').prepend(guide);
 $('#connect-quality').onclick=()=>safe(async()=>{const result=await api('/quality/login',{});location.assign(result.login_url);});
 document.querySelectorAll('[data-quality]').forEach(x=>x.onclick=()=>safe(async()=>{state.detailTab='materials';await openDetail(x.dataset.quality);}));
}
function reviews(){
 const items=state.issues.filter(i=>i.report_status);
 $('#content').innerHTML=`<div class="notice">审批只绑定你看到的草稿版本。新草稿或工单变化会使旧审批失效。当前 Jira 回写${state.status.jira_write?'已启用':'关闭'}。</div>${items.map(i=>`<div class="review-card"><div><h3>${E(i.key)} · ${E(i.summary)}</h3><p>${E(i.device||'设备待确认')} / ${E(i.firmware||'版本待确认')}</p></div><div>${badge(i.analysis_state||i.report_status)} <button class="button secondary" data-review="${E(i.key)}">打开审阅 →</button></div></div>`).join('')||'<div class="panel empty">还没有分析草稿。打开工单，导入日志后运行“材料预处理”。</div>'}`;
 document.querySelectorAll('[data-review]').forEach(x=>x.onclick=()=>safe(async()=>{state.detailTab='draft';await openDetail(x.dataset.review);}));
}
async function openDetail(key,reportId=null){state.detail=await api('/issues/'+encodeURIComponent(key)+'?compact=1'+(reportId?'&report_id='+encodeURIComponent(reportId):''));drawDetail();if(!$('#detail-dialog').open)$('#detail-dialog').showModal();detailHash();}
function detailHash(){const d=state.detail;history.replaceState(null,'','#issue='+encodeURIComponent(d.issue.key)+'&tab='+state.detailTab+(d.selected_report_id&&d.selected_report_id!==d.latest_report_id?'&report='+encodeURIComponent(d.selected_report_id):''));}
function drawDetail(){
 const d=state.detail,i=d.issue;const report=d.reports[0];
 $('#detail').innerHTML=`<div class="detail-head"><div><a class="issue-key" href="${E(i.jira_url)}" target="_blank" rel="noreferrer">${E(i.key)} ↗</a><h2>${E(i.summary)}</h2></div><button class="icon-button" id="close-detail" aria-label="关闭详情">×</button></div><div class="detail-body"><div class="detail-meta">${badge(i.status)}<span>${E(i.device||'设备待确认')} / ${E(i.firmware||'版本待确认')}</span><span>反馈：${E(i.feedback_time||'—')}</span><span>${E(i.priority_label||'未分级')} · ${E(i.priority||'优先级未设置')}</span><span>开始 ${E(i.start_date||'未设置')} · 截止 ${E(i.due_date||'未设置')}</span></div><div class="detail-actions"><button class="button secondary" data-job="refresh">↻ 刷新工单</button>${i.feedback?`<a class="button secondary" href="${E(i.feedback.url)}" target="_blank" rel="noreferrer">质量平台 ↗</a>`:''}<button class="button secondary" data-job="fetch" ${!i.device||!i.firmware?'disabled':''}>↓ XML / ELF / MAP</button><button class="button secondary" data-job="source_check">核对源码提交</button><button class="button primary" data-job="preflight">生成材料预处理报告</button><button class="button primary" data-job="analysis">AI 根因分析</button></div><div id="scene-choice"></div><div class="detail-tabs">${[['materials','材料与版本'],['evidence','日志证据'],['timeline','执行记录'],['draft','分析与审批']].map(x=>`<button class="detail-tab ${state.detailTab===x[0]?'active':''}" data-detail-tab="${x[0]}">${x[1]}</button>`).join('')}</div><div id="detail-panel"></div><div id="detail-message" role="status"></div></div>`;
 $('#close-detail').onclick=()=>$('#detail-dialog').close();
 const reloadButton=document.createElement('button');reloadButton.className='button secondary';reloadButton.textContent='刷新详情';reloadButton.onclick=()=>safe(()=>openDetail(i.key));$('.detail-actions').prepend(reloadButton);
 document.querySelectorAll('[data-detail-tab]').forEach(x=>x.onclick=()=>{state.detailTab=x.dataset.detailTab;drawDetail();detailHash();});
 const inv=d.scene_inventory;
 if(inv?.scenes?.length&&state.detailTab!=='draft'){$('#scene-choice').innerHTML=`<div class="notice"><strong>故障现场选择</strong><p>${E(inv.selection.reason)}</p><label>本轮现场 <select id="scene-id"><option value="">自动匹配（不唯一时暂停）</option>${inv.scenes.map(s=>`<option value="${E(s.id)}">${E(s.classification)} · ${E(s.thread||'线程未知')} · ${E(s.timestamp||'时间未知')} · L${s.stack_line} · ${E(s.firmware||'版本未知')} · dump ${E(s.dump_association?.status)}</option>`).join('')}</select></label><p class="small-note">选择现场后点击 AI 根因分析；只分析该现场，版本或 dump 关联未验证项仍会保留。</p><details><summary>查看现场分段、核心日志与寄存器</summary>${inv.scenes.map(s=>`<h4>${E(s.id)} · ${E(s.classification)}</h4><p>${E(s.boot_id)} · LF ${s.line_start}–${s.line_end} · 版本 ${E(s.version_match)}</p><pre class="trace-evidence">${E(s.raw_logs.map(l=>'L'+l.line+': '+l.text).join('\n'))}</pre><pre class="trace-evidence">${E(JSON.stringify({registers:s.registers,coredump:s.coredump,dump_association:s.dump_association},null,2))}</pre>`).join('')}</details></div>`;}
 const analysisRun=d.runs.find(r=>['analysis','preflight'].includes(r.kind)&&['queued','running'].includes(r.status));
 if(analysisRun){const note=document.createElement('div');note.className='notice';note.innerHTML=`此工单已有${E(jobNames[analysisRun.kind])}任务：${badge(analysisRun.status)} <button class="text-link" data-run="${E(analysisRun.id)}">查看执行过程 →</button>`;$('#scene-choice').prepend(note);document.querySelectorAll('[data-job="analysis"],[data-job="preflight"]').forEach(b=>b.disabled=true);bindRunButtons();}
 document.querySelectorAll('[data-job]').forEach(x=>x.onclick=()=>safe(async()=>{const r=await api('/issues/'+i.key+'/jobs',{kind:x.dataset.job,scene_id:$('#scene-id')?.value||null});await openRun(r.run_id);await refresh();}));
 const panel=$('#detail-panel');
 if(state.detailTab==='materials'){
  panel.innerHTML=`<div class="notice">Jira 已附日志或 ramdump 时，点击“AI 根因分析”会先自动下载、校验和解包；发现 dump 后获取对应 XML / ELF / MAP 并执行现场预处理。质量反馈缺日志时会自动从平台采集、打包并归档 Jira，再继续 AI 分析；也支持在此导入原始 ZIP。</div><form id="upload-form"><label class="small-note" for="log-file">导入原始日志 ZIP（最大 128 MB）</label><div class="search-row"><input type="file" accept=".zip" id="log-file" class="file-input" required><button class="button secondary">导入日志</button></div></form>${d.artifacts.map(a=>`<div class="file-row"><div><strong>${E(a.filename)}</strong><small>${E(a.kind)} · ${size(a.metadata.bytes||0)}${a.metadata.architecture?' · '+E(a.metadata.architecture):''}${a.metadata.projects?' · '+a.metadata.pinned_projects+'/'+a.metadata.projects+' 个提交锁定':''}${a.metadata.version?' · '+E(a.metadata.version):''}${a.kind==='image'?' · '+E(a.metadata.width)+'×'+E(a.metadata.height):''}${a.metadata.image_error?' · '+E(a.metadata.image_error):''}</small>${a.metadata.analysis_value?`<small>用途：${E(a.metadata.analysis_value)}</small><small>检查结果：${E(a.metadata.inspection||a.metadata.parse_status||'')}${a.metadata.readable_text&&a.metadata.invalid_utf8_chars?' · '+E(a.metadata.invalid_utf8_chars)+' 处编码替换（原文件保留）':''}</small>`:''}<small>SHA256 ${E((a.metadata.sha256||'').slice(0,24))}…</small></div>${a.kind==='image'?`<a class="button secondary" href="/api/artifacts/${E(a.id)}/preview?sha256=${E(a.metadata.sha256)}" target="_blank" rel="noreferrer">预览截图 ↗</a>`:''}<a class="button secondary" href="/api/artifacts/${E(a.id)}/download">下载</a></div>`).join('')||'<div class="empty">还没有材料，先导入日志或获取版本产物。</div>'}<details><summary class="small-note">工单详细描述（本地）</summary><pre>${E(i.description)}</pre></details>`;
  if(i.feedback){const q=d.quality_collection;const info=document.createElement('div');info.className='notice';info.innerHTML=q?`<strong>质量日志采集</strong> ${badge(q.state==='pending'?'quality_pending':q.state)}<p>${E(q.message||'')}</p><p class="small-note">${q.files?'文件 '+E(q.files)+' 个 · ':''}${q.bytes?size(q.bytes)+' · ':''}更新 ${fmt(q.updated)}${q.attachment_id?' · Jira 附件 '+E(q.attachment_id):''}</p>${q.run_id?`<button class="button secondary" data-run="${E(q.run_id)}">查看采集过程 →</button>`:''}${q.state==='auth_required'?'<button class="button primary" id="detail-quality-connect">连接质量平台 ↗</button>':''}`:'质量反馈：点击“AI 根因分析”会自动检查附件并在缺日志时后台采集。';panel.prepend(info);bindRunButtons();if($('#detail-quality-connect'))$('#detail-quality-connect').onclick=()=>safe(async()=>{const r=await api('/quality/login',{});location.assign(r.login_url);});}
  $('#upload-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const f=$('#log-file').files[0];if(!f)throw Error('请选择 ZIP 文件');const form=new FormData();form.append('file',f);const r=await api('/issues/'+i.key+'/logs',form);await openRun(r.run_id);});};
 }
 if(state.detailTab==='timeline'){
  panel.innerHTML=`<button class="button secondary" id="refresh-detail">↻ 刷新详情</button><br><br>${d.runs.map(r=>`<div class="job-card">${badge(r.status)} ${E(jobNames[r.kind]||r.kind)} <small>${fmt(r.updated)}</small> <button class="text-link" data-run="${E(r.id)}">查看过程 →</button>${r.error?`<p class="error-text">${E(r.error)}</p>`:''}${r.result?`<details><summary>查看结构化结果</summary><pre>${E(JSON.stringify(JSON.parse(r.result),null,2))}</pre></details>`:''}</div>`).join('')}<br>${d.events.map(eventHTML).join('')}`;
  $('#refresh-detail').onclick=()=>safe(()=>openDetail(i.key));
  bindRunButtons();
 }
 if(state.detailTab==='evidence'){
  panel.innerHTML='<form id="log-search" class="search-row"><input id="log-query" placeholder="搜索日志文本，例如 watchdog、firmware version" maxlength="200" required><button class="button primary">搜索证据</button></form><p class="small-note">结果保留文件标识与行号。关键词命中本身不能证明根因。</p><div id="log-results"></div>';
  $('#log-search').onsubmit=e=>{e.preventDefault();safe(async()=>{const r=await api('/issues/'+i.key+'/evidence?q='+encodeURIComponent($('#log-query').value));$('#log-results').innerHTML=r.matches.map(x=>`<div class="log-result"><small>文件 ${E(x.artifact_id.slice(0,8))} · 第 ${x.line} 行</small><pre>${E(x.text)}</pre></div>`).join('')||'<div class="empty">未找到匹配；请检查是否已导入可读日志。</div>';});};
 }
 if(state.detailTab==='draft'){
  panel.innerHTML=reviewMarkup(d,state.status);
  bindReview(d);
 }
}
document.querySelectorAll('.nav').forEach(x=>x.onclick=()=>{state.view=x.dataset.view;history.replaceState(null,'','#view='+state.view);render();});
$('#add-issue').onclick=()=>$('#import-dialog').showModal();
$('#detail-dialog').addEventListener('close',()=>{if(!$('#run-dialog').open)history.replaceState(null,'',location.pathname+location.search);});
$('#close-import').onclick=()=>$('#import-dialog').close();
$('#import-form').onsubmit=e=>{e.preventDefault();safe(async()=>{const added=await api('/issues',{key:$('#issue-key').value.trim()});$('#import-dialog').close();toast(added.key+(added.queued?' 已加入并启动分析':' 已在分析列表，打开已有记录'));await openRun(added.run_id);await refresh();});};
$('#sync').onclick=()=>safe(async()=>{const r=await api('/sync',{});await openRun(r.run_id);await refresh();});
safe(async()=>{state.token=(await api('/session')).token;await refresh();const params=new URLSearchParams(location.hash.slice(1));if(params.get('view')==='focus'){state.view='focus';render();}if(params.get('quality')==='connected'){state.view='integrations';render();toast('质量平台已连接，等待登录的分析任务会自动继续');}const rid=params.get('run');if(rid)await openRun(rid);else if(params.get('issue')){state.detailTab=['materials','evidence','timeline','draft'].includes(params.get('tab'))?params.get('tab'):'materials';await openDetail(params.get('issue'),params.get('report'));}setInterval(()=>safe(refresh),10000);});
