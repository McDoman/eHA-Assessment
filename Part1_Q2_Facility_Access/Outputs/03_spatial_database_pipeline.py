"""
03_spatial_database_pipeline.py
===============================
Task 2 -- Load all processed layers into a spatially enabled database with
declared coordinate reference systems, primary keys, spatial indexes, and
referential constraints between facilities, wards, LGAs and senatorial districts.

Choice of engine
----------------
PostGIS is the natural home for this and is what a ministry would run in
production. It is not installable in the assessment environment (no PostgreSQL
server, no `psql`, no administrative rights), so the database is built in
**DuckDB with the `spatial` extension**, which is a real database engine
providing everything the task requires:

  * a native `GEOMETRY` type with the full OGC/GEOS function set
  * `PRIMARY KEY`, `UNIQUE`, `NOT NULL`, `CHECK` and `FOREIGN KEY` constraints,
    enforced on insert
  * R-tree spatial indexes (`CREATE INDEX ... USING RTREE`)

Two things PostGIS gives for free are supplied here explicitly, because a
governed database should not rely on convention:

  * `spatial_ref_sys` and `geometry_columns` are created and populated by hand,
    so every geometry column has a *declared* SRID and geometry type that can be
    queried, not merely an assumed one;
  * the equivalent PostGIS DDL is written to `database/postgis_schema.sql`, so
    the identical schema — same keys, same constraints, same GiST indexes — can
    be stood up on a ministry PostGIS server without re-derivation.

Stages merged into this single pipeline script
----------------------------------------------
  A. Schema definition (DDL: types, keys, constraints, comments)
  B. CRS registry population
  C. Load of every conformed layer, parents before children so the foreign keys
     are enforced on the way in rather than bolted on afterwards
  D. Spatial index creation
  E. Constraint and integrity validation, including deliberate negative tests
     that prove the constraints actually bite
  F. Emission of the PostGIS-equivalent DDL and the validation report
"""

from __future__ import annotations

import pandas as pd
import geopandas as gpd
import duckdb

from common import (ART, CRS_GEOGRAPHIC, CRS_PROJECTED, CRS_PROJECTED_LABEL,
                    banner, get_logger)

LOG = get_logger("03_spatial_database")

SRID_GEOGRAPHIC = 4326
SRID_PROJECTED = 102022

# --------------------------------------------------------------------------
# A. Schema
# --------------------------------------------------------------------------
# Declared parents-first so that every FOREIGN KEY references a table that
# already exists and already carries the PRIMARY KEY it points at.

