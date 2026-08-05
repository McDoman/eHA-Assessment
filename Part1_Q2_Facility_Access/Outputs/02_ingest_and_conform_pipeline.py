"""
02_ingest_and_conform_pipeline.py
=================================
Ingest every supplied layer, repair it, reconcile it against the authority, and
emit one conformed GeoPackage that stage 03 loads into the database.

Stages merged into this single pipeline script
----------------------------------------------
  A. Facility register: coordinate parsing and repair from free-text
  B. Facility register: administrative name reconciliation and spatial
     re-assignment of each facility to the ward that actually contains it
  C. Personnel scores: read the MapInfo Interchange file, join to the register,
     and account for both orphan and unscored facilities
  D. Staffing norms: derive adequacy from the published standard
  E. Ward population: reconcile the two population sources
  F. Boundaries and roads: conform, validate geometry, declare CRS
  G. Emission of the conformed GeoPackage and the two ingest ledgers

Outputs
-------
  data/conformed_layers.gpkg                    all layers, CRS declared
  reports/02_coordinate_repair_ledger.csv       every coordinate touched
  reports/02_facility_join_ledger.csv           every facility-level reconciliation
  reports/02_ingest_and_conform_report.md       written account
"""

from __future__ import annotations

import re
from collections import Counter

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from common import (ART, COUNTRY_BBOX, CRS_GEOGRAPHIC, CRS_PROJECTED,
                    CRS_PROJECTED_LABEL, SRC, Ledger, banner, best_fuzzy,
                    clean_text, get_logger, match_key, FUZZY_MATCH_THRESHOLD)

LOG = get_logger("02_ingest_and_conform")

MIN_LON, MIN_LAT, MAX_LON, MAX_LAT = COUNTRY_BBOX

# Cadre columns, and the norm column each one must be tested against.
CADRES = {
    "med_officers": "min_medical_officers",
    "nurses_midwives": "min_nurses_midwives",
    "chews": "min_chews",
    "lab_scientists": "min_lab_scientists",
    "pharm_techs": "min_pharmacy_technicians",
}


# ==========================================================================
# A. Coordinate parsing and repair
# ==========================================================================

_DMS_RE = re.compile(
    r"""^\s*(?P<deg>\d{1,3})\s*[°d:]\s*
        (?P<min>\d{1,2})\s*['’m:]?\s*
        (?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*["”s]?)?\s*
        (?P<hemi>[NSEWnsew])?\s*$""",
    re.VERBOSE,
)


def parse_coordinate(raw) -> tuple[float | None, str]:
    """
    Parse one free-text coordinate cell, returning (decimal_degrees, method).

    The source system permitted free entry, so a single column mixes three
    encodings. Each is handled explicitly; nothing is coerced by guesswork.

      "7.908017"        decimal degrees                  -> decimal
      "9,066455"        comma as the decimal separator   -> comma_decimal
      "9°38'33.6\"E"    degrees/minutes/seconds with a
                        hemisphere letter                -> dms
    """
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None, "missing"
    s = clean_text(raw)
    if not s or s.lower() in {"nan", "null", "none", "n/a", "-", ""}:
        return None, "missing"

    m = _DMS_RE.match(s)
    if m:
        deg = float(m.group("deg"))
        minute = float(m.group("min") or 0)
        sec = float(m.group("sec") or 0)
        val = deg + minute / 60.0 + sec / 3600.0
        if (m.group("hemi") or "").upper() in {"S", "W"}:
            val = -val
        return val, "dms"

    # Comma decimal separator. Only safe when there is exactly one comma and it
    # is not acting as a thousands separator (which would leave 3 trailing
    # digits); these values have 6, so the test is unambiguous.
    if s.count(",") == 1 and "." not in s:
        left, right = s.split(",")
        if left.lstrip("-").isdigit() and right.isdigit() and len(right) != 3:
            try:
                return float(f"{left}.{right}"), "comma_decimal"
            except ValueError:
                pass

    try:
        return float(s.replace(" ", "")), "decimal"
    except ValueError:
        return None, "unparseable"


def in_bbox(lon, lat) -> bool:
    return (lon is not None and lat is not None
            and MIN_LON <= lon <= MAX_LON and MIN_LAT <= lat <= MAX_LAT)


