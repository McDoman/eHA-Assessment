"""
Stage 02 -- Design weights
==========================

Derives the sampling weight for every responding household and every enumerated
child from the selection probability at each stage, adjusts for non-response,
and documents every assumption in a register that names the alternative that was
considered and the direction the headline moves if the assumption is wrong.

The weight
----------
For household k in cluster i of stratum h,

    pi_1i   = n_h * M_i / M_h                    stage one, systematic PPS
    pi_2k|i = m_i / L_i                          stage two, SRS from the field listing
    w0      = 1 / (pi_1i * pi_2k|i)              base design weight
    f_i     = e_i / r_i                          non-response adjustment, within cluster
    w1      = w0 * f_i                           final household weight
    w_child = w1                                 children are enumerated, not sampled

where
    n_h = 30 clusters selected in stratum h
    M_i = 2023 census households in EA i, the PPS measure of size
    M_h = census households in the whole stratum
    m_i = 20 households selected at stage two
    L_i = households on the *fresh field listing* of EA i
    e_i = selected households that turned out to be eligible dwellings
    r_i = of those, the ones that produced a completed interview

The critical point is that `M_i` and `L_i` are different numbers. A design in
which stage one uses PPS on size and stage two takes a fixed take from that same
size is self-weighting: the two probabilities cancel and every household in a
stratum carries the same weight. Here they do not cancel, because the field
listing is a median 1.09x the census count and ranges from 0.50x to 3.77x.
The base weight is therefore proportional to L_i / M_i and varies almost eight
fold across clusters. That variation is real information about where households
actually are, and discarding it -- by analysing unweighted, or by assuming the
design is self-weighting -- would bias the estimate toward clusters whose
population the 2023 census overstated.

Children are *not* subsampled: the instrument enumerates every resident child
aged 9-59 completed months. The child weight is therefore the household weight
exactly, with no third-stage factor. The consequence is that children in large
households are correctly over-represented in the sample relative to a
one-child-per-household design, and no compensating adjustment is needed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (ART, AssumptionRegister, CLUSTERS_PER_STRATUM, HOUSEHOLDS_PER_CLUSTER,
                    STRATUM_NAMES, TRIM_MULTIPLE, banner, get_logger, md_table)

LOG = get_logger("02_design_weights")
REG = AssumptionRegister()


def main() -> None:
    banner(LOG, "STAGE 02  Design weights")

    frame = pd.read_csv(ART["frame_clean"])
    hh = pd.read_csv(ART["hh_clean"])
    ch = pd.read_csv(ART["child_clean"])

    # ------------------------------------------------------------- stage one
    f = frame[["cluster_id", "ea_code", "stratum_code", "stratum_name", "lga_name",
               "settlement_type", "households_census_2023", "stage1_selection_probability",
               "stratum_total_households", "clusters_selected_in_stratum",
               "households_listed_fieldwork", "field_status"]].copy()
    f = f.rename(columns={"households_census_2023": "M_i",
                          "stratum_total_households": "M_h",
                          "clusters_selected_in_stratum": "n_h",
                          "households_listed_fieldwork": "L_i",
                          "stage1_selection_probability": "pi_1i"})
    f["w_stage1"] = 1.0 / f["pi_1i"]

    REG.add(ref="A1",
            assumption="The stage-one inclusion probability is n_h*M_i/M_h, taken as supplied "
                       "in the frame.",
            basis="Reproduced from the frame's own columns to within 5e-9 for all 90 clusters; "
                  "the measure of size is the 2023 census household count.",
            alternative="Recomputing it from the realised systematic PPS sequence, which the "
                        "pack does not supply (no random start or ordering is given).",
            effect_if_wrong="A mis-specified stage-one probability biases the between-cluster "
                            "weighting; with PPS the direction depends on whether large or small "
                            "EAs are favoured.",
            tested="no -- not identifiable from the pack")

    REG.add(ref="A2",
            assumption="Systematic PPS is treated as with-replacement PPS for variance purposes "
                       "(the 'ultimate cluster' assumption), and no joint inclusion "
                       "probabilities are used.",
            basis="Standard practice for systematic PPS, where the exact joint probabilities are "
                  "not computable; it is the estimator implemented in R survey, Stata svy and "
                  "SAS SURVEYFREQ.",
            alternative="A successive-difference or Hartley-Rao approximation exploiting the "
                        "frame ordering, which the pack does not supply.",
            effect_if_wrong="Slightly conservative: systematic PPS from an implicitly stratified "
                            "frame is normally more efficient than with-replacement sampling, so "
                            "the reported standard errors are, if anything, too wide.",
            tested="partially -- a rescaled bootstrap is run in stage 03 as an independent check")

    # ------------------------------------------------------------- stage two
    # The second-stage probability uses the FIELD LISTING, because that is the
    # frame from which the 20 households were actually drawn. Using the census
    # count here instead would make the design look self-weighting and would be
    # wrong by the listing ratio.
    f["m_i"] = HOUSEHOLDS_PER_CLUSTER
    f["pi_2"] = f["m_i"] / f["L_i"]
    f["w_stage2"] = 1.0 / f["pi_2"]
    f["w_base"] = f["w_stage1"] * f["w_stage2"]
    f["listing_ratio"] = f["L_i"] / f["M_i"]

    LOG.info("base weight: median %.1f, range %.1f-%.1f, ratio max/min %.1f",
             f["w_base"].median(), f["w_base"].min(), f["w_base"].max(),
             f["w_base"].max() / f["w_base"].min())

    REG.add(ref="A3",
            assumption="The stage-two probability is 20/L_i with L_i the fresh field listing, "
                       "not the census count used for PPS.",
            basis="The protocol states households were drawn by SRS from a fresh field listing; "
                  "the listing count is supplied per cluster and agrees between the frame and "
                  "the household file.",
            alternative="Assuming the design is self-weighting within stratum, i.e. using the "
                        "census count at both stages.",
            effect_if_wrong="This is the single most consequential weighting choice. Assuming "
                            "self-weighting would shift the national estimate by the covariance "
                            "between the listing ratio and cluster coverage; it is quantified "
                            "directly in the stage-03 sensitivity table.",
            tested="yes -- 'self-weighting (unweighted)' variant")

    REG.add(ref="A4",
            assumption="Every one of the 20 selected households was drawn from the listing of "
                       "the cluster it is recorded in, with equal probability.",
            basis="Protocol; the household file records exactly 20 selected households in every "
                  "cluster and no substitutions are recorded.",
            alternative="Modelling within-cluster selection as unequal (e.g. segment sampling in "
                        "large EAs), which nothing in the pack indicates.",
            effect_if_wrong="Would bias within-cluster composition, most plausibly toward "
                            "accessible households near the listing start point.",
            tested="no")

    # ----------------------------------------------- non-response adjustment
    # Eligibility first: a vacant dwelling is a listed structure that holds no
    # household. It is an ineligible frame element, not a refusal, so it is
    # removed from the denominator of the response rate. Its weight share is
    # NOT redistributed -- doing so would project empty dwellings onto real
    # households and inflate the population estimate.
    grp = hh.groupby("cluster_id")
    resp = grp.agg(n_selected=("household_id", "size"),
                   n_ineligible=("is_ineligible_dwelling", "sum"),
                   n_nonresponse=("is_nonresponse", "sum"),
                   n_completed=("is_completed", "sum"))
    resp["n_eligible"] = resp["n_selected"] - resp["n_ineligible"]
    resp["response_rate"] = resp["n_completed"] / resp["n_eligible"]
    resp["nr_adjustment"] = resp["n_eligible"] / resp["n_completed"]
    resp["occupancy_rate"] = resp["n_eligible"] / resp["n_selected"]

    LOG.info("cluster response rate: median %.3f, range %.3f-%.3f; %d clusters below 0.70",
             resp["response_rate"].median(), resp["response_rate"].min(),
             resp["response_rate"].max(), int((resp["response_rate"] < 0.70).sum()))
    if (resp["n_completed"] == 0).any():
        raise RuntimeError("a cluster produced no completed interviews; the within-cluster "
                           "adjustment is undefined and a coarser weighting class is required")

    REG.add(ref="A5",
            assumption="Vacant dwellings are ineligible frame elements. They are removed from "
                       "the response-rate denominator and their weight is not redistributed.",
            basis="A vacant structure contains no household, so it contributes nothing to the "
                  "population of households the survey is estimating. Redistributing its weight "
                  "would project empty dwellings onto occupied ones.",
            alternative="Treating vacancy as non-response, which would inflate every cluster's "
                        "adjustment factor by about 3%.",
            effect_if_wrong="Under-estimates the household population by the vacancy rate "
                            "(2.7%); has almost no effect on a *proportion*, because the factor "
                            "cancels between numerator and denominator.",
            tested="yes -- 'vacancy as non-response' variant")

    REG.add(ref="A6",
            assumption="Refusals and non-contacts after three visits are non-response. The "
                       "weight of a non-responding eligible household is redistributed to "
                       "responding households in the SAME CLUSTER.",
            basis="The cluster is the natural weighting class here: it is the level at which "
                  "response propensity varies most (cluster response rates run from 53% to "
                  "100%), every cluster carries 20 selected households, and no cluster has "
                  "fewer than 10 completed interviews, so no adjustment factor is unstable.",
            alternative="A stratum x settlement-type weighting class (larger cells, less "
                        "variable factors, but assumes response propensity is constant across "
                        "clusters within a state, which the data contradict); or a "
                        "response-propensity model.",
            effect_if_wrong="Missing-at-random within cluster is assumed. If non-responding "
                            "households are less well covered than responding ones in the same "
                            "cluster -- which is the usual direction, because absent caregivers "
                            "and refusing households are harder to reach with a campaign too -- "
                            "the headline is biased UPWARD.",
            tested="yes -- stratum x settlement weighting class, and a non-contacts-as-ineligible "
                   "variant")

    REG.add(ref="A7",
            assumption="Within a cluster, non-responding households have the same expected "
                       "coverage as responding ones (non-response is ignorable given cluster).",
            basis="This is what a weight-class adjustment asserts; it cannot be verified from "
                  "the survey alone because no outcome was recorded for non-respondents.",
            alternative="An explicit non-ignorable model with an assumed coverage differential "
                        "for non-respondents.",
            effect_if_wrong="At an 83% response rate, a 20-point coverage deficit among "
                            "non-respondents would move the national estimate by about 3 points. "
                            "This is bounded explicitly in stage 03.",
            tested="yes -- non-respondent coverage penalty of 10 and 20 points")

    # Households: base weight * within-cluster non-response adjustment.
    hh = hh.merge(f[["cluster_id", "M_i", "L_i", "pi_1i", "pi_2", "w_stage1", "w_stage2",
                     "w_base", "listing_ratio", "field_status", "lga_name"]],
                  on="cluster_id", how="left", suffixes=("", "_frame"))
    hh = hh.merge(resp[["response_rate", "nr_adjustment", "n_eligible", "n_completed"]],
                  left_on="cluster_id", right_index=True, how="left")

    hh["weight_design"] = hh["w_base"]
    hh["weight_final"] = np.where(hh["is_completed"] == 1,
                                  hh["w_base"] * hh["nr_adjustment"], np.nan)

    # Alternative weighting class, carried as a column for the sensitivity run.
    hh["_cell"] = hh["stratum_code"] + "|" + hh["settlement_type"]
    cell = hh.groupby("_cell").agg(elig=("is_ineligible_dwelling", lambda s: (1 - s).sum()),
                                   comp=("is_completed", "sum"))
    cell["nr_adj_cell"] = cell["elig"] / cell["comp"]
    hh = hh.merge(cell[["nr_adj_cell"]], left_on="_cell", right_index=True, how="left")
    hh["weight_nrcell"] = np.where(hh["is_completed"] == 1,
                                   hh["w_base"] * hh["nr_adj_cell"], np.nan)

    # Vacancy-as-non-response alternative.
    resp["nr_adj_vacant"] = resp["n_selected"] / resp["n_completed"]
    hh = hh.merge(resp[["nr_adj_vacant"]], left_on="cluster_id", right_index=True, how="left")
    hh["weight_vacantnr"] = np.where(hh["is_completed"] == 1,
                                     hh["w_base"] * hh["nr_adj_vacant"], np.nan)

    # Non-contacts treated as ineligible (the favourable assumption).
    resp2 = grp.agg(n_selected=("household_id", "size"), n_completed=("is_completed", "sum"))
    nc = hh.assign(_nc=hh["result_of_visit"].eq("No eligible respondent after 3 visits")
                   .astype(int)).groupby("cluster_id")["_nc"].sum()
    resp2["n_eligible_nc"] = (resp2["n_selected"]
                              - grp["is_ineligible_dwelling"].sum() - nc)
    resp2["nr_adj_nc"] = resp2["n_eligible_nc"] / resp2["n_completed"]
    hh = hh.merge(resp2[["nr_adj_nc"]], left_on="cluster_id", right_index=True, how="left")
    hh["weight_nc_ineligible"] = np.where(hh["is_completed"] == 1,
                                          hh["w_base"] * hh["nr_adj_nc"], np.nan)

    # Trimmed weights, as a sensitivity only.
    med = hh.groupby("stratum_code")["weight_final"].transform("median")
    cap = TRIM_MULTIPLE * med
    hh["weight_trimmed"] = np.minimum(hh["weight_final"], cap)
    # Re-scale within stratum so trimming does not change the estimated population.
    scale = (hh.groupby("stratum_code")["weight_final"].transform("sum")
             / hh.groupby("stratum_code")["weight_trimmed"].transform("sum"))
    hh["weight_trimmed"] = hh["weight_trimmed"] * scale
    n_trimmed = int((hh["weight_final"] > cap).sum())
    LOG.info("households whose weight exceeds %.0fx the stratum median: %d", TRIM_MULTIPLE, n_trimmed)

    REG.add(ref="A8",
            assumption="Weights are NOT trimmed for the headline estimate.",
            basis=f"{n_trimmed} of {int(hh['is_completed'].sum())} responding households carry a "
                  f"weight above {TRIM_MULTIPLE:.0f}x their stratum median. Every large weight "
                  "here traces to a field listing that legitimately exceeded the 2023 census "
                  "count, not to a data error, so trimming would trade a real reduction in "
                  "variance for a real introduction of bias.",
            alternative=f"Trimming at {TRIM_MULTIPLE:.0f}x the stratum median with re-scaling to "
                        "preserve the population total.",
            effect_if_wrong="Trimming lowers the variance and pulls the estimate toward the "
                            "densely listed clusters; the size of the shift is reported.",
            tested="yes -- 'trimmed weights' variant")

    # --------------------------------------------------------- child weights
    # No third stage: every eligible child in a responding household is
    # enumerated, so the child inherits the household weight unchanged.
    wcols = ["weight_final", "weight_design", "weight_nrcell", "weight_vacantnr",
             "weight_nc_ineligible", "weight_trimmed", "w_base", "nr_adjustment",
             "response_rate", "listing_ratio", "M_i", "L_i", "pi_1i", "pi_2",
             "field_status", "lga_name"]
    ch = ch.merge(hh[["household_id"] + wcols], on="household_id", how="left")
    if ch["weight_final"].isna().any():
        raise RuntimeError("a child record has no household weight; children exist only in "
                           "completed households, so this indicates a join failure")

    REG.add(ref="A9",
            assumption="The child weight equals the household weight; there is no third-stage "
                       "selection factor.",
            basis="The instrument enumerates ALL resident children aged 9-59 completed months, "
                  "so within a responding household the child inclusion probability is 1.",
            alternative="A within-household selection correction, which would be required only "
                        "if one child per household had been sampled.",
            effect_if_wrong="Would mis-weight children in large households; with a mean of 1.53 "
                            "eligible children per completed household the error would be "
                            "material.",
            tested="no -- the design is unambiguous on this point")

    REG.add(ref="A10",
            assumption="Children whose vaccination status is indeterminate (44 records) and "
                       "children outside 9-59 months (23 records) are excluded from the "
                       "denominator without a further weight adjustment.",
            basis="At 1.9% and 1.0% of records, a complete-case denominator inside a weighted "
                  "ratio estimator is equivalent to an implicit adjustment that assumes the "
                  "missing are like the observed within the same weight class.",
            alternative="An explicit item non-response adjustment, or multiple imputation.",
            effect_if_wrong="Bounded exactly: setting all 44 to vaccinated and all to "
                            "unvaccinated brackets the headline.",
            tested="yes -- both bounds reported")

    # ------------------------------------------------------------ diagnostics
    resp_out = resp.join(f.set_index("cluster_id")[["stratum_code", "M_i", "L_i", "listing_ratio",
                                                    "pi_1i", "pi_2", "w_base", "field_status"]])
    resp_out["w_final_cluster_mean"] = (hh[hh["is_completed"] == 1]
                                        .groupby("cluster_id")["weight_final"].mean())
    resp_out = resp_out.reset_index()
    resp_out.to_csv(ART["weight_components"], index=False)

    diag = _diagnostics(hh, ch, f)
    diag.to_csv(ART["weight_diagnostics"], index=False)
    REG.to_frame().to_csv(ART["assumption_register"], index=False)

    hh.drop(columns=["_cell"]).to_csv(ART["hh_weighted"], index=False)
    ch.to_csv(ART["child_weighted"], index=False)
    LOG.info("wrote weighted household (%d) and child (%d) files", len(hh), len(ch))

    _write_report(hh, ch, f, resp, diag)
    banner(LOG, "STAGE 02 complete")


def _diagnostics(hh: pd.DataFrame, ch: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    """Per-stratum and national weight diagnostics, including the population check."""
    rows = []
    comp = hh[hh["is_completed"] == 1]
    for scope, hsub, csub in ([("National", comp, ch)] +
                              [(s, comp[comp.stratum_code == s], ch[ch.stratum_code == s])
                               for s in sorted(comp["stratum_code"].unique())]):
        w = hsub["weight_final"]
        cw = csub["weight_final"]
        if scope == "National":
            m_h = f["M_h"].groupby(f["stratum_code"]).first().sum()
        else:
            m_h = f.loc[f.stratum_code == scope, "M_h"].iloc[0]
        rows.append({
            "scope": scope,
            "stratum_name": STRATUM_NAMES.get(scope, "All three states"),
            "n_clusters": hsub["cluster_id"].nunique(),
            "n_households_responding": len(hsub),
            "n_children": len(csub),
            "weight_min": w.min(), "weight_median": w.median(), "weight_mean": w.mean(),
            "weight_max": w.max(), "weight_max_over_min": w.max() / w.min(),
            "weight_cv": w.std() / w.mean(),
            "deff_kish_households": 1 + (w.std(ddof=0) / w.mean()) ** 2,
            "deff_kish_children": 1 + (cw.std(ddof=0) / cw.mean()) ** 2,
            "sum_weights_households": w.sum(),
            "census_households_2023": m_h,
            "estimated_over_census": w.sum() / m_h,
            "sum_weights_children": cw.sum(),
        })
    return pd.DataFrame(rows)


def _write_report(hh, ch, f, resp, diag) -> None:
    comp = hh[hh["is_completed"] == 1]
    nat = diag[diag.scope == "National"].iloc[0]
    reg = REG.to_frame()

    fs = f[["cluster_id", "stratum_code", "M_i", "L_i", "listing_ratio", "pi_1i", "pi_2",
            "w_stage1", "w_stage2", "w_base"]].sort_values("listing_ratio")
    show = pd.concat([fs.head(3), fs.tail(3)])

    lines = [
        "# 02 - Design weights",
        "",
        "## The estimator",
        "",
        "```",
        "pi_1i    = n_h * M_i / M_h          stage one: systematic PPS on 2023 census households",
        "pi_2k|i  = m_i / L_i                stage two: SRS of 20 from the fresh field listing",
        "w0       = 1 / (pi_1i * pi_2k|i)    base design weight",
        "f_i      = e_i / r_i                non-response adjustment within cluster",
        "w1       = w0 * f_i                 final household weight",
        "w_child  = w1                       children are enumerated, not sampled",
        "```",
        "",
        "`M_i` is the census measure of size, `L_i` the field listing, `e_i` the selected "
        "households that proved to be eligible (occupied) dwellings and `r_i` those that "
        "produced a completed interview.",
        "",
        "## Why the design is not self-weighting",
        "",
        "If the field listing had matched the census measure of size, `pi_1i * pi_2` would "
        "collapse to `n_h * m_i / M_h` -- a constant within stratum -- and an unweighted "
        "analysis would have been unbiased. It does not. The base weight is proportional to "
        f"`L_i / M_i`, which runs from **{f['listing_ratio'].min():.2f}** to "
        f"**{f['listing_ratio'].max():.2f}**, so the base weight varies "
        f"**{f['w_base'].max() / f['w_base'].min():.1f}-fold** across clusters.",
        "",
        "Extreme clusters, sorted by the listing ratio:",
        "",
        md_table(show, {"listing_ratio": ".2f", "pi_1i": ".4f", "pi_2": ".4f",
                        "w_stage1": ".1f", "w_stage2": ".1f", "w_base": ".1f"}),
        "",
        "## Non-response",
        "",
        f"Of {len(hh):,} selected households, {int(hh['is_ineligible_dwelling'].sum())} were "
        f"vacant dwellings (ineligible), {int(hh['is_nonresponse'].sum())} were eligible but "
        f"produced no interview, and {len(comp):,} were completed. The household response rate "
        f"among eligible dwellings is **{100*len(comp)/int((1-hh['is_ineligible_dwelling']).sum()):.1f}%**.",
        "",
        "The adjustment is made within cluster. Cluster response rates run from "
        f"{100*resp['response_rate'].min():.0f}% to {100*resp['response_rate'].max():.0f}% "
        f"(median {100*resp['response_rate'].median():.0f}%), and "
        f"{int((resp['response_rate'] < 0.70).sum())} clusters fall below 70%; a stratum-level "
        "adjustment would have smeared that variation away and under-weighted the hardest "
        "clusters, which are also the clusters where coverage is lowest.",
        "",
        "## Weight diagnostics",
        "",
        md_table(diag[["scope", "stratum_name", "n_clusters", "n_households_responding",
                       "n_children", "weight_median", "weight_max_over_min", "weight_cv",
                       "deff_kish_children", "sum_weights_households",
                       "census_households_2023", "estimated_over_census"]],
                 {"weight_median": ".1f", "weight_max_over_min": ".1f", "weight_cv": ".3f",
                  "deff_kish_children": ".3f", "sum_weights_households": ",.0f",
                  "census_households_2023": ",.0f", "estimated_over_census": ".3f"}),
        "",
        f"**Population check.** The weights sum to {nat['sum_weights_households']:,.0f} "
        f"households against {nat['census_households_2023']:,.0f} in the 2023 census frame, a "
        f"ratio of {nat['estimated_over_census']:.2f}. That is the expected direction and "
        "magnitude: the field listing is a median 1.09x the census count, three years of growth "
        "and listing practice separate the two, and the weights are estimating *occupied "
        "households in 2026* rather than *census structures in 2023*. The weights were "
        "deliberately **not** calibrated to the census total, because the census total is the "
        "older and less relevant quantity; if a programme requires the projected numerator to "
        "reconcile to an official denominator, calibration should be applied at that point and "
        "reported as such.",
        "",
        f"**Kish design effect from unequal weights alone** is "
        f"{nat['deff_kish_children']:.3f} at national level -- i.e. the weighting by itself "
        f"costs about {100*(nat['deff_kish_children']-1):.0f}% of the effective sample. That is "
        "the price of the listing gap, and it is unavoidable given how the survey was executed. "
        "Anything above that in the total design effect is the cost of clustering.",
        "",
        "## Assumption register",
        "",
        "Every assumption, the alternative that was considered, and where the headline moves if "
        "the assumption is wrong.",
        "",
        md_table(reg),
        "",
    ]
    ART["weight_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("wrote %s", ART["weight_report"].name)


if __name__ == "__main__":
    main()
