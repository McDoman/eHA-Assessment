# Form HH/2026 — Bansara Integrated Child Health and AMR Household Survey

Digitisation of the approved paper questionnaire `Household_Questionnaire_HH2026v1.docx` for
deployment on **KoboToolbox**.

```
python scripts/run_all.py
```

Rebuilds the media, regenerates and converts the form, and runs every automated check.

## Status

| | |
|---|---|
| **Conversion** | pyxform **4.5.0** — converted **without error**, 0 warnings |
| **Structural validation** | **97 / 97** assertions, run over **both** language variants, plus a variant-equivalence diff |
| **Boundary tests** | **87 / 87**, evaluated against the deployed constraint expressions |
| **Check digit tests** | **All pass** — 292,960 transposition cases, 166,644 substitution cases |
| **Fabrication check** | Catches the reported pattern on **day 1**, after 8 interviews, 0 false positives |
| **Constraint register** | **60 rules**, generated from the form |
| **Deployment** | **BLOCKED** on D-15 and D-15b — see below |

### Two things block deployment

| | |
|---|---|
| **D-15** | 4.13 says "record from the medicine list". **There is no medicine list anywhere in the data pack.** `form/media/medicines.csv` is a clearly-marked placeholder. Replacing it is a media swap, not a form change |
| **D-15b** | The check digit scheme is stated in `specimen_label_allocation.csv` but **no example label carrying its check digit is supplied.** Two readings of "remainder" are current and they disagree for almost every serial. One physical label resolves it |

Both are missing inputs, not unfinished work. Full list in
[`docs/13_scope_and_exclusions.md`](docs/13_scope_and_exclusions.md).

## Start here

**[`docs/00_critical_reading_of_the_questionnaire.pdf`](docs/00_critical_reading_of_the_questionnaire.pdf)**
(and `.docx`) — what is wrong with the paper instrument and how each flaw was treated. Four
pages: the disposition rule, the seven findings that change the data, the fifteen coding
collisions, what was deliberately *not* fixed, what I refused to invent, and the checks that
hold each resolution in place.

## Deliverables

| # | Requirement | Where |
|---|---|---|
| 1 | Build the form; state the tool and version; include the conversion output | [`docs/01_form_and_conversion.md`](docs/01_form_and_conversion.md) · [`form/`](form/) |
| **2** | **Constraint register** | [`docs/02_constraint_register.md`](docs/02_constraint_register.md) · [`.csv`](docs/02_constraint_register.csv) — **generated from the form** |
| 3 | Coding scheme, sentinels, and every collision | [`docs/03_coding_and_sentinels.md`](docs/03_coding_and_sentinels.md) — 15 collisions |
| 4 | Cross-question consistency | [`docs/07b_consistency_checks.md`](docs/07b_consistency_checks.md) |
| 5 | Defects in the questionnaire | [`docs/04_defect_register.md`](docs/04_defect_register.md) — 24 findings |
| 6 | Serving 2,524 settlements to a 2 GB device | [`docs/05_settlement_serving.md`](docs/05_settlement_serving.md) |
| 7 | Specimen label validation and check digit | [`docs/06_specimen_labels.md`](docs/06_specimen_labels.md) |
| 8 | Rejecting a previously-used label | [`docs/07_duplicate_labels.md`](docs/07_duplicate_labels.md) |
| 9 | Test plan | [`docs/08_test_plan.md`](docs/08_test_plan.md) |
| 10 | Deployment and version control | [`docs/09_deployment_and_versioning.md`](docs/09_deployment_and_versioning.md) |
| 11 | Fabrication detection | [`docs/10_fabrication_detection.md`](docs/10_fabrication_detection.md) |
| 12 | Data protection | [`docs/11_data_protection.md`](docs/11_data_protection.md) |
| 13 | Codebook | [`docs/12_codebook.md`](docs/12_codebook.md) · [`.csv`](docs/12_codebook.csv) |
| 14 | Deliberate exclusions | [`docs/13_scope_and_exclusions.md`](docs/13_scope_and_exclusions.md) |

## Layout

```
output/
├─ README.md                         this file
├─ form/
│  ├─ HH2026_v2-0-0.xlsx             the XLSForm (a build artefact)
│  ├─ HH2026_v2-0-0.xml              the compiled XForm
│  ├─ conversion_output.txt          pyxform version and conversion log
│  ├─ validation_output.txt          97 structural assertions x2 + variant diff
│  └─ media/                         the seven CSVs attached to the form (407 KB)
├─ docs/                             the deliverables above
└─ scripts/
   ├─ run_all.py                     build and verify everything
   ├─ build_xlsform.py               THE FORM. Every rule and its justification
   ├─ prepare_media.py               build the attached CSVs from reference_media/
   ├─ validate_form.py               structural assertions against the XForm
   ├─ test_boundaries.py             boundary tests against deployed constraints
   ├─ checkdigit.py                  modulus-11 reference implementation
   ├─ test_checkdigit.py             transposition and substitution tests
   ├─ extract_registers.py           constraint register, from the form
   ├─ make_codebook.py               codebook, from the form
   └─ daily_qa_checks.py             daily fabrication checks (--demo to see them work)
```

## Three things worth knowing before reading further

**The registers are generated, not written.** `build_xlsform.py` carries each rule's
justification — what it prevents, and where the threshold came from — beside the rule itself.
`extract_registers.py` emits the register from the same object that is compiled into the
XForm, and the `rule_as_deployed` column is the literal expression, not a description of it. A
constraint added without a justification **fails the build**. The register cannot describe a
rule the form does not contain, which is the usual failure mode of constraint documentation.

**Of the 60 registered rules, 16 are stated plainly as my judgement**, 16 trace to a supplied
data file, and 10 to the questionnaire itself. Where I set a threshold the questionnaire does
not state and could not name a source, the register says "judgement" rather than dressing it
up. The plausibility bounds on weight and height are the clearest case: they are wide
deliberately, they catch keying errors rather than clinical outliers, and they are not
presented as WHO limits.

**Sections 4 and 5 are nested inside the roster repeat.** This is the one structural decision
that changes how the interview runs, and the reasoning — plus what it costs — is set out in
[`docs/01_form_and_conversion.md`](docs/01_form_and_conversion.md).

## The headline findings

- **4.06 is the most damaging defect in the instrument.** "Not measured = 99" is written into a
  centimetre field in which **99.0 cm is an ordinary height for a 3-to-4-year-old**. Once
  written, no rule can separate a measured child from a refusal. Fixed by separating the
  measurement from the reason it is absent.
- **5.05 cannot record the failure it exists to detect.** The cold box temperature field is one
  digit and a decimal, so no reading of 10 °C or above can be written down.
- **3.02 cannot be answered as printed.** It instructs the enumerator to read a count off
  column (7), which is marked office use and must be left blank in the field.
- **5.02 has no skip instruction at all**, leaving both branches of Section 5 open on either
  answer.
- **2.01 = No has no consequence**, so the form permits consent to be recorded against a
  statement that was never read.
- **The LGA and ward code boxes are the wrong width** for the codes in the supplied lookups.
- **342 households that declined follow-up** are still linkable through 1.13. They are now
  withheld from the device entirely.

Twenty-four findings in total, each recorded as resolved, escalated, or both, with the reason
for the choice. Silently fixing an ethics-approved instrument is not automatically right, so
the two changes that alter what an enumerator is *permitted to do* — rather than how a value is
*stored* — are both flagged for the ethics committee, and both are single rows to revert.
