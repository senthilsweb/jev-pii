# jev-pii

Typed answers from [TypeSafe Jev](https://typesafe.ai) inside DuckDB SQL, as extra columns. Where a prompt-style
function returns text you then parse, Jev answers a schema: a 0 to 1 score (`noul`) or one option from a list (`choice`),
so every answer is a column you can filter, join and count. The built-in question set finds personal data and the
privacy regimes that apply; you can also ask your own.

## Two ways to call it

**Per row: wrap your query, get the answers as extra columns.**

```sql
SELECT t.*, unnest(jev_pii_row(t))
FROM (SELECT name, dateOfBirth, specialty
      FROM 'https://.../users.csv' LIMIT 20) t;
-- the same 20 rows and 3 columns, plus sensitive, category, category_conf, runner_up, regime_gdpr, ...
```

One Jev call per row. The function only runs on rows that survive your `LIMIT`; a per-query cap (Settings, default 100)
stops a missing `LIMIT` from spending your quota. Use `jev_ask(t, '<questions json>')` for your own questions:

```sql
SELECT t.*, unnest(json_transform(
  jev_ask(t, '{"is_person":{"type":"noul","instructions":"Probability this row describes a real individual."}}'),
  '{"is_person":"DOUBLE"}'))
FROM (SELECT name, dateOfBirth FROM 'https://.../users.csv' LIMIT 10) t;
```

**Per column: profile a column from a sample (a catalog).**

```sql
-- one column, as a one-row table with a column per answer
SELECT * FROM jev_pii_table('users', 'contactInfo',
  (SELECT list(DISTINCT CAST(contactInfo AS VARCHAR))[:20] FROM users));

-- many columns: jev_pii() returns the same answers as one struct value; r.* expands it
SELECT table_name, column_name, r.*
FROM (SELECT *, jev_pii(table_name, column_name, samples) AS r FROM column_samples);
```

The column form is one call per **column** (a reproducible 20-value sample), then a deeper 60-value pass on the weak
ones, and feeds a policy (keep / mask / hash / drop / review) that is plain code, not a model answer. If Jev fails the
answer is `NULL` and the column goes to `review`; the pipeline never stops.

| question | Jev type | answer column |
|---|---|---|
| is it personal data? | `noul` (0..1) | `sensitive` |
| which kind? | `choice` + probability per option | `category`, `category_conf`, `runner_up` |
| does GDPR / CCPA-CPRA / HIPAA / LGPD / PIPEDA / DPDP apply? | one `noul` each | `regime_*` |

## Try it in the browser

**https://www.senthilsweb.com/jev-pii/** is a SQL studio (table list, CodeMirror editor, results grid) running DuckDB
and the same Python `jev_pii()` function in WebAssembly (Pyodide, in a Web Worker), no install.

- **Add data** with `+`: a CSV, Parquet or JSON URL, a `hf://datasets/...` file, or a file from your computer.
  You can also query a URL directly (`SELECT * FROM 'https://.../x.parquet'`) or open `?url=...`.
  The browser fetches the bytes (the host must allow CORS; GitHub raw and Hugging Face do) and DuckDB reads them
  from memory. Nothing is uploaded.
- **Discover PII**: the scan icon next to a table profiles every column (a sample of values, one Jev call per column)
  into `pii_catalog`; the mask icon creates `silver_<table>` with the policy applied.
- **On open**, the editor holds a query that makes **one Jev call per row** with four questions in it, each about a different
  column of a clinician list (name, date of birth, contact, specialty), and returns the answers as aliased `jev_*` columns.
  It runs with the mock client, so it never spends calls unasked. Example 2 adds four deliberately broken rows so you can
  see only the matching question drop.
- **Help** (the `?` icon): every function with a one-line explanation and code you can copy or put in the editor.
- **Settings** (the gear icon, a sheet that slides down): mock or live, endpoint, model, your TypeSafe key and/or proxy secret, sample size, shapes-only mode,
  and an opt-in "remember on this device". The Jev API does not allow browser origins yet, so live mode needs a
  [`proxy/`](proxy/) endpoint (`ghcr.io/senthilsweb/jev-proxy`); `?endpoint=` overrides the default.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python data/fetch.py            # Claimwise seeds + job-scout parquet
echo 'TYPESAFE_API_KEY=...' > .env        # git-ignored; without it a keyword mock runs
.venv/bin/jupyter lab pii_discovery.ipynb # or: .venv/bin/python run_claimwise.py
.venv/bin/python -m pytest tests
```

`jev_pii.udf.register(con, jev, redact=True)` sends value *shapes* (`(999)999-9999`) instead of values.

## Results (Claimwise, 50 columns, jev-1.14.0, one run)

40 columns scored, 10 `ambiguous` excluded. Precision 0.86, recall 0.86, category accuracy 0.86.
`users.contactInfo` (a person's phone) was kept sensitive while `facilities.contactInfo` (a business
line) was not. One false positive (`facilities.location`) and one miss (`activities.procedureCode`).
On the job-scout control, 17 of 18 columns scored below 0.5; `employment_type` (0.60) was flagged by mistake, and `location` (0.46) and `work_mode` (0.35) sat in the grey zone.

**Read these as a sketch, not a benchmark.** There are only 7 labelled PII columns, the labels are mine,
and the deep-dive pass did not change a single verdict on this data.

## Caveats

- **Regimes are not legal advice.** Whether GDPR or CCPA applies depends on who the data subjects are and
  on thresholds a column sample cannot see; each question states its assumption. `config/compliance_matrix.yaml`
  (from `agent-pii-discovery`) is the answer key the notebook scores against, not a source of truth.
- `regime_agreement_with_matrix` is only ~0.69: `noul` regime scores are soft (0.5 to 0.9 across the board)
  and Jev puts CCPA on health-record IDs that the matrix does not. Treat disagreements as review items.
- Claimwise is synthetic Faker data: no real personal data, and `notes` columns hold no seeded PII yet.
- Data subjects' US state is **not** modelled yet.

Data: [claimwise](https://github.com/senthilsweb/claimwise) seeds and the job-scout `ats_raw_trends.parquet`.
