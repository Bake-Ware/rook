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
    monkeypatch.setitem(sys.modules,'worker_entry',SimpleNamespace(_attach_native_plugins=lambda w:None,_builtin_enabled=lambda:[]))
    saved={'auto_start':True,'device':{'id':'device'},'active_band':'band'}
    monkeypatch.setattr(enroll,'load',lambda:saved)
    calls=[];started=[];stopped=[]
    def refresh():
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
            self.registry=SimpleNamespace(register=lambda *args:None)
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
    monkeypatch.setitem(sys.modules,'worker_entry',SimpleNamespace(_attach_native_plugins=lambda w:None,_builtin_enabled=lambda:[]))
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
            self.registry=SimpleNamespace(register=lambda *args:None)
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
            self.registry=SimpleNamespace(register=lambda *args:None);self.done=None
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
