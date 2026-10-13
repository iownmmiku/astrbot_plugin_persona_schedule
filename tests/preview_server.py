"""Local QA only: a fixture bridge around the real plugin handlers, no bot account."""
import asyncio
import copy
import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_schedule import FakeConfig, FakeContext, PersonaSchedule, ROOT, UMO, profile
from aiohttp import web


async def main():
    ctx = FakeContext()
    providers = [types.SimpleNamespace(meta=lambda pid=pid: types.SimpleNamespace(id=pid, type="chat_completion"),
                 get_model=lambda model=model: model)
                 for pid, model in [("creative-plan", "schedule-model"), ("character-chat", "reply-model"), ("memory-journal", "diary-model")]]
    ctx.get_all_providers = lambda: providers
    ctx.get_provider_by_id = lambda pid: next((p for p in providers if p.meta().id == pid), None)
    original_generate = ctx.llm_generate
    async def slow_fixture_generate(**kwargs):
        # Make refresh/reopen recovery observable during local UI QA.
        await asyncio.sleep(2)
        if "第一人称生活日记" in kwargs["system_prompt"]:
            ctx.calls.append(kwargs)
            payload = json.loads(kwargs["prompt"])
            pid = next((name for name in diary_templates if name in payload["persona"]), "星野澪")
            return types.SimpleNamespace(completion_text=json.dumps(fixture_diary(pid, payload["saved_day"]), ensure_ascii=False))
        return await original_generate(**kwargs)
    ctx.llm_generate = slow_fixture_generate
    fixtures = {
        "星野澪": "日本东京的高中生星野澪，周末喜欢和朋友去街机厅，生活在日本，不庆祝中国中秋节。",
        "林澄": "中国杭州的自由插画师林澄，喜欢茶和桂花，中秋节会回家和家人团聚。",
        "艾莉亚": "自创世界银湾城的巡林员艾莉亚，精灵，在霜月祭为古树祈福，无现实国家和学校。",
        "未央": "自创世界浮岛书库的管理员未央，喜欢在晨钟响起时整理书架，没有现实国家。",
    }
    diary_templates = {
        "星野澪": {"title": "晚风把今天翻了过去", "mood": "有点累，也很满足", "content": "教室窗边的光，下午一直落在笔记本的角上。上课时把手机放在书包里，等到午饭才有空慢慢看。平常的一天，还是有一些小事想记住。\n\n放学经过便利店，挑了最熟悉的那款饮料。路上不急着走，听一会儿喜欢的歌，再想想晚上要做的作业。\n\n现在房间安静下来了。把课本放好，明天要带的东西也检查过，终于可以安心结束今天。生活如果有存档键，大概就是此刻吧。"},
        "林澄": {"title": "把桂花的颜色留在纸上", "mood": "轻松而专注", "content": "今天的画纸上，留下了几种很柔和的颜色。我照着平时的节奏工作，累了就停下来喝口茶，让眼睛看看窗外。\n\n比起急着画完，我更喜欢把一个细节慢慢调到满意。这样的日子看起来没有大事，却也有自己的分量。\n\n晚上收拾好桌面时，心里很踏实。还有没完成的部分，留给明天的光线再看。今天就到这里，给自己留一点安静。"},
        "艾莉亚": {"title": "银湾的树影与归途", "mood": "安宁", "content": "银湾的路依然熟悉。今天循着平日的安排巡视，也给自己留了休息的片刻。每次抬头看见树梢，都会觉得这座城有它缓慢的呼吸。\n\n不必把每一天都写成冒险。有时候，按时走过该走的路，平安回到住处，就已经值得记下。\n\n夜深了，我把随身的东西放好。窗外的风声很轻，明天的事情留待天明。愿今晚的树木，也做一个温柔的梦。"},
    }
    def fixture_diary(pid, schedule):
        value = copy.deepcopy(diary_templates[pid])
        if pid == "星野澪" and not any(slot["title"] == "上课" for slot in schedule["slots"]):
            value.update(title="把周末的声音收好", mood="慢悠悠的满足", content="今天醒来后没有急着起床，让自己在被窝里多待了一会儿。把早饭和一点作业慢慢解决，出门的时候已经不早了。\n\n街机厅和便利店，都是很熟悉的去处。在外面没怎么顾上看手机，回到家才觉得腿有一点累。这样的周末不需要很特别，也能让心情轻起来。\n\n晚上把剩下的事情收拾好，听了一会儿音乐。今天就像一张随手拍下的照片，热闹过后，留下一点很柔软的颜色。")
        return value
    ctx.persona_manager.item = {"persona_id": "星野澪", "system_prompt": fixtures["星野澪"]}
    async def get_persona(pid):
        return {"persona_id": pid, "system_prompt": fixtures[pid]}
    async def get_all():
        return [await get_persona(pid) for pid in fixtures]
    ctx.persona_manager.get_persona = get_persona
    ctx.persona_manager.get_all_personas = get_all
    root = ROOT / ".qa" / "preview-state"
    with patch('astrbot_plugin_persona_schedule.main.StarTools.get_data_dir', return_value=root):
        plugin = PersonaSchedule(ctx, FakeConfig())
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    def row(start,end,title,state,note="",days=None):
        value={"start":start,"end":end,"title":title,"status":state,"note":note}
        if days: value["days"]=days
        return value
    weekdays=[1,2,3,4,5];weekend=[6,7];every=list(range(1,8))
    weekly=[row("00:00","06:50","睡觉","busy","手机静音，早上醒来再看。",every),
        row("06:50","07:30","起床、洗漱、早饭","slow","",weekdays),
        row("06:50","09:30","赖床","slow","醒了但不想起，慢慢摸手机。",weekend),
        row("07:30","08:00","去学校路上","slow","",weekdays),
        row("08:00","11:40","上课","busy","手机在书包里，静音。",weekdays),
        row("09:30","11:30","早饭、写一点作业","slow","",weekend),
        row("11:30","17:30","出门（街机厅 / 便利店 / 找朋友）","slow","在外面，回得慢、话短。",weekend),
        row("11:40","13:20","午饭和同学在教室","free","",weekdays),
        row("13:20","16:40","上课","busy","手机在书包里，静音。",weekdays),
        row("16:40","18:00","放学、便利店、回家路上","free","",weekdays),
        row("17:30","19:00","回家、晚饭","slow","",weekend),
        row("18:00","19:00","晚饭、收拾房间","slow","",weekdays),
        row("19:00","21:00","作业和自己的时间","slow","间隙看看手机。",every),
        row("21:00","22:30","听音乐、聊天","free","终于可以慢慢聊了。",every),
        row("22:30","24:00","睡觉","busy","明天再看消息。",every)]
    # Reset fixture journals on server startup. Reloading the browser preserves
    # generated jobs and entries, while restarting the QA server restores cases.
    plugin.store.data["diaries"] = {}
    for pid in diary_templates:
        persona=await plugin._persona(pid)
        p=profile(["JP"] if pid=="星野澪" else ["CN"] if pid=="林澄" else [],"original" if pid=="艾莉亚" else "real")
        p.update(identity="高中生" if pid=="星野澪" else "自由插画师" if pid=="林澄" else "精灵巡林员",world_name="银湾城" if pid=="艾莉亚" else "现实世界",timezone="Asia/Tokyo" if pid=="星野澪" else "Asia/Shanghai",calendar_notes="遵循人设文化与世界观，活动日期可修改。")
        if pid=="星野澪":
            p["weekly"]=weekly
        elif pid=="林澄":
            p["weekly"]=[row("00:00","08:00","睡觉","busy","让眼睛和画笔一起休息。",every),row("08:00","09:00","早餐、泡茶","slow","",every),row("09:00","12:00","插画创作","slow","专注画细节，间隙看消息。",every),row("12:00","14:00","午饭和休息","free","",every),row("14:00","17:00","接稿、修改色彩","slow","",every),row("17:00","19:00","散步、买菜、晚饭","slow","在外面回得慢。",every),row("19:00","22:00","喝茶、整理画桌、聊天","free","",every),row("22:00","24:00","睡觉","busy","明天再继续。",every)]
        else:
            p["weekly"]=[row("00:00","05:00","树屋休息","busy","",every),row("05:00","07:00","晨间巡视","busy","在林间不便联系。",every),row("07:00","09:00","早餐、整理装备","slow","",every),row("09:00","12:00","沿古树巡林","busy","留意林中动静。",every),row("12:00","14:00","林下休息","free","",every),row("14:00","17:00","修护巡林路标","slow","偶尔看消息。",every),row("17:00","19:00","归城、晚饭","slow","",every),row("19:00","21:30","树屋记手札、闲谈","free","",every),row("21:30","24:00","休息","busy","",every)]
        events=[{"id":"fixture-festival","title":"秋日文化祭" if pid=="星野澪" else "桂花市集" if pid=="林澄" else "霜月祭", "start":day,"end":day,"kind":"activity","day_off":False,"description":"界面测试示例活动，可修改日期、参加方式与休假状态。","origin":"generated","source_urls":[]}]
        from astrbot_plugin_persona_schedule.core import weekly_for_day
        plans = {}
        today = datetime.now(ZoneInfo(p["timezone"])).date()
        for offset in range(4):
            date = (today - timedelta(days=offset)).isoformat()
            date_events = copy.deepcopy(events) if offset == 0 else []
            for event in date_events:
                event["start"] = event["end"] = date
            plans[date] = {"date":date,"timezone":p["timezone"],"summary":"按自己的节奏生活，把想说的话留到有空的时候。","slots":weekly_for_day(p,date),"events":date_events,"origin":"generated","revision":1}
        plugin.store.data["personas"][pid]={"key":persona["key"],"profile":p,"calendars":{day[:4]:{"year":int(day[:4]),"events":events,"notes":["本页面使用演示数据，仅用于界面验证。"],"revision":1}},"days":plans}
        # Two archived entries plus yesterday's saved plan without an entry:
        # history, source details, manual write and job recovery can all be tested.
        entries = plugin.store.data["diaries"][pid] = {}
        for offset in (2,3):
            date = (today - timedelta(days=offset)).isoformat()
            timestamp = datetime.fromisoformat(date+"T23:59:00").replace(tzinfo=ZoneInfo(p["timezone"])).timestamp()+120
            value = fixture_diary(pid, plans[date])
            if offset == 3:
                value["title"] = {"星野澪":"把平凡的一天夹进书里","林澄":"茶凉之前的几笔","艾莉亚":"旧路与新的晨光"}[pid]
                value["mood"] = "平静"
            entries[date] = {**value,"date":date,"timezone":p["timezone"],"persona_key":persona["key"],"created_at":timestamp,"updated_at":timestamp,"revision":1,"source":{"profile":{key:value for key,value in p.items() if key!="weekly"},"schedule":copy.deepcopy(plans[date])}}
    plugin.store.data["personas"].pop("未央", None)
    plugin.store.save()
    bridge = '''<script>window.AstrBotPluginView={ready:async()=>({isDark:false}),apiGet:async(path,query)=>{const r=await fetch('/qa-api/'+path+'?'+new URLSearchParams(query||{}));const d=await r.json();if(!r.ok)throw Error(d.message);return d},apiPost:async(path,body)=>{const r=await fetch('/qa-api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.message);return d}};</script>'''
    async def index(request):
        html=(ROOT/'pages/schedule/index.html').read_text(encoding='utf-8').replace('<head>','<head>'+bridge)
        # QA-only theme override; production themes are supplied by AstrBot.
        if request.query.get('theme') in {'light','dark'}:
            html=html.replace('<html lang="zh-CN">','<html lang="zh-CN" data-theme="'+request.query['theme']+'">')
        return web.Response(text=html,content_type='text/html')
    async def asset(request):
        name=request.match_info['name']
        if name not in {'app.js','style.css','vendor/rhinelab/content-transition.js'}:raise web.HTTPNotFound()
        return web.FileResponse(ROOT/'pages/schedule'/name)
    handlers={"state":plugin.web_state,"detail":plugin.web_detail,"settings":plugin.web_settings,"generate":plugin.web_generate,"job":plugin.web_job,"save":plugin.web_save,"queue":plugin.web_queue,"providers":plugin.web_providers,"diagnostics":plugin.web_diagnostics,"diary":plugin.web_diary}
    async def api(request):
        try:
            payload=await request.json() if request.method=='POST' else dict(request.query)
            value=await handlers[request.match_info['name']](payload)
            return web.json_response(value)
        except Exception as exc:
            return web.json_response({'message':str(exc)},status=400)
    app=web.Application();app.router.add_get('/',index);app.router.add_route('*','/qa-api/{name}',api);app.router.add_get('/{name:.+}',asset)
    runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'127.0.0.1',8769).start()
    print('QA preview: http://127.0.0.1:8769 (fixture data only)',flush=True)
    await asyncio.Event().wait()


if __name__=='__main__':asyncio.run(main())