DDL = """
-- ===================================================================
-- Governance: coordinate reference system registry
-- ===================================================================
CREATE TABLE spatial_ref_sys (
    srid        INTEGER      NOT NULL PRIMARY KEY,
    auth_name   VARCHAR      NOT NULL,
    auth_srid   INTEGER      NOT NULL,
    srtext      VARCHAR      NOT NULL,
    proj4text   VARCHAR      NOT NULL,
    role        VARCHAR      NOT NULL,
    CHECK (srid > 0)
);

CREATE TABLE geometry_columns (
    f_table_name      VARCHAR NOT NULL,
    f_geometry_column VARCHAR NOT NULL,
    coord_dimension   INTEGER NOT NULL,
    srid              INTEGER NOT NULL,
    geometry_type     VARCHAR NOT NULL,
    PRIMARY KEY (f_table_name, f_geometry_column),
    FOREIGN KEY (srid) REFERENCES spatial_ref_sys(srid),
    CHECK (coord_dimension IN (2, 3))
);

-- ===================================================================
-- Administrative hierarchy: states > senatorial districts > LGAs > wards
-- ===================================================================
CREATE TABLE states (
    state_code  VARCHAR   NOT NULL PRIMARY KEY,
    state_name  VARCHAR   NOT NULL UNIQUE,
    geom        GEOMETRY  NOT NULL,
    CHECK (length(state_code) > 0)
);

CREATE TABLE senatorial_districts (
    sen_code      VARCHAR   NOT NULL PRIMARY KEY,
    sen_district  VARCHAR   NOT NULL UNIQUE,
    state_code    VARCHAR   NOT NULL,
    geom          GEOMETRY  NOT NULL,
    FOREIGN KEY (state_code) REFERENCES states(state_code)
);

CREATE TABLE lgas (
    lga_code      VARCHAR   NOT NULL PRIMARY KEY,
    lga_name      VARCHAR   NOT NULL,
    sen_code      VARCHAR   NOT NULL,
    state_code    VARCHAR   NOT NULL,
    geom          GEOMETRY  NOT NULL,
    FOREIGN KEY (sen_code)   REFERENCES senatorial_districts(sen_code),
    FOREIGN KEY (state_code) REFERENCES states(state_code)
);

CREATE TABLE wards (
    ward_code          VARCHAR   NOT NULL PRIMARY KEY,
    ward_name          VARCHAR   NOT NULL,
    lga_code           VARCHAR   NOT NULL,
    sen_code           VARCHAR   NOT NULL,
    state_code         VARCHAR   NOT NULL,
    total_population   BIGINT    NOT NULL,
    population_under5  BIGINT    NOT NULL,
    population_source  VARCHAR   NOT NULL,
    geom               GEOMETRY  NOT NULL,
    FOREIGN KEY (lga_code)   REFERENCES lgas(lga_code),
    FOREIGN KEY (sen_code)   REFERENCES senatorial_districts(sen_code),
    FOREIGN KEY (state_code) REFERENCES states(state_code),
    CHECK (total_population  >= 0),
    CHECK (population_under5 >= 0),
    CHECK (population_under5 <= total_population)
);

-- ===================================================================
-- Reference data
-- ===================================================================
CREATE TABLE staffing_norms (
    facility_type             VARCHAR NOT NULL PRIMARY KEY,
    min_medical_officers      INTEGER NOT NULL,
    min_nurses_midwives       INTEGER NOT NULL,
    min_chews                 INTEGER NOT NULL,
    min_lab_scientists        INTEGER NOT NULL,
    min_pharmacy_technicians  INTEGER NOT NULL,
    adequacy_rule             VARCHAR NOT NULL,
    CHECK (min_medical_officers     >= 0),
    CHECK (min_nurses_midwives      >= 0),
    CHECK (min_chews                >= 0),
    CHECK (min_lab_scientists       >= 0),
    CHECK (min_pharmacy_technicians >= 0)
);

-- The normalised product of task 1. Held as a table in its own right, with a
-- foreign key to both parents, so the assertion it makes can be queried and
-- audited against the boundary layer rather than merged away.
CREATE TABLE lga_senatorial_crosswalk (
    lga_code             VARCHAR NOT NULL PRIMARY KEY,
    lga_name             VARCHAR NOT NULL,
    lga_name_source      VARCHAR,
    sen_code             VARCHAR NOT NULL,
    sen_district         VARCHAR NOT NULL,
    state_code           VARCHAR NOT NULL,
    state_name           VARCHAR NOT NULL,
    ward_count_declared  INTEGER,
    ward_count_observed  INTEGER,
    remarks              VARCHAR,
    match_method         VARCHAR NOT NULL,
    match_confidence     DOUBLE  NOT NULL,
    match_outcome        VARCHAR NOT NULL,
    source_sheet_row     INTEGER,
    FOREIGN KEY (lga_code)   REFERENCES lgas(lga_code),
    FOREIGN KEY (sen_code)   REFERENCES senatorial_districts(sen_code),
    FOREIGN KEY (state_code) REFERENCES states(state_code),
    CHECK (match_confidence BETWEEN 0 AND 1),
    CHECK (match_outcome IN ('resolved', 'resolved_review', 'unresolved', 'conflict'))
);

-- ===================================================================
-- Facilities
-- ===================================================================
CREATE TABLE facilities (
    facility_id         VARCHAR   NOT NULL PRIMARY KEY,
    facility_name       VARCHAR   NOT NULL,
    facility_type       VARCHAR   NOT NULL,
    ownership           VARCHAR   NOT NULL,
    ward_code           VARCHAR   NOT NULL,
    lga_code            VARCHAR   NOT NULL,
    sen_code            VARCHAR   NOT NULL,
    state_code          VARCHAR   NOT NULL,
    declared_ward_name  VARCHAR,
    declared_lga_name   VARCHAR,
    ward_assignment     VARCHAR   NOT NULL,
    longitude           DOUBLE    NOT NULL,
    latitude            DOUBLE    NOT NULL,
    coord_status        VARCHAR   NOT NULL,
    coord_parse_method  VARCHAR   NOT NULL,
    geom                GEOMETRY  NOT NULL,
    FOREIGN KEY (ward_code)     REFERENCES wards(ward_code),
    FOREIGN KEY (lga_code)      REFERENCES lgas(lga_code),
    FOREIGN KEY (sen_code)      REFERENCES senatorial_districts(sen_code),
    FOREIGN KEY (state_code)    REFERENCES states(state_code),
    FOREIGN KEY (facility_type) REFERENCES staffing_norms(facility_type),
    CHECK (longitude BETWEEN -180 AND 180),
    CHECK (latitude  BETWEEN  -90 AND  90),
    CHECK (ward_assignment IN ('within_polygon', 'snapped_to_nearest')),
    CHECK (ownership IN ('Public', 'Private', 'Faith-based'))
);

-- Staffing is a separate table on a 1:1 optional relation to facilities.
-- Modelling it this way is what makes "not assessed" representable: an
-- unassessed facility simply has no row here, which is different from having a
-- row of zeros. Folding these columns into `facilities` would force the two
-- states to be confused.
CREATE TABLE facility_staffing (
    facility_id         VARCHAR NOT NULL PRIMARY KEY,
    med_officers        INTEGER NOT NULL,
    nurses_midwives     INTEGER NOT NULL,
    chews               INTEGER NOT NULL,
    lab_scientists      INTEGER NOT NULL,
    pharm_techs         INTEGER NOT NULL,
    personnel_score     DOUBLE  NOT NULL,
    sen_rank            INTEGER,
    staffing_status     VARCHAR NOT NULL,
    staffing_shortfall  VARCHAR,
    binding_cadres      INTEGER NOT NULL,
    binding_cadres_met  INTEGER NOT NULL,
    FOREIGN KEY (facility_id) REFERENCES facilities(facility_id),
    CHECK (med_officers    >= 0),
    CHECK (nurses_midwives >= 0),
    CHECK (chews           >= 0),
    CHECK (lab_scientists  >= 0),
    CHECK (pharm_techs     >= 0),
    CHECK (staffing_status IN ('adequate', 'inadequate')),
    CHECK (binding_cadres_met <= binding_cadres)
);

CREATE TABLE roads (
    road_id       VARCHAR   NOT NULL PRIMARY KEY,
    road_class    VARCHAR   NOT NULL,
    surface       VARCHAR   NOT NULL,
    speed_kmh     DOUBLE    NOT NULL,
    length_m      DOUBLE    NOT NULL,
    traverse_min  DOUBLE    NOT NULL,
    geom          GEOMETRY  NOT NULL,
    CHECK (speed_kmh > 0),
    CHECK (length_m  > 0)
);

-- ===================================================================
-- Quarantine: records that are real but cannot satisfy the constraints above.
-- They are kept, visibly, rather than dropped. Dropping them would make the
-- database look cleaner than the data actually is.
-- ===================================================================
CREATE TABLE qa_facilities_unlocated (
    facility_id     VARCHAR NOT NULL PRIMARY KEY,
    facility_name   VARCHAR NOT NULL,
    facility_type   VARCHAR NOT NULL,
    ownership       VARCHAR,
    declared_ward_name VARCHAR,
    declared_lga_name  VARCHAR,
    raw_longitude   VARCHAR,
    raw_latitude    VARCHAR,
    coord_status    VARCHAR NOT NULL,
    exclusion_reason VARCHAR NOT NULL
);

CREATE TABLE qa_scores_unregistered (
    facility_id       VARCHAR NOT NULL PRIMARY KEY,
    facility_name     VARCHAR,
    personnel_score   DOUBLE,
    longitude         DOUBLE,
    latitude          DOUBLE,
    exclusion_reason  VARCHAR NOT NULL
);

-- Every name reconciliation decision made anywhere in the pipeline, loaded so
-- that the audit trail lives in the database beside the data it explains.
CREATE TABLE reconciliation_ledger (
    ledger_id       BIGINT  NOT NULL PRIMARY KEY,
    stage           VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    entity_id       VARCHAR,
    field           VARCHAR NOT NULL,
    raw_value       VARCHAR,
    resolved_value  VARCHAR,
    method          VARCHAR NOT NULL,
    confidence      DOUBLE  NOT NULL,
    outcome         VARCHAR NOT NULL,
    note            VARCHAR,
    CHECK (confidence BETWEEN 0 AND 1),
    CHECK (outcome IN ('resolved', 'resolved_review', 'unresolved', 'conflict'))
);
"""