def repair_pair(lon, lat, lon_method, lat_method) -> tuple[float | None, float | None, str, str]:
    """
    Apply plausibility repair to a parsed coordinate pair.

    Repairs are attempted in decreasing order of confidence and each one must
    *land the point inside the study area* to be accepted. A repair that does
    not produce a plausible location is rejected, and the point is quarantined
    rather than being placed somewhere convenient.
    """
    if lon is None or lat is None:
        return None, None, "no_geometry", "coordinate missing or unparseable"

    if in_bbox(lon, lat):
        return lon, lat, "accepted", ""

    if in_bbox(lat, lon):
        return lat, lon, "repaired_axis_swap", "longitude and latitude were transposed"

    if in_bbox(abs(lon), abs(lat)) and (lon < 0 or lat < 0):
        return abs(lon), abs(lat), "repaired_sign", "hemisphere sign was wrong for the study area"

    if in_bbox(abs(lat), abs(lon)) and (lon < 0 or lat < 0):
        return abs(lat), abs(lon), "repaired_swap_and_sign", "transposed and wrongly signed"

    # Decimal-point drift, e.g. 96.6 for 9.66.
    for dl, dt in ((lon / 10, lat), (lon, lat / 10), (lon / 10, lat / 10)):
        if in_bbox(dl, dt):
            return dl, dt, "repaired_scale", "decimal point misplaced by one order of magnitude"

    return None, None, "quarantined_out_of_area", (
        f"parsed to ({lon:.5f}, {lat:.5f}) which lies outside the study area and no "
        f"repair placed it inside")


def ingest_facilities(ledger: Ledger) -> pd.DataFrame:
    df = pd.read_csv(SRC["facilities"], dtype=str)
    LOG.info("Facility register: %d rows, %d columns", *df.shape)

    rows = []
    for r in df.itertuples():
        lon, lm = parse_coordinate(r.longitude)
        lat, tm = parse_coordinate(r.latitude)
        flon, flat, status, note = repair_pair(lon, lat, lm, tm)
        rows.append({
            "facility_id": clean_text(r.facility_id),
            "raw_longitude": clean_text(r.longitude),
            "raw_latitude": clean_text(r.latitude),
            "parse_method": lm if lm == tm else f"{lm}/{tm}",
            "parsed_longitude": lon, "parsed_latitude": lat,
            "longitude": flon, "latitude": flat,
            "coord_status": status, "coord_note": note,
        })
    coords = pd.DataFrame(rows)

    for r in coords.itertuples():
        if r.coord_status == "accepted" and r.parse_method == "decimal":
            continue                      # clean on arrival, nothing to record
        ledger.record(source="health_facilities.csv", entity_id=r.facility_id,
                      field_name="coordinates",
                      raw_value=f"{r.raw_longitude} , {r.raw_latitude}",
                      resolved_value=(f"{r.longitude:.6f} , {r.latitude:.6f}"
                                      if r.longitude is not None else None),
                      method=r.parse_method, confidence=1.0 if r.longitude is not None else 0.0,
                      outcome=("resolved" if r.coord_status.startswith(("accepted", "repaired"))
                               else "unresolved"),
                      note=f"{r.coord_status}: {r.coord_note}".strip(": "))

    LOG.info("Coordinate outcomes: %s", Counter(coords.coord_status).most_common())
    LOG.info("Parse methods used : %s", Counter(coords.parse_method).most_common())

    out = df.drop(columns=["longitude", "latitude"]).copy()
    out["facility_id"] = out.facility_id.map(clean_text)
    return out.merge(coords, on="facility_id", how="left", validate="1:1")


# ==========================================================================
# B. Administrative reconciliation and spatial ward assignment
# ==========================================================================

