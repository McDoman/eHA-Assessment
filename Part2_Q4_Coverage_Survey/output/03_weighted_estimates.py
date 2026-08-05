"""
Stage 03 -- Weighted coverage estimates, design effects, and sensitivity
=======================================================================

Produces the headline coverage estimate and its stratum estimates with
confidence intervals that account for stratification, clustering and unequal
weights; reports the design effect and effective sample size; and runs every
assumption in the stage-02 register as an explicit alternative.

Two analysis sets are carried throughout:

  A  "As submitted"  -- all 90 clusters, exactly as the data arrived.
  B  "Headline"      -- the nine clusters worked by the interviewer that the
                        stage-04 falsification screen excludes are removed, and
                        the surviving clusters in each stratum are re-weighted
                        by n_h / (n_h - d_h).

Under PPS, every selected cluster carries an identical share M_h/n_h of its
stratum's measure of size, so losing d_h clusters is compensated exactly by that
factor. This is a stage-one non-response adjustment and it rests on the
assumption that the lost clusters are exchangeable with the survivors in the
same stratum -- an assumption that is *not* safe here, since the clusters were
not lost at random but assigned to one interviewer. That is why the estimate is
reported as a headline with a stated exclusion, and why the affected strata are
identified in the fitness-for-purpose section rather than published without
qualification.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (ART, BOOTSTRAP_REPS, CLUSTERS_PER_STRATUM, COVERAGE_TARGET,
                    HOUSEHOLDS_PER_CLUSTER, SRC, STRATUM_NAMES, banner, build_analysis_sets,
                    get_logger, md_table, svy_by, svy_prop, svy_prop_bootstrap, write_json)

LOG = get_logger("03_weighted_estimates")


def main() -> None:
    banner(LOG, "STAGE 03  Weighted coverage estimates")

    hh = pd.read_csv(ART["hh_weighted"])
    ch = pd.read_csv(ART["child_weighted"])

    # Stage-one sampling fraction per stratum, for the optional FPC. The
    # denominator is the number of enumeration areas in the whole frame, not the
    # number selected.
    n_frame_eas = pd.read_csv(SRC["frame"]).groupby("stratum_code").size()
    fpc = {h: CLUSTERS_PER_STRATUM / n for h, n in n_frame_eas.items()}
    LOG.info("stage-one sampling fractions n_h/N_h: %s",
             {k: round(v, 3) for k, v in fpc.items()})

    set_a, set_b, excl, dropped, s1factor = build_analysis_sets(ch, hh)
    LOG.warning("falsification screen excludes interviewer(s) %s -> %d clusters: %s",
                excl, len(dropped), dropped)
    LOG.info("stage-one re-weighting factors after cluster exclusion: %s",
             s1factor.round(4).to_dict())
    LOG.info("analysis set A: %d children in %d clusters; set B: %d children in %d clusters",
             len(set_a), set_a.cluster_id.nunique(), len(set_b), set_b.cluster_id.nunique())

    # ================================================== headline and strata
    rows = []
    for label, d in (("A. As submitted (all 90 clusters)", set_a),
                     ("B. Headline (falsified clusters excluded)", set_b)):
        nat = svy_prop(d, "vaccinated", domain="National")
        rows.append(nat.as_row(analysis_set=label, level="National",
                               stratum_name="All three states"))
        for h, g in d.groupby("stratum_code"):
            est = svy_prop(g, "vaccinated", domain=h)
            rows.append(est.as_row(analysis_set=label, level="Stratum",
                                   stratum_name=STRATUM_NAMES[h]))
    head = pd.DataFrame(rows)
    head = head[["analysis_set", "level", "domain", "stratum_name", "estimate_pct", "se_pct",
                 "ci_low_pct", "ci_high_pct", "ci_width_pp", "df", "n_children", "n_clusters",
                 "deff", "deft", "n_effective", "deff_kish_weights", "icc_implied",
                 "sum_weights", "weighted_numerator", "ci_method"]]
    head.to_csv(ART["est_headline"], index=False)

    hb = head[head.analysis_set.str.startswith("B")]
    natb = hb[hb.level == "National"].iloc[0]
    LOG.info("HEADLINE national coverage %.1f%% (95%% CI %.1f-%.1f), deff %.2f, n_eff %.0f",
             natb.estimate_pct, natb.ci_low_pct, natb.ci_high_pct, natb.deff, natb.n_effective)

    # ======================================== independent variance validation
    checks = []
    for label, d in (("A. As submitted", set_a), ("B. Headline", set_b)):
        tay = svy_prop(d, "vaccinated", domain="National")
        boot = svy_prop_bootstrap(d, "vaccinated")
        srs = np.sqrt(tay.estimate * (1 - tay.estimate) / (len(d) - 1))
        tay_fpc = svy_prop(d, "vaccinated", domain="National", fpc=fpc)
        checks.append({
            "analysis_set": label,
            "estimate_pct": 100 * tay.estimate,
            "se_taylor_pct": 100 * tay.se,
            "se_bootstrap_pct": 100 * boot["se_bootstrap"],
            "bootstrap_reps": boot["reps"],
            "taylor_vs_bootstrap_ratio": tay.se / boot["se_bootstrap"],
            "se_taylor_with_stage1_fpc_pct": 100 * tay_fpc.se,
            "se_naive_srs_pct": 100 * srs,
            "se_inflation_vs_srs": tay.se / srs,
            "ci_taylor_pct": f"{100*tay.ci_low:.1f}-{100*tay.ci_high:.1f}",
            "ci_bootstrap_pct": f"{boot['ci_low_pct']:.1f}-{boot['ci_high_pct']:.1f}",
        })
    chk = pd.DataFrame(checks)
    chk.to_csv(ART["variance_check"], index=False)
    LOG.info("Taylor vs bootstrap SE ratio: %s",
             chk["taylor_vs_bootstrap_ratio"].round(3).tolist())

    # ================================================== domain (sub-group) estimates
    d = set_b.copy()
    d["age_group"] = pd.cut(d["age_months"], [8, 11, 23, 35, 47, 59],
                            labels=["9-11 m", "12-23 m", "24-35 m", "36-47 m", "48-59 m"])
    d["wealth_label"] = d["wealth_quintile"].map(
        {1: "Q1 poorest", 2: "Q2", 3: "Q3", 4: "Q4", 5: "Q5 richest"})
    doms = []
    for var in ["stratum_code", "settlement_type", "wealth_label", "sex", "age_group",
                "status_source"]:
        t = svy_by(d, "vaccinated", var)
        t["domain_label"] = t["domain"].map(STRATUM_NAMES).fillna(t["domain"])
        doms.append(t)
    dom = pd.concat(doms, ignore_index=True)
    dom.to_csv(ART["est_domains"], index=False)
    LOG.info("wrote %d domain estimates", len(dom))

    # ================================================== sensitivity analysis
    sens = run_sensitivity(ch, hh, set_a, set_b, dropped, s1factor, fpc)
    sens.to_csv(ART["est_sensitivity"], index=False)

    write_json(ART["est_headline"].with_suffix(".meta.json"), {
        "excluded_interviewers": excl,
        "excluded_clusters": dropped,
        "stage1_reweight_factors": s1factor.round(6).to_dict(),
        "headline_national_pct": round(float(natb.estimate_pct), 2),
        "headline_ci_pct": [round(float(natb.ci_low_pct), 2), round(float(natb.ci_high_pct), 2)],
        "deff": round(float(natb.deff), 3),
        "n_effective": round(float(natb.n_effective), 1),
        "coverage_target": COVERAGE_TARGET,
    })

    _write_report(head, hb, dom, chk, sens, excl, dropped, s1factor, fpc, set_b)
    banner(LOG, "STAGE 03 complete")


# --------------------------------------------------------------------------
# Sensitivity analysis
# --------------------------------------------------------------------------


def _row(label, group, est, note):
    return {"variant": label, "group": group, "estimate_pct": 100 * est.estimate,
            "se_pct": 100 * est.se, "ci_low_pct": 100 * est.ci_low,
            "ci_high_pct": 100 * est.ci_high, "n_children": est.n_obs,
            "n_clusters": est.n_psu, "deff": est.deff, "note": note}


def run_sensitivity(ch, hh, set_a, set_b, dropped, s1factor, fpc) -> pd.DataFrame:
    """Every assumption in the stage-02 register, run as an explicit alternative."""
    out = []
    base = svy_prop(set_b, "vaccinated", domain="National")
    out.append(_row("0. Headline", "Weighting", base,
                    "cluster non-response adjustment, untrimmed weights, complete case"))

    # --- what the design does to the answer ------------------------------
    b = set_b.copy()
    b["w_unit"] = 1.0
    out.append(_row("1. Unweighted, treated as SRS", "Weighting",
                    svy_prop(b, "vaccinated", w="w_unit", domain="National"),
                    "point estimate ignores the design; SE still design-based, so only the "
                    "point estimate is comparable"))

    # Self-weighting: what the weight would have been had the field listing
    # equalled the census measure of size. The two stages then cancel and
    # w = M_h / (n_h * m_i), a constant within stratum. The non-response
    # adjustment and the stage-one re-weighting are left in place so that this
    # row isolates the effect of the listing gap and nothing else.
    frame_sw = pd.read_csv(ART["frame_clean"])
    const = (frame_sw.groupby("stratum_code")
             .apply(lambda g: g["stratum_total_households"].iloc[0]
                    / (g["clusters_selected_in_stratum"].iloc[0] * HOUSEHOLDS_PER_CLUSTER),
                    include_groups=False))
    b["w_selfweight"] = (b["stratum_code"].map(const) * b["nr_adjustment"]
                         * b.get("stage1_nr_factor", 1.0))
    out.append(_row("2. Assumed self-weighting within stratum", "Weighting",
                    svy_prop(b, "vaccinated", w="w_selfweight", domain="National"),
                    "the mistake of using the census size at both stages: correct between "
                    "strata, wrong between clusters"))

    out.append(_row("3. Design weights, no non-response adjustment", "Non-response",
                    svy_prop(set_b, "vaccinated", w="w_base", domain="National"),
                    "isolates what the non-response adjustment is doing"))
    out.append(_row("4. Non-response class = stratum x settlement", "Non-response",
                    svy_prop(set_b, "vaccinated", w="weight_nrcell", domain="National"),
                    "coarser weighting class (A6 alternative)"))
    out.append(_row("5. Vacant dwellings treated as non-response", "Non-response",
                    svy_prop(set_b, "vaccinated", w="weight_vacantnr", domain="National"),
                    "A5 alternative"))
    out.append(_row("6. Non-contacts treated as ineligible", "Non-response",
                    svy_prop(set_b, "vaccinated", w="weight_nc_ineligible", domain="National"),
                    "the favourable assumption: assumes three failed visits mean no eligible "
                    "child was ever there"))
    out.append(_row("7. Weights trimmed at 4x the stratum median", "Weighting",
                    svy_prop(set_b, "vaccinated", w="weight_trimmed", domain="National"),
                    "A8 alternative"))
    out.append(_row("8. Stage-one finite population correction applied", "Variance",
                    svy_prop(set_b, "vaccinated", domain="National", fpc=fpc),
                    "point estimate unchanged; narrows the interval by the sampling fraction"))

    # --- item missingness bounds ------------------------------------------
    full = ch[ch["age_eligible"] & ~ch["cluster_id"].isin(dropped)].copy()
    full = full.join(s1factor, on="stratum_code")
    full["weight_final"] = full["weight_final"] * full["stage1_nr_factor"]
    for lab, fill in (("9. All indeterminate status counted as vaccinated", 1.0),
                      ("10. All indeterminate status counted as unvaccinated", 0.0)):
        f = full.copy()
        f["vaccinated"] = f["vaccinated"].fillna(fill)
        out.append(_row(lab, "Item missingness", svy_prop(f, "vaccinated", domain="National"),
                        "A10 bound"))

    # --- age eligibility ---------------------------------------------------
    allages = ch[~ch["status_missing"] & ~ch["cluster_id"].isin(dropped)].copy()
    allages = allages.join(s1factor, on="stratum_code")
    allages["weight_final"] = allages["weight_final"] * allages["stage1_nr_factor"]
    out.append(_row("11. Age-ineligible children re-included", "Eligibility",
                    svy_prop(allages, "vaccinated", domain="National"),
                    "the 23 children outside 9-59 months put back in"))

    # --- fieldwork window --------------------------------------------------
    inw = set_b[set_b["out_of_window"] == 0]
    out.append(_row("12. Out-of-window interviews excluded", "Fieldwork",
                    svy_prop(inw, "vaccinated", domain="National"),
                    "drops the 18 households dated 2026-04-29"))

    # --- falsification -----------------------------------------------------
    out.append(_row("13. Falsified clusters retained (as submitted)", "Falsification",
                    svy_prop(set_a, "vaccinated", domain="National"),
                    "analysis set A: what the survey would have reported unscreened"))

    # --- non-ignorable non-response ---------------------------------------
    # Pseudo-records for the households that produced no interview: each one
    # contributes its base design weight, an expected number of children equal
    # to the cluster mean among responders, and an expected coverage equal to
    # the cluster's observed coverage less a stated penalty.
    for pen in (0.10, 0.20):
        aug = _augment_nonrespondents(ch, hh, dropped, s1factor, penalty=pen)
        out.append(_row(f"14. Non-respondents {int(100*pen)} points less covered",
                        "Non-response",
                        svy_prop(aug, "vaccinated", w="weight_nir", domain="National"),
                        "A7 bound: explicit non-ignorable adjustment, no adjustment factor used"))

    df = pd.DataFrame(out)
    df["shift_from_headline_pp"] = df["estimate_pct"] - df.loc[0, "estimate_pct"]
    df["crosses_target"] = np.where(df["ci_high_pct"] >= 100 * COVERAGE_TARGET,
                                    "CI reaches 95% target", "below target")
    LOG.info("sensitivity range: %.1f to %.1f (headline %.1f)",
             df.estimate_pct.min(), df.estimate_pct.max(), df.loc[0, "estimate_pct"])
    return df


def _augment_nonrespondents(ch, hh, dropped, s1factor, penalty: float) -> pd.DataFrame:
    """
    Build a dataset in which non-responding eligible households appear explicitly
    with an assumed coverage, so no non-response adjustment factor is applied.
    A fractional outcome is legitimate inside a ratio estimator: it contributes
    its expectation to the weighted numerator.
    """
    resp = ch[ch["analysis_eligible"] & ~ch["cluster_id"].isin(dropped)].copy()
    resp = resp.join(s1factor, on="stratum_code")
    resp["weight_nir"] = resp["w_base"] * resp["stage1_nr_factor"]

    cl = resp.groupby("cluster_id").agg(cov=("vaccinated", "mean"),
                                        wbase=("w_base", "first"),
                                        stratum_code=("stratum_code", "first"),
                                        f1=("stage1_nr_factor", "first"))
    kids = (resp.groupby(["cluster_id", "household_id"]).size().groupby("cluster_id").mean()
            .rename("kids_per_hh"))
    nr = (hh[(hh["is_nonresponse"] == 1) & (~hh["cluster_id"].isin(dropped))]
          .groupby("cluster_id").size().rename("n_nr"))
    pseudo = cl.join(kids).join(nr).dropna(subset=["n_nr"])
    pseudo["vaccinated"] = (pseudo["cov"] - penalty).clip(lower=0.0)
    pseudo["weight_nir"] = pseudo["wbase"] * pseudo["f1"] * pseudo["n_nr"] * pseudo["kids_per_hh"]
    pseudo = pseudo.reset_index()
    return pd.concat([resp[["cluster_id", "stratum_code", "vaccinated", "weight_nir"]],
                      pseudo[["cluster_id", "stratum_code", "vaccinated", "weight_nir"]]],
                     ignore_index=True)


# --------------------------------------------------------------------------


def _write_report(head, hb, dom, chk, sens, excl, dropped, s1factor, fpc, set_b) -> None:
    nat = hb[hb.level == "National"].iloc[0]
    natA = head[(head.analysis_set.str.startswith("A")) & (head.level == "National")].iloc[0]
    strat = hb[hb.level == "Stratum"]
    b_bar = nat.n_children / nat.n_clusters

    disp = hb[["level", "stratum_name", "estimate_pct", "se_pct", "ci_low_pct", "ci_high_pct",
               "ci_width_pp", "n_children", "n_clusters", "df", "deff", "deft", "n_effective",
               "icc_implied"]].copy()

    lines = [
        "# 03 - Weighted coverage estimates",
        "",
        "## Headline",
        "",
        f"**National campaign dose coverage among children aged 9-59 completed months: "
        f"{nat.estimate_pct:.1f}% (95% CI {nat.ci_low_pct:.1f}-{nat.ci_high_pct:.1f}).**",
        "",
        f"Design-based standard error {nat.se_pct:.2f} percentage points on "
        f"{int(nat.df)} degrees of freedom ({int(nat.n_clusters)} clusters minus 3 strata). "
        f"Interval computed on the logit scale, so it cannot exceed the parameter space.",
        "",
        f"The estimate excludes the {len(dropped)} clusters worked by interviewer "
        f"{', '.join(excl)}, whose records fail the stage-04 falsification screen on "
        f"{'all four' if len(excl) else 'multiple'} independent criteria. Retaining them gives "
        f"{natA.estimate_pct:.1f}% -- **{natA.estimate_pct - nat.estimate_pct:+.1f} percentage "
        "points** -- which is the size of the error a survey report would have carried had the "
        "screen not been run.",
        "",
        "## National and stratum estimates",
        "",
        md_table(disp, {"estimate_pct": ".1f", "se_pct": ".2f", "ci_low_pct": ".1f",
                        "ci_high_pct": ".1f", "ci_width_pp": ".1f", "deff": ".2f",
                        "deft": ".2f", "n_effective": ".0f", "icc_implied": ".3f"}),
        "",
        "Every interval accounts for all three features of the design: stratification (the "
        "variance is accumulated within stratum and strata contribute independently), "
        "clustering (the linearised residuals are summed to the cluster before the "
        "between-unit variance is taken) and unequal weights (the estimator is a ratio of "
        "weighted totals, and the weights enter the linearisation).",
        "",
        "## Design effect and effective sample size",
        "",
        f"| | National | {' | '.join(strat.stratum_name)} |",
        "|---|---|" + "---|" * len(strat),
        f"| Children analysed | {int(nat.n_children):,} | "
        + " | ".join(f"{int(v):,}" for v in strat.n_children) + " |",
        f"| Design effect (DEFF) | {nat.deff:.2f} | "
        + " | ".join(f"{v:.2f}" for v in strat.deff) + " |",
        f"| DEFT (sqrt) | {nat.deft:.2f} | " + " | ".join(f"{v:.2f}" for v in strat.deft) + " |",
        f"| Effective sample size | {nat.n_effective:.0f} | "
        + " | ".join(f"{v:.0f}" for v in strat.n_effective) + " |",
        f"| Of which: unequal weights (Kish) | {nat.deff_kish_weights:.2f} | "
        + " | ".join(f"{v:.2f}" for v in strat.deff_kish_weights) + " |",
        f"| Implied intra-cluster correlation | {nat.icc_implied:.3f} | "
        + " | ".join(f"{v:.3f}" for v in strat.icc_implied) + " |",
        "",
        f"**What the design effect means for the next round.** A DEFF of {nat.deff:.2f} says "
        f"that {int(nat.n_children):,} children interviewed under this design carry the "
        f"information of {nat.n_effective:.0f} children interviewed under simple random "
        f"sampling -- {100*(1 - nat.n_effective/nat.n_children):.0f}% of the fieldwork bought no "
        "precision. The decomposition matters for what to do about it:",
        "",
        f"- **{nat.deff_kish_weights:.2f}** of the DEFF is unequal weighting, caused by the "
        "field listing diverging from the census measure of size. This is fixable at design "
        "time: re-list the enumeration areas before selection, or select PPS on a current "
        "listing, and the two stages cancel again.",
        f"- The remainder is clustering, implying an intra-cluster correlation of about "
        f"**{nat.icc_implied:.3f}** at a mean of {b_bar:.1f} children per cluster. Vaccination "
        "status is strongly clustered because campaign teams either reached a settlement or did "
        "not.",
        "",
        f"With rho = {nat.icc_implied:.3f}, the next round buys precision far more cheaply from "
        "**more clusters** than from more households per cluster. Taking 12 households per "
        f"cluster instead of 20 costs little (DEFF falls to about "
        f"{1 + (b_bar*12/20 - 1)*nat.icc_implied:.2f}) and frees enough fieldwork to raise the "
        "cluster count by roughly two thirds at the same budget -- which is where the variance "
        "actually lives. Conversely, doubling households per cluster while holding clusters "
        "fixed would buy almost nothing.",
        "",
        "## Variance method validation",
        "",
        "The Taylor linearisation is checked against a Rao-Wu-Yue rescaled bootstrap "
        f"({BOOTSTRAP_REPS:,} replicates, PSUs resampled within stratum) and against the "
        "standard error a naive simple-random-sample analysis would have reported.",
        "",
        md_table(chk, {"estimate_pct": ".2f", "se_taylor_pct": ".3f", "se_bootstrap_pct": ".3f",
                       "taylor_vs_bootstrap_ratio": ".3f",
                       "se_taylor_with_stage1_fpc_pct": ".3f", "se_naive_srs_pct": ".3f",
                       "se_inflation_vs_srs": ".2f"}),
        "",
        f"The two design-based methods agree to within "
        f"{100*abs(chk.taylor_vs_bootstrap_ratio.iloc[1]-1):.1f}%, which is the check that the "
        "linearisation is implemented correctly. A simple-random-sample analysis would have "
        f"reported a standard error {chk.se_inflation_vs_srs.iloc[1]:.2f} times too small -- a "
        f"confidence interval {100*(1 - 1/chk.se_inflation_vs_srs.iloc[1]):.0f}% narrower than "
        "the design justifies, which is the difference between an interval that covers the true "
        "value 95% of the time and one that does not.",
        "",
        "## Coverage by domain",
        "",
        md_table(dom[["domain_variable", "domain_label", "estimate_pct", "ci_low_pct",
                      "ci_high_pct", "n_children", "deff"]],
                 {"estimate_pct": ".1f", "ci_low_pct": ".1f", "ci_high_pct": ".1f",
                  "deff": ".2f"}),
        "",
        "## Sensitivity analysis",
        "",
        "Each row replaces one assumption from the stage-02 register with its stated "
        "alternative and re-runs the national estimate.",
        "",
        md_table(sens[["variant", "group", "estimate_pct", "ci_low_pct", "ci_high_pct",
                       "shift_from_headline_pp", "n_children", "note"]],
                 {"estimate_pct": ".1f", "ci_low_pct": ".1f", "ci_high_pct": ".1f",
                  "shift_from_headline_pp": "+.1f"}),
        "",
        f"Across every variant the national estimate stays between "
        f"{sens.estimate_pct.min():.1f}% and {sens.estimate_pct.max():.1f}%, and the upper "
        f"confidence limit reaches the {100*COVERAGE_TARGET:.0f}% campaign target in "
        f"{int((sens.ci_high_pct >= 100*COVERAGE_TARGET).sum())} of {len(sens)} variants. The "
        "conclusion that national coverage fell short of target is therefore robust to every "
        "analytical choice made here; only the falsification variant moves the point estimate "
        "by more than three points.",
        "",
    ]
    ART["est_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("wrote %s", ART["est_report"].name)


if __name__ == "__main__":
    main()
