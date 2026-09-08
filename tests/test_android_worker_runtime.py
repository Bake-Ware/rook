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
