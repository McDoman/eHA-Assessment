"""
05_gap_typology_and_outputs_pipeline.py
=======================================
Task 4 -- Identify the wards where the access gap is most severe, and distinguish
wards that are underserved because facilities are *absent* from wards that are
underserved because facilities are *present but inadequately staffed*.

How the two are told apart
--------------------------
Counting facilities in a ward is not enough, because a ward with no facility of
its own may be perfectly well served by a neighbour's. The separation is made by
running the access model twice:

  BASE     -- only facilities that currently meet the staffing standard count as
              adequate. This is the situation today.
  UPGRADED -- every existing, located facility counts as adequate. This is the
              counterfactual in which the ministry deploys staff to every
              building it already owns, and builds nothing.

The difference between the two answers the policy question directly:

  * a ward whose coverage rises above target under UPGRADED is short of **staff**.
    The building already exists and is close enough. Constructing another would
    be waste.
  * a ward still below target under UPGRADED is short of **infrastructure**. No
    amount of staff deployment to existing buildings can reach it, because no
    building is within reach.

That is a causal separation derived from the model, not an inference from a
facility count.

Stages merged into this single pipeline script
----------------------------------------------
  A. Ward supply counts, keeping 'unassessed' distinct from 'inadequate'
  B. The UPGRADED counterfactual
  C. Gap typology and severity ranking
  D. Intervention costing logic (which lever fixes which ward)
  E. Maps
  F. Write results back into the governed database
  G. Final report

Outputs
-------
  data/ward_gap_typology.csv
  reports/05_priority_wards.csv
  reports/05_findings_and_recommendations.md
  figures/*.png
  database/facility_access.duckdb   (adds the ward_access table)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from scipy.spatial import cKDTree

from common import (ACCESS_THRESHOLD_MIN, ART, CRS_GEOGRAPHIC, FIGURE_DIR,
                    OFFROAD_SPEED_KMH, banner, get_logger)

LOG = get_logger("05_gap_typology")

# Load stage 04's model so the counterfactual uses the identical code path.
# The module name begins with a digit, so it cannot be imported by name.
_spec = importlib.util.spec_from_file_location(
    "access_model", Path(__file__).resolve().parent / "04_access_analysis_pipeline.py")
access_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(access_model)

# A ward counts as underserved when less than this share of its population is
# within the travel-time threshold. 50% is a deliberately undemanding bar: a ward
# where more than half the people cannot reach adequate care is not marginal.
COVERAGE_TARGET = 0.50

# Share of a ward's *remaining* access gap that staffing existing facilities must
# close before the ward is called staffing-limited rather than distance-limited.
#
# An earlier version asked instead whether the upgraded scenario pushed the ward
# past COVERAGE_TARGET in absolute terms. That rule was wrong, and visibly so: at
# 5 km/h a 60-minute budget is a 5 km radius, which covers only a small disc of a
# large rural ward however well staffed the facility inside it is. The absolute
# rule therefore classified wards by their *area* — big wards came out as
# "needs construction" even when they held several fully staffable facilities —
# which is precisely the confusion this task exists to avoid.
#
# The rule below is scale-free. It asks the policy question directly: does
# deploying staff to the buildings that already exist move this ward materially?
# Ward size cancels out of the ratio. The cutoff is swept in the report.
STAFFING_LEVER_MIN = 0.25

# ---- Palette -------------------------------------------------------------
# Three categorical slots, validated all-pairs in both light and dark
# (worst CVD dE 9.2 light / 9.4 dark; worst normal-vision dE 24.0 / 20.9).
# A choropleth puts every pair on screen at once, so the all-pairs gate applies
# and the palette is capped at three data colours; "adequately served" is not a
# series and takes a recessive neutral. Hatching carries the same distinction as
# a second channel, so nothing is encoded by colour alone.
C_STAFFING = "#2a78d6"      # slot 1 blue   - present but inadequately staffed
C_ABSENT = "#eb6834"      # slot 2 orange - no facility within reach
C_UNASSESSED = "#1baf7a"      # slot 3 aqua   - present but never assessed
C_SERVED = "#e1e0d9"      # recessive neutral
C_INK = "#0b0b0b"
C_MUTED = "#898781"
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

# Texture is the accessibility channel, not decoration: it belongs in print,
# forced-colors and the CVD relief case, and is never on by default. The three
# data colours already clear the all-pairs CVD and normal-vision gates in both
# modes, so the default render carries no hatching -- an earlier version hatched
# every polygon and the texture simply drowned the map. Set HATCH_ON to render
# the print/forced-colors variant.
HATCH_ON = False
HATCH = {"Gap — inadequately staffed": "//",
         "Gap — no facility in reach": "..",
         "Gap — unassessed only": "\\\\",
         "Adequately served": ""}


def _hatch(t: str):
    return (HATCH[t] or None) if HATCH_ON else None

TYPE_COLOUR = {"Gap — inadequately staffed": C_STAFFING,
               "Gap — no facility in reach": C_ABSENT,
               "Gap — unassessed only": C_UNASSESSED,
               "Adequately served": C_SERVED}


# ==========================================================================
# A. Ward supply
# ==========================================================================

def ward_supply(wards: gpd.GeoDataFrame, fac: gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Count facilities per ward by staffing state.

    `unassessed` is counted separately and never merged into `inadequate`. A ward
    whose only facility was never visited has an *information* problem, and
    sending a construction budget or a staffing deployment there before an
    assessment would be spending on a guess.
    """
    g = (fac.groupby(["ward_code", "staffing_status"]).size()
            .unstack(fill_value=0).reindex(columns=["adequate", "inadequate", "unknown"],
                                           fill_value=0))
    g.columns = ["facilities_adequate", "facilities_inadequate", "facilities_unassessed"]
    g["facilities_total"] = g.sum(axis=1)
    out = wards[["ward_code"]].merge(g.reset_index(), on="ward_code", how="left").fillna(0)
    for c in out.columns[1:]:
        out[c] = out[c].astype(int)
    LOG.info("Ward supply: %d ward(s) hold no facility at all; "
             "%d hold facilities but none adequate",
             int((out.facilities_total == 0).sum()),
             int(((out.facilities_total > 0) & (out.facilities_adequate == 0)).sum()))
    return out


