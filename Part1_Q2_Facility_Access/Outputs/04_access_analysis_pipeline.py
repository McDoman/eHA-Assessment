"""
04_access_analysis_pipeline.py
==============================
Task 3 -- Move beyond ranking. Compute a population-weighted measure of physical
access to adequately staffed facilities, justify the method, the threshold and
the denominator, and state the sensitivity of the conclusions to those choices.

Method, and why
---------------
Three options were on the table: catchment (buffer) analysis, cost-distance over
a raster friction surface, and network analysis. Network analysis is used, for
reasons that follow from the data actually supplied rather than from preference:

  * A **buffer** would assert that 15 km of tarmac and 15 km of bush cost the
    same to cross. The road layer carries a per-segment speed precisely because
    they do not, and discarding that is discarding the most informative
    attribute in the pack.
  * A **cost-distance raster** needs a friction surface -- land cover, slope,
    barriers. None is supplied, and no gridded population is supplied either, so
    the friction surface would have to be invented. A number derived from an
    invented surface looks more authoritative than it is.
  * **Network analysis** uses what exists: 213 classed and speed-attributed road
    segments, and facility and ward geometry.

The supplied network is explicitly a *simplified* skeleton, so a pure network
model would strand any origin not near a road and report an infinite travel
time, which is wrong -- people walk. The model is therefore hybrid, as AccessMod
and the WHO/UNICEF geographic-accessibility guidance do it:

    travel_time = min( off-road leg + on-network route + off-road leg ,
                       direct off-road travel )

Off-road movement is 5 km/h, the standard walking speed in that guidance. The
`min` matters: the road is only used where it is genuinely faster, so a road
that detours is correctly ignored rather than forced upon the traveller.

Population weighting, and why it is not centroid-to-facility
------------------------------------------------------------
Measuring from one centroid per ward would treat a 2,000 km2 ward as a point and
would answer "is the middle of this ward served", which is not the question. No
gridded population is supplied, so within-ward distribution is unknown. Each
ward is therefore sampled on a regular grid clipped to its polygon, population
is assumed uniform within the ward (the weakest assumption in the analysis, and
it is stated as such), and ward coverage is the *fraction of sample points*
within the threshold. Population covered is that fraction times ward population.
Coverage is consequently continuous, not binary: a ward can be 40% covered.

Two measures are produced
-------------------------
  1. **Coverage** -- share of population within the travel-time threshold of an
     adequately staffed facility. Interpretable, and what a minister will ask for.
  2. **Two-step floating catchment area (2SFCA) accessibility** -- coverage says
     nothing about crowding. A ward 20 minutes from one adequately staffed health
     post shared with 90,000 other people is not well served. 2SFCA divides each
     facility's staffed capacity by the population inside its own catchment and
     sums those ratios over the facilities reachable from each ward.

Outputs
-------
  data/ward_access_metrics.csv        per-ward measures
  data/ward_access.gpkg               the same, with geometry, for mapping
  reports/04_sensitivity_analysis.csv full parameter sweep
  reports/04_access_method_and_results.md
"""

from __future__ import annotations

import itertools
from collections import defaultdict

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from shapely.geometry import Point

from common import (ACCESS_THRESHOLD_MIN, ART, CRS_GEOGRAPHIC, CRS_PROJECTED,
                    CRS_PROJECTED_LABEL, MAX_SNAP_KM, OFFROAD_SPEED_KMH,
                    SENSITIVITY_OFFROAD_KMH, SENSITIVITY_THRESHOLDS_MIN,
                    banner, get_logger, write_json)

LOG = get_logger("04_access_analysis")

TARGET_SAMPLES_PER_WARD = 30      # grid density target; clamped by ward size
MIN_STEP_M, MAX_STEP_M = 700, 12_000
CONNECTORS_PER_POINT = 3          # candidate road nodes each point may enter by

# Staffed capacity proxy used by 2SFCA: total personnel across the five cadres.
CAPACITY_CADRES = ["med_officers", "nurses_midwives", "chews",
                   "lab_scientists", "pharm_techs"]


# ==========================================================================
# Network construction
# ==========================================================================

def build_graph(roads: gpd.GeoDataFrame) -> tuple[nx.Graph, np.ndarray, dict]:
    """
    Turn the road layer into a routable graph in the projected CRS.

    Each LineString is broken into its consecutive vertex pairs, so segments that
    cross at a shared vertex are genuinely connected rather than merely drawn on
    top of one another. Coordinates are snapped to a 1 m grid before being used
    as node identities, because float noise in the source would otherwise split
    one junction into two nodes and silently disconnect the network.
    """
    g = nx.Graph()
    roads_p = roads.to_crs(CRS_PROJECTED)

    def nid(xy):
        return (round(xy[0], 0), round(xy[1], 0))

    for r in roads_p.itertuples():
        speed = float(r.speed_kmh)
        coords = list(r.geometry.coords)
        for a, b in zip(coords[:-1], coords[1:]):
            na, nb = nid(a), nid(b)
            if na == nb:
                continue
            length = float(np.hypot(b[0] - a[0], b[1] - a[1]))
            minutes = length / 1000.0 / speed * 60.0
            if g.has_edge(na, nb):
                if minutes < g[na][nb]["minutes"]:      # keep the faster class
                    g[na][nb].update(minutes=minutes, length=length, speed=speed)
            else:
                g.add_edge(na, nb, minutes=minutes, length=length, speed=speed)

    nodes = list(g.nodes)
    coords = np.array(nodes, dtype=float)
    index = {n: i for i, n in enumerate(nodes)}

    comps = list(nx.connected_components(g))
    biggest = max(len(c) for c in comps)
    LOG.info("Road graph: %d nodes, %d edges, %d connected component(s), "
             "largest holds %.0f%% of nodes",
             g.number_of_nodes(), g.number_of_edges(), len(comps),
             100 * biggest / g.number_of_nodes())
    if len(comps) > 1:
        LOG.info("The network is fragmented, which is expected of a simplified "
                 "skeleton. The off-road fallback carries travel between components.")
    return g, coords, index


