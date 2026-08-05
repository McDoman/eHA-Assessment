# Part 2, Question 4 — Coverage survey analysis under a complex sampling design

Analysis of a post-campaign coverage survey conducted in three states in May 2026 under a
stratified two-stage cluster design with probability-proportional-to-size selection of
enumeration areas.

**Start here:** [`branded/06_survey_report.pdf`](branded/06_survey_report.pdf) — the survey
report for the national programme and the funding partner, in house style. The editable source
sits beside it as [`branded/06_survey_report.docx`](branded/06_survey_report.docx).

The same content is also available as plain
[`reports/06_survey_report.md`](reports/06_survey_report.md) and
[`reports/06_survey_report.docx`](reports/06_survey_report.docx), and every table in it is in
[`reports/06_survey_report_tables.xlsx`](reports/06_survey_report_tables.xlsx).

## Headline

| | Coverage (9–59 completed months) | 95% CI | DEFF | Effective n |
|---|---|---|---|---|
| **National** | **81.3%** | 78.7–83.6 | 1.91 | 1,032 |
| Bansara State (ST01) | 85.0% | 80.6–88.5 | 1.69 | 350 |
| Kudama State (ST02) | 88.6% | 86.7–90.3 | 0.57 | 1,318 |
| Zaruwa State (ST03) | 70.0% | 62.7–76.3 | 3.33 | 188 |

Coverage is below the 95% campaign target in every stratum, and the shortfall survives every
sensitivity variant tested. **A mop-up round is indicated.** The nine clusters worked by one
interviewer are excluded from these figures on a pre-declared falsification screen; retaining
them would have reported 83.0%.

## Running it

```
python run_all.py                    # all six stages, ~60 seconds
python 03_weighted_estimates.py      # or any stage on its own, in order
```

Requires `pandas`, `numpy`, `scipy`, `statsmodels`, `matplotlib`, `openpyxl`, `python-docx`.
Nothing is written back to the source directory; all inputs are opened read-only.

## Pipeline

| Stage | Does | Key outputs |
|---|---|---|
| `01_prepare_and_validate.py` | Reconstructs the design as executed; checks referential integrity, duplicates, PPS probabilities, listing agreement, age eligibility, skip patterns, roster arithmetic, fieldwork window | `reports/01_data_integrity_ledger.csv` |
| `02_design_weights.py` | Stage-one and stage-two selection probabilities, non-response adjustment, child weights, and the assumption register | `reports/02_assumption_register.csv`, `data/children_weighted.csv` |
| `03_weighted_estimates.py` | Weighted estimates with Taylor-linearised design-based intervals, design effects, effective sample sizes, bootstrap validation, 15-variant sensitivity | `reports/03_coverage_estimates.csv` |
| `04_data_quality.py` | Age heaping and digit preference, interviewer falsification screen, implausible patterns, missingness and its ignorability | `reports/04_quality_flags.csv` |
| `05_documented_source.py` | Card-confirmed versus caregiver-recall coverage, and how much of the headline depends on the distinction | `reports/05_coverage_by_source.csv` |
| `06_figures_and_report.py` | Nine figures, the Excel workbook, and the survey report in markdown and Word | `reports/06_survey_report.md` |
| `07_branded_report.py` | Re-renders that report in the house document style (two colours, A4, banded tables), then `build/to_pdf.py` exports it through Word | `branded/06_survey_report.docx` + `.pdf` |

`common.py` holds every path, parameter and threshold, the complex-survey estimation engine
(ratio estimator, Taylor linearisation, rescaled bootstrap, design effect) and the falsification
screen, so that no analytical rule is defined in more than one place.

## The three decisions that matter most

1. **The design is not self-weighting.** Stage one selected clusters proportional to the 2023
   census household count; stage two took 20 households from a *fresh field listing*. The two
   counts differ by a factor of 0.50 to 3.77, so the probabilities do not cancel and the base
   weight varies eight-fold. The second-stage probability is computed from the field listing.
   Assuming self-weighting instead would shift the headline by 1.6 points; treating the sample
   as simple random would understate the standard error by a factor of 1.38 (DEFT), giving a
   confidence interval 28% narrower than the design justifies.

2. **Variance is design-based throughout.** Ratio estimator, linearised residuals summed to the
   cluster, between-cluster variance accumulated within stratum, logit-scale confidence limits,
   78 degrees of freedom nationally. Validated against a Rao–Wu–Yue rescaled bootstrap
   (2,000 replicates) — the two agree to within 2.6%.

3. **One interviewer's work is excluded** by a four-rule screen declared in `common.py` before
   the data were examined: median interview under 4 minutes, 100% reported coverage, cards seen
   for 2% of children, 98% household completion. Nine clusters, 255 children. The screen is
   applied identically in every stage.

## Directory

```text
output/
├── common.py                      configuration, logging, survey estimation engine
├── run_all.py                     runs the seven stages in order, then the PDF export
├── 01…07_*.py                     pipeline stages
├── build/                         doctheme.py, to_pdf.py, PDF page previews
├── branded/                       the house-style report: DOCX + PDF
├── data/                          cleaned and weighted analysis files
├── reports/                       tables (CSV), stage reports (MD), workbook (XLSX), plain DOCX
├── figures/                       nine figures at 200 dpi
└── logs/                          per-stage execution logs
```

The branded document is generated *from* `reports/06_survey_report.md`, not maintained beside
it, so it cannot drift from the analysis: change a number in the pipeline and it changes in the
report on the next run. `reports/06_survey_report.docx` — the plain Word version — is left in
place untouched.
