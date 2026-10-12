"""Local QA only: a fixture bridge around the real plugin handlers, no bot account."""
import asyncio
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_schedule import FakeConfig, FakeContext, PersonaSchedule, ROOT, UMO, profile
from aiohttp import web


async def main():
    ctx = FakeContext()
    fixtures = {
        "星野澪": "日本东京的高中生星野澪，周末喜欢和朋友去街机厅，生活在日本，不庆祝中国中秋节。",
        "林澄": "中国杭州的自由插画师林澄，喜欢茶和桂花，中秋节会回家和家人团聚。",
        "艾莉亚": "自创世界银湾城的巡林员艾莉亚，精灵，在霜月祭为古树祈福，无现实国家和学校。",
    }
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
    for pid in fixtures:
        persona=await plugin._persona(pid)
        p=profile(["JP"] if pid=="星野澪" else ["CN"] if pid=="林澄" else [],"original" if pid=="艾莉亚" else "real")
        p.update(identity="高中生" if pid=="星野澪" else "自由插画师" if pid=="林澄" else "精灵巡林员",world_name="银湾城" if pid=="艾莉亚" else "现实世界",timezone="Asia/Tokyo" if pid=="星野澪" else "Asia/Shanghai",calendar_notes="遵循人设文化与世界观，活动日期可修改。")
        if pid=="星野澪":p["weekly"]=weekly
        events=[{"id":"fixture-festival","title":"秋日文化祭" if pid=="星野澪" else "桂花市集" if pid=="林澄" else "霜月祭", "start":day,"end":day,"kind":"activity","day_off":False,"description":"界面测试示例活动，可修改日期、参加方式与休假状态。","origin":"generated","source_urls":[]}]
        from astrbot_plugin_persona_schedule.core import weekly_for_day
        plugin.store.data["personas"][pid]={"key":persona["key"],"profile":p,"calendars":{day[:4]:{"year":int(day[:4]),"events":events,"notes":["本页面使用演示数据，仅用于界面验证。"],"revision":1}},"days":{day:{"date":day,"summary":"按自己的节奏生活，把想说的话留到有空的时候。","slots":weekly_for_day(p,day),"events":events,"origin":"generated","revision":1}}}
    plugin.store.save()
    bridge = '''<script>window.AstrBotPluginView={ready:async()=>({isDark:false}),apiGet:async(path,query)=>{const r=await fetch('/qa-api/'+path+'?'+new URLSearchParams(query||{}));const d=await r.json();if(!r.ok)throw Error(d.message);return d},apiPost:async(path,body)=>{const r=await fetch('/qa-api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.message);return d}};</script>'''
    async def index(request):
        html=(ROOT/'views/schedule/index.html').read_text(encoding='utf-8').replace('<head>','<head>'+bridge)
        # QA-only theme override; production themes are supplied by AstrBot.
        if request.query.get('theme') in {'light','dark'}:
            html=html.replace('<html lang="zh-CN">','<html lang="zh-CN" data-theme="'+request.query['theme']+'">')
        return web.Response(text=html,content_type='text/html')
    async def asset(request):
        name=request.match_info['name']
        if name not in {'app.js','style.css','vendor/rhinelab/content-transition.js'}:raise web.HTTPNotFound()
        return web.FileResponse(ROOT/'views/schedule'/name)
    handlers={"state":plugin.web_state,"detail":plugin.web_detail,"settings":plugin.web_settings,"generate":plugin.web_generate,"job":plugin.web_job,"save":plugin.web_save,"queue":plugin.web_queue}
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