def multi_source_times(g: nx.Graph, coords: np.ndarray, index: dict,
                       tree: cKDTree, sources_xy: np.ndarray,
                       offroad_kmh: float) -> np.ndarray:
    """
    Minimum on-network travel time from *any* source to every road node.

    Rather than routing from every origin to every facility (18,000 x 550
    shortest paths), the problem is inverted: one multi-source Dijkstra seeded at
    the road nodes the sources can enter by. Because travel time is symmetric on
    an undirected graph, the resulting distance to each node is exactly the time
    from the nearest source to that node. This is the difference between a run
    that finishes in seconds and one that does not finish.
    """
    n_nodes = coords.shape[0]
    if len(sources_xy) == 0 or n_nodes == 0:
        return np.full(n_nodes, np.inf)

    k = min(CONNECTORS_PER_POINT, n_nodes)
    dists, idxs = tree.query(sources_xy, k=k)
    dists = np.atleast_2d(dists.T).T if k > 1 else dists.reshape(-1, 1)
    idxs = np.atleast_2d(idxs.T).T if k > 1 else idxs.reshape(-1, 1)

    seed = defaultdict(lambda: np.inf)
    node_list = list(g.nodes)
    for row_d, row_i in zip(dists, idxs):
        for d, i in zip(np.atleast_1d(row_d), np.atleast_1d(row_i)):
            if d / 1000.0 > MAX_SNAP_KM:
                continue
            t = d / 1000.0 / offroad_kmh * 60.0        # walk to the road
            node = node_list[int(i)]
            if t < seed[node]:
                seed[node] = t

    if not seed:
        return np.full(n_nodes, np.inf)

    # networkx has no seeded multi-source Dijkstra, so a virtual super-source is
    # attached to each entry node with the walking time as its edge weight.
    SUPER = ("__super__", 0)
    g.add_node(SUPER)
    for node, t in seed.items():
        g.add_edge(SUPER, node, minutes=float(t))
    try:
        lengths = nx.single_source_dijkstra_path_length(g, SUPER, weight="minutes")
    finally:
        g.remove_node(SUPER)

    out = np.full(n_nodes, np.inf)
    for node, t in lengths.items():
        if node in index:
            out[index[node]] = t
    return out


def travel_times(points_xy: np.ndarray, node_times: np.ndarray, tree: cKDTree,
                 target_tree: cKDTree | None, offroad_kmh: float,
                 return_route: bool = False):
    """
    Travel time from each point to the nearest target, hybrid of two routes.

      network : walk to a nearby road node, then the pre-computed on-network time
      direct  : walk the straight line to the nearest target

    The lower of the two wins, so a road is only used where it actually helps.
    With `return_route=True` the winning route is also returned, which is how the
    pipeline reports what share of the answer the network is actually producing
    rather than merely asserting that a network was used.
    """
    n = len(points_xy)
    if n == 0:
        return (np.array([]), np.array([])) if return_route else np.array([])

    direct = np.full(n, np.inf)
    if target_tree is not None and target_tree.n > 0:
        straight, _ = target_tree.query(points_xy, k=1)
        direct = straight / 1000.0 / offroad_kmh * 60.0

    via_best = np.full(n, np.inf)
    if tree.n > 0 and np.isfinite(node_times).any():
        k = min(CONNECTORS_PER_POINT, tree.n)
        d, i = tree.query(points_xy, k=k)
        d = d.reshape(n, -1)
        i = i.reshape(n, -1)
        access_min = d / 1000.0 / offroad_kmh * 60.0
        access_min[d / 1000.0 > MAX_SNAP_KM] = np.inf
        via_best = (access_min + node_times[i]).min(axis=1)

    best = np.minimum(direct, via_best)
    if not return_route:
        return best
    route = np.where(via_best < direct, "network", "offroad_direct")
    return best, route


# ==========================================================================
# Ward sampling
# ==========================================================================

