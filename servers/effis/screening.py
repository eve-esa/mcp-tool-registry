"""Fire-event screening: source-labelled assumptions, not an inventory model.

IPCC Eq. 2.27 uses area * consumed dry matter/ha * emission factor. Consumed
fuel already represents biomass times combustion completeness: do not multiply
by a second combustion factor. All outputs are gross release estimates.
"""

import json
import math
from datetime import UTC, datetime, timedelta

import httpx

IPCC = "https://www.ipcc-nggip.iges.or.jp/public/2006gl/pdf/4_Volume4/V4_02_Ch2_Generic.pdf"
ANDREAE = "https://doi.org/10.1029/2000GB001382"
AR6 = "https://www.ipcc.ch/report/ar6/wg1/downloads/report/IPCC_AR6_WGI_Chapter07.pdf"
EPA = "https://www.epa.gov/greenvehicles/greenhouse-gas-emissions-typical-passenger-vehicle"
VERSION = "screening-v1: IPCC 2006 / Andreae-Merlet 2001 / AR6 WGI 2021"

# Low/high are deliberately selected sensitivity scenarios, NOT the table SE/SD,
# confidence bounds or a claim of complete uncertainty. Central values use the
# named representative row; transfers between land-cover/fuel classes are proxies.
FUEL = {
    "tree_cover:tropical": (42.2, 83.9, 160.4, "Low: all secondary tropical; central: primary tropical; high: primary moist tropical. Slash-and-burn rows transferred to wildfire"),
    "tree_cover:temperate": (16.0, 19.8, 53.0, "Low: eucalypt prescribed surface; central: other temperate wildfire; high: eucalypt wildfire"),
    "tree_cover:boreal": (21.6, 52.8, 87.5, "Boreal low: surface fire; central: wildfire general; high: land clearing"),
    "shrubland": (5.7, 14.3, 26.7, "Low: sagebrush; central: all shrublands; high: shrubland general"),
    "grassland": (2.1, 4.1, 10.0, "Grassland, mid/late dry-season; alternative consumption scenarios"),
    "cropland": (4.0, 5.5, 10.0, "Rice residues central; wheat/maize alternative crop-residue scenarios"),
    "herbaceous_wetland": (2.1, 4.1, 10.0, "Grassland proxy; not a wetland/peat combustion measurement"),
    "mangroves": (42.2, 83.9, 160.4, "Secondary/primary/moist tropical forest proxy; not measured mangrove fuel load"),
    "moss_lichen": (2.1, 4.1, 10.0, "Grassland proxy; not a measured moss/lichen fuel load"),
}
# IPCC 2006 Table 2.5 means, g gas/kg dry matter. PM2.5: Andreae & Merlet
# Table 1 (2001), g particles/kg dry matter, agricultural waste column for crops.
EF = {
    "tropical_forest": {"CO2": 1580.0, "CO": 104.0, "CH4": 6.8, "N2O": 0.20, "PM2_5": 9.1},
    "extratropical_forest": {"CO2": 1569.0, "CO": 107.0, "CH4": 4.7, "N2O": 0.26, "PM2_5": 13.0},
    "savanna_grassland": {"CO2": 1613.0, "CO": 65.0, "CH4": 2.3, "N2O": 0.21, "PM2_5": 5.4},
    "agricultural_residues": {"CO2": 1515.0, "CO": 92.0, "CH4": 2.7, "N2O": 0.07, "PM2_5": 3.9},
}
GWP100 = {"CO2": 1.0, "CH4": 27.0, "N2O": 273.0}  # AR6 WGI 2021 Table 7.15, non-fossil CH4.
NON_FUEL = {"built_up", "bare_sparse", "snow_ice", "water"}
FUELS = {k for k in FUEL if not k.startswith("tree_cover:")} | {"tree_cover"}


