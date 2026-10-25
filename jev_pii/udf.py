"""jev_pii(): a DuckDB Python UDF that asks Jev typed questions about one column."""
import json
import re

import duckdb
from duckdb import sqltypes as T

from .client import JevError
from .catalog import action_for
from .questions import SCHEMA, flatten, flatten_generic, pii_questions

_TYPES = {"DOUBLE": T.DOUBLE, "VARCHAR": T.VARCHAR}


def shape(value: str) -> str:
    """'(257)667-2991' -> '(999)999-9999': the format survives, the value does not."""
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", value)))[:40]


class Budget:
    """Caps Jev calls made by the row-level functions, so a missing LIMIT cannot spend a whole quota."""

    def __init__(self, max_calls=None):
        self.max_calls, self.used = max_calls, 0
        # DuckDB evaluates a function once per output column when its struct is expanded (unnest), so the
        # same row arrives N times. Remember the answer per query: the call is made, and counted, once.
        self.memo = {}

    def reset(self):
        self.used = 0
        self.memo.clear()

    def spend(self):
        self.used += 1
        if self.max_calls is not None and self.used > self.max_calls:
            raise RuntimeError(f"Stopped after {self.max_calls} Jev calls in one query. Add a LIMIT, or raise the cap.")


def register(con, client, redact=False, max_calls=None):
    """Attaches the Jev functions to a DuckDB connection.

    Per row (what you add to a SELECT):  SELECT t.*, unnest(jev_pii_row(t)) FROM (<your query>) t
      jev_pii_row(t)              the PII questions, answers as columns added to each row
      jev_ask(t, '<questions>')   your own typed questions; returns JSON, expand it with json_transform
    Per column (a catalog):
      jev_pii(table, column, samples), jev_pii_table(...)

    redact=True sends value shapes instead of values. max_calls caps row-level calls per query (the returned
    Budget's reset() starts a new query). Any Jev failure returns NULL (fail open); the pipeline keeps going.
    """
    questions = pii_questions()
    row_questions = pii_questions("row")
    budget = Budget(max_calls)

    def record(js):
        """One row as JSON text -> the state sent to Jev. Values are cut to 120 chars; redact sends shapes."""
        rec = {}
        for k, v in json.loads(js).items():
            v = None if v is None else str(v)[:120]
            rec[k] = shape(v) if (redact and v is not None) else v
        return {"record_shapes" if redact else "record": rec}

    def jev_pii_json(js):
        if js is None:
            return None
        key = ("pii", js)
        if key not in budget.memo:
            budget.spend()
            try:
                budget.memo[key] = flatten(client.ask(record(js), row_questions))
            except (JevError, KeyError, TypeError, ValueError):
                budget.memo[key] = None
        return budget.memo[key]

    def jev_ask_json(js, qjs):
        if js is None or qjs is None:
            return None
        key = ("ask", js, qjs)
        if key not in budget.memo:
            budget.spend()
            try:
                budget.memo[key] = json.dumps(flatten_generic(client.ask(record(js), json.loads(qjs))))
            except (JevError, KeyError, TypeError, ValueError):
                budget.memo[key] = None
        return budget.memo[key]

    struct = duckdb.struct_type({k: _TYPES[v] for k, v in SCHEMA.items()})

    def jev_pii(table, column, samples):
        samples = [s[:120] for s in (samples or [])]
        state = {"table": table, "column": column}
        state["sample_shapes" if redact else "sample_values"] = (
            [shape(s) for s in samples] if redact else samples)
        try:
            return flatten(client.ask(state, questions))
        except (JevError, KeyError, TypeError):
            return None

    for name in ("jev_pii", "policy_action", "jev_pii_json", "jev_ask_json"):        # re-registering swaps the client (mock <-> live)
        try:
            con.remove_function(name)
        except duckdb.Error:
            pass
    con.create_function("jev_pii", jev_pii, [T.VARCHAR, T.VARCHAR, duckdb.list_type(T.VARCHAR)],
                        struct, side_effects=True, null_handling="special")
    con.create_function("jev_pii_json", jev_pii_json, [T.VARCHAR], struct, side_effects=True, null_handling="special")
    con.create_function("jev_ask_json", jev_ask_json, [T.VARCHAR, T.VARCHAR], T.VARCHAR, side_effects=True,
                        null_handling="special")
    # Row level. `t` is a table alias, so the whole row goes in: jev_pii_row(t), jev_ask(t, '<questions json>')
    con.execute("CREATE OR REPLACE MACRO jev_pii_row(r) AS jev_pii_json(to_json(r))")
    con.execute("CREATE OR REPLACE MACRO jev_ask(r, questions) AS jev_ask_json(to_json(r), questions)")
    con.create_function("policy_action", action_for, [T.VARCHAR, T.DOUBLE], T.VARCHAR)
    # The scalar returns ONE struct value. This table macro returns the same answers as a one-row table with a
    # column per answer, which is what `SELECT * FROM ...` users expect.
    con.execute("CREATE OR REPLACE MACRO jev_pii_table(tbl, col, samples) AS TABLE "
                "SELECT r.* FROM (SELECT jev_pii(tbl, col, samples) AS r)")
    return budget
