"""
Emit the constraint register from the form itself.

The register is not maintained as a separate document. build_xlsform.py carries a
`note=(id, question, prevents, source)` tuple beside every rule it writes, and this
script walks the same module and dumps them alongside the literal expression that
was compiled. A rule that is removed from the form disappears from the register;
a rule added without a justification tuple shows up in the "UNJUSTIFIED" list at
the bottom and is treated as a build failure. The register therefore cannot drift
from the deployed instrument, which is the usual failure mode of hand-written
constraint documentation.

Run:  python extract_registers.py
Writes ../docs/02_constraint_register.csv and .md
"""

from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.normpath(os.path.join(HERE, "..", "docs"))
sys.path.insert(0, HERE)

import build_xlsform as B  # noqa: E402  (importing runs the form definition)

TYPE_OF = {
    "constraint": "Hard constraint (blocks)",
    "relevant": "Relevance / skip",
}

# Rules that live in the form but are enforced by structure rather than by a
# constraint expression, plus the ones enforced outside the instrument.
STRUCTURAL = [
    ("C-STR-1", "3.02 / Section 4", "Section 4 nested inside the roster repeat",
     "The child module opens automatically for every roster member aged 9-59 months and cannot be "
     "opened for anyone else. The number of modules and the number of eligible children are the same "
     "number by construction, not by a check that a clerk might skip.",
     "A child present in the roster but never interviewed, or a module completed for an ineligible child",
     "Design decision. Replaces the paper form's 'photocopy additional Section 4 pages and number them "
     "in sequence', which is where the link between page and child is lost."),
    ("C-STR-2", "4.01-4.04", "Child identity carried from the roster, not re-keyed",
     "Line number, name, age in months and sex are calculates reading the roster row.",
     "Transcription error between the roster and the child module - the paper form asks for all four to "
     "be copied by hand, and a mis-copied age changes eligibility and the specimen cut",
     "Design decision, defect D-20."),
    ("C-STR-3", "5.01", "Specimen eligibility computed, not asked",
     "5.01 is a calculate over the roster age; the enumerator sees it but cannot key it.",
     "A child of 11 months being given a specimen label, or a child of 13 months being skipped",
     "Questionnaire 5.01 states the 12-month cut. Removing the keystroke removes the error."),
    ("C-STR-4", "1.02-1.04", "Administrative hierarchy served as cascading external selects",
     "LGA -> ward -> settlement, each filtered by the code chosen above it, from attached CSVs.",
     "Settlements attributed to the wrong ward or LGA, and free-text settlement names that cannot be "
     "joined to the sample frame",
     "lgas.csv / wards.csv / settlements.csv. See docs/05_settlement_serving.md."),
    ("C-STR-5", "Section 8", "Office-use section removed",
     "8.01-8.03 (form received date, data entry clerk code, second entry verification) are not implemented.",
     "Meaningless fields being filled with meaningless values",
     "There is no data entry step and no double entry in a digital instrument. Defect D-19."),
    ("C-STR-6", "1.13", "Non-consenting households withheld from the device",
     "prev_households.csv is filtered to consent_to_follow_up = yes before it is attached to the form.",
     "Re-linking this round's data to 342 households that declined follow-up",
     "previous_round_households.csv consent_to_follow_up. See docs/11_data_protection.md DP-3."),
]

OUT_COLS = ["id", "applies_to", "form_field", "enforcement", "rule_as_deployed",
            "what_it_prevents", "source_or_basis"]


