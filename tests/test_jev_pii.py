import io
import json
import urllib.error

import duckdb
import pytest

from jev_pii import JevClient, JevError, MockJev, flatten, pii_questions, register
from jev_pii.catalog import action_for, build_catalog, build_silver, deep_dive, sample_columns
from jev_pii.udf import shape


def reply(category="PHONE_NUMBER", sensitive=0.95):
    probs = {k: 0.0 for k in pii_questions()["category"]["criteria"]}
    probs[category] = 0.9
    probs["NONE"] = 0.1
    ans = {"sensitive": {"type": "noul", "noul": sensitive},
           "category": {"type": "choice", "choice": category, "confidence": 0.9, "probabilities": probs}}
    for k in pii_questions():
        if k.startswith("regime_"):
            ans[k] = {"type": "noul", "noul": 0.7}
    return {"answers": ans, "usage": {"input_tokens": 400, "output_tokens": 60}}


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def opener_from(seq):
    it = iter(seq)

    def opener(req, timeout=0):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return Resp(json.dumps(item).encode())
    return opener


def http_error(code, retry_after=None):
    h = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("u", code, "x", h, None)


def client(seq, **kw):
    return JevClient("sk-test", cache_dir=None, sleep=lambda s: None, opener=opener_from(seq), **kw)


def test_flatten_keeps_runner_up_and_every_regime():
    flat = flatten(reply()["answers"])
    assert flat["category"] == "PHONE_NUMBER" and flat["runner_up"] == "NONE"
    assert {"regime_gdpr", "regime_ccpa_cpra", "regime_hipaa"} <= flat.keys()


def test_retries_rate_limit_then_succeeds():
    c = client([http_error(429, 1), reply()])
    assert "sensitive" in c.ask({"column": "x"}, pii_questions()) and c.calls == 1


def test_error_message_never_leaks_key_or_body():
    c = client([http_error(500)])
    with pytest.raises(JevError) as e:
        c.ask({"column": "secret-column"}, pii_questions())
    assert "sk-test" not in str(e.value) and "secret-column" not in str(e.value)


def test_breaker_opens_after_consecutive_errors():
    c = client([http_error(500)] * 2, breaker_after=2)
    for _ in range(2):
        with pytest.raises(JevError):
            c.ask({"column": "x"}, pii_questions())
    with pytest.raises(JevError, match="circuit open"):
        c.ask({"column": "x"}, pii_questions())


def test_cache_makes_repeat_calls_free(tmp_path):
    c = JevClient("k", cache_dir=str(tmp_path), opener=opener_from([reply()]))
    c.ask({"column": "x"}, pii_questions())
    c.ask({"column": "x"}, pii_questions())      # second would raise StopIteration if it hit the API
    assert c.calls == 1 and c.cache_hits == 1


def test_shape_hides_values_keeps_format():
    assert shape("(257)667-2991x154") == "(999)999-9999a999"
    assert "Allison" not in shape("Allison Hill")


@pytest.mark.parametrize("cat,s,expect", [("PERSON_NAME", .9, "mask"), ("GOVERNMENT_ID_SSN", .9, "drop"),
                                          ("HEALTH_RECORD_ID", .9, "hash"), ("NONE", .1, "keep"),
                                          ("PERSON_NAME", None, "review"), ("NONE", .7, "review"), ("ZIP_POSTAL_CODE", .4, "review")])
def test_policy_is_deterministic(cat, s, expect):
    assert action_for(cat, s) == expect


def make_con(jev):
    con = duckdb.connect()
    con.execute("CREATE TABLE people AS SELECT * FROM (VALUES ('Ann Lee','(555) 555-0101',10),"
                "('Bo Ray','(555) 555-0102',20)) t(name, phone, score)")
    register(con, jev)
    return con


def test_catalog_end_to_end_with_mock_and_silver_masking():
    con = make_con(MockJev())
    sample_columns(con, ["people"])
    build_catalog(con)
    got = dict(con.execute("SELECT column_name, action FROM column_catalog").fetchall())
    assert got == {"name": "mask", "phone": "mask", "score": "keep"}
    build_silver(con, ["people"])
    assert con.execute("SELECT name, phone, score FROM silver_people LIMIT 1").fetchone() == ("***", "***", 10)
    con.close()


def test_jev_failure_fails_open_to_review_queue():
    class Down:
        def ask(self, *a): raise JevError("down")
    con = make_con(Down())
    sample_columns(con, ["people"])
    build_catalog(con)
    assert {r[0] for r in con.execute("SELECT action FROM column_catalog").fetchall()} == {"review"}
    build_silver(con, ["people"])      # still builds; unclassified columns are nulled, not leaked
    assert con.execute("SELECT name FROM silver_people LIMIT 1").fetchone() == (None,)
    con.close()


def test_deep_dive_revisits_only_weak_columns():
    class Mixed(MockJev):
        def ask(self, state, questions):
            a = super().ask(state, questions)
            if state["column"] == "phone":
                a["category"]["confidence"] = 0.5
            return a
    con = make_con(Mixed())
    sample_columns(con, ["people"])
    build_catalog(con)
    assert deep_dive(con) == [("people", "phone")]
    con.close()


def test_endpoint_is_configurable():
    seen = []

    def opener(req, timeout=0):
        seen.append(req.full_url)
        return Resp(json.dumps(reply()).encode())
    c = JevClient("k", api_url="https://proxy.example/jev", cache_dir=None, opener=opener)
    c.ask({"column": "x"}, pii_questions())
    assert seen == ["https://proxy.example/jev"]
