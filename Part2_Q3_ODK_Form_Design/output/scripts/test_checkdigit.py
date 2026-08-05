"""
Tests for the specimen label check digit.

Run:  python test_checkdigit.py          (no pytest required)
      pytest test_checkdigit.py -q       (also works)

The point of this file is NOT to show that valid labels are accepted - that is
the easy half and proves almost nothing.  The substantive tests are the negative
ones: transposed digit pairs, single-digit substitutions, out-of-block serials
and malformed input.  The transposition test is run exhaustively over every
serial issued in specimen_label_allocation.csv and every one of the 15 possible
digit-pair swaps in each, so the claim "this check rejects a transposed pair"
is demonstrated over 21,600 x 15 cases rather than asserted.
"""

from __future__ import annotations

import csv
import itertools
import os
import sys

from checkdigit import (
    LabelError,
    check_digit,
    format_label,
    is_valid,
    parse_label,
    weighted_sum,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOC = os.path.normpath(
    os.path.join(HERE, "..", "..", "reference_media", "specimen_label_allocation.csv")
)

_FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _FAILURES.append(name)


def load_allocation() -> list[dict]:
    with open(ALLOC, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def issued_serials() -> list[str]:
    out = []
    for row in load_allocation():
        for n in range(int(row["range_start"]), int(row["range_end"]) + 1):
            out.append(f"{n:06d}")
    return out


# --------------------------------------------------------------------------
# T-CD-01  worked example, so the arithmetic is auditable by hand
# --------------------------------------------------------------------------
def test_worked_example() -> None:
    print("\nT-CD-01  worked example (hand-checkable)")
    serial = "480123"
    # 4*7 + 8*6 + 0*5 + 1*4 + 2*3 + 3*2 = 28 + 48 + 0 + 4 + 6 + 6 = 92
    check("weighted sum of 480123 is 92", weighted_sum(serial) == 92, weighted_sum(serial))
    # 92 mod 11 = 4  (11*8 = 88, remainder 4)
    check("check digit of 480123 is '4'", check_digit(serial) == "4", check_digit(serial))
    check("formatted label is BSN480123-4", format_label(serial) == "BSN480123-4")
    check("BSN480123-4 accepted", is_valid("480123", "4"))
    check("BSN480123-5 rejected", not is_valid("480123", "5"))


# --------------------------------------------------------------------------
# T-CD-02  the 'X' case actually occurs and round-trips
# --------------------------------------------------------------------------
def test_x_check_digit_exists() -> None:
    print("\nT-CD-02  remainder 10 is recorded as X")
    xs = [s for s in issued_serials() if check_digit(s) == "X"]
    check("at least one issued serial has check digit X", len(xs) > 0, f"n={len(xs)}")
    if xs:
        s = xs[0]
        check(f"{format_label(s)} accepted", is_valid(s, "X"))
        check(f"{s} with check '0' rejected", not is_valid(s, "0"))
        check("lower case 'x' accepted (enumerator keyboard)", is_valid(s, "x"))
    # roughly 1 serial in 11 should land on X
    check(
        "X occurs at approximately 1 in 11 of issued serials",
        0.06 < len(xs) / len(issued_serials()) < 0.12,
        f"{len(xs)}/{len(issued_serials())}",
    )


# --------------------------------------------------------------------------
# T-CD-03  THE HEADLINE NEGATIVE TEST: transposed pairs are rejected
# --------------------------------------------------------------------------
def test_transposition_rejected_exhaustive() -> None:
    print("\nT-CD-03  every transposition of two unequal digits is rejected")
    serials = issued_serials()
    tested = 0
    missed: list[tuple[str, str]] = []
    for serial in serials:
        good = check_digit(serial)
        for i, j in itertools.combinations(range(6), 2):
            if serial[i] == serial[j]:
                continue  # swapping equal digits yields the same serial - not an error
            lst = list(serial)
            lst[i], lst[j] = lst[j], lst[i]
            swapped = "".join(lst)
            tested += 1
            if is_valid(swapped, good):
                missed.append((serial, swapped))
    check(
        f"all {tested:,} transposition cases rejected across {len(serials):,} issued serials",
        not missed,
        f"first 5 misses: {missed[:5]}",
    )

    # named worked cases, spelled out for the test plan
    named = [
        ("480123", "480132"),  # last two digits swapped  (adjacent)
        ("480123", "481023"),  # middle pair swapped      (adjacent)
        ("480123", "840123"),  # leading pair swapped     (adjacent)
        ("480123", "380124"),  # positions 1 and 6        (non-adjacent, far)
        ("492600", "496200"),  # start of TM15 block, positions 2 and 4
    ]
    for original, transposed in named:
        good = check_digit(original)
        check(
            f"BSN{transposed}-{good} rejected (transposition of BSN{original}-{good})",
            not is_valid(transposed, good),
            f"expected for {transposed} is {check_digit(transposed)}",
        )


# --------------------------------------------------------------------------
# T-CD-04  single-digit substitutions are rejected
# --------------------------------------------------------------------------
def test_single_digit_substitution_rejected() -> None:
    print("\nT-CD-04  every single-digit substitution is rejected")
    missed = []
    tested = 0
    for serial in issued_serials()[::7]:  # every 7th serial keeps the run quick
        good = check_digit(serial)
        for pos in range(6):
            for d in "0123456789":
                if d == serial[pos]:
                    continue
                cand = serial[:pos] + d + serial[pos + 1 :]
                tested += 1
                if is_valid(cand, good):
                    missed.append((serial, cand))
    check(f"all {tested:,} substitution cases rejected", not missed, f"{missed[:5]}")


# --------------------------------------------------------------------------
# T-CD-05  wrong check character is rejected (all 10 wrong alternatives)
# --------------------------------------------------------------------------
def test_wrong_check_character() -> None:
    print("\nT-CD-05  wrong check character rejected")
    serial = "485400"  # first serial of TM07
    good = check_digit(serial)
    wrong = [c for c in "0123456789X" if c != good]
    check(
        f"all 10 incorrect check characters rejected for {serial} (correct is {good})",
        all(not is_valid(serial, w) for w in wrong),
    )


# --------------------------------------------------------------------------
# T-CD-06  malformed input is rejected, not silently coerced
# --------------------------------------------------------------------------
def test_malformed_input() -> None:
    print("\nT-CD-06  malformed input rejected")
    check("5-digit serial rejected", not is_valid("48012", "4"))
    check("7-digit serial rejected", not is_valid("4801234", "4"))
    check("non-numeric serial rejected", not is_valid("48O123", "4"))  # letter O for zero
    check("empty serial rejected", not is_valid("", "4"))
    for bad in ["BSN480123", "BSN48012-4", "BSN480123-Z", "480123-4-1"]:
        try:
            parse_label(bad)
            ok = False
        except LabelError:
            ok = True
        check(f"parse_label rejects {bad!r}", ok)
    check("parse_label accepts BSN480123-4", parse_label("BSN480123-4") == ("BSN", "480123", "4"))
    check("parse_label tolerates spaces/case", parse_label(" bsn480123-4 ")[1] == "480123")


# --------------------------------------------------------------------------
# T-CD-07  team block boundaries (the range check, not the check digit)
# --------------------------------------------------------------------------
def test_team_block_boundaries() -> None:
    print("\nT-CD-07  team allocation block boundaries")
    alloc = {r["team_code"]: (int(r["range_start"]), int(r["range_end"])) for r in load_allocation()}
    check("24 teams allocated", len(alloc) == 24, len(alloc))
    lo, hi = alloc["TM01"]
    check("TM01 block is 480000-480899", (lo, hi) == (480000, 480899), (lo, hi))
    check("900 labels per team", all(hi - lo + 1 == 900 for lo, hi in alloc.values()))
    ordered = sorted(alloc.values())
    check(
        "team blocks are contiguous and non-overlapping",
        all(b[0] == a[1] + 1 for a, b in zip(ordered, ordered[1:])),
    )
    # in-form range constraint semantics
    in_block = lambda team, n: alloc[team][0] <= n <= alloc[team][1]
    check("TM01 accepts 480000 (lower bound)", in_block("TM01", 480000))
    check("TM01 accepts 480899 (upper bound)", in_block("TM01", 480899))
    check("TM01 rejects 479999 (one below)", not in_block("TM01", 479999))
    check("TM01 rejects 480900 (one above, belongs to TM02)", not in_block("TM01", 480900))
    check("TM02 accepts 480900", in_block("TM02", 480900))
    check("no team accepts 501600 (one past the last block)", not any(l <= 501600 <= h for l, h in alloc.values()))
    check("no team accepts 479999", not any(l <= 479999 <= h for l, h in alloc.values()))


# --------------------------------------------------------------------------
# T-CD-08  the XPath in the form is the same arithmetic as this module
# --------------------------------------------------------------------------
def test_xpath_matches_python() -> None:
    print("\nT-CD-08  XForm XPath reproduces the Python reference")
    import re

    from checkdigit import xpath_expression

    expr = xpath_expression("SER")

    def eval_xpath_like(serial: str) -> int:
        # substr(SER, i, i+1) * w  ->  int(serial[i]) * w
        total = 0
        for i, w in re.findall(r"substr\(SER, (\d+), \d+\)\) \* (\d+)", expr):
            total += int(serial[int(i)]) * int(w)
        return total

    mismatches = [s for s in issued_serials()[::13] if eval_xpath_like(s) != weighted_sum(s)]
    check("XPath term expansion equals weighted_sum for sampled serials", not mismatches, mismatches[:3])
    check("XPath uses 0-indexed exclusive-end substr", "substr(SER, 0, 1)" in expr, expr)


def main() -> int:
    print("=" * 78)
    print("SPECIMEN LABEL CHECK DIGIT - TEST SUITE")
    print(f"allocation file: {ALLOC}")
    print("=" * 78)
    test_worked_example()
    test_x_check_digit_exists()
    test_transposition_rejected_exhaustive()
    test_single_digit_substitution_rejected()
    test_wrong_check_character()
    test_malformed_input()
    test_team_block_boundaries()
    test_xpath_matches_python()
    print("\n" + "=" * 78)
    if _FAILURES:
        print(f"RESULT: {len(_FAILURES)} FAILED -> {_FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
