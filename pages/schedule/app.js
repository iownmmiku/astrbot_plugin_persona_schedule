import { ContentTransition } from './vendor/rhinelab/content-transition.js';

const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {free:'空闲', slow:'半忙', busy:'忙碌'};
const origins = {public_calendar:'公共日历', reference:'检索参考 · 待核对', generated:'模型创作 / 推定', manual:'手动设置', recurring:'重复纪念日规则'};
const kinds = {festival:'节日', vacation:'休假', activity:'活动', anniversary:'纪念日'};
const sendLabels = {generating:'正在生成补回',sending:'正在发送',accepted:'平台发送调用已通过',uncertain:'发送结果待核对',unmatched:'平台未匹配',failed:'发送失败',paused:'已暂停',deferred:'等待可联系',unverified:'尚未验证'};
const titles = {today:'日程总览', weekly:'角色与周表', calendar:'节日与活动', queues:'待回留言', settings:'运行设置', diagnostics:'运行诊断', diary:'生活日记', workbench:'预演与撤销'};
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
let diary = null, diaryLoading = 0, autoPersonas = new Set();
let detailSelection = null, detailLoading = false, saving = false, followRoleToday = true;
let recurrenceData = null, recurrenceDraft = [], recurrenceDirty = false, recurrenceLoading = 0, recurrenceFetching = false, recurrenceEdit = null;
let historyData = null, historyLoading = 0, previewData = null, previewLoading = 0, previewBusy = false;
function hasPersonaDraft() {return dirty || recurrenceDirty;}
function renderDraftStatus() { $('draft-status').textContent = hasPersonaDraft() || dirtySettings ? '● 有未保存的修改' : ''; }
function markDirty(kind) { dirty = true; dirtyKinds.add(kind); renderDraftStatus(); }
let edit = null, pollTimer = null, refreshInFlight = false, diagnostics = null, boundary = null;
const minutes = value => /^\d{2}:\d{2}$/.test(value || '') ? Number(value.slice(0,2))*60+Number(value.slice(3)) : NaN;
const clockText = value => `${String(Math.floor(value/60)).padStart(2,'0')}:${String(value%60).padStart(2,'0')}`;
const parseInstant = value => value == null || value === '' ? null : typeof value === 'number' ? new Date(value < 1e12 ? value*1000 : value) : new Date(value);
const formatInstant = value => {const date=parseInstant(value);return date && !Number.isNaN(date.getTime()) ? date.toLocaleString('zh-CN',{timeZone:validTimezone(),hour12:false}) : '—';};
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
  $('page-title').textContent = titles[tab]; $('persona-toolbar').hidden = ['settings','queues','diagnostics'].includes(tab);
  $('generate').hidden = ['diary','workbench'].includes(tab);
  tabTransition.reveal($(tab), window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  if(next==='diagnostics') action(loadDiagnostics);
  if(next==='diary') action(loadDiary);
  if(next==='calendar')action(()=>loadRecurrences());
  if(next==='workbench'){renderPreviewSessions();action(loadHistory);}
}
document.querySelectorAll('nav button').forEach(el => { el.setAttribute('aria-label',titles[el.dataset.tab]); el.addEventListener('click', () => switchTab(el.dataset.tab)); });
document.querySelector('.brand').onclick=event=>{event.preventDefault();switchTab('today');};
function setGenerationBusy(busy) {
  generating=busy;
  const locked=controlsLocked(),editable=!locked&&detailMatchesSelection();
  document.querySelectorAll('#refresh,#persona,#date,#date-strip button,#month-grid button,#jump-today,#week-filter button').forEach(el=>el.disabled=locked);
  document.querySelectorAll('#generate,#generate-profile,#generate-calendar,#add-day,#add-week,#add-event,#save-day,#save-profile,#save-calendar,[data-edit],[data-delete],[data-split],[data-boundary],[data-week-day],#profile-form input,#profile-form select,#profile-form textarea,#edit-form input,#edit-form select,#edit-form textarea,#edit-form button[type="submit"],#boundary-form input,#boundary-form button[type="submit"]').forEach(el=>el.disabled=!editable);
  $('generate-diary').disabled = !editable || !diary?.can_generate;
  $('diary-latest').disabled = locked || !diary?.entries?.length;
  document.querySelectorAll('[data-diary-date]').forEach(el=>el.disabled=locked);
  document.querySelectorAll('#add-recurrence,#save-recurrences,[data-rule-edit],[data-rule-delete],#recurrence-form input,#recurrence-form select,#recurrence-form textarea,#recurrence-form button[type="submit"]').forEach(el=>el.disabled=!editable || recurrenceFetching || !recurrenceMatchesSelection());
  $('refresh-recurrences').disabled=locked || recurrenceFetching;
  document.querySelectorAll('#preview-run,#preview-model').forEach(el=>el.disabled=locked||previewBusy||!detailMatchesSelection());
  $('refresh-history').disabled=locked;
  document.querySelectorAll('[data-restore]').forEach(el=>el.disabled=locked||el.dataset.restorable!=='true');
}
function controlsLocked() {
  return generating || detailLoading || saving;
}
function detailMatchesSelection() {
  return Boolean(detailSelection?.personaId && detailSelection.personaId===$('persona').value && detailSelection.date===$('date').value);
}
function requireDetailReady() {
  if(controlsLocked())throw new Error('详情正在加载、保存或生成，请稍后再试。');
  if(!detailMatchesSelection())throw new Error('当前人设和日期的详情尚未加载，请刷新后再操作。');
}
function restoreDetailSelection() {
  if(detailSelection){$('persona').value=detailSelection.personaId;$('date').value=detailSelection.date;}
}
function times(row) { return `${row.start}–${row.end}`; }
function weekdays(days) {
  if (days?.join(',') === '1,2,3,4,5') return '周一到周五';
  if (days?.join(',') === '6,7') return '周末';
  if (days?.length === 7) return '每天';
  return (days || []).map(d => '周' + '一二三四五六日'[d-1]).join('、');
}
function slotCards(rows, type, current = -1) {
  return rows.map((row, index) => ({row,index})).sort((a,b)=>a.row.start.localeCompare(b.row.start)
    || (a.row.days?.[0] || 0)-(b.row.days?.[0] || 0)).map(({row,index}) => {
    if (type === 'week' && weekFilter !== 'all' && !row.days.some(d=>weekFilter==='weekday'?d<=5:weekFilter==='weekend'?d>=6:d===Number(weekFilter))) return '';
    const day=type==='week' ? (/^[1-7]$/.test(weekFilter)?Number(weekFilter):row.days[0]) : null;
    const pair=adjacentPair(type,index,day), range=row.slow_min_seconds != null ? ` · ${row.slow_min_seconds}–${row.slow_max_seconds} 秒` : '';
    const card = `<div class="card"><div class="card-content"><div class="card-meta">${type === 'week' ? `<span>${escape(weekdays(row.days))}</span><span>${escape(times(row))}</span>` : ''}<span class="badge ${escape(row.status)}">${escape(labels[row.status])}${row.status==='slow'?escape(range):''}</span>${row.locked?'<span class="badge locked">已锁定</span>':''}${index === current ? '<span class="current-label">正在进行 · NOW</span>' : ''}</div><div class="card-title">${escape(row.title)}</div>${row.note ? `<p>${escape(row.note)}</p>` : ''}</div><div class="actions"><button data-edit="${type}" data-index="${index}" aria-label="编辑${escape(row.title)}">${icon('pen')}</button><button data-split="${type}" data-index="${index}" aria-label="拆分${escape(row.title)}" title="拆分时段">⊞</button>${pair?`<button data-boundary="${type}" data-index="${index}" data-day="${day || ''}" aria-label="调整${escape(row.title)}后的边界" title="调整边界">↕</button>`:''}<button data-delete="${type}" data-index="${index}" class="delete" aria-label="删除${escape(row.title)}">${icon('trash')}</button></div></div>`;
    return type === 'day' ? `<article class="timeline-row ${escape(row.status)} ${index === current?'current':''}" data-slot-index="${index}" tabindex="-1"><div class="timeline-time">${escape(row.start)}<small>${escape(row.end)}</small></div>${card}</article>` : card;
  }).join('');
}
function adjacentPair(type,index,day) {
  const rows=getRows(type), first=rows[index];if(!first)return null;
  const second=rows.findIndex((r,i)=>i!==index&&r.start===first.end&&(type!=='week'||r.days.includes(day)));
  return second>=0 ? [index,second] : null;
}
function coverageErrors(rows,type) {
  const errors=[];
  const groups=type==='week'?[1,2,3,4,5,6,7]:[0];
  for(const day of groups) {
    const label=day?`周${'一二三四五六日'[day-1]}：`:'';
    const slots=rows.filter(r=>!day||r.days?.includes(day)).sort((a,b)=>minutes(a.start)-minutes(b.start));
    let cursor=0;
    for(const row of slots) {
      const start=minutes(row.start),end=minutes(row.end);
      if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(row.start||'')||!/^(([01]\d|2[0-3]):[0-5]\d|24:00)$/.test(row.end||'')||end<=start){errors.push(`${label}${row.title || '未命名时段'}的时间无效`);continue;}
      if(start>cursor)errors.push(`${label}${clockText(cursor)}–${row.start} 存在空档`);
      if(start<cursor)errors.push(`${label}${row.start} 附近有重叠`);
      cursor=Math.max(cursor,end);
    }
    if(cursor<1440)errors.push(`${label}${clockText(cursor)}–24:00 存在空档`);
  }
  return errors;
}
function renderValidation() {
  for(const type of ['day','week']) {
    const rows=type==='day'?detail.day?.slots||[]:detail.profile?.weekly||[],errors=coverageErrors(rows,type),el=$(type+'-validation');
    el.classList.toggle('invalid',errors.length>0);
    el.textContent=errors.length?`${errors.slice(0,3).join('；')}${errors.length>3?`；另有 ${errors.length-3} 处`:''}。修复后才能保存。`:'✓ 完整覆盖，无空档或重叠';
  }
}
function renderWeekMatrix() {
  const rows=detail.profile?.weekly || [];
  $('week-matrix').innerHTML=`<div class="matrix-scale"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>`+[1,2,3,4,5,6,7].map(day=>{
    const slots=rows.map((row,index)=>({row,index})).filter(({row})=>row.days.includes(day));
    return `<div class="matrix-column ${weekFilter===String(day)?'selected':''}"><button data-week-day="${day}" aria-pressed="${weekFilter===String(day)}" aria-label="筛选周${'一二三四五六日'[day-1]}">周${'一二三四五六日'[day-1]}</button><div class="matrix-track" data-track-day="${day}">${slots.map(({row,index})=>`<button class="matrix-slot ${escape(row.status)}" style="top:${minutes(row.start)/14.4}%;height:${(minutes(row.end)-minutes(row.start))/14.4}%" data-edit="week" data-edit-day="${day}" data-index="${index}" title="${escape(times(row))} ${escape(row.title)}${row.locked?' · 已锁定':''}" aria-label="编辑周${'一二三四五六日'[day-1]} ${escape(times(row))} ${escape(row.title)}"><span>${row.locked?'▣ ':''}${escape(row.title)}</span></button>${adjacentPair('week',index,day)?`<button class="boundary-handle" style="top:${minutes(row.end)/14.4}%" data-drag-boundary="${index}" data-day="${day}" aria-label="调整周${'一二三四五六日'[day-1]} ${escape(row.end)} 边界" title="拖动 / 方向键调整，回车输入时间"></button>`:''}`).join('')}</div></div>`;
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
  return todayInTimezone(validTimezone());
}
function todayInTimezone(timezone) {
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
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
  const runtime=state?.runtime?.personas?.[$('persona').value], queues=(state?.queues || []).filter(q=>(q.persona_id || state?.sessions?.find(s=>s.umo===q.umo)?.persona_id)===$('persona').value);
  const pending=runtime?.queue_count ?? queues.reduce((sum,q)=>sum+Number(q.count||0),0);
  $('persona-pending').textContent=`${pending} 条`;
  $('persona-send-state').textContent=queues.some(q=>q.delivery==='uncertain')?'有发送结果待核对':queues.some(q=>q.delivery==='sending')?'正在合并补回':pending?'等待可联系后补回':'暂无待回消息';
  const minimum=row?.slow_min_seconds ?? runtime?.slow_min_seconds ?? state?.settings.slow_min_seconds;
  const maximum=row?.slow_max_seconds ?? runtime?.slow_max_seconds ?? state?.settings.slow_max_seconds;
  $('slow-range').textContent=mode==='slow'?`${minimum}–${maximum} 秒`:mode==='busy'?'忙碌期间静默':mode==='free'?'正常回复':'—';
  let nextContact=runtime?.next_contact_at;
  if(current>=0) {
    const contact=rows.slice(current).find(r=>r.status!=='busy');
    if(row.status!=='busy')nextContact=Date.now();
    else if(contact) {
      const delta=minutes(contact.start)-minutes(clock), now=new Date();nextContact=now.getTime()+delta*60000-now.getSeconds()*1000-now.getMilliseconds();
    }
  }
  const next=parseInstant(nextContact), difference=next?Math.max(0,Math.ceil((next-new Date())/1000)):null;
  $('next-contact').textContent=next?(difference===0?'现在可联系':next.toLocaleTimeString('zh-CN',{timeZone:validTimezone(),hour:'2-digit',minute:'2-digit',hour12:false})):'尚未安排';
  $('contact-countdown').textContent=difference===null?'需生成后续日程':difference===0?(mode==='slow'?'正在半忙，回复会稍慢':'已可正常联系'):`还有 ${Math.floor(difference/3600)?Math.floor(difference/3600)+' 小时 ':''}${Math.floor(difference%3600/60)} 分 ${difference%60} 秒`;
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
  renderDates(); renderMonth(); renderAdaptations();
  $('day-list').innerHTML = rows.length ? slotCards(rows, 'day', current) : empty('当天日程尚未生成', '点击「生成这一天」，自动分析人设并生成档案、活动与日程。');
  $('day-count').textContent = `${String(rows.length).padStart(2,'0')} 个时段`;
  $('week-list').innerHTML = week.length ? slotCards(week, 'week') || empty('这些日子还没有安排','可添加周表时段，保存时需完整覆盖每天。') : empty('周表尚未生成', '生成当天会一并创建基础周表，也可以手动添加并保存。');
  $('week-count').textContent = `${week.length} 个时段`;
  renderWeekMatrix();renderValidation();
  const fields = {'world-type':'world_type','world-name':'world_name','persona-timezone':'timezone',identity:'identity',lifestyle:'lifestyle','calendar-notes':'calendar_notes'};
  for (const [id,key] of Object.entries(fields)) $(id).value = profile?.[key] || (key === 'world_type' ? 'real' : key === 'timezone' ? state?.settings.timezone || 'Asia/Shanghai' : '');
  $('countries').value = (profile?.countries || []).join(', ');
  $('calendar-year').textContent = $('date').value.slice(0,4);
  $('calendar-notices').textContent = (detail.calendar?.notes || []).join('；');
  $('event-count').textContent=`（${events.length} 项）`;
  $('event-list').innerHTML = events.length ? events.map((row,index)=>({row,index})).sort((a,b)=>a.row.start.localeCompare(b.row.start)).map(({row,index}) => `<article class="card"><div class="event-stamp" aria-hidden="true"><small>${Number(row.start.slice(5,7))} 月</small><strong>${row.start.slice(8)}</strong></div><div class="card-content"><div class="card-meta"><span>${escape(row.start)}${row.start !== row.end ? ' → '+escape(row.end) : ''}</span><span class="badge">${escape(kinds[row.kind])}${row.day_off ? ' · 休假' : ''}</span><small>${escape(origins[row.origin] || row.origin)}</small></div><div class="card-title">${escape(row.title)}</div><p>${escape(row.description)}</p><div>${(row.source_urls || []).filter(u=>u.startsWith('https://')).map((u,i)=>`<a href="${escape(u)}" target="_blank" rel="noopener noreferrer">参考 ${i+1} ↗</a>`).join(' · ')}</div></div><div class="actions">${row.origin==='recurring'||row.recurrence_id?'<span class="hint">在下方规则中编辑</span>':`<button data-edit="event" data-index="${index}" aria-label="编辑${escape(row.title)}">${icon('pen')}</button><button data-delete="event" data-index="${index}" class="delete" aria-label="删除${escape(row.title)}">${icon('trash')}</button>`}</div></article>`).join('') : empty('节庆也属于角色的世界', '选择适用的真实节日、原作活动，或创造自己的世界观事件。');
  setGenerationBusy(generating);
}
function renderSessions() {
  const q = $('session-search').value.toLowerCase();
  const sessions = [...state.sessions];
  for (const umo of selected) if (!sessions.some(s=>s.umo===umo)) sessions.push({umo,title:'手动选择的对话'});
  $('session-list').innerHTML = sessions.filter(s=>`${s.umo} ${s.title} ${s.persona_id}`.toLowerCase().includes(q)).map(s=>`<label class="session-row"><input type="checkbox" data-umo="${escape(s.umo)}" ${selected.has(s.umo) ? 'checked' : ''}><span>${escape(s.title || s.umo)}<small>${escape(s.umo)}${s.persona_id ? ' · '+escape(s.persona_id) : ''}</small></span></label>`).join('') || '<div class="hint" style="padding:16px">没有匹配的对话，可先给机器人发一条消息，或粘贴会话来源 ID。</div>';
  renderChatScope();
}
function renderChatScope() {
  const scope=$('chat-scope').value;
  $('whitelist-selection').hidden=scope!=='whitelist'&&!$('diary-chat-enabled').checked;
  $('diary-whitelist-status').textContent=$('diary-chat-enabled').checked?(selected.size?`已选 ${selected.size} 个日记素材会话。保存并启用后开始采集；聊天控制范围不会扩大这一名单。`:'当前白名单为空：日记只参考日程，不会读取任何聊天。'):'聊天素材已关闭，日记只参考当天日程与活动。';
  $('chat-scope-note').textContent=scope==='all'?'明确选择所有聊天：保存并启用插件后，所有触发聊天的会话都会受日程影响。':scope==='whitelist'?(selected.size?`已选 ${selected.size} 个会话。保存并启用插件后，仅这些聊天受日程影响。`:'白名单为空：不会控制任何聊天。先勾选会话或手动添加来源 ID。'):'不控制任何聊天。日程与日记仍可独立生成，聊天不会因作息而被延迟或取消。';
}
function renderAdaptations() {
  const rows=(state?.adaptation_log || []).filter(r=>r.persona_id===$('persona').value&&r.date===$('date').value).slice(-5).reverse();
  const actions={delay:'临时延后',defer:'临时延后',postpone:'临时延后',keep:'照常进行',notice:'提前告知',transition_notice:'提前告知',fatigue:'带些倦意',continue:'继续聊一会'};
  $('adaptation-count').textContent=rows.length?`最近 ${rows.length} 条`:'暂无记录';
  $('adaptation-list').innerHTML=rows.map(r=>{
    const session=state.sessions.find(s=>s.umo===r.umo),minutes=Number(r.minutes);
    const heading=actions[r.action] || '日程判断';
    return `<article class="adaptation-entry"><div class="adaptation-meta"><span class="badge">${escape(heading)}${Number.isFinite(minutes)&&minutes>0?' · '+escape(minutes)+' 分钟':''}</span><time>${escape(formatInstant(r.at))}</time></div><strong>${escape(r.activity || '当前活动')}</strong><p>${escape(r.reason || '根据当前对话和角色安排作出判断。')}</p><small>${escape(session?.title || r.umo || '受控对话')}${r.until?' · 持续至 '+escape(formatInstant(r.until)):''}</small></article>`;
  }).join('') || '<p class="hint">这一天还没有对话调整记录。基础安排仍按下方时间表进行。</p>';
}
function renderQueues() {
  $('queue-count').textContent = state.queues.reduce((sum,row)=>sum+Number(row.count || 0),0);
  $('queue-expiry-stats').textContent=`${state.queues.length} 个会话待回 · 累计清理 ${state.expired_messages || 0} 条过期留言`;
  $('queue-list').innerHTML = state.queues.map(row=>`<article class="card"><div class="card-content"><div class="card-meta"><span class="badge">${escape(['generating','sending'].includes(row.send_status)?sendLabels[row.send_status]:({pending:'等待可联系',paused:'已暂停',sending:'发送中',uncertain:'待核对'})[row.delivery] || row.delivery)}</span><span>${row.count} 条留言${row.dropped ? ` · 已裁掉 ${row.dropped} 条较早留言` : ''}${row.expired ? ` · 已过期 ${row.expired} 条` : ''}</span></div><div class="card-title">${escape(state.sessions.find(s=>s.umo===row.umo)?.title || row.umo)}</div><p>${escape(row.umo)}\n最早尝试：${escape(new Date(row.due*1000).toLocaleString())}${row.reason ? '\n'+escape(row.reason) : ''}</p></div><div class="actions"><button data-queue="retry" data-umo="${escape(row.umo)}" aria-label="重试队列">${icon('refresh')}</button><button data-queue="clear" data-umo="${escape(row.umo)}" class="delete" aria-label="清除队列">${icon('trash')}</button></div></article>`).join('') || empty('没有遗漏的留言', '忙碌和半忙期间收到的消息会暂存在这里，等角色有空时合并回复。');
  $('logs').innerHTML = [...state.logs].reverse().map(row=>`<div>${escape(new Date(row.at).toLocaleString())} · ${escape(row.message)}</div>`).join('') || '<p>暂无运行记录</p>';
}
async function loadDiagnostics() {
  const value=$('diagnostic-session').value;
  diagnostics=await api('GET','diagnostics',value?{umo:value}:undefined);
  const worker=diagnostics.worker || {},sessions=diagnostics.sessions || [];
  $('diagnostic-summary').innerHTML=[['后台任务',({running:'运行中',idle:'等待中',stopped:'已停止',failed:'异常',not_started:'尚未启动'})[worker.state] || worker.state || '未知'],['最近检查',formatInstant(worker.last_tick)],['WebUI 接口',diagnostics.web_available?'可用':'待确认'],['生成 / 补回模型',`${diagnostics.models?.schedule_provider_id || '跟随对话'} / ${diagnostics.models?.reply_provider_id || '跟随对话'}`]].map(([title,content])=>`<article class="paper-panel"><span>${escape(title)}</span><strong>${escape(content)}</strong></article>`).join('');
  const known=state.sessions || [],select=$('diagnostic-session');
  select.innerHTML='<option value="">全部已知会话</option>'+known.map(s=>`<option value="${escape(s.umo)}">${escape(s.title || s.umo)}</option>`).join('');
  if(value&&!known.some(s=>s.umo===value))select.add(new Option(value,value));select.value=value;
  $('integration-note').textContent=(diagnostics.integration_mode==='live'?'以下是当前 AstrBot 进程报告的状态。':'尚未取得真实 AstrBot 运行环境的联调结果。')+' “平台发送调用已通过”表示本机发送接口返回成功，不代表远端用户已经收到或阅读。'+(worker.last_error?` 后台最近错误：${worker.last_error}`:'')+(diagnostics.truncated?' 当前只显示部分会话，可通过上方选择器查询指定会话。':'');
  $('diagnostic-sessions').innerHTML=sessions.map(s=>`<article class="paper-panel diagnostic-session"><div class="section-heading"><h3>${escape(s.title || s.umo)}</h3><span class="badge ${s.allowed?'free':'busy'}">${s.allowed?'纳入日程':'未纳入日程'}</span></div><p class="diagnostic-umo">${escape(s.umo)}</p><dl>${[['实际人设',s.persona_id || '尚未识别'],['当前对话记录',s.cid || '尚未识别'],['平台',s.platform || '未知'],['生成模型',s.schedule_provider_id || '跟随对话'],['补回模型',s.reply_provider_id || '跟随对话'],['当前活动',s.current_activity || '尚无日程'],['回复状态',labels[s.current_status] || s.current_status || '未知'],['下次可联系',formatInstant(s.next_contact_at)],['慢回范围',s.slow_min_seconds != null?`${s.slow_min_seconds}–${s.slow_max_seconds} 秒`:'默认范围'],['最近发送',formatInstant(s.last_send_at)],['发送状态',sendLabels[s.send_status] || s.send_status || '尚无发送记录'],['发送接口验证',s.active_send_verified?'平台发送调用已通过':'尚未验证']].map(([key,text])=>`<div><dt>${escape(key)}</dt><dd>${escape(text)}</dd></div>`).join('')}</dl>${[s.error,s.schedule_model_error?`生成模型：${s.schedule_model_error}`:'',s.reply_model_error?`补回模型：${s.reply_model_error}`:'',s.last_send_error].filter(Boolean).map(message=>`<p class="error diagnostic-error">${escape(message)}</p>`).join('')}</article>`).join('') || empty('还没有可诊断的会话','先让机器人收到一条聊天消息，再刷新这里查看实际人设和模型。');
}
$('refresh-diagnostics').onclick=()=>action(loadDiagnostics);
$('diagnostic-session').onchange=()=>action(loadDiagnostics);
function renderSettings() {
  const s = state.settings;
  for (const [id,key] of Object.entries({enabled:'enabled','search-enabled':'search_enabled','diary-enabled':'diary_enabled','diary-chat-enabled':'diary_chat_enabled','auto-schedule-enabled':'auto_schedule_enabled','canvas-integration-enabled':'canvas_integration_enabled','adaptive-enabled':'adaptive_enabled','transition-notice-enabled':'transition_notice_enabled'})) $(id).checked = key==='diary_chat_enabled'?s[key]!==false:Boolean(s[key]);
  for (const [id,key] of Object.entries({'default-timezone':'timezone','slow-min':'slow_min_seconds','slow-max':'slow_max_seconds','queue-limit':'queue_limit','queue-expire':'queue_expire_hours','diary-limit':'diary_limit','transition-notice-minutes':'transition_notice_minutes','adaptive-max-delay-minutes':'adaptive_max_delay_minutes','active-conversation-minutes':'active_conversation_minutes','adaptive-review-interval-seconds':'adaptive_review_interval_seconds'})) $(id).value = s[key];
  $('chat-scope').value=s.whitelist_enabled?'whitelist':s.all_chats_enabled?'all':'none';
  $('auto-schedule-time').value = s.auto_schedule_time || '00:00';
  autoPersonas = new Set(s.auto_schedule_personas || []);
  $('auto-schedule-scope').value = autoPersonas.size ? 'selected' : 'analyzed';
  renderAutoPersonas();
  renderProviders(false);
  $('key-status').textContent = s.has_tavily_api_key ? '已保存密钥。留空不会覆盖现有密钥。' : '尚未配置密钥。未联网时，活动会标为模型创作或推定。';
  $('tavily-key').value = ''; $('clear-key').checked = false;
  selected = new Set(s.whitelist); dirtySettings=false; renderDraftStatus(); renderSessions();
}
function renderAutoPersonas() {
  $('auto-schedule-selection').hidden = $('auto-schedule-scope').value !== 'selected';
  $('auto-persona-count').textContent = `已选 ${autoPersonas.size} 个`;
  const personas = [...state.personas];
  for(const id of autoPersonas)if(!personas.some(p=>p.id===id))personas.push({id,unavailable:true});
  $('auto-persona-list').innerHTML = personas.map(p=>`<label class="auto-persona-row"><input type="checkbox" data-auto-persona="${escape(p.id)}" ${autoPersonas.has(p.id)?'checked':''}><span>${escape(p.id)}<small>${p.unavailable?'当前未找到 · 取消选择后保存可移除':p.ready?'已有生活档案':'待分析 · 首次生成时建立档案'}</small></span></label>`).join('') || empty('还没有 AstrBot 人设','先在 AstrBot 管理面板添加人设，再刷新这里选择。');
}
function renderProviders(preserve = true) {
  const s=state.settings,providers=state.providers || [];
  for(const [id,key] of [['schedule-provider','schedule_provider_id'],['reply-provider','reply_provider_id'],['diary-provider','diary_provider_id']]) {
    const select=$(id),value=preserve?select.value:(s[key] || (key==='diary_provider_id'?'':s.provider_id) || '');
    select.innerHTML=`<option value="">${key==='diary_provider_id'?'使用日程生成模型':'跟随对话当前模型'}</option>`+providers.map(p=>`<option value="${escape(p.id)}">${escape(p.name || (p.model&&p.model!==p.id?`${p.id} · ${p.model}`:p.id))}</option>`).join('');
    if(value&&!providers.some(p=>p.id===value))select.add(new Option(value+'（当前不可用）',value));select.value=value;
  }
  $('model-status').textContent=`已获取 ${providers.length} 个对话模型 · 日程、补回和日记可分别选择`;
  $('legacy-model-note').hidden=!s.provider_id;
  $('legacy-model-note').textContent='已加载旧版共用模型设置；保存后分别用于日程和补回。未指定日记模型时使用日程生成模型。';
}
function normalizeState(data) {
  return {...data,personas:data.personas || [],providers:data.providers || [],queues:data.queues || [],sessions:data.sessions || [],logs:data.logs || [],jobs:data.jobs || []};
}
function renderStateStatus() {
  const settings=state.settings,hasScope=settings.whitelist_enabled?Boolean(settings.whitelist?.length):Boolean(settings.all_chats_enabled);
  $('enabled-label').textContent = settings.enabled ? (settings.whitelist_enabled?(hasScope?'已启用 · 仅白名单':'已启用 · 白名单为空'):settings.all_chats_enabled?'已启用 · 所有聊天':'已启用 · 未控制聊天') : '插件尚未启用';
  document.querySelector('.aside-status').classList.toggle('is-off',!state.settings.enabled);
  const timestamp=state.checked_at || state.runtime?.checked_at || Date.now();
  $('last-updated').textContent=`自动同步 · ${formatInstant(timestamp)} · 每 15 秒更新`;
  renderQueues();updateLiveStatus();renderAdaptations();
}
async function loadState(withSettings = false) {
  state = normalizeState(await api('GET','state'));
  const previous = $('persona').value;
  $('persona').innerHTML = '<option value="">选择 AstrBot 人设</option>'+state.personas.map(p=>`<option value="${escape(p.id)}">${escape(p.id)}${p.ready ? '' : ' · 待分析'}</option>`).join('');
  const running=state.jobs.find(j=>j.status==='running');
  $('persona').value = state.personas.some(p=>p.id===previous) ? previous : (running?.persona_id && state.personas.some(p=>p.id===running.persona_id)?running.persona_id:state.personas[0]?.id || '');
  if (withSettings) renderSettings(); else renderAutoPersonas();
  renderStateStatus();
  recoverJob();
}
async function refreshLive() {
  if(refreshInFlight||!state||document.hidden)return;refreshInFlight=true;
  try {state=normalizeState(await api('GET','state'));renderStateStatus();renderProviders(true);renderAutoPersonas();recoverJob();if(tab==='diagnostics')await loadDiagnostics();if(tab==='diary')await loadDiary();}
  catch(error){$('last-updated').textContent=`自动同步失败 · ${error.message || '连接中断'}`;}
  finally {refreshInFlight=false;}
}
async function loadDetail({followToday=false} = {}) {
  const ticket = ++loading;
  const selection={personaId:$('persona').value,date:$('date').value};
  detailLoading=true;setGenerationBusy(generating);
  try {
    let data={};
    if(selection.personaId){
      data=await api('GET','detail',{persona_id:selection.personaId,date:selection.date});
      if(ticket!==loading)return false;
      const running=job || state?.jobs?.some(j=>j.persona_id===selection.personaId&&j.status==='running');
      if(followToday&&!running){
        const today=todayInTimezone(data.profile?.timezone || state?.settings.timezone);
        if(today!==selection.date){selection.date=today;data=await api('GET','detail',{persona_id:selection.personaId,date:today});}
      }
      if(ticket!==loading)return false;
      if(data.persona_id!==selection.personaId || data.day?.date && data.day.date!==selection.date)throw new Error('返回的详情与所选人设或日期不一致，请刷新后重试。');
    }
    if(ticket!==loading)return false;
    detail=data;detailSelection=selection;$('persona').value=selection.personaId;$('date').value=selection.date;
    syncPersonaTools(selection);
    diary=null;dirty=false;dirtyKinds.clear();renderDraftStatus();calendarMonth=selection.date.slice(0,7);renderDetail();recoverJob();
    // Auxiliary panels must not turn a committed detail refresh into a failure.
    if(tab==='diary')action(loadDiary);
    if(tab==='calendar')action(()=>loadRecurrences());
    if(tab==='workbench'){renderPreviewSessions();action(loadHistory);}
    return true;
  } catch(error) {
    if(ticket!==loading)return false;
    restoreDetailSelection();
    throw error;
  } finally {
    // A late response must neither unlock nor overwrite a newer selection.
    if(ticket===loading){detailLoading=false;recoverJob();setGenerationBusy(generating);}
  }
}

async function loadDiary() {
  const pid=$('persona').value,date=$('date').value,ticket=++diaryLoading;
  if(!pid){diary=null;renderDiary();return;}
  if(diary?.persona_id!==pid||diary?.date!==date){diary=null;renderDiary();}
  const data=await api('GET','diary',{persona_id:pid,date});
  if(ticket!==diaryLoading||pid!==$('persona').value||date!==$('date').value)return;
  diary=data;renderDiary();
}
function renderDiary() {
  const entry=diary?.entry,query=$('diary-search').value.trim().toLowerCase(),entries=diary?.entries || [];
  $('diary-count').textContent=`${entries.length} 篇`;
  $('diary-date').textContent=$('date').value || '—';
  $('diary-policy').textContent=diary ? `${diary.timezone} · 当地今天 ${diary.today} · ${diary.automatic?'跨日后自动归档':'自动归档已关闭，可手动写已结束的日子'}` : '选择人设，翻开属于这个世界的日记。';
  const material=diary?.chat_material || {},archivedMaterial=entry?.source?.chat_context;
  $('diary-materials').textContent=diary?(material.enabled?`当前可用聊天素材：${material.message_count || 0} 条 · ${material.conversation_count || 0} 个会话${material.truncated?' · 已按上限选取':''} · 白名单 ${diary.chat_whitelist_count || 0} 个`: '聊天素材未启用：按已保存日程与活动写日记'):'日程与白名单聊天，共同组成这一天的回忆。';
  $('diary-list').innerHTML=entries.filter(e=>`${e.date} ${e.title} ${e.mood}`.toLowerCase().includes(query)).map(e=>`<button data-diary-date="${escape(e.date)}" class="journal-entry ${e.date===$('date').value?'selected':''}" aria-pressed="${e.date===$('date').value}"><span>${escape(e.date)} <i>↗</i></span><strong>${escape(e.title)}</strong><small>${escape(e.mood || '生活片段')}</small></button>`).join('') || empty(query?'没有找到这段回忆':'日记尚未归档',query?'试试其他日期、标题或心情。':'先保存日程，等角色的一天结束后，回忆会出现在这里。');
  $('diary-empty').hidden=Boolean(entry);$('diary-content').hidden=!entry;
  $('diary-empty-reason').textContent=diary?.can_generate?'这一天已结束，点击「写这一天的日记」回顾已保存的安排。':diary?.reason || '选择过去的日期，或从目录中翻开一篇日记。';
  $('generate-diary').innerHTML=icon('pen')+(entry?'重新写这篇日记':'写这一天的日记');
  if(entry){
    $('diary-author').textContent=$('persona').value;
    $('diary-mood').textContent=entry.mood || '日常';
    $('diary-title').textContent=entry.title;
    $('diary-body').textContent=entry.content;
    $('diary-stamp').textContent=`${entry.timezone} · 第 ${entry.revision} 版 · 归档于 ${formatInstant(entry.updated_at)}`+(entry.persona_changed?' · 写于旧版人设，历史内容已保留':'');
    const source=entry.source?.schedule || {};
    $('diary-source-summary').textContent=source.summary || '旧篇目没有日程快照。';
    $('diary-source-slots').innerHTML=(source.slots || []).map(r=>`<div><time>${escape(times(r))}</time><span>${escape(r.title)}</span></div>`).join('');
    $('diary-chat-source').textContent=archivedMaterial?`归档时参考了 ${archivedMaterial.message_count || 0} 条文字、${archivedMaterial.conversation_count || 0} 个会话${archivedMaterial.truncated?'，素材按上限选取':''}。只保留数量摘要，不在日记快照中附带原始聊天。`:'这篇日记只记录了日程依据，未归档聊天素材。';
  }
  setGenerationBusy(generating);
}
$('diary-search').oninput=renderDiary;
$('diary-list').addEventListener('click',event=>{const button=event.target.closest('[data-diary-date]');if(button)action(()=>chooseDate(button.dataset.diaryDate));});
$('diary-latest').onclick=()=>action(()=>chooseDate(diary.entries[0].date));
function syncPersonaTools(selection) {
  if(recurrenceData?.persona_id!==selection.personaId){recurrenceData=null;recurrenceDraft=[];recurrenceDirty=false;recurrenceLoading++;recurrenceFetching=false;renderRecurrences();}
  if(historyData?.persona_id!==selection.personaId){historyData=null;historyLoading++;renderHistory();}
  // Saving, restoring or regenerating can change the same persona/date too.
  previewData=null;previewLoading++;previewBusy=false;renderPreview();
  renderDraftStatus();
}
function recurrenceMatchesSelection() {return recurrenceData?.persona_id===$('persona').value;}
function requireRecurrenceReady() {
  requireDetailReady();
  if(recurrenceFetching)throw new Error('重复规则正在读取，请稍后再编辑。');
  if(!recurrenceMatchesSelection())throw new Error('请先加载当前人设的重复规则。');
}
async function loadRecurrences({force=false}={}) {
  if($('recurrence-editor').open){if(force)throw new Error('请先关闭重复规则编辑窗口，再刷新。');return;}
  const pid=$('persona').value;
  if(!pid){recurrenceLoading++;recurrenceData=null;recurrenceDraft=[];recurrenceDirty=false;recurrenceFetching=false;renderRecurrences();return;}
  if(recurrenceMatchesSelection()&&recurrenceDirty&&!force){renderRecurrences();return;}
  const ticket=++recurrenceLoading;
  recurrenceFetching=true;setGenerationBusy(generating);
  $('recurrence-status').textContent='正在读取重复规则…';
  try {
    const data=await api('GET','recurrences',{persona_id:pid,date:$('date').value});
    if(ticket!==recurrenceLoading||pid!==$('persona').value)return;
    if(data.persona_id!==pid)throw new Error('重复规则的人设不匹配，请重新加载。');
    recurrenceData=data;recurrenceDraft=structuredClone(data.rules || []);recurrenceDirty=false;renderRecurrences();renderDraftStatus();
  } catch(error){if(ticket!==recurrenceLoading||pid!==$('persona').value)return;$('recurrence-status').textContent='重复规则加载失败：'+(error.message || '请重试');throw error;}
  finally {if(ticket===recurrenceLoading)recurrenceFetching=false;setGenerationBusy(generating);}
}
function renderRecurrences() {
  $('recurrence-count').textContent=`${recurrenceDraft.length} 项`;
  $('recurrence-status').textContent=recurrenceDirty?'● 规则草稿尚未保存':recurrenceMatchesSelection()?'已读取保存的规则 · 自动展开到每年活动':'选择人设后读取规则';
  $('recurrence-list').innerHTML=recurrenceDraft.map((rule,index)=>`<article class="recurrence-row"><div class="recurrence-date"><strong>${String(rule.month).padStart(2,'0')}.${String(rule.day).padStart(2,'0')}</strong><small>每年${rule.duration_days>1?' · '+escape(rule.duration_days)+' 天':''}</small></div><div class="recurrence-copy"><div class="card-meta"><span class="badge ${rule.enabled?'free':'busy'}">${rule.enabled?'启用':'已停用'}</span><span>${escape(kinds[rule.kind] || rule.kind)}${rule.day_off?' · 休假':''}</span><span>${rule.calendar==='fictional'?'虚构历法 · 公历映射':'公历'}</span></div><h3>${escape(rule.title)}</h3>${rule.description?`<p>${escape(rule.description)}</p>`:''}${rule.mapping?`<p class="hint">映射：${escape(rule.mapping)}</p>`:''}<small>${rule.start_year||rule.end_year?`${rule.start_year || '不限'} ～ ${rule.end_year || '不限'} 年`:'每年延续'}${Number(rule.month)===2&&Number(rule.day)===29?' · 平年'+(rule.leap_policy==='feb28'?'改在 2 月 28 日':'跳过'):''}</small></div><div class="actions"><button data-rule-edit="${index}" aria-label="编辑重复规则${escape(rule.title)}">${icon('pen')}</button><button data-rule-delete="${index}" class="delete" aria-label="删除重复规则${escape(rule.title)}">${icon('trash')}</button></div></article>`).join('') || empty('为特别的日子留一个位置','添加生日、约定或世界观活动；以后生成新年份，也会保留这些规则。');
  setGenerationBusy(generating);
}
function openRecurrence(index=-1) {
  requireRecurrenceReady();
  const row=index>=0?recurrenceDraft[index]:{title:'',kind:'anniversary',calendar:'gregorian',month:Number($('date').value.slice(5,7)),day:Number($('date').value.slice(8)),duration_days:1,leap_policy:'skip',enabled:true};
  if(!row)throw new Error('规则已变化，请重新打开。');
  recurrenceEdit={index,personaId:$('persona').value};
  for(const [id,key] of Object.entries({'rule-title':'title','rule-kind':'kind','rule-calendar':'calendar','rule-month':'month','rule-day':'day','rule-duration':'duration_days','rule-leap':'leap_policy','rule-start-year':'start_year','rule-end-year':'end_year','rule-mapping':'mapping','rule-description':'description'}))$(id).value=row[key] ?? '';
  $('rule-day-off').checked=Boolean(row.day_off);$('rule-enabled').checked=row.enabled!==false;
  $('recurrence-error').textContent='';$('recurrence-title').textContent=index>=0?'编辑重复纪念日':'添加重复纪念日';renderRuleMapping();$('recurrence-editor').showModal();
}
function renderRuleMapping() {$('rule-mapping-label').hidden=$('rule-calendar').value!=='fictional';$('rule-mapping').required=$('rule-calendar').value==='fictional';}
function applyRecurrence() {
  requireRecurrenceReady();if(recurrenceEdit?.personaId!==$('persona').value)throw new Error('人设已变化，请关闭编辑后重试。');
  const old=recurrenceDraft[recurrenceEdit.index] || {},row={...old,title:$('rule-title').value.trim(),kind:$('rule-kind').value,calendar:$('rule-calendar').value,month:Number($('rule-month').value),day:Number($('rule-day').value),duration_days:Number($('rule-duration').value),leap_policy:$('rule-leap').value,mapping:$('rule-mapping').value.trim(),description:$('rule-description').value.trim(),day_off:$('rule-day-off').checked,enabled:$('rule-enabled').checked};
  if(!row.title)throw new Error('请填写名称。');
  if(!Number.isInteger(row.month)||row.month<1||row.month>12||!Number.isInteger(row.day)||row.day<1||row.day>new Date(Date.UTC(2000,row.month,0)).getUTCDate())throw new Error('请填写有效的月份和日期。');
  if(!Number.isInteger(row.duration_days)||row.duration_days<1||row.duration_days>366)throw new Error('持续时间应为 1～366 天。');
  for(const [id,key] of [['rule-start-year','start_year'],['rule-end-year','end_year']]){const value=$(id).value;if(value){row[key]=Number(value);if(!Number.isInteger(row[key])||row[key]<1970||row[key]>2100)throw new Error('年份应为 1970～2100。');}else delete row[key];}
  if(row.start_year&&row.end_year&&row.start_year>row.end_year)throw new Error('结束年份不能早于开始年份。');
  if(row.calendar==='fictional'&&!row.mapping)throw new Error('请说明虚构历法如何映射到公历日期。');
  if(recurrenceEdit.index<0){if(recurrenceDraft.length>=100)throw new Error('最多保留 100 项重复规则。');recurrenceDraft.push(row);}else recurrenceDraft[recurrenceEdit.index]=row;
  recurrenceDirty=true;renderDraftStatus();renderRecurrences();$('recurrence-editor').close();notice('规则已加入草稿，请保存重复纪念日。');
}
async function saveRecurrences() {
  requireRecurrenceReady();
  const pid=$('persona').value;
  if(!await askConfirm('保存重复规则会更新年度活动，相关生成日程可能需要重新生成；手工时段保留。继续？'))return;
  requireRecurrenceReady();if(pid!==$('persona').value)throw new Error('人设已变化，请重新确认。');const rules=structuredClone(recurrenceDraft),pending=[...dirtyKinds],draft=structuredClone(detail);
  saving=true;setGenerationBusy(generating);
  try {await api('POST','recurrences-save',{persona_id:pid,rules});recurrenceDirty=false;await loadRecurrences({force:true});await loadDetail();
    for(const kind of pending){if(kind==='calendar'&&draft.calendar){const recurring=(detail.calendar?.events || []).filter(row=>row.origin==='recurring'||row.recurrence_id);detail.calendar={...draft.calendar,events:[...draft.calendar.events.filter(row=>row.origin!=='recurring'&&!row.recurrence_id),...recurring]};}else detail[kind]=draft[kind];markDirty(kind);}if(pending.length)renderDetail();
    notice('重复纪念日已保存，跨年生成会自动带入。'+(pending.length?'其他页面的未保存修改仍保留。':''));}
  finally {saving=false;recoverJob();setGenerationBusy(generating);renderDraftStatus();}
}
$('add-recurrence').onclick=()=>action(()=>openRecurrence());
$('refresh-recurrences').onclick=()=>action(async()=>{if(recurrenceDirty&&!await askConfirm('重新读取会放弃重复规则草稿，继续？'))return;await loadRecurrences({force:true});});
$('save-recurrences').onclick=()=>action(saveRecurrences);
$('rule-calendar').onchange=renderRuleMapping;
for(const id of ['close-recurrence','cancel-recurrence'])$(id).onclick=()=>$('recurrence-editor').close();
$('recurrence-form').onsubmit=event=>{event.preventDefault();try {applyRecurrence();}catch(error){$('recurrence-error').textContent=error.message;}};
$('recurrence-list').addEventListener('click',event=>action(async()=>{
  if(controlsLocked()||recurrenceFetching||!recurrenceMatchesSelection())return;
  const edit=event.target.closest('[data-rule-edit]'),del=event.target.closest('[data-rule-delete]');
  if(edit)openRecurrence(Number(edit.dataset.ruleEdit));
  if(del&&await askConfirm('从草稿中删除这项重复规则？保存后生效。')){requireRecurrenceReady();recurrenceDraft.splice(Number(del.dataset.ruleDelete),1);recurrenceDirty=true;renderDraftStatus();renderRecurrences();}
}));
function renderPreviewSessions() {
  const previous=$('preview-session').value;
  $('preview-session').innerHTML='<option value="">仅查看角色安排</option>'+(state?.sessions || []).filter(s=>s.persona_id===$('persona').value).map(s=>`<option value="${escape(s.umo)}">${escape(s.title || s.umo)}</option>`).join('');
  if((state?.sessions || []).some(s=>s.umo===previous&&s.persona_id===$('persona').value))$('preview-session').value=previous;
}
async function runPreview(evaluate=false) {
  requireDetailReady();if(previewBusy)return;
  const selection={...detailSelection},ticket=++previewLoading;
  const payload={persona_id:selection.personaId,date:selection.date,time:$('preview-time').value,message:$('preview-message').value,umo:$('preview-session').value,evaluate,active_conversation:$('preview-active').checked};
  if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(payload.time))throw new Error('请输入有效的角色当地时间。');
  previewBusy=true;previewData=null;renderPreview();$('preview-loading').textContent=evaluate?'模型正在权衡…':'正在读取保存的安排…';setGenerationBusy(generating);
  try {const result=await api('POST','preview',payload);if(ticket!==previewLoading||!detailMatchesSelection()||selection.personaId!==detailSelection.personaId||selection.date!==detailSelection.date)return;if(result.persona_id!==selection.personaId||result.date!==selection.date)throw new Error('预演结果与当前人设或日期不符，请重试。');previewData={...result,requested_evaluation:evaluate};renderPreview();}
  catch(error){if(ticket!==previewLoading)return;$('preview-result').innerHTML=empty('这次预演未完成',error.message || '请检查已保存日程后重试。');throw error;}
  finally {if(ticket===previewLoading){previewBusy=false;$('preview-loading').textContent='';setGenerationBusy(generating);}}
}
function renderPreview() {
  const result=previewData;
  if(!result){$('preview-result').innerHTML=empty('留一个时刻给角色','选择日期、时间和会话，再运行预演。');$('preview-loading').textContent='';return;}
  const base=result.base_activity || {},effective=result.effective_activity || {},scope=result.scope || {},decision=result.decision;
  const behavior=scope.command_bypass?'已注册命令优先，交给 AstrBot 处理':!scope.allowed?'当前会话不受日程拦截':result.would_queue?(result.status==='busy'?'静默暂存，恢复联系后合并补回':'暂存消息，延迟后合并回复'):'正常进入聊天回复';
  $('preview-result').innerHTML=`<div class="forecast-heading"><span class="badge ${escape(result.status)}">${escape(labels[result.status] || result.status)}</span><strong>${escape(result.date)} ${escape(result.time)}</strong><small>${escape(result.timezone)}</small></div><h3 class="forecast-behavior">${escape(behavior)}</h3><dl class="forecast-facts"><div><dt>基础活动</dt><dd>${escape(base.title || '未安排')} <small>${escape(base.start || '')}–${escape(base.end || '')}</small></dd></div><div><dt>最终活动</dt><dd>${escape(effective.title || base.title || '未安排')}</dd></div><div><dt>慢回范围</dt><dd>${result.delay_seconds?escape(result.delay_seconds.join('～'))+' 秒':'不适用'}</dd></div><div><dt>下次可联系</dt><dd>${result.next_contact_at?escape(formatInstant(result.next_contact_at)):'后续尚未安排'}</dd></div></dl><p class="forecast-reason">${escape(result.reason || scope.reason || '')}</p>${result.upcoming_activity?`<p class="hint">接下来：${escape(result.upcoming_activity.slot?.title || result.upcoming_activity.title || '下一项安排')}</p>`:''}${decision?`<div class="forecast-decision"><span class="section-code">MODEL / ONE JUDGMENT</span><p>${escape(({keep:'按原安排',defer:'有限延后',brief_exception:'短暂回复窗口'})[decision.action] || decision.action)}${decision.delay_minutes?' · '+escape(decision.delay_minutes)+' 分钟':''}</p><p>${escape(decision.reason || '')}</p></div>`:''}<p class="hint">${result.model_evaluated?'已取得日程模型的模拟判断':result.requested_evaluation?'未采用模型调整，保留基础策略':'未调用模型'} · 只读结果，没有暂存消息、发送回复或保存日程调整。</p>`;
}
$('preview-run').onclick=()=>action(()=>runPreview(false));
$('preview-model').onclick=()=>action(()=>runPreview(true));
async function loadHistory() {
  const pid=$('persona').value,ticket=++historyLoading;
  if(!pid){historyData=null;renderHistory();return;}
  $('history-list').innerHTML='<p class="hint">正在读取编辑快照…</p>';
  try {const data=await api('GET','history',{persona_id:pid});
    if(ticket!==historyLoading||pid!==$('persona').value)return;
    if(data.persona_id!==pid)throw new Error('快照的人设不匹配，请刷新重试。');
    historyData=data;renderHistory();
  } catch(error){if(ticket!==historyLoading||pid!==$('persona').value)return;$('history-list').innerHTML=empty('快照暂时未能读取',error.message || '请稍后刷新。');throw error;}
}
function renderHistory() {
  const entries=historyData?.entries || [];
  $('history-count').textContent=`${entries.length} / ${historyData?.limit || 20} 条`;
  $('history-list').innerHTML=entries.map(row=>`<article class="history-row"><div class="history-mark" aria-hidden="true">↶</div><div class="history-copy"><div class="card-meta"><time>${escape(formatInstant(row.at))}</time><span>${escape(row.date || '')}</span></div><h3>${escape(row.label || row.kind)}</h3><ul>${(row.changes || []).map(change=>`<li>${escape(change)}</li>`).join('')}</ul>${!row.can_restore?'<p class="hint">人设已变化，保留记录供查看。</p>':''}</div><button data-restore="${escape(row.id)}" data-restorable="${Boolean(row.can_restore)}" ${row.can_restore?'':'disabled'}>恢复这份快照</button></article>`).join('') || empty('还没有编辑快照','下一次保存编辑前会自动留存，可从这里找回之前的安排。');
  setGenerationBusy(generating);
}
async function restoreSnapshot(id) {
  requireDetailReady();const pid=$('persona').value;
  if(historyData?.persona_id!==pid||!historyData.entries.some(row=>row.id===id&&row.can_restore))throw new Error('这份快照当前不可恢复，请刷新列表。');
  if(!await askConfirm((hasPersonaDraft()?'当前未保存的日程和重复规则草稿会被放弃。':'')+'恢复会替换这个角色的生活安排，并先保存当前状态供再次恢复。聊天队列和日记保持独立。继续？'))return;
  requireDetailReady();if(pid!==$('persona').value||historyData?.persona_id!==pid)throw new Error('人设已变化，请重新选择快照。');saving=true;setGenerationBusy(generating);
  try {await api('POST','restore',{persona_id:pid,id});recurrenceData=null;recurrenceDraft=[];recurrenceDirty=false;await loadDetail();await loadHistory();if(tab==='calendar')await loadRecurrences({force:true});notice('已恢复编辑快照；恢复前状态也已留存，可在列表中找回。');}
  finally {saving=false;recoverJob();setGenerationBusy(generating);}
}
$('refresh-history').onclick=()=>action(loadHistory);
$('history-list').addEventListener('click',event=>{const button=event.target.closest('[data-restore]');if(button)action(()=>restoreSnapshot(button.dataset.restore));});
function getRows(type) {
  if (type === 'week') { detail.profile ||= {weekly:[]}; return detail.profile.weekly; }
  if (type === 'event') { detail.calendar ||= {events:[]}; return detail.calendar.events; }
  detail.day ||= {slots:[],summary:'手动日程',events:[]}; return detail.day.slots;
}
function openEditor(type, index = -1, day = null) {
  requireDetailReady();
  const row = index >= 0 ? structuredClone(getRows(type)[index]) : type === 'event' ? {start:$('date').value,end:$('date').value,kind:'activity',day_off:false} : {start:'00:00',end:'24:00',status:'free',days:[1,2,3,4,5,6,7]};
  if(type==='week'&&day)row.days=[day];
  edit = {type,index,day,selection:{...detailSelection}}; $('edit-title').textContent = type === 'event' ? '编辑活动' : day?`编辑周${'一二三四五六日'[day-1]}时间段`:'编辑时间段'; $('edit-error').textContent = '';
  $('edit-fields').innerHTML = `<label>名称<input name="title" required maxlength="160" value="${escape(row.title)}"></label>`+(type === 'event' ?
    `<div class="row2"><label>开始日期<input type="date" name="start" required value="${escape(row.start)}"></label><label>结束日期（含当天）<input type="date" name="end" required value="${escape(row.end)}"></label></div><label>活动类型<select name="kind">${Object.entries(kinds).map(([k,v])=>`<option value="${k}" ${row.kind===k?'selected':''}>${v}</option>`).join('')}</select></label><label class="check"><input name="day_off" type="checkbox" ${row.day_off?'checked':''}>当天休假，调整常规工作 / 上课</label><label>内容与日期依据<textarea name="description" rows="4">${escape(row.description)}</textarea></label><label>参考链接（每行一个 HTTPS 链接）<textarea name="source_urls" rows="2">${escape((row.source_urls||[]).join('\n'))}</textarea></label>` :
    `${type === 'week' ? `<div class="days">${[1,2,3,4,5,6,7].map(d=>`<label><input name="days" type="checkbox" value="${d}" ${row.days.includes(d)?'checked':''}>周${'一二三四五六日'[d-1]}</label>`).join('')}</div>` : ''}<div class="row2"><label>开始（HH:MM）<input name="start" required pattern="[0-2][0-9]:[0-5][0-9]" value="${escape(row.start)}"></label><label>结束（可填 24:00）<input name="end" required pattern="[0-2][0-9]:[0-5][0-9]" value="${escape(row.end)}"></label></div><label>回复状态<select name="status">${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${row.status===k?'selected':''}>${v}</option>`).join('')}</select></label><div class="row2 slow-overrides"><label>独立慢回最短（秒，选填）<input name="slow_min_seconds" type="number" min="5" max="7200" value="${escape(row.slow_min_seconds)}" placeholder="跟随默认"></label><label>独立慢回最长（秒，选填）<input name="slow_max_seconds" type="number" min="5" max="7200" value="${escape(row.slow_max_seconds)}" placeholder="跟随默认"></label></div><label class="check"><input name="locked" type="checkbox" ${row.locked?'checked':''}>锁定此安排，重新生成时保留</label><label>具体情境<textarea name="note" rows="3">${escape(row.note)}</textarea></label>`);
  if(day)$('edit-fields').querySelectorAll('[name="days"]').forEach(input=>{if(Number(input.value)!==day)input.disabled=true;});
  $('editor').showModal();
}
function readSlot(form,base={}) {
  const row={...base,title:String(form.get('title') || '').trim(),start:form.get('start'),end:form.get('end'),status:form.get('status'),note:form.get('note'),locked:form.has('locked')};
  if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(row.start || '')||!/^(([01]\d|2[0-3]):[0-5]\d|24:00)$/.test(row.end || '')||minutes(row.end)<=minutes(row.start))throw new Error('时间格式须为 HH:MM，结束晚于开始；跨午夜请拆开。');
  const min=String(form.get('slow_min_seconds') || '').trim(),max=String(form.get('slow_max_seconds') || '').trim();
  if(Boolean(min)!==Boolean(max))throw new Error('独立慢回最短和最长需同时填写，或同时留空。');
  if(min) {
    const minimum=Number(min),maximum=Number(max);if(!Number.isInteger(minimum)||!Number.isInteger(maximum)||minimum<5||minimum>7200||maximum<minimum||maximum>7200)throw new Error('慢回范围须为 5–7200 秒的整数，最长不小于最短。');
    row.slow_min_seconds=minimum;row.slow_max_seconds=maximum;
  } else {delete row.slow_min_seconds;delete row.slow_max_seconds;}
  if(edit.type==='week'){row.days=form.getAll('days').map(Number);if(!row.days.length)throw new Error('至少选择一天。');}
  return row;
}
$('edit-form').addEventListener('input',()=>{
  if(!edit||edit.type==='event')return;
  try {
    const rows=structuredClone(getRows(edit.type)),row=readSlot(new FormData($('edit-form')),rows[edit.index]);
    if(edit.index>=0&&edit.day){const others=rows[edit.index].days.filter(d=>d!==edit.day);if(others.length){rows[edit.index].days=others;rows.push(row);}else rows[edit.index]=row;}
    else if(edit.index>=0)rows[edit.index]=row;else rows.push(row);
    const errors=coverageErrors(rows,edit.type);$('edit-error').textContent=errors.length?`预览检查：${errors.slice(0,2).join('；')}。可先应用，再调整相邻时段。`:'';
  } catch(error){$('edit-error').textContent=error.message;}
});
$('edit-form').addEventListener('submit',event=>{
  event.preventDefault();
  try {requireDetailReady();if(edit.selection.personaId!==detailSelection.personaId||edit.selection.date!==detailSelection.date)throw new Error('人设或日期已变化，请关闭编辑后重试。');}catch(error){$('edit-error').textContent=error.message;return;}
  const form = new FormData(event.target), {type,index} = edit, rows = getRows(type);
  let row = index >= 0 ? {...rows[index]} : {};
  row.title = form.get('title').trim(); row.start = form.get('start'); row.end = form.get('end');
  if (row.start >= row.end && (type !== 'event' || row.start !== row.end)) { $('edit-error').textContent='结束必须晚于开始，跨午夜时段请拆开。'; return; }
  if (type === 'event') { row.kind=form.get('kind'); row.day_off=form.has('day_off'); row.description=form.get('description'); row.source_urls=form.get('source_urls').split('\n').map(s=>s.trim()).filter(Boolean); if (row.source_urls.some(u=>!u.startsWith('https://'))) { $('edit-error').textContent='参考链接须使用 HTTPS。'; return; } row.origin='manual'; }
  else {try {row=readSlot(form,row);}catch(error){$('edit-error').textContent=error.message;return;}}
  if(index>=0&&edit.day){const others=rows[index].days.filter(d=>d!==edit.day);if(others.length){rows[index].days=others;rows.push(row);}else rows[index]=row;}
  else if(index>=0) rows[index]=row; else rows.push(row);
  markDirty(type === 'week' ? 'profile' : type === 'event' ? 'calendar' : 'day'); $('editor').close(); renderDetail(); notice('已应用到预览，请点击对应的保存按钮。');
});
// Keep profile text edits when a weekly card rerenders the preview.
$('profile-form').addEventListener('input',()=>{
  if(controlsLocked()||!detailMatchesSelection())return;
  detail.profile ||= {weekly:[]};
  Object.assign(detail.profile,{world_type:$('world-type').value,world_name:$('world-name').value,
    timezone:$('persona-timezone').value,identity:$('identity').value,lifestyle:$('lifestyle').value,
    calendar_notes:$('calendar-notes').value,countries:$('countries').value.split(/[,，\s]+/).filter(Boolean).map(s=>s.toUpperCase())});
  markDirty('profile');
});
for (const id of ['close-editor','cancel-editor']) $(id).onclick=()=>$('editor').close();
document.addEventListener('click',async event=>{
  const editButton=event.target.closest('[data-edit]'), del=event.target.closest('[data-delete]'), queue=event.target.closest('[data-queue]'),split=event.target.closest('[data-split]'),boundaryButton=event.target.closest('[data-boundary]');
  if((editButton||del||split||boundaryButton)&&(controlsLocked()||!detailMatchesSelection()))return;
  if(editButton) openEditor(editButton.dataset.edit,Number(editButton.dataset.index),Number(editButton.dataset.editDay)||(editButton.dataset.edit==='week'&&/^[1-7]$/.test(weekFilter)?Number(weekFilter):null));
  if(split)openBoundary(split.dataset.split,Number(split.dataset.index),split.dataset.split==='week'&&/^[1-7]$/.test(weekFilter)?Number(weekFilter):null,true);
  if(boundaryButton)openBoundary(boundaryButton.dataset.boundary,Number(boundaryButton.dataset.index),Number(boundaryButton.dataset.day)||null);
  if(del && await askConfirm('从预览中删除这一项？保存后生效。')) { const rows=getRows(del.dataset.delete),index=Number(del.dataset.index);if(del.dataset.delete==='week'&&/^[1-7]$/.test(weekFilter)&&rows[index].days.length>1)rows[index].days=rows[index].days.filter(d=>d!==Number(weekFilter));else rows.splice(index,1);markDirty(del.dataset.delete === 'week' ? 'profile' : del.dataset.delete === 'event' ? 'calendar' : 'day'); renderDetail(); }
  if(queue) action(async()=>{ if(!await askConfirm(queue.dataset.queue==='retry'?'确认已核对平台消息？重试可能重复发送。':'清除这个对话的待回复消息？')) return; await api('POST','queue',{umo:queue.dataset.umo,action:queue.dataset.queue}); await loadState(); notice('队列已更新。'); });
});
function singleDayIndex(index,day) {
  const rows=getRows('week'),row=rows[index];if(!day||row.days.length===1)return index;
  const copy=structuredClone(row);copy.days=[day];row.days=row.days.filter(d=>d!==day);rows.push(copy);return rows.length-1;
}
function applyBoundary(type,index,day,value,split=false) {
  requireDetailReady();
  const rows=getRows(type),row=rows[index],amount=minutes(value),pair=split?null:adjacentPair(type,index,day);
  if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(value))throw new Error('请输入有效的 HH:MM。');
  if(split) {
    if(amount<=minutes(row.start)||amount>=minutes(row.end))throw new Error('拆分时间须位于这个时段内部。');
    if(type==='week'&&day)index=singleDayIndex(index,day);
    const current=rows[index],second={...structuredClone(current),start:value,title:current.title+' · 后半段'};current.end=value;rows.push(second);
  } else {
    if(!pair)throw new Error('找不到相邻时段，请先修复空档或重叠。');
    if(amount<=minutes(row.start)||amount>=minutes(rows[pair[1]].end))throw new Error('交界时间须位于这两个时段的起止时间内部。');
    let second=pair[1];if(type==='week'){index=singleDayIndex(index,day);second=singleDayIndex(second,day);}
    rows[index].end=value;rows[second].start=value;
  }
  markDirty(type==='week'?'profile':'day');renderDetail();
}
function openBoundary(type,index,day,split=false) {
  requireDetailReady();
  const row=getRows(type)[index],pair=split?null:adjacentPair(type,index,day);if(!split&&!pair)return;
  boundary={type,index,day,split,selection:{...detailSelection}};$('boundary-title').textContent=split?'拆分时间段':'调整相邻时段';
  $('boundary-context').textContent=(day?`仅修改周${'一二三四五六日'[day-1]}。`:'')+(split?`${times(row)} · ${row.title}`:`${row.title} → ${getRows(type)[pair[1]].title}，两段会共同调整，保持连续。`);
  $('boundary-time').value=split?clockText(Math.floor((minutes(row.start)+minutes(row.end))/2)):row.end;$('boundary-error').textContent='';$('boundary-dialog').showModal();
}
$('cancel-boundary').onclick=()=>$('boundary-dialog').close();
$('boundary-form').onsubmit=event=>{event.preventDefault();try {if(boundary.selection.personaId!==detailSelection?.personaId||boundary.selection.date!==detailSelection?.date)throw new Error('人设或日期已变化，请关闭编辑后重试。');applyBoundary(boundary.type,boundary.index,boundary.day,$('boundary-time').value,boundary.split);$('boundary-dialog').close();notice('已调整预览，请保存对应日程或周表。');}catch(error){$('boundary-error').textContent=error.message;}};
$('week-matrix').addEventListener('click',event=>{
  const day=event.target.closest('[data-week-day]');if(day){weekFilter=day.dataset.weekDay;renderWeekFilter();renderDetail();}
});
$('week-matrix').addEventListener('keydown',event=>{
  const handle=event.target.closest('[data-drag-boundary]');if(!handle||controlsLocked()||!detailMatchesSelection())return;
  const index=Number(handle.dataset.dragBoundary),day=Number(handle.dataset.day);
  if(event.key==='Enter'||event.key===' '){event.preventDefault();openBoundary('week',index,day);return;}
  if(!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();
  const value=minutes(getRows('week')[index].end)+(['ArrowUp','ArrowLeft'].includes(event.key)?-5:5);
  try {applyBoundary('week',index,day,clockText(value));const row=getRows('week').findIndex(r=>r.days.length===1&&r.days[0]===day&&r.end===clockText(value));$('week-matrix').querySelector(`[data-drag-boundary="${row>=0?row:index}"][data-day="${day}"]`)?.focus();}catch(error){notice(error.message,true);}
});
$('week-matrix').addEventListener('pointerdown',event=>{
  const handle=event.target.closest('[data-drag-boundary]');if(!handle||controlsLocked()||!detailMatchesSelection()||event.button!==0)return;
  event.preventDefault();handle.setPointerCapture(event.pointerId);
  const index=Number(handle.dataset.dragBoundary),day=Number(handle.dataset.day),track=handle.parentElement,rect=track.getBoundingClientRect(),pair=adjacentPair('week',index,day),rows=getRows('week');if(!pair)return;
  const initial=minutes(rows[index].end);let value=initial;handle.classList.add('dragging');
  const move=e=>{value=Math.max(minutes(rows[index].start)+1,Math.min(minutes(rows[pair[1]].end)-1,Math.round((e.clientY-rect.top)/rect.height*1440/5)*5));handle.style.top=value/14.4+'%';handle.title=clockText(value);};
  const finish=e=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',finish);handle.removeEventListener('pointercancel',cancel);handle.classList.remove('dragging');if(handle.hasPointerCapture(e.pointerId))handle.releasePointerCapture(e.pointerId);if(value===initial)return;try{applyBoundary('week',index,day,clockText(value));notice(`周${'一二三四五六日'[day-1]}边界调整至 ${clockText(value)}，请保存周表。`);}catch(error){notice(error.message,true);renderWeekMatrix();}};
  const cancel=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',finish);handle.removeEventListener('pointercancel',cancel);renderWeekMatrix();};
  handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',cancel);
});
for (const [id,type] of [['add-day','day'],['add-week','week'],['add-event','event']]) $(id).onclick=()=>openEditor(type);
function requirePersona() { if(!$('persona').value) throw new Error('请先选择人设。'); }
async function save(kind,value) {
  requirePersona();
  requireDetailReady();
  if(kind==='day'||kind==='profile'){const errors=coverageErrors(kind==='day'?value.slots || []:value.weekly || [],kind==='day'?'day':'week');if(errors.length)throw new Error('暂不能保存：'+errors.slice(0,3).join('；'));}
  const remaining = [...dirtyKinds].filter(k=>k!==kind), draft = structuredClone(detail);
  const target={...detailSelection};
  saving=true;setGenerationBusy(generating);
  try {
    await api('POST','save',{persona_id:target.personaId,date:target.date,kind,value});
    await loadDetail();
    if(detailSelection?.personaId===target.personaId&&detailSelection?.date===target.date){
      for(const pending of remaining) { detail[pending === 'profile' ? 'profile' : pending === 'calendar' ? 'calendar' : 'day'] = draft[pending]; markDirty(pending); }
      if(remaining.length)renderDetail();
    }
  } finally {saving=false;recoverJob();setGenerationBusy(generating);}
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
  requireDetailReady();
  if(scope==='diary') {
    if(!diary?.can_generate)throw new Error(diary?.reason || '请选择已结束且有日程的日期。');
    if((dirty || diary.entry)&&!await askConfirm('按这一天保存的日程和可用的白名单聊天重新写日记，成功后替换这一篇；未保存的日程修改会丢弃。继续？'))return;
  } else if((dirty || scope!=='day') && !await askConfirm(scope==='profile'?'重新分析会重建未锁定的档案、周表、活动和当天安排。已保存的锁定时段保留，未保存修改会丢弃，继续？':scope==='calendar'?'重新生成会替换全年活动及相关日程。已保存的锁定时段保留，继续？':'重新生成会覆盖未保存修改，已保存的锁定时段保留，继续？')) return;
  setGenerationBusy(true);
  try {
    const result=await api('POST','generate',{persona_id:$('persona').value,date:$('date').value,scope});
    job=result.job_id;
  } catch(error) {setGenerationBusy(false);throw error;}
  renderJob({status:'running',stage:'queued',progress:0,message:'任务已提交'}); notice(scope==='diary'?'正在回顾这一天，按角色的语气写日记。刷新页面后可恢复查看。':'正在按人设安排生活。首次生成需要分析档案和年度活动，可能需要几分钟；刷新页面后可恢复查看。');
  pollTimer=setTimeout(pollJob,1500);
}
function renderJob(result) {
  const stages={queued:'准备生成',waiting:'等待生成',profile:'分析人设',calendar:'生成活动',day:'安排当天',diary:'写生活日记',saving:'校验保存',done:'生成完成',failed:'生成失败',interrupted:'任务已中断'};
  const progress=Math.max(0,Math.min(100,Number(result.progress)||0));
  $('job-status').innerHTML=`<span>${escape(stages[result.stage] || result.stage || '正在生成')} · ${escape(result.message || '')}</span>${result.status==='running'?`<progress max="100" value="${progress}" aria-label="生成进度">${progress}%</progress>`:''}`;
}
function recoverJob() {
  if(job||detailLoading||saving)return;
  const running=state.jobs.find(j=>j.persona_id===$('persona').value&&j.status==='running');
  if(running){job=running.id || running.job_id;if(!dirty&&running.date){$('date').value=running.date;followRoleToday=false;}setGenerationBusy(true);renderJob(running);clearTimeout(pollTimer);pollTimer=setTimeout(pollJob,300);}
}
function missingJob(error) {
  return error?.code==='JOB_NOT_FOUND'||Number(error?.status)===404||/任务不存在[，,]?\s*可能插件已经重载/.test(error?.message || '');
}
async function pollJob() {
  if(!job)return;const ticket=job;
  let result;
  try {result=await api('GET','job',{id:ticket});}
  catch(error) {
    if(ticket!==job)return;
    if(missingJob(error)){
      job=null;clearTimeout(pollTimer);
      state.jobs=state.jobs.filter(j=>(j.id || j.job_id)!==ticket);
      setGenerationBusy(false);renderJob({status:'interrupted',stage:'interrupted',message:'任务记录已失效'});
      notice('任务记录已失效，可能插件已重载或记录已清理。已解除操作锁定，请查看已保存日程后按需重新生成。',true);
      try {await loadDetail();}catch(refreshError){notice('任务记录已失效，已解除操作锁定；详情刷新失败：'+(refreshError.message || '请手动刷新'),true);}
      return;
    }
    $('job-status').textContent='连接中断，正在恢复任务查询…';pollTimer=setTimeout(pollJob,5000);$('last-updated').textContent=error.message || '任务查询暂时不可用';return;
  }
  if(ticket!==job)return;renderJob(result);
  if(result.status==='running'){pollTimer=setTimeout(pollJob,1800);return;}
  job=null;clearTimeout(pollTimer);
  state.jobs=state.jobs.filter(j=>(j.id || j.job_id)!==ticket);
  setGenerationBusy(false);notice(result.message || (result.status==='failed'?'生成失败':'已完成'),result.status==='failed');
  try {await loadDetail();await loadState();}catch(error){notice('生成任务已结束；详情刷新失败：'+(error.message || '请手动刷新'),true);}
}
for (const [id,scope] of [['generate','day'],['generate-profile','profile'],['generate-calendar','calendar'],['generate-diary','diary']]) $(id).onclick=()=>action(()=>generate(scope));
$('save-settings').onclick=()=>action(async()=>{
  const scope=$('auto-schedule-scope').value;
  if(scope==='selected'&&!autoPersonas.size)throw new Error('请至少选择一个主动生成人设，或改用“所有已分析角色”。');
  if(!/^([01]\d|2[0-3]):[0-5]\d$/.test($('auto-schedule-time').value))throw new Error('请输入有效的角色当地生成时间。');
  const chatScope=$('chat-scope').value;
  const payload={enabled:$('enabled').checked,whitelist_enabled:chatScope==='whitelist',all_chats_enabled:chatScope==='all',whitelist:[...selected],schedule_provider_id:$('schedule-provider').value,reply_provider_id:$('reply-provider').value,diary_provider_id:$('diary-provider').value,diary_enabled:$('diary-enabled').checked,diary_chat_enabled:$('diary-chat-enabled').checked,diary_limit:Number($('diary-limit').value),auto_schedule_enabled:$('auto-schedule-enabled').checked,auto_schedule_time:$('auto-schedule-time').value,auto_schedule_personas:scope==='selected'?[...autoPersonas]:[],canvas_integration_enabled:$('canvas-integration-enabled').checked,adaptive_enabled:$('adaptive-enabled').checked,transition_notice_enabled:$('transition-notice-enabled').checked,transition_notice_minutes:Number($('transition-notice-minutes').value),adaptive_max_delay_minutes:Number($('adaptive-max-delay-minutes').value),active_conversation_minutes:Number($('active-conversation-minutes').value),adaptive_review_interval_seconds:Number($('adaptive-review-interval-seconds').value),timezone:$('default-timezone').value,slow_min_seconds:Number($('slow-min').value),slow_max_seconds:Number($('slow-max').value),queue_limit:Number($('queue-limit').value),queue_expire_hours:Number($('queue-expire').value),search_enabled:$('search-enabled').checked};
  if($('clear-key').checked) payload.tavily_api_key=''; else if($('tavily-key').value.trim()) payload.tavily_api_key=$('tavily-key').value.trim();
  await api('POST','settings',payload); await loadState(true); notice(chatScope==='whitelist'&&!selected.size?'设置已保存。白名单为空，当前不会控制任何聊天。':chatScope==='none'?'设置已保存。当前不控制聊天，日程和日记仍可生成。':'设置已保存。');
});
$('refresh-models').onclick=()=>action(async()=>{const button=$('refresh-models');button.disabled=true;try {const result=await api('GET','providers');state.providers=result.providers || [];renderProviders(true);notice('已从 AstrBot 获取模型列表。当前未保存的模型选择仍保留。');}finally {button.disabled=false;}});
$('session-list').addEventListener('change',event=>{if(event.target.dataset.umo) { const u=event.target.dataset.umo; event.target.checked?selected.add(u):selected.delete(u); renderChatScope(); }});
$('session-search').oninput=renderSessions;
$('chat-scope').onchange=renderChatScope;
$('diary-chat-enabled').onchange=renderChatScope;
$('auto-schedule-scope').onchange=renderAutoPersonas;
$('auto-persona-list').addEventListener('change',event=>{const id=event.target.dataset.autoPersona;if(id){event.target.checked?autoPersonas.add(id):autoPersonas.delete(id);$('auto-persona-count').textContent=`已选 ${autoPersonas.size} 个`;}});
$('settings').addEventListener('input',event=>{if(event.target.matches('input,select,textarea')&&event.target.id!=='session-search'){dirtySettings=true;renderDraftStatus();}});
$('settings').addEventListener('change',event=>{if(event.target.matches('input,select,textarea')&&event.target.id!=='session-search'){dirtySettings=true;renderDraftStatus();}});
$('add-umo').onclick=()=>action(async()=>{const u=$('manual-umo').value.trim();if(u.split(':').length<3)throw new Error('请输入完整会话来源 ID。');selected.add(u);dirtySettings=true;renderDraftStatus();$('manual-umo').value='';renderSessions();notice('已加入选择，保存设置后生效。');});
$('refresh').onclick=()=>action(async()=>{if((hasPersonaDraft()||dirtySettings)&&!await askConfirm('刷新会放弃未保存的修改，继续？'))return;await loadState(true);await loadDetail();if(recurrenceDirty)await loadRecurrences({force:true});notice('已刷新。');});
$('refresh-queues').onclick=()=>action(()=>loadState());
let priorPersona='',priorDate='';
async function chooseDate(value, openDay = false, {followToday=false} = {}) {
  if(controlsLocked()) return;
  if(value<'1970-01-01'||value>'2100-12-31') return;
  if(dirty&&!await askConfirm('切换会放弃未保存的修改，继续？')) return;
  $('date').value=value;
  if(!await loadDetail())return;followRoleToday=followToday;priorPersona=$('persona').value;priorDate=$('date').value;
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
$('jump-today').onclick=()=>action(()=>chooseDate(localToday(),false,{followToday:true}));
for(const [id,delta] of [['month-prev',-1],['month-next',1]]) $(id).onclick=()=>{const date=dateObject(calendarMonth+'-01');date.setUTCMonth(date.getUTCMonth()+delta);calendarMonth=isoDate(date).slice(0,7);renderMonth();};
function renderWeekFilter() {document.querySelectorAll('[data-week-filter]').forEach(b=>{const active=b.dataset.weekFilter===weekFilter;b.classList.toggle('active',active);b.setAttribute('aria-pressed',active);});}
$('week-filter').addEventListener('click',event=>{const button=event.target.closest('[data-week-filter]');if(!button)return;weekFilter=button.dataset.weekFilter;renderWeekFilter();renderDetail();});
for (const id of ['persona','date']) $(id).onchange=()=>action(async()=>{if(controlsLocked()){restoreDetailSelection();return;}if((dirty||(id==='persona'&&recurrenceDirty))&&!await askConfirm('切换会放弃未保存的修改，继续？')){restoreDetailSelection();return;}if(!await loadDetail({followToday:id==='persona'&&followRoleToday}))return;if(id==='date')followRoleToday=false;priorPersona=$('persona').value;priorDate=$('date').value;});
const liveTimer=setInterval(()=>{if(state&&!document.hidden)updateLiveStatus();},1000);
const stateTimer=setInterval(()=>{if(state)refreshLive();},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state)refreshLive();});
window.addEventListener('beforeunload',event=>{if(hasPersonaDraft()||dirtySettings){event.preventDefault();event.returnValue='';}});
window.addEventListener('pagehide',()=>{clearTimeout(pollTimer);clearInterval(liveTimer);clearInterval(stateTimer);tabTransition.cancel();});
async function init() {
  $('date').value=todayInTimezone();
  if(!bridge){notice('请从 AstrBot 管理面板 → 插件 → 角色日程 → 可视化视图打开本页面。',true);document.querySelectorAll('button').forEach(b=>b.disabled=true);return;}
  await bridge.ready();await loadState(true);await loadDetail({followToday:followRoleToday});priorPersona=$('persona').value;priorDate=$('date').value;
}
action(init);
