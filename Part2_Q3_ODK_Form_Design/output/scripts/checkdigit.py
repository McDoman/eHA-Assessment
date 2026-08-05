"""
Specimen label check-digit scheme for the HH/2026 survey.

Scheme as stated in reference_media/specimen_label_allocation.csv:

    "Modulus 11, weights 2 to 7 applied right to left, remainder 10 recorded as X"

Interpretation implemented here (PRIMARY, literal reading of the string):

    serial            d1 d2 d3 d4 d5 d6        (6 digits, left to right)
    weights           7  6  5  4  3  2         (2..7 applied RIGHT TO LEFT)
    S      = sum(di * wi)
    check  = S mod 11                          ("the remainder")
    if check == 10 -> printed as "X"

A full label is therefore  BSN <6 digits> - <check>, e.g. BSN480000-0.

A second reading is possible and is used by some modulus-11 schemes
(NHS number, ISBN-10 style):  check = (11 - S mod 11) mod 11.  The allocation
file does not ship any specimen label with its check digit attached, so there is
no ground truth in the data pack to discriminate between the two.  The literal
reading is implemented as the default; VARIANT_SUBTRACTIVE is implemented beside
it so the switch is a one-line change if the print vendor confirms otherwise.
This is recorded as an open item in docs/04_defect_register.md (D-15b) and the
constraint register (C-5.03c).

TRANSPOSITION PROPERTY (why this check is worth having)
-------------------------------------------------------
Transposing the digits at positions i and j changes the weighted sum by
    (w_i - w_j) * (d_i - d_j)
For our weight vector, |w_i - w_j| is between 1 and 5, and for a real
transposition |d_i - d_j| is between 1 and 9.  11 is prime and both factors are
non-zero and strictly less than 11, so their product is never congruent to
0 mod 11.  The remainder therefore always changes, and EVERY transposition of
two unequal digits is detected - adjacent or not.  The same argument shows every
single-digit substitution is detected.  See test_checkdigit.py, which proves
this exhaustively over the whole issued range rather than asserting it.
"""

from __future__ import annotations

SERIAL_LEN = 6
WEIGHTS = (7, 6, 5, 4, 3, 2)  # positions left->right, i.e. 2..7 right->left
VARIANT_SUBTRACTIVE = False  # see module docstring


class LabelError(ValueError):
    """Raised when a label is structurally malformed."""


def weighted_sum(serial: str) -> int:
    if not isinstance(serial, str) or len(serial) != SERIAL_LEN or not serial.isdigit():
        raise LabelError(f"serial must be exactly {SERIAL_LEN} digits, got {serial!r}")
    return sum(int(d) * w for d, w in zip(serial, WEIGHTS))


def check_digit(serial: str) -> str:
    """Return the check character ('0'..'9' or 'X') for a 6-digit serial."""
    s = weighted_sum(serial)
    r = (11 - s % 11) % 11 if VARIANT_SUBTRACTIVE else s % 11
    return "X" if r == 10 else str(r)


def is_valid(serial: str, check: str) -> bool:
    """True if `check` is the correct check character for `serial`."""
    try:
        expected = check_digit(serial)
    except LabelError:
        return False
    return str(check).strip().upper() == expected


def format_label(serial: str, prefix: str = "BSN") -> str:
    return f"{prefix}{serial}-{check_digit(serial)}"


def parse_label(label: str) -> tuple[str, str, str]:
    """Split 'BSN480000-0' into ('BSN', '480000', '0'). Raises LabelError."""
    t = str(label).strip().upper().replace(" ", "")
    if "-" not in t:
        raise LabelError(f"label must contain '-' separating the check digit: {label!r}")
    body, check = t.rsplit("-", 1)
    prefix, serial = body[:-SERIAL_LEN], body[-SERIAL_LEN:]
    if not serial.isdigit() or len(serial) != SERIAL_LEN:
        raise LabelError(f"serial part must be {SERIAL_LEN} digits: {label!r}")
    if len(check) != 1 or check not in "0123456789X":
        raise LabelError(f"check character must be one of 0-9 or X: {label!r}")
    return prefix, serial, check


def xpath_expression(serial_ref: str = "${c5_03_serial_str}") -> str:
    """
    Emit the XPath used inside the XForm, so the form and this reference
    implementation are demonstrably the same algorithm.
    ODK's substr(string, start, end) is 0-indexed with an exclusive end.
    """
    terms = " + ".join(
        f"number(substr({serial_ref}, {i}, {i + 1})) * {w}"
        for i, w in enumerate(WEIGHTS)
    )
    return terms


if __name__ == "__main__":  # tiny CLI: python checkdigit.py 480000
    import sys

    for arg in sys.argv[1:]:
        a = arg.strip().upper()
        if "-" in a:
            _, ser, chk = parse_label(a)
            print(f"{a}\t{'VALID' if is_valid(ser, chk) else 'REJECTED'}\t(expected {check_digit(ser)})")
        else:
            print(f"{a}\t-> {format_label(a)}")