def _error(message, error_type="invalid_input", **extra):
    return json.dumps({"ok": False, "error_type": error_type, "error": message, **extra}, allow_nan=False)


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("Numeric inputs cannot be booleans")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("Numeric inputs must be finite and nonnegative (positive for area/totals)")
    return value


def _bbox(value):
    west, south, east, north = [float(v) for v in value.split(",")]
    if not all(math.isfinite(v) for v in (west, south, east, north)) or not (
        -180 <= west < east <= 180 and -90 <= south < north <= 90
    ):
        raise ValueError("Use finite west,south,east,north without antimeridian crossing")
    box = west, south, east, north
    if _area(box) <= 0:
        raise ValueError("Degenerate physical bbox area")
    return box


def _date(value):
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("Use an ISO YYYY-MM-DD date")
    return parsed


def _area(box):
    west, south, east, north = box
    return 6371.0**2 * math.radians(east - west) * (math.sin(math.radians(north)) - math.sin(math.radians(south))) * 100


def _range(values):
    return {k: float(f"{v:.3g}") for k, v in zip(["low", "central", "high"], values, strict=True)}


PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_STATS = "https://planetarycomputer.microsoft.com/api/data/v1/item/statistics"
WORLD = {10: "tree_cover", 20: "shrubland", 30: "grassland", 40: "cropland", 50: "built_up",
         60: "bare_sparse", 70: "snow_ice", 80: "water", 90: "herbaceous_wetland", 95: "mangroves", 100: "moss_lichen"}


async def _items(client, collection, box, period=None):
    query = {"collections": [collection], "bbox": list(box), "limit": 50}
    if period:
        query["datetime"] = period
    response = await client.post(PC_SEARCH, json=query)
    response.raise_for_status()
    data = response.json()
    features = data["features"]
    if not isinstance(features, list) or any(link.get("rel") == "next" for link in data.get("links", [])):
        raise ValueError("Search is incomplete/paginated; narrow bbox instead of assuming complete coverage")
    matched = data.get("numberMatched", data.get("context", {}).get("matched", len(features)))
    if matched > len(features) or len(features) >= 50:
        raise ValueError("Search truncated; no complete estimate")
    return features


def _pieces(items, box):
    if not items or len(items) > 8:
        raise ValueError("No suitable data or too many tiles; narrow the bbox")
    pieces, identifiers = [], set()
    for item in items:
        if not isinstance(item["id"], str) or item["id"] in identifiers:
            raise ValueError("Invalid/duplicate tile identifier")
        identifiers.add(item["id"])
        tile = _bbox(",".join(str(v) for v in item["bbox"]))
        west, south = max(box[0], tile[0]), max(box[1], tile[1])
        east, north = min(box[2], tile[2]), min(box[3], tile[3])
        if west >= east or south >= north:
            continue
        piece = (west, south, east, north)
        for _, previous in pieces:
            if max(west, previous[0]) < min(east, previous[2]) and max(south, previous[1]) < min(north, previous[3]):
                raise ValueError("Overlapping tile envelopes; cannot guarantee nonduplicated area")
        pieces.append((item, piece))
    if not pieces or abs(sum(_area(p) for _, p in pieces) / _area(box) - 1) > 1e-6:
        raise ValueError("Incomplete spatial tile coverage")
    return pieces


async def _histogram(client, collection, item, asset, piece):
    west, south, east, north = piece
    feature = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
        [[west, south], [east, south], [east, north], [west, north], [west, south]]]}}
    response = await client.post(PC_STATS, params={"collection": collection, "item": item["id"],
                                 "assets": asset, "categorical": "true", "max_size": 1024}, json=feature)
    response.raise_for_status()
    statistics = response.json()["properties"]["statistics"]
    if len(statistics) != 1:
        raise ValueError("Ambiguous raster statistics")
    stats = next(iter(statistics.values()))
    valid = _number(stats["valid_percent"])
    counts, values = stats["histogram"]
    if not 99.99 <= valid <= 100 or not counts or len(counts) != len(values):
        raise ValueError("Incomplete/invalid categorical coverage")
    counts = [_number(v) for v in counts]
    values = [_number(v) for v in values]
    if any(not v.is_integer() for v in values):
        raise ValueError("Invalid categorical codes")
    total = _number(sum(counts), positive=True)
    return counts, [int(v) for v in values], total


