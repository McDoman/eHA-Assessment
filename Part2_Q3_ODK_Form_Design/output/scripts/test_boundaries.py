"""
Executable boundary tests, run against the constraint expressions that were
actually compiled into the XForm.

The test cases below do not restate the thresholds. Each one reads the
`constraint` attribute for its question out of HH2026_v2-0-0.xml, evaluates it
with the candidate value, and compares the result with the expected verdict. If
somebody widens a range in build_xlsform.py and forgets the test plan, these
fail. If somebody edits the test plan to match a bug, the form still governs.

A small translator converts the subset of XPath this form uses into Python. It
covers comparison operators, and/or/not, round(), string-length(), regex(),
selected(), count-selected(), selected-at(), number(), date() and today(). Cases
whose logic cannot be expressed that way - repeat aggregates, cross-question
gates, relevance chains - are NOT faked here; they are listed in
docs/08_test_plan.md as device tests with their expected results.

Run:  python test_boundaries.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
XML = os.path.normpath(os.path.join(HERE, "..", "form", "HH2026_v2-0-0.xml"))
XF = "{http://www.w3.org/2002/xforms}"

DEVICE_TODAY = dt.date(2026, 6, 8)  # a device in the middle of the fieldwork window

_FAILURES: list[str] = []
_RUN = 0


# --------------------------------------------------------------------------
# XPath -> Python, for the subset this form uses
# --------------------------------------------------------------------------
def _round(v, n):
    # XPath round() is half-up; Python's round() is half-to-even.
    from decimal import Decimal, ROUND_HALF_UP

    return float(Decimal(str(v)).quantize(Decimal("1." + "0" * int(n)), rounding=ROUND_HALF_UP))


def _regex(v, p):
    return re.match(p, str(v)) is not None


def _selected(v, x):
    return x in str(v).split()


def _count_selected(v):
    return len(str(v).split())


def _selected_at(v, i):
    parts = str(v).split()
    return parts[int(i)] if int(i) < len(parts) else ""


def _date(s):
    return dt.date.fromisoformat(str(s).strip("'\""))


def translate(expr: str, ctx: dict[str, str]) -> str:
    """Rewrite an ODK constraint into an equivalent Python expression."""
    e = " " + " ".join(expr.split()) + " "

    # Mask quoted literals before any rewriting. A regex pattern such as
    # "^[A-Za-z][A-Za-z .'-]*$" contains a '.' that must not be mistaken for the
    # context node, and a '-' that must not be mistaken for an operator.
    literals: list[str] = []

    def _stash(m):
        literals.append(m.group(0))
        return f" __LIT{len(literals) - 1}__ "

    e = re.sub(r"'[^']*'|\"[^\"]*\"", _stash, e)

    # node references supplied by the caller, longest first
    for path in sorted(ctx, key=len, reverse=True):
        e = e.replace(path, f"CTX[{path!r}]")

    # function calls -> helpers (before '.' is rewritten)
    e = re.sub(r"\bround\s*\(\s*\.\s*,", "_round(V,", e)
    e = re.sub(r"\bstring-length\s*\(\s*\.\s*\)", "len(str(V))", e)
    e = re.sub(r"\bregex\s*\(\s*\.\s*,", "_regex(V,", e)
    e = re.sub(r"\bselected-at\s*\(\s*\.\s*,", "_selected_at(V,", e)
    e = re.sub(r"\bselected\s*\(\s*\.\s*,", "_selected(V,", e)
    e = re.sub(r"\bcount-selected\s*\(\s*\.\s*\)", "_count_selected(V)", e)
    e = re.sub(r"\bnumber\s*\(", "float(", e)
    e = re.sub(r"\bdate\s*\(", "_date(", e)
    e = re.sub(r"\btoday\s*\(\s*\)", "TODAY", e)
    e = re.sub(r"\bnot\s*\(", "not (", e)

    # bare '.' (the node under test) -> V
    e = re.sub(r"(?<![\w')\]])\.(?![\w\d(])", " V ", e)

    # operators: '=' -> '==' but leave >=, <=, !=
    e = re.sub(r"(?<![<>!=])=(?!=)", "==", e)
    e = re.sub(r"\bmod\b", "%", e)

    for i, lit in enumerate(literals):
        e = e.replace(f"__LIT{i}__", lit)
    return e


def evaluate(expr: str, value, ctx: dict[str, str] | None = None):
    py = translate(expr, ctx or {})
    env = {
        "V": value, "TODAY": DEVICE_TODAY, "CTX": ctx or {},
        "_round": _round, "_regex": _regex, "_selected": _selected,
        "_count_selected": _count_selected, "_selected_at": _selected_at, "_date": _date,
        "float": float, "len": len, "str": str,
    }
    return bool(eval(py, {"__builtins__": {}}, env))  # noqa: S307 - fixed grammar, no user input


# --------------------------------------------------------------------------
# constraints straight out of the compiled form
# --------------------------------------------------------------------------
def load_constraints() -> dict[str, str]:
    tree = ET.parse(XML).getroot()
    out = {}
    for b in tree.iter(XF + "bind"):
        c = b.get("constraint")
        if c:
            out[b.get("nodeset").rsplit("/", 1)[-1]] = c
    return out


CONSTRAINTS = load_constraints()


def case(test_id: str, field: str, value, expect: bool, what: str, ctx=None) -> None:
    global _RUN
    _RUN += 1
    expr = CONSTRAINTS.get(field)
    if expr is None:
        print(f"  FAIL  {test_id}  no constraint compiled for {field}")
        _FAILURES.append(test_id)
        return
    try:
        got = evaluate(expr, value, ctx)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {test_id}  {field}={value!r} -> evaluator error: {exc}")
        _FAILURES.append(test_id)
        return
    verdict = "ACCEPT" if got else "REJECT"
    want = "ACCEPT" if expect else "REJECT"
    if got == expect:
        print(f"  PASS  {test_id}  {field} = {value!r:>14}  -> {verdict:6}  {what}")
    else:
        print(f"  FAIL  {test_id}  {field} = {value!r:>14}  -> {verdict}, expected {want}  {what}")
        print(f"        constraint: {expr}")
        _FAILURES.append(test_id)


LBL = {" /data/s1/calc_lbl_lo ": "480000", " /data/s1/calc_lbl_hi ": "480899"}


def main() -> int:
    print("=" * 96)
    print("BOUNDARY TESTS - evaluated against the constraints compiled into HH2026_v2-0-0.xml")
    print(f"device date assumed: {DEVICE_TODAY}")
    print("=" * 96)

    print("\nT-01  Fieldwork window (1-14 June 2026)")
    case("T-01a", "q1_10_visit_date", dt.date(2026, 5, 31), False, "day before the window opens")
    case("T-01b", "q1_10_visit_date", dt.date(2026, 6, 1), True, "first day of the window")
    case("T-01c", "q1_10_visit_date", dt.date(2026, 6, 8), True, "mid-window, equals device date")
    case("T-01d", "q1_10_visit_date", dt.date(2026, 6, 14), False, "last day of window but ahead of the device date")
    case("T-01e", "q1_10_visit_date", dt.date(2026, 6, 15), False, "day after the window closes")
    case("T-01f", "q1_10_visit_date", dt.date(2026, 7, 5), False, "outside the fieldwork window entirely")
    case("T-01g", "q1_10_visit_date", dt.date(2025, 6, 8), False, "right day, wrong year")

    print("\nT-02  Household size 1-40 (3.01)")
    case("T-02a", "q3_01_hh_size", 0, False, "empty household")
    case("T-02b", "q3_01_hh_size", 1, True, "lower bound")
    case("T-02c", "q3_01_hh_size", 40, True, "upper bound")
    case("T-02d", "q3_01_hh_size", 41, False, "one above upper bound")
    case("T-02e", "q3_01_hh_size", 99, False, "the paper form's 2-digit maximum, which is also the no-answer sentinel")

    print("\nT-03  Age in months 0-59 (roster col 6)")
    case("T-03a", "r_age_months", -1, False, "negative")
    case("T-03b", "r_age_months", 0, True, "newborn, lower bound")
    case("T-03c", "r_age_months", 59, True, "upper bound - last month of eligibility")
    case("T-03d", "r_age_months", 60, False, "5 years exactly - must be recorded in years")
    case("T-03e", "r_age_months", 98, False, "the two-digit do-not-know sentinel")

    print("\nT-04  Age in years 5-97 (roster col 5)")
    case("T-04a", "r_age_years", 4, False, "under 5 - must be recorded in months")
    case("T-04b", "r_age_years", 5, True, "lower bound")
    case("T-04c", "r_age_years", 97, True, "upper bound")
    case("T-04d", "r_age_years", 98, False, "collides with the do-not-know sentinel (collision X-8)")
    case("T-04e", "r_age_years", 99, False, "collides with the no-answer sentinel")

    print("\nT-05  Weight 2.0-30.0 kg, one decimal (4.05)")
    case("T-05a", "c4_05_weight", 1.9, False, "below lower bound")
    case("T-05b", "c4_05_weight", 2.0, True, "lower bound")
    case("T-05c", "c4_05_weight", 30.0, True, "upper bound")
    case("T-05d", "c4_05_weight", 30.1, False, "above upper bound")
    case("T-05e", "c4_05_weight", 8.75, False, "two decimals - more precision than the scale gives")
    case("T-05f", "c4_05_weight", 99.0, False, "the paper form's 'not measured' code, now impossible to enter")
    case("T-05g", "c4_05_weight", 52.0, False, "decimal point slip for 5.2")

    print("\nT-06  Height 45.0-125.0 cm, one decimal (4.06)")
    case("T-06a", "c4_06_height", 44.9, False, "below lower bound")
    case("T-06b", "c4_06_height", 45.0, True, "lower bound")
    case("T-06c", "c4_06_height", 125.0, True, "upper bound")
    case("T-06d", "c4_06_height", 125.1, False, "above upper bound")
    case("T-06e", "c4_06_height", 99.0, True, "*** the critical case: 99.0 cm is a REAL height and must be accepted "
                                              "(defect D-8 / collision X-3) ***")
    case("T-06f", "c4_06_height", 1.05, False, "recorded in metres")
    case("T-06g", "c4_06_height", 150.0, False, "transposition of 105.0")

    print("\nT-07  Cold box temperature -5.0 to 40.0 C (5.05)")
    case("T-07a", "c5_05_temp", -5.1, False, "below lower bound")
    case("T-07b", "c5_05_temp", -5.0, True, "lower bound - a frozen box is recordable")
    case("T-07c", "c5_05_temp", 4.0, True, "inside the 2-8 C target band")
    case("T-07d", "c5_05_temp", 31.0, True, "*** cold chain breach: unrecordable on the paper form, which prints one "
                                            "digit (defect D-10) ***")
    case("T-07e", "c5_05_temp", 40.0, True, "upper bound")
    case("T-07f", "c5_05_temp", 40.1, False, "above upper bound")

    print("\nT-08  Structure number and household serial 1-999 (1.06, 1.07)")
    case("T-08a", "q1_06_structure", 0, False, "zero")
    case("T-08b", "q1_06_structure", 1, True, "lower bound")
    case("T-08c", "q1_06_structure", 999, True, "upper bound - the paper field is 3 boxes")
    case("T-08d", "q1_06_structure", 1000, False, "above the printed field width")
    case("T-08e", "q1_07_hh_serial", 1, True, "lower bound")
    case("T-08f", "q1_07_hh_serial", 999, True, "upper bound")

    print("\nT-09  Eligible children 0-20 (3.02)")
    case("T-09a", "q3_02_stated", -1, False, "negative")
    case("T-09b", "q3_02_stated", 0, True, "zero is a real answer, and also the skip trigger (collision X-7)")
    case("T-09c", "q3_02_stated", 20, True, "upper bound")
    case("T-09d", "q3_02_stated", 21, False, "above upper bound")

    print("\nT-10  Specimen serial: six digits, inside TM01's block 480000-480899")
    ctx = dict(LBL)
    for tid, val, exp, what in [
        ("T-10a", "480000", True, "first serial of TM01's block"),
        ("T-10b", "480899", True, "last serial of TM01's block"),
        ("T-10c", "479999", False, "one below TM01's block"),
        ("T-10d", "480900", False, "first serial of TM02's block - another team's label"),
        ("T-10e", "48012", False, "five digits"),
        ("T-10f", "4801234", False, "seven digits"),
        ("T-10g", "48O123", False, "letter O keyed for zero"),
        ("T-10h", "501600", False, "one past the last issued block"),
    ]:
        # The uniqueness predicate needs repeat context that does not exist
        # outside a running form, so substitute "this label is not a duplicate"
        # and test the format and range halves of the same expression.
        # T-19 in docs/08_test_plan.md covers the duplicate case on the device.
        expr = CONSTRAINTS["c5_03_serial"]
        expr_nodup = re.sub(r"count\(/data.*?\]\)\s*=\s*1", "1 = 1", expr)
        assert "count(" not in expr_nodup, "uniqueness predicate was not substituted"
        CONSTRAINTS["_serial_test"] = expr_nodup
        case(tid, "_serial_test", val, exp, what, ctx)

    print("\nT-11  Prior household identifier format (1.13b)")
    case("T-11a", "q1_13b_prev_hh_text", "BAN-000123", True, "correct format")
    case("T-11b", "q1_13b_prev_hh_text", "BAN-123", False, "too few digits")
    case("T-11c", "q1_13b_prev_hh_text", "ban-000123", False, "lower case prefix")
    case("T-11d", "q1_13b_prev_hh_text", "000123", False, "prefix missing")
    case("T-11e", "q1_13b_prev_hh_text", "BAN-0001234", False, "too many digits")

    print("\nT-12  Roster name: initials and family name only (roster col 2)")
    case("T-12a", "r_name", "S. Sule", True, "the format used in the previous round register")
    case("T-12b", "r_name", "A", False, "single character")
    case("T-12c", "r_name", "Abdulrahman Musa Ibrahim", False, "full given names - refused by design (DP-1)")
    case("T-12d", "r_name", "S. Sule2", False, "digits")

    print("\nT-13  Assets: 'None of these' is exclusive (6.07)")
    case("T-13a", "q6_07_assets", "A C", True, "radio and mobile telephone")
    case("T-13b", "q6_07_assets", "H", True, "none of these, alone")
    case("T-13c", "q6_07_assets", "A H", False, "owns a radio and owns nothing")
    case("T-13d", "q6_07_assets", "A B C D E F G", True, "everything except 'none'")

    print("\nT-14  GPS: inside Bansara, accuracy at most 100 m (1.11)")
    case("T-14a", "q1_11_gps", "11.2721 7.7131 300 8", True, "a real settlement in the frame, 8 m accuracy")
    case("T-14b", "q1_11_gps", "0 0 0 5", False, "null island")
    case("T-14c", "q1_11_gps", "11.2721 7.7131 300 100", True, "accuracy exactly at the 100 m ceiling")
    case("T-14d", "q1_11_gps", "11.2721 7.7131 300 101", False, "accuracy one metre worse than the ceiling")
    case("T-14e", "q1_11_gps", "9.0576 7.4951 300 6", False, "outside the state (Abuja)")
    case("T-14f", "q1_11_gps", "10.20 6.80 300 6", True, "exactly on the south-west corner of the bounding box")
    case("T-14g", "q1_11_gps", "11.75 8.60 300 6", True, "exactly on the north-east corner")
    case("T-14h", "q1_11_gps", "11.76 8.60 300 6", False, "one hundredth of a degree north of the box")

    print("\nT-15  Free text minimum lengths")
    case("T-15a", "q1_05_altname", "x", False, "single character standing in for a name")
    case("T-15b", "q1_05_altname", "Tudun Wada", True, "a real local name")
    case("T-15c", "c5_07_other", "no", False, "two characters")
    case("T-15d", "c4_14_medicine_other", "Septrin", True, "a reported brand name")
    case("T-15e", "q7_01a_short_reason", "busy", False, "under 10 characters - not an explanation")
    case("T-15f", "q7_01a_short_reason", "Household had one child only and no specimen was sought", True, "a real explanation")

    print("\n" + "=" * 96)
    if _FAILURES:
        print(f"RESULT: {_RUN - len(_FAILURES)}/{_RUN} passed, {len(_FAILURES)} FAILED -> {_FAILURES}")
        return 1
    print(f"RESULT: {_RUN}/{_RUN} boundary cases behave as specified")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
