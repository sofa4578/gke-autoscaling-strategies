import os
import math
import uuid
import json
import asyncio
from concurrent.futures import ProcessPoolExecutor
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException

import redis
from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from tenacity import retry, stop_after_attempt, wait_fixed

REDIS_HOST     = os.getenv("REDIS_HOST", "localhost")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
QUEUE_KEY      = "task_queue"   

app = FastAPI(title="Diploma Highload API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
Instrumentator(
    should_group_status_codes=False,
    should_ignore_untemplated=True,
).instrument(app).expose(app, endpoint="/metrics")

process_pool = ProcessPoolExecutor(max_workers=2)

ITEMS_DB: dict = {
    str(i): {"id": str(i), "name": f"product_{i}", "price": round(i * 9.99, 2), "stock": i * 10}
    for i in range(1, 501)
}

@retry(stop=stop_after_attempt(5), wait=wait_fixed(2))
def _connect_redis() -> redis.Redis:
    client = redis.Redis(
        host=REDIS_HOST,
        port=6379,
        db=0,
        password=REDIS_PASSWORD if REDIS_PASSWORD else None,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    client.ping()
    return client

try:
    cache = _connect_redis()
    print(f"Redis connected: {REDIS_HOST}")
except Exception as e:
    print(f"Redis unavailable: {e}")
    cache = None

def _require_redis():
    if cache is None:
        raise HTTPException(status_code=503, detail="Redis is unavailable")

def _cpu_task(n: int) -> float:
    result = 0.0
    for i in range(1, n):
        result += math.sqrt(i) * math.log(i + 1)
    return result


@app.get("/", tags=["General"])
async def root():
    _require_redis()
    try:
        visits = cache.incr("visits")
        return {"message": "Diploma Highload API v2", "visits": visits}
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/health", tags=["General"])
async def health():
    redis_ok = False
    if cache:
        try:
            cache.ping()
            redis_ok = True
        except Exception:
            pass
    return {"status": "ok", "redis": redis_ok}


@app.get("/items", tags=["Items"])
async def list_items(skip: int = 0, limit: int = 20):
    cache_key = f"items:list:{skip}:{limit}"
    if cache:
        try:
            cached = cache.get(cache_key)
            if cached:
                return {"source": "cache", "data": json.loads(cached)}
        except redis.RedisError:
            pass

    items = list(ITEMS_DB.values())[skip: skip + limit]
    result = {"source": "db", "total": len(ITEMS_DB), "items": items}

    if cache:
        try:
            cache.setex(cache_key, 30, json.dumps(result))
        except redis.RedisError:
            pass
    return result


@app.get("/items/{item_id}", tags=["Items"])
async def get_item(item_id: str):
    cache_key = f"item:{item_id}"
    if cache:
        try:
            cached = cache.get(cache_key)
            if cached:
                return {"source": "cache", "data": json.loads(cached)}
        except redis.RedisError:
            pass

    item = ITEMS_DB.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

    if cache:
        try:
            cache.setex(cache_key, 60, json.dumps(item))
        except redis.RedisError:
            pass
    return {"source": "db", "data": item}


@app.post("/items", tags=["Items"], status_code=201)
async def create_item(name: str, price: float, stock: int = 0):
    item_id = str(uuid.uuid4())[:8]
    ITEMS_DB[item_id] = {"id": item_id, "name": name, "price": price, "stock": stock}
    if cache:
        try:
            keys = cache.keys("items:list:*")
            if keys:
                cache.delete(*keys)
        except redis.RedisError:
            pass
    return {"created": item_id, "data": ITEMS_DB[item_id]}


@app.get("/stats", tags=["Analytics"])
async def stats():
    cache_key = "stats:summary"
    if cache:
        try:
            cached = cache.get(cache_key)
            if cached:
                return {"source": "cache", **json.loads(cached)}
        except redis.RedisError:
            pass

    total_items  = len(ITEMS_DB)
    total_value  = sum(v["price"] * v["stock"] for v in ITEMS_DB.values())
    avg_price    = sum(v["price"] for v in ITEMS_DB.values()) / total_items if total_items else 0
    result = {"total_items": total_items, "total_value": round(total_value, 2), "avg_price": round(avg_price, 2)}

    if cache:
        try:
            cache.setex(cache_key, 10, json.dumps(result))
        except redis.RedisError:
            pass
    return {"source": "db", **result}


stress_semaphore = asyncio.Semaphore(3)  

@app.get("/stress")
async def stress(n: int = 500000):
    if n > 5_000_000:
        raise HTTPException(status_code=400, detail="n too large")
    async with stress_semaphore:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(process_pool, _cpu_task, n)
    return {"computations": n, "result": round(result, 2)}


@app.post("/tasks", tags=["Task Queue"])
async def enqueue_task(payload: str = "default_task"):
    _require_redis()
    for attempt in range(3):        
        try:
            task_id = str(uuid.uuid4())[:8]
            task = json.dumps({"id": task_id, "payload": payload})
            queue_len = cache.rpush(QUEUE_KEY, task)
            return {"task_id": task_id, "queue_length": queue_len}
        except redis.RedisError:
            if attempt == 2:
                raise HTTPException(status_code=503, detail="Redis unavailable after retries")
            await asyncio.sleep(0.1)


@app.get("/tasks/process", tags=["Task Queue"])
async def process_task():
    _require_redis()
    try:
        raw = cache.lpop(QUEUE_KEY)
        if not raw:
            return {"status": "queue_empty"}
        task = json.loads(raw)
        # Імітуємо обробку
        await asyncio.sleep(0.1)
        return {"status": "processed", "task": task}
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/tasks/length", tags=["Task Queue"])
async def queue_length():
    _require_redis()
    try:
        length = cache.llen(QUEUE_KEY)
        return {"queue_length": length}
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail=str(e))
