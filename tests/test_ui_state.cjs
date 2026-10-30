// Exercise the shipped UI state machine with a minimal DOM/bridge, no browser or bot.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../pages/schedule/app.js'), 'utf8')
  .replace(/^import[^\n]*\n/, '').replace(/action\(init\);\s*$/, '');
const date = '2026-10-28';
const fullDay = [{start:'00:00',end:'24:00',status:'free',title:'A 的安排'}];
const detailFor = (personaId, day = date, timezone = 'Asia/Tokyo') => ({
  persona_id:personaId,edit_token:'version-'+personaId,profile:{timezone,weekly:[]},calendar:{events:[]},
  day:{date:day,slots:structuredClone(fullDay),summary:personaId+' 的一天'},
});
const baseState = () => ({settings:{timezone:'Asia/Hong_Kong'},
  personas:['A','B','C'].map(id=>({id,ready:true})),jobs:[],providers:[],queues:[],sessions:[],logs:[]});
const deferred = () => {let resolve,reject;const promise = new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

function harness(handler = async (method, resource, data) => {
  if(resource==='state')return baseState();
  if(resource==='detail')return detailFor(data.persona_id,data.date);
  return {};
}) {
  const elements = new Map(), timers = new Map(), calls = [];
  let timerId = 0;
  const element = id => {
    if(!elements.has(id))elements.set(id, {
      id,value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,dataset:{},style:{},
      listeners:new Map(),classList:{toggle(){},add(){},remove(){}},
      addEventListener(type,fn){this.listeners.set(type,[...(this.listeners.get(type)||[]),fn]);},
      removeEventListener(){},setAttribute(){},querySelectorAll(){return [];},querySelector(){return null;},
      showModal(){this.open=true;},close(){this.open=false;},focus(){},matches(){return false;},
    });
    return elements.get(id);
  };
  const document = {
    hidden:false,getElementById:element,addEventListener(){},querySelector:element,
    querySelectorAll(selector){return selector.split(',').filter(s=>/^#[\w-]+$/.test(s)).map(s=>element(s.slice(1)));},
  };
  const bridge = {
    ready:async()=>{},
    apiGet:(resource,data)=>{calls.push({method:'GET',resource,data});return handler('GET',resource,data);},
    apiPost:(resource,data)=>{calls.push({method:'POST',resource,data});return handler('POST',resource,data);},
  };
  class FixedDate extends Date {
    constructor(...args){super(...(args.length?args:['2026-10-28T15:30:00Z']));}
    static now(){return new Date('2026-10-28T15:30:00Z').getTime();}
  }
  const context = vm.createContext({
    document,window:{AstrBotPluginPage:bridge,addEventListener(){},matchMedia:()=>({matches:false})},
    ContentTransition:class {cancel(){} reveal(){}},Date:FixedDate,Intl,structuredClone,console,
    setInterval:()=>0,clearInterval(){},
    setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},
    clearTimeout(id){timers.delete(id);},
  });
  vm.runInContext(source+`\n
    renderDetail=()=>setGenerationBusy(generating);
    renderSettings=()=>{};renderAutoPersonas=()=>{};renderStateStatus=()=>{};
    renderProviders=()=>{};renderDiary=()=>{};
    globalThis.ui={loadDetail,save,openEditor,applyBoundary,pollJob,recoverJob,init,chooseDate,
      requireDetailReady,controlsLocked,detailMatchesSelection,runPreview,loadHistory,restoreSnapshot,
      loadRecurrences,saveRecurrences,openRecurrence,applyRecurrence,renderChatScope,
      postEdit,compareConflict,reloadAfterConflict,readAdditionalSettings,incidentPreviewPayload,
      loadIncidents,endIncident,refreshEffectiveDay,previewImport,applyImport,readImportFile,
      displayDay,requireDayEditing,normalizeDetail,renderBackupSettings,renderRuntimeMetrics,renderRecovery,updateLiveStatus};
  `,context);
  const run = code => vm.runInContext(code,context);
  context.seedDetail=detailFor('A');context.seedState=baseState();
  run(`state=seedState;detail=seedDetail;detailSelection={personaId:'A',date:'${date}'};
       $('persona').value='A';$('date').value='${date}';priorPersona='A';priorDate='${date}';`);
  const snapshot = () => JSON.parse(run(`JSON.stringify({detail,detailSelection,detailLoading,dirty,
    dirtyKinds:[...dirtyKinds],job,generating,followRoleToday,state,recurrenceData,recurrenceDraft,recurrenceDirty,historyData,previewData,previewBusy,
    conflictData,importPreview,incidentData,showActualSchedule,dirtyBackupSettings,
    persona:$('persona').value,date:$('date').value,notice:$('notice').textContent})`));
  return {ui:context.ui,run,snapshot,calls,timers,element,context};
}

test('failed persona switch rolls selectors back and preserves the previous unsaved draft', async()=>{
  const h=harness(async()=>{throw new Error('detail unavailable');});
  h.run("dirty=true;dirtyKinds.add('day');detail.day.summary='未保存的 A';$('persona').value='B';");
  await assert.rejects(h.ui.loadDetail(),/detail unavailable/);
  const s=h.snapshot();
  assert.equal(s.persona,'A');assert.equal(s.date,date);assert.equal(s.detail.day.summary,'未保存的 A');
  assert.equal(s.dirty,true);assert.deepEqual(s.dirtyKinds,['day']);assert.equal(s.detailLoading,false);
  assert.equal(h.ui.detailMatchesSelection(),true);
});

test('pending detail blocks editing and saving the old persona', async()=>{
  const response=deferred(),h=harness(()=>response.promise);
  h.run("$('persona').value='B';");
  const loading=h.ui.loadDetail();
  assert.equal(h.ui.controlsLocked(),true);assert.equal(h.element('save-day').disabled,true);
  assert.throws(()=>h.ui.openEditor('day',0),/加载|稍后/);
  assert.throws(()=>h.ui.applyBoundary('day',0,null,'12:00',true),/加载|稍后/);
  await assert.rejects(h.ui.save('day',detailFor('A').day),/加载|稍后/);
  assert.equal(h.calls.filter(c=>c.method==='POST').length,0);
  response.resolve(detailFor('B'));await loading;
  assert.equal(h.snapshot().detail.persona_id,'B');assert.equal(h.ui.controlsLocked(),false);
});

test('a selector/detail identity mismatch never submits a save', async()=>{
  const h=harness();h.run("$('persona').value='B';");
  await assert.rejects(h.ui.save('day',detailFor('A').day),/详情尚未加载/);
  assert.throws(()=>h.ui.openEditor('day',0),/详情尚未加载/);
  assert.equal(h.calls.length,0);
});

for(const [label,response] of [
  ['persona',detailFor('C')],['date',detailFor('B','2026-10-27')],
])test(`a mismatched ${label} in a detail response is rejected before draft replacement`,async()=>{
  const h=harness(async()=>response);
  h.run("dirty=true;dirtyKinds.add('day');$('persona').value='B';");
  await assert.rejects(h.ui.loadDetail(),/详情与所选人设或日期不一致/);
  assert.equal(h.snapshot().persona,'A');assert.equal(h.snapshot().detail.persona_id,'A');
  assert.equal(h.snapshot().dirty,true);
});

for(const lockedBy of ['pending detail','selection mismatch']) {
  for(const type of ['keydown','pointerdown'])test(`weekly ${type} cannot mutate drafts during ${lockedBy}`,()=>{
    const h=harness();h.run(lockedBy==='pending detail'?'detailLoading=true;':"$('persona').value='B';");
    const before=h.snapshot().detail,handle={dataset:{dragBoundary:'0',day:'1'}};
    let prevented=false;
    const event={target:{closest:()=>handle},key:'ArrowDown',button:0,preventDefault(){prevented=true;}};
    for(const listener of h.element('week-matrix').listeners.get(type)||[])listener(event);
    assert.equal(prevented,false);assert.deepEqual(h.snapshot().detail,before);
  });
}

test('a pending save locks selection and rejects duplicate edits and submissions',async()=>{
  const post=deferred(),h=harness(async(method,resource,data)=>{
    if(method==='POST')return post.promise;
    if(resource==='detail')return detailFor(data.persona_id,data.date);
  });
  const saving=h.ui.save('day',detailFor('A').day);
  assert.equal(h.ui.controlsLocked(),true);assert.equal(h.element('persona').disabled,true);
  await h.ui.chooseDate('2026-09-17');assert.equal(h.snapshot().date,date);
  assert.throws(()=>h.ui.openEditor('day',0),/加载|生成|保存|稍后/);
  await assert.rejects(h.ui.save('day',detailFor('A').day),/加载|生成|保存|稍后/);
  assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
  assert.equal(h.calls[0].data.persona_id,'A');assert.equal(h.calls[0].data.date,date);
  post.resolve({ok:true});await saving;
  assert.equal(h.ui.controlsLocked(),false);assert.equal(h.snapshot().detail.persona_id,'A');
});

test('a failed save unlocks the page while preserving unsaved drafts',async()=>{
  const h=harness(async()=>{throw new Error('save unavailable');});
  h.run("dirty=true;dirtyKinds.add('day');detail.day.summary='未保存的内容';");
  await assert.rejects(h.ui.save('day',detailFor('A').day),/save unavailable/);
  assert.equal(h.ui.controlsLocked(),false);assert.equal(h.snapshot().dirty,true);
  assert.equal(h.snapshot().detail.day.summary,'未保存的内容');
});

test('an older response cannot overwrite or unlock the newer pending persona', async()=>{
  const b=deferred(),c=deferred(),h=harness((_,__,data)=>data.persona_id==='B'?b.promise:c.promise);
  h.run("$('persona').value='B';");const first=h.ui.loadDetail();
  h.run("$('persona').value='C';");const second=h.ui.loadDetail();
  b.resolve(detailFor('B'));await first;
  assert.equal(h.snapshot().detail.persona_id,'A');assert.equal(h.snapshot().detailLoading,true);
  c.resolve(detailFor('C'));await second;
  assert.equal(h.snapshot().detail.persona_id,'C');assert.equal(h.snapshot().detailLoading,false);
});

test('a late failed response cannot roll back a newer successful selection', async()=>{
  const b=deferred(),h=harness((_,__,data)=>data.persona_id==='B'?b.promise:Promise.resolve(detailFor('C')));
  h.run("$('persona').value='B';");const first=h.ui.loadDetail();
  h.run("$('persona').value='C';");await h.ui.loadDetail();
  b.reject(new Error('old request failed'));await first;
  assert.equal(h.snapshot().persona,'C');assert.equal(h.snapshot().detail.persona_id,'C');
});

test('the initial page follows the persona date across a timezone midnight', async()=>{
  const h=harness();await h.ui.init();
  assert.equal(h.snapshot().date,'2026-10-29');
  assert.equal(h.snapshot().detail.day.date,'2026-10-29');
});

test('an explicitly selected historical date stays historical', async()=>{
  const h=harness();await h.ui.chooseDate('2026-09-17');
  assert.equal(h.snapshot().date,'2026-09-17');assert.equal(h.snapshot().detail.day.date,'2026-09-17');
});

test('switching persona in today mode follows the new persona timezone',async()=>{
  const h=harness(async(_,resource,data)=>resource==='state'?baseState():detailFor(data.persona_id,data.date,data.persona_id==='B'?'America/Los_Angeles':'Asia/Tokyo'));
  await h.ui.init();assert.equal(h.snapshot().date,'2026-10-29');
  h.element('persona').value='B';await h.element('persona').onchange();
  assert.equal(h.snapshot().date,'2026-10-28');assert.equal(h.snapshot().detail.persona_id,'B');
});

test('switching persona keeps a deliberately selected historical date',async()=>{
  const h=harness();await h.ui.init();await h.ui.chooseDate('2026-09-17');
  h.element('persona').value='B';await h.element('persona').onchange();
  assert.equal(h.snapshot().date,'2026-09-17');assert.equal(h.snapshot().detail.persona_id,'B');
});

test('following persona today does not replace a restored running job date', async()=>{
  const h=harness();h.run(`job='running';generating=true;state.jobs=[{id:'running',persona_id:'A',date:'2026-09-17',status:'running'}];$('date').value='2026-09-17';`);
  await h.ui.loadDetail({followToday:true});
  assert.equal(h.snapshot().date,'2026-09-17');assert.equal(h.snapshot().job,'running');
});

for(const [label,error] of [
  ['legacy bridge text',new Error('任务不存在，可能插件已经重载')],
  ['machine code',Object.assign(new Error('gone'),{code:'JOB_NOT_FOUND'})],
  ['HTTP 404',Object.assign(new Error('not found'),{status:404})],
])test(`a missing generation job unlocks the page and cannot recover itself (${label})`,async()=>{
  const h=harness(async(_,resource,data)=>{
    if(resource==='job')throw error;
    if(resource==='detail')return detailFor(data.persona_id,data.date);
    if(resource==='state')return baseState();
  });
  h.run("job='old';generating=true;state.jobs=[{id:'old',persona_id:'A',date:'2026-10-28',status:'running'}];");
  await h.ui.pollJob();h.ui.recoverJob();
  const s=h.snapshot();assert.equal(s.job,null);assert.equal(s.generating,false);
  assert.equal(s.state.jobs.some(j=>(j.id||j.job_id)==='old'&&j.status==='running'),false);
  assert.equal(h.ui.controlsLocked(),false);
});

test('temporary job transport failures preserve the active job and schedule a retry', async()=>{
  const h=harness(async()=>{throw new Error('network unavailable');});
  h.run("job='active';generating=true;");await h.ui.pollJob();
  assert.equal(h.snapshot().job,'active');assert.equal(h.snapshot().generating,true);
  assert.equal([...h.timers.values()].some(t=>t.delay===5000),true);
});

test('an unrelated HTTP 400 is not misclassified as a missing job', async()=>{
  const h=harness(async()=>{throw Object.assign(new Error('temporary gateway failure'),{status:400});});
  h.run("job='active';generating=true;");await h.ui.pollJob();
  assert.equal(h.snapshot().job,'active');assert.equal(h.snapshot().generating,true);
});

test('a completed job stays completed when refreshing the resulting detail fails', async()=>{
  const h=harness(async(_,resource)=>{
    if(resource==='job')return {id:'complete',status:'done',stage:'done',progress:100};
    throw new Error('detail refresh failed');
  });
  h.run("job='complete';generating=true;state.jobs=[{id:'complete',persona_id:'A',status:'running'}];");
  await h.ui.pollJob();h.ui.recoverJob();
  assert.equal(h.snapshot().job,null);assert.equal(h.snapshot().generating,false);
  assert.equal([...h.timers.values()].some(t=>t.delay===5000),false);
});

const previewFor = (personaId='A',day=date) => ({persona_id:personaId,date:day,time:'21:00',timezone:'Asia/Tokyo',
  read_only:true,status:'busy',scope:{allowed:true,reason:'白名单'},would_queue:true,
  base_activity:{title:'睡觉',start:'21:00',end:'24:00'},effective_activity:{title:'睡觉'},model_evaluated:false});
const recurringRule = {id:'annual-1',title:'霜月祭',kind:'festival',calendar:'fictional',month:11,day:15,
  duration_days:1,leap_policy:'skip',mapping:'霜月暂映射为公历11月',enabled:true};

test('a basic preview sends only a read-only request and leaves saved detail and queues untouched', async()=>{
  const h=harness(async(_,resource)=>{assert.equal(resource,'preview');return previewFor();});
  h.element('preview-time').value='21:00';h.element('preview-message').value='重要的话';
  h.element('preview-session').value='adapter:FriendMessage:1';h.element('preview-active').checked=true;
  const original=h.snapshot();await h.ui.runPreview();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].data.evaluate,false);
  assert.equal(h.calls[0].data.active_conversation,true);assert.equal(h.calls[0].data.message,'重要的话');
  assert.deepEqual(h.snapshot().detail,original.detail);assert.deepEqual(h.snapshot().state,original.state);
  assert.match(h.element('preview-result').innerHTML,/静默暂存/);assert.equal(h.snapshot().previewBusy,false);
});

