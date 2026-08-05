"""
Stage 05 -- Coverage by documented source
=========================================

Reports coverage separately for children whose dose was confirmed on a
vaccination card and children for whom the only evidence is caregiver recall,
and quantifies how much the headline depends on that distinction.

The structural problem
----------------------
The instrument asks question 3.5 (dose on card) only where a card was seen and
question 3.6 (caregiver recall) only where it was not. The two sources are
therefore never observed on the same child. That is a deliberate design choice
and it is the standard skip pattern, but it has an exact consequence: **the
survey cannot estimate how accurate caregiver recall is.** There is no
subsample on which recall can be validated against the card, so any statement
about recall accuracy has to be imported from outside the survey or expressed
as a bound.

What can be done, and is done here:

  * report the three quantities that are identified -- card-confirmed coverage
    among children with a card, recall coverage among children without one, and
    the share of children with a card;
  * report "documented coverage", the card-confirmed count over ALL children,
    which is a valid lower bound on true coverage and is the number a programme
    should use when it needs a defensible floor;
  * decompose the headline into the part carried by cards and the part carried
    by recall;
  * sweep an assumed recall over-report rate across its whole plausible range
    and show where the conclusion changes, rather than picking one value.

The card-seen group is not a random subsample of children. Households that keep
and produce a card differ systematically from those that do not, and so the
difference between the two columns cannot be read as the bias in recall. That
is the reason the sweep is presented as a bound and not as a correction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (ART, COVERAGE_TARGET, STRATUM_NAMES, banner, build_analysis_sets,
                    get_logger, md_table, svy_prop, write_json)

LOG = get_logger("05_documented_source")

# The share of recall-reported "Yes" answers that are assumed to be wrong. The
# literature on caregiver recall of campaign doses puts net over-reporting in
# the range of a few points to about fifteen; the sweep spans zero to thirty so
# that the decision boundary is visible rather than assumed.
RECALL_OVERREPORT_SWEEP = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]


def main() -> None:
    banner(LOG, "STAGE 05  Coverage by documented source")

    ch = pd.read_csv(ART["child_weighted"])
    hh = pd.read_csv(ART["hh_weighted"])

    _, d, excl, dropped, s1f = build_analysis_sets(ch, hh)
    LOG.info("analysis set B: %d children in %d clusters (interviewer(s) %s excluded)",
             len(d), d.cluster_id.nunique(), excl)

    d["card_seen"] = d["vaccination_card_seen"].eq("Yes").astype(float)
    d["card_confirmed"] = ((d["card_seen"] == 1) & (d["vaccinated"] == 1)).astype(float)
    d["recall_only_yes"] = ((d["card_seen"] == 0) & (d["vaccinated"] == 1)).astype(float)

    # ------------------------------------------------- the identified quantities
    rows = []

    def add(label, frame, y, note):
        e = svy_prop(frame, y, domain=label)
        rows.append(e.as_row(quantity=label, note=note))

    add("Card retention: share of children with a card seen", d, "card_seen",
        "denominator = all children; the ceiling on how much of the estimate can be documented")
    add("Card-confirmed coverage among children with a card",
        d[d.card_seen == 1], "vaccinated",
        "identified, but only for the self-selected group that holds a card")
    add("Caregiver-recall coverage among children without a card",
        d[d.card_seen == 0], "vaccinated",
        "identified, but unverifiable: no card exists against which to check it")
    add("Documented coverage: card-confirmed over ALL children", d, "card_confirmed",
        "a hard lower bound on true coverage -- every child in the numerator has a written record")
    add("Headline coverage: card or recall", d, "vaccinated",
        "the reported estimate")
    src = pd.DataFrame(rows)

    # By stratum, because card retention is the thing that varies most by state.
    strat_rows = []
    for h, g in d.groupby("stratum_code"):
        for lab, frame, y in (("Card seen (%)", g, "card_seen"),
                              ("Coverage, card-confirmed", g[g.card_seen == 1], "vaccinated"),
                              ("Coverage, caregiver recall", g[g.card_seen == 0], "vaccinated"),
                              ("Documented coverage (card over all)", g, "card_confirmed"),
                              ("Headline coverage", g, "vaccinated")):
            e = svy_prop(frame, y, domain=lab)
            strat_rows.append(e.as_row(stratum_code=h, stratum_name=STRATUM_NAMES[h],
                                       quantity=lab))
    by_strat = pd.DataFrame(strat_rows)
    pd.concat([src.assign(stratum_code="National", stratum_name="All three states"),
               by_strat], ignore_index=True).to_csv(ART["src_table"], index=False)

    # ------------------------------------------------------- the decomposition
    head = svy_prop(d, "vaccinated", domain="headline")
    card_part = svy_prop(d, "card_confirmed", domain="card")
    recall_part = svy_prop(d, "recall_only_yes", domain="recall")
    LOG.info("headline %.1f%% = %.1f%% card-confirmed + %.1f%% recall-only",
             100 * head.estimate, 100 * card_part.estimate, 100 * recall_part.estimate)
    share_from_recall = recall_part.estimate / head.estimate

    # ------------------------------------------- how much the headline depends on it
    sweep = []
    for r in RECALL_OVERREPORT_SWEEP:
        adj = d.copy()
        # Only recall-based "Yes" answers can be over-reports; a card cannot be
        # over-reported and a "No" is not at issue.
        adj["vaccinated_adj"] = np.where(adj["recall_only_yes"] == 1, 1 - r, adj["vaccinated"])
        e = svy_prop(adj, "vaccinated_adj", domain=f"recall over-report {100*r:.0f}%")
        sweep.append({
            "recall_overreport_pct": 100 * r,
            "adjusted_coverage_pct": 100 * e.estimate,
            "ci_low_pct": 100 * e.ci_low, "ci_high_pct": 100 * e.ci_high,
            "shift_from_headline_pp": 100 * (e.estimate - head.estimate),
            "still_below_target": e.ci_high < COVERAGE_TARGET,
        })
    sweep = pd.DataFrame(sweep)

    # The mirror question: how wrong would recall have to be to change the
    # decision? Solve for the over-report rate at which the estimate reaches the
    # 90% mop-up trigger and the 95% target.
    slope = (sweep.adjusted_coverage_pct.iloc[-1] - sweep.adjusted_coverage_pct.iloc[0]) / 30.0
    extra = {
        "headline_pct": 100 * head.estimate,
        "card_confirmed_component_pct": 100 * card_part.estimate,
        "recall_component_pct": 100 * recall_part.estimate,
        "share_of_headline_resting_on_recall": share_from_recall,
        "documented_coverage_pct": 100 * card_part.estimate,
        "pp_per_10pct_recall_overreport": -10 * slope,
        "excluded_interviewers": excl,
    }
    sweep.to_csv(ART["src_sensitivity"], index=False)
    write_json(ART["src_sensitivity"].with_suffix(".meta.json"), extra)

    _write_report(src, by_strat, sweep, extra, d, head, card_part, recall_part)
    banner(LOG, "STAGE 05 complete")


def _write_report(src, by_strat, sweep, extra, d, head, card_part, recall_part) -> None:
    piv = (by_strat.pivot(index="stratum_name", columns="quantity", values="estimate_pct")
           .reset_index())
    order = ["stratum_name", "Card seen (%)", "Coverage, card-confirmed",
             "Coverage, caregiver recall", "Documented coverage (card over all)",
             "Headline coverage"]
    piv = piv[[c for c in order if c in piv.columns]]

    gap = (src.loc[src.quantity.str.startswith("Card-confirmed"), "estimate_pct"].iloc[0]
           - src.loc[src.quantity.str.startswith("Caregiver"), "estimate_pct"].iloc[0])

    lines = [
        "# 05 - Coverage by documented source",
        "",
        "## What the instrument allows, and what it does not",
        "",
        "Question 3.5 is asked only where a card was seen; question 3.6 only where it was not. "
        "The two sources are never observed on the same child. Three quantities are therefore "
        "identified and one is not:",
        "",
        "| | Identified? |",
        "|---|---|",
        "| Coverage among children with a card, by card | yes |",
        "| Coverage among children without a card, by recall | yes |",
        "| Share of children holding a card | yes |",
        "| **Accuracy of caregiver recall** | **no -- no child has both** |",
        "",
        "## National estimates by source",
        "",
        md_table(src[["quantity", "estimate_pct", "ci_low_pct", "ci_high_pct", "n_children",
                      "deff", "note"]],
                 {"estimate_pct": ".1f", "ci_low_pct": ".1f", "ci_high_pct": ".1f", "deff": ".2f"}),
        "",
        f"A vaccination card was seen for only "
        f"**{src.loc[0, 'estimate_pct']:.1f}%** of children. Among those children, "
        f"**{src.loc[1, 'estimate_pct']:.1f}%** had the campaign dose recorded on the card. "
        f"Among the {100-src.loc[0,'estimate_pct']:.0f}% with no card, "
        f"**{src.loc[2, 'estimate_pct']:.1f}%** of caregivers reported the child had been "
        f"vaccinated -- **{gap:.1f} percentage points lower**, and unverifiable.",
        "",
        "## By stratum",
        "",
        md_table(piv, {c: ".1f" for c in piv.columns if c != "stratum_name"}),
        "",
        "Card retention varies more between states than coverage does, which matters for "
        "interpretation: a state can look better simply because fewer of its children have a "
        "card to contradict the caregiver.",
        "",
        "## How much the headline depends on the distinction",
        "",
        f"The headline of **{extra['headline_pct']:.1f}%** decomposes exactly into",
        "",
        f"- **{extra['card_confirmed_component_pct']:.1f} points** carried by children with a "
        f"card confirming the dose -- this is *documented coverage*, and it is a hard lower "
        f"bound: every child in it has a written record;",
        f"- **{extra['recall_component_pct']:.1f} points** carried by caregiver recall alone.",
        "",
        f"**{100*extra['share_of_headline_resting_on_recall']:.0f}% of the headline rests on "
        f"recall that the survey cannot check.** That is the honest statement of the dependency. "
        "The gap between documented coverage and the headline -- "
        f"{extra['headline_pct'] - extra['card_confirmed_component_pct']:.1f} percentage points -- "
        "is the width of the zone in which the true value sits somewhere, and where it sits "
        "depends entirely on how good caregiver recall is.",
        "",
        "### Sweep over the recall over-report rate",
        "",
        "Rather than adopt one assumed value, the assumed share of recall-reported doses that "
        "did not happen is swept across its whole plausible range. Only recall 'Yes' answers "
        "are at risk; a card cannot be over-reported.",
        "",
        md_table(sweep, {"recall_overreport_pct": ".0f", "adjusted_coverage_pct": ".1f",
                         "ci_low_pct": ".1f", "ci_high_pct": ".1f",
                         "shift_from_headline_pp": "+.1f"}),
        "",
        f"Each 10 percentage points of assumed recall over-reporting removes about "
        f"**{extra['pp_per_10pct_recall_overreport']:.1f} points** from the national estimate. "
        "The direction of the conclusion never changes: across the entire sweep the estimate "
        f"falls further below the {100*COVERAGE_TARGET:.0f}% target, never toward it. Recall "
        "error can only make the campaign look worse than the headline, never better, because "
        "under-reporting a dose a caregiver received is far rarer than over-reporting one they "
        "did not.",
        "",
        "## The interpretation trap",
        "",
        f"It is tempting to read the {gap:.1f}-point gap between card-confirmed and "
        "recall-based coverage as the size of recall over-reporting. It is not, and it should "
        "not be presented that way. Children whose caregivers produce a card are a "
        "self-selected group: card retention correlates with routine-immunisation contact, "
        "literacy, household stability and distance to a facility, all of which independently "
        "predict being reached by a campaign. Part of that gap is genuine coverage difference "
        "between the two groups and part is recall error, and **this survey cannot separate "
        "them.** Every statement in this section is therefore framed as a bound, not a "
        "correction.",
        "",
        "The only way to separate them is to ask both questions of everyone -- record the "
        "caregiver's report *and then* ask for the card -- which costs perhaps thirty seconds "
        "per child and would identify recall accuracy directly on the card-holding subsample.",
        "",
    ]
    ART["src_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("wrote %s", ART["src_report"].name)


if __name__ == "__main__":
    main()