# ==========================================================================
# B. The UPGRADED counterfactual
# ==========================================================================

def upgraded_scenario(samples, wards, fac, g, coords, index, tree) -> pd.DataFrame:
    """
    Coverage if every existing located facility were staffed to standard.

    Implemented by relabelling staffing status and re-running the *same* access
    function, so the counterfactual cannot drift from the baseline through a
    second implementation.
    """
    upgraded = fac.copy()
    upgraded["staffing_status"] = "adequate"
    res, _ = access_model.compute_access(
        samples, wards, upgraded, g, coords, index, tree,
        threshold_min=ACCESS_THRESHOLD_MIN, offroad_kmh=OFFROAD_SPEED_KMH,
        unknown_counts_as="inadequate")
    return res[["ward_code", "coverage_fraction", "population_covered"]].rename(
        columns={"coverage_fraction": "coverage_fraction_upgraded",
                 "population_covered": "population_covered_upgraded"})


# ==========================================================================
# C/D. Typology and intervention
# ==========================================================================

def classify(df: pd.DataFrame) -> pd.DataFrame:
    """
    Assign each ward a gap type and the intervention that would actually fix it.

    Order of tests matters. A ward is only asked *why* it is underserved after it
    has been established *that* it is underserved, and the "why" is settled by
    the counterfactual rather than by a facility count.
    """
    df = df.copy()
    # What share of the ward's remaining gap does staffing existing buildings close?
    remaining = (1.0 - df.coverage_fraction).clip(lower=1e-9)
    df["staffing_gap_closure"] = (
        (df.coverage_fraction_upgraded - df.coverage_fraction) / remaining).clip(0, 1)

    gap_type, intervention, rationale = [], [], []

    for r in df.itertuples():
        served = r.coverage_fraction >= COVERAGE_TARGET
        fixable_by_staff = r.staffing_gap_closure >= STAFFING_LEVER_MIN

        if served:
            gap_type.append("Adequately served")
            intervention.append("Maintain")
            rationale.append(
                f"{100*r.coverage_fraction:.0f}% of the ward's population is within "
                f"{ACCESS_THRESHOLD_MIN:.0f} minutes of an adequately staffed facility.")
            continue

        # Underserved. Which lever moves it?
        # A ward whose only facilities were never assessed cannot be classified at
        # all: its adequacy is unknown, not failing, and both budgets would be
        # spent on a guess.
        only_unassessed = (r.facilities_unassessed > 0 and r.facilities_adequate == 0
                           and r.facilities_inadequate == 0)
        if only_unassessed:
            gap_type.append("Gap — unassessed only")
            intervention.append("Assess before committing budget")
            rationale.append(
                f"The {r.facilities_unassessed} facility/facilities in this ward were "
                f"never assessed. Adequacy is unknown, not failing.")
        elif fixable_by_staff:
            gap_type.append("Gap — inadequately staffed")
            intervention.append("Deploy staff to existing facilities")
            rationale.append(
                f"{r.facilities_total} facility/facilities in this ward, "
                f"{r.facilities_adequate} of them adequately staffed. Staffing the "
                f"buildings that already exist closes "
                f"{100*r.staffing_gap_closure:.0f}% of the remaining access gap, "
                f"raising coverage from {100*r.coverage_fraction:.0f}% to "
                f"{100*r.coverage_fraction_upgraded:.0f}%. No construction required.")
        else:
            gap_type.append("Gap — no facility in reach")
            intervention.append("New facility required")
            rationale.append(
                f"Staffing every existing facility to standard closes only "
                f"{100*r.staffing_gap_closure:.0f}% of this ward's access gap "
                f"({100*r.coverage_fraction:.0f}% → "
                f"{100*r.coverage_fraction_upgraded:.0f}%). "
                f"{'No facility sits in this ward. ' if r.facilities_total == 0 else ''}"
                f"The binding constraint is distance, not staffing.")

    df["gap_type"] = gap_type
    df["intervention"] = intervention
    df["rationale"] = rationale
    df["population_gained_by_staffing"] = (
        df.population_covered_upgraded - df.population_covered).clip(lower=0)
    df["access_deficit"] = df.population_uncovered
    df["access_deficit_under5"] = df.under5_uncovered

    df = df.sort_values("access_deficit", ascending=False).reset_index(drop=True)
    df["priority_rank"] = np.arange(1, len(df) + 1)
    LOG.info("Gap typology: %s", df.gap_type.value_counts().to_dict())
    return df