async def get_burn_fuel_types(bbox: str, fire_date: str | None = None) -> str:
    """WorldCover bbox proportions, not composition of only the burnt pixels.

    Use EFFIS bbox with buffer_km=0, or a labelled aggregate bbox. WorldCover
    2020/2021 reference maps are proxies, not guaranteed pre-fire observations.
    Requires complete search/tile envelopes and >=99.99% valid statistics;
    paginated, missing or overlapping coverage is unavailable, not zero fuel.
    """
    try:
        box = _bbox(bbox)
        fire = _date(fire_date) if fire_date is not None else None
        year = 2020 if fire is not None and fire.year <= 2021 else 2021
    except (ValueError, TypeError, AttributeError):
        return _error("Use a valid bbox and optional ISO fire date")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            items = await _items(client, "esa-worldcover", box)
            selected = [item for item in items if f"_{year}_" in item["id"]]
            pieces = _pieces(selected, box)
            hectares = {}
            for item, piece in pieces:
                counts, values, total = await _histogram(client, "esa-worldcover", item, "map", piece)
                for count, code in zip(counts, values, strict=True):
                    if code not in WORLD:
                        raise ValueError("Unknown/nodata land-cover class")
                    name = WORLD[code]
                    hectares[name] = hectares.get(name, 0) + _area(piece) * count / total
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return _error("WorldCover unavailable/incomplete; narrow bbox or provide explicit documented shares",
                      "fuel_unavailable", provider_http_status=exc.response.status_code
                      if isinstance(exc, httpx.HTTPStatusError) else None)
    burnable = {k: v for k, v in hectares.items() if k not in NON_FUEL and v > 0}
    total = sum(burnable.values())
    if not total:
        return _error("No supported burnable land-cover classes in valid map statistics", "fuel_unavailable")
    pre_fire = year < fire.year if fire is not None else None
    choice = (f"{year} map reference year precedes fire year; not event-specific pre-fire observation" if pre_fire
              else f"{year} map (not pre-fire for this event; temporal proxy)" if fire
              else f"{year} map; fire date unknown, pre-fire relation unknown")
    return json.dumps({
        "ok": True, "bbox": bbox, "fire_date": fire_date, "worldcover_year": year, "pre_fire_map": pre_fire,
        "map_choice": choice, "source": f"ESA WorldCover {year} {'v100' if year == 2020 else 'v200'} via Planetary Computer",
        "box_hectares_by_class": hectares, "fuel_shares_pct": {k: 100 * v / total for k, v in burnable.items()},
        "tile_ids": [item["id"] for item, _ in pieces], "coverage_policy": "complete tile envelopes; >=99.99% valid stats",
        "note": "Whole-bbox categorical proportions, not only burnt pixels; downsampled statistics are approximate",
        "warnings": ["Land-cover/fuel and temporal transfers are screening assumptions, not verified event fuel measurements.",
                     "Categorical downsampling and spherical bbox-area weighting introduce approximation."],
        "attribution": f"© ESA WorldCover project {year} / Contains modified Copernicus Sentinel data ({year}) processed by ESA WorldCover consortium",
        "source_url": "https://doi.org/10.5281/zenodo.5571936" if year == 2020 else "https://doi.org/10.5281/zenodo.7254221",
    }, allow_nan=False)


