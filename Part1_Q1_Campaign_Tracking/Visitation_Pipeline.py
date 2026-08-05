import os
import sys
import time
import duckdb
from tqdm.auto import tqdm

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def run_visitation_analysis():
    # ==========================================
    # 1. CONFIGURATION
    # ==========================================
    # Add .replace('\\', '/') to safely parse Windows paths in SQL
    POLYGON_RAW = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\shp\Set_extent.shp".replace('\\', '/')
    
    # Point this to the FOLDER containing your GTS CSV files
    # .rstrip('/') ensures we don't accidentally double up on slashes later
    GTS_TRACKS_FOLDER = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\tracks".replace('\\', '/').rstrip('/')
    
    # Update these to match the exact column names for coordinates in your track CSVs
    TRACK_LON_FIELD = "longitude" 
    TRACK_LAT_FIELD = "latitude"  
    
    ETALLY_RAW = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\etally_daily.csv".replace('\\', '/')
    
    OUTPUT_CSV = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\Output\settlement_visitation.csv".replace('\\', '/')
    OUTPUT_SHP = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\Output\settlement_visitation.shp".replace('\\', '/')

    # Authoritative state outline, used for the coordinate plausibility rule.
    # Taken from the supplied boundary file rather than a hand-typed bounding box.
    BOUNDARIES_GPKG = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\boundaries.gpkg".replace('\\', '/')

    # Audit trail for the track-record QA gate
    OUTPUT_QA_CSV = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\Output\gts_qa_summary.csv".replace('\\', '/')
    OUTPUT_QA_RULES_CSV = r"C:\Users\adedo.lukmon\Downloads\Technical_asssessment\eHA_Assessment_Data_Pack_v4_CANDIDATE\Part1_Q1_Campaign_Tracking\Output\gts_qa_rule_report.csv".replace('\\', '/')

    # ==========================================
    # 1b. QA THRESHOLDS
    # ==========================================
    # Every value here is defended in the accompanying QA documentation. They are
    # named constants rather than literals buried in SQL so that a reviewer can
    # change one and re-run without reading the query.
    DWELL_SPEED_KMH       = 1      # "effectively stationary" = admissible evidence
    ACCURACY_M            = 10     # 22% of the smallest settlement extent radius (45 m)
    CAMPAIGN_START        = '2026-03-09'
    CAMPAIGN_END          = '2026-03-13'
    DUTY_START_HOUR       = 7      # stationary assembly spike observed at 07:00
    DUTY_END_HOUR         = 19     # sunset ~18:45 local; after dark visits not credible
    
    # Anchored to the script, not the shell's working directory, so the store
    # lands in the same place no matter where the pipeline is invoked from.
    DB_FILE = os.path.join(SCRIPT_DIR, "visitation_store.duckdb")
    
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)

    # ==========================================
    # 2. INITIALIZE DUCKDB & SPATIAL EXTENSION
    # ==========================================
    print("Initializing DuckDB and loading spatial extension...")
    con = duckdb.connect(DB_FILE)
    con.execute("INSTALL spatial;")
    con.execute("LOAD spatial;")

    # ==========================================
    # 3. HELPER FUNCTIONS FOR STEPS
    # ==========================================
    def export_shapefile():
        if os.path.exists(OUTPUT_SHP):
            for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
                target_file = OUTPUT_SHP.replace('.shp', ext)
                if os.path.exists(target_file):
                    os.remove(target_file)
                    
        # Reattach the CRS the ingest cast stripped, otherwise GDAL writes no .prj
        # and the output lands in ArcGIS with an unknown coordinate system.
        con.execute(f"""
            COPY (
                SELECT * REPLACE (geom::GEOMETRY('EPSG:4326') AS geom)
                FROM visitation_results
            ) TO '{OUTPUT_SHP}'
            WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile');
        """)

    # ------------------------------------------
    # Track record validation rule set
    # ------------------------------------------
    # Six removal rules. A point failing any of them is not admissible evidence
    # of a visit. Rule IDs are left unrenumbered so that figures already quoted
    # against R1 and R3-R7 stay traceable.
    #
    # Every point is labelled rather than filtered away, so rejected volume is
    # countable. NULL is never silently equivalent to failure: a point missing
    # speed or accuracy cannot be evaluated at all and is removed under its own
    # rule (R1) rather than vanishing through a NULL comparison.
    validation_query = f"""
    CREATE OR REPLACE TABLE gts_tracks_qa AS
    SELECT s.*,
        (s.speed_kmh IS NULL OR s.accuracy_m IS NULL)          AS r1_missing_qc_field,
        (NOT ST_Within(ST_Point(s.longitude, s.latitude),
                       (SELECT geom FROM state_boundary)))      AS r3_outside_state,
        (CAST(s.timestamp AS DATE) NOT BETWEEN DATE '{CAMPAIGN_START}'
                                           AND DATE '{CAMPAIGN_END}') AS r4_outside_campaign,
        (extract(hour FROM s.timestamp) < {DUTY_START_HOUR}
         OR extract(hour FROM s.timestamp) >= {DUTY_END_HOUR})  AS r5_outside_duty_hours,
        (s.accuracy_m > {ACCURACY_M})                           AS r6_accuracy_too_coarse,
        (s.speed_kmh > {DWELL_SPEED_KMH})                       AS r7_not_stationary
    FROM gts_tracks s;

    -- A point is admissible only if it breaks no removal rule.
    CREATE OR REPLACE TABLE gts_tracks_valid AS
    SELECT * FROM gts_tracks_qa
    WHERE NOT (r1_missing_qc_field OR r3_outside_state
            OR r4_outside_campaign OR r5_outside_duty_hours
            OR r6_accuracy_too_coarse OR r7_not_stationary);

    -- Per-rule report. n_violating counts every point breaking the rule,
    -- independent of other rules, so figures are directly quotable and do not
    -- depend on evaluation order.
    CREATE OR REPLACE TABLE qa_rule_report AS
    SELECT * FROM (VALUES
        ('R1', 'Missing QC field',        'REMOVE', 'speed_kmh or accuracy_m IS NULL',
         (SELECT count(*) FROM gts_tracks_qa WHERE r1_missing_qc_field)),
        ('R3', 'Outside state boundary',  'REMOVE', 'point not within state polygon',
         (SELECT count(*) FROM gts_tracks_qa WHERE r3_outside_state)),
        ('R4', 'Outside campaign window', 'REMOVE', 'date outside {CAMPAIGN_START}..{CAMPAIGN_END}',
         (SELECT count(*) FROM gts_tracks_qa WHERE r4_outside_campaign)),
        ('R5', 'Outside duty hours',      'REMOVE', 'hour outside {DUTY_START_HOUR}:00-{DUTY_END_HOUR}:00',
         (SELECT count(*) FROM gts_tracks_qa WHERE r5_outside_duty_hours)),
        ('R6', 'Accuracy too coarse',     'REMOVE', 'accuracy_m > {ACCURACY_M} m',
         (SELECT count(*) FROM gts_tracks_qa WHERE r6_accuracy_too_coarse)),
        ('R7', 'Not stationary',          'REMOVE', 'speed_kmh > {DWELL_SPEED_KMH} km/h',
         (SELECT count(*) FROM gts_tracks_qa WHERE r7_not_stationary))
    ) AS r(rule_id, rule_name, action, threshold, n_violating);

    -- Outcome partition: every ingested point lands in exactly one bucket.
    CREATE OR REPLACE TABLE qa_summary AS
    SELECT qa_flag, count(*) AS n_points,
           round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct_of_ingested
    FROM (
        SELECT CASE
            WHEN r1_missing_qc_field   THEN 'REMOVED_R1_missing_qc_field'
            WHEN r3_outside_state      THEN 'REMOVED_R3_outside_state'
            WHEN r4_outside_campaign   THEN 'REMOVED_R4_outside_campaign'
            WHEN r5_outside_duty_hours THEN 'REMOVED_R5_outside_duty_hours'
            WHEN r6_accuracy_too_coarse THEN 'REMOVED_R6_accuracy_too_coarse'
            WHEN r7_not_stationary     THEN 'REMOVED_R7_not_stationary'
            ELSE 'PASS'
        END AS qa_flag
        FROM gts_tracks_qa
    )
    GROUP BY qa_flag
    ORDER BY n_points DESC;
    """

    # Main analytical query
    analysis_query = """
    CREATE OR REPLACE TABLE visitation_results AS
    SELECT 
        poly.* EXCLUDE (_uid),
        
        CASE 
            WHEN gts_match.poly_uid IS NOT NULL THEN 'V'
            WHEN etally_match.poly_uid IS NOT NULL THEN 'V'
            ELSE 'NV'
        END AS Vis_Status,
        
        CASE 
            WHEN gts_match.poly_uid IS NOT NULL THEN 'GTS'
            WHEN etally_match.poly_uid IS NOT NULL THEN 'eTally'
            ELSE 'Non'
        END AS Vis_Source
        
    FROM settlements poly
    
    -- 1. GTS Spatial Join. No filtering here: gts_tracks_valid already contains
    --    only the records that passed the QA gate.
    LEFT JOIN (
        SELECT DISTINCT p._uid AS poly_uid
        FROM settlements p
        JOIN gts_tracks_valid t ON ST_Intersects(p.geom, t.geom)
    ) gts_match ON poly._uid = gts_match.poly_uid
    
    -- 2. eTally Attribute Join (Tabular relational join)
    LEFT JOIN (
        SELECT DISTINCT p._uid AS poly_uid
        FROM settlements p
        JOIN etally_records t ON p.settlement = t.settlement_id
    ) etally_match ON poly._uid = etally_match.poly_uid;
    """

    # ==========================================
    # 4. STEP DEFINITIONS
    # ==========================================
    steps = [
        ("Ingesting Settlement Extents",
         # ST_Read returns GEOMETRY('EPSG:4326'); the RTREE binder only accepts plain
         # GEOMETRY, so strip the CRS annotation with a cast on the way in.
         lambda: con.execute(f"""
             CREATE OR REPLACE TABLE settlements AS
             SELECT row_number() OVER () AS _uid, * REPLACE (geom::GEOMETRY AS geom)
             FROM ST_Read('{POLYGON_RAW}');
         """)),
         
        ("Ingesting GTS Tracks (Folder of CSVs)", 
         # Using union_by_name=True allows it to safely ingest CSVs even if their column orders are slightly mixed
         # ST_Point converts the tabular coordinates into a spatial geometry column
         lambda: con.execute(f"""
             CREATE OR REPLACE TABLE gts_tracks AS 
             SELECT *, ST_Point({TRACK_LON_FIELD}, {TRACK_LAT_FIELD}) AS geom 
             FROM read_csv_auto('{GTS_TRACKS_FOLDER}/*.csv', union_by_name=True);
         """)),
         
        ("Ingesting eTally Records",
         lambda: con.execute(f"CREATE OR REPLACE TABLE etally_records AS SELECT * FROM read_csv_auto('{ETALLY_RAW}');")),

        ("Ingesting State Boundary (for QA rule R3)",
         lambda: con.execute(f"""
             CREATE OR REPLACE TABLE state_boundary AS
             SELECT geom::GEOMETRY AS geom
             FROM ST_Read('{BOUNDARIES_GPKG}', layer='state');
         """)),

        ("Validating GTS Track Records (QA rule set)",
         lambda: con.execute(validation_query)),

        ("Building Spatial Indexes (R-Tree)",
         # Indexed on the validated subset, which is the only table the spatial
         # join actually reads.
         lambda: [
             con.execute("CREATE INDEX IF NOT EXISTS poly_idx ON settlements USING RTREE (geom);"),
             con.execute("CREATE INDEX IF NOT EXISTS gts_idx ON gts_tracks_valid USING RTREE (geom);")
         ]),
         
        ("Calculating Visitation Status (w/ QC)", 
         lambda: con.execute(analysis_query)),
         
        ("Exporting tabular output (CSV)",
         lambda: con.execute(f"COPY (SELECT * EXCLUDE (geom) FROM visitation_results) TO '{OUTPUT_CSV}' (HEADER, DELIMITER ',');")),

        ("Exporting QA audit trail (CSV)",
         lambda: [
             con.execute(f"COPY qa_summary TO '{OUTPUT_QA_CSV}' (HEADER, DELIMITER ',');"),
             con.execute(f"COPY qa_rule_report TO '{OUTPUT_QA_RULES_CSV}' (HEADER, DELIMITER ',');")
         ]),
         
        ("Exporting spatial output (Shapefile)", 
         export_shapefile)
    ]

    # ==========================================
    # 5. EXECUTION PIPELINE WITH TQDM
    # ==========================================
    print("\nStarting Visitation Pipeline...")
    current_step_desc = "Initialization"
    
    try:
        with tqdm(total=len(steps), desc="Overall Progress", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} Steps") as pbar:
            for description, func in steps:
                pbar.set_description(description)
                current_step_desc = description
                
                func()
                
                pbar.update(1)
                time.sleep(0.1)
                
        # ------------------------------------------
        # QA report
        # ------------------------------------------
        ingested, passed = con.execute(
            "SELECT (SELECT count(*) FROM gts_tracks_qa), (SELECT count(*) FROM gts_tracks_valid)"
        ).fetchone()

        # The labels partition the ingested set: if they stop summing, a record
        # escaped classification and the pass rate below cannot be trusted.
        counted = con.execute("SELECT sum(n_points) FROM qa_summary").fetchone()[0]
        if counted != ingested:
            raise ValueError(
                f"QA integrity check failed: {counted} classified vs {ingested} ingested"
            )

        print("\n" + "=" * 78)
        print("GTS TRACK RECORD VALIDATION")
        print("=" * 78)
        print(f"{'ID':<5}{'Rule':<28}{'Action':<8}{'Points':>12}  Threshold")
        print("-" * 78)
        for rid, name, action, thresh, n in con.execute(
            "SELECT rule_id, rule_name, action, threshold, n_violating "
            "FROM qa_rule_report ORDER BY rule_id"
        ).fetchall():
            print(f"{rid:<5}{name:<28}{action:<8}{n:>12,}  {thresh}")
        print("-" * 78)
        print(f"Ingested {ingested:,} | admissible {passed:,} "
              f"({100.0 * passed / ingested:.2f}%) | removed {ingested - passed:,}")
        print("Rule counts are independent, so they overlap and do not sum to the total.")
        print("The disjoint partition is in qa_summary / gts_qa_summary.csv.")
        print("Only points passing every REMOVE rule were used for the visitation check.")

        print(f"\nPipeline complete!")
        print(f"  -> Tabular Output: {OUTPUT_CSV}")
        print(f"  -> Spatial Output: {OUTPUT_SHP}")
        print(f"  -> QA Audit Trail: {OUTPUT_QA_CSV}")

    # Both handlers exit non-zero. A half-finished pipeline must not report
    # success: the outputs it leaves behind look plausible but are wrong.
    except duckdb.Error as e:
        print(f"\n[!] DuckDB Error during step: '{current_step_desc}'")
        print(f"Error Details: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[!] Python Error during step: '{current_step_desc}'")
        print(f"Error Details: {str(e)}")
        sys.exit(1)
    finally:
        con.close()

if __name__ == "__main__":
    run_visitation_analysis()