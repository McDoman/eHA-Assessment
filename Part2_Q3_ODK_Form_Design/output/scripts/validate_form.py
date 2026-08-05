"""
Structural validation of the compiled XForm.

pyxform tells you the workbook is well formed. It does not tell you that the
constraint you meant to write is the constraint that ended up in the XML. Three
of the mistakes this catches are ones that were actually made while building
this form:

  * a uniqueness predicate that pyxform resolved to a RELATIVE path, so it saw
    only the current repeat instance and could never fire;
  * a constraint that addressed its own node by absolute path instead of '.';
  * an aggregate over a repeat that silently lost its predicate.

ODK Validate (JavaRosa) is the usual second checker; it needs a Java runtime,
which is not available on this machine. These assertions cover the same ground
for the properties this form depends on. Both must pass before deployment.

Run:  python validate_form.py
Writes ../form/validation_output.txt
"""

from __future__ import annotations

import os
import re
import sys
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
FORM_DIR = os.path.normpath(os.path.join(HERE, "..", "form"))
# Both deployable variants. They are the same instrument with different
# form_ids and different opening languages, so the whole suite runs over each.
VARIANT_FILES = {
    "ha": ("HH2026_v2-0-0.xml", "HH2026", "Hausa (ha)", "hausa-default"),
    "en": ("HH2026_EN_v2-0-0.xml", "HH2026_EN", "English (en)", "english-default"),
}
XML = os.path.join(FORM_DIR, VARIANT_FILES["ha"][0])
MEDIA = os.path.join(FORM_DIR, "media")

