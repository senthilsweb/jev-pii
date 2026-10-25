# jev-pii

PII and compliance discovery in plain SQL. A DuckDB Python UDF, `jev_pii()`, asks
[TypeSafe Jev](https://typesafe.ai) several **typed** questions about one column and returns a struct, so
every answer becomes a column in a catalog table, the bronze-to-silver pattern.

Where `prompt()` (MotherDuck) and `ai_parse` (Databricks) return free text, Jev returns a schema:

| question | Jev type | catalog column |
|---|---|---|
| is it personal data? | `noul` (0..1) | `sensitive` |
| which kind? | `choice` + probability per option | `category`, `category_conf`, `runner_up` |
| does GDPR / CCPA-CPRA / HIPAA / LGPD / PIPEDA / DPDP apply? | one `noul` each | `regime_*`, `regimes` |
| what to do about it | **not a model answer**, policy in code | `action`: keep / mask / hash / drop / review |

One call per **column** (a reproducible 20-value sample), then a deeper 60-value pass on the weak ones.
If Jev fails the answer is `NULL` and the column goes to `review`; the pipeline never stops.

```sql
SELECT table_name, column_name, r.*
FROM (SELECT *, jev_pii(table_name, column_name, samples) AS r FROM column_samples);
```

## Try it in the browser

`index.html` runs the same Python function inside DuckDB in WebAssembly (Pyodide), no install: **https://www.senthilsweb.com/jev-pii/**
Mock mode works anywhere. Live mode needs the Jev API to allow browser origins (CORS); the page has a one-click check.

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
On the job-scout control, 16 of 18 columns stayed `keep`; `employment_type` was flagged by mistake.

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
