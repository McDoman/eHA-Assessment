"""
common.py
=========
Shared configuration, logging, and name-reconciliation utilities for the
Part 1 / Question 2 facility-access pipeline.

Every pipeline stage imports from here so that paths, the coordinate reference
systems, the analytical parameters, and above all the *name normalisation rule*
are declared in exactly one place. Name normalisation is the single most
consequential piece of logic in this assessment -- the facility register, the
scoring file, the boundary layer and the Surveyor General's crosswalk each spell
the same administrative units differently -- so it is defined once, applied
everywhere, and every decision it makes is written to a ledger.

Author: technical assessment submission
"""

from __future__ import annotations

import json
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

OUTPUTS_DIR = Path(__file__).resolve().parent
SOURCE_DIR = OUTPUTS_DIR.parent

DATA_DIR = OUTPUTS_DIR / "data"          # conformed intermediate + final layers
DB_DIR = OUTPUTS_DIR / "database"        # the spatial database and its DDL
REPORT_DIR = OUTPUTS_DIR / "reports"     # ledgers, tables, written analysis
FIGURE_DIR = OUTPUTS_DIR / "figures"     # maps and charts
LOG_DIR = OUTPUTS_DIR / "logs"           # per-run execution logs

for _d in (DATA_DIR, DB_DIR, REPORT_DIR, FIGURE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Source files, as received. These are opened read-only. Nothing in this
# pipeline writes back into the source directory -- the crosswalk workbook in
# particular is never modified, which is the explicit requirement of task 1.
SRC = {
    "facilities": SOURCE_DIR / "health_facilities.csv",
    "scores_mif": SOURCE_DIR / "facility_personnel_scores.mif",
    "norms": SOURCE_DIR / "minimum_staffing_norms.csv",
    "ward_pop": SOURCE_DIR / "ward_population.csv",
    "boundaries": SOURCE_DIR / "admin_boundaries.gpkg",
    "roads": SOURCE_DIR / "road_network.geojson",
    "crosswalk": SOURCE_DIR / "LGA_SEN_Districts.xlsx",
}

# Canonical intermediate artefacts passed between stages.
ART = {
    "crosswalk_table": DATA_DIR / "lga_senatorial_crosswalk.csv",
    "crosswalk_ledger": REPORT_DIR / "01_crosswalk_reconciliation_ledger.csv",
    "crosswalk_exceptions": REPORT_DIR / "01_crosswalk_exceptions.csv",
    "crosswalk_report": REPORT_DIR / "01_crosswalk_normalisation_report.md",
    "conformed_gpkg": DATA_DIR / "conformed_layers.gpkg",
    "coord_ledger": REPORT_DIR / "02_coordinate_repair_ledger.csv",
    "join_ledger": REPORT_DIR / "02_facility_join_ledger.csv",
    "ingest_report": REPORT_DIR / "02_ingest_and_conform_report.md",
    "database": DB_DIR / "facility_access.duckdb",
    "postgis_ddl": DB_DIR / "postgis_schema.sql",
    "db_validation": REPORT_DIR / "03_database_validation.md",
    "access_ward": DATA_DIR / "ward_access_metrics.csv",
    "access_gpkg": DATA_DIR / "ward_access.gpkg",
    "sensitivity": REPORT_DIR / "04_sensitivity_analysis.csv",
    "access_report": REPORT_DIR / "04_access_method_and_results.md",
    "typology": DATA_DIR / "ward_gap_typology.csv",
    "priority_wards": REPORT_DIR / "05_priority_wards.csv",
    "final_report": REPORT_DIR / "05_findings_and_recommendations.md",
}

# --------------------------------------------------------------------------
# Coordinate reference systems
# --------------------------------------------------------------------------
# Everything arrives in geographic WGS 84. All *measurement* (distance, area,
# buffering, network length) is done in a projected CRS, never in degrees.
#
# The study area spans roughly 5.5E-11.5E, 6.8N-11.7N. That straddles the
# UTM 31N/32N zone boundary at 6E, so no single UTM zone is honest for the
# whole country. Africa Albers Equal Area (ESRI:102022) is the standard
# continental equal-area choice and keeps distance distortion under ~1% over
# an area this size, so it is used as the analysis CRS.
CRS_GEOGRAPHIC = "EPSG:4326"
CRS_PROJECTED = (
    "+proj=aea +lat_0=0 +lon_0=25 +lat_1=20 +lat_2=-23 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs"          # ESRI:102022, Africa Albers Equal Area Conic
)
CRS_PROJECTED_LABEL = "ESRI:102022 (Africa Albers Equal Area Conic)"

# Plausible-extent gate for the study area, used to quarantine coordinates that
# survive parsing but fall outside the country. Padded by ~0.5 degrees.
COUNTRY_BBOX = (5.0, 6.3, 12.0, 12.2)      # min_lon, min_lat, max_lon, max_lat

# --------------------------------------------------------------------------
# Analytical parameters (all thresholds declared here, none buried in code)
# --------------------------------------------------------------------------

# Headline travel-time threshold for access to an adequately staffed facility.
# 60 minutes is the conventional primary-care access standard.
ACCESS_THRESHOLD_MIN = 60.0

# Thresholds swept in the sensitivity analysis.
SENSITIVITY_THRESHOLDS_MIN = [30.0, 45.0, 60.0, 90.0, 120.0]

# Off-network travel speed. Most of the population reaching a health facility in
# a rural setting does the first and last leg on foot or by informal transport
# over unmapped tracks. 5 km/h is the walking speed used by AccessMod and by the
# WHO/UNICEF geographic-accessibility guidance.
OFFROAD_SPEED_KMH = 5.0

# Sensitivity variants for the off-road speed assumption.
SENSITIVITY_OFFROAD_KMH = [4.0, 5.0, 8.0, 15.0]

# Maximum distance a ward centroid or facility may be from the road network and
# still be snapped onto it. Beyond this the trip is modelled as entirely
# off-network. Generous, because the supplied network is a simplified skeleton.
MAX_SNAP_KM = 40.0

FUZZY_MATCH_THRESHOLD = 0.88     # SequenceMatcher ratio required to auto-accept
FUZZY_REVIEW_THRESHOLD = 0.75    # below auto-accept but worth a human look

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
# Administrative name normalisation
# --------------------------------------------------------------------------
# Observed corruptions across the four sources:
#   trailing / doubled whitespace ...... "Daibewo  ", "Adekeni "
#   full upper case .................... "YOBRIFA-WEST", "DUTZAMA"
#   administrative-type suffixes ....... "Ngenuta-Central Local Govt Area",
#                                        "Adekeni LGA"
#   separator drift .................... "Adelaja North" vs "Adelaja-North",
#                                        "AgbkotuSouth" vs "Agbkotu-South"
#   punctuation variants ............... "L.G.A", "L.G.A."
#
# The match key therefore folds case, strips administrative-type suffixes, and
# discards *all* non-alphanumeric characters so that separator drift cannot
# cause a false negative. This is deliberately aggressive: the risk it creates
# is collapsing two genuinely distinct units, so the pipeline asserts that the
# key is unique within each authoritative layer before it is used.

_ADMIN_SUFFIXES = [
    r"local\s*government\s*area",
    r"local\s*govt\.?\s*area",
    r"local\s*govt\.?",
    r"l\.?\s*g\.?\s*a\.?",
    r"lga",
    r"senatorial\s*district",
    r"district",
    r"state",
]
_SUFFIX_RE = re.compile(r"\b(?:" + "|".join(_ADMIN_SUFFIXES) + r")\b\s*$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def clean_text(value) -> str:
    """Whitespace/unicode tidy of a raw cell value, preserving human spelling."""
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace(" ", " ")                     # non-breaking space
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" .,;:-–—")                # stray trailing punctuation
    return s


def title_case_admin(value: str) -> str:
    """
    Render a cleaned administrative name in the house style used by the
    authoritative boundary layer: title case, hyphens preserved and the token
    after a hyphen also capitalised ("agbkotu-south" -> "Agbkotu-South").
    """
    s = clean_text(value)
    if not s:
        return ""
    parts = re.split(r"(\s+|-)", s.lower())
    out = []
    for p in parts:
        if p.strip() and p != "-":
            out.append(p[:1].upper() + p[1:])
        else:
            out.append(p)
    return "".join(out)


def match_key(value, strip_suffix: bool = True) -> str:
    """
    The canonical join key for an administrative name.

    Case-folded, administrative-type suffix removed, all non-alphanumerics
    dropped. "Ngenuta-Central Local Govt Area", "NGENUTA-CENTRAL",
    "Ngenuta Central " and "NgenutaCentral" all collapse to "NGENUTACENTRAL".
    """
    s = clean_text(value)
    if not s:
        return ""
    if strip_suffix:
        prev = None
        while prev != s:                              # e.g. "X Local Govt Area LGA"
            prev = s
            s = _SUFFIX_RE.sub("", s).strip()
    return _NON_ALNUM.sub("", s).upper()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def best_fuzzy(candidate_key: str, universe: dict[str, str]) -> tuple[str | None, float]:
    """
    Nearest key in `universe` (match_key -> canonical name) by character
    similarity. Returns (matched_key, score); (None, 0.0) if universe is empty.
    """
    best, best_score = None, 0.0
    for k in universe:
        s = similarity(candidate_key, k)
        if s > best_score:
            best, best_score = k, s
    return best, best_score


# --------------------------------------------------------------------------
# Reconciliation ledger
# --------------------------------------------------------------------------


@dataclass
class Ledger:
    """
    An append-only record of every name reconciliation decision.

    The assessment requires "a documented record of every name it reconciled and
    every name it could not". Each row states the source, the raw value, the
    value it was mapped to, the rule that produced the mapping, a confidence
    score, and the outcome. Rows with outcome != 'resolved' are the exceptions
    report.
    """

    rows: list[dict] = field(default_factory=list)

    def record(self, *, source: str, field_name: str, raw_value, resolved_value,
               method: str, confidence: float, outcome: str, note: str = "",
               entity_id: str = "") -> None:
        self.rows.append({
            "source": source,
            "entity_id": entity_id,
            "field": field_name,
            "raw_value": "" if raw_value is None else str(raw_value),
            "resolved_value": "" if resolved_value is None else str(resolved_value),
            "method": method,
            "confidence": round(float(confidence), 4),
            "outcome": outcome,          # resolved | resolved_review | unresolved | conflict
            "note": note,
        })

    def to_frame(self):
        import pandas as pd
        cols = ["source", "entity_id", "field", "raw_value", "resolved_value",
                "method", "confidence", "outcome", "note"]
        if not self.rows:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(self.rows)[cols]

    def summary(self) -> dict:
        from collections import Counter
        return dict(Counter(r["outcome"] for r in self.rows))


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
