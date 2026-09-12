import asyncio, json, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
if '--live' not in sys.argv:
 raise SystemExit('Pass --live to run a real Codex turn in a disposable repository.')
from aiohttp import web
from aiohttp.test_utils import TestServer
from pytest import MonkeyPatch
from playwright.async_api import async_playwright
from test_band_management import portal
from test_work_sessions import until
from rook.worker.plugins.proc import ProcPlugin
from rook.remote.work_web import WorkWeb

class LocalBand:
 def __init__(self):
  self.proc=ProcPlugin()
  self.workers={'local':dict(worker_id='local',name='cachyrig',band='test',last_seen=time.time(),caps=['proc.start','proc.read','proc.write'])}
 async def call(self,cap,args,target,timeout):
  result=await getattr(self.proc,'_'+cap.split('.')[1])(**args)
  return dict(ok=True,**{'from':target},result=result)

async def main():
 with tempfile.TemporaryDirectory(prefix='rook-work-live-') as temp:
  path=Path(temp);workspace=path/'repo';workspace.mkdir()
  process=await asyncio.create_subprocess_exec('git','init','-q',str(workspace));await process.wait()
  p=portal.__wrapped__(path,MonkeyPatch());band=LocalBand();p.server._band=band
  async def index(r):return web.Response(text=(ROOT/'rook/web/index.html').read_text(),content_type='text/html')
  async def empty(r):return web.json_response([])
  p.app.router.add_get('/',index)
  for route in ['/api/bands','/api/band/workers','/api/avatars','/api/presence','/api/chat/rooms']:
   p.app.router.add_get(route,empty)
  async with TestServer(p.app) as server,async_playwright() as pw:
   url=str(server.make_url('/'));p.account.origin=url.rstrip('/')
   browser=await pw.chromium.launch(headless=True)
   ctx=await browser.new_context(viewport={'width':1440,'height':1000})
   await ctx.add_cookies([{'name':'rook_account','value':p.headers['Cookie'].split('=',1)[1],'url':url}])
   page=await ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   await page.goto(url+'#work')
   await page.locator('#work-create input[name=title]').fill('Live Work verification')
   await page.locator('#work-create input[name=cwd]').fill(str(workspace))
   await page.locator('#work-create button[type=submit]').click()
   work=p.account.work_web
   await until(lambda:len(work.store.all())>0,20)
   sid=work.store.all()[0]['id']
   try:
    await until(lambda:work.store.get(sid)['status']=='ready',60)
   except Exception:
    print(json.dumps(work.store.get(sid)),flush=True);raise
   print('READY model='+work.store.get(sid)['model'],flush=True)
   await page.locator('#work-input').fill('This is an integration test in a disposable repository. Use your file editing tool to create smoke.txt containing exactly rook work smoke followed by a newline. Then reply WORK_SMOKE_OK. Do not do anything else.')
   await page.locator('#work-compose button').click()
   await until(lambda:work.store.get(sid)['status'] in ('working','sending'),20)
   await page.close()
   print('Browser closed during active turn',flush=True)
   async def wait_done():
    async with asyncio.timeout(120):
     while True:
      s=work.store.get(sid)
      if s['pending']:
       print('PENDING '+json.dumps(s['pending']),flush=True)
       for key,req in list(s['pending'].items()):
        if req['method'] in ('item/fileChange/requestApproval','item/commandExecution/requestApproval'):
         await work.command(sid,dict(op='answer',id='test-approval-'+key,request=key,decision='accept'))
      if s['status']=='ready' and s['order']:return
      if s['status'] in ('error','uncertain','closed'):
       print(json.dumps(s),flush=True);raise RuntimeError(s['error'])
      await asyncio.sleep(.2)
   await wait_done()
   print('COMPLETED '+json.dumps({'status':work.store.get(sid)['status'],'error':work.store.get(sid)['error'],'items':[i['type'] for i in work.store.get(sid)['items'].values()]}),flush=True)
   assert (workspace/'smoke.txt').read_text()=='rook work smoke\n'
   page=await ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
   await page.goto(url+'#work')
   await page.locator('[data-session="'+sid+'"]').click()
   await page.get_by_text('WORK_SMOKE_OK',exact=True).wait_for(timeout=15000)
   await page.screenshot(path='/tmp/rook-work-desktop.png')
   await page.locator('[data-pane=changes]').click()
   assert 'rook work smoke' in await page.locator('#work-diff').inner_text()
   await page.set_viewport_size({'width':390,'height':844})
   await page.screenshot(path='/tmp/rook-work-mobile.png')
   assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
   await page.locator('#work-close').click()
   await until(lambda:work.store.get(sid)['status']=='closed',20)
   await page.locator('#work-resume').click()
   await until(lambda:work.store.get(sid)['status']=='ready',40)
   print('REOPENED saved Codex thread '+work.store.get(sid)['thread_id'],flush=True)
   await work.command(sid,dict(op='close',id='test-cleanup-close'))
   await browser.close()
   assert not errors,errors
   print('PASS: real Codex file edit, server-owned run while page closed, restored history/diff, desktop/mobile, reopen.',flush=True)
  await band.proc.stop()
asyncio.run(main())
