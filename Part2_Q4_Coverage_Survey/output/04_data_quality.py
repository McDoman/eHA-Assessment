"""
Stage 04 -- Data quality assessment
===================================

Uses the fieldwork log and the responses together. Four families of check:

  1. Digit preference and age heaping in the reported age in months, and digit
     preference in the reported interview duration.
  2. Interviewer-level outliers in reported coverage, interview duration, card
     sighting and completion, with a pre-declared falsification screen.
  3. Implausible response and effort patterns, including patterns that are only
     visible when the fieldwork log and the household file are read together.
  4. The pattern of missingness -- unit and item -- and whether it is plausibly
     ignorable.

The screening rules are declared in `common.py`, not here, so that the decision
rule is fixed before the data are looked at rather than fitted to them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from common import (AGE_MAX_MONTHS, AGE_MIN_MONTHS, ART, FLAG_CARD_RATIO, FLAG_COMPLETION_MIN,
                    FLAG_COVERAGE_MIN, FLAG_DURATION_RATIO, FLAG_EXCLUDE_AT, HEAP_ANCHORS,
                    SRC, STRATUM_NAMES, banner, falsification_screen, get_logger, md_table)

LOG = get_logger("04_data_quality")


# --------------------------------------------------------------------------
# 1. Digit preference and age heaping
# --------------------------------------------------------------------------


def age_heaping(ch: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Age is reported in completed months, so the classical Whipple and Myers
    indices (built for ages in years ending in 0 and 5) do not transfer
    directly. The analogue used here is the distribution of age modulo 12: under
    a smooth age distribution across a 51-month eligible range, each residue
    should hold about 1/12 of children. Heaping onto whole years shows up as an
    excess at residue 0.
    """
    inr = ch[ch["age_months"].between(AGE_MIN_MONTHS, AGE_MAX_MONTHS)]
    n = len(inr)

    resid = inr["age_months"].mod(12).value_counts(normalize=True).reindex(range(12), fill_value=0)
    myers = float((100 * resid - 100 / 12).abs().sum() / 2)

    obs = inr["age_months"].value_counts().reindex(range(AGE_MIN_MONTHS, AGE_MAX_MONTHS + 1),
                                                  fill_value=0).sort_index()
    anchors = [a for a in HEAP_ANCHORS if AGE_MIN_MONTHS <= a <= AGE_MAX_MONTHS]
    obs_anchor = int(obs.loc[anchors].sum())
    exp_anchor = n * len(anchors) / (AGE_MAX_MONTHS - AGE_MIN_MONTHS + 1)
    heap_index = obs_anchor / exp_anchor

    # Chi-square against a uniform age distribution over the eligible range.
    chi2, p = stats.chisquare(obs.to_numpy())

    # How much of the sample would have to move to remove the heaping: the index
    # of dissimilarity between the observed distribution and a smoothed one.
    smooth = obs.rolling(5, center=True, min_periods=1).median()
    smooth = smooth / smooth.sum()
    diss = float((obs / obs.sum() - smooth).abs().sum() / 2)

    tbl = pd.DataFrame({
        "age_months": obs.index, "n_children": obs.to_numpy(),
        "share_pct": 100 * obs.to_numpy() / n,
        "expected_share_pct": 100 / (AGE_MAX_MONTHS - AGE_MIN_MONTHS + 1),
        "is_year_anchor": [a in anchors for a in obs.index],
    })
    # A second, separate signature: a pile-up at the top of the eligible range.
    # Ages are heaped onto whole years everywhere except at 60 months, which is
    # outside the definition -- so children who would have been recorded at 60
    # appear at 59 instead, right at the ceiling that keeps them in scope.
    exp_per_month = n / (AGE_MAX_MONTHS - AGE_MIN_MONTHS + 1)
    ceiling_n = int(obs.loc[AGE_MAX_MONTHS])
    neighbours = float(obs.loc[AGE_MAX_MONTHS - 5:AGE_MAX_MONTHS - 1].mean())

    summary = {
        "n_in_range": n,
        "myers_type_index": myers,
        "year_anchor_index": heap_index,
        "n_at_upper_boundary": ceiling_n,
        "expected_per_month": exp_per_month,
        "boundary_excess_ratio": ceiling_n / neighbours,
        "pct_at_year_anchors": 100 * obs_anchor / n,
        "pct_expected_at_anchors": 100 * len(anchors) / (AGE_MAX_MONTHS - AGE_MIN_MONTHS + 1),
        "chi2_uniform": float(chi2), "chi2_p": float(p),
        "index_of_dissimilarity": diss,
        "n_below_range": int((ch["age_months"] < AGE_MIN_MONTHS).sum()),
        "n_above_range": int((ch["age_months"] > AGE_MAX_MONTHS).sum()),
    }
    return tbl, summary


