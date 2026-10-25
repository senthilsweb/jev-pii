"""Download the public demo data into data/raw/ (git-ignored)."""
import urllib.request
from pathlib import Path

RAW = Path(__file__).parent / "raw"
CLAIMWISE = "https://raw.githubusercontent.com/senthilsweb/claimwise/main/dbt-pipeline/seeds/healthcare_data/{}.csv"
JOB_SCOUT = "https://raw.githubusercontent.com/senthilsweb/ai-agents/main/agents/job-scout/data/ats_raw_trends.parquet"

RAW.mkdir(exist_ok=True)
for t in ["users", "encounters", "claims", "activities", "collections", "facilities", "health_plans"]:
    urllib.request.urlretrieve(CLAIMWISE.format(t), RAW / f"{t}.csv")
urllib.request.urlretrieve(JOB_SCOUT, RAW / "ats_raw_trends.parquet")
print("ok ->", RAW)