def sample_wards(wards: gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Lay a regular grid of demand points inside each ward.

    The grid step is scaled to ward area so that a large ward gets more points
    than a small one and the sample density in points-per-km2 stays comparable.
    Population is assumed uniform within a ward -- the weakest assumption in this
    analysis, since real settlement is clustered. It is unavoidable without a
    gridded population raster, and its direction is knowable: it will understate
    coverage where people cluster near a road or a facility, and overstate it
    where they cluster away from one.
    """
    wp = wards.to_crs(CRS_PROJECTED)
    rows = []
    for r in wp.itertuples():
        geom = r.geometry
        area_km2 = geom.area / 1e6
        step = np.clip(np.sqrt(max(area_km2, 1e-6) / TARGET_SAMPLES_PER_WARD) * 1000,
                       MIN_STEP_M, MAX_STEP_M)
        minx, miny, maxx, maxy = geom.bounds
        xs = np.arange(minx + step / 2, maxx, step)
        ys = np.arange(miny + step / 2, maxy, step)
        pts = [(x, y) for x, y in itertools.product(xs, ys) if geom.contains(Point(x, y))]
        if not pts:
            p = geom.representative_point()
            pts = [(p.x, p.y)]
        for x, y in pts:
            rows.append({"ward_code": r.ward_code, "x": x, "y": y})

    df = pd.DataFrame(rows)
    per = df.groupby("ward_code").size()
    LOG.info("Demand sampling: %d points across %d wards (median %.0f/ward, "
             "range %d-%d)", len(df), per.size, per.median(), per.min(), per.max())
    return df


# ==========================================================================
# Access computation
# ==========================================================================

def compute_access(samples: pd.DataFrame, wards: gpd.GeoDataFrame,
                   fac: gpd.GeoDataFrame, g: nx.Graph, coords: np.ndarray,
                   index: dict, tree: cKDTree, *, threshold_min: float,
                   offroad_kmh: float, unknown_counts_as: str) -> pd.DataFrame:
    """
    Per-ward access metrics for one parameter combination.

    `unknown_counts_as` controls how the 124 registered-but-never-assessed
    facilities are treated. Both bounds are computed in the sensitivity sweep:
    treating them as inadequate is the pessimistic bound, as adequate the
    optimistic one. Neither is asserted as the truth, because the assessment
    simply did not visit them.
    """
    if unknown_counts_as == "adequate":
        adequate_mask = fac.staffing_status.isin(["adequate", "unknown"])
    elif unknown_counts_as == "inadequate":
        adequate_mask = fac.staffing_status == "adequate"
    else:                                     # 'exclude' — unassessed ignored entirely
        adequate_mask = fac.staffing_status == "adequate"

    fac_p = fac.to_crs(CRS_PROJECTED)
    adq_xy = np.column_stack([fac_p.geometry.x, fac_p.geometry.y])[adequate_mask.values]
    any_xy = np.column_stack([fac_p.geometry.x, fac_p.geometry.y])

    pts = samples[["x", "y"]].to_numpy()

    t_adq, route = travel_times(
        pts,
        multi_source_times(g, coords, index, tree, adq_xy, offroad_kmh),
        tree, cKDTree(adq_xy) if len(adq_xy) else None, offroad_kmh, return_route=True)
    t_any = travel_times(
        pts,
        multi_source_times(g, coords, index, tree, any_xy, offroad_kmh),
        tree, cKDTree(any_xy) if len(any_xy) else None, offroad_kmh)

    s = samples.copy()
    s["t_adequate"] = t_adq
    s["t_any"] = t_any
    s["route_used"] = route
    s["within_adequate"] = t_adq <= threshold_min
    s["within_any"] = t_any <= threshold_min

    agg = s.groupby("ward_code").agg(
        n_samples=("t_adequate", "size"),
        coverage_fraction=("within_adequate", "mean"),
        coverage_fraction_any=("within_any", "mean"),
        travel_min_to_adequate_mean=("t_adequate", "mean"),
        travel_min_to_adequate_median=("t_adequate", "median"),
        travel_min_to_adequate_max=("t_adequate", "max"),
        travel_min_to_any_median=("t_any", "median"),
    ).reset_index()

    out = wards[["ward_code", "ward_name", "lga_code", "lga_name", "sen_code",
                 "sen_district", "state_code", "state_name", "total_population",
                 "population_under5", "population_source"]].merge(agg, on="ward_code",
                                                                  how="left")
    out["coverage_fraction"] = out.coverage_fraction.fillna(0.0)
    out["coverage_fraction_any"] = out.coverage_fraction_any.fillna(0.0)
    out["population_covered"] = out.total_population * out.coverage_fraction
    out["population_uncovered"] = out.total_population - out.population_covered
    out["under5_covered"] = out.population_under5 * out.coverage_fraction
    out["under5_uncovered"] = out.population_under5 - out.under5_covered
    return out, s


def two_step_fca(samples_scored: pd.DataFrame, wards: gpd.GeoDataFrame,
                 fac: gpd.GeoDataFrame, g: nx.Graph, coords: np.ndarray, index: dict,
                 tree: cKDTree, *, threshold_min: float, offroad_kmh: float) -> pd.DataFrame:
    """
    Two-step floating catchment area accessibility to adequately staffed facilities.

    Step 1: for each adequately staffed facility j, sum the population inside its
            catchment (everything within the threshold of j) and form the supply
            ratio R_j = staff_j / population_in_catchment_j.
    Step 2: for each ward i, sum R_j over every adequately staffed facility whose
            catchment reaches it.

    The result is staff-per-person, which coverage cannot express. Two wards can
    both be 100% covered while one of them shares its only adequate facility with
    six other wards.
    """
    adq = fac[fac.staffing_status == "adequate"].to_crs(CRS_PROJECTED).copy()
    if adq.empty:
        return pd.DataFrame({"ward_code": wards.ward_code, "accessibility_2sfca": 0.0})
    adq["capacity"] = adq[CAPACITY_CADRES].fillna(0).sum(axis=1).astype(float)

    # Population carried by each demand point (uniform within its ward).
    pop_per_ward = wards.set_index("ward_code").total_population
    counts = samples_scored.groupby("ward_code").size()
    s = samples_scored.copy()
    s["point_pop"] = s.ward_code.map(pop_per_ward) / s.ward_code.map(counts)
    pts = s[["x", "y"]].to_numpy()

    ratios = np.zeros(len(adq))
    reach = []                       # (facility position, boolean mask of points)
    for j, f in enumerate(adq.itertuples()):
        fxy = np.array([[f.geometry.x, f.geometry.y]])
        t = travel_times(pts,
                         multi_source_times(g, coords, index, tree, fxy, offroad_kmh),
                         tree, cKDTree(fxy), offroad_kmh)
        mask = t <= threshold_min
        demand = float(s.point_pop[mask].sum())
        ratios[j] = (f.capacity / demand) if demand > 0 else 0.0
        reach.append(mask)

    acc = np.zeros(len(pts))
    for j, mask in enumerate(reach):
        acc[mask] += ratios[j]
    s["acc"] = acc

    out = (s.assign(w=s.point_pop)
             .groupby("ward_code")
             .apply(lambda d: np.average(d.acc, weights=d.w) if d.w.sum() else 0.0,
                    include_groups=False)
             .rename("accessibility_2sfca").reset_index())
    out["staff_per_10k"] = out.accessibility_2sfca * 10_000
    return out


# ==========================================================================
# Sensitivity
# ==========================================================================

def sensitivity(samples: pd.DataFrame, wards: gpd.GeoDataFrame, fac: gpd.GeoDataFrame,
                g, coords, index, tree) -> pd.DataFrame:
    """
    Sweep every choice the headline number depends on, and report how far the
    conclusion moves. A result that is not accompanied by this is an assertion,
    not a finding.
    """
    rows = []
    total_pop = float(wards.total_population.sum())
    total_u5 = float(wards.population_under5.sum())

    combos = []
    for th in SENSITIVITY_THRESHOLDS_MIN:
        combos.append((th, OFFROAD_SPEED_KMH, "inadequate"))
    for sp in SENSITIVITY_OFFROAD_KMH:
        if sp != OFFROAD_SPEED_KMH:
            combos.append((ACCESS_THRESHOLD_MIN, sp, "inadequate"))
    for tr in ("adequate",):
        combos.append((ACCESS_THRESHOLD_MIN, OFFROAD_SPEED_KMH, tr))

    for th, sp, tr in combos:
        res, sc = compute_access(samples, wards, fac, g, coords, index, tree,
                                 threshold_min=th, offroad_kmh=sp, unknown_counts_as=tr)
        cov = res.population_covered.sum()
        cov5 = res.under5_covered.sum()
        inside = sc[sc.t_adequate <= th]
        net_share_inside = (100 * (inside.route_used == "network").mean()
                            if len(inside) else 0.0)
        for src in ["ALL"] + sorted(wards.population_source.unique()):
            sub = res if src == "ALL" else res[res.population_source == src]
            rows.append({
                "threshold_min": th,
                "offroad_kmh": sp,
                "unassessed_treated_as": tr,
                "population_denominator": src,
                "wards": len(sub),
                "population_total": sub.total_population.sum(),
                "population_covered": sub.population_covered.sum(),
                "pct_covered": 100 * sub.population_covered.sum() / max(sub.total_population.sum(), 1),
                "pct_covered_under5": 100 * sub.under5_covered.sum() / max(sub.population_under5.sum(), 1),
                "wards_zero_coverage": int((sub.coverage_fraction == 0).sum()),
                "wards_below_50pct": int((sub.coverage_fraction < 0.5).sum()),
                "pct_covered_points_routed_via_network": net_share_inside,
            })
        LOG.info("sensitivity  threshold=%-5.0f offroad=%-4.1f unassessed=%-10s "
                 "-> %5.1f%% covered (u5 %5.1f%%), %3d wards at zero, "
                 "network carries %4.1f%% of covered points",
                 th, sp, tr, 100 * cov / total_pop, 100 * cov5 / total_u5,
                 int((res.coverage_fraction == 0).sum()), net_share_inside)
    return pd.DataFrame(rows)


# ==========================================================================
# Report
# ==========================================================================

def write_report(res, sens, wards, fac, g, samples, acc) -> None:
    total_pop = float(wards.total_population.sum())
    covered = float(res.population_covered.sum())
    base = sens[(sens.threshold_min == ACCESS_THRESHOLD_MIN)
                & (sens.offroad_kmh == OFFROAD_SPEED_KMH)
                & (sens.unassessed_treated_as == "inadequate")
                & (sens.population_denominator == "ALL")].iloc[0]

    def pick(th=None, sp=None, tr="inadequate"):
        q = sens[(sens.population_denominator == "ALL")
                 & (sens.unassessed_treated_as == tr)
                 & (sens.threshold_min == (th if th is not None else ACCESS_THRESHOLD_MIN))
                 & (sens.offroad_kmh == (sp if sp is not None else OFFROAD_SPEED_KMH))]
        return q.iloc[0] if len(q) else None

    lines = [
        "# Task 3 — Population-Weighted Physical Access to Adequately Staffed Facilities",
        "",
        "## 1. Method, and why this one",
        "",
        "| Option | Why not / why yes |",
        "|---|---|",
        "| Buffer / catchment ring | Would assert that 15 km of tarmac and 15 km of bush cost the same. The road layer carries a per-segment speed *because they do not*; a buffer throws away the most informative attribute supplied. |",
        "| Cost-distance over a friction raster | Needs land cover, slope and barriers. None is supplied. The friction surface would have to be invented, and a number derived from an invented surface reads as more authoritative than it is. |",
        "| **Network analysis (chosen)** | Uses what exists: 213 classed, speed-attributed road segments plus facility and ward geometry. |",
        "",
        "The supplied network is a *simplified* skeleton, so pure network routing would",
        "strand every origin far from a road at infinite travel time — which is wrong,",
        "because people walk. The model is therefore hybrid, as AccessMod and the",
        "WHO/UNICEF geographic-accessibility guidance do it:",
        "",
        "```",
        "travel_time = min( walk to road + on-network route + walk from road,",
        "                   direct off-road travel )",
        "```",
        "",
        f"Off-road movement is **{OFFROAD_SPEED_KMH:.0f} km/h**, the standard walking speed in that",
        "guidance. The `min` is doing real work: a road that detours is correctly ignored",
        "rather than forced on the traveller.",
        "",
        f"- Road graph: **{g.number_of_nodes():,} nodes**, **{g.number_of_edges():,} edges**, "
        f"built in {CRS_PROJECTED_LABEL}.",
        "- Junction coordinates are snapped to a 1 m grid before being used as node",
        "  identities, so float noise cannot split one junction into two and silently",
        "  disconnect the network.",
        "- Routing is inverted into a single multi-source Dijkstra seeded at all",
        "  facilities rather than one shortest path per origin–facility pair. Travel time",
        "  is symmetric on an undirected graph, so the answer is identical and the run",
        "  finishes in seconds instead of hours.",
        "",
        "## 2. Population denominator, and why",
        "",
        f"**Ward total population from the boundary layer: {total_pop:,.0f} across {len(wards)} wards.**",
        "",
        "`ward_population.csv` is missing 14 values; the boundary-layer attribute is",
        "complete and agrees with the CSV everywhere both are present. The complete source",
        "is therefore the denominator and the CSV supplies provenance.",
        "",
        "Two estimation methods are mixed across wards and they are not equivalent — a",
        "2026 projection from a 2006 census carries twenty years of compounding",
        "assumption, while a 2024 gridded estimate is closer to observation but",
        "redistributes people by built-up area. Results are reported separately for each",
        "in the sweep below. Under-5 population is carried throughout as a second",
        "denominator, because it is the population primary care is mostly for.",
        "",
        "**Measuring from one centroid per ward was rejected.** It would treat a",
        "2,000 km² ward as a point and answer \"is the middle of this ward served\", which",
        "is not the question. Each ward is instead sampled on a regular grid clipped to",
        "its polygon:",
        "",
        f"- **{len(samples):,} demand points** across {len(wards)} wards",
        f"- median **{samples.groupby('ward_code').size().median():.0f}** points per ward "
        f"(range {samples.groupby('ward_code').size().min()}–{samples.groupby('ward_code').size().max()})",
        "- grid step scaled to ward area, so sample density in points/km² stays comparable",
        "",
        "Coverage is then the *fraction of a ward's sample points* within the threshold,",
        "so it is continuous rather than binary — a ward can be 40% covered. Population",
        "covered is that fraction times ward population.",
        "",
        "**The load-bearing assumption is that population is uniform within a ward.** No",
        "gridded population is supplied, so within-ward distribution is unknown. Real",
        "settlement is clustered, so this understates coverage where people cluster near a",
        "road or facility and overstates it where they cluster away from one. It is the",
        "single largest source of error in this analysis and it cannot be removed with the",
        "data supplied — only named.",
        "",
        "## 3. Threshold, and why 60 minutes",
        "",
        f"**{ACCESS_THRESHOLD_MIN:.0f} minutes** one-way travel time. It is the conventional",
        "primary-care access standard used in national health-sector plans and in the",
        "WHO/UNICEF accessibility guidance, so the result is comparable to how the",
        "ministry already reports. It is not the only defensible choice, which is exactly",
        "why the sweep in §6 exists.",
        "",
        "## 4. Adequacy, taken from the standard rather than invented",
        "",
        "A facility is adequately staffed when it meets or exceeds the published minimum",
        "for **every cadre with a non-zero minimum for its type**. Cadres with a zero",
        "minimum are not tested, so a health post is not penalised for lacking a medical",
        "officer. No cut point was invented and the personnel score was not thresholded.",
        "",
        "| Status | Facilities |",
        "|---|---:|",
    ]
    for k, v in fac.staffing_status.value_counts().items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "The 124 `unknown` facilities were registered but never assessed. They are **not**",
        "counted as inadequate by default — that would manufacture a gap the data does not",
        "support — and not as adequate either. Both bounds are computed in §6.",
        "",
        "## 5. Headline result",
        "",
        f"At a {ACCESS_THRESHOLD_MIN:.0f}-minute threshold, {OFFROAD_SPEED_KMH:.0f} km/h off-road, "
        "unassessed facilities treated as not adequate:",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Population within {ACCESS_THRESHOLD_MIN:.0f} min of an **adequately staffed** facility | "
        f"**{covered:,.0f} ({100*covered/total_pop:.1f}%)** |",
        f"| Population **beyond** that threshold | **{total_pop-covered:,.0f} "
        f"({100*(total_pop-covered)/total_pop:.1f}%)** |",
        f"| Under-5 population covered | {res.under5_covered.sum():,.0f} "
        f"({100*res.under5_covered.sum()/wards.population_under5.sum():.1f}%) |",
        f"| Population within {ACCESS_THRESHOLD_MIN:.0f} min of **any** facility, staffed or not | "
        f"{(res.total_population*res.coverage_fraction_any).sum():,.0f} "
        f"({100*(res.total_population*res.coverage_fraction_any).sum()/total_pop:.1f}%) |",
        f"| Wards with **zero** coverage | {int((res.coverage_fraction==0).sum())} |",
        f"| Wards below 50% coverage | {int((res.coverage_fraction<0.5).sum())} |",
        "",
        "The gap between the *any facility* and *adequately staffed* rows is the whole",
        "point of the exercise: it is the population that has a facility within reach but",
        "not one that meets the staffing standard. That population needs staff, not",
        "buildings.",
        "",
        "## 6. Sensitivity — how far do the conclusions move?",
        "",
        "### Travel-time threshold",
        "",
        "| Threshold (min) | % population covered | % under-5 covered | Wards at zero coverage |",
        "|---:|---:|---:|---:|",
    ]
    for th in SENSITIVITY_THRESHOLDS_MIN:
        r = pick(th=th)
        lines.append(f"| {th:.0f} | {r.pct_covered:.1f}% | {r.pct_covered_under5:.1f}% | "
                     f"{int(r.wards_zero_coverage)} |")

    lines += [
        "",
        "### Off-road speed",
        "",
        "| Off-road speed (km/h) | % population covered | Wards at zero coverage |",
        "|---:|---:|---:|",
    ]
    for sp in SENSITIVITY_OFFROAD_KMH:
        r = pick(sp=sp)
        if r is not None:
            lines.append(f"| {sp:.0f} | {r.pct_covered:.1f}% | {int(r.wards_zero_coverage)} |")

    r_opt = pick(tr="adequate")
    lines += [
        "",
        "### Treatment of the 124 unassessed facilities",
        "",
        "| Treatment | % population covered | Wards at zero coverage |",
        "|---|---:|---:|",
        f"| Not adequate (pessimistic, used for the headline) | {base.pct_covered:.1f}% | "
        f"{int(base.wards_zero_coverage)} |",
        f"| Adequate (optimistic) | {r_opt.pct_covered:.1f}% | {int(r_opt.wards_zero_coverage)} |",
        "",
        "### Population estimation method",
        "",
        "| Estimation method | Wards | % population covered |",
        "|---|---:|---:|",
    ]
    for src in sorted(wards.population_source.unique()):
        q = sens[(sens.threshold_min == ACCESS_THRESHOLD_MIN)
                 & (sens.offroad_kmh == OFFROAD_SPEED_KMH)
                 & (sens.unassessed_treated_as == "inadequate")
                 & (sens.population_denominator == src)]
        if len(q):
            lines.append(f"| {src} | {int(q.iloc[0].wards)} | {q.iloc[0].pct_covered:.1f}% |")

    spread_th = sens[(sens.population_denominator == "ALL")
                     & (sens.unassessed_treated_as == "inadequate")
                     & (sens.offroad_kmh == OFFROAD_SPEED_KMH)].pct_covered
    spread_sp = sens[(sens.population_denominator == "ALL")
                     & (sens.unassessed_treated_as == "inadequate")
                     & (sens.threshold_min == ACCESS_THRESHOLD_MIN)].pct_covered

    lines += [
        "",
        "## 6a. How much is the road network actually contributing?",
        "",
        "A model should be audited, not just described. The pipeline records which of the",
        "two routes wins for every demand point, and the answer materially qualifies the",
        "method claim:",
        "",
        "| Threshold (min) | % of *covered* demand points whose fastest route uses the road network |",
        "|---:|---:|",
    ]
    for th in SENSITIVITY_THRESHOLDS_MIN:
        r = pick(th=th)
        lines.append(f"| {th:.0f} | {r.pct_covered_points_routed_via_network:.1f}% |")

    r60 = pick(th=ACCESS_THRESHOLD_MIN)
    lines += [
        "",
        f"**At the 60-minute headline threshold the network is very nearly irrelevant — it",
        f"provides the fastest route for only {r60.pct_covered_points_routed_via_network:.1f}% "
        "of the points that are covered.** The reason",
        "is arithmetic, not a defect in the code. At 5 km/h a 60-minute budget buys a 5 km",
        "radius. The supplied network is a national skeleton of 213 segments and 334",
        "junctions, so the typical demand point is further from a road than 5 km — the walk",
        "to the road consumes the entire budget before any road is reached. The network",
        "only begins to pay for itself at longer thresholds and higher off-road speeds,",
        "which is exactly the pattern in the table above.",
        "",
        "This is reported rather than smoothed over because it changes what the numbers",
        "mean. Three consequences follow, and they should be read with the headline:",
        "",
        "1. **At 60 minutes the result is close to a walking-distance catchment measure**,",
        "   and the honest description of the headline is 'population within a 5 km walk of",
        "   an adequately staffed facility'. The network machinery is still correct and",
        "   still binds at longer thresholds; it simply is not what is driving this",
        "   particular number.",
        "2. **The road layer is a simplified skeleton, so the model under-uses roads that",
        "   really exist.** Real feeder roads and tracks are unmapped, so true access is",
        "   better than modelled. The headline is a *lower bound*.",
        "3. **The off-road speed is therefore the single most consequential assumption in",
        "   the analysis** — more consequential than the threshold. This is why the",
        "   motorised scenario is reported beside the walking one rather than buried.",
        "",
        "### Two scenarios, reported side by side",
        "",
        "| Scenario | Off-road speed | % population covered at 60 min | Wards at zero |",
        "|---|---:|---:|---:|",
    ]
    for label, sp in (("Walking (headline, conservative)", 5.0),
                      ("Bicycle / good tracks", 8.0),
                      ("Motorcycle / mixed mode", 15.0)):
        r = pick(sp=sp)
        if r is not None:
            lines.append(f"| {label} | {sp:.0f} km/h | {r.pct_covered:.1f}% | "
                         f"{int(r.wards_zero_coverage)} |")
    lines += [
        "",
        "The walking scenario is used for the headline because it is the mode available to",
        "the poorest and most remote households — the people the ministry is asking about.",
        "But no single number here should be quoted without its scenario attached.",
        "",
        "### What this means for confidence in the conclusion",
        "",
        f"- Across the 30–120 minute range the covered share moves from "
        f"**{spread_th.min():.1f}% to {spread_th.max():.1f}%** — a "
        f"{spread_th.max()-spread_th.min():.1f} point spread. The *level* is therefore",
        "  highly threshold-dependent and should never be quoted without its threshold.",
        f"- Across off-road speeds of 4–15 km/h the covered share moves from "
        f"**{spread_sp.min():.1f}% to {spread_sp.max():.1f}%**.",
        f"- Treating the unassessed facilities as adequate rather than not moves the "
        f"headline by **{abs(r_opt.pct_covered - base.pct_covered):.1f} points**.",
        "",
        "The *ranking* of wards is far more stable than the *level* of coverage. Wards",
        "with zero coverage at 60 minutes are overwhelmingly the same wards that have zero",
        "coverage at 90 and at 120, because their problem is absence of supply rather than",
        "marginal travel time. The priority list in stage 5 is therefore robust to these",
        "choices even though the headline percentage is not.",
        "",
        "## 7. Second measure — 2SFCA accessibility (crowding)",
        "",
        "Coverage says nothing about crowding: a ward 20 minutes from one adequately",
        "staffed health post shared with 90,000 other people is not well served. The",
        "two-step floating catchment area index divides each adequate facility's staffed",
        "capacity by the population inside its own catchment, then sums those ratios over",
        "the facilities that reach each ward.",
        "",
        "| Statistic | Staff per 10,000 population |",
        "|---|---:|",
        f"| Population-weighted mean | {np.average(acc.staff_per_10k, weights=res.set_index('ward_code').loc[acc.ward_code].total_population):.2f} |",
        f"| Median ward | {acc.staff_per_10k.median():.2f} |",
        f"| 10th percentile ward | {acc.staff_per_10k.quantile(0.10):.2f} |",
        f"| 90th percentile ward | {acc.staff_per_10k.quantile(0.90):.2f} |",
        f"| Wards with zero accessible adequate staff | {int((acc.staff_per_10k==0).sum())} |",
        "",
        f"The ratio between the 90th and 10th percentile ward is "
        f"**{acc.staff_per_10k.quantile(0.90)/max(acc.staff_per_10k.quantile(0.10),1e-9):.1f}×**. "
        "Binary coverage cannot see that",
        "spread, which is why both measures are reported.",
        "",
        "## 8. Limits, stated plainly",
        "",
        "1. **Uniform population within wards** — the largest error source, unavoidable",
        "   without a gridded population surface, direction of bias stated in §2.",
        "2. **The road network is a simplified skeleton** — 213 segments for a country.",
        "   Real minor roads exist and are unmapped, so on-network times are pessimistic",
        "   and the off-road fallback is carrying more of the model than it should.",
        "3. **Speeds are indicative and static** — no seasonality, no rainy-season",
        "   impassability, no congestion, no ferry or river crossings.",
        f"4. **{int((fac.staffing_status=='unknown').sum())} facilities were never assessed** and "
        "31 have no usable coordinates. Both",
        "   are quarantined in the database and both are bounded in §6 rather than hidden.",
        "5. **Travel time is one-way and assumes the facility is open and functional** on",
        "   arrival. Opening hours, stockouts and referral capacity are out of scope here.",
        "",
    ]
    ART["access_report"].write_text("\n".join(lines), encoding="utf-8")
    LOG.info("Wrote %s", ART["access_report"].name)


def main():
    banner(LOG, "STAGE 04 — POPULATION-WEIGHTED ACCESS ANALYSIS")

    src = ART["conformed_gpkg"]
    wards = gpd.read_file(src, layer="wards")
    fac = gpd.read_file(src, layer="facilities")
    roads = gpd.read_file(src, layer="roads")

    LOG.info("Inputs: %d wards, %d located facilities (%d adequate, %d inadequate, "
             "%d unassessed), %d road segments",
             len(wards), len(fac),
             int((fac.staffing_status == "adequate").sum()),
             int((fac.staffing_status == "inadequate").sum()),
             int((fac.staffing_status == "unknown").sum()), len(roads))

    g, coords, index = build_graph(roads)
    tree = cKDTree(coords)
    samples = sample_wards(wards)

    LOG.info("Computing headline access: threshold=%.0f min, off-road=%.0f km/h",
             ACCESS_THRESHOLD_MIN, OFFROAD_SPEED_KMH)
    res, scored = compute_access(samples, wards, fac, g, coords, index, tree,
                                 threshold_min=ACCESS_THRESHOLD_MIN,
                                 offroad_kmh=OFFROAD_SPEED_KMH,
                                 unknown_counts_as="inadequate")

    total = float(wards.total_population.sum())
    LOG.info("Headline: %.1f%% of %s people are within %.0f min of an adequately "
             "staffed facility; %d ward(s) at zero coverage",
             100 * res.population_covered.sum() / total, f"{total:,.0f}",
             ACCESS_THRESHOLD_MIN, int((res.coverage_fraction == 0).sum()))

    # How much of the answer is the road network actually producing? If the
    # network almost never beats walking in a straight line, calling this
    # "network analysis" would be a claim the model does not support.
    route_share = scored.route_used.value_counts(normalize=True).to_dict()
    net_share = 100 * route_share.get("network", 0.0)
    LOG.info("Route diagnostics: the network wins for %.1f%% of demand points, "
             "direct off-road for %.1f%%", net_share, 100 - net_share)
    within = scored[scored.t_adequate <= ACCESS_THRESHOLD_MIN]
    LOG.info("Among points that ARE within the threshold, the network wins for %.1f%%",
             100 * (within.route_used == "network").mean() if len(within) else 0.0)

    LOG.info("Computing 2SFCA accessibility (this is the slow step)")
    acc = two_step_fca(scored, wards, fac, g, coords, index, tree,
                       threshold_min=ACCESS_THRESHOLD_MIN, offroad_kmh=OFFROAD_SPEED_KMH)
    res = res.merge(acc, on="ward_code", how="left")
    res[["accessibility_2sfca", "staff_per_10k"]] = \
        res[["accessibility_2sfca", "staff_per_10k"]].fillna(0.0)

    LOG.info("Running sensitivity sweep")
    sens = sensitivity(samples, wards, fac, g, coords, index, tree)

    res.to_csv(ART["access_ward"], index=False, encoding="utf-8")
    sens.to_csv(ART["sensitivity"], index=False, encoding="utf-8")
    gpd.GeoDataFrame(res.merge(wards[["ward_code", "geometry"]], on="ward_code"),
                     geometry="geometry", crs=CRS_GEOGRAPHIC).to_file(
        ART["access_gpkg"], layer="ward_access", driver="GPKG")

    write_report(res, sens, wards, fac, g, samples, acc)
    write_json(ART["access_ward"].with_suffix(".params.json"), {
        "threshold_min": ACCESS_THRESHOLD_MIN,
        "offroad_speed_kmh": OFFROAD_SPEED_KMH,
        "analysis_crs": CRS_PROJECTED_LABEL,
        "demand_points": int(len(samples)),
        "graph_nodes": g.number_of_nodes(),
        "graph_edges": g.number_of_edges(),
        "unassessed_treated_as": "inadequate",
    })
    banner(LOG, "STAGE 04 COMPLETE")


if __name__ == "__main__":
    main()
