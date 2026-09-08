"""Lifecycle of a native worker: reconnect in-process, never exec app_process."""
import asyncio
import logging

log=logging.getLogger('rook.android.runtime')
_loop=None
_stop=None
REFRESH_SECONDS=30


def start(hub,psk,name):
    from rook.worker.core import Worker
    from rook.worker.transports.telesthete_hub import TelestheteHubTransport
    from rook.worker import enroll,wconfig
    from rook_android.androidctx import app_context
    from worker_entry import _attach_native_plugins,_builtin_enabled
    context=app_context()
    prefs=context.getSharedPreferences('rook',0) if context else None
    fallback=(hub,psk,name)

    async def desired():
        current=tuple(str(prefs.getString(k,v) or v) for k,v in zip(('hub','psk','name'),fallback)) if prefs else fallback
        saved=enroll.load()
        selected=str(prefs.getString('band_id','') or '') if prefs else ''
        use_identity=saved.get('auto_start') and saved.get('device') and (not selected or selected==saved.get('active_band'))
        if use_identity:
            saved=await asyncio.to_thread(enroll.refresh)
            band=next(b for b in saved['bands'] if b['id']==saved['active_band'])
            current=(band['hub'],band['psk'],current[2])
            if prefs:
                prefs.edit().putString('hub',band['hub']).putString('psk',band['psk']).putString('band_id',band['id']).putInt('band_epoch',band['epoch']).apply()
        cfg=wconfig.load();wconfig.apply_env(cfg)
        return (current[0] if use_identity else cfg.get('hub',current[0]),
                current[1] if use_identity else cfg.get('psk',current[1]),
                cfg.get('name',current[2]))

    async def runner():
        global _stop,_loop
        _loop=asyncio.get_running_loop();_stop=asyncio.Event()
        try:
            while not _stop.is_set():
                wconfig.boot_reconcile()
                try:current=await desired()
                except Exception as error:
                    log.error('Device authorization unavailable (%s); leaving band.',type(error).__name__)
                    return
                address,key,worker_name=current
                host,port=address.rsplit(':',1)
                transport=TelestheteHubTransport(psk=key,hub_host=host,hub_port=int(port),use_ws=port in ('443','8443'))
                worker=Worker(transport=transport,enabled=_builtin_enabled(),name=worker_name)
                _attach_native_plugins(worker)
                cycle=asyncio.Event()

                async def restart():
                    async def later(event=cycle):
                        await asyncio.sleep(.5);event.set()
                    asyncio.create_task(later())
                    return {'ok':True,'supervisor':'android-service','restarting':True}

                def status():
                    from rook.worker._build_info import as_dict
                    package=context.getPackageManager().getPackageInfo(context.getPackageName(),0) if context else None
                    return {**as_dict(),'supervisor':'android-service','pyz_exists':False,
                            'app_version':str(package.versionName) if package else '',
                            'app_version_code':int(package.versionCode) if package else 0}

                worker.registry.register('worker.restart',restart)
                worker.registry.register('worker.status',status)
                task=asyncio.create_task(worker.run())
                try:
                    checked=_loop.time()
                    while not _stop.is_set() and not cycle.is_set() and not task.done():
                        try:await asyncio.wait_for(_stop.wait(),1)
                        except asyncio.TimeoutError:pass
                        if _loop.time()-checked>=REFRESH_SECONDS:
                            checked=_loop.time()
                            try:
                                if await desired()!=current:break
                            except Exception as error:
                                log.error('Device authorization unavailable (%s); leaving band.',type(error).__name__)
                                _stop.set()
                finally:
                    await worker.shutdown()
                    try:await asyncio.wait_for(task,2)
                    except (Exception,asyncio.CancelledError):pass
        finally:
            _loop=None;_stop=None

    asyncio.run(runner())


def stop():
    loop,event=_loop,_stop
    if loop is not None and event is not None:loop.call_soon_threadsafe(event.set)