SPATIAL_INDEXES = [
    ("idx_states_geom",     "states",     "geom"),
    ("idx_sen_geom",        "senatorial_districts", "geom"),
    ("idx_lgas_geom",       "lgas",       "geom"),
    ("idx_wards_geom",      "wards",      "geom"),
    ("idx_facilities_geom", "facilities", "geom"),
    ("idx_roads_geom",      "roads",      "geom"),
]

ATTRIBUTE_INDEXES = [
    ("idx_facilities_ward",  "facilities", "ward_code"),
    ("idx_facilities_lga",   "facilities", "lga_code"),
    ("idx_facilities_type",  "facilities", "facility_type"),
    ("idx_wards_lga",        "wards",      "lga_code"),
    ("idx_lgas_sen",         "lgas",       "sen_code"),
    ("idx_staffing_status",  "facility_staffing", "staffing_status"),
    ("idx_ledger_outcome",   "reconciliation_ledger", "outcome"),
]

GEOMETRY_REGISTRY = [
    ("states", "geom", 2, SRID_GEOGRAPHIC, "POLYGON"),
    ("senatorial_districts", "geom", 2, SRID_GEOGRAPHIC, "POLYGON"),
    ("lgas", "geom", 2, SRID_GEOGRAPHIC, "MULTIPOLYGON"),
    ("wards", "geom", 2, SRID_GEOGRAPHIC, "POLYGON"),
    ("facilities", "geom", 2, SRID_GEOGRAPHIC, "POINT"),
    ("roads", "geom", 2, SRID_GEOGRAPHIC, "LINESTRING"),
]


def _register(con, name: str, frame) -> None:
    """Expose a pandas frame to DuckDB under a temporary view name."""
    con.register(name, frame)


def load_geo(con, table: str, gdf: gpd.GeoDataFrame, cols: list[str]) -> int:
    """
    Insert a geo layer, converting geometry to WKT and re-declaring the SRID on
    the way in with ST_GeomFromText. The SRID is asserted at load time, not
    inferred later.
    """
    df = pd.DataFrame(gdf[cols].copy())
    df["wkt"] = gdf.geometry.to_wkt()
    _register(con, "_stage", df)
    collist = ", ".join(cols)
    con.execute(
        f"INSERT INTO {table} ({collist}, geom) "
        f"SELECT {collist}, ST_GeomFromText(wkt) FROM _stage"
    )
    con.unregister("_stage")
    n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    LOG.info("loaded %-24s %5d row(s)", table, n)
    return n


def load_plain(con, table: str, df: pd.DataFrame, cols: list[str]) -> int:
    _register(con, "_stage", df[cols].copy())
    collist = ", ".join(cols)
    con.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _stage")
    con.unregister("_stage")
    n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    LOG.info("loaded %-24s %5d row(s)", table, n)
    return n


# --------------------------------------------------------------------------
# E. Validation
# --------------------------------------------------------------------------