test('model preview is an explicit choice and is never submitted twice while pending', async()=>{
  const response=deferred(),h=harness(()=>response.promise);h.element('preview-time').value='21:00';
  const pending=h.ui.runPreview(true);await h.ui.runPreview(true);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].data.evaluate,true);
  response.resolve({...previewFor(),model_evaluated:true});await pending;
  assert.match(h.element('preview-result').innerHTML,/已取得日程模型/);
});

test('a preview response cannot leak into a newly selected persona', async()=>{
  const response=deferred(),h=harness(async(_,resource,data)=>resource==='preview'?response.promise:detailFor(data.persona_id,data.date));
  h.element('preview-time').value='21:00';const pending=h.ui.runPreview();
  h.element('persona').value='B';await h.ui.loadDetail();response.resolve(previewFor());await pending;
  assert.equal(h.snapshot().previewData,null);assert.equal(h.snapshot().previewBusy,false);
  assert.doesNotMatch(h.element('preview-result').innerHTML,/静默暂存/);
});

test('preview explains that excluded conversations do not inherit busy silence', async()=>{
  const h=harness(async()=>({...previewFor(),scope:{allowed:false},would_queue:false}));
  h.element('preview-time').value='21:00';await h.ui.runPreview();
  assert.match(h.element('preview-result').innerHTML,/当前会话不受日程拦截/);
  assert.doesNotMatch(h.element('preview-result').innerHTML,/静默暂存/);
});

