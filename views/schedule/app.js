import { ContentTransition } from './vendor/rhinelab/content-transition.js';

const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {free:'空闲', slow:'半忙', busy:'忙碌'};
const origins = {public_calendar:'公共日历', reference:'检索参考 · 待核对', generated:'模型创作 / 推定', manual:'手动设置'};
const kinds = {festival:'节日', vacation:'休假', activity:'活动', anniversary:'纪念日'};
const titles = {today:'日程总览', weekly:'角色与周表', calendar:'节日与活动', queues:'待回留言', settings:'运行设置'};
const tabTransition = new ContentTransition();
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const isoDate = date => date.toISOString().slice(0,10);
const dateObject = value => new Date(value+'T12:00:00Z');
function validTimezone() {
  const timezone=detail.profile?.timezone || state?.settings.timezone;
  try {new Intl.DateTimeFormat('en',{timeZone:timezone});return timezone;} catch {return state?.settings.timezone || undefined;}
}
let weekFilter = 'all', calendarMonth = '';
let state = null, detail = {}, tab = 'today', selected = new Set(), dirty = false, dirtySettings = false, loading = 0, job = null, generating = false;
let dirtyKinds = new Set();
function renderDraftStatus() { $('draft-status').textContent = dirty || dirtySettings ? '● 有未保存的修改' : ''; }
function markDirty(kind) { dirty = true; dirtyKinds.add(kind); renderDraftStatus(); }
let edit = null, pollTimer = null;
const api = (method, path, data) => method === 'GET' ? bridge.apiGet(path, data) : bridge.apiPost(path, data);
function notice(message, error = false) { $('notice').hidden = !message; $('notice').textContent = message; $('notice').classList.toggle('error', error); }
async function action(fn) { try { await fn(); } catch (error) { notice(error.message || '操作失败', true); } }
function askConfirm(message) {
  const dialog=$('confirm-dialog');
  if(dialog.open) return Promise.resolve(false);
  $('confirm-message').textContent=message;dialog.returnValue='cancel';
  return new Promise(resolve=>{dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true});dialog.showModal();});
}
function empty(heading, message) { return `<div class="empty"><strong>${escape(heading)}</strong>${escape(message)}</div>`; }
function switchTab(next) {
  tab = next;
  document.querySelectorAll('.tab').forEach(el => el.hidden = el.id !== tab);
  document.querySelectorAll('nav button').forEach(el => {el.classList.toggle('active', el.dataset.tab === tab);el.setAttribute('aria-pressed',el.dataset.tab === tab);});
  $('page-title').textContent = titles[tab]; $('persona-toolbar').hidden = ['settings','queues'].includes(tab);
  tabTransition.reveal($(tab), window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}
document.querySelectorAll('nav button').forEach(el => { el.setAttribute('aria-label',titles[el.dataset.tab]); el.addEventListener('click', () => switchTab(el.dataset.tab)); });
document.querySelector('.brand').onclick=event=>{event.preventDefault();switchTab('today');};
function setGenerationBusy(busy) {
  generating=busy;
  document.querySelectorAll('#generate,#generate-profile,#generate-calendar,#persona,#date,#date-strip button,#month-grid button,#jump-today,#add-day,#add-week,#add-event,#save-day,#save-profile,#save-calendar,[data-edit],[data-delete],#profile-form input,#profile-form select,#profile-form textarea').forEach(el=>el.disabled=busy);
}
function times(row) { return `${row.start}–${row.end}`; }
function weekdays(days) {
  if (days?.join(',') === '1,2,3,4,5') return '周一到周五';
  if (days?.join(',') === '6,7') return '周末';
  if (days?.length === 7) return '每天';
  return (days || []).map(d => '周' + '一二三四五六日'[d-1]).join('、');
}
function slotCards(rows, type, current = -1) {
  return rows.map((row, index) => {
    if (type === 'week' && weekFilter !== 'all' && !row.days.some(d=>weekFilter==='weekday'?d<=5:d>=6)) return '';
    const card = `<div class="card"><div class="card-content"><div class="card-meta">${type === 'week' ? `<span>${escape(weekdays(row.days))}</span><span>${escape(times(row))}</span>` : ''}<span class="badge ${escape(row.status)}">${escape(labels[row.status])}</span>${index === current ? '<span class="current-label">正在进行 · NOW</span>' : ''}</div><div class="card-title">${escape(row.title)}</div>${row.note ? `<p>${escape(row.note)}</p>` : ''}</div><div class="actions"><button data-edit="${type}" data-index="${index}" aria-label="编辑${escape(row.title)}">${icon('pen')}</button><button data-delete="${type}" data-index="${index}" class="delete" aria-label="删除${escape(row.title)}">${icon('trash')}</button></div></div>`;
    return type === 'day' ? `<article class="timeline-row ${escape(row.status)} ${index === current?'current':''}" data-slot-index="${index}" tabindex="-1"><div class="timeline-time">${escape(row.start)}<small>${escape(row.end)}</small></div>${card}</article>` : card;
  }).join('');
}
function renderDates() {
  const value = $('date').value, start = dateObject(value), today = localToday();
  start.setUTCDate(start.getUTCDate() - (start.getUTCDay()+6)%7);
  $('journal-month').textContent = `${value.slice(0,4)} / ${value.slice(5,7)} · 本周索引`;
  $('selected-date-number').textContent=value.slice(8);
  $('selected-date-month').textContent=`${value.slice(0,4)}.${value.slice(5,7)}`;
  $('selected-date-weekday').textContent=`星期${'日一二三四五六'[dateObject(value).getUTCDay()]}`;
  $('date-strip').innerHTML = Array.from({length:7},(_,i)=>{
    const date = new Date(start); date.setUTCDate(start.getUTCDate()+i); const iso = isoDate(date);
    return `<button data-date="${iso}" class="${iso===value?'selected':''} ${iso===today?'is-today':''}" aria-label="查看 ${iso} 的日程" aria-pressed="${iso===value}"><span class="day-name">周${'一二三四五六日'[i]}</span><span class="day-number">${date.getUTCDate().toString().padStart(2,'0')}</span><span class="date-mark"></span></button>`;
  }).join('');
}
function localToday() {
  return new Intl.DateTimeFormat('en-CA',{timeZone:validTimezone(),year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
}
function renderMonth() {
  const value = $('date').value;
  if (!calendarMonth || calendarMonth.slice(0,4)!==value.slice(0,4)) calendarMonth=value.slice(0,7);
  const first = dateObject(calendarMonth+'-01'), start = new Date(first);
  start.setUTCDate(first.getUTCDate()-(first.getUTCDay()+6)%7);
  const next = new Date(first); next.setUTCMonth(first.getUTCMonth()+1);
  const cellCount = Math.ceil(((next-first)/86400000+(first.getUTCDay()+6)%7)/7)*7;
  const events = detail.calendar?.events || [], today = localToday();
  $('month-title').textContent=`${calendarMonth.slice(0,4)} / ${calendarMonth.slice(5,7)}`;
  $('month-prev').disabled=calendarMonth.endsWith('-01'); $('month-next').disabled=calendarMonth.endsWith('-12');
  $('month-grid').innerHTML=Array.from({length:cellCount},(_,i)=>{
    const date=new Date(start);date.setUTCDate(start.getUTCDate()+i);const iso=isoDate(date), matches=events.filter(e=>e.start<=iso&&iso<=e.end);
    return `<button class="month-day ${iso.slice(0,7)!==calendarMonth?'other-month':''} ${iso===today?'is-today':''} ${iso===value?'selected':''}" data-calendar-date="${iso}" aria-label="${iso}${matches.length?' · '+escape(matches.map(e=>e.title).join('、')):''}"><span>${date.getUTCDate()}</span>${matches.slice(0,2).map(e=>`<span class="month-event ${e.day_off?'day-off':''}" title="${escape(e.title)}">${escape(e.title)}</span>`).join('')}${matches.length>2?`<span class="month-more">还有 ${matches.length-2} 项</span>`:''}</button>`;
  }).join('');
}
function updateLiveStatus() {
  const profile=detail.profile, day=detail.day, rows=day?.slots || [];
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {timeZone:validTimezone(), year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date()).filter(p => p.type !== 'literal').map(p => [p.type,p.value]));
  const today = `${parts.year}-${parts.month}-${parts.day}`, clock = `${parts.hour}:${parts.minute}`;
  $('live-time').textContent=clock;
  let current = -1;
  if(day && profile && day.date===today) current=rows.findIndex(r=>r.start<=clock&&clock<r.end);
  const row=rows[current], mode=row?.status || '';
  $('current-title').textContent=row?.title || (day ? '这一天的生活安排' : '等待生成日程');
  $('current-state').textContent=row ? labels[mode] : day ? '日程预览' : '尚无安排';
  $('current-state').dataset.mode=mode;
  $('status-code').textContent=({free:'ON',slow:'SLOW',busy:'OFF'})[mode] || '—';
  document.querySelector('.availability-diagram').dataset.mode=mode;
  $('hero-current').textContent=row ? `${times(row)} / 正在进行` : day ? `${day.date} / ${rows.length} 个时段` : '等待分析角色日常';
  $('live-date-label').textContent=current>=0 ? `LIVE / ${clock}` : 'DAILY PREVIEW';
  $('current-note').textContent=profile ? `${profile.timezone}${row?.note ? ' · '+row.note : ''}` : '先选择人设并生成档案。';
  document.querySelectorAll('[data-slot-index]').forEach(el=>{
    const active=Number(el.dataset.slotIndex)===current;
    el.classList.toggle('current',active);
    const label=el.querySelector('.current-label');
    if(label&&!active) label.remove();
    if(active&&el.matches('.timeline-row')&&!label) el.querySelector('.card-meta')?.insertAdjacentHTML('beforeend','<span class="current-label">正在进行 · NOW</span>');
  });
  return current;
}
function renderDetail() {
  const profile = detail.profile, day = detail.day, events = detail.calendar?.events || [];
  const rows = day?.slots || [], week = profile?.weekly || [];
  const current=updateLiveStatus();
  $('day-summary').textContent = day?.summary || '这一天还没有安排';
  const todaysEvents = day?.events || events.filter(e=>e.start<=$('date').value&&$('date').value<=e.end);
  $('day-events').innerHTML = todaysEvents.map(e=>`<span class="hero-event">${escape(e.title)}</span>`).join('');
  $('today-events').innerHTML = todaysEvents.map(e=>`<p><strong>${escape(e.title)}</strong>${e.description?'<br>'+escape(e.description):''}</p>`).join('') || '<p>无特别活动，按常规作息安排。</p>';
  const personaName = $('persona').value || '等待选择人设';
  $('persona-name').textContent=personaName; $('persona-avatar').textContent=Array.from(personaName)[0] || '✧';
  $('persona-identity').textContent=profile?.identity || '从人设出发，记录生活。';
  const worldTypes={real:'现实世界',fanfiction:'同人世界',original:'自创世界'};
  $('persona-tags').innerHTML=[profile?.world_name || worldTypes[profile?.world_type],...(profile?.countries || [])].filter(Boolean).map(s=>`<span>${escape(s)}</span>`).join('');
  $('profile-preview').textContent=profile ? `${profile.world_name} · ${profile.identity}` : '展开编辑世界观与生活习惯';
  const minutes = t=>Number(t.slice(0,2))*60+Number(t.slice(3));
  $('rhythm-bar').innerHTML=rows.map((r,index)=>`<button class="${escape(r.status)} ${index===current?'current':''}" data-slot-index="${index}" style="flex:${minutes(r.end)-minutes(r.start)}" title="${escape(times(r))} · ${escape(labels[r.status])} · ${escape(r.title)}" aria-label="定位 ${escape(times(r))} ${escape(r.title)}"></button>`).join('');
  renderDates(); renderMonth();
  $('day-list').innerHTML = rows.length ? slotCards(rows, 'day', current) : empty('当天日程尚未生成', '点击「生成这一天」，自动分析人设并生成档案、活动与日程。');
  $('day-count').textContent = `${String(rows.length).padStart(2,'0')} 个时段`;
  $('week-list').innerHTML = week.length ? slotCards(week, 'week') || empty('这些日子还没有安排','可添加周表时段，保存时需完整覆盖每天。') : empty('周表尚未生成', '生成当天会一并创建基础周表，也可以手动添加并保存。');
  $('week-count').textContent = `${week.length} 个时段`;
  const fields = {'world-type':'world_type','world-name':'world_name','persona-timezone':'timezone',identity:'identity',lifestyle:'lifestyle','calendar-notes':'calendar_notes'};
  for (const [id,key] of Object.entries(fields)) $(id).value = profile?.[key] || (key === 'world_type' ? 'real' : key === 'timezone' ? state?.settings.timezone || 'Asia/Shanghai' : '');
  $('countries').value = (profile?.countries || []).join(', ');
  $('calendar-year').textContent = $('date').value.slice(0,4);
  $('calendar-notices').textContent = (detail.calendar?.notes || []).join('；');
  $('event-count').textContent=`（${events.length} 项）`;
  $('event-list').innerHTML = events.length ? events.map((row,index)=>({row,index})).sort((a,b)=>a.row.start.localeCompare(b.row.start)).map(({row,index}) => `<article class="card"><div class="event-stamp" aria-hidden="true"><small>${Number(row.start.slice(5,7))} 月</small><strong>${row.start.slice(8)}</strong></div><div class="card-content"><div class="card-meta"><span>${escape(row.start)}${row.start !== row.end ? ' → '+escape(row.end) : ''}</span><span class="badge">${escape(kinds[row.kind])}${row.day_off ? ' · 休假' : ''}</span><small>${escape(origins[row.origin] || row.origin)}</small></div><div class="card-title">${escape(row.title)}</div><p>${escape(row.description)}</p><div>${(row.source_urls || []).filter(u=>u.startsWith('https://')).map((u,i)=>`<a href="${escape(u)}" target="_blank" rel="noopener noreferrer">参考 ${i+1} ↗</a>`).join(' · ')}</div></div><div class="actions"><button data-edit="event" data-index="${index}" aria-label="编辑${escape(row.title)}">${icon('pen')}</button><button data-delete="event" data-index="${index}" class="delete" aria-label="删除${escape(row.title)}">${icon('trash')}</button></div></article>`).join('') : empty('节庆也属于角色的世界', '选择适用的真实节日、原作活动，或创造自己的世界观事件。');
  setGenerationBusy(generating);
}
function renderSessions() {
  const q = $('session-search').value.toLowerCase();
  const sessions = [...state.sessions];
  for (const umo of selected) if (!sessions.some(s=>s.umo===umo)) sessions.push({umo,title:'手动选择的对话'});
  $('session-list').innerHTML = sessions.filter(s=>`${s.umo} ${s.title} ${s.persona_id}`.toLowerCase().includes(q)).map(s=>`<label class="session-row"><input type="checkbox" data-umo="${escape(s.umo)}" ${selected.has(s.umo) ? 'checked' : ''}><span>${escape(s.title || s.umo)}<small>${escape(s.umo)}${s.persona_id ? ' · '+escape(s.persona_id) : ''}</small></span></label>`).join('') || '<div class="hint" style="padding:16px">没有匹配的对话，可先给机器人发一条消息，或粘贴会话来源 ID。</div>';
}
function renderQueues() {
  $('queue-count').textContent = state.queues.length;
  $('queue-list').innerHTML = state.queues.map(row=>`<article class="card"><div class="card-content"><div class="card-meta"><span class="badge">${escape(({pending:'等待可联系',paused:'已暂停',sending:'发送中',uncertain:'待核对'})[row.delivery] || row.delivery)}</span><span>${row.count} 条留言${row.dropped ? ` · 已裁掉 ${row.dropped} 条较早留言` : ''}</span></div><div class="card-title">${escape(state.sessions.find(s=>s.umo===row.umo)?.title || row.umo)}</div><p>${escape(row.umo)}\n最早尝试：${escape(new Date(row.due*1000).toLocaleString())}${row.reason ? '\n'+escape(row.reason) : ''}</p></div><div class="actions"><button data-queue="retry" data-umo="${escape(row.umo)}" aria-label="重试队列">${icon('refresh')}</button><button data-queue="clear" data-umo="${escape(row.umo)}" class="delete" aria-label="清除队列">${icon('trash')}</button></div></article>`).join('') || empty('没有遗漏的留言', '忙碌和半忙期间收到的消息会暂存在这里，等角色有空时合并回复。');
  $('logs').innerHTML = [...state.logs].reverse().map(row=>`<div>${escape(new Date(row.at).toLocaleString())} · ${escape(row.message)}</div>`).join('') || '<p>暂无运行记录</p>';
}
function renderSettings() {
  const s = state.settings;
  for (const [id,key] of Object.entries({enabled:'enabled','whitelist-enabled':'whitelist_enabled','search-enabled':'search_enabled'})) $(id).checked = s[key];
  for (const [id,key] of Object.entries({'default-timezone':'timezone','slow-min':'slow_min_seconds','slow-max':'slow_max_seconds','queue-limit':'queue_limit','queue-expire':'queue_expire_hours'})) $(id).value = s[key];
  $('provider').innerHTML = '<option value="">跟随对话当前模型</option>'+state.providers.map(p=>`<option value="${escape(p.id)}">${escape(p.id)}${p.name !== p.id ? ' · '+escape(p.name) : ''}</option>`).join('');
  if (s.provider_id && !state.providers.some(p=>p.id===s.provider_id)) $('provider').add(new Option(s.provider_id+'（当前不可用）',s.provider_id));
  $('provider').value = s.provider_id;
  $('key-status').textContent = s.has_tavily_api_key ? '已保存密钥。留空不会覆盖现有密钥。' : '尚未配置密钥。未联网时，活动会标为模型创作或推定。';
  $('tavily-key').value = ''; $('clear-key').checked = false;
  selected = new Set(s.whitelist); dirtySettings=false; renderDraftStatus(); renderSessions();
}
async function loadState(withSettings = false) {
  state = await api('GET','state');
  $('enabled-label').textContent = state.settings.enabled ? (state.settings.whitelist_enabled ? '已启用 · 仅白名单' : '已启用 · 所有对话') : '插件尚未启用';
  document.querySelector('.aside-status').classList.toggle('is-off',!state.settings.enabled);
  const previous = $('persona').value;
  $('persona').innerHTML = '<option value="">选择 AstrBot 人设</option>'+state.personas.map(p=>`<option value="${escape(p.id)}">${escape(p.id)}${p.ready ? '' : ' · 待分析'}</option>`).join('');
  $('persona').value = state.personas.some(p=>p.id===previous) ? previous : state.personas[0]?.id || '';
  if (withSettings) renderSettings();
  renderQueues();
}
async function loadDetail() {
  const ticket = ++loading;
  if (!$('persona').value) { detail={}; renderDetail(); return; }
  const data = await api('GET','detail',{persona_id:$('persona').value,date:$('date').value});
  if (ticket !== loading) return;
  detail = data; dirty = false; dirtyKinds.clear(); renderDraftStatus(); calendarMonth=$('date').value.slice(0,7); renderDetail();
}
function getRows(type) {
  if (type === 'week') { detail.profile ||= {weekly:[]}; return detail.profile.weekly; }
  if (type === 'event') { detail.calendar ||= {events:[]}; return detail.calendar.events; }
  detail.day ||= {slots:[],summary:'手动日程',events:[]}; return detail.day.slots;
}
function openEditor(type, index = -1) {
  const row = index >= 0 ? getRows(type)[index] : type === 'event' ? {start:$('date').value,end:$('date').value,kind:'activity',day_off:false} : {start:'00:00',end:'24:00',status:'free',days:[1,2,3,4,5,6,7]};
  edit = {type,index}; $('edit-title').textContent = type === 'event' ? '编辑活动' : '编辑时间段'; $('edit-error').textContent = '';
  $('edit-fields').innerHTML = `<label>名称<input name="title" required maxlength="160" value="${escape(row.title)}"></label>`+(type === 'event' ?
    `<div class="row2"><label>开始日期<input type="date" name="start" required value="${escape(row.start)}"></label><label>结束日期（含当天）<input type="date" name="end" required value="${escape(row.end)}"></label></div><label>活动类型<select name="kind">${Object.entries(kinds).map(([k,v])=>`<option value="${k}" ${row.kind===k?'selected':''}>${v}</option>`).join('')}</select></label><label class="check"><input name="day_off" type="checkbox" ${row.day_off?'checked':''}>当天休假，调整常规工作 / 上课</label><label>内容与日期依据<textarea name="description" rows="4">${escape(row.description)}</textarea></label><label>参考链接（每行一个 HTTPS 链接）<textarea name="source_urls" rows="2">${escape((row.source_urls||[]).join('\n'))}</textarea></label>` :
    `${type === 'week' ? `<div class="days">${[1,2,3,4,5,6,7].map(d=>`<label><input name="days" type="checkbox" value="${d}" ${row.days.includes(d)?'checked':''}>周${'一二三四五六日'[d-1]}</label>`).join('')}</div>` : ''}<div class="row2"><label>开始（HH:MM）<input name="start" required pattern="[0-2][0-9]:[0-5][0-9]" value="${escape(row.start)}"></label><label>结束（可填 24:00）<input name="end" required pattern="[0-2][0-9]:[0-5][0-9]" value="${escape(row.end)}"></label></div><label>回复状态<select name="status">${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${row.status===k?'selected':''}>${v}</option>`).join('')}</select></label><label>具体情境<textarea name="note" rows="3">${escape(row.note)}</textarea></label>`);
  $('editor').showModal();
}
$('edit-form').addEventListener('submit',event=>{
  event.preventDefault(); const form = new FormData(event.target), {type,index} = edit, rows = getRows(type);
  const row = index >= 0 ? {...rows[index]} : {};
  row.title = form.get('title').trim(); row.start = form.get('start'); row.end = form.get('end');
  if (row.start >= row.end && (type !== 'event' || row.start !== row.end)) { $('edit-error').textContent='结束必须晚于开始，跨午夜时段请拆开。'; return; }
  if (type === 'event') { row.kind=form.get('kind'); row.day_off=form.has('day_off'); row.description=form.get('description'); row.source_urls=form.get('source_urls').split('\n').map(s=>s.trim()).filter(Boolean); if (row.source_urls.some(u=>!u.startsWith('https://'))) { $('edit-error').textContent='参考链接须使用 HTTPS。'; return; } row.origin='manual'; }
  else { row.status=form.get('status'); row.note=form.get('note'); if(type==='week') { row.days=form.getAll('days').map(Number); if(!row.days.length) { $('edit-error').textContent='至少选择一天。'; return; } } }
  if(index>=0) rows[index]=row; else rows.push(row);
  markDirty(type === 'week' ? 'profile' : type === 'event' ? 'calendar' : 'day'); $('editor').close(); renderDetail(); notice('已应用到预览，请点击对应的保存按钮。');
});
// Keep profile text edits when a weekly card rerenders the preview.
$('profile-form').addEventListener('input',()=>{
  detail.profile ||= {weekly:[]};
  Object.assign(detail.profile,{world_type:$('world-type').value,world_name:$('world-name').value,
    timezone:$('persona-timezone').value,identity:$('identity').value,lifestyle:$('lifestyle').value,
    calendar_notes:$('calendar-notes').value,countries:$('countries').value.split(/[,，\s]+/).filter(Boolean).map(s=>s.toUpperCase())});
  markDirty('profile');
});
for (const id of ['close-editor','cancel-editor']) $(id).onclick=()=>$('editor').close();
document.addEventListener('click',async event=>{
  const editButton=event.target.closest('[data-edit]'), del=event.target.closest('[data-delete]'), queue=event.target.closest('[data-queue]');
  if(editButton) openEditor(editButton.dataset.edit,Number(editButton.dataset.index));
  if(del && await askConfirm('从预览中删除这一项？保存后生效。')) { getRows(del.dataset.delete).splice(Number(del.dataset.index),1); markDirty(del.dataset.delete === 'week' ? 'profile' : del.dataset.delete === 'event' ? 'calendar' : 'day'); renderDetail(); }
  if(queue) action(async()=>{ if(!await askConfirm(queue.dataset.queue==='retry'?'确认已核对平台消息？重试可能重复发送。':'清除这个对话的待回复消息？')) return; await api('POST','queue',{umo:queue.dataset.umo,action:queue.dataset.queue}); await loadState(); notice('队列已更新。'); });
});
for (const [id,type] of [['add-day','day'],['add-week','week'],['add-event','event']]) $(id).onclick=()=>openEditor(type);
function requirePersona() { if(!$('persona').value) throw new Error('请先选择人设。'); }
async function save(kind,value) {
  requirePersona();
  const remaining = [...dirtyKinds].filter(k=>k!==kind), draft = structuredClone(detail);
  await api('POST','save',{persona_id:$('persona').value,date:$('date').value,kind,value});
  await loadDetail();
  for(const pending of remaining) { detail[pending === 'profile' ? 'profile' : pending === 'calendar' ? 'calendar' : 'day'] = draft[pending]; markDirty(pending); }
  if(remaining.length) renderDetail();
  notice('已保存'+(kind==='profile'?'。旧活动与日程已失效，请生成当天。':kind==='calendar'?'。当天安排已失效，请重新生成。':'。')+(remaining.length?'其他页未保存的修改仍保留在预览中。':''));
}
$('save-day').onclick=()=>action(()=>save('day',detail.day || {slots:[]}));
$('save-calendar').onclick=()=>action(()=>save('calendar',detail.calendar?.events || []));
$('save-profile').onclick=()=>action(async()=>{
  const profile={world_type:$('world-type').value,world_name:$('world-name').value,countries:$('countries').value.split(/[,，\s]+/).filter(Boolean).map(s=>s.toUpperCase()),timezone:$('persona-timezone').value,identity:$('identity').value,lifestyle:$('lifestyle').value,calendar_notes:$('calendar-notes').value,weekly:detail.profile?.weekly || []};
  if(!await askConfirm('保存档案与周表会让旧的活动和日程失效，继续？')) return;
  await save('profile',profile);
});
async function generate(scope) {
  requirePersona();
  if((dirty || scope!=='day') && !await askConfirm(scope==='profile'?'重新分析会重建档案、周表、活动和当天安排，继续？':scope==='calendar'?'重新生成会替换全年活动及相关日程，继续？':'重新生成将覆盖未保存的修改，继续？')) return;
  setGenerationBusy(true);
  try {
    const result=await api('POST','generate',{persona_id:$('persona').value,date:$('date').value,scope});
    job=result.job_id;
  } catch(error) {setGenerationBusy(false);throw error;}
  $('job-status').textContent='正在生成，请稍候…'; notice('正在按人设安排生活。首次生成需要分析档案和年度活动，可能需要几分钟。');
  pollTimer=setTimeout(pollJob,1500);
}
async function pollJob() {
  try { const result=await api('GET','job',{id:job}); if(result.status==='running'){pollTimer=setTimeout(pollJob,1800);return;}
    $('job-status').textContent=result.message;
    notice(result.message,result.status==='failed'); await loadDetail(); await loadState();
    job=null; setGenerationBusy(false);
  } catch(error) { job=null; setGenerationBusy(false); $('job-status').textContent='任务查询中断'; notice(error.message,true); }
}
for (const [id,scope] of [['generate','day'],['generate-profile','profile'],['generate-calendar','calendar']]) $(id).onclick=()=>action(()=>generate(scope));
$('save-settings').onclick=()=>action(async()=>{
  const payload={enabled:$('enabled').checked,whitelist_enabled:$('whitelist-enabled').checked,whitelist:[...selected],provider_id:$('provider').value,timezone:$('default-timezone').value,slow_min_seconds:Number($('slow-min').value),slow_max_seconds:Number($('slow-max').value),queue_limit:Number($('queue-limit').value),queue_expire_hours:Number($('queue-expire').value),search_enabled:$('search-enabled').checked};
  if($('clear-key').checked) payload.tavily_api_key=''; else if($('tavily-key').value.trim()) payload.tavily_api_key=$('tavily-key').value.trim();
  await api('POST','settings',payload); await loadState(true); notice('设置已保存。');
});
$('session-list').addEventListener('change',event=>{if(event.target.dataset.umo) { const u=event.target.dataset.umo; event.target.checked?selected.add(u):selected.delete(u); }});
$('session-search').oninput=renderSessions;
$('settings').addEventListener('input',event=>{if(event.target.matches('input,select,textarea')&&event.target.id!=='session-search'){dirtySettings=true;renderDraftStatus();}});
$('settings').addEventListener('change',event=>{if(event.target.matches('input,select,textarea')&&event.target.id!=='session-search'){dirtySettings=true;renderDraftStatus();}});
$('add-umo').onclick=()=>action(async()=>{const u=$('manual-umo').value.trim();if(u.split(':').length<3)throw new Error('请输入完整会话来源 ID。');selected.add(u);dirtySettings=true;renderDraftStatus();$('manual-umo').value='';renderSessions();notice('已加入选择，保存设置后生效。');});
$('refresh').onclick=()=>action(async()=>{if((dirty||dirtySettings)&&!await askConfirm('刷新会放弃未保存的修改，继续？'))return;await loadState(true);await loadDetail();notice('已刷新。');});
$('refresh-queues').onclick=()=>action(()=>loadState());
let priorPersona='',priorDate='';
async function chooseDate(value, openDay = false) {
  if(generating) return;
  if(value<'1970-01-01'||value>'2100-12-31') return;
  if(dirty&&!await askConfirm('切换会放弃未保存的修改，继续？')) return;
  $('date').value=value;
  await loadDetail();priorPersona=$('persona').value;priorDate=value;
  if(openDay) switchTab('today');
}
$('date-strip').addEventListener('click',event=>{const button=event.target.closest('[data-date]');if(button) action(()=>chooseDate(button.dataset.date));});
$('rhythm-bar').addEventListener('click',event=>{
  const button=event.target.closest('[data-slot-index]');if(!button)return;
  const row=$('day-list').querySelector(`[data-slot-index="${button.dataset.slotIndex}"]`);if(!row)return;
  $('day-list').querySelectorAll('.focused').forEach(el=>el.classList.remove('focused'));
  row.classList.add('focused');row.focus({preventScroll:true});row.scrollIntoView({block:'center',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
});
$('month-grid').addEventListener('click',event=>{const button=event.target.closest('[data-calendar-date]');if(button) action(()=>chooseDate(button.dataset.calendarDate,true));});
$('jump-today').onclick=()=>action(()=>chooseDate(localToday()));
for(const [id,delta] of [['month-prev',-1],['month-next',1]]) $(id).onclick=()=>{const date=dateObject(calendarMonth+'-01');date.setUTCMonth(date.getUTCMonth()+delta);calendarMonth=isoDate(date).slice(0,7);renderMonth();};
$('week-filter').addEventListener('click',event=>{const button=event.target.closest('[data-week-filter]');if(!button)return;weekFilter=button.dataset.weekFilter;document.querySelectorAll('[data-week-filter]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',b===button);});renderDetail();});
for (const id of ['persona','date']) $(id).onchange=()=>action(async()=>{if(dirty&&!await askConfirm('切换会放弃未保存的修改，继续？')){$('persona').value=priorPersona;$('date').value=priorDate;return;}await loadDetail();priorPersona=$('persona').value;priorDate=$('date').value;});
const liveTimer=setInterval(()=>{if(state)updateLiveStatus();},30000);
window.addEventListener('beforeunload',event=>{if(dirty||dirtySettings){event.preventDefault();event.returnValue='';}});
window.addEventListener('pagehide',()=>{clearTimeout(pollTimer);clearInterval(liveTimer);tabTransition.cancel();});
async function init() {
  $('date').value=new Intl.DateTimeFormat('en-CA',{year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  if(!bridge){notice('请从 AstrBot 管理面板 → 插件 → 角色日程 → 可视化视图打开本页面。',true);document.querySelectorAll('button').forEach(b=>b.disabled=true);return;}
  await bridge.ready();await loadState(true);await loadDetail();priorPersona=$('persona').value;priorDate=$('date').value;
}
action(init);