def validate(con) -> list[dict]:
    """
    Prove the database is governed, in three ways.

      1. Content checks -- row counts and the absence of orphans.
      2. Metadata checks -- every geometry column has a declared SRID.
      3. Negative tests -- deliberately attempt an insert that violates each
         class of constraint. A constraint that is declared but not enforced is
         worse than no constraint, because it is believed. These tests fail the
         run if a bad insert *succeeds*.
    """
    checks = []

    def add(name, passed, detail):
        checks.append({"check": name, "result": "PASS" if passed else "FAIL", "detail": detail})
        LOG.log(20 if passed else 40, "%-52s %s | %s", name, "PASS" if passed else "FAIL", detail)

    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in (
        "states", "senatorial_districts", "lgas", "wards", "facilities",
        "facility_staffing", "roads", "staffing_norms", "lga_senatorial_crosswalk",
        "qa_facilities_unlocated", "qa_scores_unregistered", "reconciliation_ledger")}
    add("All tables populated", all(v > 0 for v in counts.values()),
        ", ".join(f"{k}={v}" for k, v in counts.items()))

    # Referential integrity, asserted by query as well as by constraint.
    for child, ckey, parent, pkey in [
        ("facilities", "ward_code", "wards", "ward_code"),
        ("facilities", "lga_code", "lgas", "lga_code"),
        ("facilities", "sen_code", "senatorial_districts", "sen_code"),
        ("wards", "lga_code", "lgas", "lga_code"),
        ("lgas", "sen_code", "senatorial_districts", "sen_code"),
        ("senatorial_districts", "state_code", "states", "state_code"),
        ("facility_staffing", "facility_id", "facilities", "facility_id"),
        ("lga_senatorial_crosswalk", "lga_code", "lgas", "lga_code"),
    ]:
        orphans = con.execute(
            f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p "
            f"ON c.{ckey} = p.{pkey} WHERE p.{pkey} IS NULL").fetchone()[0]
        add(f"FK {child}.{ckey} -> {parent}.{pkey}", orphans == 0, f"{orphans} orphan(s)")

    # Every geometry column is registered with a declared SRID.
    reg = con.execute("SELECT f_table_name, f_geometry_column, srid FROM geometry_columns").fetchall()
    add("Every geometry column has a declared SRID", len(reg) == len(GEOMETRY_REGISTRY),
        f"{len(reg)} registered: " + ", ".join(f"{t}.{c}={s}" for t, c, s in reg))

    # Spatial correctness: facilities really do sit inside the ward they claim.
    bad = con.execute("""
        SELECT count(*) FROM facilities f JOIN wards w USING (ward_code)
        WHERE f.ward_assignment = 'within_polygon'
          AND NOT ST_Within(f.geom, w.geom)
    """).fetchone()[0]
    add("Facilities lie within their assigned ward", bad == 0, f"{bad} outside")

    # Geometry validity.
    for t in ("wards", "lgas", "senatorial_districts", "states"):
        inv = con.execute(f"SELECT count(*) FROM {t} WHERE NOT ST_IsValid(geom)").fetchone()[0]
        add(f"{t}.geom is OGC-valid", inv == 0, f"{inv} invalid")

    # Spatial indexes exist.
    idx = con.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE index_name LIKE 'idx_%geom'").fetchall()
    add("Spatial (R-tree) indexes present", len(idx) == len(SPATIAL_INDEXES),
        f"{len(idx)} of {len(SPATIAL_INDEXES)}: {[i[0] for i in idx]}")

    # Population conservation.
    tot = con.execute("SELECT sum(total_population) FROM wards").fetchone()[0]
    add("Ward population conserved through load", tot == 22936947, f"{tot:,}")

    # -------- negative tests --------
    negatives = [
        ("PRIMARY KEY rejects a duplicate ward",
         "INSERT INTO wards SELECT * FROM wards LIMIT 1"),
        ("FOREIGN KEY rejects a facility in a non-existent ward",
         "INSERT INTO facilities (facility_id, facility_name, facility_type, ownership, "
         "ward_code, lga_code, sen_code, state_code, ward_assignment, longitude, latitude, "
         "coord_status, coord_parse_method, geom) VALUES ('ZZTEST', 'Test', "
         "'Health Post', 'Public', 'W9999', 'LGA001', 'SD01', 'ST01', 'within_polygon', "
         "8.0, 9.0, 'accepted', 'decimal', ST_Point(8.0, 9.0))"),
        ("CHECK rejects under-5 population above total population",
         "INSERT INTO wards VALUES ('WZZZZ', 'Test', 'LGA001', 'SD11', 'ST04', "
         "10, 999, 'test', ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))'))"),
        # Deliberately targets a facility that has NO staffing row, so the insert
        # reaches the CHECK instead of being stopped earlier by the primary key.
        ("CHECK rejects an unknown staffing status",
         "INSERT INTO facility_staffing VALUES "
         "((SELECT f.facility_id FROM facilities f "
         "  LEFT JOIN facility_staffing s USING (facility_id) "
         "  WHERE s.facility_id IS NULL LIMIT 1), 0,0,0,0,0, 1.0, 1, 'maybe', '', 1, 0)"),
        ("FOREIGN KEY rejects staffing for a non-existent facility",
         "INSERT INTO facility_staffing VALUES "
         "('NOSUCHFAC', 0,0,0,0,0, 1.0, 1, 'adequate', '', 1, 1)"),
        ("NOT NULL rejects a facility with no geometry",
         "INSERT INTO facilities (facility_id, facility_name, facility_type, ownership, "
         "ward_code, lga_code, sen_code, state_code, ward_assignment, longitude, latitude, "
         "coord_status, coord_parse_method) VALUES ('ZZTEST2', 'Test', 'Health Post', "
         "'Public', (SELECT ward_code FROM wards LIMIT 1), (SELECT lga_code FROM lgas LIMIT 1), "
         "(SELECT sen_code FROM senatorial_districts LIMIT 1), "
         "(SELECT state_code FROM states LIMIT 1), 'within_polygon', 8.0, 9.0, "
         "'accepted', 'decimal')"),
    ]
    for name, sql in negatives:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(sql)
            con.execute("ROLLBACK")
            add(name, False, "the constraint did NOT fire — bad row was accepted")
        except Exception as exc:                      # noqa: BLE001 - the failure IS the pass
            con.execute("ROLLBACK")
            add(name, True, f"rejected: {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")

    return checks


POSTGIS_HEADER = f"""-- ===================================================================
-- PostGIS-equivalent schema for the facility access database
-- Generated by 03_spatial_database_pipeline.py
--
-- The delivered database is DuckDB + spatial (PostGIS is not installable in
-- the assessment environment). This file is the same schema expressed for a
-- ministry PostGIS server: identical primary keys, identical foreign keys,
-- identical CHECK constraints, typed and SRID-constrained geometry columns,
-- and GiST indexes in place of DuckDB's R-tree indexes.
--
-- Storage CRS  : EPSG:4326  (WGS 84 geographic)
-- Analysis CRS : {SRID_PROJECTED}  {CRS_PROJECTED_LABEL}
-- ===================================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS facility_access;
SET search_path TO facility_access, public;

-- Africa Albers Equal Area Conic is not in the stock spatial_ref_sys table.
INSERT INTO public.spatial_ref_sys (srid, auth_name, auth_srid, proj4text, srtext)
VALUES ({SRID_PROJECTED}, 'ESRI', {SRID_PROJECTED},
        '{CRS_PROJECTED}',
        'PROJCS["Africa_Albers_Equal_Area_Conic",GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Albers"],PARAMETER["False_Easting",0.0],'
        'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",25.0],'
        'PARAMETER["Standard_Parallel_1",20.0],PARAMETER["Standard_Parallel_2",-23.0],'
        'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')
ON CONFLICT (srid) DO NOTHING;
"""

