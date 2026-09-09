"""Optional real-browser dashboard checks; run from the repository root.

Requires Playwright and its Chromium, or CHROMIUM_EXECUTABLE. All mutations use
a temporary local account/token store. No production services are contacted.
"""
import asyncio,sys,tempfile,time,io,os
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_band_management import portal,add_worker
from pytest import MonkeyPatch
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from starlette.applications import Starlette
import httpx
from PIL import Image
from rook.band_mcp.account_tokens import build_account_token_routes
from rook.band_mcp.tokens import TokenStore
from rook.band_mcp.chat_rooms import ChatStore

async def main():
 with tempfile.TemporaryDirectory() as temp:
  p=portal.__wrapped__(Path(temp),MonkeyPatch());add_worker(p)
  roster=[{'worker_id':name,'name':name,'caps':['info.host','shell.exec','screenshot.capture','worker.enrollment_move_prepare','worker.description_set','files.directory.list']+(['device.info'] if name=='tablet' else []),'plugins':[],'band':p.source['psk_hash'][:8],'version':'120.steady.iguana','last_seen_age_secs':2} for name in ('worker1','tablet','windows','mac')]
  roster[1]['hb']={'battery':{'percent':100,'charging':True}}
  async def index(r):return web.Response(text=Path('rook/web/index.html').read_text(),content_type='text/html')
  async def bands(r):return web.json_response([{'id':b['psk_hash'][:8],'name':b['name'],'primary':b.get('primary',False)} for b in p.store.bands(p.uid,configs=True)])
  async def workers(r):return web.json_response(roster)
  async def call(r):
   d=await r.json();wid=d['worker_id']
   if d['cap']=='worker.description_set':
    value=' '.join(d['args']['description'].split());next(w for w in roster if w['worker_id']==wid)['description']=value
    return web.json_response({'ok':True,'result':{'ok':True,'description':value,'announced':True}})
   value={'android_release':'11','model':'Test tablet','screen':{'w':1280,'h':800,'dpi':213}} if wid=='tablet' else {'system':{'worker1':'Linux','windows':'Windows','mac':'Darwin'}[wid],'machine':'x86_64'}
   return web.json_response({'ok':True,'result':value})
  chat=ChatStore(str(Path(temp)/'chat.db'));provider=TokenStore(persist_path=str(Path(temp)/'tokens.json'))
  token_app=Starlette(routes=build_account_token_routes(provider,chat,accounts=p.store))
  http=httpx.AsyncClient(transport=httpx.ASGITransport(app=token_app),base_url='http://mcp.test')
  async def proxy(r):
   response=await http.request(r.method,'/tokens/account-api',headers={'Cookie':r.headers.get('Cookie','')},content=await r.read())
   return web.Response(body=response.content,status=response.status_code,content_type='application/json')
  api_app=web.Application();api_app.router.add_route('*','/tokens/account-api',proxy)
  async def avatar(r):
   result=chat.get_avatar(r.query['id']);return web.Response(body=result[1],content_type=result[0]) if result else web.Response(status=404)
  p.app.router.add_get('/',index);p.app.router.add_get('/api/bands',bands);p.app.router.add_get('/api/band/workers',workers);p.app.router.add_post('/api/band/call',call);p.app.router.add_get('/api/avatar',avatar)
  async with TestServer(api_app) as upstream:
   for route in p.app.router.routes():
    if route.resource.canonical=='/account/tokens/api':route.handler.__self__.url=str(upstream.make_url('/tokens/account-api'))
   async with TestServer(p.app) as server,async_playwright() as pw:
    browser=await pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_EXECUTABLE'),headless=True)
    ctx=await browser.new_context(viewport={'width':1440,'height':1100});url=str(server.make_url('/'));p.account.origin=url.rstrip('/')
    await ctx.add_cookies([{'name':'rook_account','value':p.headers['Cookie'].split('=',1)[1],'url':url}])
    page=await ctx.new_page();page.set_default_timeout(10000);errors=[];page.on('pageerror',lambda e:(errors.append(str(e)),print('JS error',str(e),flush=True)))
    await page.goto(url);await page.wait_for_timeout(1200);await page.screenshot(path='/tmp/rook-new-debug.png');await page.wait_for_function("[...document.querySelectorAll('.worker-group-heading')].some(e=>e.textContent.includes('Android'))")
    assert await page.locator('.worker-group-heading').count()==4
    assert await page.locator('.device-icon').count()==4
    await page.locator('[data-id=worker1] .worker-menu-button').click();assert await page.locator('#worker-menu button').count()==5
    await page.locator('#worker-menu').get_by_text('Move to band…',exact=True).click();await page.locator('#band-dialog').wait_for(state='visible');await page.locator('#cancel-dialog').click()
    await page.locator('#tab-workers').click()
    await page.locator('[data-id=worker1] .worker-menu-button').click();await page.locator('#worker-menu').get_by_text('Edit description…',exact=True).click()
    await page.locator('.description-dialog textarea').fill('CI <build> host & package signing');await page.locator('.description-dialog [type=submit]').click();await page.locator('.description-dialog').wait_for(state='detached')
    assert await page.locator('[data-id=worker1] .worker-description').inner_text()=='CI <build> host & package signing'
    await page.locator('#filter').click();await page.locator('#filter').fill('package signing');assert await page.locator('.worker-menu-button').count()==1
    await page.locator('#filter').fill('')
    await page.locator('#worker-group').select_option('band');assert await page.locator('.worker-group-heading').count()==1
    await page.locator('#worker-sort').select_option('os');await page.screenshot(path='/tmp/rook-new-workers.png')
    await page.locator('.view-toggle [data-layout=grid]').click()
    assert await page.locator('#view-workers').get_attribute('data-layout')=='grid'
    assert await page.locator('.worker-group').count()==1
    assert await page.locator('.worker-group-items .item').count()==4
    await page.locator('[data-id=worker1] .caret').click()
    assert await page.locator('[data-id=worker1] .caret').get_attribute('aria-expanded')=='true'
    root=page.locator('[data-id=worker1] .cggrid')
    await root.locator('summary').filter(has_text='files').click()
    await root.locator('summary').filter(has_text='directory').focus()
    await page.keyboard.press('Enter')
    await root.locator('[data-cap="files.directory.list"]').wait_for(state='visible')
    await page.evaluate("window.savedCapModal=openCapModal;openCapModal=(wid,cap)=>window.capTest=[wid,cap]")
    await root.locator('[data-cap="files.directory.list"]').click()
    assert await page.evaluate('capTest')==['worker1','files.directory.list']
    await page.evaluate('openCapModal=window.savedCapModal;dirty=true;reconcile()')
    assert await root.locator('[data-cap="files.directory.list"]').is_visible()
    await page.screenshot(path='/tmp/rook-new-grid.png')
    await page.locator('#worker-group').select_option('os')
    assert await page.locator('.worker-group').count()==4
    await page.locator('#worker-group').select_option('none')
    assert await page.locator('.worker-group-heading').count()==0
    await page.reload();await page.locator('.view-toggle [data-layout=grid][aria-pressed=true]').wait_for()
    await page.locator('#worker-group').select_option('band')
    await page.evaluate("document.querySelector('#view-workers').style.minHeight='2200px';window.scrollTo(0,600)")
    await page.wait_for_timeout(100)
    assert abs((await page.locator('.topbar').bounding_box())['y'])<1
    assert (await page.locator('#worker-stats').bounding_box())['y']>=0
    await page.evaluate("document.querySelector('#view-workers').style.minHeight='';window.scrollTo(0,0)")
    await page.locator('.view-toggle [data-layout=list]').click()

    await page.locator('#tab-install').click();await page.locator('#rook-art[data-mode=webgl] canvas').wait_for(state='visible')
    assert await page.locator('#view-install img').count()==0
    await page.locator('#art-toggle').click();assert await page.locator('#rook-art').get_attribute('data-motion')=='paused'
    await page.locator('#art-toggle').click();assert await page.locator('#rook-art').get_attribute('data-motion')=='on'
    await page.screenshot(path='/tmp/rook-new-install.png')
    await page.locator('#tab-account').click();await page.locator('[data-section-tab=profile]').wait_for()
    await page.locator('form:has([name=op][value=profile]) [name=name]').fill('A better account')
    await page.get_by_role('button',name='Save name',exact=True).click();await page.wait_for_function("document.querySelector('.profile-hero p').textContent==='A better account'")
    await page.screenshot(path='/tmp/rook-new-account.png')
    await page.locator('[data-section-tab=access]').click();await page.get_by_role('button',name='Show pairing code',exact=True).click();await page.locator('.pairing-code').wait_for(state='visible')
    await page.wait_for_timeout(1100);assert 'seconds remaining' in await page.locator('.pairing-expiry').inner_text();await page.locator('#view-account [data-close]').click()
    await page.get_by_role('button',name='Create invitation link',exact=True).click();await page.locator('#view-account dialog').wait_for(state='visible');assert 'invite=' in await page.locator('#view-account .dialog-content').inner_text();await page.locator('#view-account [data-close]').click()
    await page.locator('#tab-tokens').click();await page.locator('[data-create]').wait_for();await page.locator('[data-create]').click()
    await page.locator('.token-create [name=name]').fill('browser-canary');await page.locator('.token-create button').click();await page.locator('.token-secret').wait_for(state='visible')
    secret=await page.locator('.token-secret').inner_text();assert provider.verify_bearer(secret)
    assert secret not in page.url
    await page.locator('#view-tokens [data-close]').click();await page.locator('.token-secret').wait_for(state='detached');assert secret not in await page.content()
    await page.locator('[data-picture="user:operator"]').click(no_wait_after=True)
    raw=io.BytesIO();Image.new('RGB',(100,100),'#a4bc92').save(raw,format='PNG')
    await page.locator('.avatar-file').set_input_files({'name':'picture.png','mimeType':'image/png','buffer':raw.getvalue()})
    await page.locator('[data-clear="user:operator"]').wait_for();assert chat.get_avatar('user:operator')
    await page.locator('[data-clear="user:operator"]').click();await page.locator('[data-clear="user:operator"]').wait_for(state='detached');assert not chat.get_avatar('user:operator')
    await page.screenshot(path='/tmp/rook-new-tokens.png')
    await page.locator('[data-revoke]').click();await page.locator('.token-revoke button').click();await page.locator('.token-row').wait_for(state='detached');assert provider.verify_bearer(secret) is None
    for width in (1100,820,390):
     await page.set_viewport_size({'width':width,'height':1000})
     for view in ('workers','account','tokens','bands','sessions','install'):
      await page.locator('#tab-'+view).click()
      assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),str(width)+' '+view+' overflow'
      if view=='workers':
       await page.locator('.view-toggle [data-layout=grid]').click()
       assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
       await page.screenshot(path='/tmp/rook-grid-'+str(width)+'.png')
       await page.locator('.view-toggle [data-layout=list]').click()
    await page.locator('#tab-account').click();await page.screenshot(path='/tmp/rook-new-account-mobile.png')
    assert not errors,errors
    assert await page.evaluate("performance.getEntriesByType('navigation').length")==1
    print('PASS: grouped masonry/persisted view; nested keyboard capabilities; sticky controls/stats; install art; persistent description UI/search/escaping; device groups/icons/sort; worker menu/move; profile, pairing, invitation; live token store create/revoke; picture upload/clear; secrets cleared; mobile; no JS errors')
    await browser.close()
  await http.aclose()
asyncio.run(main())
