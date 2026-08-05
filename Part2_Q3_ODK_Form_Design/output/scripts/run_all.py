"""
Build and verify everything, in order. This is the command to run.

  python run_all.py

Steps:
  1. prepare_media.py     rebuild the seven attached CSVs from reference_media/
  2. build_xlsform.py     write the XLSForm and convert it with pyxform
  3. validate_form.py     structural assertions against the compiled XForm
  4. test_boundaries.py   boundary cases against the deployed constraints
  5. test_checkdigit.py   check digit, including every transposition
  6. extract_registers.py regenerate the constraint register from the form
  7. make_codebook.py     regenerate the codebook from the form
  8. daily_qa_checks.py   demonstrate the fabrication check catches the pattern

Any non-zero exit fails the build.
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

STEPS = [
    ("prepare_media.py", [], "Build attached media CSVs"),
    ("build_xlsform.py", [], "Write XLSForm and convert with pyxform"),
    ("validate_form.py", [], "Structural validation of the compiled XForm"),
    ("test_boundaries.py", [], "Boundary tests against deployed constraints"),
    ("test_checkdigit.py", [], "Check digit and transposition tests"),
    ("extract_registers.py", [], "Regenerate the constraint register"),
    ("make_codebook.py", [], "Regenerate the codebook"),
    ("daily_qa_checks.py", ["--demo", "--day", "2026-06-01"], "Fabrication check, day 1"),
]


def main() -> int:
    results = []
    for script, args, desc in STEPS:
        print("\n" + "=" * 88)
        print(f"  {script}  -  {desc}")
        print("=" * 88)
        p = subprocess.run([sys.executable, os.path.join(HERE, script), *args],
                           cwd=HERE, capture_output=True, text=True)
        tail = [l for l in (p.stdout or "").splitlines() if l.strip()][-6:]
        print("\n".join(tail))
        if p.returncode != 0 and p.stderr:
            print("STDERR:", p.stderr[-1500:])
        results.append((script, p.returncode))

    print("\n" + "=" * 88)
    print("  BUILD SUMMARY")
    print("=" * 88)
    for script, rc in results:
        print(f"  {'OK  ' if rc == 0 else 'FAIL'}  {script}")
    failed = [s for s, rc in results if rc != 0]
    print("=" * 88)
    if failed:
        print(f"  FAILED: {failed}")
        return 1
    print("  All steps passed.")
    print("  Remember: D-15 (medicine list) and D-15b (check digit scheme) block deployment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
