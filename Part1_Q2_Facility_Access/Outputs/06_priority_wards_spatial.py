"""
Stage 06 -- give the priority ward table a geometry.

`reports/05_priority_wards.csv` is the ranked intervention list from stage 05. It
is a flat table keyed on `ward_code` and cannot be mapped. This stage joins it to
the ward polygons already carried in `data/ward_access.gpkg` and writes the
result back into that same GeoPackage as a second layer, `priority_wards`.

Design decisions worth stating:

  * **The CSV is the authoritative attribute source.** Eight column names appear
    in both inputs (ward_name, lga_name, sen_district, state_name,
    total_population, coverage_fraction, travel_min_to_adequate_median,
    staff_per_10k). Rather than let the join emit `_x`/`_y` suffixes, the CSV's
    copies are kept and the reference layer contributes only geometry plus the
    three code columns the CSV lacks (lga_code, sen_code, state_code). The output
    attribute table is therefore the priority table exactly as stage 05 produced
    it, plus keys and a geometry.

  * **The join is validated, not assumed.** Every ward_code in the CSV must find
    exactly one polygon. An unmatched or duplicated key aborts the write rather
    than silently producing a layer with missing geometries.

  * **The write is idempotent.** `mode="a"` on a GeoPackage appends *rows* to an
    existing layer, so re-running this script would double the feature count.
    The layer is written through pyogrio with the GDAL layer-creation option
    `OVERWRITE=YES`, which replaces the layer in place. `ward_access` is never
    touched -- it is read, and asserted to still hold its 620 features after the
    write.

  * **CRS is inherited from the reference layer** (EPSG:4326) and asserted after
    the round trip. No reprojection: this layer is for mapping and for joining,
    and stage 04 already does all measurement in the projected analysis CRS.

Run:  python 06_priority_wards_spatial.py
"""

from __future__ import annotations

import sys

import geopandas as gpd
import pandas as pd
import pyogrio

from common import ART, banner, get_logger

LOG = get_logger("06_priority_wards_spatial")

REF_LAYER = "ward_access"
OUT_LAYER = "priority_wards"

# Contributed by the reference layer. Everything else comes from the CSV.
KEYS_FROM_REF = ["ward_code", "lga_code", "sen_code", "state_code", "geometry"]