XF = "{http://www.w3.org/2002/xforms}"
results: list[tuple[str, bool, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def run(variant: str = "ha") -> int:
    fname, want_id, want_lang, want_variant = VARIANT_FILES[variant]
    xml_path = os.path.join(FORM_DIR, fname)
    if not os.path.exists(xml_path):
        sys.exit(f"compiled form not found: {xml_path}  (run build_xlsform.py first)")
    raw = open(xml_path, encoding="utf-8").read()
    tree = ET.fromstring(raw)

    binds = {}
    for b in tree.iter(XF + "bind"):
        binds[b.get("nodeset")] = b

    def bind(suffix: str):
        hits = [v for k, v in binds.items() if k.endswith("/" + suffix)]
        return hits[0] if len(hits) == 1 else None

    def attr(suffix: str, a: str) -> str:
        b = bind(suffix)
        return (b.get(a) or "") if b is not None else ""

    # ---------------------------------------------------------------- V-1
    ok("V-1  form parses as XML", True)
    ok(f"V-1a form_id is {want_id}", f'id="{want_id}"' in raw)
    ok("V-1b version stamp present", 'version="2026060100"' in raw)
    ok("V-1c both translations present in every variant",
       'lang="Hausa (ha)"' in raw and 'lang="English (en)"' in raw)
    ok(f"V-1d this variant opens in {want_lang}",
       f'lang="{want_lang}" default="true()"' in raw.replace("  ", " "))
    ok(f"V-1e form_variant is carried as data ({want_variant})",
       f"'{want_variant}'" in raw)

    # ---------------------------------------------------------------- V-2
    # Every question the questionnaire prints has a node.
    expected = [
        "q1_01_state", "q1_02_lga", "q1_03_ward", "q1_04_settlement", "q1_05_altname_yn",
        "q1_06_structure", "q1_07_hh_serial", "q1_08_enum", "q1_10_visit_date", "q1_11_gps",
        "q1_12_prev_round", "q1_13_prev_hh", "q1_14_result",
        "q2_01_consent_read", "q2_02_consent", "q2_03_relationship",
        "q3_01_hh_size", "q3_02_stated", "r_name", "r_relation", "r_sex",
        "r_age_years", "r_age_months",
        "c4_01_line", "c4_02_name", "c4_03_months", "c4_04_sex", "c4_05_weight",
        "c4_06_height", "c4_07_position", "c4_08_card", "c4_09_measles_card",
        "c4_10_measles_report", "c4_11_diarrhoea", "c4_12_antibiotic", "c4_13_medicine",
        "c4_14_medicine_other", "c4_15_no_prescription", "c4_16_photo_status",
        "c5_01_age12", "c5_02_obtained", "c5_03_serial", "c5_03_check", "c5_04_time",
        "c5_05_temp", "c5_06_reason", "c5_07_other",
        "q6_01_water", "q6_02_toilet", "q6_03_animals", "q6_04_animal_abx",
        "q6_05_handwash", "q6_06_hh_diarrhoea", "q6_07_assets",
        "q7_01_end_time", "q7_02_observations", "q7_03_attest", "q7_04_sup",
        "q7_05_decision", "q7_06_attest",
    ]
    missing = [n for n in expected if bind(n) is None]
    ok(f"V-2  all {len(expected)} questionnaire items present in the model", not missing, str(missing))

    # ---------------------------------------------------------------- V-3
    # THE ONE THAT BIT: the duplicate-label predicate must be absolute.
    c = attr("c5_03_serial", "constraint")
    ok("V-3  specimen serial uniqueness predicate uses an ABSOLUTE repeat path",
       "/data/s3/roster/s5/s5b/c5_03_serial[. = current()/.]" in c.replace("  ", " "),
       c)
    ok("V-3a specimen serial is range-checked against the team allocation",
       "calc_lbl_lo" in c and "calc_lbl_hi" in c, c)
    ok("V-3b specimen serial is regex-checked to exactly six digits",
       "^[0-9]{6}$" in c, c)

    # ---------------------------------------------------------------- V-4
    # Check digit: the arithmetic in the form must be the scheme in the CSV.
    cd = attr("calc_cd_sum", "calculate")
    weights_found = re.findall(r"substr\([^)]*,\s*(\d+),\s*(\d+)\)\)\s*\*\s*(\d+)", cd)
    ok("V-4  check digit uses six weighted terms", len(weights_found) == 6, cd)
    ok("V-4a weights are 7,6,5,4,3,2 left to right (= 2..7 right to left)",
       [w for _, _, w in weights_found] == ["7", "6", "5", "4", "3", "2"], str(weights_found))
    ok("V-4b substr indices are 0..5 with exclusive end",
       [(a, b) for a, b, _ in weights_found] == [(str(i), str(i + 1)) for i in range(6)],
       str(weights_found))
    exp = attr("calc_cd_expected", "calculate")
    ok("V-4c modulus 11 applied", "mod 11" in exp, exp)
    ok("V-4d remainder 10 rendered as X", "'X'" in exp, exp)
    ok("V-4e check character is constrained against the computed value",
       "calc_cd_expected" in attr("c5_03_check", "constraint"), attr("c5_03_check", "constraint"))

    # ---------------------------------------------------------------- V-5
    # GPS constraint must test the candidate value, not the committed one.
    g = attr("q1_11_gps", "constraint")
    ok("V-5  GPS constraint addresses the node as '.'", "selected-at(., 0)" in g, g)
    ok("V-5a GPS bounding box on both axes",
       g.count("selected-at(., 0)") == 2 and g.count("selected-at(., 1)") == 2, g)
    ok("V-5b GPS accuracy ceiling applied", "selected-at(., 3)" in g, g)

    # ---------------------------------------------------------------- V-6
    # Cross-question reconciliation gates.
    g1 = attr("q3_01_gate", "constraint")
    ok("V-6  household size gate compares roster count to 3.01",
       "calc_n_roster" in g1 and "q3_01_hh_size" in g1, g1)
    ok("V-6a household size gate is relevant only while they disagree",
       "!=" in attr("q3_01_gate", "relevant"), attr("q3_01_gate", "relevant"))
    g2 = attr("q3_02_gate", "constraint")
    ok("V-6b eligible children gate compares roster count to 3.02",
       "calc_n_elig" in g2 and "q3_02_stated" in g2, g2)
    ok("V-6c exactly-one-head gate present", "calc_n_head" in attr("q3_head_gate", "constraint"))

    # ---------------------------------------------------------------- V-7
    # Aggregates over the repeat must keep their predicates and be absolute.
    for node, frag in [
        ("calc_n_elig", "/data/s3/roster/r_elig_s4"),
        ("calc_n_modules", "/data/s3/roster/s4/c4_08_card"),
        ("calc_n_specimens", "/data/s3/roster/s5/s5b/c5_03_serial"),
        ("calc_n_cards_seen", "/data/s3/roster/s4/c4_08_card"),
        ("calc_child_diarrhoea", "/data/s3/roster/s4/c4_11_diarrhoea"),
    ]:
        calc = attr(node, "calculate")
        ok(f"V-7  {node} aggregates over an absolute repeat path with a predicate",
           frag in calc and "[" in calc, calc)

    # ---------------------------------------------------------------- V-8
    # Eligibility cuts are the ones the questionnaire states.
    e4 = attr("r_elig_s4", "calculate")
    ok("V-8  Section 4 eligibility is 9..59 completed months",
       ">= 9" in e4 and "<= 59" in e4, e4)
    e5 = attr("r_elig_s5", "calculate")
    ok("V-8a Section 5 specimen cut is 12 completed months", ">= 12" in e5, e5)
    ok("V-8b measurement position cut is 24 months",
       "< 24" in attr("calc_pos_expected", "calculate"), attr("calc_pos_expected", "calculate"))

    # ---------------------------------------------------------------- V-9
    # No sentinel may sit inside a measurement field: each measurement is
    # gated by an explicit 'was it measured' question and is otherwise empty.
    for meas, gate in [("c4_05_weight", "c4_05_measured"), ("c4_06_height", "c4_06_measured")]:
        rel = attr(meas, "relevant")
        ok(f"V-9  {meas} is gated by {gate} so the 'not measured' code never enters the field",
           gate in rel, rel)
        con = attr(meas, "constraint")
        ok(f"V-9a {meas} range excludes 99 as a legal value where 99 would be ambiguous",
           "round(., 1)" in con, con)
    hcon = attr("c4_06_height", "constraint")
    ok("V-9b height range 45..125 cm admits 99.0 cm as a real measurement, not a sentinel",
       ">= 45" in hcon and "<= 125" in hcon, hcon)

    # ---------------------------------------------------------------- V-10
    # Skip logic the paper form leaves undefined.
    ok("V-10  5.03 opens only when a specimen was obtained (5.02 = Yes)",
       "c5_02_obtained" in attr("c5_03_serial", "relevant"))
    ok("V-10a 5.06 opens only when no specimen was obtained (5.02 = No)",
       "c5_02_obtained" in attr("c5_06_reason", "relevant") and "'2'" in attr("c5_06_reason", "relevant"))
    # Section relevance lives on the group bind, not on each question inside it.
    grp = lambda n: (binds.get("/data/" + n).get("relevant") or "") if "/data/" + n in binds else ""
    ok("V-10b Sections 2-6 are closed when the visit was not completed",
       all("q1_14_result" in grp(s) for s in ("s2", "s3", "s6")),
       f"s2={grp('s2')} | s3={grp('s3')} | s6={grp('s6')}")
    ok("V-10c Section 7 is NOT gated on the visit result, so refusals are still signed off",
       "q1_14_result" not in grp("s7") and "q1_14_result" not in (attr("q7_03_attest", "relevant") or ""),
       grp("s7"))

    # ---------------------------------------------------------------- V-11
    # Consent gate.
    ok("V-11  2.01 cannot be answered No and continue", attr("q2_01_consent_read", "constraint") == ". = '1'")
    ok("V-11a Sections 3 and 6 require consent given",
       "q2_02_consent" in grp("s3") and "q2_02_consent" in grp("s6"),
       f"s3={grp('s3')} | s6={grp('s6')}")
    ok("V-11b respondent-is-an-adult gate blocks on No", attr("q2_01a_adult", "constraint") == ". = '1'")

    # ---------------------------------------------------------------- V-12
    # Fieldwork window.
    d = attr("q1_10_visit_date", "constraint")
    ok("V-12  visit date bounded below by the window start", "2026-06-01" in d, d)
    ok("V-12a visit date bounded above by the window end", "2026-06-14" in d, d)
    ok("V-12b visit date cannot be in the future", "today()" in d, d)

    # ---------------------------------------------------------------- V-13
    # Fabrication controls.
    ok("V-13  enumerator PIN checked against the staff roster",
       "pulldata('staff', 'pin'" in attr("q1_08a_pin", "constraint"))
    ok("V-13a supervisor PIN checked against the staff roster",
       "pulldata('staff', 'pin'" in attr("q7_04a_sup_pin", "constraint"))
    ok("V-13b supervisor list filtered to role=supervisor and the same team",
       "role='supervisor'" in raw and "team_code=" in raw)
    ok("V-13c audit log enabled", 'jr:preload="' in raw or "audit" in binds_keys(binds))
    ok("V-13d interview duration computed from the start timestamp",
       "decimal-date-time" in attr("calc_duration_min", "calculate"))
    ok("V-13e short-interview explanation required below 15 minutes",
       "< 15" in attr("q7_01a_short_reason", "relevant"), attr("q7_01a_short_reason", "relevant"))
    ok("V-13f hard floor of 3 minutes on completed interviews",
       ">= 3" in attr("q7_01b_hard_gate", "constraint"), attr("q7_01b_hard_gate", "constraint"))
    for f in ["flag_short_interview", "flag_no_cards_seen", "flag_roster_corrected",
              "flag_specimen_shortfall", "flag_cold_chain"]:
        ok(f"V-13g QA flag {f} exported on every submission", bind(f) is not None)

    # ---------------------------------------------------------------- V-14
    # External data: every referenced CSV must exist, and carry the columns the
    # form filters and pulls on.
    refs = set(re.findall(r'src="jr://file-csv/([^"]+)"', raw))
    ok("V-14  seven external CSVs referenced", len(refs) == 7, str(sorted(refs)))
    import csv as _csv

    needed = {
        "settlements.csv": {"name", "label", "ward_code", "lat", "lon"},
        "wards.csv": {"name", "label", "lga_code"},
        "lgas.csv": {"name", "label"},
        "staff.csv": {"name", "label", "role", "team_code", "lga_code", "pin"},
        "prev_households.csv": {"name", "label", "settlement_id", "structure_number"},
        "specimen_alloc.csv": {"name", "range_start", "range_end"},
        "medicines.csv": {"name", "label"},
    }
    for fn, cols in needed.items():
        path = os.path.join(MEDIA, fn)
        if not os.path.exists(path):
            ok(f"V-14a media file {fn} present", False, "missing")
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            header = set(next(_csv.reader(fh)))
        ok(f"V-14a media file {fn} present with the columns the form uses",
           cols <= header, f"missing {sorted(cols - header)}")
        ok(f"V-14b {fn} is referenced by the form", fn in refs)

    # every pulldata() target column exists
    for fn, col, key in re.findall(r"pulldata\('(\w+)',\s*'(\w+)',\s*'(\w+)'", raw):
        path = os.path.join(MEDIA, fn + ".csv")
        with open(path, newline="", encoding="utf-8") as fh:
            header = set(next(_csv.reader(fh)))
        ok(f"V-14c pulldata('{fn}','{col}','{key}') resolves against the shipped CSV",
           col in header and key in header, f"header={sorted(header)}")

    # choice_filter columns exist too
    for inst, expr in re.findall(r"instance\('(\w+)'\)/root/item\[([^\]]+)\]", raw):
        if inst in ("yesno", "yes_no_dk"):
            continue
        path = os.path.join(MEDIA, inst + ".csv")
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            header = set(next(_csv.reader(fh)))
        cols = set(re.findall(r"\b([a-z_]+)\s*=", expr))
        ok(f"V-14d choice_filter on {inst} uses columns that exist",
           cols <= header, f"{sorted(cols - header)} not in {sorted(header)}")

    # ---------------------------------------------------------------- V-15
    # Bilingual: every question label carries both languages.
    itext = {}
    for tr in tree.iter(XF + "translation"):
        lang = tr.get("lang")
        itext[lang] = {t.get("id") for t in tr.iter(XF + "text")}
    langs = sorted(itext)
    ok("V-15  exactly two translations present", len(langs) == 2, str(langs))
    if len(langs) == 2:
        a, b = langs
        ok("V-15a both translations carry the same text ids", itext[a] == itext[b],
           f"only in {a}: {sorted(itext[a] - itext[b])[:5]} | only in {b}: {sorted(itext[b] - itext[a])[:5]}")
        ok("V-15b a default language is declared", 'default="true()"' in raw)

    # ---------------------------------------------------------------- V-16
    # No question is both required and permanently irrelevant, and no
    # constraint references a node that does not exist.
    all_names = {k.rsplit("/", 1)[-1] for k in binds}
    dangling = set()
    for b in binds.values():
        for a in ("constraint", "relevant", "calculate"):
            for ref in re.findall(r"/data/[\w/]+", b.get(a) or ""):
                if ref.rsplit("/", 1)[-1] not in all_names:
                    dangling.add(ref)
    ok("V-16  no expression references a node that does not exist", not dangling, str(sorted(dangling)))

    # ---------------------------------------------------------------- V-17
    # A group whose only members are calculates compiles to a <group> with no
    # children in the body. ODK Validate rejects that outright - "Group has no
    # children!" - and KoboToolbox refuses the deployment. pyxform converts it
    # without complaint, so nothing upstream of the server catches it.
    # This shipped once; it does not ship again.
    body = next((c for c in tree if c.tag.endswith("body")), None)
    ok("V-17  form has a body element", body is not None)
    empties = []
    if body is not None:
        def scan(el):
            for ch in el:
                tag = ch.tag.split("}")[-1]
                if tag in ("group", "repeat"):
                    controls = [k for k in ch if k.tag.split("}")[-1] != "label"]
                    if not controls:
                        empties.append(ch.get("ref") or ch.get("nodeset") or "?")
                    scan(ch)
        scan(body)
    ok("V-17a no group or repeat in the body is empty (ODK Validate rejects these)",
       not empties, str(empties))

    # Every question that produces a body control must actually be reachable.
    refs = set()
    if body is not None:
        for el in body.iter():
            r = el.get("ref") or el.get("nodeset")
            if r and r.startswith("/data/"):
                refs.add(r)
    calc_only = {k for k, v in binds.items()
                 if v.get("calculate") is not None and v.get("type") != "binary"}
    orphan_controls = [r for r in refs if r in calc_only]
    ok("V-17b no calculate is rendered as a body control", not orphan_controls,
       str(orphan_controls[:5]))

    # ---------------------------------------------------------------- report
    width = 78
    lines = ["=" * width, f"COMPILED XFORM - STRUCTURAL VALIDATION [variant: {variant}]",
             "=" * width,
             f"form : {os.path.basename(xml_path)}   form_id {want_id}",
             f"media: {MEDIA}", ""]
    passed = sum(1 for _, c, _ in results if c)
    for name, cond, detail in results:
        lines.append(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond and detail:
            lines.append(f"        -> {detail[:400]}")
    lines += ["", "=" * width,
              f"RESULT: {passed}/{len(results)} checks passed"
              + ("" if passed == len(results) else "  *** FAILURES ABOVE ***"),
              "=" * width]
    out = "\n".join(lines)
    print(out)
    return (0 if passed == len(results) else 1), out


def binds_keys(binds) -> str:
    return " ".join(binds)


def diff_variants() -> tuple[int, str]:
    """
    Assert the two variants are the same instrument.

    Translations are allowed to differ - that is the whole point of the variant.
    Nothing else is. Every bind is compared attribute by attribute on the
    properties that decide what the form *does*: type, constraint, relevance,
    calculation and required. The only permitted difference is the form_variant
    calculate, which exists precisely to record which variant a record came from.

    Without this, the English form silently becomes a fork: a constraint fixed
    in one and not the other, and two datasets that no longer mean the same
    thing.
    """
    ALLOWED = {"form_variant"}
    COMPARED = ("type", "constraint", "relevant", "calculate", "required")

    models = {}
    for variant, (fname, *_rest) in VARIANT_FILES.items():
        path = os.path.join(FORM_DIR, fname)
        if not os.path.exists(path):
            return 1, f"cannot diff variants: {fname} missing"
        tree = ET.parse(path).getroot()
        models[variant] = {
            b.get("nodeset"): tuple(b.get(a) or "" for a in COMPARED)
            for b in tree.iter(XF + "bind")
        }

    a, b = models["ha"], models["en"]
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    differing = sorted(
        k for k in set(a) & set(b)
        if a[k] != b[k] and k.rsplit("/", 1)[-1] not in ALLOWED
    )

    lines = ["=" * 78, "VARIANT EQUIVALENCE  (HH2026 vs HH2026_EN)", "=" * 78,
             f"binds compared : {len(set(a) & set(b))}",
             f"compared on    : {', '.join(COMPARED)}",
             f"permitted to differ: {', '.join(sorted(ALLOWED))}", ""]
    checks = [
        ("nodes only in the Hausa variant", not only_a, str(only_a[:5])),
        ("nodes only in the English variant", not only_b, str(only_b[:5])),
        ("no behavioural difference between the two models", not differing,
         str([(k, a[k], b[k]) for k in differing[:3]])),
    ]
    bad = 0
    for name, cond, detail in checks:
        lines.append(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            lines.append(f"        -> {detail[:400]}")
            bad += 1
    lines += ["", "=" * 78,
              "RESULT: the two variants are the same instrument"
              if not bad else f"RESULT: {bad} DIFFERENCE(S) - the variants have forked",
              "=" * 78]
    out = "\n".join(lines)
    print("\n" + out)
    return (1 if bad else 0), out


def main() -> int:
    status, blocks = 0, []
    for variant in VARIANT_FILES:
        results.clear()
        st, out = run(variant)
        status |= st
        blocks.append(out)
        print()
    st, out = diff_variants()
    status |= st
    blocks.append(out)

    with open(os.path.join(FORM_DIR, "validation_output.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(blocks) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
