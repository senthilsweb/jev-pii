import io
import json
import urllib.error

import duckdb
import pytest

from jev_pii import JevClient, JevError, MockJev, flatten, pii_questions, register
from jev_pii.catalog import action_for, build_catalog, build_silver, deep_dive, discover, sample_columns
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


def test_proxy_secret_is_sent_and_key_is_optional():
    seen = []

    def opener(req, timeout=0):
        seen.append(dict(req.header_items()))
        return Resp(json.dumps(reply()).encode())
    c = JevClient(None, api_url="https://proxy.example/v1/systemone", proxy_secret="s" * 20,
                  cache_dir=None, opener=opener)
    c.ask({"column": "x"}, pii_questions())
    assert seen[0].get("X-proxy-secret") == "s" * 20 and "Authorization" not in seen[0]


def test_register_twice_swaps_the_client():
    con = make_con(MockJev())

    class Fixed(MockJev):
        def ask(self, state, questions):
            a = super().ask(state, questions)
            a["category"]["choice"] = "GENDER"
            return a
    register(con, Fixed())
    assert con.execute("SELECT jev_pii('t','name',['a']).category").fetchone() == ("GENDER",)
    con.close()


def test_discover_is_incremental_and_repeatable_per_table():
    con = make_con(MockJev())
    con.execute("CREATE TABLE other AS SELECT * FROM (VALUES ('x@y.com', 1)) t(email, n)")
    steps = []
    assert discover(con, "people", progress=lambda d, t, c: steps.append((d, t, c))) == 3
    assert steps[-1] == (3, 3, "score")
    discover(con, "other")
    discover(con, "people")                      # again: replaces people's rows, keeps other's
    got = con.execute("SELECT table_name, count(*) FROM pii_catalog GROUP BY 1 ORDER BY 1").fetchall()
    assert got == [("other", 2), ("people", 3)]
    actions = dict(con.execute("SELECT column_name, action FROM pii_catalog").fetchall())
    assert actions["name"] == "mask" and actions["score"] == "keep" and actions["n"] == "keep"
    build_silver(con, ["people"], catalog="pii_catalog")
    assert con.execute("SELECT name, score FROM silver_people LIMIT 1").fetchone() == ("***", 10)
    con.close()


def test_table_macro_returns_a_rowset_with_a_column_per_answer():
    con = make_con(MockJev())
    cur = con.execute("SELECT * FROM jev_pii_table('t', 'phone', ['(555) 555-0101', '(555) 555-0102'])")
    cols = [d[0] for d in cur.description]
    row = cur.fetchall()
    assert len(row) == 1 and cols[:5] == ["sensitive", "category", "category_conf", "runner_up", "runner_up_p"]
    assert dict(zip(cols, row[0]))["category"] == "PHONE_NUMBER"
    # real data as the sample, the way a user would write it
    got = con.execute("SELECT category FROM jev_pii_table('people', 'name', "
                      "(SELECT list(DISTINCT CAST(name AS VARCHAR))[:20] FROM people))").fetchall()
    assert got == [("PERSON_NAME",)]
    # re-registering (mock <-> live) keeps the macro working
    register(con, MockJev())
    assert con.execute("SELECT count(*) FROM jev_pii_table('t', 'x', ['1'])").fetchall() == [(1,)]
    con.close()


class Counting(MockJev):
    def __init__(self):
        self.seen = []

    def ask(self, state, questions):
        self.seen.append((state, questions))
        return super().ask(state, questions)


def people_con(client, **kw):
    con = duckdb.connect()
    con.execute("CREATE TABLE people AS SELECT * FROM (VALUES ('Ann Lee','1965-02-10','Cardiology'),"
                "('Bo Ray','1990-01-01','Oncology'),('Cy Fox','1980-05-05','Radiology')) t(name, dateOfBirth, specialty)")
    return con, register(con, client, **kw)


def test_row_function_keeps_the_original_columns_and_adds_one_per_answer():
    c = Counting()
    con, _ = people_con(c)
    cur = con.execute("SELECT t.*, unnest(jev_pii_row(t)) FROM "
                      "(SELECT name, dateOfBirth, specialty FROM people LIMIT 2) t")
    cols, rows = [d[0] for d in cur.description], cur.fetchall()
    assert cols[:3] == ["name", "dateOfBirth", "specialty"] and "sensitive" in cols and "category" in cols
    assert len(rows) == 2 and len(cols) == 3 + 11
    assert len(c.seen) == 2                                     # 2 rows, 11 output columns: still only 2 calls
    assert c.seen[0][0] == {"record": {"name": "Ann Lee", "dateOfBirth": "1965-02-10", "specialty": "Cardiology"}}
    assert "record" in c.seen[0][1]["sensitive"]["instructions"]
    con.close()


