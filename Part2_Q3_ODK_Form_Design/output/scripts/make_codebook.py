"""
Generate the codebook from the form definition.

Like the constraint register, this is derived from build_xlsform.py rather than
written alongside it, so it describes the instrument that exists. It emits one
row per variable with its analysis table, type, value set and derivation.

Run:  python make_codebook.py     ->  ../docs/12_codebook.csv
"""

from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.normpath(os.path.join(HERE, "..", "docs"))
sys.path.insert(0, HERE)

import build_xlsform as B  # noqa: E402

EN = B.EN

# Which analysis table each variable lands in. The roster repeat is the only
# repeat; Sections 4 and 5 are groups inside it, so they flatten onto the same
# row and are split into views by eligibility.
def table_for(name: str, in_repeat: bool, group: str) -> str:
    if not in_repeat:
        return "hh"
    if group == "s4":
        return "child   (view of roster where elig_s4 = 1)"
    if group in ("s5", "s5b"):
        return "specimen (view of roster where specimen_label is not null)"
    return "roster"


SKIP_TYPES = {"begin_group", "end_group", "begin_repeat", "end_repeat", "note"}
META = {"start", "end", "today", "deviceid", "audit"}


def value_set(row) -> str:
    t = row["type"]
    if t.startswith("select_one_from_file") or t.startswith("select_multiple_from_file"):
        return f"external: {t.split()[-1]}"
    if t.startswith(("select_one", "select_multiple")):
        lst = t.split()[-1]
        opts = [c for c in B.choices if c["list_name"] == lst]
        rendered = "; ".join(f"{c['name']}={c[EN]}" for c in opts[:14])
        if len(opts) > 14:
            rendered += f"; ... ({len(opts)} options)"
        return rendered
    con = row.get("constraint", "")
    return con if con else t


def derivation(row) -> str:
    if row.get("calculation"):
        return "DERIVED: " + " ".join(row["calculation"].split())
    if row.get("relevant"):
        return "asked when: " + " ".join(row["relevant"].split())
    return "asked"


def main() -> int:
    rows = []
    stack: list[str] = []
    in_repeat = False
    for r in B.survey:
        t = r["type"]
        if t == "begin_repeat":
            in_repeat = True
            stack.append(r["name"])
            continue
        if t == "end_repeat":
            in_repeat = False
            stack.pop() if stack else None
            continue
        if t == "begin_group":
            stack.append(r["name"])
            continue
        if t == "end_group":
            stack.pop() if stack else None
            continue
        if t == "note":
            continue
        group = stack[-1] if stack else ""
        rows.append({
            "variable": r["name"],
            "table": "hh" if r["name"] in META else table_for(r["name"], in_repeat, group),
            "section": stack[0] if stack else "meta",
            "label_en": r.get(EN, ""),
            "xls_type": t,
            "storage_type": {
                "integer": "integer", "decimal": "decimal", "date": "date", "time": "time",
                "geopoint": "string 'lat lon alt acc'", "image": "file reference",
                "calculate": "string (cast on load)", "text": "string",
                "start": "dateTime", "end": "dateTime", "today": "date",
                "deviceid": "string", "audit": "file reference",
            }.get(t, "string" if t.startswith("select_one") else
                  "space-delimited string" if t.startswith("select_multiple") else t),
            "value_set_or_range": value_set(r),
            "required": r.get("required", ""),
            "derivation": derivation(r),
        })

    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "12_codebook.csv")
    cols = ["variable", "table", "section", "label_en", "xls_type", "storage_type",
            "value_set_or_range", "required", "derivation"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["table"]] = counts.get(r["table"], 0) + 1
    print(f"codebook: {len(rows)} variables -> {path}")
    for k, v in sorted(counts.items()):
        print(f"  {k:<48} {v:>4}")
    derived = sum(1 for r in rows if r["derivation"].startswith("DERIVED"))
    print(f"  of which derived (never keyed):              {derived:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
