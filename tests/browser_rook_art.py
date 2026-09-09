"""Optional WebGL art checks. Run from repo root with Playwright Chromium."""
import asyncio,os,re
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

async def main():
    html=Path('rook/web/index.html').read_text()
    art=html[html.index('  <div class="rook-art"'):html.index('  <aside class="app-sidebar"')]
    boot=html[html.rindex('<script type="module">'):html.index('</body>')]
    async def index(request):
        return web.Response(text='<html><head><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/rook-art.css"></head><body>'+art+'<button id="art-toggle" hidden>Pause artwork</button>'+boot+'</body></html>',content_type='text/html')
    async def asset(request):
        name=request.match_info['name']
        return web.FileResponse(Path('rook/web')/name)
    app=web.Application();app.router.add_get('/',index);app.router.add_get('/account/bands/assets/{name}',asset);app.router.add_get('/{name}',asset)
    async with TestServer(app) as server,async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_EXECUTABLE'),headless=True)
        context=await browser.new_context(viewport={'width':1440,'height':1000})
        await context.add_init_script('''window.artFrames=0;const clear=WebGL2RenderingContext.prototype.clear;WebGL2RenderingContext.prototype.clear=function(...args){if(this.getParameter(this.FRAMEBUFFER_BINDING)===null)window.artFrames++;return clear.apply(this,args)};''')
        page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto(str(server.make_url('/')));await page.locator('[data-mode=webgl]').wait_for();await page.wait_for_timeout(150)
        before=await page.evaluate('artFrames');await page.wait_for_timeout(550);after=await page.evaluate('artFrames')
        assert 2<=after-before<=13,('frame cap',before,after)
        await page.locator('#art-toggle').click();assert await page.locator('#rook-art').get_attribute('data-motion')=='paused'
        before=await page.evaluate('artFrames');await page.wait_for_timeout(250);assert await page.evaluate('artFrames')==before
        await page.screenshot(path='/tmp/rook-art-study.png')
        await page.reload();await page.locator('[data-mode=webgl]').wait_for();assert await page.locator('#rook-art').get_attribute('data-motion')=='paused'
        await page.locator('#art-toggle').click();await page.wait_for_timeout(100)
        await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
        before=await page.evaluate('artFrames');await page.wait_for_timeout(250);assert await page.evaluate('artFrames')==before
        await page.evaluate("delete document.hidden;document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_timeout(150);assert await page.evaluate('artFrames')>before
        await page.evaluate("window.artLoss=document.querySelector('canvas').getContext('webgl2').getExtension('WEBGL_lose_context');artLoss.loseContext()")
        await page.locator('[data-mode=sketch]').wait_for();assert await page.locator('.rook-sketch').is_visible()
        assert await page.locator('#art-toggle').is_hidden()
        await page.evaluate("artLoss.restoreContext()")
        await page.locator('[data-mode=webgl]').wait_for()
        reduced=await browser.new_context(reduced_motion='reduce',viewport={'width':390,'height':844})
        quiet=await reduced.new_page();await quiet.goto(str(server.make_url('/')));await quiet.locator('[data-mode=webgl]').wait_for()
        assert await quiet.locator('#rook-art').get_attribute('data-motion')=='paused'
        assert await quiet.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await quiet.screenshot(path='/tmp/rook-art-mobile.png')
        fallback=await browser.new_context()
        await fallback.add_init_script("const get=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(type,...args){return type.startsWith('webgl')?null:get.call(this,type,...args)}")
        flat=await fallback.new_page();await flat.goto(str(server.make_url('/')));await flat.wait_for_timeout(2000)
        assert await flat.locator('.rook-sketch').is_visible() and await flat.locator('#art-toggle').is_hidden()
        assert not errors,errors
        print('PASS: real WebGL render, <=20fps, persistent pause, hidden-tab pause, reduced motion/mobile, context loss/restoration, static fallback')
        await browser.close()
asyncio.run(main())