test('a failed preview clears the old successful result and releases its buttons', async()=>{
  const h=harness(async()=>{throw new Error('未保存这一天');});
  h.element('preview-time').value='21:00';h.context.oldPreview=previewFor();h.run('previewData=oldPreview;');
  await assert.rejects(h.ui.runPreview(),/未保存/);
  assert.equal(h.snapshot().previewData,null);assert.equal(h.snapshot().previewBusy,false);
  assert.match(h.element('preview-result').innerHTML,/未保存这一天/);
});

test('recurrence draft stays with its persona and survives ordinary same-persona detail reload', async()=>{
  const h=harness();h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[rule]};recurrenceDraft=[rule];recurrenceDirty=true;");
  await h.ui.loadDetail();assert.equal(h.snapshot().recurrenceDirty,true);
  h.element('persona').value='B';await h.ui.loadDetail();
  assert.equal(h.snapshot().recurrenceDirty,false);assert.deepEqual(h.snapshot().recurrenceDraft,[]);
});

test('loading recurrence rules never overwrites an existing local rule draft', async()=>{
  const h=harness();h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[]};recurrenceDraft=[rule];recurrenceDirty=true;");
  await h.ui.loadRecurrences();assert.equal(h.calls.length,0);
  assert.equal(h.snapshot().recurrenceDraft[0].title,'霜月祭');
});

