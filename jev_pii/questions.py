"""The typed questions asked about one column.

Jev answers a fixed schema, never free text: a `noul` is a 0..1 probability, a `choice` is one
option from `criteria` plus a probability per option. That is what makes the output queryable.
Category keys are the canonical entity types of config/compliance_matrix.yaml, so the answer
can be checked against the matrix instead of replacing it.
"""

CATEGORIES = {
    "PERSON_NAME": "a person's name",
    "EMAIL_ADDRESS": "an email address",
    "PHONE_NUMBER": "a personal phone number",
    "PHYSICAL_ADDRESS": "a person's home or mailing address",
    "ZIP_POSTAL_CODE": "a ZIP or postal code",
    "DATE_OF_BIRTH": "a date of birth",
    "GENDER": "gender or sex",
    "GOVERNMENT_ID_SSN": "a social security number",
    "GOVERNMENT_ID_NATIONAL": "a national identity number",
    "GOVERNMENT_ID_DRIVER_LICENSE": "a driver's licence number",
    "FINANCIAL_ACCOUNT_NUMBER": "a bank or financial account number",
    "CREDIT_CARD_NUMBER": "a payment card number",
    "HEALTH_CONDITION": "a diagnosis, procedure, treatment or clinical note about a person",
    "HEALTH_RECORD_ID": "a patient or medical record identifier that links to one person",
    "IP_ADDRESS": "an IP address",
    "DEVICE_IDENTIFIER": "a device or cookie identifier",
    "LOGIN_CREDENTIAL": "a username, password or token",
    "EMPLOYMENT_INFO": "an individual's employment details",
    "OTHER_SENSITIVE": "other personal data about an individual",
    "NONE": "not personal data about an individual: codes, amounts, statuses, dates of "
            "business events, or details of a business, facility or product",
}

# Applicability depends on WHO the data subjects are, which a column cannot tell us, so each
# question states its assumption. Keys match the jurisdictions in compliance_matrix.yaml.
REGIMES = {
    "GDPR": "the people described may be EU residents",
    "CCPA_CPRA": "the people described may be California residents",
    "HIPAA": "the data is held by a US healthcare provider, health plan or their business associate",
    "LGPD": "the people described may be Brazilian residents",
    "PIPEDA": "the people described may be Canadian residents",
    "DPDP_INDIA": "the people described may be residents of India",
}


def pii_questions(scope: str = "column") -> dict:
    """The typed questions. scope="column" asks about a column from a sample; scope="row" asks about one record."""
    row = scope == "row"
    thing = "record" if row else "column"
    q = {
        "sensitive": {
            "type": "noul",
            "instructions": ("Probability that this record identifies or describes an individual person. "
                             if row else
                             "Probability that this column holds personal data that identifies or "
                             "describes an individual person. ") +
                            "Business or facility details, internal codes, amounts and statuses are not personal data.",
        },
        "category": {
            "type": "choice",
            "instructions": ("Which kind of personal data in this record is the most identifying? Choose NONE if "
                             "the record has none." if row else
                             "Which kind of personal data does this column hold? Choose NONE if "
                             "it is not personal data about an individual."),
            "criteria": CATEGORIES,
        },
    }
    for key, assumption in REGIMES.items():
        q[f"regime_{key}"] = {
            "type": "noul",
            "instructions": f"Probability that {key} obligations apply to handling this {thing}, "
                            f"assuming {assumption}. Zero if the {thing} is not personal data.",
        }
    return q


def flatten_generic(answers: dict) -> dict:
    """Any question set -> one flat object: a noul becomes a number, a choice becomes its label plus <q>_conf."""
    out = {}
    for name, a in answers.items():
        if "noul" in a:
            out[name] = float(a["noul"])
        elif "choice" in a:
            out[name] = a["choice"]
            if "confidence" in a:
                out[f"{name}_conf"] = float(a["confidence"])
        else:
            out[name] = a
    return out


def flatten(answers: dict) -> dict:
    """Jev's answer object -> one flat row, one value per question (plus the runner-up)."""
    cat = answers["category"]
    ranked = sorted(cat.get("probabilities", {}).items(), key=lambda kv: -kv[1])
    runner = next(((k, p) for k, p in ranked if k != cat["choice"]), ("", 0.0))
    out = {
        "sensitive": float(answers["sensitive"]["noul"]),
        "category": cat["choice"],
        "category_conf": float(cat["confidence"]),
        "runner_up": runner[0],
        "runner_up_p": float(runner[1]),
    }
    for key in REGIMES:
        out[f"regime_{key.lower()}"] = float(answers[f"regime_{key}"]["noul"])
    return out


# The struct DuckDB returns, in the same order as flatten().
SCHEMA = {
    "sensitive": "DOUBLE", "category": "VARCHAR", "category_conf": "DOUBLE",
    "runner_up": "VARCHAR", "runner_up_p": "DOUBLE",
    **{f"regime_{k.lower()}": "DOUBLE" for k in REGIMES},
}
