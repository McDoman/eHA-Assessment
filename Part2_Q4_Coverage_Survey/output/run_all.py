"""
run_all.py
==========
Executes the six pipeline stages in order, in separate processes so that each
stage's log file is written cleanly and a failure is attributable to one stage.

    python run_all.py

Stages are independent scripts and can also be run individually, but they are
ordered: 02 needs 01's cleaned files, 03-05 need 02's weights, 06 needs
everything.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from common import banner, get_logger

LOG = get_logger("run_all")

STAGES = [
    ("01_prepare_and_validate.py", "Preparation and structural validation"),
    ("02_design_weights.py", "Design weights and the assumption register"),
    ("03_weighted_estimates.py", "Weighted estimates, design effects, sensitivity"),
    ("04_data_quality.py", "Data quality assessment"),
    ("05_documented_source.py", "Coverage by documented source"),
    ("06_figures_and_report.py", "Figures, tables and the survey report"),
    ("07_branded_report.py", "Survey report in the house document style"),
]

# The PDF export drives the installed Word through COM, so it is kept out of
# the stage list: the pipeline must still complete on a machine without Office.
PDF_EXPORT = ("build/to_pdf.py", "branded/06_survey_report.docx")


def main() -> int:
    here = Path(__file__).resolve().parent
    banner(LOG, "Part 2 / Question 4 -- post-campaign coverage survey pipeline")
    t0 = time.time()

    for script, description in STAGES:
        LOG.info("-> %-32s %s", script, description)
        started = time.time()
        result = subprocess.run([sys.executable, script], cwd=here)
        if result.returncode != 0:
            LOG.error("%s failed with exit code %d -- stopping", script, result.returncode)
            return result.returncode
        LOG.info("   completed in %.1fs", time.time() - started)

    LOG.info("-> %-32s %s", PDF_EXPORT[0], "PDF export via the installed Word")
    pdf = subprocess.run([sys.executable, *PDF_EXPORT], cwd=here)
    if pdf.returncode != 0:
        LOG.warning("PDF export failed (exit %d). The DOCX is still written; run "
                    "`python %s %s` on a machine with Word installed.",
                    pdf.returncode, *PDF_EXPORT)

    banner(LOG, f"Pipeline complete in {time.time() - t0:.1f}s")
    LOG.info("Branded report:  branded/06_survey_report.docx (and .pdf)")
    LOG.info("Plain report:    reports/06_survey_report.md (and .docx)")
    LOG.info("All tables:      reports/06_survey_report_tables.xlsx")
    LOG.info("Figures:         figures/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