test('fictional recurrence requires a mapping and invalid dates never enter the draft', ()=>{
  const h=harness();h.run("recurrenceData={persona_id:'A',rules:[]};");h.ui.openRecurrence();
  h.element('rule-title').value='霜月祭';h.element('rule-calendar').value='fictional';h.element('rule-month').value='11';h.element('rule-day').value='15';
  assert.throws(()=>h.ui.applyRecurrence(),/映射/);assert.equal(h.snapshot().recurrenceDraft.length,0);
  h.element('rule-mapping').value='映射到公历';h.element('rule-month').value='4';h.element('rule-day').value='31';
  assert.throws(()=>h.ui.applyRecurrence(),/有效的月份和日期/);
  h.element('rule-day').value='30';h.ui.applyRecurrence();
  assert.equal(h.snapshot().recurrenceDraft.length,1);assert.equal(h.snapshot().recurrenceDirty,true);
  assert.equal(h.calls.length,0);
});

test('saving recurring rules preserves unrelated unsaved day edits', async()=>{
  const h=harness(async(_,resource,data)=>{
    if(resource==='recurrences-save')return {saved:true};
    if(resource==='recurrences')return {persona_id:'A',rules:[recurringRule]};
    if(resource==='detail')return detailFor(data.persona_id,data.date);
  });
  h.context.rule=recurringRule;h.run("recurrenceData={persona_id:'A',rules:[]};recurrenceDraft=[rule];recurrenceDirty=true;dirty=true;dirtyKinds.add('day');detail.day.summary='手工日程草稿';askConfirm=async()=>true;");
  await h.ui.saveRecurrences();const s=h.snapshot();
  assert.equal(s.recurrenceDirty,false);assert.equal(s.dirty,true);assert.equal(s.detail.day.summary,'手工日程草稿');
  assert.equal(h.calls.filter(c=>c.method==='POST').length,1);assert.equal(h.calls[0].resource,'recurrences-save');
});