POSTGIS_BODY = """
CREATE TABLE states (
    state_code  text            NOT NULL PRIMARY KEY,
    state_name  text            NOT NULL UNIQUE,
    geom        geometry(Polygon, 4326) NOT NULL
);

CREATE TABLE senatorial_districts (
    sen_code      text NOT NULL PRIMARY KEY,
    sen_district  text NOT NULL UNIQUE,
    state_code    text NOT NULL REFERENCES states(state_code) ON UPDATE CASCADE,
    geom          geometry(Polygon, 4326) NOT NULL
);

CREATE TABLE lgas (
    lga_code    text NOT NULL PRIMARY KEY,
    lga_name    text NOT NULL,
    sen_code    text NOT NULL REFERENCES senatorial_districts(sen_code) ON UPDATE CASCADE,
    state_code  text NOT NULL REFERENCES states(state_code) ON UPDATE CASCADE,
    geom        geometry(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE wards (
    ward_code          text   NOT NULL PRIMARY KEY,
    ward_name          text   NOT NULL,
    lga_code           text   NOT NULL REFERENCES lgas(lga_code) ON UPDATE CASCADE,
    sen_code           text   NOT NULL REFERENCES senatorial_districts(sen_code) ON UPDATE CASCADE,
    state_code         text   NOT NULL REFERENCES states(state_code) ON UPDATE CASCADE,
    total_population   bigint NOT NULL CHECK (total_population >= 0),
    population_under5  bigint NOT NULL CHECK (population_under5 >= 0),
    population_source  text   NOT NULL,
    geom               geometry(Polygon, 4326) NOT NULL,
    CONSTRAINT ck_wards_under5_le_total CHECK (population_under5 <= total_population)
);

CREATE TABLE staffing_norms (
    facility_type            text    NOT NULL PRIMARY KEY,
    min_medical_officers     integer NOT NULL CHECK (min_medical_officers     >= 0),
    min_nurses_midwives      integer NOT NULL CHECK (min_nurses_midwives      >= 0),
    min_chews                integer NOT NULL CHECK (min_chews                >= 0),
    min_lab_scientists       integer NOT NULL CHECK (min_lab_scientists       >= 0),
    min_pharmacy_technicians integer NOT NULL CHECK (min_pharmacy_technicians >= 0),
    adequacy_rule            text    NOT NULL
);

CREATE TABLE lga_senatorial_crosswalk (
    lga_code            text NOT NULL PRIMARY KEY REFERENCES lgas(lga_code) ON UPDATE CASCADE,
    lga_name            text NOT NULL,
    lga_name_source     text,
    sen_code            text NOT NULL REFERENCES senatorial_districts(sen_code) ON UPDATE CASCADE,
    sen_district        text NOT NULL,
    state_code          text NOT NULL REFERENCES states(state_code) ON UPDATE CASCADE,
    state_name          text NOT NULL,
    ward_count_declared integer,
    ward_count_observed integer,
    remarks             text,
    match_method        text   NOT NULL,
    match_confidence    double precision NOT NULL CHECK (match_confidence BETWEEN 0 AND 1),
    match_outcome       text   NOT NULL
        CHECK (match_outcome IN ('resolved','resolved_review','unresolved','conflict')),
    source_sheet_row    integer
);

CREATE TABLE facilities (
    facility_id        text   NOT NULL PRIMARY KEY,
    facility_name      text   NOT NULL,
    facility_type      text   NOT NULL REFERENCES staffing_norms(facility_type) ON UPDATE CASCADE,
    ownership          text   NOT NULL CHECK (ownership IN ('Public','Private','Faith-based')),
    ward_code          text   NOT NULL REFERENCES wards(ward_code) ON UPDATE CASCADE,
    lga_code           text   NOT NULL REFERENCES lgas(lga_code) ON UPDATE CASCADE,
    sen_code           text   NOT NULL REFERENCES senatorial_districts(sen_code) ON UPDATE CASCADE,
    state_code         text   NOT NULL REFERENCES states(state_code) ON UPDATE CASCADE,
    declared_ward_name text,
    declared_lga_name  text,
    ward_assignment    text   NOT NULL
        CHECK (ward_assignment IN ('within_polygon','snapped_to_nearest')),
    longitude          double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    latitude           double precision NOT NULL CHECK (latitude  BETWEEN  -90 AND  90),
    coord_status       text   NOT NULL,
    coord_parse_method text   NOT NULL,
    geom               geometry(Point, 4326) NOT NULL
);

-- A separate 1:1-optional table, so that "never assessed" is representable as
-- the absence of a row rather than as a row of zeros.
CREATE TABLE facility_staffing (
    facility_id        text NOT NULL PRIMARY KEY
                       REFERENCES facilities(facility_id) ON DELETE CASCADE ON UPDATE CASCADE,
    med_officers       integer NOT NULL CHECK (med_officers    >= 0),
    nurses_midwives    integer NOT NULL CHECK (nurses_midwives >= 0),
    chews              integer NOT NULL CHECK (chews           >= 0),
    lab_scientists     integer NOT NULL CHECK (lab_scientists  >= 0),
    pharm_techs        integer NOT NULL CHECK (pharm_techs     >= 0),
    personnel_score    double precision NOT NULL,
    sen_rank           integer,
    staffing_status    text NOT NULL CHECK (staffing_status IN ('adequate','inadequate')),
    staffing_shortfall text,
    binding_cadres     integer NOT NULL,
    binding_cadres_met integer NOT NULL,
    CONSTRAINT ck_staffing_met_le_binding CHECK (binding_cadres_met <= binding_cadres)
);

CREATE TABLE roads (
    road_id      text NOT NULL PRIMARY KEY,
    road_class   text NOT NULL,
    surface      text NOT NULL,
    speed_kmh    double precision NOT NULL CHECK (speed_kmh > 0),
    length_m     double precision NOT NULL CHECK (length_m  > 0),
    traverse_min double precision NOT NULL,
    geom         geometry(LineString, 4326) NOT NULL
);

CREATE TABLE ward_access (
    ward_code                    text NOT NULL PRIMARY KEY
                                 REFERENCES wards(ward_code) ON UPDATE CASCADE,
    facilities_total             integer NOT NULL,
    facilities_adequate          integer NOT NULL,
    facilities_inadequate        integer NOT NULL,
    facilities_unknown           integer NOT NULL,
    travel_min_to_adequate       double precision,
    travel_min_to_any            double precision,
    covered_60min                boolean NOT NULL,
    population_covered           double precision NOT NULL,
    population_uncovered         double precision NOT NULL,
    access_deficit               double precision NOT NULL,
    gap_type                     text NOT NULL,
    priority_rank                integer
);

CREATE TABLE qa_facilities_unlocated (
    facility_id      text NOT NULL PRIMARY KEY,
    facility_name    text NOT NULL,
    facility_type    text NOT NULL,
    ownership        text,
    declared_ward_name text,
    declared_lga_name  text,
    raw_longitude    text,
    raw_latitude     text,
    coord_status     text NOT NULL,
    exclusion_reason text NOT NULL
);

CREATE TABLE qa_scores_unregistered (
    facility_id      text NOT NULL PRIMARY KEY,
    facility_name    text,
    personnel_score  double precision,
    longitude        double precision,
    latitude         double precision,
    exclusion_reason text NOT NULL
);

CREATE TABLE reconciliation_ledger (
    ledger_id      bigserial PRIMARY KEY,
    stage          text NOT NULL,
    source         text NOT NULL,
    entity_id      text,
    field          text NOT NULL,
    raw_value      text,
    resolved_value text,
    method         text NOT NULL,
    confidence     double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    outcome        text NOT NULL
        CHECK (outcome IN ('resolved','resolved_review','unresolved','conflict')),
    note           text
);

-- ------------------------------------------------------------------
-- Spatial indexes (GiST) and attribute indexes
-- ------------------------------------------------------------------
CREATE INDEX idx_states_geom     ON states     USING GIST (geom);
CREATE INDEX idx_sen_geom        ON senatorial_districts USING GIST (geom);
CREATE INDEX idx_lgas_geom       ON lgas       USING GIST (geom);
CREATE INDEX idx_wards_geom      ON wards      USING GIST (geom);
CREATE INDEX idx_facilities_geom ON facilities USING GIST (geom);
CREATE INDEX idx_roads_geom      ON roads      USING GIST (geom);

CREATE INDEX idx_facilities_ward ON facilities (ward_code);
CREATE INDEX idx_facilities_lga  ON facilities (lga_code);
CREATE INDEX idx_facilities_type ON facilities (facility_type);
CREATE INDEX idx_wards_lga       ON wards      (lga_code);
CREATE INDEX idx_lgas_sen        ON lgas       (sen_code);
CREATE INDEX idx_staffing_status ON facility_staffing (staffing_status);
CREATE INDEX idx_ledger_outcome  ON reconciliation_ledger (outcome);

-- ------------------------------------------------------------------
-- Governance views
-- ------------------------------------------------------------------

-- Where the Surveyor General's crosswalk and the boundary layer disagree.
CREATE OR REPLACE VIEW v_crosswalk_divergence AS
SELECT x.lga_code, x.lga_name,
       x.sen_district  AS crosswalk_assignment,
       s.sen_district  AS boundary_layer_assignment,
       x.remarks
FROM   lga_senatorial_crosswalk x
JOIN   lgas l                 ON l.lga_code = x.lga_code
JOIN   senatorial_districts s ON s.sen_code = l.sen_code
WHERE  x.sen_code <> l.sen_code;

-- Ward-level supply, keeping "not assessed" distinct from "inadequate".
CREATE OR REPLACE VIEW v_ward_supply AS
SELECT w.ward_code, w.ward_name, w.lga_code, w.sen_code, w.total_population,
       count(f.facility_id)                                       AS facilities_total,
       count(*) FILTER (WHERE s.staffing_status = 'adequate')     AS facilities_adequate,
       count(*) FILTER (WHERE s.staffing_status = 'inadequate')   AS facilities_inadequate,
       count(*) FILTER (WHERE f.facility_id IS NOT NULL
                          AND s.facility_id IS NULL)              AS facilities_unassessed
FROM   wards w
LEFT   JOIN facilities f        ON f.ward_code   = w.ward_code
LEFT   JOIN facility_staffing s ON s.facility_id = f.facility_id
GROUP  BY w.ward_code, w.ward_name, w.lga_code, w.sen_code, w.total_population;
"""


