"""Bronze -> catalog -> deep dive -> silver -> score, over the seven Claimwise seed tables."""
import sys
import duckdb
from jev_pii import make_client, register
from jev_pii.catalog import build_catalog, build_silver, deep_dive, sample_columns
from jev_pii.client import load_dotenv
from jev_pii.score import score

TABLES = ["users", "encounters", "claims", "activities", "collections", "facilities", "health_plans"]
load_dotenv(".env")      # TYPESAFE_API_KEY=...; git-ignored
jev = make_client()
print("client:", jev.name, jev.model)
con = duckdb.connect()
for t in TABLES:
    con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('data/raw/{t}.csv')")
register(con, jev, redact="--redact" in sys.argv)
sample_columns(con, TABLES)
build_catalog(con)
print("pass 1:", score(con))
print("deep dive:", deep_dive(con))
build_silver(con, TABLES)
print("pass 2:", score(con))
print(con.sql("SELECT table_name, column_name, category, round(sensitive,2) s, round(category_conf,2) c, runner_up, "
              "regimes, action "
              "FROM column_catalog WHERE sensitive >= 0.3 OR sensitive IS NULL ORDER BY table_name, ordinal"))
print("calls", jev.calls, "cache", jev.cache_hits, "tokens", jev.input_tokens, jev.output_tokens)
con.close()