async def _mapping_coverage(client, item, piece, first, last):
    """Screen supported QA/interval assets; independent sampled histograms, not a pixelwise uncertainty model."""
    qa_counts, qa_values, _ = await _histogram(client, "modis-64A1-061", item, "QA", piece)
    first_counts, first_values, _ = await _histogram(client, "modis-64A1-061", item, "First_Day", piece)
    last_counts, last_values, _ = await _histogram(client, "modis-64A1-061", item, "Last_Day", piece)
    first_values = [value for count, value in zip(first_counts, first_values, strict=True) if count > 0]
    last_values = [value for count, value in zip(last_counts, last_values, strict=True) if count > 0]
    year_days = first.replace(month=12, day=31).timetuple().tm_yday
    if any(not 1 <= value <= year_days for value in first_values + last_values):
        raise ValueError("Unsupported reliable mapping interval")
    shortened = sum(count for count, value in zip(qa_counts, qa_values, strict=True) if value & 4)
    invalid = sum(count for count, value in zip(qa_counts, qa_values, strict=True)
                  if value > 255 or value & 3 != 3)
    first_max, last_min = max(first_values), min(last_values)
    covered = (not shortened and not invalid and first_max <= first.timetuple().tm_yday
               and last_min >= last.timetuple().tm_yday)
    return {"tile_id": item["id"], "status": "sampled_month_covered" if covered else "incomplete",
            "shortened_sample_count": shortened, "invalid_or_nonland_sample_count": invalid,
            "max_reliable_first_day": first_max, "min_reliable_last_day": last_min,
            "requested_first_day": first.timetuple().tm_yday, "requested_last_day": last.timetuple().tm_yday,
            "assets": ["QA", "First_Day", "Last_Day"],
            "source_url": "https://lpdaac.usgs.gov/documents/1006/MCD64_User_Guide_V61.pdf",
            "basis": "MCD64A1 v6.1 User Guide §3.1.2; categorical sampled statistics, not full-resolution QA certainty"}