def write_postgis_ddl() -> None:
    ART["postgis_ddl"].write_text(POSTGIS_HEADER + POSTGIS_BODY, encoding="utf-8")
    LOG.info("Wrote PostGIS-equivalent DDL: %s", ART["postgis_ddl"].name)


def write_validation_report(con, checks) -> None:
    tables = con.execute("""
        SELECT table_name, estimated_size FROM duckdb_tables()
        WHERE schema_name = 'main' ORDER BY table_name""").fetchall()
    idx = con.execute("""
        SELECT index_name, table_name, is_unique FROM duckdb_indexes()
        ORDER BY table_name, index_name""").fetchall()
    reg = con.execute("SELECT * FROM geometry_columns ORDER BY f_table_name").fetchall()
    srs = con.execute("SELECT srid, auth_name, role FROM spatial_ref_sys ORDER BY srid").fetchall()

    lines = [
        "# Stage 3 — Governed Spatial Database",
        "",
        "## 1. Engine",
        "",
        "PostGIS is the production target and is what a ministry should run. It cannot be",
        "installed in the assessment environment (no PostgreSQL server, no `psql`, no",
        "administrative rights), so the delivered database is **DuckDB with the `spatial`",
        "extension** — a real engine with a native `GEOMETRY` type, the GEOS function set,",
        "enforced `PRIMARY KEY` / `FOREIGN KEY` / `CHECK` / `NOT NULL` constraints, and",
        "R-tree spatial indexes.",
        "",
        "The identical schema is emitted for PostGIS in `database/postgis_schema.sql`:",
        "same keys, same foreign keys, same checks, SRID-constrained typed geometry",
        "columns and GiST indexes. Nothing about the design depends on the substitution.",
        "",
        f"Database file: `database/{ART['database'].name}`",
        "",
        "## 2. Declared coordinate reference systems",
        "",
        "DuckDB has no `spatial_ref_sys` or `geometry_columns` of its own, so both are",
        "created and populated explicitly. Every geometry column's SRID is therefore a",
        "queryable declaration rather than an assumption carried in someone's head.",
        "",
        "| SRID | Authority | Role |",
        "|---:|---|---|",
    ] + [f"| {s[0]} | {s[1]} | {s[2]} |" for s in srs] + [
        "",
        "| Table | Geometry column | Dim | SRID | Declared type |",
        "|---|---|---:|---:|---|",
    ] + [f"| `{r[0]}` | `{r[1]}` | {r[2]} | {r[3]} | {r[4]} |" for r in reg] + [
        "",
        f"Storage is EPSG:4326 throughout. All measurement is performed in {SRID_PROJECTED},",
        f"{CRS_PROJECTED_LABEL}, which is equal-area and keeps linear distortion below about",
        "1% across a study area of this size. No distance is ever computed in degrees.",
        "",
        "## 3. Tables",
        "",
        "| Table | Rows |",
        "|---|---:|",
    ] + [f"| `{t[0]}` | {t[1]:,} |" for t in tables] + [
        "",
        "Two modelling decisions carry analytical weight:",
        "",
        "- **`facility_staffing` is a separate table** on an optional 1:1 relation to",
        "  `facilities`. A facility that was never assessed simply has no row. Folding the",
        "  cadre counts into `facilities` would force *unassessed* and *zero staff* into the",
        "  same representation, and the whole of task 4 depends on telling them apart.",
        "- **The crosswalk is loaded as its own table**, not merged into `lgas`, so the",
        "  Surveyor General's assertion and the boundary layer's assertion both survive and",
        "  can be compared. `v_crosswalk_divergence` reports exactly where they differ.",
        "",
        "## 4. Indexes",
        "",
        "| Index | Table | Unique |",
        "|---|---|---|",
    ] + [f"| `{i[0]}` | `{i[1]}` | {'yes' if i[2] else 'no'} |" for i in idx] + [
        "",
        "The six `idx_*_geom` indexes are R-tree indexes over the geometry columns; the",
        "remainder support the foreign-key and filter columns used by the analysis.",
        "",
        "## 5. Validation",
        "",
        "Content, metadata and — crucially — *negative* tests. A declared constraint that",
        "is not enforced is more dangerous than no constraint, because it is believed, so",
        "each class of constraint is attacked with an insert that must fail. These rows",
        "pass when the database **rejects** the bad insert.",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ] + [f"| {c['check']} | **{c['result']}** | {c['detail']} |" for c in checks] + [""]

    ART["db_validation"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote %s", ART["db_validation"].name)


def main():
    banner(LOG, "STAGE 03 — GOVERNED SPATIAL DATABASE")

    src = ART["conformed_gpkg"]
    wards = gpd.read_file(src, layer="wards")
    lgas = gpd.read_file(src, layer="lgas")
    sens = gpd.read_file(src, layer="senatorial_districts")
    states = gpd.read_file(src, layer="states")
    roads = gpd.read_file(src, layer="roads")
    facs = gpd.read_file(src, layer="facilities")

    norms = pd.read_csv(src.with_name("staffing_norms.csv"))
    unlocated = pd.read_csv(src.with_name("facilities_unlocated.csv"), dtype=str)
    orphans = pd.read_csv(src.with_name("scores_unregistered.csv"))
    crosswalk = pd.read_csv(ART["crosswalk_table"], dtype={"lga_code": str})

    ledger = pd.concat([
        pd.read_csv(ART["crosswalk_ledger"]).assign(stage="01_crosswalk"),
        pd.read_csv(ART["coord_ledger"]).assign(stage="02_coordinates"),
        pd.read_csv(ART["join_ledger"]).assign(stage="02_join"),
    ], ignore_index=True)
    ledger.insert(0, "ledger_id", range(1, len(ledger) + 1))
    for c in ("entity_id", "raw_value", "resolved_value", "note"):
        ledger[c] = ledger[c].fillna("").astype(str)

    if ART["database"].exists():
        ART["database"].unlink()
    con = duckdb.connect(str(ART["database"]))
    con.execute("INSTALL spatial; LOAD spatial;")
    LOG.info("DuckDB %s with spatial extension loaded",
             con.execute("SELECT version()").fetchone()[0])

    con.execute(DDL)
    LOG.info("Schema created: %d table(s)",
             con.execute("SELECT count(*) FROM duckdb_tables()").fetchone()[0])

    # ---- B. CRS registry -----------------------------------------------
    con.execute("""
        INSERT INTO spatial_ref_sys VALUES
        (?, 'EPSG', ?, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],
         PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]',
         '+proj=longlat +datum=WGS84 +no_defs', 'storage CRS for all geometry columns'),
        (?, 'ESRI', ?, 'PROJCS["Africa_Albers_Equal_Area_Conic", ...]', ?,
         'analysis CRS: all distance, length and area measurement')
    """, [SRID_GEOGRAPHIC, SRID_GEOGRAPHIC, SRID_PROJECTED, SRID_PROJECTED, CRS_PROJECTED])
    con.executemany(
        "INSERT INTO geometry_columns VALUES (?, ?, ?, ?, ?)",
        [(t, c, d, s, g) for t, c, d, s, g in GEOMETRY_REGISTRY])
    LOG.info("CRS registry declared: storage EPSG:%d, analysis %d (%s)",
             SRID_GEOGRAPHIC, SRID_PROJECTED, CRS_PROJECTED_LABEL)

    # ---- C. Load, parents before children ------------------------------
    load_geo(con, "states", states, ["state_code", "state_name"])
    load_geo(con, "senatorial_districts", sens, ["sen_code", "sen_district", "state_code"])
    load_geo(con, "lgas", lgas, ["lga_code", "lga_name", "sen_code", "state_code"])
    load_geo(con, "wards", wards, ["ward_code", "ward_name", "lga_code", "sen_code",
                                   "state_code", "total_population", "population_under5",
                                   "population_source"])
    load_plain(con, "staffing_norms", norms, list(norms.columns))
    load_plain(con, "lga_senatorial_crosswalk", crosswalk, [
        "lga_code", "lga_name", "lga_name_source", "sen_code", "sen_district", "state_code",
        "state_name", "ward_count_declared", "ward_count_observed", "remarks",
        "match_method", "match_confidence", "match_outcome", "source_sheet_row"])

    load_geo(con, "facilities", facs, [
        "facility_id", "facility_name", "facility_type", "ownership", "ward_code", "lga_code",
        "sen_code", "state_code", "declared_ward_name", "declared_lga_name", "ward_assignment",
        "longitude", "latitude", "coord_status", "coord_parse_method"])

    staffed = facs[facs.staffing_status.isin(["adequate", "inadequate"])].copy()
    staffed["staffing_shortfall"] = staffed.staffing_shortfall.fillna("")
    load_plain(con, "facility_staffing", staffed, [
        "facility_id", "med_officers", "nurses_midwives", "chews", "lab_scientists",
        "pharm_techs", "personnel_score", "sen_rank", "staffing_status",
        "staffing_shortfall", "binding_cadres", "binding_cadres_met"])

    load_geo(con, "roads", roads, ["road_id", "road_class", "surface", "speed_kmh",
                                   "length_m", "traverse_min"])

    unlocated = unlocated.assign(exclusion_reason=lambda d: d.coord_status.map({
        "no_geometry": "coordinate missing or unparseable in the source register",
        "quarantined_out_of_area": "parsed coordinate falls outside the study area and no "
                                   "repair placed it inside",
    }).fillna("no usable location"))
    for c in ("raw_longitude", "raw_latitude"):
        if c not in unlocated.columns:
            unlocated[c] = None
    load_plain(con, "qa_facilities_unlocated", unlocated, [
        "facility_id", "facility_name", "facility_type", "ownership", "declared_ward_name",
        "declared_lga_name", "raw_longitude", "raw_latitude", "coord_status", "exclusion_reason"])

    orphans = orphans.rename(columns={"score_facility_name": "facility_name",
                                      "score_longitude": "longitude",
                                      "score_latitude": "latitude"})
    orphans["exclusion_reason"] = ("scored facility absent from the facility register; "
                                   "no facility type, ward or ownership, so it cannot be "
                                   "tested against a type-specific staffing norm")
    load_plain(con, "qa_scores_unregistered", orphans,
               ["facility_id", "facility_name", "personnel_score", "longitude", "latitude",
                "exclusion_reason"])

    load_plain(con, "reconciliation_ledger", ledger, [
        "ledger_id", "stage", "source", "entity_id", "field", "raw_value", "resolved_value",
        "method", "confidence", "outcome", "note"])

    # ---- D. Indexes ----------------------------------------------------
    for name, table, col in SPATIAL_INDEXES:
        con.execute(f"CREATE INDEX {name} ON {table} USING RTREE ({col})")
    for name, table, col in ATTRIBUTE_INDEXES:
        con.execute(f"CREATE INDEX {name} ON {table} ({col})")
    LOG.info("Created %d R-tree spatial index(es) and %d attribute index(es)",
             len(SPATIAL_INDEXES), len(ATTRIBUTE_INDEXES))

    # ---- Governance views ----------------------------------------------
    con.execute("""
        CREATE OR REPLACE VIEW v_crosswalk_divergence AS
        SELECT x.lga_code, x.lga_name,
               x.sen_district AS crosswalk_assignment,
               s.sen_district AS boundary_layer_assignment,
               x.remarks
        FROM lga_senatorial_crosswalk x
        JOIN lgas l ON l.lga_code = x.lga_code
        JOIN senatorial_districts s ON s.sen_code = l.sen_code
        WHERE x.sen_code <> l.sen_code
    """)
    con.execute("""
        CREATE OR REPLACE VIEW v_ward_supply AS
        SELECT w.ward_code, w.ward_name, w.lga_code, w.sen_code, w.total_population,
               count(f.facility_id) AS facilities_total,
               count(*) FILTER (WHERE s.staffing_status = 'adequate')   AS facilities_adequate,
               count(*) FILTER (WHERE s.staffing_status = 'inadequate') AS facilities_inadequate,
               count(*) FILTER (WHERE f.facility_id IS NOT NULL
                                  AND s.facility_id IS NULL)            AS facilities_unassessed
        FROM wards w
        LEFT JOIN facilities f        ON f.ward_code = w.ward_code
        LEFT JOIN facility_staffing s ON s.facility_id = f.facility_id
        GROUP BY 1,2,3,4,5
    """)

    div = con.execute("SELECT * FROM v_crosswalk_divergence").fetchdf()
    if len(div):
        LOG.warning("Crosswalk diverges from the boundary layer for %d LGA(s):\n%s",
                    len(div), div.to_string(index=False))

    checks = validate(con)
    write_postgis_ddl()
    write_validation_report(con, checks)
    con.close()

    if any(c["result"] == "FAIL" for c in checks):
        LOG.error("Database validation FAILED — see %s", ART["db_validation"].name)
    banner(LOG, "STAGE 03 COMPLETE")


if __name__ == "__main__":
    main()
