"""Exercise embedded reconnect/revocation without exec or an Android device."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


def test_native_reconnects_and_stops_on_revocation(monkeypatch):
    from rook.worker import core,enroll,wconfig
    from rook.worker.transports import telesthete_hub
    path=Path(__file__).parents[1]/'android/app/src/main/python/rook_android/worker_runtime.py'
    spec=importlib.util.spec_from_file_location('runtime_test',path)
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    runtime.REFRESH_SECONDS=.01
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'android/app/src/main/python'))
    monkeypatch.setitem(sys.modules,'rook_android.androidctx',SimpleNamespace(app_context=lambda:None))
    monkeypatch.setitem(sys.modules,'worker_entry',SimpleNamespace(worker_kwargs=lambda:{}))
    saved={'auto_start':True,'device':{'id':'device'},'active_band':'band'}
    monkeypatch.setattr(enroll,'load',lambda:saved)
    calls=[];started=[];stopped=[]
    def refresh(**kwargs):
        assert kwargs['want']==('workers',) and callable(kwargs['on_result'])
        calls.append(1)
        if len(calls)>=4:raise ValueError('revoked')
        key='old' if len(calls)==1 else 'new'
        return {**saved,'bands':[{'id':'band','psk':key,'hub':'hub.example.com:443','epoch':len(calls)}]}
    monkeypatch.setattr(enroll,'refresh',refresh)
    monkeypatch.setattr(wconfig,'load',lambda:{})
    monkeypatch.setattr(wconfig,'apply_env',lambda _:None)
    monkeypatch.setattr(wconfig,'boot_reconcile',lambda:None)
    class FakeWorker:
        def __init__(self,transport,**kwargs):
            self.transport=transport;self.done=asyncio.Event()
            self.registry=SimpleNamespace(register=lambda *args,**kw:None)
        async def run(self):
            started.append(self.transport.psk)
            await self.done.wait()
        async def shutdown(self):
            stopped.append(self.transport.psk);self.done.set()
    monkeypatch.setattr(core,'Worker',FakeWorker)
    monkeypatch.setattr(telesthete_hub,'TelestheteHubTransport',lambda **kwargs:SimpleNamespace(**kwargs))
    runtime.start('hub.example.com:443','old','phone')
    assert started==['old','new']
    assert stopped==['old','new']
    assert runtime._loop is None


def _runtime(monkeypatch):
    from rook.worker import core,wconfig
    from rook.worker.transports import telesthete_hub
    path=Path(__file__).parents[1]/'android/app/src/main/python/rook_android/worker_runtime.py'
    spec=importlib.util.spec_from_file_location('runtime_storm_test',path)
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'android/app/src/main/python'))
    monkeypatch.setitem(sys.modules,'rook_android.androidctx',SimpleNamespace(app_context=lambda:None))
    monkeypatch.setitem(sys.modules,'worker_entry',SimpleNamespace(worker_kwargs=lambda:{}))
    monkeypatch.setattr(wconfig,'load',lambda:{})
    monkeypatch.setattr(wconfig,'apply_env',lambda _:None)
    monkeypatch.setattr(wconfig,'boot_reconcile',lambda:None)
    monkeypatch.setattr(telesthete_hub,'TelestheteHubTransport',lambda **kwargs:SimpleNamespace(**kwargs))
    return runtime,core


def test_failing_worker_neither_storms_the_hub_nor_spins(monkeypatch):
    """A worker that dies at start used to reconnect about once a second and
    re-run the device proof (challenge + config) on every attempt."""
    from rook.worker import enroll
    import time
    runtime,core=_runtime(monkeypatch)
    runtime.REFRESH_SECONDS=60;runtime.BACKOFF_MIN=.05;runtime.BACKOFF_MAX=.2;runtime.STABLE_SECONDS=10
    saved={'auto_start':True,'device':{'id':'device'},'active_band':'band'}
    monkeypatch.setattr(enroll,'load',lambda:saved)
    refreshes=[];starts=[]
    def refresh(**kwargs):
        refreshes.append(1)
        return {**saved,'bands':[{'id':'band','psk':'key','hub':'hub.example.com:443','epoch':1}]}
    monkeypatch.setattr(enroll,'refresh',refresh)
    class Crashing:
        def __init__(self,transport,**kwargs):
            self.registry=SimpleNamespace(register=lambda *args,**kw:None)
        async def run(self):
            starts.append(time.monotonic())
            if len(starts)>=5:runtime.stop()
            raise RuntimeError('cannot start')
        async def shutdown(self):pass
    monkeypatch.setattr(core,'Worker',Crashing)
    runtime.start('hub.example.com:443','key','phone')
    assert len(starts)==5
    assert refreshes==[1]            # one proof per refresh window, not one per reconnect
    gaps=[b-a for a,b in zip(starts,starts[1:])]
    assert gaps[-1]>gaps[0]          # backing off, not a fixed tight loop
    assert runtime._runners==set()


def test_stop_reaches_every_live_runner(monkeypatch):
    import threading
    runtime,core=_runtime(monkeypatch)
    from rook.worker import enroll
    monkeypatch.setattr(enroll,'load',lambda:{})
    up=threading.Semaphore(0)
    class Idle:
        def __init__(self,transport,**kwargs):
            self.registry=SimpleNamespace(register=lambda *args,**kw:None);self.done=None
        async def run(self):
            self.done=asyncio.Event();up.release();await self.done.wait()
        async def shutdown(self):
            if self.done:self.done.set()
    monkeypatch.setattr(core,'Worker',Idle)
    threads=[threading.Thread(target=runtime.start,args=('hub.example.com:443','key','phone'),daemon=True) for _ in range(2)]
    for t in threads:t.start()
    assert up.acquire(timeout=5) and up.acquire(timeout=5)
    runtime.stop()
    for t in threads:t.join(5)
    assert not any(t.is_alive() for t in threads)


def test_link_down_too_long_rebuilds_the_connection(monkeypatch):
    """A link that stays down (a wedged connect) gets a fresh transport after
    STALE_SECONDS instead of leaving the phone off the band for hours."""
    from rook.worker import enroll
    from rook.worker.transports import telesthete_hub
    runtime,core=_runtime(monkeypatch)
    runtime.STALE_SECONDS=.2
    monkeypatch.setattr(enroll,'load',lambda:{})
    transports=[]
    def make(**kwargs):
        t=SimpleNamespace(link=SimpleNamespace(connected=False),**kwargs);transports.append(t);return t
    monkeypatch.setattr(telesthete_hub,'TelestheteHubTransport',make)
    class Idle:
        def __init__(self,transport,**kwargs):
            self.registry=SimpleNamespace(register=lambda *args,**kw:None);self.done=asyncio.Event()
        async def run(self):
            if len(transports)>=3:runtime.stop()
            await self.done.wait()
        async def shutdown(self):self.done.set()
    monkeypatch.setattr(core,'Worker',Idle)
    runtime.start('hub.example.com:443','key','phone')
    assert len(transports)>=3


def test_kick_reaches_the_live_link(monkeypatch):
    import threading
    from rook.worker import enroll
    from rook.worker.transports import telesthete_hub
    runtime,core=_runtime(monkeypatch)
    monkeypatch.setattr(enroll,'load',lambda:{})
    kicked=threading.Event()
    link=SimpleNamespace(connected=True,kick=kicked.set)
    monkeypatch.setattr(telesthete_hub,'TelestheteHubTransport',lambda **kwargs:SimpleNamespace(link=link,**kwargs))
    up=threading.Event()
    class Idle:
        def __init__(self,transport,**kwargs):
            self.registry=SimpleNamespace(register=lambda *args,**kw:None);self.done=None
        async def run(self):
            self.done=asyncio.Event();up.set();await self.done.wait()
        async def shutdown(self):
            if self.done:self.done.set()
    monkeypatch.setattr(core,'Worker',Idle)
    assert runtime.kick('test') is False          # nothing running yet
    t=threading.Thread(target=runtime.start,args=('hub.example.com:443','key','phone'),daemon=True);t.start()
    assert up.wait(5)
    for _ in range(50):
        if runtime._transport is not None:break
        import time;time.sleep(.02)
    assert runtime.kick('wifi -> cellular') is True
    assert kicked.wait(5)
    runtime.stop();t.join(5)
    assert not t.is_alive() and runtime._transport is None


class _Prefs:
    """SharedPreferences as the runtime uses it (Chaquopy proxies the Java one)."""
    def __init__(self,**values):self.values=dict(values)
    def getString(self,key,default):return self.values.get(key,default)
    def getBoolean(self,key,default):return self.values.get(key,default)
    def edit(self):
        prefs=self
        class Editor:
            def __getattr__(self,name):
                def put(key,value):prefs.values[key]=value;return self
                return put
            def apply(self):pass
        return Editor()


def _context(prefs):
    package=SimpleNamespace(versionName='0.0.0',versionCode=1)
    return SimpleNamespace(getSharedPreferences=lambda name,mode:prefs,getPackageName=lambda:'app',
                           getPackageManager=lambda:SimpleNamespace(getPackageInfo=lambda name,flags:package))


def _dial(monkeypatch,prefs,saved=None,bands=None,cfg=None):
    """Start the runtime once against ``prefs``; the transport kwargs it built."""
    from rook.worker import enroll,wconfig
    from rook.worker.transports import telesthete_hub
    runtime,core=_runtime(monkeypatch)
    monkeypatch.setitem(sys.modules,'rook_android.androidctx',SimpleNamespace(app_context=lambda:_context(prefs)))
    monkeypatch.setattr(enroll,'load',lambda:saved or {})
    monkeypatch.setattr(enroll,'refresh',lambda **kw:{**saved,'bands':bands})
    if cfg is not None:monkeypatch.setattr(wconfig,'load',lambda:cfg)
    built=[]
    def make(**kwargs):
        built.append(kwargs);return SimpleNamespace(link=None,**kwargs)
    monkeypatch.setattr(telesthete_hub,'TelestheteHubTransport',make)
    class Once:
        def __init__(self,transport,**kwargs):
            self.registry=SimpleNamespace(register=lambda *args,**kw:None)
        async def run(self):runtime.stop()
        async def shutdown(self):pass
    monkeypatch.setattr(core,'Worker',Once)
    runtime.start(prefs.values.get('hub','x:1'),'key','phone')
    assert len(built)==1
    return built[0]


def test_transport_rule():
    path=Path(__file__).parents[1]/'android/app/src/main/python/rook_android/worker_runtime.py'
    spec=importlib.util.spec_from_file_location('runtime_rule_test',path)
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    assert runtime.use_ws('443',False) and runtime.use_ws('8443',False)
    assert not runtime.use_ws('7474',False) and not runtime.use_ws('7005',False)
    assert runtime.use_ws('7005',True) and runtime.use_ws('7474',True)


def test_paired_lan_hub_dials_the_websocket_bridge(monkeypatch):
    """A LAN hub's pairing reply says hub <ip>:7005 (its /band bridge): before,
    the port rule sent UDP to 7005 and the phone never joined."""
    t=_dial(monkeypatch,_Prefs(hub='192.168.1.20:7005',psk='key',hub_ws=True))
    assert (t['hub_host'],t['hub_port'],t['use_ws'])==('192.168.1.20',7005,True)


def test_typed_hub_keeps_the_port_rule(monkeypatch):
    assert _dial(monkeypatch,_Prefs(hub='192.168.1.20:7474',psk='key'))['use_ws'] is False
    assert _dial(monkeypatch,_Prefs(hub='hub.example.com:443',psk='key'))['use_ws'] is True
    assert _dial(monkeypatch,_Prefs(hub='192.168.1.20:7474',psk='key',hub_ws=False))['use_ws'] is False


def test_pushed_hub_that_differs_from_the_paired_one_uses_the_port_rule(monkeypatch):
    prefs=_Prefs(hub='192.168.1.20:7005',psk='key',hub_ws=True)
    t=_dial(monkeypatch,prefs,cfg={'hub':'192.168.1.20:7474'})
    assert (t['hub_port'],t['use_ws'])==(7474,False)


def test_identity_band_dials_the_bridge_and_remembers_it(monkeypatch):
    saved={'auto_start':True,'device':{'id':'device'},'active_band':'band'}
    prefs=_Prefs(hub='old.example.com:443',psk='old')
    t=_dial(monkeypatch,prefs,saved=saved,bands=[{'id':'band','psk':'k2','hub':'10.0.0.5:7005','epoch':1}])
    assert (t['hub_host'],t['hub_port'],t['use_ws'],t['psk'])==('10.0.0.5',7005,True,'k2')
    assert prefs.values['hub']=='10.0.0.5:7005' and prefs.values['hub_ws'] is True