test('a failed recurring save keeps the draft and unlocks the page', async()=>{
  const h=harness(async()=>{throw new Error('save failed');});h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[]};recurrenceDraft=[rule];recurrenceDirty=true;askConfirm=async()=>true;");
  await assert.rejects(h.ui.saveRecurrences(),/save failed/);
  assert.equal(h.snapshot().recurrenceDirty,true);assert.equal(h.ui.controlsLocked(),false);
});

test('history responses from a previous persona are ignored', async()=>{
  const response=deferred(),h=harness(()=>response.promise);const pending=h.ui.loadHistory();
  h.element('persona').value='B';response.resolve({persona_id:'A',entries:[{id:'old',can_restore:true}]});await pending;
  assert.equal(h.snapshot().historyData,null);
});

test('restoring a snapshot requires confirmation and keeps all drafts if declined', async()=>{
  const h=harness();h.run("historyData={persona_id:'A',entries:[{id:'old',can_restore:true}]};dirty=true;recurrenceDirty=true;askConfirm=async()=>false;");
  await h.ui.restoreSnapshot('old');assert.equal(h.calls.length,0);
  assert.equal(h.snapshot().dirty,true);assert.equal(h.snapshot().recurrenceDirty,true);
});

test('snapshot restore failure preserves existing day and recurring drafts', async()=>{
  const h=harness(async()=>{throw new Error('restore failed');});
  h.run("historyData={persona_id:'A',entries:[{id:'old',can_restore:true}]};dirty=true;recurrenceDirty=true;askConfirm=async()=>true;");
  await assert.rejects(h.ui.restoreSnapshot('old'),/restore failed/);
  assert.equal(h.snapshot().dirty,true);assert.equal(h.snapshot().recurrenceDirty,true);assert.equal(h.ui.controlsLocked(),false);
});

test('diary whitelist remains explicitly visible when controlling every conversation', ()=>{
  const h=harness();h.element('chat-scope').value='all';h.element('diary-chat-enabled').checked=true;
  h.ui.renderChatScope();assert.equal(h.element('whitelist-selection').hidden,false);
  assert.match(h.element('diary-whitelist-status').textContent,/白名单为空/);
  h.element('diary-chat-enabled').checked=false;h.ui.renderChatScope();assert.equal(h.element('whitelist-selection').hidden,true);
});

test('an in-flight recurring refresh blocks edits until its saved rules are loaded', async()=>{
  const response=deferred(),h=harness(()=>response.promise);
  h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[rule]};recurrenceDraft=[rule];");
  const pending=h.ui.loadRecurrences({force:true});
  assert.equal(h.element('add-recurrence').disabled,true);
  assert.equal(h.element('save-recurrences').disabled,true);
  assert.throws(()=>h.ui.openRecurrence(0),/读取|加载|稍后/);
  await assert.rejects(h.ui.saveRecurrences(),/读取|加载|稍后/);
  assert.equal(h.calls.filter(c=>c.method==='POST').length,0);
  response.resolve({persona_id:'A',rules:[{...recurringRule,title:'已保存的规则'}]});await pending;
  assert.equal(h.element('add-recurrence').disabled,false);
  h.ui.openRecurrence(0);assert.equal(h.element('rule-title').value,'已保存的规则');
});

