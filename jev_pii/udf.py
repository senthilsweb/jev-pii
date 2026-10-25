"""jev_pii(): a DuckDB Python UDF that asks Jev typed questions about one column."""
import re

import duckdb
from duckdb import sqltypes as T

from .client import JevError
from .catalog import action_for
from .questions import SCHEMA, flatten, pii_questions

_TYPES = {"DOUBLE": T.DOUBLE, "VARCHAR": T.VARCHAR}


def shape(value: str) -> str:
    """'(257)667-2991' -> '(999)999-9999': the format survives, the value does not."""
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", value)))[:40]


def register(con, client, redact=False):
    """Registers jev_pii(table, column, samples) -> STRUCT of one field per question.

    redact=True sends value shapes instead of values, for data you may not send out.
    Any Jev failure returns NULL (fail open): the column lands in the review queue, the
    pipeline keeps going.
    """
    questions = pii_questions()
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

    for name in ("jev_pii", "policy_action"):        # re-registering swaps the client (mock <-> live)
        try:
            con.remove_function(name)
        except duckdb.Error:
            pass
    con.create_function("jev_pii", jev_pii, [T.VARCHAR, T.VARCHAR, duckdb.list_type(T.VARCHAR)],
                        struct, side_effects=True, null_handling="special")
    con.create_function("policy_action", action_for, [T.VARCHAR, T.DOUBLE], T.VARCHAR)
