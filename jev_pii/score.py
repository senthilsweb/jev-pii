"""Score the catalog against hand-labelled ground truth and the compliance matrix."""
import pandas as pd

from .catalog import SENSITIVE_AT, load_matrix

REGIME_COLS = {"GDPR": "regime_gdpr", "CCPA_CPRA": "regime_ccpa_cpra", "HIPAA": "regime_hipaa",
               "LGPD": "regime_lgpd", "PIPEDA": "regime_pipeda", "DPDP_INDIA": "regime_dpdp_india"}


def score(con, truth_csv="data/ground_truth.csv", matrix_path="config/compliance_matrix.yaml"):
    cat = con.execute("SELECT * FROM column_catalog").df()
    truth = pd.read_csv(truth_csv)
    df = truth.merge(cat, left_on=["table", "column"], right_on=["table_name", "column_name"])
    df["pred"] = df["sensitive"].astype(float).ge(SENSITIVE_AT) & df["category"].ne("NONE")
    scored = df[df["ambiguous"] == 0]
    tp = int((scored["pred"] & (scored["is_pii"] == 1)).sum())
    fp = int((scored["pred"] & (scored["is_pii"] == 0)).sum())
    fn = int((~scored["pred"] & (scored["is_pii"] == 1)).sum())
    p, r = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    pii = scored[scored["is_pii"] == 1]
    matrix = load_matrix(matrix_path)
    agree = total = 0
    for _, row in pii[pii["category"] == pii["true_category"]].iterrows():
        expected = set(matrix.get(row["true_category"], []))
        for reg, col in REGIME_COLS.items():
            total += 1
            agree += (row[col] >= 0.5) == (reg in expected)
    return {
        "columns_scored": len(scored), "ambiguous_excluded": int((df["ambiguous"] == 1).sum()),
        "precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / max(p + r, 1e-9), 3),
        "category_accuracy": round(float((pii["category"] == pii["true_category"]).mean()), 3) if len(pii) else None,
        "regime_agreement_with_matrix": round(agree / total, 3) if total else None,
        "false_positives": scored[scored["pred"] & (scored["is_pii"] == 0)][["table", "column", "category"]].values.tolist(),
        "misses": scored[~scored["pred"] & (scored["is_pii"] == 1)][["table", "column", "true_category"]].values.tolist(),
    }
