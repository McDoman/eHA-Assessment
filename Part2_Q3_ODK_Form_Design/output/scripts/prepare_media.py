"""
Build the media (external CSV) files that are attached to the form on
KoboToolbox.

Design rules applied here - the reasoning is in docs/05_settlement_serving.md:

 1. Nothing that the form does not read is shipped to the device.  Columns are
    dropped, not carried "in case".  2,524 settlements x 12 columns becomes
    2,524 x 6.
 2. Every file gets `name` and `label` columns because that is what
    select_one_from_file binds to, and a key column that pulldata() can index.
 3. previous_round_households is filtered to consent_to_follow_up == 'yes'
    before it ever reaches a tablet.  342 households declined follow-up; putting
    their identifiers and dwelling coordinates on 120 devices would re-identify
    people who asked not to be re-approached.  Their GPS columns are dropped for
    everyone - the form does not use them.
 4. settlements.geojson (712 KB) is NOT shipped.  See the design note.
 5. medicines.csv is a PLACEHOLDER.  The questionnaire's 4.13 says "record from
    the medicine list" and no medicine list exists anywhere in the data pack.
    This is defect D-15, escalated, not silently resolved.

Run:  python prepare_media.py
"""

from __future__ import annotations

import csv
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "..", "reference_media"))
OUT = os.path.normpath(os.path.join(HERE, "..", "form", "media"))

# Bounding box of the survey area, derived from settlements.csv and widened.
# Used by the build script to write the GPS constraint - single source of truth.
BBOX = {"lat_min": 10.20, "lat_max": 11.75, "lon_min": 6.80, "lon_max": 8.60}


def read(name: str) -> list[dict]:
    with open(os.path.join(SRC, name), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


WRITTEN: set[str] = set()


def write(name: str, rows: list[dict], fields: list[str]) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fields})
    content = buf.getvalue()

    # Only touch the file if the content actually changed. This keeps the build
    # idempotent, and it means a CSV that happens to be open in Excel does not
    # fail the build unless its content genuinely needs to change.
    existing = None
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            existing = fh.read()

    WRITTEN.add(name)
    if existing == content:
        print(f"  {name:<28} {len(rows):>6,} rows  {len(content) / 1024:>8.1f} KB  (unchanged)")
        return

    try:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(content)
    except PermissionError:
        raise SystemExit(
            f"\n  Cannot write {path}\n"
            f"  Its content needs to change, but the file is open in another program\n"
            f"  (Excel holds a lock on CSVs it has open). Close it and run this again."
        ) from None
    print(f"  {name:<28} {len(rows):>6,} rows  {os.path.getsize(path) / 1024:>8.1f} KB")


def build_lgas() -> list[dict]:
    rows = sorted(read("lgas.csv"), key=lambda r: r["label"])
    write("lgas.csv", rows, ["name", "label"])
    return rows


def build_wards() -> None:
    rows = sorted(read("wards.csv"), key=lambda r: (r["lga_code"], r["label"]))
    write("wards.csv", rows, ["name", "label", "lga_code"])


def build_settlements() -> None:
    src = read("settlements.csv")
    rows = []
    for r in src:
        rows.append(
            {
                "name": r["name"],
                # settlement_type is folded into the label so the enumerator can
                # tell "Adwade (Hamlet)" from a same-named village without the
                # form having to render a second column on a 7-inch screen.
                "label": f"{r['label']} ({r['settlement_type']})",
                "ward_code": r["ward_code"],
                "lga_code": r["lga_code"],
                "lon": r["longitude"],
                "lat": r["latitude"],
            }
        )
    rows.sort(key=lambda r: (r["ward_code"], r["label"]))
    write("settlements.csv", rows, ["name", "label", "ward_code", "lga_code", "lon", "lat"])


def build_staff(lgas: list[dict]) -> None:
    # staff_roster.assigned_lga holds the LGA *name*; the form cascades on codes.
    lga_code_by_name = {r["label"]: r["name"] for r in lgas}
    src = read("staff_roster.csv")
    rows = []
    unmapped = set()
    for r in src:
        code = lga_code_by_name.get(r["assigned_lga"], "")
        if not code:
            unmapped.add(r["assigned_lga"])
        rows.append(
            {
                "name": r["name"],
                "label": f"{r['name']} - {r['label']}",
                "team_code": r["team_code"],
                "role": "supervisor" if r["role"] == "Team supervisor" else "enumerator",
                "lga_code": code,
                "pin": r["pin"],
                "phlebotomy_certified": r["phlebotomy_certified"],
            }
        )
    if unmapped:
        raise SystemExit(f"assigned_lga values with no LGA code: {sorted(unmapped)}")
    rows.sort(key=lambda r: r["name"])
    write("staff.csv", rows, ["name", "label", "team_code", "role", "lga_code", "pin", "phlebotomy_certified"])
    n_sup = sum(1 for r in rows if r["role"] == "supervisor")
    print(f"       -> {len(rows) - n_sup} enumerators, {n_sup} supervisors, {len({r['team_code'] for r in rows})} teams")


