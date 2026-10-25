"""Bronze -> column catalog -> silver. All set-based SQL; Jev is called once per column."""
import yaml

# Policy is deterministic code, not a model answer: the AI picks the category, the rules
# decide what to do about it (same split as the bank-statements pipeline).
ACTIONS = {
    "drop": {"GOVERNMENT_ID_SSN", "GOVERNMENT_ID_NATIONAL", "GOVERNMENT_ID_DRIVER_LICENSE",
             "CREDIT_CARD_NUMBER", "LOGIN_CREDENTIAL"},
    "hash": {"HEALTH_RECORD_ID", "FINANCIAL_ACCOUNT_NUMBER", "DEVICE_IDENTIFIER", "IP_ADDRESS"},
    "mask": {"PERSON_NAME", "EMAIL_ADDRESS", "PHONE_NUMBER", "PHYSICAL_ADDRESS", "ZIP_POSTAL_CODE",
             "DATE_OF_BIRTH", "GENDER", "HEALTH_CONDITION", "EMPLOYMENT_INFO", "OTHER_SENSITIVE"},
}
SENSITIVE_AT = 0.5       # sensitive score at or above this counts as personal data
REVIEW_BELOW = 0.8       # category confidence below this goes back for a deeper look


def action_for(category: str, sensitive: float | None) -> str:
    if sensitive is None:
        return "review"
    if sensitive < SENSITIVE_AT:
        # a specific category with a low score is a disagreement too: never keep it silently
        return "keep" if category == "NONE" else "review"
    if category == "NONE":       # the two answers disagree: a person decides, not a default
        return "review"
    return next((a for a, cats in ACTIONS.items() if category in cats), "mask")


def sample_columns(con, tables, n=20, seed="jev"):
    """One row per column: a reproducible sample of n distinct non-null values."""
    con.execute("CREATE OR REPLACE TABLE column_samples "
                "(table_name VARCHAR, column_name VARCHAR, ordinal INTEGER, data_type VARCHAR, "
                "n_distinct BIGINT, samples VARCHAR[])")
    for t in tables:
        for col, typ, pos in con.execute(f"SELECT column_name, data_type, ordinal_position FROM information_schema.columns "
                                    f"WHERE table_name = '{t}' ORDER BY ordinal_position").fetchall():
            q = f'CAST("{col}" AS VARCHAR)'
            con.execute(f"""
                INSERT INTO column_samples
                SELECT '{t}', '{col}', {pos}, '{typ}', count(DISTINCT v),
                       list(v ORDER BY hash(v || '{seed}'))[:{n}]
                FROM (SELECT {q} AS v FROM {t} WHERE "{col}" IS NOT NULL)""")


def build_catalog(con):
    """column_samples -> column_catalog: the original columns plus one per Jev answer."""
    con.execute("""
        CREATE OR REPLACE TABLE column_catalog AS
        SELECT table_name, column_name, ordinal, data_type, n_distinct, r.*, '' AS action, []::VARCHAR[] AS regimes
        FROM (SELECT *, jev_pii(table_name, column_name, samples) AS r FROM column_samples)""")
    _apply_policy(con)


def _apply_policy(con, catalog="column_catalog"):
    # NULL answer = Jev failed or was unsure: park the column for a human, never guess.
    con.execute(f"UPDATE {catalog} SET action = CASE WHEN sensitive IS NULL THEN 'review' "
                "ELSE policy_action(category, sensitive) END")
    # Regimes are only reported for columns we act on: a noul answer is never exactly zero.
    con.execute(f"""UPDATE {catalog} SET regimes = CASE WHEN action IN ('mask', 'hash', 'drop') THEN
        list_filter([CASE WHEN regime_gdpr >= 0.5 THEN 'GDPR' END, CASE WHEN regime_ccpa_cpra >= 0.5 THEN 'CCPA_CPRA' END,
                     CASE WHEN regime_hipaa >= 0.5 THEN 'HIPAA' END, CASE WHEN regime_lgpd >= 0.5 THEN 'LGPD' END,
                     CASE WHEN regime_pipeda >= 0.5 THEN 'PIPEDA' END, CASE WHEN regime_dpdp_india >= 0.5 THEN 'DPDP_INDIA' END],
                    lambda x: x IS NOT NULL) ELSE []::VARCHAR[] END""")


def deep_dive(con, n=60):
    """Re-ask low-confidence columns with a bigger sample; replace their catalog rows."""
    weak = con.execute(f"""
        SELECT table_name, column_name FROM column_catalog
        WHERE sensitive IS NULL OR category_conf < {REVIEW_BELOW}
           OR sensitive BETWEEN 0.3 AND 0.7""").fetchall()
    for t, col in weak:
        q = f'CAST("{col}" AS VARCHAR)'
        con.execute(f"DELETE FROM column_catalog WHERE table_name = '{t}' AND column_name = '{col}'")
        con.execute(f"""
            INSERT INTO column_catalog
            SELECT table_name, column_name, ordinal, data_type, n_distinct, r.*, '', []::VARCHAR[]
            FROM (SELECT table_name, column_name, ordinal, data_type, n_distinct,
                         jev_pii(table_name, column_name,
                                 (SELECT list(v ORDER BY hash(v || 'deep'))[:{n}]
                                  FROM (SELECT DISTINCT {q} AS v FROM {t} WHERE "{col}" IS NOT NULL)))
                         AS r
                  FROM column_samples WHERE table_name = '{t}' AND column_name = '{col}')""")
    _apply_policy(con)
    return weak


def build_silver(con, tables, catalog="column_catalog"):
    """CREATE VIEW silver_<t>: each column kept, masked, hashed or dropped by the catalog."""
    expr = {"keep": '"{c}"', "mask": "'***' AS \"{c}\"", "hash": 'md5(CAST("{c}" AS VARCHAR)) AS "{c}"',
            "review": "NULL AS \"{c}\""}
    for t in tables:
        cols = []
        for c, a in con.execute(f"SELECT column_name, action FROM {catalog} WHERE table_name = ? "
                                "ORDER BY ordinal", [t]).fetchall():
            if a != "drop":
                cols.append(expr[a].format(c=c))
        con.execute(f"CREATE OR REPLACE VIEW silver_{t} AS SELECT {', '.join(cols)} FROM {t}")


def discover(con, table, n=20, progress=None):
    """Profile one table: sample each column, ask Jev, append the answers to pii_catalog.

    Safe to repeat: the table's earlier rows are replaced. `progress(done, total, column)` is
    called after every column so a UI can show it. Returns the number of columns profiled.
    """
    sample_columns(con, [table], n)
    cols = [r[0] for r in con.execute("SELECT column_name FROM column_samples ORDER BY ordinal").fetchall()]
    exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'pii_catalog'").fetchone()[0] > 0
    if exists:
        con.execute("DELETE FROM pii_catalog WHERE table_name = ?", [table])
    select = """SELECT table_name, column_name, ordinal, data_type, n_distinct, r.*, '' AS action,
                       []::VARCHAR[] AS regimes
                FROM (SELECT *, jev_pii(table_name, column_name, samples) AS r
                      FROM column_samples WHERE column_name = ?)"""
    for i, col in enumerate(cols):
        if exists:
            con.execute(f"INSERT INTO pii_catalog {select}", [col])
        else:
            con.execute(f"CREATE TABLE pii_catalog AS {select}", [col])
            exists = True
        if progress:
            progress(i + 1, len(cols), col)
    _apply_policy(con, "pii_catalog")
    return len(cols)


def load_matrix(path="config/compliance_matrix.yaml"):
    return yaml.safe_load(open(path))["mappings"]