def duration_digit_preference(hh: pd.DataFrame) -> dict:
    """Terminal-digit preference in a continuous field recorded by the interviewer."""
    comp = hh[hh["is_completed"] == 1]
    last = comp["interview_duration_min"] % 10
    obs = last.value_counts().reindex(range(10), fill_value=0).sort_index()
    chi2, p = stats.chisquare(obs.to_numpy())
    round_share = float(last.isin([0, 5]).mean())
    return {"n": int(len(comp)), "pct_ending_0_or_5": 100 * round_share,
            "pct_expected": 20.0, "chi2": float(chi2), "chi2_p": float(p),
            "distribution": {int(k): int(v) for k, v in obs.items()}}


# --------------------------------------------------------------------------
# 2. Interviewer-level outliers
# --------------------------------------------------------------------------


def interviewer_table(ch: pd.DataFrame, hh: pd.DataFrame, fw: pd.DataFrame) -> pd.DataFrame:
    comp = hh[hh["is_completed"] == 1]
    med_dur = comp["interview_duration_min"].median()
    card_rate = ch["vaccination_card_seen"].eq("Yes").mean()

    rows = []
    for iv, g in ch.groupby("interviewer_id"):
        h = hh[hh["interviewer_id"] == iv]
        hc = h[h["is_completed"] == 1]
        f = fw[fw["interviewer_id"] == iv]
        cov = float(g["vaccinated"].mean(skipna=True))
        n = int(g["vaccinated"].notna().sum())
        rows.append({
            "interviewer_id": iv,
            "team": f["team"].iloc[0] if len(f) else "",
            "n_clusters": h["cluster_id"].nunique(),
            "n_households": len(h),
            "n_children": len(g),
            "completion_rate": h["is_completed"].mean(),
            "median_duration_min": hc["interview_duration_min"].median(),
            "mean_duration_min": hc["interview_duration_min"].mean(),
            "sd_duration_min": hc["interview_duration_min"].std(),
            "card_seen_rate": g["vaccination_card_seen"].eq("Yes").mean(),
            "reported_coverage": cov,
            "children_per_household": len(g) / max(len(hc), 1),
            "days_worked": f["fieldwork_date"].nunique(),
            "max_households_in_a_day": int(f["households_attempted"].max()) if len(f) else np.nan,
            "max_clusters_in_a_day": int(f["clusters_worked"].max()) if len(f) else np.nan,
            "spot_checked_days": int(f["supervisor_spot_check"].eq("Yes").sum()),
            "gps_verified_days": int(f["gps_verified"].eq("Yes").sum()),
            "_n_cov": n,
        })
    t = pd.DataFrame(rows)

    # Robust z-scores against the median and the median absolute deviation, so
    # that the outlier itself does not inflate the yardstick that judges it.
    for col in ["reported_coverage", "median_duration_min", "card_seen_rate", "completion_rate"]:
        med = t[col].median()
        mad = 1.4826 * (t[col] - med).abs().median()
        t[f"z_{col}"] = (t[col] - med) / mad if mad > 0 else np.nan

    # Pre-declared falsification rules, scored by the shared screen in common.py
    # so that stages 03, 04, 05 and 06 cannot drift apart.
    t = t.merge(falsification_screen(ch, hh), on="interviewer_id", how="left")
    return t.drop(columns=["_n_cov"]).sort_values("rules_triggered", ascending=False)


# --------------------------------------------------------------------------
# 3. Implausible patterns
# --------------------------------------------------------------------------


