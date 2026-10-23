"""Lifecycle of a native worker: reconnect in-process, never exec app_process."""
import asyncio
import logging

log=logging.getLogger('rook.android.runtime')
_loop=None
_stop=None
#: Every runner still alive, as (loop, stop event). stop() signals all of them,
#: so a runner whose shutdown was still in flight when the service restarted
#: cannot survive as an orphan that keeps its own 30 s refresh going.
_runners=set()
REFRESH_SECONDS=30
#: A connection that dies sooner than this counts as a failed start: the next
#: attempt waits (jittered, doubling up to BACKOFF_MAX) instead of reconnecting
#: at once.
STABLE_SECONDS=10
BACKOFF_MIN=2
BACKOFF_MAX=60
#: Off the band this long with the link still not up: rebuild the transport.
#: The link retries on its own; this is the backstop for one that wedges.
STALE_SECONDS=120
#: The transport of the live worker, for kick() (network changes).
_transport=None


def start(hub,psk,name):
    from rook.worker.core import Worker
    from rook.worker.transports.telesthete_hub import TelestheteHubTransport
    from rook.worker import enroll,wconfig
    from rook.core.backoff import Backoff
    from rook_android.androidctx import app_context
    from worker_entry import worker_kwargs
    from rook.core.plugin import CapMeta
    context=app_context()
    prefs=context.getSharedPreferences('rook',0) if context else None
    fallback=(hub,psk,name)

    # The last device-authorized config and when it was fetched (loop time).
    # Reconnects reuse it inside REFRESH_SECONDS: one challenge + one config
    # per window per phone, however often the worker itself restarts.
    fetched=[None,None]

    from rook_android import roster

    async def desired(force=False):
        current=tuple(str(prefs.getString(k,v) or v) for k,v in zip(('hub','psk','name'),fallback)) if prefs else fallback
        saved=enroll.load()
        selected=str(prefs.getString('band_id','') or '') if prefs else ''
        use_identity=saved.get('auto_start') and saved.get('device') and (not selected or selected==saved.get('active_band'))
        if use_identity:
            now=asyncio.get_running_loop().time()
            if force or fetched[0] is None or now-fetched[0]>=REFRESH_SECONDS:
                # The same proof also fetches the account's band rosters for the
                # Workers tab (hubs without it just leave them out).
                fetched[1]=await asyncio.to_thread(enroll.refresh,want=('workers',),on_result=roster.hub_result)
                fetched[0]=asyncio.get_running_loop().time()
            saved=fetched[1]
            band=next(b for b in saved['bands'] if b['id']==saved['active_band'])
            roster.note_identity(True,band)
            current=(band['hub'],band['psk'],current[2])
            if prefs:
                prefs.edit().putString('hub',band['hub']).putString('psk',band['psk']).putString('band_id',band['id']).putInt('band_epoch',band['epoch']).apply()
        else:
            roster.note_identity(False)
        cfg=wconfig.load();wconfig.apply_env(cfg)
        return (current[0] if use_identity else cfg.get('hub',current[0]),
                current[1] if use_identity else cfg.get('psk',current[1]),
                cfg.get('name',current[2]))

    async def runner():
        global _stop,_loop
        # This runner's own stop event: never re-read the module globals, which
        # a newer runner may already have replaced.
        loop=asyncio.get_running_loop();stopping=asyncio.Event()
        _loop,_stop=loop,stopping
        me=(loop,stopping);_runners.add(me)
        # Jitter keeps a fleet of phones that lost the hub together from
        # reconnecting in lockstep (rook.core.backoff, shared with the worker).
        retry=Backoff(base=BACKOFF_MIN,cap=BACKOFF_MAX)
        try:
            while not stopping.is_set():
                wconfig.boot_reconcile()
                try:current=await desired()
                except Exception as error:
                    log.error('Device authorization unavailable (%s); leaving band.',type(error).__name__)
                    return
                address,key,worker_name=current
                host,port=address.rsplit(':',1)
                transport=TelestheteHubTransport(psk=key,hub_host=host,hub_port=int(port),use_ws=port in ('443','8443'))
                worker=Worker(transport=transport,name=worker_name,**worker_kwargs())
                # Passive keep-awake: screen-driving caps wake the phone and hold
                # it awake. It must never keep the worker off the band.
                try:
                    from rook_android.keepawake import wrap_registry
                    log.info('keep-awake wraps %d screen caps',len(wrap_registry(worker.registry)))
                except Exception:
                    log.exception('keep-awake wrap failed')
                package=context.getPackageManager().getPackageInfo(context.getPackageName(),0) if context else None
                worker.app_release = {'platform':'android', 'version':str(package.versionName),
                                      'code':int(package.versionCode)} if package else {}
                # The app's Workers tab: band announces this worker already receives.
                roster.attach(worker)
                cycle=asyncio.Event()

                async def restart():
                    """Restart the embedded worker (the Android service reconnects it)."""
                    async def later(event=cycle):
                        await asyncio.sleep(.5);event.set()
                    asyncio.create_task(later())
                    return {'ok':True,'supervisor':'android-service','restarting':True}

                def status():
                    """Build, app version and supervisor of this embedded worker."""
                    from rook.worker._build_info import as_dict
                    package=context.getPackageManager().getPackageInfo(context.getPackageName(),0) if context else None
                    power={}
                    try:
                        from java import jclass
                        Ctx=jclass('android.content.Context')
                        pm=context.getSystemService(Ctx.POWER_SERVICE)
                        power['battery_optimization_exempt']=bool(pm.isIgnoringBatteryOptimizations(context.getPackageName()))
                        power['device_idle']=bool(pm.isDeviceIdleMode())
                        usage=context.getSystemService(Ctx.USAGE_STATS_SERVICE)
                        if usage is not None:power['standby_bucket']=int(usage.getAppStandbyBucket())
                    except Exception:pass
                    return {**as_dict(),'supervisor':'android-service','pyz_exists':False,'power':power,
                            'app_version':str(package.versionName) if package else '',
                            'app_version_code':int(package.versionCode) if package else 0,
                            'app_release':worker.app_release}

                # The service supervises the worker here (selfupdate is the
                # desktop's and is placed away from the APK): same names and
                # tiers as its caps.
                worker.registry.register('worker.restart',restart,
                                         meta=CapMeta(risk='admin',tags=('destructive',)))
                worker.registry.register('worker.status',status,meta=CapMeta(risk='read'))
                task=asyncio.create_task(worker.run())
                began=loop.time()
                global _transport
                _transport=transport
                try:
                    checked=loop.time()
                    down_since=None
                    while not stopping.is_set() and not cycle.is_set() and not task.done():
                        try:await asyncio.wait_for(stopping.wait(),1)
                        except asyncio.TimeoutError:pass
                        link=getattr(transport,'link',None)
                        if link is not None and link.connected:down_since=None
                        elif down_since is None:down_since=loop.time()
                        elif loop.time()-down_since>=STALE_SECONDS:
                            log.warning('Off the band for %ds; rebuilding the connection.',STALE_SECONDS)
                            break
                        if loop.time()-checked>=REFRESH_SECONDS:
                            checked=loop.time()
                            try:
                                if await desired(force=True)!=current:break
                            except Exception as error:
                                log.error('Device authorization unavailable (%s); leaving band.',type(error).__name__)
                                stopping.set()
                finally:
                    if _transport is transport:_transport=None
                    roster.detach(worker)
                    await worker.shutdown()
                    try:await asyncio.wait_for(task,2)
                    except (Exception,asyncio.CancelledError):pass
                if task.done() and not cycle.is_set() and not stopping.is_set():
                    # The worker died on its own. Without this pause a worker
                    # that fails at start reconnects (and, before the refresh
                    # cache, re-authenticated) about once a second.
                    if loop.time()-began>=STABLE_SECONDS:retry.reset()
                    backoff=retry.next()
                    log.warning('Band worker exited; reconnecting in %.1fs.',backoff)
                    try:await asyncio.wait_for(stopping.wait(),backoff)
                    except asyncio.TimeoutError:pass
                else:
                    retry.reset()
        finally:
            _runners.discard(me)
            if _stop is stopping:
                _loop=None;_stop=None

    asyncio.run(runner())


def kick(reason=''):
    """The phone's default network changed: drop the band link (or the connect
    in flight) and reconnect at once on the new network. Thread-safe; called
    from WorkerService's network callback."""
    loop,transport=_loop,_transport
    link=getattr(transport,'link',None)
    if loop is None or link is None or not hasattr(link,'kick'):return False
    log.info('Network changed (%s); reconnecting the band link.',reason or 'unknown')
    try:loop.call_soon_threadsafe(link.kick)
    except RuntimeError:return False
    return True


def stop():
    for loop,event in list(_runners):
        try:loop.call_soon_threadsafe(event.set)
        except RuntimeError:pass   # that loop already closed
