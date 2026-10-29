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
  persona_id:personaId,profile:{timezone,weekly:[]},calendar:{events:[]},
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
      requireDetailReady,controlsLocked,detailMatchesSelection};
  `,context);
  const run = code => vm.runInContext(code,context);
  context.seedDetail=detailFor('A');context.seedState=baseState();
  run(`state=seedState;detail=seedDetail;detailSelection={personaId:'A',date:'${date}'};
       $('persona').value='A';$('date').value='${date}';priorPersona='A';priorDate='${date}';`);
  const snapshot = () => JSON.parse(run(`JSON.stringify({detail,detailSelection,detailLoading,dirty,
    dirtyKinds:[...dirtyKinds],job,generating,followRoleToday,state,
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
