"""Jev client: retries, circuit breaker, on-disk cache, fail-open. Stdlib only.

Pattern lifted from job-pilot's pipeline/jev.py. Without TYPESAFE_API_KEY, make_client()
returns MockJev, a keyword heuristic, so the notebook still runs; it is labelled `mock` in
every result and exists only to make the pipeline testable offline, not to be accurate.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.14.0"          # pinned; jev-latest moves under you
RETRY_STATUS = (429, 529)


class JevError(RuntimeError):
    """One call failed or came back unusable."""


class JevClient:
    name = "jev"

    def __init__(self, api_key, model=DEFAULT_MODEL, cache_dir=".cache", timeout=30, api_url=API_URL, proxy_secret=None,
                 max_retries=2, breaker_after=5, opener=urllib.request.urlopen, sleep=time.sleep):
        self._key, self.model, self.timeout, self.url = api_key, model, timeout, api_url
        self._secret = proxy_secret      # only for a jev-proxy that holds the key (see proxy/)
        self.max_retries, self.breaker_after = max_retries, breaker_after
        self._open, self._sleep = opener, sleep
        self._lock = threading.Lock()
        self._errors = 0
        self.calls = self.cache_hits = self.input_tokens = self.output_tokens = 0
        self._dir = Path(cache_dir) / "jev" if cache_dir else None
        if self._dir:
            self._dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, body: bytes):
        return self._dir / (hashlib.sha256(body).hexdigest()[:32] + ".json") if self._dir else None

    def ask(self, state: dict, questions: dict) -> dict:
        """Returns the `answers` object, or raises JevError (callers decide the fallback)."""
        body = json.dumps({"model": self.model, "state": state, "questions": questions},
                          sort_keys=True).encode()
        path = self._cache_path(body)
        if path and path.exists():
            with self._lock:
                self.cache_hits += 1
            return json.loads(path.read_text())["answers"]
        if self._errors >= self.breaker_after:
            raise JevError("circuit open: too many consecutive errors")
        for attempt in range(self.max_retries + 1):
            headers = {"Content-Type": "application/json"}
            if self._key:
                headers["Authorization"] = f"Bearer {self._key}"
            if self._secret:
                headers["X-Proxy-Secret"] = self._secret
            req = urllib.request.Request(self.url, data=body, headers=headers)
            try:
                with self._open(req, timeout=self.timeout) as r:
                    reply = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code in RETRY_STATUS and attempt < self.max_retries:
                    self._sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                self._fail(f"HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                self._fail(type(e).__name__)
        answers = reply.get("answers")
        if not isinstance(answers, dict):
            self._fail("no answers in reply")
        usage = reply.get("usage", {})
        with self._lock:
            self._errors = 0
            self.calls += 1
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
        if path:
            path.write_text(json.dumps({"answers": answers}))
        return answers

    def _fail(self, why):
        with self._lock:
            self._errors += 1
        raise JevError(why)      # never includes the key or the request body


class MockJev:
    """Keyword heuristic with Jev's answer shape. NOT a model."""
    name = "mock"
    model = "mock"
    calls = cache_hits = input_tokens = output_tokens = 0
    _HINTS = [
        ("PERSON_NAME", r"\bname\b"), ("DATE_OF_BIRTH", r"birth|dob"),
        ("PHONE_NUMBER", r"phone|contact|tel"), ("EMAIL_ADDRESS", r"mail"),
        ("GOVERNMENT_ID_SSN", r"ssn"), ("PHYSICAL_ADDRESS", r"address|street"),
        ("HEALTH_CONDITION", r"diagnos|procedure"), ("HEALTH_RECORD_ID", r"patient"),
    ]

    def ask(self, state: dict, questions: dict) -> dict:
        col = state.get("column", "").lower()
        hit = next((k for k, rx in self._HINTS if re.search(rx, col)), "NONE")
        pii = hit != "NONE"
        probs = {k: 0.0 for k in questions["category"]["criteria"]}
        probs[hit] = 0.9
        probs["NONE" if pii else "OTHER_SENSITIVE"] = 0.1
        ans = {"sensitive": {"type": "noul", "noul": 0.9 if pii else 0.1},
               "category": {"type": "choice", "choice": hit, "confidence": 0.9, "probabilities": probs}}
        for k in questions:
            if k.startswith("regime_"):
                ans[k] = {"type": "noul", "noul": 0.8 if pii else 0.05}
        return ans


def load_dotenv(path):
    """Read KEY=VALUE lines into os.environ without overriding what is already set."""
    p = Path(path).expanduser()
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)", line)
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2).strip().strip("'\"")


def make_client(**kw):
    key = os.environ.get("TYPESAFE_API_KEY")
    return JevClient(key, **kw) if key else MockJev()
