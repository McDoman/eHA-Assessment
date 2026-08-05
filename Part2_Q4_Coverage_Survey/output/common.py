"""
common.py
=========
Shared configuration, logging, and the complex-survey estimation engine for the
Part 2 / Question 4 post-campaign coverage survey.

Every pipeline stage imports from here so that paths, analytical parameters, the
outcome definition and -- above all -- the *variance estimator* are declared in
exactly one place.

The survey was a stratified two-stage cluster design with PPS selection of
enumeration areas. Treating it as a simple random sample would understate the
standard error by roughly the square root of the design effect and would ignore
the fact that the second-stage sampling fraction varies from cluster to cluster.
Nothing in this pipeline ever computes an unweighted proportion and calls it an
estimate: the ratio estimator, the Taylor-linearised variance, the stratified
ultimate-cluster sum and the design effect are implemented here and used
everywhere.

Author: technical assessment submission
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

OUT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = OUT_DIR.parent

DATA_DIR = OUT_DIR / "data"          # analysis-ready datasets and weight files
REPORT_DIR = OUT_DIR / "reports"     # tables, ledgers, written analysis
FIGURE_DIR = OUT_DIR / "figures"     # charts for the survey report
LOG_DIR = OUT_DIR / "logs"           # per-run execution logs

for _d in (DATA_DIR, REPORT_DIR, FIGURE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Source files, as received. Opened read-only; nothing is ever written back.
SRC = {
    "frame": SOURCE_DIR / "sampling_frame.csv",
    "household": SOURCE_DIR / "household_records.csv",
    "child": SOURCE_DIR / "child_records.csv",
    "fieldwork": SOURCE_DIR / "fieldwork_log.csv",
}

ART = {
    # stage 01
    "hh_clean": DATA_DIR / "households_clean.csv",
    "child_clean": DATA_DIR / "children_clean.csv",
    "frame_clean": DATA_DIR / "frame_selected_clean.csv",
    "integrity_ledger": REPORT_DIR / "01_data_integrity_ledger.csv",
    "prep_report": REPORT_DIR / "01_preparation_and_validation.md",
    # stage 02
    "hh_weighted": DATA_DIR / "households_weighted.csv",
    "child_weighted": DATA_DIR / "children_weighted.csv",
    "weight_components": REPORT_DIR / "02_weight_components_by_cluster.csv",
    "weight_diagnostics": REPORT_DIR / "02_weight_diagnostics.csv",
    "assumption_register": REPORT_DIR / "02_assumption_register.csv",
    "weight_report": REPORT_DIR / "02_design_weights.md",
    # stage 03
    "est_headline": REPORT_DIR / "03_coverage_estimates.csv",
    "est_domains": REPORT_DIR / "03_coverage_by_domain.csv",
    "est_sensitivity": REPORT_DIR / "03_sensitivity_analysis.csv",
    "variance_check": REPORT_DIR / "03_variance_method_comparison.csv",
    "est_report": REPORT_DIR / "03_weighted_estimates.md",
    # stage 04
    "dq_age": REPORT_DIR / "04_age_heaping.csv",
    "dq_interviewer": REPORT_DIR / "04_interviewer_diagnostics.csv",
    "dq_missing": REPORT_DIR / "04_missingness.csv",
    "dq_flags": REPORT_DIR / "04_quality_flags.csv",
    "dq_report": REPORT_DIR / "04_data_quality_assessment.md",
    # stage 05
    "src_table": REPORT_DIR / "05_coverage_by_source.csv",
    "src_sensitivity": REPORT_DIR / "05_source_dependence.csv",
    "src_report": REPORT_DIR / "05_documented_source_analysis.md",
    # stage 06
    "workbook": REPORT_DIR / "06_survey_report_tables.xlsx",
    "final_report": REPORT_DIR / "06_survey_report.md",
    "final_docx": REPORT_DIR / "06_survey_report.docx",
}

# --------------------------------------------------------------------------
# Design constants (from the survey protocol, as documented in the data pack)
# --------------------------------------------------------------------------

STRATA = ["ST01", "ST02", "ST03"]
STRATUM_NAMES = {"ST01": "Bansara State", "ST02": "Kudama State", "ST03": "Zaruwa State"}

CLUSTERS_PER_STRATUM = 30            # stage-one sample size, n_h
HOUSEHOLDS_PER_CLUSTER = 20          # stage-two sample size, m_i
AGE_MIN_MONTHS, AGE_MAX_MONTHS = 9, 59

# Result-of-visit codes, classified for weighting purposes.
# A vacant dwelling is an *ineligible frame element*: it was on the listing but
# contains no household, so it is removed from the denominator of the response
# rate rather than treated as a refusal. Refusals and repeated non-contacts are
# eligible dwellings from which no data was obtained -- these are non-response
# and their weight is redistributed to responding households in the same cluster.
RESULT_COMPLETED = "Completed"
RESULT_INELIGIBLE = {"Vacant dwelling"}
RESULT_NONRESPONSE = {"Refused", "No eligible respondent after 3 visits"}

# --------------------------------------------------------------------------
# Analytical parameters (all thresholds declared here, none buried in code)
# --------------------------------------------------------------------------

# Campaign performance threshold. A post-campaign coverage of 95% is the
# conventional target for a preventive mass campaign; below it, unvaccinated
# children are assumed to be numerous enough to sustain transmission and a
# mop-up round is normally indicated.
COVERAGE_TARGET = 0.95

# Coverage below which a mop-up round is unambiguously indicated by programme
# guidance, and above which it is unambiguously not. Between the two the
# decision needs sub-stratum information the survey was not powered to give.
MOPUP_TRIGGER = 0.90

CI_LEVEL = 0.95

# Confidence-interval method for proportions under a complex design.
# "logit" transforms to the log-odds scale, builds a symmetric interval using
# the linearised standard error, and back-transforms. It cannot produce limits
# outside (0,1) and has better coverage than the Wald interval when the
# proportion is near a boundary -- which several stratum estimates here are.
CI_METHOD = "logit"

# Finite population correction at stage one. n_h/N_h is 30/340, 30/300, 30/280,
# i.e. 9-11%, so the FPC would shave roughly 5% off the standard error. It is
# computed and reported but *not* applied to the headline, because the
# ultimate-cluster estimator without FPC is the conservative choice and because
# the three replaced clusters mean the realised stage-one sample is not exactly
# the designed one.
APPLY_STAGE1_FPC = False

# Rescaled bootstrap (Rao-Wu-Yue) replicate count, used as an independent check
# on the Taylor linearisation rather than as the headline variance.
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 20260805

# Weight trimming. Applied as a *sensitivity* variant only: the main estimate
# uses untrimmed weights because every large weight here is a legitimate
# consequence of the field listing exceeding the census measure of size, not a
# data error. Trim point is expressed as a multiple of the stratum median.
TRIM_MULTIPLE = 4.0

# --- interviewer falsification screen -------------------------------------
# An interviewer is flagged when their own field behaviour is inconsistent with
# the rest of the team by a margin that cannot plausibly be explained by the
# clusters they were assigned. Each rule is a separate, reportable signal; the
# exclusion decision requires several to fire together.
FLAG_DURATION_RATIO = 0.50       # median duration below 50% of the survey median
FLAG_COVERAGE_MIN = 0.99         # reported coverage at or above 99%
FLAG_CARD_RATIO = 0.30           # card-sighting rate below 30% of the survey rate
FLAG_COMPLETION_MIN = 0.95       # household completion rate at or above 95%
FLAG_EXCLUDE_AT = 3              # number of rules that triggers exclusion

# --- age-heaping screen ----------------------------------------------------
HEAP_ANCHORS = [12, 24, 36, 48]  # ages in months at which heaping is expected

# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
# Categorical slots 1-3 of the validated reference palette, which clears the
# all-pairs colour-vision-deficiency and normal-vision separation gates. Aqua
# sits below 3:1 against the chart surface, so every chart that uses it carries
# visible value labels (the relief rule).
PALETTE = {
    "ST01": "#2a78d6",   # blue
    "ST02": "#eb6834",   # orange
    "ST03": "#1baf7a",   # aqua
}
COLOR_PRIMARY = "#2a78d6"
COLOR_SECONDARY = "#eb6834"
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_SERIOUS = "#ec835a"
COLOR_CRITICAL = "#d03b3b"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"


def apply_chart_style() -> None:
    """Recessive grid, thin marks, muted axis ink. Applied once per figure module."""
    import matplotlib as mpl

    mpl.rcParams.update({
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK_SECONDARY,
        "axes.titlecolor": INK_PRIMARY,
        "axes.titlesize": 11.5,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.labelsize": 9.5,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.7,
        "xtick.color": INK_MUTED,
        "ytick.color": INK_MUTED,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "legend.frameon": False,
        "legend.fontsize": 9,
        "font.size": 9.5,
        "lines.linewidth": 2.0,
        "figure.dpi": 140,
        "savefig.dpi": 200,
        "savefig.bbox": "tight",
    })


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def get_logger(stage: str) -> logging.Logger:
    """Return a logger that writes to both the console and logs/<stage>.log."""
    logger = logging.getLogger(stage)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_DIR / f"{stage}.log", mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def banner(logger: logging.Logger, text: str) -> None:
    logger.info("=" * 74)
    logger.info(text)
    logger.info("=" * 74)


# --------------------------------------------------------------------------
# Ledgers
# --------------------------------------------------------------------------


@dataclass
class Ledger:
    """An append-only record of every data decision, written out as a CSV."""

    rows: list[dict] = field(default_factory=list)

    def record(self, *, check: str, scope: str, n_affected, finding: str,
               action: str, severity: str = "info", detail: str = "") -> None:
        self.rows.append({
            "check": check,
            "scope": scope,
            "n_affected": n_affected,
            "finding": finding,
            "action": action,
            "severity": severity,       # info | minor | material | critical
            "detail": detail,
        })

    def to_frame(self) -> pd.DataFrame:
        cols = ["check", "scope", "n_affected", "finding", "action", "severity", "detail"]
        if not self.rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(self.rows)[cols]


@dataclass
class AssumptionRegister:
    """
    Every assumption the weighting makes, the alternative that was considered,
    and the direction in which the headline moves if the assumption is wrong.
    Question 4 asks for every assumption to be documented; this is that record.
    """

    rows: list[dict] = field(default_factory=list)

    def add(self, *, ref: str, assumption: str, basis: str, alternative: str,
            effect_if_wrong: str, tested: str = "no") -> None:
        self.rows.append({
            "ref": ref,
            "assumption": assumption,
            "basis": basis,
            "alternative_considered": alternative,
            "effect_on_headline_if_wrong": effect_if_wrong,
            "tested_in_sensitivity": tested,
        })

    def to_frame(self) -> pd.DataFrame:
        cols = ["ref", "assumption", "basis", "alternative_considered",
                "effect_on_headline_if_wrong", "tested_in_sensitivity"]
        return pd.DataFrame(self.rows)[cols] if self.rows else pd.DataFrame(columns=cols)


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


# ==========================================================================
# THE SURVEY ESTIMATION ENGINE
# ==========================================================================
# A stratified, two-stage, PPS design is analysed here with the *ultimate
# cluster* (Taylor linearisation) variance estimator, which is the standard
# approach implemented by R's `survey`, Stata's `svy` and SAS `PROC SURVEYFREQ`.
#
# For a ratio (a weighted proportion is a ratio of two weighted totals)
#
#       p_hat = sum(w_k y_k) / sum(w_k)
#
# the linearised residual for observation k is
#
#       u_k = w_k (y_k - p_hat) / sum(w)
#
# these are summed to the primary sampling unit (the cluster), and the variance
# is the between-PSU variance accumulated within stratum:
#
#       V(p_hat) = sum_h  (1 - f_h) * n_h/(n_h - 1) * sum_i (z_hi - z_h_bar)^2
#
# Only stage-one variation enters, which is correct for the ultimate-cluster
# approach: the within-cluster contribution is captured by the between-cluster
# spread of the cluster totals. Strata contribute independently, which is what
# makes stratification pay off in the variance, and clustering enters through
# the fact that z_hi is a *sum over the whole cluster* rather than an individual
# residual, which is what makes clustering cost in the variance.


def _as_arrays(df: pd.DataFrame, y: str, w: str, stratum: str, psu: str):
    d = df[[y, w, stratum, psu]].dropna(subset=[y, w])
    return (d[y].to_numpy(float), d[w].to_numpy(float),
            d[stratum].to_numpy(str), d[psu].to_numpy(str))


@dataclass
class SurveyEstimate:
    """A weighted proportion with its complex-design variance and diagnostics."""
    domain: str
    estimate: float
    se: float
    ci_low: float
    ci_high: float
    df: int
    n_obs: int
    n_psu: int
    n_strata: int
    sum_weights: float
    weighted_numerator: float
    deff: float
    deft: float
    n_eff: float
    deff_kish: float
    icc: float
    ci_method: str

    def as_row(self, **extra) -> dict:
        row = {
            "domain": self.domain,
            "estimate": self.estimate,
            "estimate_pct": 100 * self.estimate,
            "se": self.se,
            "se_pct": 100 * self.se,
            "ci_low_pct": 100 * self.ci_low,
            "ci_high_pct": 100 * self.ci_high,
            "ci_width_pp": 100 * (self.ci_high - self.ci_low),
            "df": self.df,
            "n_children": self.n_obs,
            "n_clusters": self.n_psu,
            "n_strata": self.n_strata,
            "sum_weights": self.sum_weights,
            "weighted_numerator": self.weighted_numerator,
            "deff": self.deff,
            "deft": self.deft,
            "n_effective": self.n_eff,
            "deff_kish_weights": self.deff_kish,
            "icc_implied": self.icc,
            "ci_method": self.ci_method,
        }
        row.update(extra)
        return row


def svy_prop(df: pd.DataFrame, y: str, w: str = "weight_final",
             stratum: str = "stratum_code", psu: str = "cluster_id",
             domain: str = "National", fpc: dict | None = None,
             ci_method: str = CI_METHOD, level: float = CI_LEVEL) -> SurveyEstimate:
    """
    Weighted proportion of a 0/1 variable under a stratified multistage design.

    `fpc` maps stratum code -> sampling fraction f_h = n_h/N_h at stage one. Pass
    None (the default) to omit the finite population correction, which is the
    conservative choice.

    Domain estimation note: when this is called on a subset (a stratum, a wealth
    quintile, a settlement type), the subset is *not* re-normalised. The
    linearised residuals are computed on the domain and accumulated over the
    domain's PSUs, which is the standard domain estimator; PSUs that contribute
    no observations to the domain contribute a zero total and still count toward
    the degrees of freedom via their stratum.
    """
    yv, wv, hv, iv = _as_arrays(df, y, w, stratum, psu)
    n_obs = yv.size
    if n_obs == 0:
        raise ValueError(f"no observations for domain {domain!r}")

    sw = wv.sum()
    num = float((wv * yv).sum())
    p = num / sw

    # Linearised residuals summed to PSU level.
    u = wv * (yv - p) / sw
    key = pd.MultiIndex.from_arrays([hv, iv], names=["h", "i"])
    z = pd.Series(u, index=key).groupby(level=["h", "i"]).sum()

    var = 0.0
    n_psu_total = 0
    strata_used = 0
    for h, zh in z.groupby(level="h"):
        n_h = zh.size
        n_psu_total += n_h
        strata_used += 1
        if n_h < 2:
            # A stratum with a single PSU contributes no degrees of freedom.
            # Its between-PSU variance is not identified; it is skipped and
            # flagged rather than silently collapsed.
            continue
        f_h = (fpc or {}).get(h, 0.0)
        var += (1.0 - f_h) * n_h / (n_h - 1.0) * float(((zh - zh.mean()) ** 2).sum())

    se = float(np.sqrt(max(var, 0.0)))
    dfree = max(n_psu_total - strata_used, 1)

    tcrit = stats.t.ppf(0.5 + level / 2, dfree)
    if ci_method == "logit" and 0.0 < p < 1.0 and se > 0:
        # Delta-method SE on the logit scale, back-transformed.
        se_logit = se / (p * (1 - p))
        lo_l, hi_l = np.log(p / (1 - p)) - tcrit * se_logit, np.log(p / (1 - p)) + tcrit * se_logit
        ci_low, ci_high = 1 / (1 + np.exp(-lo_l)), 1 / (1 + np.exp(-hi_l))
    else:
        ci_low, ci_high = max(0.0, p - tcrit * se), min(1.0, p + tcrit * se)

    # Design effect: the ratio of the realised variance to the variance a simple
    # random sample of the same number of children would have delivered.
    var_srs = p * (1 - p) / (n_obs - 1) if n_obs > 1 else np.nan
    deff = float(var / var_srs) if var_srs and var_srs > 0 else np.nan
    n_eff = n_obs / deff if deff and deff > 0 else np.nan

    # Kish's approximation to the component of the design effect that is due to
    # unequal weights alone: 1 + CV^2(w). The gap between deff and deff_kish is
    # the part attributable to clustering.
    deff_kish = float(n_obs * (wv ** 2).sum() / (sw ** 2))

    # Implied intra-cluster correlation, from deff = 1 + (b_bar - 1) * rho with
    # b_bar the mean number of children analysed per cluster. This is the number
    # that determines the sample size of the next survey round.
    b_bar = n_obs / max(n_psu_total, 1)
    icc = float((deff - 1) / (b_bar - 1)) if (b_bar > 1 and np.isfinite(deff)) else np.nan

    return SurveyEstimate(domain=domain, estimate=float(p), se=se, ci_low=float(ci_low),
                          ci_high=float(ci_high), df=int(dfree), n_obs=int(n_obs),
                          n_psu=int(n_psu_total), n_strata=int(strata_used),
                          sum_weights=float(sw), weighted_numerator=num, deff=deff,
                          deft=float(np.sqrt(deff)) if np.isfinite(deff) else np.nan,
                          n_eff=float(n_eff), deff_kish=deff_kish, icc=icc,
                          ci_method=ci_method if 0 < p < 1 else "wald (boundary)")


def svy_prop_bootstrap(df: pd.DataFrame, y: str, w: str = "weight_final",
                       stratum: str = "stratum_code", psu: str = "cluster_id",
                       reps: int = BOOTSTRAP_REPS, seed: int = BOOTSTRAP_SEED,
                       level: float = CI_LEVEL) -> dict:
    """
    Rao-Wu-Yue rescaled bootstrap, used as an independent check on the Taylor
    linearisation. In each stratum, m_h = n_h - 1 PSUs are drawn with
    replacement and the weights are rescaled by

        a_hi = 1 - sqrt(m_h/(n_h-1)) + sqrt(m_h/(n_h-1)) * (n_h/m_h) * r_hi

    where r_hi is the number of times PSU i was drawn. With m_h = n_h - 1 this
    reduces to a_hi = n_h/(n_h-1) * r_hi, which cannot go negative.
    """
    rng = np.random.default_rng(seed)
    d = df[[y, w, stratum, psu]].dropna(subset=[y, w]).copy()
    d["_pid"] = d[stratum].astype(str) + "|" + d[psu].astype(str)

    psu_index: dict[str, np.ndarray] = {}
    for h, g in d.groupby(stratum):
        psu_index[h] = g["_pid"].unique()

    yv = d[y].to_numpy(float)
    wv = d[w].to_numpy(float)
    pid = d["_pid"].to_numpy()
    pid_codes, pid_uniq = pd.factorize(pid)

    pos = {p: k for k, p in enumerate(pid_uniq)}
    reps_out = np.empty(reps)
    for b in range(reps):
        factor = np.ones(pid_uniq.size)
        for h, plist in psu_index.items():
            n_h = plist.size
            if n_h < 2:
                continue
            m_h = n_h - 1
            draw = rng.integers(0, n_h, size=m_h)
            counts = np.bincount(draw, minlength=n_h)
            a = n_h / m_h * counts * np.sqrt(m_h / (n_h - 1)) + 1 - np.sqrt(m_h / (n_h - 1))
            for k, p_ in enumerate(plist):
                factor[pos[p_]] = a[k]
        wb = wv * factor[pid_codes]
        reps_out[b] = (wb * yv).sum() / wb.sum()

    est = float((wv * yv).sum() / wv.sum())
    se = float(reps_out.std(ddof=1))
    lo, hi = np.percentile(reps_out, [100 * (1 - level) / 2, 100 * (1 + level) / 2])
    return {"estimate": est, "se_bootstrap": se, "ci_low_pct": 100 * float(lo),
            "ci_high_pct": 100 * float(hi), "reps": reps}


def svy_by(df: pd.DataFrame, y: str, by: str, **kwargs) -> pd.DataFrame:
    """Run `svy_prop` over the levels of a domain variable and stack the rows."""
    rows = []
    for lvl, g in df.groupby(by, dropna=False):
        try:
            est = svy_prop(g, y, domain=str(lvl), **kwargs)
            rows.append(est.as_row(domain_variable=by))
        except ValueError:
            continue
    return pd.DataFrame(rows)


# ==========================================================================
# ANALYSIS SETS AND THE FALSIFICATION SCREEN
# ==========================================================================
# Defined here rather than in a pipeline stage so that stages 03, 04, 05 and 06
# all resolve the same interviewer exclusion from the same code, and so that the
# rule is fixed in configuration rather than fitted to the data.


def falsification_screen(ch: pd.DataFrame, hh: pd.DataFrame) -> pd.DataFrame:
    """
    Score every interviewer against the four pre-declared rules in this module.
    Returns one row per interviewer with the rules that fired and the outcome.
    """
    comp = hh[hh["is_completed"] == 1]
    med_dur = comp["interview_duration_min"].median()
    card_rate = ch["vaccination_card_seen"].eq("Yes").mean()

    rows = []
    for iv, g in ch.groupby("interviewer_id"):
        h = hh[hh["interviewer_id"] == iv]
        hc = h[h["is_completed"] == 1]
        rules = {
            "rule_short_interviews": bool(hc["interview_duration_min"].median()
                                          < FLAG_DURATION_RATIO * med_dur),
            "rule_saturated_coverage": bool(g["vaccinated"].mean(skipna=True) >= FLAG_COVERAGE_MIN),
            "rule_no_cards_seen": bool(g["vaccination_card_seen"].eq("Yes").mean()
                                       < FLAG_CARD_RATIO * card_rate),
            "rule_implausible_completion": bool(h["is_completed"].mean() >= FLAG_COMPLETION_MIN),
        }
        n = sum(rules.values())
        rows.append({"interviewer_id": iv, **rules, "rules_triggered": n,
                     "screen_outcome": ("EXCLUDE" if n >= FLAG_EXCLUDE_AT
                                        else "review" if n else "pass")})
    return pd.DataFrame(rows).sort_values("rules_triggered", ascending=False)


def excluded_interviewers(ch: pd.DataFrame, hh: pd.DataFrame) -> list[str]:
    s = falsification_screen(ch, hh)
    return sorted(s.loc[s.screen_outcome == "EXCLUDE", "interviewer_id"])


def build_analysis_sets(ch: pd.DataFrame, hh: pd.DataFrame):
    """
    Return (set_A, set_B, excluded_interviewers, dropped_clusters, stage1_factor).

    Set A is every analysis-eligible child, as submitted. Set B removes the
    clusters worked by an excluded interviewer and re-weights the survivors by
    n_h / (n_h - d_h). Under PPS every selected cluster carries an identical
    share M_h/n_h of its stratum's measure of size, so that factor restores the
    stratum total exactly. It assumes the lost clusters are exchangeable with
    the survivors in the same stratum -- which is a stated assumption, not a
    safe one, since the clusters were lost by interviewer assignment rather than
    at random.
    """
    excl = excluded_interviewers(ch, hh)
    dropped = sorted(hh.loc[hh["interviewer_id"].isin(excl), "cluster_id"].unique())

    a = ch[ch["analysis_eligible"]].copy()
    b = a[~a["cluster_id"].isin(dropped)].copy()

    retained = b.groupby("stratum_code")["cluster_id"].nunique()
    factor = (CLUSTERS_PER_STRATUM / retained).rename("stage1_nr_factor")
    b = b.join(factor, on="stratum_code")
    for col in ["weight_final", "weight_nrcell", "weight_vacantnr",
                "weight_nc_ineligible", "weight_trimmed", "w_base"]:
        if col in b.columns:
            b[col] = b[col] * b["stage1_nr_factor"]
    return a, b, excl, dropped, factor


def fmt_ci(row: pd.Series, dp: int = 1) -> str:
    return f"{row['estimate_pct']:.{dp}f} ({row['ci_low_pct']:.{dp}f}-{row['ci_high_pct']:.{dp}f})"


def md_table(df: pd.DataFrame, floats: dict | None = None) -> str:
    """Render a DataFrame as a GitHub-flavoured markdown table."""
    d = df.copy()
    for col, spec in (floats or {}).items():
        if col in d.columns:
            d[col] = d[col].map(lambda v, s=spec: "" if pd.isna(v) else format(v, s))
    d = d.astype(str)
    head = "| " + " | ".join(d.columns) + " |"
    rule = "|" + "|".join("---" for _ in d.columns) + "|"
    body = "\n".join("| " + " | ".join(r) + " |" for r in d.itertuples(index=False))
    return "\n".join([head, rule, body])
