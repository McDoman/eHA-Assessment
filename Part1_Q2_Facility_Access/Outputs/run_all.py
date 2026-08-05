"""
run_all.py
==========
Run the whole Question 2 pipeline end to end, in order, from a clean state.

    python run_all.py

Each stage is a standalone script and can also be run on its own, but they are
ordered: 02 needs 01's crosswalk, 03 needs 02's conformed layers, 04 needs 03's
database inputs, and 05 needs 04's access metrics. The runner enforces that order
and stops at the first failure rather than carrying a broken artefact forward.

Nothing here writes to the source data directory. Every output lands under
Outputs/.
"""

from __future__ import annotations

import runpy
import sys
import time
import traceback
from pathlib import Path

from common import ART, LOG_DIR, banner, get_logger

LOG = get_logger("run_all")

STAGES = [
    ("01_crosswalk_normalisation_pipeline.py",
     "Normalise the Surveyor General's crosswalk (task 1)"),
    ("02_ingest_and_conform_pipeline.py",
     "Ingest, repair and conform every layer"),
    ("03_spatial_database_pipeline.py",
     "Build the governed spatial database (task 2)"),
    ("04_access_analysis_pipeline.py",
     "Population-weighted access analysis (task 3)"),
    ("05_gap_typology_and_outputs_pipeline.py",
     "Gap typology, priorities and outputs (task 4)"),
]

KEY_OUTPUTS = [
    ("Normalised crosswalk table", ART["crosswalk_table"]),
    ("Crosswalk reconciliation ledger", ART["crosswalk_ledger"]),
    ("Crosswalk exceptions", ART["crosswalk_exceptions"]),
    ("Conformed layers", ART["conformed_gpkg"]),
    ("Spatial database", ART["database"]),
    ("PostGIS-equivalent DDL", ART["postgis_ddl"]),
    ("Ward access metrics", ART["access_ward"]),
    ("Sensitivity sweep", ART["sensitivity"]),
    ("Ward gap typology", ART["typology"]),
    ("Priority wards", ART["priority_wards"]),
]


def main() -> int:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))

    banner(LOG, "FACILITY READINESS AND ACCESS PIPELINE — FULL RUN")
    started = time.time()
    timings = []

    for script, description in STAGES:
        path = here / script
        if not path.exists():
            LOG.error("Missing stage script: %s", script)
            return 1
        LOG.info("")
        LOG.info(">>> %s — %s", script, description)
        t0 = time.time()
        try:
            runpy.run_path(str(path), run_name="__main__")
        except Exception:
            LOG.error("Stage %s FAILED:\n%s", script, traceback.format_exc())
            LOG.error("Pipeline halted. Nothing downstream was run, so no stale "
                      "artefact has been carried forward.")
            return 1
        elapsed = time.time() - t0
        timings.append((script, elapsed))
        LOG.info("<<< %s completed in %.1fs", script, elapsed)

    banner(LOG, "ALL STAGES COMPLETE")
    for script, elapsed in timings:
        LOG.info("  %-46s %6.1fs", script, elapsed)
    LOG.info("  %-46s %6.1fs", "TOTAL", time.time() - started)

    LOG.info("")
    LOG.info("Key outputs:")
    missing = 0
    for label, path in KEY_OUTPUTS:
        ok = path.exists()
        missing += (not ok)
        LOG.info("  [%s] %-34s %s", "ok" if ok else "MISSING", label,
                 path.relative_to(here))
    LOG.info("  Logs: %s", LOG_DIR.relative_to(here))

    if missing:
        LOG.error("%d expected output(s) missing.", missing)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