def build_prev_households() -> None:
    src = read("previous_round_households.csv")
    kept, dropped = [], 0
    for r in src:
        if r["consent_to_follow_up"].strip().lower() != "yes":
            dropped += 1
            continue  # rule 3 above
        kept.append(
            {
                "name": r["household_id"],
                "label": (
                    f"{r['household_id']}  |  {r['head_of_household_initials']}"
                    f"  |  structure {r['structure_number']}"
                    f"  |  {r['children_under5_last_round']} u5"
                ),
                "settlement_id": r["settlement_id"],
                "structure_number": r["structure_number"],
                "children_u5_prev": r["children_under5_last_round"],
            }
        )
    kept.sort(key=lambda r: (r["settlement_id"], int(r["structure_number"])))
    write(
        "prev_households.csv",
        kept,
        ["name", "label", "settlement_id", "structure_number", "children_u5_prev"],
    )
    print(f"       -> {dropped} households withheld (consent_to_follow_up = no); GPS columns dropped for all")


def build_specimen_alloc() -> None:
    src = read("specimen_label_allocation.csv")
    rows = [
        {
            "name": r["team_code"],
            "label": f"{r['team_code']} {r['label_prefix']}{r['range_start']}-{r['range_end']}",
            "prefix": r["label_prefix"],
            "range_start": r["range_start"],
            "range_end": r["range_end"],
        }
        for r in src
    ]
    rows.sort(key=lambda r: r["name"])
    write("specimen_alloc.csv", rows, ["name", "label", "prefix", "range_start", "range_end"])


# ---------------------------------------------------------------------------
# PLACEHOLDER medicine list - see defect D-15.
# Codes are two digits and deliberately avoid 96 (Other), 98 (do not know) and
# 99 (no answer) so that the questionnaire's own sentinel scheme does not
# collide with a substantive category.  aware_class follows the WHO AWaRe
# classification; anti-tuberculosis agents are excluded rather than guessed at.
# ---------------------------------------------------------------------------
MEDICINES = [
    ("01", "Amoxicillin", "Access"),
    ("02", "Amoxicillin + clavulanic acid", "Access"),
    ("03", "Ampicillin", "Access"),
    ("04", "Ampicillin + cloxacillin (Ampiclox)", "Access"),
    ("05", "Cloxacillin", "Access"),
    ("06", "Phenoxymethylpenicillin (Penicillin V)", "Access"),
    ("07", "Benzylpenicillin (injection)", "Access"),
    ("08", "Benzathine benzylpenicillin (injection)", "Access"),
    ("09", "Cotrimoxazole (sulfamethoxazole + trimethoprim)", "Access"),
    ("10", "Metronidazole", "Access"),
    ("11", "Gentamicin (injection)", "Access"),
    ("12", "Doxycycline", "Access"),
    ("13", "Tetracycline", "Access"),
    ("14", "Chloramphenicol", "Access"),
    ("15", "Nitrofurantoin", "Access"),
    ("16", "Cefalexin (cephalexin)", "Access"),
    ("17", "Cefazolin (injection)", "Access"),
    ("18", "Erythromycin", "Watch"),
    ("19", "Azithromycin", "Watch"),
    ("20", "Clarithromycin", "Watch"),
    ("21", "Ciprofloxacin", "Watch"),
    ("22", "Ofloxacin", "Watch"),
    ("23", "Levofloxacin", "Watch"),
    ("24", "Cefuroxime", "Watch"),
    ("25", "Cefixime", "Watch"),
    ("26", "Ceftriaxone (injection)", "Watch"),
    ("27", "Cefotaxime (injection)", "Watch"),
    ("28", "Ceftazidime (injection)", "Watch"),
    ("29", "Streptomycin (injection)", "Watch"),
    ("30", "Meropenem (injection)", "Watch"),
    ("31", "Vancomycin", "Watch"),
    ("32", "Clindamycin", "Access"),
    ("33", "Sulfadimidine / sulfonamide (other)", "Access"),
    ("34", "Neomycin (oral)", "Access"),
    ("35", "Unbranded tablet or capsule, antibiotic type not identifiable", "Unclassified"),
    ("36", "Injection given, agent not identifiable", "Unclassified"),
    # Sentinels live in the option list, not in a separate numeric field, because
    # 4.13 is a pure categorical code.  They keep the questionnaire's own two-digit
    # sentinel values (96 Other, 98 Do not know) and no substantive medicine is
    # given code 96, 98 or 99, so there is no collision.  Codes sort last.
    ("96", "Other antibiotic - write the name at 4.14", "Other"),
    ("98", "Do not know / caregiver cannot name it", "DontKnow"),
]