def test_row_function_only_sends_the_selected_columns():
    c = Counting()
    con, _ = people_con(c)
    con.execute("SELECT unnest(jev_pii_row(t)) FROM (SELECT name FROM people LIMIT 1) t").fetchall()
    assert list(c.seen[0][0]["record"]) == ["name"]
    con.close()


def test_row_calls_are_capped_per_query_and_reset():
    c = Counting()
    con, budget = people_con(c, max_calls=2)
    q = "SELECT unnest(jev_pii_row(t)) FROM (SELECT * FROM people) t"
    with pytest.raises(Exception, match="Stopped after 2 Jev calls"):
        con.execute(q).fetchall()
    budget.reset()
    assert len(con.execute(q.replace("(SELECT * FROM people)", "(SELECT * FROM people LIMIT 2)")).fetchall()) == 2
    con.close()


def test_row_function_redact_sends_shapes_not_values():
    c = Counting()
    con, _ = people_con(c, redact=True)
    con.execute("SELECT unnest(jev_pii_row(t)) FROM (SELECT name, dateOfBirth FROM people LIMIT 1) t").fetchall()
    state = c.seen[0][0]
    assert "record" not in state and state["record_shapes"] == {"name": "Aaa Aaa", "dateOfBirth": "9999-99-99"}
    con.close()


def test_row_function_fails_open_to_nulls():
    class Down:
        def ask(self, *a): raise JevError("down")
    con, _ = people_con(Down())
    rows = con.execute("SELECT t.name, r.sensitive FROM (SELECT name, jev_pii_row(people) AS r FROM people LIMIT 2) t "
                       "JOIN (SELECT name, jev_pii_row(people) AS r FROM people LIMIT 2) u USING (name) , "
                       "LATERAL (SELECT u.r AS r) r").fetchall() if False else \
        con.execute("SELECT unnest(jev_pii_row(t)) FROM (SELECT name FROM people LIMIT 2) t").fetchall()
    assert all(all(v is None for v in row) for row in rows) and len(rows) == 2
    con.close()


def test_generic_ask_with_your_own_questions_expands_to_typed_columns():
    seen = []

    class Fake:
        def ask(self, state, questions):
            seen.append(questions)
            return {"is_personal": {"type": "noul", "noul": 0.97},
                    "kind": {"type": "choice", "choice": "NAME", "confidence": 0.88, "probabilities": {}}}
    con, _ = people_con(Fake())
    qs = ('{"is_personal":{"type":"noul","instructions":"Is this a person?"},'
          '"kind":{"type":"choice","instructions":"Which kind?","criteria":{"NAME":"a name","NONE":"none"}}}')
    cur = con.execute(f"SELECT t.name, unnest(json_transform(jev_ask(t, '{qs}'), "
                      "'{\"is_personal\":\"DOUBLE\",\"kind\":\"VARCHAR\",\"kind_conf\":\"DOUBLE\"}')) "
                      "FROM (SELECT name FROM people LIMIT 1) t")
    cols, rows = [d[0] for d in cur.description], cur.fetchall()
    assert cols == ["name", "is_personal", "kind", "kind_conf"] and rows == [("Ann Lee", 0.97, "NAME", 0.88)]
    assert seen[0]["is_personal"]["instructions"] == "Is this a person?"
    con.close()


def test_the_same_row_twice_costs_one_call():
    c = Counting()
    con, _ = people_con(c)
    con.execute("CREATE TABLE dup AS SELECT * FROM (VALUES ('Ann','x'),('Ann','x'),('Ann','x')) t(a, b)")
    live = JevClient("k", cache_dir=None, opener=opener_from([reply()]))      # only ONE canned reply: a second call would raise
    register(con, live)
    con.execute("SELECT unnest(jev_pii_row(t)) FROM (SELECT * FROM dup) t").fetchall()
    assert live.calls == 1                                      # one call, however many times DuckDB evaluates the row
    con.close()


def test_policy_table_is_the_same_rule_as_policy_action():
    from jev_pii.catalog import policy_rows
    rows = dict(policy_rows())
    assert rows["NONE"] == "keep" and rows["GOVERNMENT_ID_SSN"] == "drop" and rows["HEALTH_RECORD_ID"] == "hash"
    assert rows["PERSON_NAME"] == "mask" and len(rows) == 20
    for cat, action in rows.items():                       # a confident answer must follow the table exactly
        assert action_for(cat, 0.9 if cat != "NONE" else 0.1) == action