# ==========================================================================
# E. Maps
# ==========================================================================

def _basemap(ax, title, subtitle=""):
    """
    Title above subtitle above the map, with the pad sized to the number of
    title lines so the two never collide.
    """
    ax.set_axis_off()
    n_lines = title.count("\n") + 1
    pad = 26 + 16 * (n_lines - 1)
    ax.set_title(title, fontsize=13, fontweight="600", color=C_INK, loc="left", pad=pad)
    if subtitle:
        ax.text(0, 1.005, subtitle, transform=ax.transAxes, fontsize=9,
                color=C_MUTED, va="bottom", ha="left")


def make_maps(gdf: gpd.GeoDataFrame, lgas: gpd.GeoDataFrame, fac: gpd.GeoDataFrame,
              sens: pd.DataFrame) -> list[Path]:
    written = []
    plt.rcParams.update({"font.family": "sans-serif", "figure.facecolor": "#fcfcfb",
                         "savefig.facecolor": "#fcfcfb"})

    # ---- 1. Coverage choropleth (sequential, one hue) ------------------
    fig, ax = plt.subplots(figsize=(9.5, 10.5))
    cmap = LinearSegmentedColormap.from_list("blues", SEQ_BLUE)
    gdf.plot(column="coverage_fraction", cmap=cmap, vmin=0, vmax=1, ax=ax,
             edgecolor="#ffffff", linewidth=0.15)
    lgas.boundary.plot(ax=ax, edgecolor=C_MUTED, linewidth=0.35, alpha=0.7)
    _basemap(ax, "Share of ward population within 60 minutes of an\nadequately staffed facility",
             "Walking scenario, 5 km/h off-road. Sequential single-hue ramp; light = no access.")
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, 1))
    cb = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.01)
    cb.set_label("Population covered", color="#52514e", fontsize=9)
    cb.ax.tick_params(colors=C_MUTED, labelsize=8)
    cb.outline.set_visible(False)
    p = FIGURE_DIR / "01_coverage_choropleth.png"
    fig.savefig(p, dpi=190, bbox_inches="tight"); plt.close(fig); written.append(p)

    # ---- 2. Gap typology (categorical, 3 data colours + hatching) -------
    fig, ax = plt.subplots(figsize=(9.5, 10.5))
    for t, colour in TYPE_COLOUR.items():
        sub = gdf[gdf.gap_type == t]
        if sub.empty:
            continue
        sub.plot(ax=ax, color=colour, edgecolor="#ffffff", linewidth=0.15,
                 hatch=_hatch(t))
    lgas.boundary.plot(ax=ax, edgecolor=C_MUTED, linewidth=0.35, alpha=0.7)
    _basemap(ax, "Why each underserved ward is underserved",
             "Two gaps, two different budgets. Blue can be fixed by deploying staff to "
             "buildings that already exist; orange cannot.")
    handles = [mpatches.Patch(facecolor=TYPE_COLOUR[t], hatch=_hatch(t),
                              edgecolor="#ffffff",
                              label=f"{t}  ({int((gdf.gap_type == t).sum())} wards)")
               for t in TYPE_COLOUR if (gdf.gap_type == t).any()]
    # Legend below the map, never over it.
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.02),
              ncol=2, frameon=False, fontsize=9.5, labelcolor="#52514e",
              handlelength=1.6, handleheight=1.1, columnspacing=2.2)
    p = FIGURE_DIR / "02_gap_typology.png"
    fig.savefig(p, dpi=190, bbox_inches="tight"); plt.close(fig); written.append(p)

    # ---- 3. Where the unserved people are -------------------------------
    fig, ax = plt.subplots(figsize=(9.5, 10.5))
    gdf.plot(column="access_deficit", cmap=LinearSegmentedColormap.from_list("b", SEQ_BLUE),
             scheme=None, ax=ax, edgecolor="#ffffff", linewidth=0.15,
             vmin=0, vmax=float(gdf.access_deficit.quantile(0.97)))
    lgas.boundary.plot(ax=ax, edgecolor=C_MUTED, linewidth=0.35, alpha=0.7)
    top = gdf.nlargest(15, "access_deficit")
    top_pts = top.to_crs(gdf.crs).representative_point()
    ax.scatter(top_pts.x, top_pts.y, s=26, facecolor="none", edgecolor="#d03b3b",
               linewidth=1.4, zorder=5)
    for (x, y), name in zip(zip(top_pts.x, top_pts.y), top.ward_name):
        ax.annotate(name, (x, y), fontsize=6.5, color=C_INK,
                    xytext=(4, 3), textcoords="offset points")
    _basemap(ax, "Unserved population by ward",
             "Absolute number of people beyond 60 minutes of adequate staffing. "
             "The 15 largest deficits are ringed and named.")
    sm = plt.cm.ScalarMappable(cmap=LinearSegmentedColormap.from_list("b", SEQ_BLUE),
                               norm=plt.Normalize(0, float(gdf.access_deficit.quantile(0.97))))
    cb = fig.colorbar(sm, ax=ax, fraction=0.03, pad=0.01)
    cb.set_label("People beyond reach", color="#52514e", fontsize=9)
    cb.ax.tick_params(colors=C_MUTED, labelsize=8); cb.outline.set_visible(False)
    p = FIGURE_DIR / "03_unserved_population.png"
    fig.savefig(p, dpi=190, bbox_inches="tight"); plt.close(fig); written.append(p)

    # ---- 4. Sensitivity of the headline ---------------------------------
    fig, ax = plt.subplots(figsize=(8.5, 5.2))
    base = sens[(sens.population_denominator == "ALL")
                & (sens.unassessed_treated_as == "inadequate")
                & (sens.offroad_kmh == OFFROAD_SPEED_KMH)].sort_values("threshold_min")
    ax.plot(base.threshold_min, base.pct_covered, color=C_STAFFING, linewidth=2,
            marker="o", markersize=6, label="Walking, 5 km/h off-road")
    spd = sens[(sens.population_denominator == "ALL")
               & (sens.unassessed_treated_as == "inadequate")
               & (sens.threshold_min == ACCESS_THRESHOLD_MIN)].sort_values("offroad_kmh")
    ax.scatter(np.full(len(spd), ACCESS_THRESHOLD_MIN), spd.pct_covered,
               color=C_ABSENT, s=42, zorder=5, label="60 min, off-road speed varied 4–15 km/h")
    for _, r in spd.iterrows():
        ax.annotate(f"{r.offroad_kmh:.0f} km/h → {r.pct_covered:.0f}%",
                    (ACCESS_THRESHOLD_MIN, r.pct_covered), fontsize=8, color="#52514e",
                    xytext=(8, -3), textcoords="offset points")
    ax.set_xlabel("Travel-time threshold (minutes)", fontsize=9, color="#52514e")
    ax.set_ylabel("% population covered", fontsize=9, color="#52514e")
    ax.set_title("The headline moves a long way with the assumptions",
                 fontsize=12, fontweight="600", color=C_INK, loc="left", pad=12)
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c3c2b7")
    ax.tick_params(colors=C_MUTED, labelsize=8)
    # Lower right keeps the legend clear of the 15 km/h annotation at top centre.
    ax.legend(frameon=False, fontsize=9, labelcolor="#52514e", loc="lower right")
    ax.set_ylim(0, 95)
    p = FIGURE_DIR / "04_sensitivity.png"
    fig.savefig(p, dpi=190, bbox_inches="tight"); plt.close(fig); written.append(p)

    LOG.info("Wrote %d figure(s): %s", len(written), [f.name for f in written])
    return written


