# jev-proxy

A tiny CORS proxy for the TypeSafe Jev API, so a browser page (the Pyodide demo in this repo) can call it.
`api.typesafe.ai` does not send CORS headers, so browsers block direct calls; this forwards one request
shape to one upstream URL and adds the headers.

```
browser (www.senthilsweb.com)  --POST /v1/systemone, Authorization: Bearer <user's key>-->  jev-proxy  -->  api.typesafe.ai
```

## What it does, and refuses to do
- Forwards the caller's own `Authorization` header and JSON body. Nothing else: no cookies, no extra headers.
- **Holds no key of its own**, so it cannot be used as a free relay on anyone's quota.
- Never stores or logs keys or bodies. Its log line is method, status, latency and origin. There is a test for it.
- One route (`POST /v1/systemone`), one fixed upstream, CORS only for `ALLOWED_ORIGINS`, 256 KB body cap,
  per-client rate limit, 60 s upstream timeout (502 / 504 on upstream failure).
- Requests with no `Origin` header (scripts, notebooks) are allowed. CORS protects browsers, not servers;
  the real guards against abuse are "bring your own key" and the rate limit.

**Trust, honestly:** every user's key passes through whoever runs this proxy. Read the code (one file,
`api/index.py`), or run your own copy. Do not point it at a key you cannot afford to rotate.

## Settings (environment)
| variable | default | meaning |
|---|---|---|
| `ALLOWED_ORIGINS` | `https://www.senthilsweb.com,http://localhost:*` | comma list; `:*` allows any port |
| `RATE_PER_MIN` | `120` | requests per minute per client |
| `MAX_BODY_BYTES` | `262144` | request size cap |
| `TRUST_PROXY` | `0` | `1` to read the client IP from `X-Forwarded-For` (only behind your own reverse proxy) |
| `UPSTREAM_URL` | `https://api.typesafe.ai/v1/systemone` | for tests only |

## Run
```bash
docker run -p 8080:8080 -e ALLOWED_ORIGINS=https://www.senthilsweb.com ghcr.io/senthilsweb/jev-proxy:latest
curl localhost:8080/healthz
```
Local dev without Docker: `pip install -r requirements-dev.txt && uvicorn api.index:app --port 8080`, tests: `python -m pytest tests`.

## Homelab with a subdomain
Use `compose.yml` and put a TLS reverse proxy in front (the demo page is https, so mixed content is blocked).
Caddy needs three lines:

```
jev.example.com {
    reverse_proxy 127.0.0.1:8080
}
```
Point DNS for the subdomain at the host, then use `https://jev.example.com/v1/systemone` as the endpoint
(on the demo page: `?endpoint=https://jev.example.com/v1/systemone`, or the Endpoint field).

## Vercel
Set the project root to `proxy/`. `vercel.json` rewrites everything to `api/index.py` (an ASGI app).
Settings go in the project's environment variables. Not tested on Vercel yet. The rate limiter is in memory,
so on serverless it is best-effort per instance only.

## Image
`ghcr.io/senthilsweb/jev-proxy` (linux/amd64 + arm64), built by `.github/workflows/proxy-image.yml` after the
tests pass. Runs as a non-root user with a healthcheck.
