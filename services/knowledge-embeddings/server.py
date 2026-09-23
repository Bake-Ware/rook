"""CPU embeddings on Soundwave; expose only on a loopback/SSH-forwarded port."""
import asyncio
from aiohttp import web
from fastembed import TextEmbedding

MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
model = TextEmbedding(model_name=MODEL, cache_dir='/models', threads=2)
lock = asyncio.Lock()

async def embed(request):
    data = await request.json()
    texts = data.get('texts')
    if not isinstance(texts, list) or not 1 <= len(texts) <= 16 or any(not isinstance(t, str) or len(t) > 5000 for t in texts):
        raise web.HTTPBadRequest(text='Expected 1–16 texts, at most 5000 characters each')
    async with lock:
        vectors = await asyncio.to_thread(lambda: [v.tolist() for v in model.embed(texts, batch_size=16)])
    return web.json_response({'model': MODEL, 'vectors': vectors})

async def health(request):
    return web.json_response({'ok': True, 'model': MODEL})

app = web.Application(client_max_size=128*1024)
app.router.add_post('/embed', embed)
app.router.add_get('/health', health)
web.run_app(app, host='0.0.0.0', port=8768, access_log=None)
