import json

import httpx
import pytest
from fastapi.testclient import TestClient

from api import index as proxy

KEY = "Bearer sk-test-0123456789abcdef"
GOOD = {"Origin": "https://www.senthilsweb.com", "Authorization": KEY, "Content-Type": "application/json"}
seen = []


def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    return httpx.Response(200, json={"answers": {"ok": 1}},
                          headers={"Retry-After": "3", "Set-Cookie": "x=1", "x-typesafe-request-id": "req_1"})


@pytest.fixture(autouse=True)
def fresh():
    seen.clear()
    proxy._buckets.clear()
    proxy.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    proxy.RATE_PER_MIN = 120
    return TestClient(proxy.app)


@pytest.fixture
def c(fresh):
    return fresh


def test_forwards_body_and_users_own_key_only(c):
    r = c.post("/v1/systemone", content=b'{"state":{}}', headers={**GOOD, "Cookie": "session=abc", "X-Other": "1"})
    assert r.status_code == 200 and r.json() == {"answers": {"ok": 1}}
    sent = seen[0]
    assert sent.url == "https://api.typesafe.ai/v1/systemone"
    assert sent.headers["authorization"] == KEY and sent.content == b'{"state":{}}'
    assert "cookie" not in sent.headers and "x-other" not in sent.headers and "origin" not in sent.headers


def test_cors_headers_for_allowed_origin_and_no_set_cookie_back(c):
    r = c.post("/v1/systemone", content=b"{}", headers=GOOD)
    assert r.headers["access-control-allow-origin"] == "https://www.senthilsweb.com"
    assert r.headers["vary"] == "Origin" and r.headers["retry-after"] == "3"
    assert "set-cookie" not in r.headers


def test_preflight_allowed_and_denied(c):
    ok = c.options("/v1/systemone", headers={"Origin": "http://localhost:4321",
                                              "Access-Control-Request-Method": "POST"})
    assert ok.status_code == 204 and "authorization" in ok.headers["access-control-allow-headers"].lower()
    bad = c.options("/v1/systemone", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 403 and "access-control-allow-origin" not in bad.headers


def test_disallowed_origin_never_reaches_upstream(c):
    r = c.post("/v1/systemone", content=b"{}", headers={**GOOD, "Origin": "https://evil.example"})
    assert r.status_code == 403 and not seen


def test_no_origin_is_allowed_for_notebooks_and_scripts(c):
    r = c.post("/v1/systemone", content=b"{}", headers={"Authorization": KEY})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


def test_missing_or_short_key_is_rejected_locally(c):
    assert c.post("/v1/systemone", content=b"{}", headers={"Origin": GOOD["Origin"]}).status_code == 401
    assert c.post("/v1/systemone", content=b"{}", headers={**GOOD, "Authorization": "Bearer x"}).status_code == 401
    assert not seen


def test_body_limit(c):
    r = c.post("/v1/systemone", content=b"x" * (proxy.MAX_BODY + 1), headers=GOOD)
    assert r.status_code == 413 and not seen


def test_rate_limit_per_client(c):
    proxy.RATE_PER_MIN = 3
    codes = [c.post("/v1/systemone", content=b"{}", headers=GOOD).status_code for _ in range(5)]
    assert codes[:3] == [200] * 3 and codes[3:] == [429, 429]


def test_only_the_one_route_exists(c):
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/v1/other").status_code == 404
    assert c.post("/anything", headers=GOOD, content=b"{}").status_code == 404
    assert c.get("/v1/systemone").status_code == 405


def test_unreachable_upstream_is_502_with_cors(c):
    def down(req): raise httpx.ConnectError("down")
    proxy.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    r = c.post("/v1/systemone", content=b"{}", headers=GOOD)
    assert r.status_code == 502 and r.headers["access-control-allow-origin"] == GOOD["Origin"]


def test_upstream_429_passes_status_and_retry_after(c):
    def limited(req): return httpx.Response(429, json={"error": "rate"}, headers={"Retry-After": "7"})
    proxy.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(limited))
    r = c.post("/v1/systemone", content=b"{}", headers=GOOD)
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert r.headers["access-control-allow-origin"] == GOOD["Origin"]


def test_timeout_is_504(c):
    def slow(req): raise httpx.ReadTimeout("slow")
    proxy.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    r = c.post("/v1/systemone", content=b"{}", headers=GOOD)
    assert r.status_code == 504 and r.json()["error"] == "upstream timeout"


def test_key_and_body_are_never_logged(c, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        c.post("/v1/systemone", content=b'{"state":{"secret_column":"sensitive-value"}}', headers=GOOD)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "sk-test" not in text and "sensitive-value" not in text and "secret_column" not in text
