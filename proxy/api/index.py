"""CORS proxy for the TypeSafe Jev API, so a browser page can call it.

Why it exists: api.typesafe.ai does not send CORS headers, so browser code (the Pyodide demo)
cannot call it. This forwards ONE request shape to ONE upstream URL and adds the headers.

What it does and does not do:
  - forwards the caller's own `Authorization` header and JSON body, nothing else (no cookies);
  - holds no API key of its own unless you set TYPESAFE_API_KEY *and* PROXY_SECRET; that key is only
    used for callers who send the secret, and a caller's own Authorization header always overrides it;
  - never stores or logs keys or bodies; its log line is method, status, ms and origin;
  - answers CORS only for ALLOWED_ORIGINS; limits body size and requests per minute per client.

Run: uvicorn api.index:app          (Vercel: this file is the ASGI entrypoint)
"""
import hmac
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
# Optional server-side key, so callers need not send one. It is only ever used when the caller
# proves they may spend it with PROXY_SECRET (header X-Proxy-Secret); without a secret it is off.
SERVER_KEY = os.environ.get("TYPESAFE_API_KEY", "").strip()
PROXY_SECRET = os.environ.get("PROXY_SECRET", "").strip()
# Optional "no credentials needed" mode: callers that send neither a key nor the secret still get served with the
# server-side key. That makes the proxy spend YOUR quota for anyone who can reach it, so it is off by default and
# bounded by a stricter per-client rate limit and a daily budget (per instance, in memory).
OPEN_SERVER_KEY = os.environ.get("OPEN_SERVER_KEY", "0") == "1"
OPEN_RATE_PER_MIN = int(os.environ.get("OPEN_RATE_PER_MIN", 20))
OPEN_DAILY_MAX = int(os.environ.get("OPEN_DAILY_MAX", 1000))
_open_day = {"day": "", "n": 0}
PASS_BACK = ("retry-after", "retry-after-ms", "x-typesafe-request-id")

log = logging.getLogger("jev_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
if OPEN_SERVER_KEY and not SERVER_KEY:
    log.warning("OPEN_SERVER_KEY=1 but TYPESAFE_API_KEY is empty: there is no key to serve with, so it has no effect")
if OPEN_SERVER_KEY and SERVER_KEY:
    log.warning("open mode ON: callers with no credentials are served with the server key (%d/min per client, %d/day total)",
                OPEN_RATE_PER_MIN, OPEN_DAILY_MAX)
if SERVER_KEY and len(PROXY_SECRET) < 16 and not OPEN_SERVER_KEY:
    log.warning("TYPESAFE_API_KEY is set but PROXY_SECRET is missing or shorter than 16 characters: "
                "server-key mode is OFF (callers must send their own Authorization header)")
app = FastAPI(title="jev-pii proxy", docs_url=None, redoc_url=None, openapi_url=None)
_buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()   # client -> (tokens, last_ts)


def origin_ok(origin: str) -> bool:
    return any(origin == a or (a.endswith(":*") and origin.startswith(a[:-1])) for a in ALLOWED)


def client_id(request: Request) -> str:
    if TRUST_PROXY:                                   # only behind your own edge (Fly, Caddy, Traefik)
        if request.headers.get("fly-client-ip"):      # Fly sets this itself and overwrites any client value
            return request.headers["fly-client-ip"].strip()
        if request.headers.get("x-forwarded-for"):
            return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "?"


def allow(client: str, per_min: int | None = None) -> bool:
    """Token bucket. In-memory: per process, so on serverless it is best-effort only."""
    per_min = RATE_PER_MIN if per_min is None else per_min
    now = time.monotonic()
    tokens, last = _buckets.pop(client, (float(per_min), now))
    tokens = min(float(per_min), tokens + (now - last) * per_min / 60)
    ok = tokens >= 1
    _buckets[client] = (tokens - 1 if ok else tokens, now)
    while len(_buckets) > 10_000:
        _buckets.popitem(last=False)
    return ok


def open_budget_left() -> bool:
    """Daily cap on calls served with the server key to callers who sent no credentials (UTC day)."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if _open_day["day"] != day:
        _open_day.update(day=day, n=0)
    if _open_day["n"] >= OPEN_DAILY_MAX:
        return False
    _open_day["n"] += 1
    return True


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
                               "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Proxy-Secret",
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
    if auth:                                            # the caller's own key always wins
        if not auth.lower().startswith("bearer ") or len(auth) < 15:
            return reply(401, b'{"error":"Authorization must be: Bearer <your TypeSafe key>"}', out)
    elif SERVER_KEY and len(PROXY_SECRET) >= 16 and hmac.compare_digest(
            request.headers.get("x-proxy-secret", "").encode(), PROXY_SECRET.encode()):
        auth = f"Bearer {SERVER_KEY}"
    elif SERVER_KEY and OPEN_SERVER_KEY and not request.headers.get("x-proxy-secret"):
        if not allow("open:" + client_id(request), OPEN_RATE_PER_MIN):
            return reply(429, b'{"error":"rate limit for keyless use"}', {**out, "Retry-After": "10"})
        if not open_budget_left():
            return reply(429, b'{"error":"daily budget for keyless use is used up; send your own key"}', out)
        auth = f"Bearer {SERVER_KEY}"
    else:                                               # no key, or a wrong/missing secret: same answer
        return reply(401, b'{"error":"send your own key as Authorization: Bearer ..., or the proxy secret"}', out)
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
