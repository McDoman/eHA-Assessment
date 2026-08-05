"""
Daily fieldwork quality assurance checks.

Run every morning against everything received so far. The target is the pattern
described in the operating conditions: one enumerator, 94 interviews, mean
duration 4 minutes, almost no vaccination cards sighted, discovered only after
fieldwork had closed. These checks are designed to surface it on the first day
the data reaches a hub, which with the sync plan in docs/09 is within 72 hours of
the first fabricated interview.

Usage
-----
  python daily_qa_checks.py --demo
        Synthesise a plausible round - 120 enumerators, 14 days, one of them
        fabricating - and run the checks against it. This exists so the
        detection logic can be demonstrated to work rather than asserted to.

  python daily_qa_checks.py --input submissions.csv [--day 2026-06-04]
        Run against a real Kobo CSV export.

Expected columns (all produced by the form; see docs/12_codebook.md):
  q1_08_enum, calc_team, q1_10_visit_date, start, end, calc_duration_min,
  calc_n_roster, calc_n_elig, calc_n_modules, calc_n_cards_seen,
  calc_n_specimens, calc_n_elig_s5, calc_gps_lat, calc_gps_lon,
  q1_04_settlement, q1_14_result, flag_* , deviceid
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median

# --------------------------------------------------------------------------
# Thresholds. All judgement, all calibrated on the reported incident, all here
# rather than scattered through the checks so they can be argued about.
# --------------------------------------------------------------------------
T_DURATION_MEDIAN = 15      # minutes; below this the interviews are too quick
T_CARD_RATE = 0.30          # proportion of child modules with a card sighted
T_VOLUME_PER_DAY = 8        # households per enumerator per day
T_MIN_INTERVIEWS = 5        # do not judge an enumerator on fewer than this
T_GPS_IDENTICAL = 3         # households sharing a GPS point to 4 dp
T_GAP_MINUTES = 2           # minimum gap between the end of one interview and
                            # the start of the next; below this they overlap
T_SPECIMEN_RATE = 0.40      # specimens obtained / children aged 12m+


# --------------------------------------------------------------------------
# demo data
# --------------------------------------------------------------------------
def synthesise(seed: int = 7) -> list[dict]:
    """A round of fieldwork with one fabricating enumerator (ENU042)."""
    rng = random.Random(seed)
    rows: list[dict] = []
    enums = [f"ENU{i:03d}" for i in range(1, 97)]
    fabricator = "ENU042"
    start_day = date(2026, 6, 1)

    for d in range(14):
        day = start_day + timedelta(days=d)
        for e in enums:
            team = f"TM{(int(e[3:]) - 1) % 24 + 1:02d}"
            fake = e == fabricator
            n = 8 if fake else rng.randint(3, 6)
            clock = datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
            base_lat, base_lon = 10.6 + rng.random() * 0.9, 7.2 + rng.random() * 0.8
            for _ in range(n):
                if fake:
                    dur = rng.randint(3, 6)
                    cards = 0
                    lat, lon = round(base_lat, 4), round(base_lon, 4)  # never moves
                    n_elig = rng.choice([1, 1, 2])
                    spec = 0
                else:
                    dur = rng.randint(22, 55)
                    n_elig = rng.choice([0, 1, 1, 2, 3])
                    cards = sum(1 for _ in range(n_elig) if rng.random() < 0.62)
                    lat = round(base_lat + rng.uniform(-0.05, 0.05), 6)
                    lon = round(base_lon + rng.uniform(-0.05, 0.05), 6)
                    spec = sum(1 for _ in range(n_elig) if rng.random() < 0.7)
                s = clock
                en = clock + timedelta(minutes=dur)
                clock = en + timedelta(minutes=1 if fake else rng.randint(12, 40))
                rows.append({
                    "q1_08_enum": e, "calc_team": team,
                    "q1_10_visit_date": day.isoformat(),
                    "start": s.isoformat(timespec="seconds"),
                    "end": en.isoformat(timespec="seconds"),
                    "calc_duration_min": dur,
                    "calc_n_roster": rng.randint(3, 9),
                    "calc_n_elig": n_elig, "calc_n_modules": n_elig,
                    "calc_n_cards_seen": cards,
                    "calc_n_elig_s5": n_elig, "calc_n_specimens": spec,
                    "calc_gps_lat": lat, "calc_gps_lon": lon,
                    "q1_14_result": 1, "deviceid": f"dev-{e}",
                })
    return rows


def load_csv(path: str) -> list[dict]:
    import csv

    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def f(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def run_checks(rows: list[dict], as_of: str | None) -> tuple[list[dict], dict]:
    rows = [r for r in rows if str(r.get("q1_14_result", "1")) == "1"]
    if as_of:
        rows = [r for r in rows if str(r.get("q1_10_visit_date", "")) <= as_of]

    by_enum: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_enum[r["q1_08_enum"]].append(r)

    all_dur = [f(r, "calc_duration_min") for r in rows if f(r, "calc_duration_min") > 0]
    survey_median_dur = median(all_dur) if all_dur else 0

    findings = []
    for e, rs in sorted(by_enum.items()):
        if len(rs) < T_MIN_INTERVIEWS:
            continue
        team = rs[0].get("calc_team", "")
        durs = [f(r, "calc_duration_min") for r in rs if f(r, "calc_duration_min") > 0]
        med_dur = median(durs) if durs else 0

        n_modules = sum(f(r, "calc_n_modules") for r in rs)
        n_cards = sum(f(r, "calc_n_cards_seen") for r in rs)
        card_rate = n_cards / n_modules if n_modules else None

        n_elig5 = sum(f(r, "calc_n_elig_s5") for r in rs)
        n_spec = sum(f(r, "calc_n_specimens") for r in rs)
        spec_rate = n_spec / n_elig5 if n_elig5 else None

        days = {r.get("q1_10_visit_date") for r in rs}
        per_day = len(rs) / max(len(days), 1)

        pts = defaultdict(int)
        for r in rs:
            pts[(round(f(r, "calc_gps_lat"), 4), round(f(r, "calc_gps_lon"), 4))] += 1
        max_same_point = max(pts.values()) if pts else 0

        # impossible sequences: an interview starting before the previous ended,
        # or within T_GAP_MINUTES of it
        overlaps = 0
        seq = sorted((r for r in rs if r.get("start") and r.get("end")),
                     key=lambda r: r["start"])
        for a, b in zip(seq, seq[1:]):
            try:
                gap = (datetime.fromisoformat(b["start"]) - datetime.fromisoformat(a["end"])).total_seconds() / 60
            except ValueError:
                continue
            if gap < T_GAP_MINUTES:
                overlaps += 1

        reasons = []
        if med_dur and med_dur < T_DURATION_MEDIAN:
            reasons.append(f"median duration {med_dur:.0f} min (survey median {survey_median_dur:.0f})")
        if card_rate is not None and card_rate < T_CARD_RATE and n_modules >= 5:
            reasons.append(f"cards seen {card_rate:.0%} of {int(n_modules)} child modules")
        if per_day >= T_VOLUME_PER_DAY:
            reasons.append(f"{per_day:.1f} households/day")
        if max_same_point >= T_GPS_IDENTICAL:
            reasons.append(f"{max_same_point} households share one GPS point to 4 dp")
        if overlaps:
            reasons.append(f"{overlaps} interviews start within {T_GAP_MINUTES} min of the previous one ending")
        if spec_rate is not None and spec_rate < T_SPECIMEN_RATE and n_elig5 >= 5:
            reasons.append(f"specimens obtained for {spec_rate:.0%} of eligible children")

        if reasons:
            # Escalation composite. Either single signal has innocent
            # explanations - a run of small households is quick, a poor ward
            # genuinely has few retained cards. Together they do not: an
            # interview too short to have happened, in which no card was ever
            # produced, is the reported incident. Volume is a supporting reason,
            # not part of the composite, because an enumerator working a dense
            # urban block legitimately clears more households in a day.
            too_quick = bool(med_dur) and med_dur < T_DURATION_MEDIAN
            no_cards = card_rate is not None and card_rate < T_CARD_RATE and n_modules >= 5
            never_moved = max_same_point >= T_GPS_IDENTICAL
            severity = (
                "INVESTIGATE TODAY"
                if (too_quick and no_cards) or (too_quick and never_moved)
                else "REVIEW"
            )
            findings.append({
                "enumerator": e, "team": team, "n": len(rs), "severity": severity,
                "median_duration": round(med_dur, 1),
                "card_rate": None if card_rate is None else round(card_rate, 3),
                "per_day": round(per_day, 1),
                "reasons": reasons,
            })

    summary = {
        "submissions": len(rows),
        "enumerators": len(by_enum),
        "survey_median_duration": round(survey_median_dur, 1),
        "as_of": as_of or "all data",
    }
    findings.sort(key=lambda x: (x["severity"] != "INVESTIGATE TODAY", x["median_duration"]))
    return findings, summary


def report(findings, summary) -> None:
    print("=" * 88)
    print("DAILY FIELDWORK QA REPORT")
    print("=" * 88)
    print(f"  data through        : {summary['as_of']}")
    print(f"  submissions         : {summary['submissions']:,}")
    print(f"  enumerators active  : {summary['enumerators']}")
    print(f"  survey median duration: {summary['survey_median_duration']} min")
    print()
    urgent = [x for x in findings if x["severity"] == "INVESTIGATE TODAY"]
    print(f"  INVESTIGATE TODAY   : {len(urgent)}")
    print(f"  REVIEW              : {len(findings) - len(urgent)}")
    print()
    if not findings:
        print("  No enumerator met any flag threshold.")
        return
    for x in findings:
        print(f"  [{x['severity']}]  {x['enumerator']}  (team {x['team']}, {x['n']} interviews)")
        for r in x["reasons"]:
            print(f"        - {r}")
        print()
    if urgent:
        print("-" * 88)
        print("ACTION: the supervisor for each enumerator above accompanies them on their next")
        print("        three interviews today, and re-visits two completed households to verify")
        print("        the household existed and the interview took place.")
        print("-" * 88)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--input")
    ap.add_argument("--day", help="report on data up to this date, e.g. 2026-06-04")
    a = ap.parse_args()

    if a.demo:
        rows = synthesise()
        print("(demo data: 96 enumerators over 14 days; ENU042 is fabricating)\n")
    elif a.input:
        rows = load_csv(a.input)
    else:
        ap.error("give --demo or --input")

    findings, summary = run_checks(rows, a.day)
    report(findings, summary)

    if a.demo:
        caught = [x for x in findings if x["enumerator"] == "ENU042"]
        print("\nDEMO SELF-CHECK")
        ok = bool(caught) and caught[0]["severity"] == "INVESTIGATE TODAY"
        print(f"  ENU042 flagged as INVESTIGATE TODAY : {'YES' if ok else 'NO'}")
        others = [x for x in findings if x["severity"] == "INVESTIGATE TODAY" and x["enumerator"] != "ENU042"]
        print(f"  false positives at that severity    : {len(others)}")
        return 0 if ok and not others else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