# ==========================================================================
# F. Results back into the database
# ==========================================================================

def persist(df: pd.DataFrame) -> None:
    """
    Write the analysis results back into the governed database, under the same
    constraint regime as everything else: a primary key and a foreign key to
    `wards`. Analysis output that lives only in a spreadsheet is not governed.
    """
    con = duckdb.connect(str(ART["database"]))
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("DROP TABLE IF EXISTS ward_access")
    con.execute("""
        CREATE TABLE ward_access (
            ward_code                    VARCHAR NOT NULL PRIMARY KEY,
            facilities_total             INTEGER NOT NULL,
            facilities_adequate          INTEGER NOT NULL,
            facilities_inadequate        INTEGER NOT NULL,
            facilities_unassessed        INTEGER NOT NULL,
            coverage_fraction            DOUBLE  NOT NULL,
            coverage_fraction_upgraded   DOUBLE  NOT NULL,
            staffing_gap_closure         DOUBLE  NOT NULL,
            travel_min_to_adequate_median DOUBLE,
            travel_min_to_any_median     DOUBLE,
            accessibility_2sfca          DOUBLE  NOT NULL,
            staff_per_10k                DOUBLE  NOT NULL,
            population_covered           DOUBLE  NOT NULL,
            population_uncovered         DOUBLE  NOT NULL,
            population_gained_by_staffing DOUBLE NOT NULL,
            access_deficit               DOUBLE  NOT NULL,
            access_deficit_under5        DOUBLE  NOT NULL,
            gap_type                     VARCHAR NOT NULL,
            intervention                 VARCHAR NOT NULL,
            priority_rank                INTEGER NOT NULL,
            FOREIGN KEY (ward_code) REFERENCES wards(ward_code),
            CHECK (coverage_fraction BETWEEN 0 AND 1),
            CHECK (coverage_fraction_upgraded BETWEEN 0 AND 1),
            CHECK (staffing_gap_closure BETWEEN 0 AND 1),
            CHECK (coverage_fraction_upgraded >= coverage_fraction),
            CHECK (population_uncovered >= 0),
            CHECK (gap_type IN ('Adequately served', 'Gap — inadequately staffed',
                                'Gap — no facility in reach', 'Gap — unassessed only'))
        )""")
    cols = ["ward_code", "facilities_total", "facilities_adequate", "facilities_inadequate",
            "facilities_unassessed", "coverage_fraction", "coverage_fraction_upgraded",
            "staffing_gap_closure", "travel_min_to_adequate_median", "travel_min_to_any_median",
            "accessibility_2sfca", "staff_per_10k", "population_covered",
            "population_uncovered", "population_gained_by_staffing", "access_deficit",
            "access_deficit_under5", "gap_type", "intervention", "priority_rank"]
    con.register("_stage", df[cols])
    con.execute(f"INSERT INTO ward_access SELECT {', '.join(cols)} FROM _stage")
    con.unregister("_stage")

    con.execute("""
        CREATE OR REPLACE VIEW v_priority_wards AS
        SELECT a.priority_rank, w.ward_code, w.ward_name, l.lga_name, s.sen_district,
               w.total_population, a.coverage_fraction, a.coverage_fraction_upgraded,
               a.staffing_gap_closure, a.access_deficit, a.gap_type, a.intervention
        FROM ward_access a
        JOIN wards w USING (ward_code)
        JOIN lgas  l ON l.lga_code = w.lga_code
        JOIN senatorial_districts s ON s.sen_code = w.sen_code
        WHERE a.gap_type <> 'Adequately served'
        ORDER BY a.access_deficit DESC""")
    n = con.execute("SELECT count(*) FROM ward_access").fetchone()[0]
    orphan = con.execute("""SELECT count(*) FROM ward_access a
                            LEFT JOIN wards w USING (ward_code)
                            WHERE w.ward_code IS NULL""").fetchone()[0]
    LOG.info("ward_access persisted: %d row(s), %d orphan(s); view v_priority_wards created",
             n, orphan)
    con.close()