async def detect_burned_area(bbox: str, month: str) -> str:
    """Approximate monthly MCD64A1 bbox aggregate, NOT an individual perimeter.

    Useful as an explicitly labelled fallback. Multiple recorded fires/pixels
    are combined; earliest/latest/peak days are aggregate dates, not event IDs.
    Requires complete search/tile envelopes, >=99.99% valid statistics and
    sampled QA/reliable intervals covering the month. Shortened mapping is
    unavailable, not a complete zero month. No full-resolution QA, worldwide
    availability, small-fire detection or accuracy guarantee is established.
    """
    try:
        box = _bbox(bbox)
        if len(month) == 10:
            _date(month)
        elif len(month) != 7:
            raise ValueError("Use YYYY-MM or an ISO date")
        first = _date(month[:7] + "-01")
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if first > datetime.now(UTC).date():
            raise ValueError("Future month")
    except (ValueError, TypeError, AttributeError, OverflowError):
        return _error("Use a valid bbox and past/current YYYY-MM month")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            items = await _items(client, "modis-64A1-061", box,
                                 f"{first}T00:00:00Z/{last}T23:59:59Z")
            pieces = _pieces(items, box)
            day_hectares, coverage = {}, []
            for item, piece in pieces:
                quality = await _mapping_coverage(client, item, piece, first, last)
                if quality["status"] != "sampled_month_covered":
                    return _error("Reliable sampled mapping does not cover the requested month; no complete-month aggregate computed",
                                  "temporal_coverage_incomplete", bbox=bbox, month=first.strftime("%Y-%m"),
                                  temporal_coverage={"status": "incomplete", "tiles": [quality]})
                coverage.append(quality)
                counts, values, total = await _histogram(client, "modis-64A1-061", item, "Burn_Date", piece)
                for count, value in zip(counts, values, strict=True):
                    if value == 0 or count == 0:
                        continue
                    if not first.timetuple().tm_yday <= value <= last.timetuple().tm_yday:
                        raise ValueError("Burn day outside requested month or unmapped data")
                    day_hectares[value] = day_hectares.get(value, 0) + _area(piece) * count / total
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return _error("MODIS monthly data unavailable/incomplete; no aggregate computed", "burned_area_unavailable",
                      provider_http_status=exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
    hectares = sum(day_hectares.values())
    result = {
        "ok": True, "scope_kind": "monthly_bbox_aggregate", "bbox": bbox, "month": first.strftime("%Y-%m"),
        "burned_area_ha": float(f"{hectares:.3g}"), "burned_pct_of_box": 100 * hectares / _area(box),
        "source": "NASA MODIS MCD64A1 v061 (500 m), Microsoft Planetary Computer",
        "source_url": "https://doi.org/10.5067/MODIS/MCD64A1.061", "tile_ids": [item["id"] for item, _ in pieces],
        "date_semantics": "Earliest/latest/peak recorded burn day across the monthly aggregate, not individual event dates",
        "coverage_policy": "complete tile envelopes; >=99.99% valid statistics; sampled QA/intervals cover requested month",
        "temporal_coverage": {"status": "sampled_month_covered", "tiles": coverage,
                              "scope": "Independent categorical sampled QA/interval statistics; not full-resolution or complete uncertainty evidence"},
        "method": "Spherical clipped-bbox hectares × categorical positive Burn_Date fraction; downsampled approximation",
        "note": "Monthly detected-pixel aggregate may combine multiple fires; not an EFFIS event, polygon or exposure input",
        "warnings": ["No individual perimeter or worldwide availability/detection guarantee.",
                     "500 m sampling and categorical downsampling can miss unsampled QA conditions; no full-resolution or algorithm-uncertainty guarantee.",
                     "No burned pixels is not proof no fire occurred; product availability can lag the month."],
    }
    if day_hectares:
        def day_date(value):
            return (first.replace(month=1, day=1) + timedelta(days=value - 1)).isoformat()

        result.update(first_burn_date=day_date(min(day_hectares)), last_burn_date=day_date(max(day_hectares)),
                      peak_burn_date=day_date(max(day_hectares, key=day_hectares.get)))
    else:
        result["message"] = "No burned pixels detected in sampled statistics with observed month-coverage checks; not proof no fire occurred"
    return json.dumps(result, allow_nan=False)


async def estimate_fire_emissions(
    burned_area_ha: float, bbox: str, fuel_shares_pct: dict[str, float] | None = None,
    fire_date: str | None = None,
) -> str:
    """Sourced gross screening estimates, not measurements or inventory net impact.

    Area is caller-supplied (not authenticated EFFIS provenance). Omitted shares
    use WorldCover bbox proportions, not burnt-pixel composition. Latitude is a
    coarse forest proxy, not mapped biome. Low/high vary consumed fuel only;
    no accuracy percentage or confidence interval is established. Values use
    screening-v1 representative/proxy rows, not every prototype fuel default.
    """
    try:
        box = _bbox(bbox)
        area = _number(burned_area_ha, positive=True)
        if area > _area(box) * 1.05:
            raise ValueError("Burned area exceeds the approximate bbox area (5% envelope tolerance)")
        if fire_date is not None:
            _date(fire_date)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return _error("Supply a finite positive area compatible with the bbox and a valid optional ISO date")
    fuel_source = "caller-supplied shares; not independently authenticated or clipped to burnt pixels"
    if fuel_shares_pct is None:
        fuels = json.loads(await get_burn_fuel_types(bbox, fire_date))
        if not fuels["ok"]:
            return json.dumps(fuels)
        fuel_shares_pct = fuels["fuel_shares_pct"]
        fuel_source = f"WorldCover {fuels['worldcover_year']} bbox proxy, not burnt-pixel composition"
    try:
        if not isinstance(fuel_shares_pct, dict) or not fuel_shares_pct:
            raise ValueError("Supply fuel shares")
        weights = {k: _number(v) for k, v in fuel_shares_pct.items()}
        if set(weights) - FUELS - NON_FUEL:
            raise ValueError("Unknown fuel classes")
        shares = {k: v for k, v in weights.items() if k not in NON_FUEL and v > 0}
        total = _number(sum(shares.values()), positive=True)
    except (ValueError, TypeError, OverflowError):
        return _error("Use finite nonnegative shares with at least one supported burnable fuel", allowed=sorted(FUELS))
    latitude = (box[1] + box[3]) / 2
    biome = "tropical" if abs(latitude) < 23.5 else ("boreal" if abs(latitude) > 50 else "temperate")
    tonnes = {k: [0.0, 0.0, 0.0] for k in EF["tropical_forest"]}
    by_fuel = {}
    for fuel, weight in shares.items():
        assigned_area = area * (weight / total)
        key = f"tree_cover:{biome}" if fuel == "tree_cover" else fuel
        low, central, high, row = FUEL[key]
        category = "savanna_grassland"
        if fuel in {"tree_cover", "mangroves"}:
            category = "tropical_forest" if fuel == "mangroves" or biome == "tropical" else "extratropical_forest"
        elif fuel == "cropland":
            category = "agricultural_residues"
        factors = EF[category]
        for gas, factor in factors.items():
            for i, consumed in enumerate([low, central, high]):
                tonnes[gas][i] += assigned_area * consumed * factor / 1000
        by_fuel[fuel] = {
            "area_ha": assigned_area, "fuel_burned_t_per_ha": central,
            "co2_t_central": float(f"{assigned_area * central * factors['CO2'] / 1000:.3g}"),
            "fuel_consumption_t_dry_matter_ha": {"low": low, "central": central, "high": high},
            "consumption_basis": {"source": IPCC, "table": "2.4", "row_or_transfer": row},
            "emission_factor_class": category, "emission_factors_g_per_kg_dry_matter": factors,
        }
    co2e = [sum(tonnes[g][i] * value for g, value in GWP100.items()) for i in range(3)]
    return json.dumps({
        "ok": True, "method_version": VERSION, "burned_area_ha": area, "bbox": bbox, "fire_date": fire_date,
        "area_provenance": "caller supplied; no independent EFFIS/event authentication", "fuel_source": fuel_source,
        "biome": biome, "biome_method": "absolute-latitude heuristic (<23.5 tropical, >50 boreal), not mapped ecozone",
        "fuel_shares_pct_used": {k: 100 * v / total for k, v in shares.items()},
        "excluded_non_fuel_classes": sorted(set(weights) & NON_FUEL),
        "emissions_tonnes": {g: _range(v) for g, v in tonnes.items()}, "co2_equivalent_tonnes": _range(co2e),
        "car_years_equivalent": _range([v / 4.6 for v in tonnes["CO2"]]), "by_fuel_type": by_fuel,
        "range_type": "fuel-consumption sensitivity, not confidence intervals",
        "method": "area ha × consumed dry matter t/ha × emission factor g/kg ÷ 1000 = gas/particle tonnes",
        "gwp100": {"values": GWP100, "version": "AR6 WGI 2021 Table 7.15, non-fossil CH4", "source": AR6},
        "sources": [{"url": IPCC, "section": "Eq. 2.27 and Tables 2.4/2.5"},
                    {"url": ANDREAE, "section": "Table 1 PM2.5 means"}, {"url": EPA, "basis": "4.6 t tailpipe CO2/car-year"}],
        "warnings": ["SCREENING estimate, not measured emissions or validated accuracy.",
                     "Low/high are selected alternative fuel-consumption scenarios, not statistical bounds or complete uncertainty.",
                     "Fuel transfers and latitude-based forest classes are explicit proxies; soil/peat combustion is excluded.",
                     "Gross CO2/CO2e release is not net climate impact, an inventory total, offset or guaranteed regrowth.",
                     "Car-years compare gross CO2 with typical tailpipe CO2 only, not life-cycle equivalence."],
        "caveats": "Screening assumptions; sensitivity is not confidence. No claimed accuracy percentage.",
    }, allow_nan=False)