def main() -> int:
    banner(LOG, "STAGE 06 | PRIORITY WARDS -> SPATIAL LAYER")

    gpkg = ART["access_gpkg"]
    csv = ART["priority_wards"]

    # ---------------------------------------------------------------- read
    ref = gpd.read_file(gpkg, layer=REF_LAYER)
    LOG.info("Reference layer %s: %d features, CRS %s, %s",
             REF_LAYER, len(ref), ref.crs, sorted(set(ref.geom_type)))

    pri = pd.read_csv(csv, encoding="utf-8")
    LOG.info("Priority table %s: %d rows, %d columns",
             csv.name, len(pri), pri.shape[1])

    # ---------------------------------------------------------- validate key
    missing_cols = [c for c in KEYS_FROM_REF if c not in ref.columns]
    if missing_cols:
        LOG.error("Reference layer is missing expected columns: %s", missing_cols)
        return 1
    if "ward_code" not in pri.columns:
        LOG.error("Priority table has no ward_code column; nothing to join on")
        return 1

    dup_ref = ref.ward_code[ref.ward_code.duplicated()].tolist()
    dup_pri = pri.ward_code[pri.ward_code.duplicated()].tolist()
    if dup_ref or dup_pri:
        LOG.error("ward_code is not unique -- reference: %s, priority: %s",
                  dup_ref[:5], dup_pri[:5])
        return 1

    unmatched = sorted(set(pri.ward_code) - set(ref.ward_code))
    if unmatched:
        LOG.error("%d ward_code(s) in the priority table have no polygon: %s",
                  len(unmatched), unmatched[:10])
        return 1
    LOG.info("Join key validated: %d/%d priority wards matched a unique polygon "
             "(%d wards in the reference layer are not on the priority list)",
             len(pri), len(pri), len(ref) - len(pri))

    # ---------------------------------------------------------------- join
    # Left join from the CSV so the output preserves priority_rank order and
    # carries only the 592 wards stage 05 actually ranked.
    overlap = sorted(set(pri.columns) & set(ref.columns) - {"ward_code"})
    LOG.info("Columns present in both inputs, kept from the CSV: %s", overlap)

    out = pri.merge(ref[KEYS_FROM_REF], on="ward_code", how="left", validate="1:1")
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=ref.crs)

    # put the identifying columns first, geometry last
    front = ["priority_rank", "ward_code", "ward_name", "lga_code", "lga_name",
             "sen_code", "sen_district", "state_code", "state_name"]
    ordered = [c for c in front if c in out.columns]
    ordered += [c for c in out.columns if c not in ordered and c != "geometry"]
    out = out[ordered + ["geometry"]]

    if out.geometry.isna().any():
        LOG.error("%d feature(s) ended up with null geometry", int(out.geometry.isna().sum()))
        return 1
    n_invalid = int((~out.geometry.is_valid).sum())
    if n_invalid:
        LOG.warning("%d geometry/geometries are invalid in the source polygons; "
                    "carried through unchanged rather than silently repaired", n_invalid)

    # priority_rank is a rank over all 620 wards that stage 05 then filtered to
    # the 592 needing intervention, so the sequence legitimately has gaps where a
    # well-served ward was dropped. Only duplicate ranks would be a real defect.
    rank = out.priority_rank
    if rank.duplicated().any():
        LOG.error("priority_rank contains duplicates: %s",
                  rank[rank.duplicated()].tolist()[:10])
        return 1
    LOG.info("priority_rank runs %d..%d over %d wards (%d gaps, one per ward "
             "ranked by stage 05 but not on the priority list)",
             int(rank.min()), int(rank.max()), len(rank),
             int(rank.max()) - len(rank))

    # ---------------------------------------------------------------- write
    existed = OUT_LAYER in [layer[0] for layer in pyogrio.list_layers(gpkg)]
    LOG.info("Writing layer %s into %s (%s)", OUT_LAYER, gpkg.name,
             "replacing the existing layer" if existed else "new layer")
    # OVERWRITE=YES replaces this layer only. geopandas' mode="a" would append
    # rows to it instead, so re-running would double the feature count.
    pyogrio.write_dataframe(out, gpkg, layer=OUT_LAYER, OVERWRITE="YES")

    # ---------------------------------------------------------------- verify
    back = gpd.read_file(gpkg, layer=OUT_LAYER)
    ref_after = gpd.read_file(gpkg, layer=REF_LAYER)
    layers = [layer[0] for layer in pyogrio.list_layers(gpkg)]

    ok = True
    checks = [
        ("both layers present", set(layers) == {REF_LAYER, OUT_LAYER}, layers),
        (f"{OUT_LAYER} feature count", len(back) == len(pri), f"{len(back)} vs {len(pri)}"),
        (f"{OUT_LAYER} CRS preserved", back.crs == ref.crs, str(back.crs)),
        (f"{OUT_LAYER} all geometries present", not back.geometry.isna().any(), ""),
        (f"{OUT_LAYER} attribute count", back.shape[1] == out.shape[1],
         f"{back.shape[1]} vs {out.shape[1]}"),
        (f"{REF_LAYER} untouched", len(ref_after) == len(ref), f"{len(ref_after)} vs {len(ref)}"),
    ]
    for name, passed, detail in checks:
        LOG.info("  %s  %s%s", "PASS" if passed else "FAIL", name,
                 f"  [{detail}]" if detail else "")
        ok &= bool(passed)

    if not ok:
        LOG.error("Verification failed; do not use the written layer")
        return 1

    LOG.info("")
    LOG.info("%s: %d polygons, %d attributes, CRS %s",
             OUT_LAYER, len(back), back.shape[1] - 1, back.crs)
    LOG.info("Total population on the priority list: %s",
             f"{int(back.total_population.sum()):,}")
    LOG.info("Gap types: %s", back.gap_type.value_counts().to_dict())
    banner(LOG, "STAGE 06 COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