def reconcile_facility_admin(fac: pd.DataFrame, wards: gpd.GeoDataFrame,
                             lgas: gpd.GeoDataFrame, ledger: Ledger) -> pd.DataFrame:
    """
    Reconcile each facility's declared administrative names, then override the
    declaration with the ward that geometrically contains the point.

    The declared ward is an attribute typed by a human. The containing ward is a
    spatial fact. Where they disagree the spatial fact wins for analysis, but
    the disagreement is recorded rather than erased, because a systematic
    mismatch would indicate that the coordinates, not the labels, are wrong.
    """
    ward_universe = {match_key(n): n for n in wards.ward_name}
    lga_universe = {match_key(n): n for n in lgas.lga_name}

    def resolve(raw, universe, field_name, fid):
        raw_clean = clean_text(raw)
        if raw_clean in set(universe.values()):
            return raw_clean, "exact", 1.0, "resolved"
        key = match_key(raw_clean)
        if key in universe:
            ledger.record(source="health_facilities.csv", entity_id=fid, field_name=field_name,
                          raw_value=raw_clean, resolved_value=universe[key],
                          method="normalised", confidence=1.0, outcome="resolved",
                          note="case/whitespace/suffix/separator repair")
            return universe[key], "normalised", 1.0, "resolved"
        bk, score = best_fuzzy(key, universe)
        if bk and score >= FUZZY_MATCH_THRESHOLD:
            ledger.record(source="health_facilities.csv", entity_id=fid, field_name=field_name,
                          raw_value=raw_clean, resolved_value=universe[bk],
                          method="fuzzy_accepted", confidence=score, outcome="resolved",
                          note=f"character similarity {score:.3f}")
            return universe[bk], "fuzzy_accepted", score, "resolved"
        ledger.record(source="health_facilities.csv", entity_id=fid, field_name=field_name,
                      raw_value=raw_clean, resolved_value=None, method="no_match",
                      confidence=score if bk else 0.0, outcome="unresolved",
                      note=f"nearest candidate {universe.get(bk)!r} at {score:.3f}")
        return None, "no_match", score if bk else 0.0, "unresolved"

    dec_ward, dec_lga, methods = [], [], []
    for r in fac.itertuples():
        w, _, _, _ = resolve(r.ward_name, ward_universe, "ward_name", r.facility_id)
        l, m, _, _ = resolve(r.lga_name, lga_universe, "lga_name", r.facility_id)
        dec_ward.append(w); dec_lga.append(l); methods.append(m)
    fac["declared_ward_name"] = dec_ward
    fac["declared_lga_name"] = dec_lga
    fac["admin_match_method"] = methods

    # Spatial assignment.
    located = fac[fac.longitude.notna()].copy()
    pts = gpd.GeoDataFrame(
        located,
        geometry=[Point(x, y) for x, y in zip(located.longitude, located.latitude)],
        crs=CRS_GEOGRAPHIC,
    )
    joined = gpd.sjoin(
        pts, wards[["ward_code", "ward_name", "lga_code", "lga_name", "sen_code",
                    "sen_district", "state_code", "state_name", "geometry"]],
        how="left", predicate="within",
    ).drop(columns="index_right")
    joined = joined.rename(columns={
        "ward_code": "ward_code_spatial", "ward_name_right": "ward_name_spatial",
        "lga_code": "lga_code_spatial", "lga_name_right": "lga_name_spatial",
    })

    # A point may land in no polygon at all (a sliver, or just offshore of the
    # ward boundary). Rather than discard it, snap it to the nearest ward and
    # record how far it had to move.
    orphan = joined.ward_code_spatial.isna()
    if orphan.any():
        LOG.info("%d located facility/facilities fell outside every ward polygon; "
                 "snapping to nearest ward", int(orphan.sum()))
        near = gpd.sjoin_nearest(
            joined.loc[orphan, ["facility_id", "geometry"]].to_crs(CRS_PROJECTED),
            wards[["ward_code", "ward_name", "lga_code", "lga_name", "sen_code",
                   "sen_district", "state_code", "state_name", "geometry"]].to_crs(CRS_PROJECTED),
            how="left", distance_col="snap_m",
        )
        for r in near.itertuples():
            mask = joined.facility_id == r.facility_id
            joined.loc[mask, "ward_code_spatial"] = r.ward_code
            joined.loc[mask, "ward_name_spatial"] = r.ward_name
            joined.loc[mask, "lga_code_spatial"] = r.lga_code
            joined.loc[mask, "lga_name_spatial"] = r.lga_name
            joined.loc[mask, "sen_code"] = r.sen_code
            joined.loc[mask, "sen_district"] = r.sen_district
            joined.loc[mask, "state_code"] = r.state_code
            joined.loc[mask, "state_name"] = r.state_name
            joined.loc[mask, "ward_assignment"] = "snapped_to_nearest"
            ledger.record(source="health_facilities.csv", entity_id=r.facility_id,
                          field_name="ward_code", raw_value="(outside all ward polygons)",
                          resolved_value=r.ward_code, method="nearest_ward_snap",
                          confidence=0.7, outcome="resolved_review",
                          note=f"snapped {r.snap_m/1000:.2f} km to the nearest ward")
    joined["ward_assignment"] = joined.get("ward_assignment", pd.Series(index=joined.index,
                                                                       dtype=object))
    joined["ward_assignment"] = joined.ward_assignment.fillna("within_polygon")

    mismatch = joined[joined.declared_ward_name.notna()
                      & (joined.declared_ward_name != joined.ward_name_spatial)]
    LOG.info("Declared ward disagrees with containing ward for %d of %d located facilities",
             len(mismatch), len(joined))
    for r in mismatch.itertuples():
        ledger.record(source="health_facilities.csv", entity_id=r.facility_id,
                      field_name="ward_name", raw_value=r.declared_ward_name,
                      resolved_value=r.ward_name_spatial, method="spatial_override",
                      confidence=0.95, outcome="resolved_review",
                      note="declared ward differs from the ward containing the point; "
                           "geometry taken as authoritative")

    unlocated = fac[fac.longitude.isna()].copy()
    for c in ["ward_code_spatial", "ward_name_spatial", "lga_code_spatial", "lga_name_spatial",
              "sen_code", "sen_district", "state_code", "state_name"]:
        unlocated[c] = None
    unlocated["ward_assignment"] = "no_geometry"
    unlocated["geometry"] = None

    return pd.concat([joined, unlocated], ignore_index=True)