test('a failed recurring refresh preserves the current rules and unlocks editing', async()=>{
  const h=harness(async()=>{throw new Error('rules unavailable');});h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[rule]};recurrenceDraft=[rule];");
  await assert.rejects(h.ui.loadRecurrences({force:true}),/rules unavailable/);
  assert.equal(h.element('add-recurrence').disabled,false);
  assert.equal(h.snapshot().recurrenceDraft[0].title,recurringRule.title);
});

test('reentering the calendar cannot cancel a confirmed refresh of a dirty rule draft', async()=>{
  const response=deferred(),h=harness(()=>response.promise);h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[rule]};recurrenceDraft=[rule];recurrenceDirty=true;");
  const pending=h.ui.loadRecurrences({force:true});await h.ui.loadRecurrences();
  response.resolve({persona_id:'A',rules:[{...recurringRule,title:'重新读取的规则'}]});await pending;
  assert.equal(h.snapshot().recurrenceDirty,false);
  assert.equal(h.snapshot().recurrenceDraft[0].title,'重新读取的规则');
  assert.equal(h.element('add-recurrence').disabled,false);
});

test('reloading recurring rules does not replace a rule currently open in the editor', async()=>{
  const h=harness();h.context.rule=recurringRule;
  h.run("recurrenceData={persona_id:'A',rules:[rule]};recurrenceDraft=[rule];");h.ui.openRecurrence(0);
  h.element('rule-title').value='编辑中的规则';
  await h.ui.loadRecurrences();
  assert.equal(h.calls.length,0);
  assert.equal(h.element('rule-title').value,'编辑中的规则');
});

test('refreshing the same saved day invalidates a previous preview result', async()=>{
  const h=harness();h.context.oldPreview=previewFor();h.run('previewData=oldPreview;');
  await h.ui.loadDetail();
  assert.equal(h.snapshot().previewData,null);
  assert.doesNotMatch(h.element('preview-result').innerHTML,/静默暂存/);
});

test('an auxiliary diary error cannot discard unrelated drafts after a successful save', async()=>{
  const h=harness(async(method,resource,data)=>{
    if(method==='POST')return {saved:true};
    if(resource==='detail')return detailFor(data.persona_id,data.date);
    if(resource==='diary')throw new Error('diary unavailable');
  });
  h.run("tab='diary';dirty=true;dirtyKinds.add('calendar');dirtyKinds.add('profile');detail.profile.identity='尚未保存的身份';");
  await h.ui.save('calendar',[]);
  assert.equal(h.snapshot().detail.profile.identity,'尚未保存的身份');
  assert.equal(h.snapshot().dirty,true);assert.deepEqual(h.snapshot().dirtyKinds,['profile']);
  assert.equal(h.ui.controlsLocked(),false);
});

test('saving submits the version loaded with the draft',async()=>{
  const h=harness(async(method,resource,data)=>method==='POST'?{saved:true}:resource==='detail'?detailFor(data.persona_id,data.date):{});
  await h.ui.save('day',detailFor('A').day);
  assert.equal(h.calls.find(c=>c.resource==='save').data.expected_token,'version-A');
});

test('edit conflict and comparison keep unsaved detail and recurrence drafts intact',async()=>{
  const fresh={...detailFor('A'),edit_token:'new-version'};fresh.day.summary='另一页面的安排';
  const h=harness(async(method,resource)=>{
    if(method==='POST')throw Object.assign(new Error('编辑冲突：资料已更新'),{status:409});
    return resource==='detail'?fresh:{persona_id:'A',rules:[{title:'另一页面的纪念日'}],edit_token:'new-version'};
  });
  h.run("dirty=true;dirtyKinds.add('day');detail.day.summary='本地日程草稿';recurrenceDirty=true;recurrenceDraft=[{title:'本地规则草稿'}];");
  await assert.rejects(h.ui.save('day',h.snapshot().detail.day),/编辑冲突/);
  await h.ui.compareConflict();
  const s=h.snapshot();assert.equal(s.detail.day.summary,'本地日程草稿');assert.equal(s.detail.edit_token,'version-A');
  assert.equal(s.recurrenceDraft[0].title,'本地规则草稿');assert.equal(s.conflictData.latest.edit_token,'new-version');
  assert.match(h.element('conflict-comparison').innerHTML,/本地日程草稿/);assert.match(h.element('conflict-comparison').innerHTML,/另一页面的安排/);
  h.run('askConfirm=async()=>false;');const count=h.calls.length;await h.ui.reloadAfterConflict();assert.equal(h.calls.length,count);assert.equal(h.snapshot().dirty,true);
});