# Where the ministry's approved list is expected once it arrives. Drop it here
# and it wins automatically - see docs/04_defect_register.md D-15.
MINISTRY_LIST = os.path.join(SRC, "medicine_list.csv")

SENTINEL_CODES = {
    "96": ("Other antibiotic - write the name at 4.14", "Other"),
    "98": ("Do not know / caregiver cannot name it", "DontKnow"),
}


def build_medicines() -> None:
    """
    The ministry's approved list wins if it is present; the placeholder is only
    a fallback.

    This matters because prepare_media.py regenerates every media file on each
    build. Dropping an approved list straight into form/media/ would be silently
    overwritten on the next run, so the swap has to happen at the source.
    """
    if os.path.exists(MINISTRY_LIST):
        src = read("medicine_list.csv")
        cols = set(src[0]) if src else set()
        if not {"name", "label"} <= cols:
            raise SystemExit(
                f"\n  {MINISTRY_LIST}\n"
                f"  must have 'name' (the code) and 'label' (what the enumerator reads).\n"
                f"  Found: {sorted(cols)}"
            )
        rows = [
            {"name": r["name"].strip(), "label": r["label"].strip(),
             "aware_class": (r.get("aware_class") or "").strip()}
            for r in src if r.get("name", "").strip()
        ]

        codes = [r["name"] for r in rows]
        dupes = sorted({c for c in codes if codes.count(c) > 1})
        if dupes:
            raise SystemExit(f"\n  medicine_list.csv has duplicate codes: {dupes}")

        # Collision X-10: a substantive medicine must never take a sentinel code.
        clashes = sorted(set(codes) & {"96", "98", "99"})
        substantive = [c for c in clashes if c not in SENTINEL_CODES or
                       rows[codes.index(c)]["label"] not in
                       (SENTINEL_CODES.get(c, ("",))[0],)]
        if "99" in codes or substantive:
            raise SystemExit(
                f"\n  medicine_list.csv assigns a real medicine to a reserved code: "
                f"{substantive or ['99']}\n"
                f"  96 = Other, 98 = Do not know and 99 = no answer are the questionnaire's own\n"
                f"  two-digit sentinels. A medicine using one of them is collision X-10 and makes\n"
                f"  4.13 unanalysable. Recode that medicine, or tell the ministry the scheme clashes."
            )

        for code, (label, cls) in SENTINEL_CODES.items():
            if code not in codes:
                rows.append({"name": code, "label": label, "aware_class": cls})

        rows.sort(key=lambda r: r["name"])
        write("medicines.csv", rows, ["name", "label", "aware_class"])
        n_real = len(rows) - len(SENTINEL_CODES)
        print(f"       -> APPROVED ministry list ({n_real} medicines + "
              f"{len(SENTINEL_CODES)} sentinels). Defect D-15 RESOLVED.")
        return

    rows = [{"name": c, "label": n, "aware_class": a} for c, n, a in MEDICINES]
    write("medicines.csv", rows, ["name", "label", "aware_class"])
    print("       -> PLACEHOLDER list (defect D-15): the approved ministry medicine list")
    print("          was not supplied with the questionnaire. Do not deploy without it.")
    print(f"          To resolve: put the approved list at")
    print(f"          {MINISTRY_LIST}")
    print("          with columns name,label[,aware_class] and rerun. It wins automatically.")


def main() -> None:
    # Files are overwritten in place rather than the directory being wiped and
    # rebuilt. rmtree fails outright if any CSV is open in Excel, which holds a
    # lock on it, and half-deleting the media set is a worse state to be left in
    # than an overwrite that fails cleanly on one file. Stale files are removed
    # afterwards, once we know what was actually written.
    os.makedirs(OUT, exist_ok=True)
    print(f"source : {SRC}\ntarget : {OUT}\n")
    lgas = build_lgas()
    build_wards()
    build_settlements()
    build_staff(lgas)
    build_prev_households()
    build_specimen_alloc()
    build_medicines()

    stale = [f for f in os.listdir(OUT) if f not in WRITTEN]
    for f in stale:
        try:
            os.remove(os.path.join(OUT, f))
            print(f"  removed stale media file: {f}")
        except OSError as exc:
            print(f"  WARNING: could not remove stale file {f}: {exc}")

    total = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
    print(f"\n  TOTAL media payload per device: {total / 1024:.1f} KB")
    src_total = sum(
        os.path.getsize(os.path.join(SRC, f)) for f in os.listdir(SRC) if f.endswith((".csv", ".geojson"))
    )
    print(f"  (reference_media as supplied:   {src_total / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