# ==========================================================================
# C. Personnel scores from MapInfo Interchange Format
# ==========================================================================

def ingest_scores(fac: pd.DataFrame, ledger: Ledger) -> tuple[pd.DataFrame, dict]:
    """
    Read the .mif/.mid pair and join it to the register.

    The .mif header declares `CoordSys Earth Projection 1, 104`, which is
    MapInfo's code for a plain latitude/longitude system on WGS 84 — that is
    EPSG:4326. GDAL reads that declaration, so the CRS is asserted from the file
    rather than assumed, then re-declared explicitly on the way out.
    """
    mif = gpd.read_file(SRC["scores_mif"])
    LOG.info("MIF/MID: %d rows, declared CRS %s", len(mif), mif.crs)
    if mif.crs is None:
        mif = mif.set_crs(CRS_GEOGRAPHIC)
    elif mif.crs.to_epsg() != 4326:
        mif = mif.to_crs(CRS_GEOGRAPHIC)

    scores = pd.DataFrame({
        "facility_id": mif.FACILITY_ID.map(clean_text),
        "score_facility_name": mif.FACILITY_NAME.map(clean_text),
        "med_officers": mif.MED_OFFICERS.astype("Int64"),
        "nurses_midwives": mif.NURSES_MIDWIVES.astype("Int64"),
        "chews": mif.CHEWS.astype("Int64"),
        "lab_scientists": mif.LAB_SCIENTISTS.astype("Int64"),
        "pharm_techs": mif.PHARM_TECHS.astype("Int64"),
        "personnel_score": mif.PERSONNEL_SCORE.astype(float),
        "sen_rank": mif.SEN_RANK.astype("Int64"),
        "score_longitude": mif.geometry.x,
        "score_latitude": mif.geometry.y,
    })

    reg_ids, sc_ids = set(fac.facility_id), set(scores.facility_id)
    unscored, orphan = sorted(reg_ids - sc_ids), sorted(sc_ids - reg_ids)

    for fid in orphan:
        ledger.record(source="facility_personnel_scores.mid", entity_id=fid,
                      field_name="facility_id", raw_value=fid, resolved_value=None,
                      method="no_match", confidence=0.0, outcome="unresolved",
                      note="scored facility is absent from the facility register; "
                           "excluded from analysis because it has no ward, type or ownership")
    for fid in unscored:
        ledger.record(source="health_facilities.csv", entity_id=fid,
                      field_name="personnel_score", raw_value="(register)", resolved_value=None,
                      method="no_match", confidence=0.0, outcome="unresolved",
                      note="registered facility was not scored in the assessment; "
                           "staffing adequacy is unknown, not inadequate")

    LOG.info("Score join: %d matched, %d registered-but-unscored, %d scored-but-unregistered",
             len(reg_ids & sc_ids), len(unscored), len(orphan))
    return scores, {"unscored": unscored, "orphan": orphan,
                    "matched": len(reg_ids & sc_ids)}


