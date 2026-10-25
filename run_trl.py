"""Score jev_pii on a slice of trl-lab/contextual-sensitive-data, queried in place from Hugging Face.

    python run_trl.py [N]      # N columns, default 100; reproducible slice, results are cached

CC BY-NC 4.0 data: read over hf://, never copied into this repo.
"""
import ast
import sys
import duckdb
import pandas as pd
from jev_pii import make_client, register
from jev_pii.catalog import build_catalog, sample_columns  # noqa: F401  (build_catalog used below)
from jev_pii.client import load_dotenv

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
SRC = "hf://datasets/trl-lab/contextual-sensitive-data/train_data_personal.csv"
# their label -> our category, for the labels we have an equivalent for
MAP = {"PERSON_NAME": "PERSON_NAME", "EMAIL_ADDRESS": "EMAIL_ADDRESS", "PHONE_NUMBER": "PHONE_NUMBER",
       "STREET_ADDRESS": "PHYSICAL_ADDRESS", "ZIPCODE": "ZIP_POSTAL_CODE", "DATE_OF_BIRTH": "DATE_OF_BIRTH",
       "GENDER": "GENDER"}

load_dotenv(".env")
jev = make_client()
print("client:", jev.name, jev.model, "| slice:", N)
con = duckdb.connect()
rows = con.sql(f"""SELECT column_name, records, NULLIF(output, 'None') AS truth FROM '{SRC}'
                   ORDER BY hash(column_name || records) LIMIT {N}""").fetchall()

def values(rec):
    try:
        v = [str(x) for x in ast.literal_eval(rec) if str(x).strip()]
    except (ValueError, SyntaxError):
        v = []
    return list(dict.fromkeys(v))[:20]

con.execute("CREATE TABLE column_samples (table_name VARCHAR, column_name VARCHAR, ordinal INTEGER, "
            "data_type VARCHAR, n_distinct BIGINT, samples VARCHAR[])")
for i, (name, rec, truth) in enumerate(rows):
    v = values(rec)
    con.execute("INSERT INTO column_samples VALUES ('unknown', ?, ?, 'VARCHAR', ?, ?)", [name, i, len(v), v])
con.execute("CREATE TABLE truth AS SELECT * FROM (VALUES " + ",".join(
    f"({i}, {('NULL' if t is None else repr(t))})" for i, (_, _, t) in enumerate(rows)) + ") t(ordinal, truth)")
register(con, jev)
build_catalog(con)
df = con.sql("SELECT c.*, t.truth FROM column_catalog c JOIN truth t USING (ordinal)").df()
df["is_pii"] = df["truth"].notna()
df["pred"] = (df["sensitive"].fillna(0) >= 0.5) & (df["category"].fillna("NONE") != "NONE")

def prf(d):
    tp, fp, fn = int((d.pred & d.is_pii).sum()), int((d.pred & ~d.is_pii).sum()), int((~d.pred & d.is_pii).sum())
    p, r = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    return {"n": len(d), "pii": int(d.is_pii.sum()), "tp": tp, "fp": fp, "fn": fn,
            "precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / max(p + r, 1e-9), 3)}

print("\nALL           ", prf(df))
fl = df.assign(pred=df.action.isin(["mask", "hash", "drop", "review"]))
print("FLAGGED/REVIEW", prf(fl), "  <- acted on or sent to a human, never silently kept")
print("  ...without ID", prf(fl[fl.truth != "GENERIC_ID"]))
no_id = df[df.truth != "GENERIC_ID"]
print("WITHOUT ID    ", prf(no_id))
print("GENERIC_ID    ", prf(df[(df.truth == "GENERIC_ID") | ~df.is_pii]))
m = df[df.truth.isin(MAP)]
print("category acc (mappable labels):", round(float((m.category == m.truth.map(MAP)).mean()), 3), f"on {len(m)} columns")
print("unclassified (Jev failed -> review):", int(df.sensitive.isna().sum()))
print("\nFALSE POSITIVES:"); print(df[df.pred & ~df.is_pii][["column_name", "category", "sensitive"]].round(2).to_string(index=False))
print("\nMISSES:"); print(df[~df.pred & df.is_pii][["column_name", "truth", "category", "sensitive"]].round(2).to_string(index=False))
print("\nwrong category on caught PII:"); w = m[m.pred & (m.category != m.truth.map(MAP))]
print(w[["column_name", "truth", "category"]].to_string(index=False))
print(f"\n{jev.calls} calls, {jev.cache_hits} cache hits, {jev.input_tokens} in / {jev.output_tokens} out tokens")
df.to_csv(".cache/trl_slice_results.csv", index=False)
con.close()