def main() -> int:
    rows = []
    by_name = {}
    for r in B.survey:
        by_name.setdefault(r["name"], r)

    for n in B.CONSTRAINT_NOTES:
        row = by_name.get(n["name"], {})
        if row.get("constraint"):
            enforcement = "Hard constraint (blocks submission)"
            rule = row["constraint"]
        elif row.get("choice_filter"):
            enforcement = "Choice filter (illegal options are never shown)"
            rule = f"{row['type']}  WHERE  {row['choice_filter']}"
        elif row.get("relevant"):
            enforcement = "Relevance (question is asked only when true)"
            rule = row["relevant"]
        elif row.get("calculation"):
            enforcement = "Derived (computed, never keyed)"
            rule = row["calculation"]
        else:
            enforcement = "Structural (no expression to bypass)"
            rule = row.get("type", "")
        # A gate question that only appears while two values disagree is a soft
        # warning in appearance but a hard block in effect - say so.
        if row.get("relevant") and row.get("constraint"):
            enforcement = "Hard gate (appears only on failure, cannot be dismissed)"
            rule = f"WHEN {row['relevant']}  THEN REQUIRE {row['constraint']}"
        rows.append({
            "id": n["id"],
            "applies_to": n["question"],
            "form_field": n["name"],
            "enforcement": enforcement,
            "rule_as_deployed": " ".join(rule.split()),
            "what_it_prevents": n["prevents"],
            "source_or_basis": n["source"],
        })

    for sid, applies, field, rule, prevents, source in STRUCTURAL:
        rows.append({
            "id": sid, "applies_to": applies, "form_field": field,
            "enforcement": "Structural (no expression to bypass)",
            "rule_as_deployed": rule, "what_it_prevents": prevents, "source_or_basis": source,
        })

    rows.sort(key=lambda r: (r["id"].startswith("C-STR"), r["id"]))

    os.makedirs(DOCS, exist_ok=True)
    csv_path = os.path.join(DOCS, "02_constraint_register.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    # unjustified rules -> build failure
    justified = {n["name"] for n in B.CONSTRAINT_NOTES}
    trivial = ("gate", "_confirm", "_reason", "_other", "_ns", "_altname", "_pin")
    unjustified = [
        r["name"] for r in B.survey
        if r.get("constraint") and r["name"] not in justified
    ]

    n_judgement = sum(1 for r in rows if "udgement" in r["source_or_basis"])
    n_data = sum(1 for r in rows if ".csv" in r["source_or_basis"])
    n_quest = sum(1 for r in rows if "uestionnaire" in r["source_or_basis"])

    md = [
        "# 02 - Constraint register",
        "",
        "**This file is generated.** Every row is produced by `scripts/extract_registers.py`",
        "from the same object that `scripts/build_xlsform.py` compiles into the XForm. The",
        "`rule_as_deployed` column is the literal expression in the form, not a description of",
        "it. If a rule is edited, removed or added, this file changes on the next build; it",
        "cannot describe a constraint the form does not contain.",
        "",
        f"- Rules registered: **{len(rows)}**",
        f"- Traceable to a supplied data file: **{n_data}**",
        f"- Traceable to the questionnaire itself: **{n_quest}**",
        f"- Stated plainly as my judgement: **{n_judgement}**",
        "",
        "The machine-readable copy is `02_constraint_register.csv`. Wide columns are easier to",
        "read there; the table below is the same content.",
        "",
        "## How to read `enforcement`",
        "",
        "| Value | Meaning in the field |",
        "|---|---|",
        "| Hard constraint (blocks submission) | The enumerator cannot move past the question until the value is legal. |",
        "| Hard gate (appears only on failure, cannot be dismissed) | The question is invisible while the data are consistent. It appears the moment two values disagree, states the disagreement in words, and cannot be answered until the underlying data are corrected. This is how the reconciliation checks are enforced. |",
        "| Choice filter (illegal options are never shown) | The wrong answer is not on the screen. Stronger than a constraint, because there is nothing to reject. |",
        "| Relevance (question is asked only when true) | Skip logic. Prevents data being recorded where it has no meaning. |",
        "| Derived (computed, never keyed) | The value is calculated from data already collected. There is no keystroke to get wrong. |",
        "| Structural (no expression to bypass) | Enforced by the shape of the form. There is no rule to satisfy because the error cannot be expressed. |",
        "",
        "## Register",
        "",
    ]
    md.append("| ID | Applies to | Field | Enforcement | What it prevents | Source or basis |")
    md.append("|---|---|---|---|---|---|")
    for r in rows:
        md.append(
            f"| {r['id']} | {r['applies_to']} | `{r['form_field']}` | {r['enforcement'].split(' (')[0]} "
            f"| {r['what_it_prevents']} | {r['source_or_basis']} |"
        )
    md += [
        "",
        "## Rules as deployed",
        "",
        "The exact expression compiled into the XForm, for audit.",
        "",
    ]
    for r in rows:
        md += [f"**{r['id']}** - {r['applies_to']} (`{r['form_field']}`)", "",
               "```", r["rule_as_deployed"], "```", ""]

    if unjustified:
        md += ["## UNJUSTIFIED RULES - build must fail", ""] + [f"- `{u}`" for u in unjustified]

    with open(os.path.join(DOCS, "02_constraint_register.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print(f"constraint register: {len(rows)} rules -> {csv_path}")
    print(f"  judgement-based {n_judgement} | data-file-based {n_data} | questionnaire-based {n_quest}")
    if unjustified:
        print(f"  UNJUSTIFIED (no register entry): {unjustified}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