def implausible_patterns(ch: pd.DataFrame, hh: pd.DataFrame, fw: pd.DataFrame) -> pd.DataFrame:
    comp = hh[hh["is_completed"] == 1]
    rows = []

    def add(pattern, n, denom, where, why, action):
        rows.append({"pattern": pattern, "n": n, "denominator": denom,
                     "share_pct": 100 * n / denom if denom else np.nan,
                     "where": where, "why_it_matters": why, "action": action})

    with_kids = comp[comp["children_enumerated"] > 0]
    add("Completed interview under 5 minutes with at least one child enumerated",
        int((with_kids["interview_duration_min"] < 5).sum()), len(with_kids),
        ", ".join(sorted(with_kids.loc[with_kids["interview_duration_min"] < 5,
                                       "interviewer_id"].unique())),
        "the instrument has a household roster, an asset module and a repeated child block; "
        "under five minutes is not enough time to administer it",
        "contributes to the falsification screen")

    cl = ch.groupby("cluster_id")["vaccinated"].agg(["mean", "size"])
    sat = cl[(cl["mean"] == 1.0)]
    add("Cluster with 100% reported coverage and no variation at all",
        len(sat), len(cl), ", ".join(sat.index),
        "even in a well-run campaign some children are missed; a cluster with literally no "
        "unvaccinated child is a signature of manufactured data",
        "cross-checked against the interviewer screen")

    hhkids = ch.groupby("household_id")["vaccinated"].agg(["mean", "size"])
    multi = hhkids[hhkids["size"] >= 2]
    add("Household with 2+ children, all reported vaccinated", int((multi["mean"] == 1).sum()),
        len(multi), "-",
        "expected under genuinely high coverage with within-household correlation; reported "
        "for completeness, not treated as a defect",
        "no action")

    day = fw[fw["households_attempted"] > 20]
    add("Interviewer-day with more than 20 households attempted", len(day), len(fw),
        ", ".join(f"{r.interviewer_id} {r.fieldwork_date} ({r.households_attempted} hh)"
                  for r in day.itertuples()),
        "the design allocates 20 households per cluster; more than that in a day means either "
        "two clusters in a day or a duplicated upload",
        "one is the duplicated cluster, one is the flagged interviewer")

    multi_cl = fw[fw["clusters_worked"] > 1]
    add("Interviewer-day covering more than one cluster", len(multi_cl), len(fw),
        ", ".join(f"{r.interviewer_id} {r.fieldwork_date} ({r.clusters_worked} clusters)"
                  for r in multi_cl.itertuples()),
        "enumeration areas are geographically separate; covering several in a day, in "
        "different states, is not physically possible",
        "flagged; the five-cluster day is quarantined as a sensitivity variant")

    oow = comp[comp["out_of_window"] == 1]
    add("Interview dated outside the May 2026 fieldwork window", len(oow), len(comp),
        ", ".join(sorted(oow["interview_date"].unique())) + " ("
        + ", ".join(sorted(oow["interviewer_id"].unique())) + ")",
        "a post-campaign survey cannot measure post-campaign status before the campaign; "
        "these are most likely pilot records that reached the production dataset",
        "retained in the headline, excluded in a sensitivity variant (moves it 0.01 points)")

    zero = comp[comp["children_enumerated"] == 0]
    add("Completed interview with no eligible child", len(zero), len(comp), "-",
        "expected and correct: these households contribute to the household response rate and "
        "to the estimated household population, but not to the coverage denominator",
        "no action")

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 4. Missingness
# --------------------------------------------------------------------------


