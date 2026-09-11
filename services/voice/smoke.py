"""Synthetic, read-only deployment checks. Run beside the candidate; never logs credentials."""
import asyncio
import json
import os
import uuid
import websockets

async def main():
    uri=os.environ.get('VOICE_SMOKE_URL','ws://127.0.0.1:8901/ws')
    headers={'Authorization':'Bearer '+os.environ['VOICE_TOKEN']} if os.environ.get('VOICE_TOKEN') else {}
    cid=str(uuid.uuid4())
    async def connect():
        ws=await websockets.connect(uri,additional_headers=headers)
        await ws.send(json.dumps({'type':'hello','protocol':2,'conversation':cid,'aec':True}))
        return ws
    async def until(ws,kind,timeout=70):
        text=[]; packets=0
        async with asyncio.timeout(timeout):
            while True:
                event=await ws.recv()
                if isinstance(event,bytes):
                    assert event.startswith(b'RK2A'); packets+=1;continue
                event=json.loads(event)
                if event['type']=='error':raise AssertionError(event['msg'])
                if event['type']=='assistant_delta': text.append(event['text'])
                if event['type']==kind:return '\n'.join(text),packets,event
    ws=await connect()
    await until(ws,'session')
    await ws.send(json.dumps({'type':'text','text':'Remember this test code word: cobalt-marmot. Reply briefly.','speak':False}))
    await until(ws,'assistant_done')
    await ws.close(); await asyncio.sleep(.5)
    ws=await connect(); await until(ws,'session')
    await ws.send(json.dumps({'type':'text','text':'What was the test code word?','speak':False}))
    answer,_,_=await until(ws,'assistant_done')
    assert 'cobalt' in answer.lower() and 'marmot' in answer.lower(), 'reconnect recall failed'
    print('PASS: model conversation survives reconnect')
    await ws.send(json.dumps({'type':'text','text':'Say the numbers from one to twenty out loud.','speak':True}))
    async with asyncio.timeout(60):
        while True:
            event=await ws.recv()
            if isinstance(event,bytes): assert event.startswith(b'RK2A');break
            assert json.loads(event)['type']!='error',event
    await ws.send(json.dumps({'type':'stop'}))
    await until(ws,'interrupt',10)
    # An acknowledgement must be the boundary after which old audio never reappears.
    with __import__('contextlib').suppress(TimeoutError):
        async with asyncio.timeout(.5):
            while True: assert not isinstance(await ws.recv(),bytes), 'stale audio after stop acknowledgement'
    print('PASS: real TTS framing and interruption')
    await ws.send(json.dumps({'type':'text','text':'Use rook_read to get info.uptime from kaiju.','speak':False}))
    async with asyncio.timeout(90):
        started=False
        while True:
            raw=await ws.recv()
            if isinstance(raw,bytes):continue
            event=json.loads(raw)
            if event['type']=='error':raise AssertionError(event['msg'])
            if event['type']=='tool':
                if event['status']=='running':started=True
                elif started:
                    assert event['status']=='completed',event
                    break
    print('PASS: read-only Rook job completes')
    await ws.close()

if __name__=='__main__': asyncio.run(main())
