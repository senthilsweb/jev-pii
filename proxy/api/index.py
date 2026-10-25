"""CORS proxy for the TypeSafe Jev API, so a browser page can call it.

Why it exists: api.typesafe.ai does not send CORS headers, so browser code (the Pyodide demo)
cannot call it. This forwards ONE request shape to ONE upstream URL and adds the headers.

What it does and does not do:
  - forwards the caller's own `Authorization` header and JSON body, nothing else (no cookies);
  - holds no API key of its own, so it cannot be used as a free relay on anyone's quota;
  - never stores or logs keys or bodies; its log line is method, status, ms and origin;
  - answers CORS only for ALLOWED_ORIGINS; limits body size and requests per minute per client.

Run: uvicorn api.index:app          (Vercel: this file is the ASGI entrypoint)
"""
import logging
import os
import time
from collections import OrderedDict

import httpx
from fastapi import FastAPI, Request, Response

UPSTREAM = os.environ.get("UPSTREAM_URL", "https://api.typesafe.ai/v1/systemone")
ALLOWED = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "https://www.senthilsweb.com,http://localhost:*").split(",") if o.strip()]
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", 256 * 1024))
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", 120))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"      # read X-Forwarded-For (behind Caddy/Traefik)
PASS_BACK = ("retry-after", "retry-after-ms", "x-typesafe-request-id")

log = logging.getLogger("jev_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
app = FastAPI(title="jev-pii proxy", docs_url=None, redoc_url=None, openapi_url=None)
_buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()   # client -> (tokens, last_ts)


def origin_ok(origin: str) -> bool:
    return any(origin == a or (a.endswith(":*") and origin.startswith(a[:-1])) for a in ALLOWED)


def client_id(request: Request) -> str:
    if TRUST_PROXY and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "?"


def allow(client: str) -> bool:
    """Token bucket. In-memory: per process, so on serverless it is best-effort only."""
    now = time.monotonic()
    tokens, last = _buckets.pop(client, (float(RATE_PER_MIN), now))
    tokens = min(float(RATE_PER_MIN), tokens + (now - last) * RATE_PER_MIN / 60)
    ok = tokens >= 1
    _buckets[client] = (tokens - 1 if ok else tokens, now)
    while len(_buckets) > 10_000:
        _buckets.popitem(last=False)
    return ok


def cors(origin: str) -> dict:
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
            "Access-Control-Expose-Headers": "Retry-After, retry-after-ms, x-typesafe-request-id"}


def http() -> httpx.AsyncClient:
    if not hasattr(app.state, "client"):
        app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False)
    return app.state.client


def reply(status, body=b"", headers=None, ctype="application/json"):
    return Response(content=body, status_code=status, headers=headers or {}, media_type=ctype)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def root():
    return Response("jev-pii proxy: POST /v1/systemone with your own Authorization header. "
                    "https://github.com/senthilsweb/jev-pii\n", media_type="text/plain")


@app.options("/v1/systemone")
async def preflight(request: Request):
    origin = request.headers.get("origin", "")
    if not origin_ok(origin):
        return reply(403, b'{"error":"origin not allowed"}')
    return reply(204, headers={**cors(origin), "Access-Control-Allow-Methods": "POST, OPTIONS",
                               "Access-Control-Allow-Headers": "Authorization, Content-Type",
                               "Access-Control-Max-Age": "600"})


@app.post("/v1/systemone")
async def forward(request: Request):
    t0 = time.monotonic()
    origin = request.headers.get("origin", "")
    out = {}
    if origin:
        if not origin_ok(origin):
            return reply(403, b'{"error":"origin not allowed"}')
        out = cors(origin)
    if not allow(client_id(request)):
        return reply(429, b'{"error":"rate limit"}', {**out, "Retry-After": "10"})
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or len(auth) < 15:
        return reply(401, b'{"error":"send your own TypeSafe key as Authorization: Bearer ..."}', out)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return reply(413, b'{"error":"body too large"}', out)
    try:
        up = await http().post(UPSTREAM, content=body, headers={
            "Authorization": auth, "Content-Type": "application/json"})
    except httpx.TimeoutException:
        status, payload = 504, b'{"error":"upstream timeout"}'
        headers = out
    except httpx.HTTPError:
        status, payload = 502, b'{"error":"upstream unreachable"}'
        headers = out
    else:
        status, payload = up.status_code, up.content
        headers = {**out, **{h: up.headers[h] for h in PASS_BACK if h in up.headers}}
    log.info("POST /v1/systemone %s %dms origin=%s", status, (time.monotonic() - t0) * 1000, origin or "-")
    return reply(status, payload, headers)