test('a recurring draft uses its own original version after an unrelated detail reload',async()=>{
  const h=harness(async()=>{throw new Error('stop after payload');});h.context.rule=recurringRule;
  h.run("askConfirm=async()=>true;recurrenceData={persona_id:'A',edit_token:'rules-old',rules:[rule]};recurrenceDraft=[rule];recurrenceDirty=true;detail.edit_token='detail-new';");
  await assert.rejects(h.ui.saveRecurrences(),/stop after payload/);
  assert.equal(h.calls[0].data.expected_token,'rules-old');assert.equal(h.snapshot().recurrenceDirty,true);
});

test('incident settings turn percentages into finite unit probabilities',()=>{
  const h=harness();h.element('background-call-limit').value='0';h.element('diary-archive-delay').value='5';h.element('incident-enabled').checked=false;
  for(const [value,expected] of [['20',.2],['0',0],['100',1],['12.5',.125]]){
    h.element('incident-probability').value=value;const settings=h.ui.readAdditionalSettings();assert.equal(settings.incident_probability,expected);assert.equal(settings.incident_enabled,false);
  }
  for(const value of ['', 'NaN','Infinity','-1','101']){h.element('incident-probability').value=value;assert.throws(()=>h.ui.readAdditionalSettings(),/概率/);}
});

test('simulated incident is opt-in and only reaches the read-only preview endpoint',async()=>{
  const h=harness(async()=>previewFor());const before=h.snapshot().detail;
  h.element('preview-time').value='14:20';h.element('preview-incident-title').value='临时接待访客';h.element('preview-incident-status').value='busy';h.element('preview-incident-duration').value='30';
  await h.ui.runPreview(false);assert.equal(h.calls[0].data.simulate_incident,undefined);
  h.element('preview-incident-enabled').checked=true;await h.ui.runPreview(false);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[1].data.simulate_incident)),{title:'临时接待访客',status:'busy',duration_minutes:30});
  assert.ok(h.calls.every(c=>c.resource==='preview'));assert.deepEqual(h.snapshot().detail,before);
  h.element('preview-incident-duration').value='61';await assert.rejects(h.ui.runPreview(false),/15|60|分钟/);assert.equal(h.calls.length,2);
});

test('incident responses for an old selection are ignored',async()=>{
  const pending=deferred(),h=harness(()=>pending.promise);const running=h.ui.loadIncidents();
  h.run("$('persona').value='B';");pending.resolve({persona_id:'A',entry:{id:'old'}});await running;
  assert.equal(h.snapshot().incidentData,null);
});

test('ending an incident requires confirmation and submits an edit version',async()=>{
  const h=harness(async()=>{throw new Error('cancel test request');});
  h.run("incidentData={persona_id:'A',entry:{id:'incident-A',status:'active'}};askConfirm=async()=>false;");
  await h.ui.endIncident('incident-A');assert.equal(h.calls.length,0);
  h.run('askConfirm=async()=>true;');await assert.rejects(h.ui.endIncident('incident-A'),/cancel test request/);
  assert.equal(h.calls[0].resource,'incident-end');assert.equal(h.calls[0].data.expected_token,'version-A');assert.equal(h.ui.controlsLocked(),false);
});

test('effective schedule refresh preserves draft versions and distinguishes base from actual events',async()=>{
  const response=detailFor('A');response.edit_token='changed';response.base_day=structuredClone(response.day);response.day={...response.day,slots:[{...fullDay[0],title:'突发插曲',incident_id:'one'}]};
  const h=harness(async()=>response);h.run("dirty=true;dirtyKinds.add('day');detail.day.summary='未保存安排';");
  await h.ui.refreshEffectiveDay();assert.equal(h.snapshot().detail.day.summary,'未保存安排');assert.equal(h.snapshot().detail.edit_token,'version-A');
  assert.equal(h.snapshot().detail.effective_day.slots[0].incident_id,'one');
  h.run('dirty=false;dirtyKinds.clear();');await h.ui.refreshEffectiveDay();assert.equal(h.snapshot().detail.edit_token,'changed');
  assert.equal(h.ui.displayDay().slots[0].title,'突发插曲');assert.throws(()=>h.ui.requireDayEditing(),/基础安排/);
  h.run('showActualSchedule=false;');assert.doesNotThrow(()=>h.ui.requireDayEditing());assert.equal(h.ui.displayDay().slots[0].title,'A 的安排');
});

test('backup restore is previewed first and cancelled confirmation sends nothing',async()=>{
  const h=harness(async()=>({preview_token:'preview-1',expected_token:'preview-version',changes:['更新 1 天安排'],summary:{days:1}}));
  await h.ui.previewImport('backup-1');assert.equal(h.calls.length,1);assert.equal(h.calls[0].resource,'import-preview');assert.equal(h.calls[0].data.backup_id,'backup-1');
  h.run('askConfirm=async()=>false;');await h.ui.applyImport();assert.equal(h.calls.length,1);assert.equal(h.snapshot().importPreview.preview_token,'preview-1');
});