def missingness(ch: pd.DataFrame, hh: pd.DataFrame,
                exclude_clusters: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Unit non-response (no interview) and item non-response (no vaccination
    status). The ignorability question is asked separately for each, because the
    evidence available is different.
    """
    unit_rows = []
    for var in ["stratum_code", "settlement_type"]:
        for lvl, g in hh.groupby(var):
            elig = g[g["is_ineligible_dwelling"] == 0]
            unit_rows.append({
                "type": "Unit (household)", "variable": var, "level": str(lvl),
                "n_eligible": len(elig), "n_responding": int(elig["is_completed"].sum()),
                "response_rate_pct": 100 * elig["is_completed"].mean(),
                "refusal_pct": 100 * elig["result_of_visit"].eq("Refused").mean(),
                "noncontact_pct": 100 * elig["result_of_visit"]
                .eq("No eligible respondent after 3 visits").mean(),
            })
    unit = pd.DataFrame(unit_rows)

    # Item missingness on the outcome, profiled against everything observed.
    item_rows = []
    ch = ch.copy()
    ch["item_missing"] = ch["status_missing"].astype(int)
    for var in ["stratum_code", "settlement_type", "status_source", "sex", "wealth_quintile"]:
        for lvl, g in ch.groupby(var, dropna=False):
            item_rows.append({
                "type": "Item (vaccination status)", "variable": var, "level": str(lvl),
                "n_children": len(g), "n_missing": int(g["item_missing"].sum()),
                "missing_pct": 100 * g["item_missing"].mean(),
            })
    item = pd.DataFrame(item_rows)

    # Is unit non-response ignorable? Two things are testable and one is not.
    #
    #  (a) Testable: does response depend on the design variables that ARE
    #      observed for non-respondents? A logistic model of response on those
    #      variables measures how much of the variation the weight adjustment
    #      can absorb.
    #  (b) Testable indirectly: at cluster level, is the response rate related
    #      to measured coverage? If clusters that were hard to interview are also
    #      clusters where the campaign did less well, non-response is unlikely to
    #      be ignorable, because the same mechanism drives both.
    #  (c) Not testable: whether, *within* a cluster, the households that refused
    #      differ in coverage from those that responded. Nothing in the data can
    #      speak to this, because no outcome was recorded for non-respondents.
    import statsmodels.formula.api as smf

    elig = hh[hh["is_ineligible_dwelling"] == 0].copy()
    elig["listing_size_100"] = elig["L_i"] / 100.0
    elig["day"] = pd.to_datetime(elig["interview_date"]).dt.dayofyear
    m = smf.logit("is_completed ~ C(stratum_code) + C(settlement_type) + listing_size_100 + day",
                  data=elig).fit(disp=0)
    pred = m.predict(elig)

    cl = (ch.groupby("cluster_id")
          .agg(coverage=("vaccinated", "mean"), n_children=("vaccinated", "size")))
    rr = (hh[hh["is_ineligible_dwelling"] == 0].groupby("cluster_id")["is_completed"].mean()
          .rename("response_rate"))
    clus_all = cl.join(rr).dropna()
    rho_all, p_all = stats.spearmanr(clus_all["response_rate"], clus_all["coverage"])

    # The falsified clusters report both saturated coverage and near-perfect
    # completion, so they would manufacture exactly this association. The
    # screened correlation is the one that carries the argument.
    clus = clus_all.drop(index=[c for c in exclude_clusters if c in clus_all.index])
    rho, p_rho = stats.spearmanr(clus["response_rate"], clus["coverage"])
    slope = np.polyfit(clus["response_rate"], clus["coverage"], 1)[0]

    diag = {
        "logit_pseudo_r2": float(m.prsquared),
        "logit_llr_p": float(m.llr_pvalue),
        "predicted_response_range": [float(pred.min()), float(pred.max())],
        "cluster_rr_vs_coverage_spearman": float(rho),
        "cluster_rr_vs_coverage_p": float(p_rho),
        "cluster_rr_vs_coverage_slope": float(slope),
        "cluster_rr_vs_coverage_spearman_unscreened": float(rho_all),
        "cluster_rr_vs_coverage_p_unscreened": float(p_all),
        "n_clusters": int(len(clus)),
        "overall_response_rate": float(hh.loc[hh.is_ineligible_dwelling == 0, "is_completed"].mean()),
        "logit_summary": m.summary2().tables[1].round(4).to_dict(),
    }
    return unit, item, diag


# --------------------------------------------------------------------------


def main() -> None:
    banner(LOG, "STAGE 04  Data quality assessment")

    hh = pd.read_csv(ART["hh_weighted"])
    ch = pd.read_csv(ART["child_weighted"])
    fw = pd.read_csv(SRC["fieldwork"])

    age_tbl, age_sum = age_heaping(ch)
    LOG.info("age heaping: %.1f%% of in-range children sit at 12/24/36/48 months against %.1f%% "
             "expected (index %.1f, Myers-type %.1f)", age_sum["pct_at_year_anchors"],
             age_sum["pct_expected_at_anchors"], age_sum["year_anchor_index"],
             age_sum["myers_type_index"])
    age_tbl.to_csv(ART["dq_age"], index=False)

    dur = duration_digit_preference(hh)
    LOG.info("duration terminal digit: %.1f%% end in 0 or 5 against 20%% expected (chi2 p=%.3f)",
             dur["pct_ending_0_or_5"], dur["chi2_p"])

    iv = interviewer_table(ch, hh, fw)
    excluded = iv.loc[iv.screen_outcome == "EXCLUDE", "interviewer_id"].tolist()
    LOG.warning("falsification screen: %s excluded, %d flagged for review",
                excluded or "none", int((iv.screen_outcome == "review").sum()))
    iv.to_csv(ART["dq_interviewer"], index=False)

    pat = implausible_patterns(ch, hh, fw)
    LOG.info("implausible-pattern checks: %d", len(pat))

    flagged_clusters = sorted(hh.loc[hh.interviewer_id.isin(excluded), "cluster_id"].unique())
    unit, item, mdiag = missingness(ch, hh, flagged_clusters)
    LOG.info("unit response rate %.3f; cluster response rate vs coverage Spearman rho=%.3f "
             "(p=%.4f)", mdiag["overall_response_rate"], mdiag["cluster_rr_vs_coverage_spearman"],
             mdiag["cluster_rr_vs_coverage_p"])
    pd.concat([unit, item], ignore_index=True).to_csv(ART["dq_missing"], index=False)

    flags = _flag_register(age_sum, dur, iv, pat, mdiag, fw, ch)
    flags.to_csv(ART["dq_flags"], index=False)
    LOG.info("quality flag register: %d entries (%d material or critical)", len(flags),
             int(flags.severity.isin(["material", "critical"]).sum()))

    _write_report(age_tbl, age_sum, dur, iv, pat, unit, item, mdiag, flags, fw, ch, hh)
    banner(LOG, "STAGE 04 complete")


def _flag_register(age_sum, dur, iv, pat, mdiag, fw, ch) -> pd.DataFrame:
    excl = iv.loc[iv.screen_outcome == "EXCLUDE", "interviewer_id"].tolist()
    rows = [
        {"flag": "Interviewer falsification", "severity": "critical",
         "evidence": f"interviewer(s) {', '.join(excl)} trigger "
                     f"{int(iv.rules_triggered.max())} of 4 pre-declared rules: median interview "
                     f"{iv.median_duration_min.min():.0f} min against a survey median of "
                     f"{iv.median_duration_min.median():.0f}, "
                     f"{100*iv.reported_coverage.max():.0f}% reported coverage, "
                     f"{100*iv.card_seen_rate.min():.0f}% card sighting against "
                     f"{100*ch['vaccination_card_seen'].eq('Yes').mean():.0f}%, and "
                     f"{100*iv.completion_rate.max():.0f}% completion",
         "affects": "9 clusters, 255 children, spread across all three strata",
         "fixable_by": "better field control",
         "action": "clusters excluded from the headline; retained estimate reported for contrast"},
        {"flag": "Age heaping on whole years", "severity": "material",
         "evidence": f"{age_sum['pct_at_year_anchors']:.1f}% of in-range children are reported at "
                     f"exactly 12, 24, 36 or 48 months against {age_sum['pct_expected_at_anchors']:.1f}% "
                     f"expected -- a {age_sum['year_anchor_index']:.1f}-fold excess; "
                     f"{100*age_sum['index_of_dissimilarity']:.0f}% of the age distribution would "
                     "have to be redistributed to smooth it",
         "affects": "every age-disaggregated estimate; age-group boundaries at 11/12 and 23/24 months",
         "fixable_by": "better instrument",
         "action": "age-group estimates reported with wide boundaries and a stated caveat; "
                   "the headline does not depend on age"},
        {"flag": "Pile-up at the upper eligibility boundary", "severity": "material",
         "evidence": f"{age_sum['n_at_upper_boundary']} children reported at exactly 59 months, "
                     f"{age_sum['boundary_excess_ratio']:.1f}x the average of the five months "
                     "below; 60 months is out of scope, so the whole-year heap lands on the "
                     "last age that keeps a child eligible",
         "affects": "the eligible population definition itself, not just the age variable",
         "fixable_by": "better instrument",
         "action": "quantified and reported; no analytical correction is possible without "
                   "knowing which of these children are genuinely 59 months old"},
        {"flag": "Children enumerated outside the eligible age range", "severity": "minor",
         "evidence": f"{age_sum['n_below_range']} children below 9 months and "
                     f"{age_sum['n_above_range']} above 59 months",
         "affects": "23 records", "fixable_by": "better instrument",
         "action": "excluded from the headline denominator; re-included in sensitivity (0.1 points)"},
        {"flag": "Duplicate cluster submission", "severity": "critical",
         "evidence": "20 households and 20 children in cluster C034 are exact duplicates, and the "
                     "fieldwork log inherits the duplication as a 40-household interviewer-day",
         "affects": "1 cluster", "fixable_by": "better analysis",
         "action": "de-duplicated in stage 01"},
        {"flag": "Out-of-window interviews", "severity": "material",
         "evidence": "18 households dated 2026-04-29, one interviewer, five clusters, three "
                     "states, one day",
         "affects": "18 households", "fixable_by": "better field control",
         "action": "retained; sensitivity variant moves the headline by 0.01 points"},
        {"flag": "Non-ignorable unit non-response", "severity": "material",
         "evidence": f"cluster response rate and cluster coverage are positively associated "
                     f"(Spearman rho = {mdiag['cluster_rr_vs_coverage_spearman']:.2f}, "
                     f"p = {mdiag['cluster_rr_vs_coverage_p']:.4f}); the same conditions that "
                     "made a household hard to interview made it hard to vaccinate",
         "affects": f"{100*(1-mdiag['overall_response_rate']):.0f}% of eligible households",
         "fixable_by": "better instrument (a short non-response form)",
         "action": "within-cluster adjustment applied; residual bias bounded at -2.0 and -3.9 "
                   "points in the stage-03 sensitivity"},
        {"flag": "Card sighting far below the level needed for documented coverage",
         "severity": "material",
         "evidence": f"a vaccination card was seen for only "
                     f"{100*ch['vaccination_card_seen'].eq('Yes').mean():.0f}% of children",
         "affects": "the headline's dependence on unverifiable recall",
         "fixable_by": "better instrument and better programme (card issuing)",
         "action": "quantified in stage 05"},
        {"flag": "Supervisory control too thin to detect falsification",
         "severity": "material",
         "evidence": f"{100*fw['supervisor_spot_check'].eq('Yes').mean():.0f}% of interviewer-days "
                     f"carried a supervisor spot check and "
                     f"{100*fw['gps_verified'].eq('Yes').mean():.0f}% were GPS verified; the "
                     "flagged interviewer's days were GPS verified, so location alone did not "
                     "detect the problem",
         "affects": "the whole survey's assurance", "fixable_by": "better field control",
         "action": "reported as a recommendation for the next round"},
        {"flag": "Interview duration digit preference", "severity": "minor",
         "evidence": f"{dur['pct_ending_0_or_5']:.1f}% of durations end in 0 or 5 against 20% "
                     f"expected (chi-square p = {dur['chi2_p']:.3f})",
         "affects": "duration only, which is not an estimand",
         "fixable_by": "better instrument (timestamped interviews)",
         "action": "none; duration is used only as a falsification signal"},
    ]
    return pd.DataFrame(rows)


def _write_report(age_tbl, age_sum, dur, iv, pat, unit, item, mdiag, flags, fw, ch, hh) -> None:
    excl = iv.loc[iv.screen_outcome == "EXCLUDE", "interviewer_id"].tolist()
    top = age_tbl.sort_values("n_children", ascending=False).head(8)
    ivshow = iv[["interviewer_id", "team", "n_clusters", "n_children", "completion_rate",
                 "median_duration_min", "card_seen_rate", "reported_coverage",
                 "z_reported_coverage", "z_median_duration_min", "rules_triggered",
                 "screen_outcome"]]

    lines = [
        "# 04 - Data quality assessment",
        "",
        "## 4.1 Digit preference and age heaping",
        "",
        f"Age is reported in completed months, so the classical Whipple and Myers indices do not "
        f"transfer directly; the analogue used is the distribution of age modulo 12 over the "
        f"51-month eligible range, where each residue should hold about 8.3% of children.",
        "",
        f"- **{age_sum['pct_at_year_anchors']:.1f}%** of in-range children are reported at exactly "
        f"12, 24, 36 or 48 months, against **{age_sum['pct_expected_at_anchors']:.1f}%** expected "
        f"-- a **{age_sum['year_anchor_index']:.1f}-fold excess**.",
        f"- Myers-type blended index: **{age_sum['myers_type_index']:.1f}** (0 = no preference).",
        f"- Index of dissimilarity against a smoothed distribution: "
        f"**{100*age_sum['index_of_dissimilarity']:.0f}%** of children would have to be moved to "
        f"remove the heaping.",
        f"- Chi-square against a uniform age distribution: chi2 = {age_sum['chi2_uniform']:.0f}, "
        f"p < 0.001.",
        f"- {age_sum['n_below_range']} children below 9 months and {age_sum['n_above_range']} "
        f"above 59 months were enumerated although the instrument excludes them.",
        f"- A second, distinct signature sits at the top of the range: "
        f"**{age_sum['n_at_upper_boundary']} children are reported at exactly 59 months**, "
        f"{age_sum['boundary_excess_ratio']:.1f} times the average of the five months below it. "
        f"Sixty months is outside the definition, so the children who would have been heaped "
        f"there appear at the ceiling that keeps them in scope instead. This is a *selection* "
        f"problem, not only a measurement one: some of those children are five years old and "
        f"should never have been enumerated, and they are old enough to have been covered by a "
        f"previous round.",
        "",
        "Most common reported ages:",
        "",
        md_table(top[["age_months", "n_children", "share_pct", "expected_share_pct",
                      "is_year_anchor"]],
                 {"share_pct": ".1f", "expected_share_pct": ".1f"}),
        "",
        "**What it means.** Caregivers report age in whole years and the interviewer converts. "
        "This is a *measurement* problem in the age variable, not in the outcome: it does not "
        "bias the headline, which does not condition on age. It does contaminate every "
        "age-disaggregated figure, and it puts real weight on the wrong side of the 9-month "
        "eligibility boundary -- the excess at 12 months is drawn partly from children who are "
        "really 10 or 11 months old, which is exactly the age band where campaign coverage is "
        "usually weakest.",
        "",
        f"Terminal-digit preference in the interview duration is mild by comparison: "
        f"{dur['pct_ending_0_or_5']:.1f}% of durations end in 0 or 5 against 20% expected "
        f"(chi-square p = {dur['chi2_p']:.3f}). Durations are recorded plausibly; it is the ages "
        "that are not.",
        "",
        "## 4.2 Interviewer-level outliers",
        "",
        "Four rules were declared in `common.py` before the data were examined. An interviewer "
        f"is excluded when {FLAG_EXCLUDE_AT} or more fire:",
        "",
        f"1. median completed-interview duration below {100*FLAG_DURATION_RATIO:.0f}% of the "
        "survey median;",
        f"2. reported coverage at or above {100*FLAG_COVERAGE_MIN:.0f}%;",
        f"3. card-sighting rate below {100*FLAG_CARD_RATIO:.0f}% of the survey rate;",
        f"4. household completion rate at or above {100*FLAG_COMPLETION_MIN:.0f}%.",
        "",
        md_table(ivshow, {"completion_rate": ".3f", "median_duration_min": ".1f",
                          "card_seen_rate": ".3f", "reported_coverage": ".3f",
                          "z_reported_coverage": ".1f", "z_median_duration_min": ".1f"}),
        "",
        f"**{', '.join(excl)} fails all four.** The z-scores are computed against the median and "
        "the median absolute deviation of the other interviewers, so the outlier does not "
        "inflate the yardstick that judges it. The pattern is not a difference of degree: the "
        f"median interview lasts {iv.median_duration_min.min():.0f} minutes against a survey "
        f"median of {iv.median_duration_min.median():.0f}; coverage is reported at "
        f"{100*iv.reported_coverage.max():.0f}% with not one unvaccinated child in nine clusters; "
        f"a card was seen for {100*iv.card_seen_rate.min():.0f}% of children against "
        f"{100*ch['vaccination_card_seen'].eq('Yes').mean():.0f}% for everyone else; and "
        f"{100*iv.completion_rate.max():.0f}% of households were completed against a survey "
        f"average of {100*hh['is_completed'].mean():.0f}%.",
        "",
        "That combination is not consistent with an unusually skilled interviewer working "
        "unusually well-covered clusters. Skipping the card question is what makes the interview "
        "short, and answering the recall question 'Yes' every time is what makes coverage "
        "saturate. The fieldwork log corroborates it independently: the same interviewer records "
        "two clusters and 40 households attempted in a single day at a mean of 3.6 minutes each.",
        "",
        f"Note what the log did **not** catch: {int(iv.loc[iv.screen_outcome=='EXCLUDE','gps_verified_days'].sum())} "
        f"of that interviewer's {int(iv.loc[iv.screen_outcome=='EXCLUDE','days_worked'].sum())} "
        "days were GPS verified. The interviewer was where they were supposed to be. GPS proves "
        "presence, not that an interview happened.",
        "",
        "## 4.3 Implausible response and effort patterns",
        "",
        md_table(pat, {"share_pct": ".1f"}),
        "",
        "## 4.4 Missingness",
        "",
        "### Unit non-response",
        "",
        md_table(unit, {"response_rate_pct": ".1f", "refusal_pct": ".1f", "noncontact_pct": ".1f"}),
        "",
        f"The overall household response rate among eligible dwellings is "
        f"**{100*mdiag['overall_response_rate']:.1f}%**. It is not uniform: it is lowest in "
        "Zaruwa State and materially lower in urban clusters, where non-contact rather than "
        "refusal does most of the work.",
        "",
        "### Item non-response on the outcome",
        "",
        md_table(item, {"missing_pct": ".1f"}),
        "",
        "### Is the missingness plausibly ignorable?",
        "",
        "**Item missingness: yes, plausibly.** 44 children (1.9%) have neither a card answer nor "
        "a recall answer. It is spread across 15 interviewers and all three strata, it is not "
        "concentrated in any wealth quintile or settlement type, and at that size the bound is "
        "narrow: counting all 44 as vaccinated gives 81.7%, counting all as unvaccinated gives "
        "79.6%. The headline is inside a two-point band whatever the truth is, so the "
        "ignorability assumption does not have to carry any weight. Complete-case analysis "
        "inside the weighted estimator, with both bounds reported.",
        "",
        "**Unit non-response: no, not plausibly ignorable.** Three things are worth separating.",
        "",
        f"1. *What is testable and passes.* A logistic model of household response on the "
        f"variables observed for respondents and non-respondents alike -- stratum, settlement "
        f"type, cluster listing size, fieldwork day -- has a pseudo-R2 of only "
        f"{mdiag['logit_pseudo_r2']:.3f}, with fitted response propensities between "
        f"{100*mdiag['predicted_response_range'][0]:.0f}% and "
        f"{100*mdiag['predicted_response_range'][1]:.0f}%. Little of the variation in response "
        "is explained by the design variables, which is why the adjustment is made within "
        "cluster rather than on a propensity model.",
        f"2. *What is testable and fails.* At cluster level, the response rate and measured "
        f"coverage move together: Spearman rho = "
        f"**{mdiag['cluster_rr_vs_coverage_spearman']:.2f}** "
        f"(p = {mdiag['cluster_rr_vs_coverage_p']:.4f}, n = {mdiag['n_clusters']} clusters after "
        f"removing the falsified clusters, which would have manufactured this association and "
        f"push rho to {mdiag['cluster_rr_vs_coverage_spearman_unscreened']:.2f} if left in), with "
        f"a slope of {mdiag['cluster_rr_vs_coverage_slope']:.2f} coverage points per point of "
        "response rate. Clusters that were hard to interview are clusters where the campaign did "
        "less well. The mechanism is shared -- absent households, mobile populations and "
        "insecure or inaccessible settlements are missed by the survey team and by the "
        "vaccination team alike -- and a mechanism that operates between clusters almost "
        "certainly operates within them too.",
        "3. *What is not testable at all.* Whether, within a cluster, the specific households "
        "that refused or were never found differ in coverage from those that responded. No "
        "outcome was recorded for a single non-responding household, so the survey contains no "
        "information on this point. It is an assumption, not a finding.",
        "",
        "**What was done about it.** The weight adjustment is made within cluster, which is the "
        "finest weighting class the data support and which absorbs the between-cluster part of "
        "the problem -- the part we can see. The within-cluster part cannot be absorbed, so it "
        "is bounded instead: stage 03 reports the headline under an assumed 10-point and "
        "20-point coverage deficit among non-respondents, which moves it to 79.2% and 77.4%. "
        "Those are the numbers a decision maker should treat as the plausible lower end.",
        "",
        "## 4.5 Quality flag register",
        "",
        md_table(flags[["flag", "severity", "affects", "fixable_by", "action"]]),
        "",
    ]
    ART["dq_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("wrote %s", ART["dq_report"].name)


if __name__ == "__main__":
    main()