# ==========================================================================
# D. Adequacy against the published staffing norms
# ==========================================================================

def apply_norms(fac: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Classify each facility against the published minimum staffing standard.

    The rule is taken verbatim from the standard rather than invented: a
    facility is adequately staffed when it meets or exceeds the minimum for
    *every cadre with a non-zero minimum*. Cadres whose minimum is zero for a
    facility type are not tested, so a health post is not penalised for having
    no medical officer.

    Three states are produced, and the third matters:
      adequate    -- scored, and meets every binding minimum
      inadequate  -- scored, and fails at least one binding minimum
      unknown     -- not scored at all
    """
    norms = pd.read_csv(SRC["norms"])
    norms["facility_type"] = norms.facility_type.map(clean_text)
    n_by_type = {r.facility_type: r for r in norms.itertuples()}

    status, shortfalls, binding_n, met_n = [], [], [], []
    for r in fac.itertuples():
        norm = n_by_type.get(r.facility_type)
        if norm is None or pd.isna(getattr(r, "personnel_score", np.nan)):
            status.append("unknown"); shortfalls.append(""); binding_n.append(pd.NA)
            met_n.append(pd.NA)
            continue
        gaps, binding, met = [], 0, 0
        for cadre, min_col in CADRES.items():
            minimum = int(getattr(norm, min_col))
            if minimum <= 0:
                continue                       # not a binding requirement for this type
            binding += 1
            have = getattr(r, cadre)
            have = 0 if pd.isna(have) else int(have)
            if have >= minimum:
                met += 1
            else:
                gaps.append(f"{cadre}:{have}/{minimum}")
        status.append("adequate" if not gaps else "inadequate")
        shortfalls.append("; ".join(gaps))
        binding_n.append(binding); met_n.append(met)

    fac["staffing_status"] = status
    fac["staffing_shortfall"] = shortfalls
    fac["binding_cadres"] = pd.array(binding_n, dtype="Int64")
    fac["binding_cadres_met"] = pd.array(met_n, dtype="Int64")
    LOG.info("Staffing adequacy: %s", Counter(status).most_common())
    return fac, norms


# ==========================================================================
# E/F. Population reconciliation, boundaries and roads
# ==========================================================================

def reconcile_population(wards: gpd.GeoDataFrame, ledger: Ledger) -> gpd.GeoDataFrame:
    """
    Settle the disagreement between the two population sources.

    `ward_population.csv` carries the estimation method per ward but is missing
    14 values. The `wards` layer in the GeoPackage is complete. The complete
    source is used as the denominator; the CSV contributes the provenance field,
    and every value that differs between the two is recorded.
    """
    csv = pd.read_csv(SRC["ward_pop"])
    LOG.info("ward_population.csv: %d rows, %d null total_population",
             len(csv), int(csv.total_population.isna().sum()))

    merged = wards.merge(
        csv[["ward_code", "total_population", "population_under5", "population_source"]],
        on="ward_code", how="left", suffixes=("", "_csv"), validate="1:1")

    diff = merged[merged.total_population_csv.notna()
                  & (merged.total_population_csv.round(0) != merged.total_population.round(0))]
    for r in diff.itertuples():
        ledger.record(source="ward_population.csv", entity_id=r.ward_code,
                      field_name="total_population", raw_value=r.total_population_csv,
                      resolved_value=r.total_population, method="prefer_complete_source",
                      confidence=0.9, outcome="resolved_review",
                      note="value differs from the boundary layer; boundary layer used")
    for r in merged[merged.total_population_csv.isna()].itertuples():
        ledger.record(source="ward_population.csv", entity_id=r.ward_code,
                      field_name="total_population", raw_value=None,
                      resolved_value=r.total_population, method="prefer_complete_source",
                      confidence=1.0, outcome="resolved",
                      note="population missing from the CSV; taken from the boundary layer")

    merged["population_source"] = merged.population_source.fillna("Boundary layer attribute")
    merged = merged.drop(columns=["total_population_csv", "population_under5_csv"])
    LOG.info("Population reconciled: %d ward(s) differed, %d were missing from the CSV; "
             "denominator total = %s",
             len(diff), int(csv.total_population.isna().sum()),
             f"{merged.total_population.sum():,.0f}")
    return merged


def ingest_roads() -> gpd.GeoDataFrame:
    roads = gpd.read_file(SRC["roads"])
    if roads.crs is None:
        roads = roads.set_crs(CRS_GEOGRAPHIC)
    roads = roads.to_crs(CRS_GEOGRAPHIC)
    roads["speed_kmh"] = pd.to_numeric(roads.speed_kmh, errors="coerce")
    bad = roads.speed_kmh.isna() | (roads.speed_kmh <= 0)
    if bad.any():
        LOG.warning("%d road segment(s) have an unusable speed; set to the class median",
                    int(bad.sum()))
        roads.loc[bad, "speed_kmh"] = roads.groupby("road_class").speed_kmh.transform("median")
    roads["length_m"] = roads.to_crs(CRS_PROJECTED).length
    roads["traverse_min"] = roads.length_m / 1000.0 / roads.speed_kmh * 60.0
    LOG.info("Roads: %d segments, %.0f km total, speeds %.0f–%.0f km/h",
             len(roads), roads.length_m.sum() / 1000, roads.speed_kmh.min(), roads.speed_kmh.max())
    return roads


# ==========================================================================
# G. Report
# ==========================================================================

def write_report(fac, coord_counts, join_stats, staffing_counts, wards, roads,
                 ledger, unlocated_ids) -> None:
    lines = [
        "# Stage 2 — Ingest, Repair and Conform",
        "",
        f"Analysis CRS: **{CRS_PROJECTED_LABEL}**. Storage CRS: **{CRS_GEOGRAPHIC}**.",
        "All distance, length and area computation is done in the projected CRS; nothing",
        "is measured in degrees.",
        "",
        "## 1. Coordinate repair",
        "",
        "The register's coordinates are text because the source system permitted free",
        "entry, and three encodings are mixed in the same column. Each is parsed",
        "explicitly. A repair is only accepted if it places the point inside the study",
        "area — a repair that does not is rejected and the facility is quarantined rather",
        "than being moved somewhere convenient.",
        "",
        "| Outcome | Facilities |",
        "|---|---:|",
    ]
    for k, v in coord_counts["status"].most_common():
        lines.append(f"| `{k}` | {v} |")
    lines += ["", "| Parse method | Facilities |", "|---|---:|"]
    for k, v in coord_counts["method"].most_common():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        f"{len(unlocated_ids)} facility/facilities carry no usable location. They are kept in",
        "the database — they are real facilities and their staffing is real — but they are",
        "excluded from the distance computation, and that exclusion is stated in the",
        "results rather than hidden.",
        "",
        "## 2. Administrative reconciliation and spatial assignment",
        "",
        "Declared ward and LGA names were reconciled to the boundary layer. Each facility",
        "was then re-assigned to the ward whose polygon actually contains it. Where the",
        "declared ward and the containing ward disagree, geometry wins for analysis and the",
        "disagreement is written to the ledger.",
        "",
        "| Assignment | Facilities |",
        "|---|---:|",
    ]
    for k, v in Counter(fac.ward_assignment.dropna()).most_common():
        lines.append(f"| `{k}` | {v} |")

    lines += [
        "",
        "## 3. Personnel scores (MapInfo Interchange Format)",
        "",
        "The `.mif` header declares `CoordSys Earth Projection 1, 104` — MapInfo's code for",
        "geographic latitude/longitude on WGS 84, i.e. EPSG:4326. That declaration is read",
        "from the file and re-asserted explicitly rather than assumed.",
        "",
        "| Relation | Facilities |",
        "|---|---:|",
        f"| Registered **and** scored | {join_stats['matched']} |",
        f"| Registered but **not scored** | {len(join_stats['unscored'])} |",
        f"| Scored but **not registered** | {len(join_stats['orphan'])} |",
        "",
        "The distinction between *unscored* and *inadequately staffed* is preserved",
        "throughout. A facility that was never assessed is recorded as `unknown`, never",
        "silently counted as failing — treating missing assessment as failure would",
        "manufacture an access gap that the data does not support.",
        "",
        f"The {len(join_stats['orphan'])} scored-but-unregistered records "
        f"(`{', '.join(join_stats['orphan'][:9])}`) have no facility type, so they cannot be",
        "tested against a type-specific staffing norm, and no ward, type or ownership. They",
        "are retained in the database in a quarantine table and excluded from analysis.",
        "",
        "## 4. Staffing adequacy",
        "",
        "Adequacy is taken from `minimum_staffing_norms.csv` verbatim: a facility is",
        "adequate when it meets or exceeds the minimum for **every cadre with a non-zero",
        "minimum** for its type. Cadres with a zero minimum are not tested, so a health",
        "post is not penalised for having no medical officer. No cut point was invented.",
        "",
        "| Status | Facilities |",
        "|---|---:|",
    ]
    for k, v in staffing_counts.most_common():
        lines.append(f"| `{k}` | {v} |")

    lines += [
        "",
        "## 5. Population denominator",
        "",
        "Two population sources disagree. `ward_population.csv` records the estimation",
        "method per ward but is missing 14 values; the `wards` layer is complete. The",
        "complete source is used as the denominator and the CSV contributes provenance.",
        "Every differing and every missing value is in the ledger.",
        "",
        f"- Wards: **{len(wards)}**",
        f"- Total population (denominator): **{wards.total_population.sum():,.0f}**",
        f"- Under-5 population (sensitivity denominator): **{wards.population_under5.sum():,.0f}**",
        "",
        "| Estimation method | Wards |",
        "|---|---:|",
    ]
    for k, v in wards.population_source.value_counts().items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "The two methods are not equivalent. A 2026 projection from a 2006 census carries",
        "twenty years of compounding assumption; a 2024 gridded estimate is closer to",
        "observation but redistributes population by built-up area. Because the two are",
        "mixed across wards, the population-weighted results in stage 4 are re-run against",
        "each method separately as a sensitivity test.",
        "",
        "## 6. Road network",
        "",
        f"- Segments: **{len(roads)}**",
        f"- Total length: **{roads.length_m.sum()/1000:,.0f} km**",
        f"- Speed range: **{roads.speed_kmh.min():.0f}–{roads.speed_kmh.max():.0f} km/h**",
        "",
        "| Class | Segments | Mean speed (km/h) |",
        "|---|---:|---:|",
    ]
    for cls, grp in roads.groupby("road_class"):
        lines.append(f"| {cls} | {len(grp)} | {grp.speed_kmh.mean():.0f} |")

    s = ledger.summary()
    lines += [
        "",
        "## 7. Ledger summary",
        "",
        "| Outcome | Records |",
        "|---|---:|",
    ] + [f"| `{k}` | {v} |" for k, v in sorted(s.items())] + [
        "",
        "Full detail: `02_coordinate_repair_ledger.csv` and `02_facility_join_ledger.csv`.",
        "",
    ]
    ART["ingest_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote %s", ART["ingest_report"].name)


def main():
    banner(LOG, "STAGE 02 — INGEST AND CONFORM")
    coord_ledger, join_ledger = Ledger(), Ledger()

    wards = gpd.read_file(SRC["boundaries"], layer="wards")
    lgas = gpd.read_file(SRC["boundaries"], layer="lgas")
    sens = gpd.read_file(SRC["boundaries"], layer="senatorial_districts")
    states = gpd.read_file(SRC["boundaries"], layer="states")
    for name, lyr in (("wards", wards), ("lgas", lgas),
                      ("senatorial_districts", sens), ("states", states)):
        invalid = int((~lyr.is_valid).sum())
        if invalid:
            LOG.warning("%s: %d invalid geometry/geometries, repairing with a zero buffer",
                        name, invalid)
            lyr.geometry = lyr.geometry.buffer(0)
        if lyr.crs is None:
            lyr.set_crs(CRS_GEOGRAPHIC, inplace=True)
        LOG.info("%-20s %4d features  CRS %s  valid=%s",
                 name, len(lyr), lyr.crs, invalid == 0)

    fac = ingest_facilities(coord_ledger)
    coord_counts = {"status": Counter(fac.coord_status), "method": Counter(fac.parse_method)}

    fac = reconcile_facility_admin(fac, wards, lgas, join_ledger)
    scores, join_stats = ingest_scores(fac, join_ledger)
    fac = fac.merge(scores, on="facility_id", how="left", validate="1:1")
    fac, norms = apply_norms(fac)

    wards = reconcile_population(wards, join_ledger)
    roads = ingest_roads()

    # ---- assemble the conformed facility layer -------------------------
    fac_out = gpd.GeoDataFrame({
        "facility_id": fac.facility_id,
        "facility_name": fac.facility_name.map(clean_text),
        "facility_type": fac.facility_type.map(clean_text),
        "ownership": fac.ownership.map(clean_text),
        "ward_code": fac.ward_code_spatial,
        "ward_name": fac.ward_name_spatial,
        "lga_code": fac.lga_code_spatial,
        "lga_name": fac.lga_name_spatial,
        "sen_code": fac.sen_code,
        "sen_district": fac.sen_district,
        "state_code": fac.state_code,
        "state_name": fac.state_name,
        "declared_ward_name": fac.declared_ward_name,
        "declared_lga_name": fac.declared_lga_name,
        "ward_assignment": fac.ward_assignment,
        "longitude": fac.longitude,
        "latitude": fac.latitude,
        "coord_status": fac.coord_status,
        "coord_parse_method": fac.parse_method,
        "med_officers": fac.med_officers,
        "nurses_midwives": fac.nurses_midwives,
        "chews": fac.chews,
        "lab_scientists": fac.lab_scientists,
        "pharm_techs": fac.pharm_techs,
        "personnel_score": fac.personnel_score,
        "sen_rank": fac.sen_rank,
        "staffing_status": fac.staffing_status,
        "staffing_shortfall": fac.staffing_shortfall,
        "binding_cadres": fac.binding_cadres,
        "binding_cadres_met": fac.binding_cadres_met,
    }, geometry=fac.geometry if "geometry" in fac else None, crs=CRS_GEOGRAPHIC)

    unlocated_ids = fac_out.loc[fac_out.geometry.isna(), "facility_id"].tolist()
    fac_located = fac_out[fac_out.geometry.notna()].copy()
    fac_unlocated = pd.DataFrame(fac_out[fac_out.geometry.isna()].drop(columns="geometry"))

    orphan_scores = scores[~scores.facility_id.isin(fac_out.facility_id)].copy()

    # ---- write the conformed GeoPackage --------------------------------
    out = ART["conformed_gpkg"]
    if out.exists():
        out.unlink()
    wards.to_file(out, layer="wards", driver="GPKG")
    lgas.to_file(out, layer="lgas", driver="GPKG")
    sens.to_file(out, layer="senatorial_districts", driver="GPKG")
    states.to_file(out, layer="states", driver="GPKG")
    roads.to_file(out, layer="roads", driver="GPKG")
    fac_located.to_file(out, layer="facilities", driver="GPKG")
    LOG.info("Conformed GeoPackage written: %s", out.name)

    fac_unlocated.to_csv(DATA := ART["conformed_gpkg"].with_name("facilities_unlocated.csv"),
                         index=False, encoding="utf-8")
    orphan_scores.to_csv(ART["conformed_gpkg"].with_name("scores_unregistered.csv"),
                         index=False, encoding="utf-8")
    norms.to_csv(ART["conformed_gpkg"].with_name("staffing_norms.csv"),
                 index=False, encoding="utf-8")

    coord_ledger.to_frame().to_csv(ART["coord_ledger"], index=False, encoding="utf-8")
    join_ledger.to_frame().to_csv(ART["join_ledger"], index=False, encoding="utf-8")

    write_report(fac_out, coord_counts, join_stats, Counter(fac_out.staffing_status),
                 wards, roads, join_ledger, unlocated_ids)

    LOG.info("Facilities: %d located, %d unlocated, %d unregistered scores quarantined",
             len(fac_located), len(fac_unlocated), len(orphan_scores))
    banner(LOG, "STAGE 02 COMPLETE")


if __name__ == "__main__":
    main()