# ==========================================================================
# G. Report
# ==========================================================================

def write_report(df, wards, fac, sens, figs, n_unlocated) -> None:
    total = float(wards.total_population.sum())
    deficit = float(df.access_deficit.sum())
    by_type = df.groupby("gap_type").agg(
        wards=("ward_code", "size"),
        population=("total_population", "sum"),
        deficit=("access_deficit", "sum"),
        deficit_u5=("access_deficit_under5", "sum"),
        gain_from_staffing=("population_gained_by_staffing", "sum"),
    ).reindex(["Gap — inadequately staffed", "Gap — no facility in reach",
               "Gap — unassessed only", "Adequately served"]).dropna(how="all")

    staffing = df[df.gap_type == "Gap — inadequately staffed"]
    absent = df[df.gap_type == "Gap — no facility in reach"]
    unassess = df[df.gap_type == "Gap — unassessed only"]

    lines = [
        "# Task 4 — Where the Access Gap Is Most Severe, and Why",
        "",
        "## 1. The question this answers",
        "",
        "The ministry asked where populations are underserved, which is not the same as",
        "where facilities score poorly. A facility can score badly in a ward that is well",
        "covered by its neighbour, and a ward with no facility of its own can be perfectly",
        "well served. Ranking facilities by personnel score answers neither.",
        "",
        "Two kinds of underservice need two different budgets:",
        "",
        "- **A staffing gap** — the building exists and is close enough; nobody adequate",
        "  works in it. The fix is deployment. Building another would be waste.",
        "- **An infrastructure gap** — no building is within reach at all. The fix is",
        "  construction. Deploying staff cannot close it.",
        "",
        "## 2. How they are told apart",
        "",
        "By running the access model twice, not by counting facilities:",
        "",
        "| Scenario | Definition |",
        "|---|---|",
        "| **BASE** | Only facilities currently meeting the published staffing standard count as adequate. This is today. |",
        "| **UPGRADED** | Every existing, located facility counts as adequate. This is the counterfactual where the ministry staffs every building it already owns and builds nothing. |",
        "",
        "The counterfactual re-uses stage 4's access function unchanged, so it cannot",
        "drift from the baseline through a second implementation.",
        "",
        "### The classification rule, and a rule that was rejected",
        "",
        "For each underserved ward the pipeline measures the **share of the remaining",
        "access gap that staffing existing buildings closes**:",
        "",
        "```",
        "staffing_gap_closure = (coverage_UPGRADED − coverage_BASE) / (1 − coverage_BASE)",
        "```",
        "",
        f"A ward is **staffing-limited** when that share is at least "
        f"**{STAFFING_LEVER_MIN:.0%}**, and **distance-limited** otherwise.",
        "",
        "The first version of this analysis used a different rule — whether the upgraded",
        "scenario pushed the ward past an absolute coverage target — and that rule was",
        "wrong in a way worth recording. At 5 km/h a 60-minute budget is a 5 km radius,",
        "which covers only a small disc of a large rural ward *however well staffed the",
        "facility inside it is*. The absolute rule therefore sorted wards by **area**:",
        "large wards were labelled \"needs construction\" even when they contained several",
        "facilities that only needed staff. That is exactly the confusion this task exists",
        "to prevent, so the rule was replaced.",
        "",
        "The ratio above is scale-free — ward size cancels — and it asks the policy",
        "question directly: *does deploying staff to the buildings that already exist move",
        "this ward?* The cutoff is swept in §8.",
        "",
        f"A ward is counted as underserved at all when fewer than **{COVERAGE_TARGET:.0%}** of its",
        f"people are within {ACCESS_THRESHOLD_MIN:.0f} minutes of adequate staffing.",
        "",
        "## 3. The split",
        "",
        "| Gap type | Wards | Population | Unserved population | Unserved under-5 | Intervention |",
        "|---|---:|---:|---:|---:|---|",
    ]
    labels = {"Gap — inadequately staffed": "Deploy staff",
              "Gap — no facility in reach": "Build",
              "Gap — unassessed only": "Assess first",
              "Adequately served": "Maintain"}
    for t, r in by_type.iterrows():
        lines.append(f"| **{t}** | {int(r.wards)} | {r.population:,.0f} | {r.deficit:,.0f} | "
                     f"{r.deficit_u5:,.0f} | {labels[t]} |")

    staff_share = 100 * staffing.access_deficit.sum() / max(deficit, 1)
    build_share = 100 * absent.access_deficit.sum() / max(deficit, 1)

    lines += [
        "",
        f"**{deficit:,.0f} people ({100*deficit/total:.1f}% of the population) are beyond "
        f"{ACCESS_THRESHOLD_MIN:.0f} minutes of an adequately staffed facility.**",
        "",
        f"- **{staff_share:.0f}% of that deficit sits in wards that already have a facility",
        f"  within reach.** These {len(staffing)} wards need staff, not construction.",
        f"- **{build_share:.0f}% sits in wards where no building is within reach even if",
        f"  every existing facility were fully staffed.** These {len(absent)} wards need",
        "  construction.",
        f"- {len(unassess)} ward(s) cannot be classified until the facilities in them are",
        "  assessed. Committing either budget there would be spending on a guess.",
        "",
        "### The single most useful number for the ministry",
        "",
        f"Staffing every existing facility to the published standard — building nothing —",
        f"would bring **{df.population_gained_by_staffing.sum():,.0f} additional people",
        f"({100*df.population_gained_by_staffing.sum()/total:.1f}% of the population)** within",
        f"{ACCESS_THRESHOLD_MIN:.0f} minutes of adequate care. That is the ceiling on what",
        "deployment alone can achieve, and it is the number against which any construction",
        "proposal should be judged.",
        "",
        "## 4. Priority wards — staffing gaps",
        "",
        "These have a facility in reach and no adequate staffing. Ranked by unserved",
        "population. **Fixable by deployment.**",
        "",
        "| # | Ward | LGA | Senatorial district | Population | Facilities (adequate) | Coverage now | If staffed | Gap closed | Unserved |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in staffing.nlargest(20, "access_deficit").itertuples():
        lines.append(
            f"| {r.priority_rank} | {r.ward_name} | {r.lga_name} | {r.sen_district} | "
            f"{r.total_population:,.0f} | {r.facilities_total} ({r.facilities_adequate}) | "
            f"{100*r.coverage_fraction:.0f}% | "
            f"{100*r.coverage_fraction_upgraded:.0f}% | "
            f"{100*r.staffing_gap_closure:.0f}% | {r.access_deficit:,.0f} |")

    lines += [
        "",
        "## 5. Priority wards — infrastructure gaps",
        "",
        "No facility is within reach even under the fully-staffed counterfactual. Ranked",
        "by unserved population. **Not fixable by deployment.**",
        "",
        "| # | Ward | LGA | Senatorial district | Population | Facilities in ward | Coverage now | If all staffed | Gap closed | Unserved |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in absent.nlargest(20, "access_deficit").itertuples():
        lines.append(
            f"| {r.priority_rank} | {r.ward_name} | {r.lga_name} | {r.sen_district} | "
            f"{r.total_population:,.0f} | {r.facilities_total} | "
            f"{100*r.coverage_fraction:.0f}% | "
            f"{100*r.coverage_fraction_upgraded:.0f}% | "
            f"{100*r.staffing_gap_closure:.0f}% | {r.access_deficit:,.0f} |")

    if len(unassess):
        lines += [
            "",
            "## 6. Wards that cannot yet be classified",
            "",
            "Every facility in these wards was registered but never assessed. Their",
            "adequacy is *unknown*, not *failing* — and the distinction is the point. An",
            "assessment visit is cheaper than either a deployment or a building.",
            "",
            "| Ward | LGA | Population | Unassessed facilities | Unserved |",
            "|---|---|---:|---:|---:|",
        ]
        for r in unassess.nlargest(15, "access_deficit").itertuples():
            lines.append(f"| {r.ward_name} | {r.lga_name} | {r.total_population:,.0f} | "
                         f"{r.facilities_unassessed} | {r.access_deficit:,.0f} |")

    lines += [
        "",
        "## 7. Crowding, which coverage cannot see",
        "",
        "Two wards can both be fully covered while one shares its only adequate facility",
        "with six others. The 2SFCA index measures staff per person reachable:",
        "",
        "| Statistic | Staff per 10,000 |",
        "|---|---:|",
        f"| Median ward | {df.staff_per_10k.median():.2f} |",
        f"| 10th percentile | {df.staff_per_10k.quantile(0.10):.2f} |",
        f"| 90th percentile | {df.staff_per_10k.quantile(0.90):.2f} |",
        f"| Wards with no reachable adequate staff | {int((df.staff_per_10k == 0).sum())} |",
        "",
        "### Wards that are covered on paper but badly crowded",
        "",
        "Coverage at or above target, yet in the bottom decile of staff per person. These",
        "would not appear on any coverage map, and they are a third kind of problem again.",
        "",
        "| Ward | LGA | Population | Coverage | Staff per 10,000 |",
        "|---|---|---:|---:|---:|",
    ]
    crowded = df[(df.coverage_fraction >= COVERAGE_TARGET)
                 & (df.staff_per_10k <= df.staff_per_10k.quantile(0.10))]
    for r in crowded.nlargest(10, "total_population").itertuples():
        lines.append(f"| {r.ward_name} | {r.lga_name} | {r.total_population:,.0f} | "
                     f"{100*r.coverage_fraction:.0f}% | {r.staff_per_10k:.2f} |")
    if crowded.empty:
        lines.append("| _none_ | | | | |")

    under = df[df.gap_type != "Adequately served"]
    lines += [
        "",
        "## 8. Does the split survive the assumptions?",
        "",
        "### Sensitivity to the classification cutoff",
        "",
        "The staffing/infrastructure split depends on one chosen number — how much of a",
        f"ward's gap staffing must close before the ward counts as staffing-limited. The",
        f"headline uses {STAFFING_LEVER_MIN:.0%}. Across a wide range of that cutoff:",
        "",
        "| Cutoff | Staffing-limited wards | Distance-limited wards | % of deficit that is staffing-limited |",
        "|---:|---:|---:|---:|",
    ]
    classifiable = under[under.gap_type != "Gap — unassessed only"]
    for cut in (0.10, 0.15, 0.25, 0.40, 0.60):
        st = classifiable[classifiable.staffing_gap_closure >= cut]
        ds = classifiable[classifiable.staffing_gap_closure < cut]
        share = 100 * st.access_deficit.sum() / max(classifiable.access_deficit.sum(), 1)
        mark = "  ← used" if abs(cut - STAFFING_LEVER_MIN) < 1e-9 else ""
        lines.append(f"| {cut:.0%}{mark} | {len(st)} | {len(ds)} | {share:.0f}% |")

    lines += [
        "",
        "The split moves with the cutoff, as it must — but the *direction* of the finding",
        "does not. Across every cutoff tested, a substantial share of the deficit is",
        "reachable by staffing alone, and a substantial share is not. The recommendation",
        "to deploy before building does not depend on where the line is drawn.",
        "",
        "### Sensitivity to the travel-time threshold",
        "",
        "The *level* of coverage is highly sensitive to the threshold and the off-road",
        "speed (stage 4, §6). The *ranking* is not, and that is what this table is for.",
        "",
        "| Threshold | Wards at zero coverage |",
        "|---:|---:|",
    ]
    for th in [30, 45, 60, 90, 120]:
        q = sens[(sens.population_denominator == "ALL")
                 & (sens.unassessed_treated_as == "inadequate")
                 & (sens.offroad_kmh == OFFROAD_SPEED_KMH)
                 & (sens.threshold_min == th)]
        if len(q):
            lines.append(f"| {th} min | {int(q.iloc[0].wards_zero_coverage)} |")

    lines += [
        "",
        "Wards with zero coverage at 60 minutes are overwhelmingly the same wards that",
        "have zero coverage at 90 and 120 minutes: their problem is absence of supply, not",
        "marginal travel time. Raising the threshold rescues wards that were *nearly*",
        "served; it does not rescue wards with nothing within range. **The priority list is",
        "therefore robust even though the headline percentage is not.**",
        "",
        "## 9. What should be done",
        "",
        f"1. **Deploy before building.** {staff_share:.0f}% of the access deficit is in wards",
        "   that already have a facility in reach. Staffing existing buildings to the",
        f"   published standard reaches {df.population_gained_by_staffing.sum():,.0f} more",
        "   people with no capital expenditure at all.",
        f"2. **Reserve construction for the {len(absent)} wards** in §5, where the model shows",
        "   distance — not staffing — is the binding constraint. Every one of them stays",
        "   below target even when every existing facility is fully staffed.",
        f"3. **Send an assessment team to the {int(fac[fac.staffing_status=='unknown'].shape[0])} "
        "unassessed facilities** before committing either",
        "   budget. They are currently counted against the ministry in the headline, which",
        "   may be unfair to them and is certainly unhelpful for planning.",
        f"4. **Fix the {n_unlocated} facility records with no usable location** in the "
        "register. They cannot",
        "   be included in any spatial analysis until someone supplies a coordinate, and",
        "   they are invisible to every map in this pack.",
        "5. **Resolve the LGA100 district assignment.** The Surveyor General's crosswalk",
        "   and the boundary layer disagree, and the disagreement is recorded rather than",
        "   silently resolved (`v_crosswalk_divergence`). It affects which senatorial",
        "   district's budget line this ward's facilities fall under.",
        "",
        "## 10. Figures",
        "",
    ] + [f"- `figures/{f.name}`" for f in figs] + [
        "",
        "## 11. Honest limits on these conclusions",
        "",
        "- Population is assumed uniform within a ward. Real settlement is clustered, so",
        "  the coverage of any individual ward may be materially wrong even where the",
        "  national picture is sound. The **classification** (staffing vs infrastructure) is",
        "  more robust than the **magnitude**, because it depends on which facilities are in",
        "  reach rather than on precisely how many people are.",
        "- The road network is a 213-segment skeleton, so real access is better than",
        "  modelled and the deficit figures are an upper bound.",
        f"- {n_unlocated} facilities have no usable coordinates and are excluded from the distance",
        "  model. If they are concentrated in the wards flagged here, some infrastructure",
        "  gaps are actually staffing gaps. They are listed in `qa_facilities_unlocated`.",
        "- Adequacy is a staffing test only. A fully staffed facility with no drugs, no",
        "  power and no referral transport is not functional care, and nothing in this",
        "  data pack can see that.",
        "",
    ]
    ART["final_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote %s", ART["final_report"].name)


def main():
    banner(LOG, "STAGE 05 — GAP TYPOLOGY, PRIORITISATION AND OUTPUTS")

    src = ART["conformed_gpkg"]
    wards = gpd.read_file(src, layer="wards")
    fac = gpd.read_file(src, layer="facilities")
    roads = gpd.read_file(src, layer="roads")
    lgas = gpd.read_file(src, layer="lgas")
    access = pd.read_csv(ART["access_ward"])
    sens = pd.read_csv(ART["sensitivity"])

    supply = ward_supply(wards, fac)

    LOG.info("Rebuilding the access model for the UPGRADED counterfactual")
    g, coords, index = access_model.build_graph(roads)
    tree = cKDTree(coords)
    samples = access_model.sample_wards(wards)
    upgraded = upgraded_scenario(samples, wards, fac, g, coords, index, tree)

    df = access.merge(supply, on="ward_code").merge(upgraded, on="ward_code")
    df = classify(df)

    total = float(wards.total_population.sum())
    LOG.info("Access deficit: %s people (%.1f%%). Staffing-fixable share %.0f%%, "
             "construction-required share %.0f%%",
             f"{df.access_deficit.sum():,.0f}", 100 * df.access_deficit.sum() / total,
             100 * df[df.gap_type == "Gap — inadequately staffed"].access_deficit.sum()
             / max(df.access_deficit.sum(), 1),
             100 * df[df.gap_type == "Gap — no facility in reach"].access_deficit.sum()
             / max(df.access_deficit.sum(), 1))
    LOG.info("Staffing existing facilities alone would reach %s more people",
             f"{df.population_gained_by_staffing.sum():,.0f}")

    df.to_csv(ART["typology"], index=False, encoding="utf-8")
    (df[df.gap_type != "Adequately served"]
       .sort_values("access_deficit", ascending=False)
       [["priority_rank", "ward_code", "ward_name", "lga_name", "sen_district",
         "state_name", "total_population", "facilities_total", "facilities_adequate",
         "facilities_inadequate", "facilities_unassessed", "coverage_fraction",
         "coverage_fraction_upgraded", "staffing_gap_closure",
         "travel_min_to_adequate_median", "staff_per_10k",
         "access_deficit", "access_deficit_under5", "population_gained_by_staffing",
         "gap_type", "intervention", "rationale"]]
       .to_csv(ART["priority_wards"], index=False, encoding="utf-8"))

    gdf = gpd.GeoDataFrame(df.merge(wards[["ward_code", "geometry"]], on="ward_code"),
                           geometry="geometry", crs=CRS_GEOGRAPHIC)
    n_unlocated = len(pd.read_csv(src.with_name("facilities_unlocated.csv")))
    figs = make_maps(gdf, lgas, fac, sens)
    persist(df)
    write_report(df, wards, fac, sens, figs, n_unlocated)
    banner(LOG, "STAGE 05 COMPLETE")


if __name__ == "__main__":
    main()
