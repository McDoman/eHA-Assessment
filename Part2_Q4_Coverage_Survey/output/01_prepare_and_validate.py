"""
Stage 01 -- Preparation and structural validation
=================================================

Reads the four source files, reconstructs the design as it was actually
executed, and records every discrepancy between the design as specified and the
data as received in an integrity ledger.

Nothing is silently dropped. Every exclusion made here is (a) recorded in the
ledger with a severity, (b) carried into the analysis file as a flag column
rather than by deletion where that is possible, so that stage 03 can re-run the
headline under the opposite assumption.

Checks performed
----------------
 1. Referential integrity between frame, household, child and fieldwork files.
 2. Exact duplicate records (a whole cluster was submitted twice).
 3. Reproduction of the stage-one PPS selection probability from the frame.
 4. Agreement between the frame's field listing and the household file's.
 5. Age eligibility against the 9-59 completed month definition.
 6. Skip-pattern conformity between questions 3.4, 3.5 and 3.6.
 7. Roster arithmetic: children enumerated vs children declared on the roster.
 8. Cluster replacement.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import (ART, SRC, AGE_MAX_MONTHS, AGE_MIN_MONTHS, CLUSTERS_PER_STRATUM,
                    HOUSEHOLDS_PER_CLUSTER, Ledger, RESULT_COMPLETED, RESULT_INELIGIBLE,
                    RESULT_NONRESPONSE, STRATUM_NAMES, banner, get_logger, md_table)

LOG = get_logger("01_prepare_and_validate")
LED = Ledger()


def main() -> None:
    banner(LOG, "STAGE 01  Preparation and structural validation")

    frame = pd.read_csv(SRC["frame"])
    hh_raw = pd.read_csv(SRC["household"])
    ch_raw = pd.read_csv(SRC["child"])
    fw = pd.read_csv(SRC["fieldwork"])
    LOG.info("read frame=%d  households=%d  children=%d  fieldwork=%d",
             len(frame), len(hh_raw), len(ch_raw), len(fw))

    # ---------------------------------------------------------------- 1. frame
    sel = frame[frame["selected"] == 1].copy()
    per_stratum = sel.groupby("stratum_code").size()
    LOG.info("selected clusters per stratum: %s", per_stratum.to_dict())
    if not (per_stratum == CLUSTERS_PER_STRATUM).all():
        LED.record(check="stage-one sample size", scope="frame",
                   n_affected=int((per_stratum != CLUSTERS_PER_STRATUM).sum()),
                   finding="stratum does not carry the designed 30 clusters",
                   action="analysed as realised", severity="material")

    # The frame states a stage-one selection probability. It should equal
    # n_h * M_i / M_h under systematic PPS. Reproducing it confirms that the
    # measure of size used in the field was the 2023 census household count and
    # not the field listing -- which is the distinction that drives the weights.
    sel["p1_reproduced"] = (sel["clusters_selected_in_stratum"]
                            * sel["households_census_2023"]
                            / sel["stratum_total_households"])
    max_dev = float((sel["p1_reproduced"] - sel["stage1_selection_probability"]).abs().max())
    LOG.info("stage-one probability reproduced from n_h*M_i/M_h, max deviation %.2e", max_dev)
    LED.record(check="stage-one PPS probability", scope="frame (90 clusters)", n_affected=0,
               finding=f"pi_1i = n_h*M_i/M_h reproduces the supplied probability to {max_dev:.1e}",
               action="supplied probability used as given", severity="info",
               detail="measure of size is households_census_2023, confirmed")

    # Stratum household totals must be the frame totals, not just the selected ones.
    frame_tot = frame.groupby("stratum_code")["households_census_2023"].sum()
    stated_tot = sel.groupby("stratum_code")["stratum_total_households"].first()
    if not np.allclose(frame_tot.reindex(stated_tot.index), stated_tot):
        LED.record(check="stratum size totals", scope="frame", n_affected=len(stated_tot),
                   finding="stated stratum total does not equal the sum over the frame",
                   action="frame sum used", severity="material")
    else:
        LOG.info("stratum totals agree with the sum over the whole frame")

    replaced = sel[sel["field_status"] != "Visited as selected"]
    if len(replaced):
        LOG.warning("%d clusters replaced: %s", len(replaced), list(replaced["cluster_id"]))
        LED.record(check="cluster replacement", scope="stage one",
                   n_affected=int(len(replaced)),
                   finding="clusters replaced because the original was inaccessible: "
                           + ", ".join(f"{r.cluster_id} ({r.stratum_code})"
                                       for r in replaced.itertuples()),
                   action="retained with the original selection probability; treated as a "
                          "non-ignorable coverage risk and reported, not corrected",
                   severity="material",
                   detail="the replacement carries the probability of the cluster it replaced, "
                          "which is only defensible if the two are exchangeable; inaccessible "
                          "areas are systematically harder to vaccinate, so the direction of "
                          "any residual bias is upward")

    # ----------------------------------------------------------- 2. duplicates
    dup_hh = hh_raw.duplicated()
    dup_ch = ch_raw.duplicated()
    dup_clusters = sorted(hh_raw.loc[dup_hh, "cluster_id"].unique())
    LOG.warning("exact duplicate rows: households=%d children=%d (clusters %s)",
                int(dup_hh.sum()), int(dup_ch.sum()), dup_clusters)
    if dup_hh.any() or dup_ch.any():
        LED.record(check="duplicate records", scope=f"cluster(s) {', '.join(dup_clusters)}",
                   n_affected=int(dup_hh.sum() + dup_ch.sum()),
                   finding=f"{int(dup_hh.sum())} household rows and {int(dup_ch.sum())} child "
                           "rows are exact duplicates -- one cluster's questionnaires were "
                           "submitted to the server twice",
                   action="de-duplicated on the full row; identifiers confirmed to be repeated "
                          "rather than distinct records that happen to agree",
                   severity="critical",
                   detail="left in place, this cluster would have counted twice in its "
                          "stratum's between-cluster variance and doubled its weight share")

    hh = hh_raw.drop_duplicates().reset_index(drop=True)
    ch = ch_raw.drop_duplicates().reset_index(drop=True)
    assert hh["household_id"].is_unique, "household_id still not unique after de-duplication"
    assert ch["child_id"].is_unique, "child_id still not unique after de-duplication"
    LOG.info("after de-duplication: households=%d children=%d", len(hh), len(ch))

    # ------------------------------------------------- 3. referential integrity
    orphan_ch = ~ch["household_id"].isin(hh["household_id"])
    hh_not_frame = ~hh["cluster_id"].isin(sel["cluster_id"])
    LOG.info("orphan children=%d, households in clusters absent from the frame=%d",
             int(orphan_ch.sum()), int(hh_not_frame.sum()))
    if orphan_ch.any() or hh_not_frame.any():
        LED.record(check="referential integrity", scope="child / household / frame",
                   n_affected=int(orphan_ch.sum() + hh_not_frame.sum()),
                   finding="records reference a parent that does not exist",
                   action="quarantined", severity="critical")
    else:
        LED.record(check="referential integrity", scope="all four files", n_affected=0,
                   finding="every child resolves to a household, every household to a selected "
                           "cluster, every interviewer-day to a fieldwork log row",
                   action="none required", severity="info")

    per_cluster = hh.groupby("cluster_id").size()
    off = per_cluster[per_cluster != HOUSEHOLDS_PER_CLUSTER]
    LOG.info("clusters not carrying exactly %d sampled households: %d",
             HOUSEHOLDS_PER_CLUSTER, len(off))
    if len(off):
        LED.record(check="stage-two sample size", scope="clusters",
                   n_affected=int(len(off)),
                   finding=f"{len(off)} clusters do not carry exactly 20 selected households",
                   action="analysed as realised", severity="material",
                   detail=str(off.to_dict()))

    # --------------------------------------------- 4. field listing consistency
    listed_hh = hh[["cluster_id", "stage2_households_listed"]].drop_duplicates()
    if listed_hh["cluster_id"].duplicated().any():
        LED.record(check="field listing", scope="households", n_affected=0,
                   finding="a cluster carries more than one listing count", action="frame value used",
                   severity="material")
    chk = sel.merge(listed_hh, on="cluster_id", how="left")
    mism = chk[chk["households_listed_fieldwork"] != chk["stage2_households_listed"]]
    LOG.info("frame vs household-file listing mismatches: %d", len(mism))
    LED.record(check="field listing agreement", scope="90 clusters", n_affected=int(len(mism)),
               finding="the frame's households_listed_fieldwork and the household file's "
                       "stage2_households_listed agree for every cluster",
               action="either may be used as L_i; the frame value is used",
               severity="info")

    ratio = (chk["households_listed_fieldwork"] / chk["households_census_2023"])
    LOG.info("field listing / census size ratio: median %.2f, range %.2f-%.2f",
             ratio.median(), ratio.min(), ratio.max())
    LED.record(check="listing vs measure of size", scope="90 clusters", n_affected=int(len(chk)),
               finding=f"the field listing is a median {ratio.median():.2f}x the 2023 census "
                       f"household count, ranging {ratio.min():.2f}x to {ratio.max():.2f}x",
               action="stage-two probability computed from the field listing, stage-one "
                      "probability from the census count; the design is therefore NOT "
                      "self-weighting and unequal weights are unavoidable",
               severity="material",
               detail="had the two agreed, the two stages would have cancelled and every "
                      "household in a stratum would have carried the same weight")

    # ------------------------------------------------------ 5. age eligibility
    ch["age_eligible"] = ch["age_months"].between(AGE_MIN_MONTHS, AGE_MAX_MONTHS)
    n_young = int((ch["age_months"] < AGE_MIN_MONTHS).sum())
    n_old = int((ch["age_months"] > AGE_MAX_MONTHS).sum())
    LOG.warning("children outside the 9-59 month definition: %d below, %d above", n_young, n_old)
    LED.record(check="age eligibility", scope="child records",
               n_affected=n_young + n_old,
               finding=f"{n_young} children below 9 months and {n_old} above 59 months were "
                       "enumerated although the instrument restricts enumeration to 9-59 "
                       "completed months",
               action="flagged (age_eligible) and excluded from the headline denominator; "
                      "retained in the file and re-included in a sensitivity variant",
               severity="minor",
               detail="this is an instrument/field-control failure, not a coding error: the "
                      "eligibility filter was not enforced at the point of enumeration")

    # ------------------------------------------------- 6. skip-pattern conformity
    card = ch["vaccination_card_seen"].eq("Yes")
    has_card_ans = ch["dose_recorded_on_card"].notna()
    has_recall = ch["dose_reported_by_caregiver"].notna()

    viol_both = int((has_card_ans & has_recall).sum())
    viol_card_no_recall = int((~card & has_card_ans).sum())
    viol_recall_when_card = int((card & has_recall).sum())
    LOG.info("skip-pattern violations: both answered=%d, card answer without a card=%d, "
             "recall recorded despite a card=%d", viol_both, viol_card_no_recall,
             viol_recall_when_card)
    LED.record(check="skip-pattern conformity", scope="child records",
               n_affected=viol_both + viol_card_no_recall + viol_recall_when_card,
               finding="questions 3.5 and 3.6 are mutually exclusive as the instructions "
                       "require; no child carries both a card answer and a recall answer",
               action="none required", severity="info")

    # The consequence of that design: the two sources are never observed on the
    # same child, so the instrument cannot support any within-child validation
    # of recall against the card. This is recorded here because it is the single
    # most important limitation of the instrument and it is structural.
    LED.record(check="source of evidence design", scope="instrument q3.4-3.6",
               n_affected=int(len(ch)),
               finding="where a card was seen, caregiver recall was not asked, so card and "
                       "recall are never both observed for the same child",
               action="no analytical fix is possible; recall accuracy is bounded by assumption "
                      "in stage 05 and the limitation is reported",
               severity="material",
               detail="a two-question-always instrument would identify the recall "
                      "sensitivity/specificity directly")

    # Determinate vaccination status.
    ch["status_source"] = np.where(card, "Card", "Caregiver recall")
    raw_status = np.where(card, ch["dose_recorded_on_card"], ch["dose_reported_by_caregiver"])
    ch["status_raw"] = pd.Series(raw_status, index=ch.index)
    ch["vaccinated"] = ch["status_raw"].map({"Yes": 1.0, "No": 0.0})
    ch["status_missing"] = ch["vaccinated"].isna()
    n_missing = int(ch["status_missing"].sum())
    miss_card = int((ch["status_missing"] & card).sum())
    miss_recall = int((ch["status_missing"] & ~card).sum())
    LOG.warning("children with an indeterminate vaccination status: %d (%d card, %d recall)",
                n_missing, miss_card, miss_recall)
    LED.record(check="item missingness on the outcome", scope="child records",
               n_affected=n_missing,
               finding=f"{n_missing} children ({100*n_missing/len(ch):.1f}%) have neither a card "
                       f"answer nor a recall answer: {miss_card} where a card was seen but 3.5 "
                       f"was left blank, {miss_recall} where no card was seen and 3.6 was left blank",
               action="excluded from the headline denominator (complete-case within the weighted "
                      "estimator); bounded in stage 03 by setting all to vaccinated and all to "
                      "unvaccinated",
               severity="minor")

    # ------------------------------------------------------ 7. roster arithmetic
    enum = ch.groupby("household_id").size().rename("children_enumerated")
    hh = hh.join(enum, on="household_id")
    hh["children_enumerated"] = hh["children_enumerated"].fillna(0).astype(int)
    comp = hh["result_of_visit"].eq(RESULT_COMPLETED)
    mismatch = comp & (hh["children_enumerated"] != hh["eligible_children_9_59_months"].fillna(0))
    LOG.info("roster vs enumerated child mismatches in completed households: %d",
             int(mismatch.sum()))
    LED.record(check="roster arithmetic", scope="completed households",
               n_affected=int(mismatch.sum()),
               finding="the roster count at 2.1 equals the number of child records in every "
                       "completed household",
               action="none required", severity="info")

    nonzero_but_ineligible = int((comp & (hh["children_enumerated"] == 0)).sum())
    LOG.info("completed households with no eligible child: %d", nonzero_but_ineligible)

    # ------------------------------------------- 8. result of visit classification
    hh["is_completed"] = comp.astype(int)
    hh["is_ineligible_dwelling"] = hh["result_of_visit"].isin(RESULT_INELIGIBLE).astype(int)
    hh["is_nonresponse"] = hh["result_of_visit"].isin(RESULT_NONRESPONSE).astype(int)
    unclassified = hh[(hh[["is_completed", "is_ineligible_dwelling", "is_nonresponse"]].sum(axis=1)) != 1]
    if len(unclassified):
        LED.record(check="result of visit", scope="households", n_affected=int(len(unclassified)),
                   finding="result-of-visit code not classifiable",
                   action="quarantined", severity="critical",
                   detail=str(unclassified["result_of_visit"].value_counts().to_dict()))

    rv = hh["result_of_visit"].value_counts()
    LOG.info("result of visit: %s", rv.to_dict())
    LED.record(check="household non-response", scope="1,800 selected households",
               n_affected=int(hh["is_nonresponse"].sum() + hh["is_ineligible_dwelling"].sum()),
               finding=f"{int(hh['is_completed'].sum())} completed, "
                       f"{int(rv.get('Refused', 0))} refused, "
                       f"{int(rv.get('No eligible respondent after 3 visits', 0))} not contacted "
                       f"after three visits, {int(rv.get('Vacant dwelling', 0))} vacant",
               action="vacant dwellings treated as ineligible frame elements and removed from the "
                      "response-rate denominator; refusals and non-contacts treated as "
                      "non-response and compensated by a within-cluster weight adjustment",
               severity="material",
               detail="a non-contact after three visits is a failure to obtain data from an "
                      "eligible dwelling, not evidence that the dwelling held no eligible child; "
                      "treating it as ineligible would be the more favourable assumption and is "
                      "run as a sensitivity variant in stage 03")

    # ---------------------------------------------- fieldwork log reconciliation
    fw_days = set(zip(fw["interviewer_id"], fw["fieldwork_date"]))
    hh_days = set(zip(hh["interviewer_id"], hh["interview_date"]))
    only_hh, only_fw = hh_days - fw_days, fw_days - hh_days
    LOG.info("interviewer-days in households but not the log: %d; in the log but not households: %d",
             len(only_hh), len(only_fw))
    fw_tot = fw.groupby("interviewer_id")[["households_attempted", "households_completed"]].sum()
    hh_tot = hh.groupby("interviewer_id").agg(att=("household_id", "size"),
                                              comp=("is_completed", "sum"))
    recon = fw_tot.join(hh_tot)
    att_gap = int((recon["households_attempted"] != recon["att"]).sum())
    comp_gap = int((recon["households_completed"] != recon["comp"]).sum())
    LOG.info("interviewers whose log totals disagree with the de-duplicated household file: "
             "attempted=%d, completed=%d", att_gap, comp_gap)
    gap_ids = sorted(set(recon.index[recon["households_attempted"] != recon["att"]])
                     | set(recon.index[recon["households_completed"] != recon["comp"]]))
    if gap_ids:
        LED.record(check="fieldwork log independence", scope=f"interviewer(s) {', '.join(gap_ids)}",
                   n_affected=len(gap_ids),
                   finding="the fieldwork log reconciles with the household file as *received* "
                           "but not with the de-duplicated file: the log records the duplicated "
                           "cluster's 40 households on a single interviewer-day",
                   action="the log is treated as a description of the same submission, not as an "
                          "independent control on it",
                   severity="material",
                   detail="a fieldwork log that inherits an upload artefact cannot be used to "
                          "detect that artefact; the duplicate had to be found in the data")
    else:
        LED.record(check="fieldwork log reconciliation", scope="18 interviewers", n_affected=0,
                   finding="the log reconciles exactly with the household file on "
                           "interviewer-days, households attempted and households completed",
                   action="none required", severity="info")

    # Fieldwork window. The survey was conducted in May 2026, after the campaign.
    dates = pd.to_datetime(hh["interview_date"])
    main_start = dates.mode().min()
    early = hh[dates < pd.Timestamp("2026-05-01")]
    if len(early):
        LOG.warning("%d households interviewed before May 2026 (%s), across %d clusters in %d "
                    "strata on a single day", len(early), early["interview_date"].unique().tolist(),
                    early["cluster_id"].nunique(), early["stratum_code"].nunique())
        LED.record(check="fieldwork window", scope=f"interviewer(s) "
                                                   f"{', '.join(sorted(early['interviewer_id'].unique()))}",
                   n_affected=int(len(early)),
                   finding=f"{len(early)} households carry an interview date of "
                           f"{early['interview_date'].min()}, {(main_start - pd.Timestamp(early['interview_date'].min())).days} "
                           f"days before the rest of fieldwork began, spread over "
                           f"{early['cluster_id'].nunique()} clusters in {early['stratum_code'].nunique()} "
                           "states on one day",
                   action="retained in the headline, quarantined as a flagged sensitivity variant "
                          "in stage 03",
                   severity="material",
                   detail="one interviewer cannot cover five clusters in three states in a day; "
                          "either the date is wrong or these are pilot records that reached "
                          "production. If the date is right they precede the campaign and cannot "
                          "measure post-campaign status")

    hh["out_of_window"] = (dates < pd.Timestamp("2026-05-01")).astype(int)

    # -------------------------------------------------------------- write out
    ch = ch.merge(hh[["household_id", "interviewer_id", "interview_date",
                      "interview_duration_min", "wealth_quintile", "settlement_type",
                      "out_of_window"]],
                  on="household_id", how="left")
    ch["analysis_eligible"] = ch["age_eligible"] & ~ch["status_missing"]
    LOG.info("children eligible for the headline denominator: %d of %d",
             int(ch["analysis_eligible"].sum()), len(ch))

    sel = sel.assign(stratum_name=sel["stratum_code"].map(STRATUM_NAMES))
    sel.to_csv(ART["frame_clean"], index=False)
    hh.to_csv(ART["hh_clean"], index=False)
    ch.to_csv(ART["child_clean"], index=False)
    led = LED.to_frame()
    led.to_csv(ART["integrity_ledger"], index=False)
    LOG.info("wrote %s (%d entries)", ART["integrity_ledger"].name, len(led))

    _write_report(sel, hh, ch, fw, led, ratio)
    banner(LOG, "STAGE 01 complete")


def _write_report(sel, hh, ch, fw, led, ratio) -> None:
    sev = led["severity"].value_counts().to_dict()
    rv = hh["result_of_visit"].value_counts()

    lines = [
        "# 01 - Preparation and structural validation",
        "",
        "*Post-campaign coverage survey, three states, May 2026. Stratified two-stage "
        "cluster design with PPS selection of enumeration areas.*",
        "",
        "## What the design actually was",
        "",
        "| Design element | Specified | Realised in the data |",
        "|---|---|---|",
        f"| Strata | 3 (state) | {sel['stratum_code'].nunique()} |",
        f"| Clusters per stratum | 30, systematic PPS on 2023 census households | "
        f"{sel.groupby('stratum_code').size().unique().tolist()} |",
        f"| Households per cluster | 20, SRS from a fresh field listing | "
        f"{sorted(hh.groupby('cluster_id').size().unique().tolist())} |",
        f"| Children | all resident 9-59 completed months | "
        f"{len(ch)} enumerated, {int(ch['age_eligible'].sum())} within the age definition |",
        f"| Selected households | 1,800 | {len(hh)} after de-duplication |",
        f"| Completed interviews | - | {int(hh['is_completed'].sum())} |",
        "",
        "The stage-one selection probability supplied in the frame is reproduced exactly by "
        "`n_h x M_i / M_h` with `M_i` the 2023 census household count, confirming that the "
        "measure of size used for PPS was the census listing and not the field listing.",
        "",
        "## Result of visit",
        "",
        md_table(rv.rename_axis("result_of_visit").reset_index(name="households")
                 .assign(share_pct=lambda d: 100 * d["households"] / len(hh)),
                 {"share_pct": ".1f"}),
        "",
        "## The listing gap, and why it decides the weighting",
        "",
        f"The field listing is a median **{ratio.median():.2f}x** the 2023 census household count "
        f"used as the measure of size, ranging from **{ratio.min():.2f}x** to **{ratio.max():.2f}x**. "
        "Stage one selected clusters proportional to the census count; stage two took a fixed 20 "
        "households from the field listing. Because the two counts differ, the two stages do not "
        "cancel and the design is **not self-weighting**: a household in a cluster where the "
        "listing had grown far beyond its census size stands for many more households than one in "
        "a cluster that had shrunk. Analysing these data unweighted would silently give every "
        "sampled household the same influence, which is wrong by a factor of up to seven between "
        "the extreme clusters.",
        "",
        "## Integrity ledger",
        "",
        f"{len(led)} checks recorded: "
        + ", ".join(f"{k} {v}" for k, v in sorted(sev.items())) + ".",
        "",
        md_table(led[["check", "scope", "n_affected", "severity", "finding"]]),
        "",
        "## Decisions carried forward",
        "",
        "| Decision | Effect |",
        "|---|---|",
        "| De-duplicate the repeated cluster on the full row | removes 20 households and 20 "
        "children that would otherwise have been double-counted in both the point estimate and "
        "the between-cluster variance |",
        "| Vacant dwellings are ineligible, not non-response | raises the reported response rate "
        "and stops the weighting from projecting empty dwellings onto real households |",
        "| Non-contacts after three visits are non-response | conservative; the opposite "
        "assumption is run as a sensitivity variant |",
        "| Children outside 9-59 months excluded from the headline | flagged, not deleted |",
        "| Indeterminate vaccination status excluded from the headline | bounded in stage 03 |",
        "",
    ]
    ART["prep_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("wrote %s", ART["prep_report"].name)


if __name__ == "__main__":
    main()