test('confirmed restore uses the preview version even if a later refresh changed detail',async()=>{
  const h=harness(async(method,resource)=>{if(resource==='import-preview')return {preview_token:'preview-1',expected_token:'preview-version'};throw new Error('编辑冲突：资料已更新');});
  await h.ui.previewImport('backup-1');h.run("askConfirm=async()=>true;detail.edit_token='newer';dirty=true;dirtyKinds.add('day');detail.day.summary='保留草稿';");
  await assert.rejects(h.ui.applyImport(),/编辑冲突/);
  assert.equal(h.calls[1].data.expected_token,'preview-version');assert.equal(h.snapshot().detail.day.summary,'保留草稿');assert.equal(h.snapshot().dirty,true);
});

test('old import preview cannot replace a newly selected file or persona',async()=>{
  for(const change of ['file','persona']){
    const pending=deferred(),h=harness(()=>pending.promise);const running=h.ui.previewImport('backup-1');
    if(change==='file')await h.ui.readImportFile({name:'fresh.json',size:2,text:async()=>'{}'});else h.run("$('persona').value='B';");
    pending.resolve({preview_token:'stale'});await running;assert.equal(h.snapshot().importPreview,null);
  }
});

test('backup settings draft survives ordinary settings rendering',()=>{
  const h=harness();h.run("dirtyBackupSettings=true;$('backup-limit').value='12';");h.ui.renderBackupSettings();assert.equal(h.element('backup-limit').value,'12');
});

test('runtime usage does not invent token counts when provider omitted usage',()=>{
  const h=harness();h.run("state.model_runtime={today:{purposes:{schedule:{calls:2,success:2}}}};state.storage_metrics={bytes:2048,last_ms:1.5,p95_ms:2};");
  h.ui.renderRuntimeMetrics();assert.match(h.element('model-runtime-list').innerHTML,/未提供 token 用量/);assert.doesNotMatch(h.element('model-runtime-list').innerHTML,/输入 0/);
  assert.match(h.element('storage-metrics').innerHTML,/2.0 KiB/);
});

test('late effective refresh cannot roll back a newer schedule or an ongoing save',async()=>{
  const first=deferred(),second=deferred();let calls=0;
  const h=harness(()=>++calls===1?first.promise:second.promise);
  const a=h.ui.refreshEffectiveDay(),b=h.ui.refreshEffectiveDay();
  second.resolve({...detailFor('A'),edit_token:'newest'});await b;
  first.resolve({...detailFor('A'),edit_token:'stale'});await a;assert.equal(h.snapshot().detail.edit_token,'newest');
  const pending=deferred(),g=harness(()=>pending.promise),refresh=g.ui.refreshEffectiveDay();g.run('saving=true;');
  pending.resolve({...detailFor('A'),edit_token:'while-saving'});await refresh;assert.equal(g.snapshot().detail.edit_token,'version-A');
});

test('editing original slots keeps the live status tied to the actual incident',()=>{
  const h=harness();h.run(`detail.profile.timezone='Asia/Hong_Kong';showActualSchedule=false;
    detail.effective_day={date:'${date}',slots:[{start:'00:00',end:'24:00',status:'busy',title:'正在处理突发事情'}]};`);
  h.ui.updateLiveStatus();assert.equal(h.element('current-title').textContent,'正在处理突发事情');assert.equal(h.element('current-state').textContent,'忙碌');
  assert.equal(h.ui.displayDay().slots[0].status,'free');
});

test('recovery protection is visible on data and diagnostics without enabling or replaying anything',()=>{
  const h=harness();h.run("state.storage_metrics={recovery:{at:1793320000,backup_id:'backup-fixture.json',requires_review:true}};backupData={backups:[{id:'backup-fixture.json',at:1793310000}]};");
  h.ui.renderRuntimeMetrics();
  for(const id of ['backup-recovery','diagnostic-recovery']){
    assert.equal(h.element(id).hidden,false);assert.match(h.element(id).innerHTML,/已从备份恢复，请先核对/);
    assert.match(h.element(id).innerHTML,/恢复时间/);assert.match(h.element(id).innerHTML,/来源/);
    assert.match(h.element(id).innerHTML,/暂停插件控制/);assert.match(h.element(id).innerHTML,/旧队列已暂停/);
  }
  assert.equal(h.calls.length,0);
});

test('confirmed recovery becomes an informational record and absent recovery stays hidden',()=>{
  const h=harness();h.run("state.storage_metrics={recovery:{at:1793320000,backup_id:'<fixture>',requires_review:false}};");
  h.ui.renderRecovery();assert.match(h.element('backup-recovery').innerHTML,/已确认/);
  assert.doesNotMatch(h.element('backup-recovery').innerHTML,/请先核对|暂停插件控制/);
  assert.match(h.element('backup-recovery').innerHTML,/&lt;fixture&gt;/);
  h.run('state.storage_metrics={};');h.ui.renderRecovery();
  assert.equal(h.element('backup-recovery').hidden,true);assert.equal(h.element('diagnostic-recovery').hidden,true);
});
