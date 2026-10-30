"""Client and pipeline for the CEDA archive OpenSearch and OPeNDAP services."""

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import numpy as np
import pandas as pd
import requests
import xarray as xr
from dateutil import parser as dateutil_parser
from requests.adapters import HTTPAdapter
from urllib3 import Retry

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "ceda-opensearch-mcp/1.0")

_PARAM_NAMES = {
    "uuid": "uuid",
    "query": "query",
    "max_records": "maximumRecords",
    "start_page": "startPage",
    "start_record": "startRecord",
    "start_date": "startDate",
    "end_date": "endDate",
    "parent_identifier": "parentIdentifier",
    "bbox": "bbox",
    "ecv": "ecv",
    "frequency": "frequency",
    "institute": "institute",
    "processing_level": "processingLevel",
    "product_string": "productString",
    "product_version": "productVersion",
    "project": "project",
    "data_type": "dataType",
    "sensor": "sensor",
    "platform": "platform",
    "file_format": "fileFormat",
    "drs_id": "drsId",
}
_FORMATS = {
    "geojson": "application/geo+json",
    "atom": "application/atom+xml",
}
_LATITUDE_NAMES = ("lat", "latitude", "y")
_LONGITUDE_NAMES = ("lon", "longitude", "x")
_TIME_NAMES = ("time", "time0", "valid_time", "t")


def _http_timeout() -> int:
    return int(os.getenv("HTTP_REQUEST_TIMEOUT", "10"))


def _verify_tls() -> bool:
    return os.getenv("HTTP_REQUEST_TLS_VERIFY", "true").lower() in {
        "true",
        "1",
        "yes",
    }


def _iso_utc(moment: datetime) -> str:
    """Render a datetime as a UTC ISO 8601 string with a trailing Z."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_KEYWORD_WHEN = {
    "now": lambda moment: moment,
    "today": lambda moment: moment,
    "yesterday": lambda moment: moment - timedelta(days=1),
    "tomorrow": lambda moment: moment + timedelta(days=1),
    "this week": lambda moment: moment - timedelta(days=moment.weekday()),
    "last week": lambda moment: moment - timedelta(days=moment.weekday() + 7),
    "this month": lambda moment: moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
    "last month": lambda moment: moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1),
    "this year": lambda moment: moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
    "last year": lambda moment: (
        moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    ),
}
_AGO_PATTERN = re.compile(r"^(\d+)\s*(day|week|month|year)s?\s+ago$")


def parse_when(text: str, now: datetime | None = None) -> str:
    """Normalize ISO 8601 or natural-language dates to ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepted forms include ``2020-06-01T00:00:00Z``, ``2020-06-01``, ``2020-06``,
    ``June 2020``, ``25 June 2020``, ``today``, ``yesterday``, ``3 days ago``,
    ``2 weeks ago``, ``last month``, and ``this year``.
    """
    raw = str(text).strip()
    if not raw:
        raise ValueError("Date is empty; try e.g. 2020-06-01, 'June 2020' or '3 days ago'.")
    moment = now or datetime.now(UTC)
    lowered = raw.lower()
    ago = _AGO_PATTERN.match(lowered)
    if ago:
        amount = int(ago.group(1))
        unit = ago.group(2)
        deltas = {
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
            "month": timedelta(days=30 * amount),
            "year": timedelta(days=365 * amount),
        }
        return _iso_utc(moment - deltas[unit])
    if lowered in _KEYWORD_WHEN:
        return _iso_utc(_KEYWORD_WHEN[lowered](moment))
    try:
        parsed = dateutil_parser.parse(raw, fuzzy=True, dayfirst=False, default=datetime(moment.year, moment.month, 1))
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"Could not understand the date {text!r}; try e.g. 2020-06-01, 'June 2020' or '3 days ago'."
        ) from exc
    return _iso_utc(parsed)


class CedaOpenSearchError(Exception):
    """Raised when a CEDA OpenSearch request or data query fails."""


class CedaOpenSearchClient:
    """Client for CEDA's OpenSearch request endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        verify: bool | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv(
                "CEDA_OPENSEARCH_URL",
                "https://archive.opensearch.ceda.ac.uk/opensearch",
            )
        ).rstrip("/")
        self.timeout = timeout if timeout is not None else _http_timeout()
        self.verify = verify if verify is not None else _verify_tls()
        self._session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.mount("http://", HTTPAdapter(max_retries=retry))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def _get(self, path: str, params: dict[str, Any]) -> requests.Response:
        url = f"{self.base_url}/{path}"
        try:
            response = self._session.get(url, params=params, timeout=self.timeout, verify=self.verify)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CedaOpenSearchError(f"Request to {url} failed: {exc}") from exc
        return response

    def search(self, *, response_format: str = "geojson", **filters: Any) -> dict[str, Any] | str:
        """Search the archive and return GeoJSON or Atom XML.

        Filters use snake_case names mapped to the OpenSearch parameters of the
        description document: ``uuid``, ``query``, ``max_records``, ``start_page``,
        ``start_record``, ``start_date``, ``end_date``, ``parent_identifier``,
        ``bbox``, ``ecv``, ``frequency``, ``institute``, ``processing_level``,
        ``product_string``, ``product_version``, ``project``, ``data_type``,
        ``sensor``, ``platform``, ``file_format``, and ``drs_id``. Omit
        ``parent_identifier`` to search the whole archive.
        """
        if response_format not in _FORMATS:
            raise ValueError(f"response_format must be one of {sorted(_FORMATS)}, got {response_format!r}")
        unknown = sorted(set(filters) - set(_PARAM_NAMES))
        if unknown:
            raise ValueError(f"Unknown search filters: {', '.join(unknown)}")
        params: dict[str, Any] = {"httpAccept": _FORMATS[response_format]}
        params.update({_PARAM_NAMES[key]: value for key, value in filters.items() if value is not None})
        response = self._get("request", params)
        if response_format == "atom":
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise CedaOpenSearchError("Response was not valid JSON") from exc

    def search_datasets(self, **filters: Any) -> list[dict[str, Any]]:
        """Search for dataset entries (uuid, title, date) with any filter."""
        result = self.search(**filters)
        if isinstance(result, str):
            raise CedaOpenSearchError("Expected GeoJSON from the CEDA service")
        return dataset_entries(result)

    def find_datasets(
        self,
        ecv: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, str]]:
        """Find CCI datasets for an essential climate variable."""
        datasets: list[dict[str, str]] = []
        page_size = 100
        start_record = 1
        while True:
            result = self.search(
                parent_identifier="cci",
                ecv=ecv,
                start_date=start_date,
                end_date=end_date,
                max_records=page_size,
                start_record=start_record,
            )
            if isinstance(result, str):
                raise CedaOpenSearchError("Expected GeoJSON from the CEDA service")
            features = result.get("features", [])
            datasets.extend(
                {
                    "uuid": feature["properties"]["identifier"],
                    "title": feature["properties"]["title"],
                }
                for feature in features
            )
            start_record += len(features)
            if len(features) < page_size or start_record > result.get("totalResults", 0):
                break
        return datasets

    def list_files(
        self,
        dataset_uuid: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List a dataset's files, including their OPeNDAP URLs.

        ``limit`` bounds the number of files returned; ``None`` (the default)
        pages through the whole result set and returns every file.
        """
        files: list[dict[str, Any]] = []
        start_record = 1
        while limit is None or len(files) < limit:
            result = self.search(
                parent_identifier=dataset_uuid,
                start_date=start_date,
                end_date=end_date,
                max_records=100 if limit is None else min(100, limit - len(files)),
                start_record=start_record,
            )
            if isinstance(result, str):
                raise CedaOpenSearchError("Expected GeoJSON from the CEDA service")
            features = result.get("features", [])
            files.extend(_file_entry(feature) for feature in features)
            start_record += len(features)
            if not features or start_record > result.get("totalResults", 0):
                break
        return files if limit is None else files[:limit]


def _file_entry(feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties", {})
    links = {link.get("title", "").lower(): link.get("href") for link in properties.get("links", {}).get("related", [])}
    return {
        "title": properties.get("title"),
        "date": properties.get("date"),
        "filesize": properties.get("filesize"),
        "opendap": links.get("opendap"),
    }


def _coord_name(dataset: xr.Dataset, candidates: tuple[str, ...]) -> str | None:
    """Return the first coordinate name present among the candidates."""
    return next((name for name in candidates if name in dataset.coords), None)


def open_subset(
    opendap_url: str,
    latitude: float | None = None,
    longitude: float | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> xr.Dataset:
    """Open and subset a remote dataset by point or bounding box.

    Coordinate names are detected flexibly (``lat``/``latitude``/``y`` and
    ``lon``/``longitude``/``x``) so datasets beyond CCI's ``lat``/``lon``
    naming still work.
    """
    dataset = xr.open_dataset(opendap_url, engine="netcdf4")
    latitude_name = _coord_name(dataset, _LATITUDE_NAMES)
    longitude_name = _coord_name(dataset, _LONGITUDE_NAMES)
    if latitude is not None and longitude is not None and latitude_name and longitude_name:
        return dataset.sel({latitude_name: latitude, longitude_name: longitude}, method="nearest")
    if bbox is not None and latitude_name and longitude_name:
        west, south, east, north = bbox
        latitudes = dataset[latitude_name]
        latitude_slice = slice(south, north) if latitudes[0] < latitudes[-1] else slice(north, south)
        return dataset.sel({latitude_name: latitude_slice, longitude_name: slice(west, east)})
    return dataset


def dataset_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract identifier/title/date entries from a GeoJSON search response."""
    entries: list[dict[str, Any]] = []
    for feature in result.get("features", []):
        properties = feature.get("properties", {})
        entries.append(
            {
                "uuid": properties.get("identifier"),
                "title": properties.get("title"),
                "date": properties.get("date"),
            }
        )
    return entries


def _concat_dim(parts: list[xr.Dataset]) -> str:
    """Pick the dimension to join file parts along (a time-like one if present)."""
    for name in _TIME_NAMES:
        if all(name in part.dims or name in part.coords for part in parts):
            return name
    return str(parts[0].dims[0])


def run_extraction(
    client: CedaOpenSearchClient,
    *,
    dataset_uuid: str | None = None,
    dataset_index: int | None = None,
    max_files: int | None = None,
    max_datasets: int = 1,
    variables: list[str] | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    area: tuple[float, float, float, float] | None = None,
    **search_filters: Any,
) -> list[dict[str, Any]]:
    """Search the archive with arbitrary filters and load OPeNDAP subsets.

    ``search_filters`` accepts any snake_case OpenSearch parameter. When
    ``dataset_uuid`` is given the search is skipped entirely. ``variables``
    filters data variables by case-insensitive substring match.
    """
    if dataset_uuid is not None:
        datasets: list[dict[str, Any]] = [{"uuid": dataset_uuid, "title": dataset_uuid}]
    else:
        datasets = []
        page_size = 100
        start_record = 1
        while True:
            page = client.search_datasets(max_records=page_size, start_record=start_record, **search_filters)
            datasets.extend(page)
            if len(page) < page_size:
                break
            start_record += len(page)
        if not datasets:
            raise CedaOpenSearchError("No datasets matched the search filters")
        if dataset_index is not None:
            if not 0 <= dataset_index < len(datasets):
                raise CedaOpenSearchError(f"dataset_index must be between 0 and {len(datasets) - 1}")
            datasets = [datasets[dataset_index]]
    datasets = datasets[:max_datasets]

    start_date = search_filters.get("start_date")
    end_date = search_filters.get("end_date")
    results: list[dict[str, Any]] = []
    for dataset in datasets:
        files = [
            item for item in client.list_files(dataset["uuid"], start_date, end_date, max_files) if item["opendap"]
        ]
        if not files:
            continue
        parts = [open_subset(item["opendap"], latitude, longitude, area).load() for item in files[:max_files]]
        data = xr.concat(parts, dim=_concat_dim(parts)) if len(parts) > 1 else parts[0]
        if variables:
            matched = [name for name in data.data_vars if any(token.lower() in name.lower() for token in variables)]
            if not matched:
                available = ", ".join(list(data.data_vars)[:20])
                raise ValueError(f"None of {variables} matched the dataset variables: {available}")
            data = data[matched]
        results.append({"uuid": dataset["uuid"], "title": dataset["title"], "data": data})
    if not results:
        raise CedaOpenSearchError("No OPeNDAP files were available for the selected datasets")
    return results


def _round(value: float) -> float:
    """Round to 6 significant digits to keep tool output compact."""
    return float(f"{value:.6g}")


def numeric_variables(dataset: xr.Dataset) -> list[str]:
    """Names of numeric data variables, preserving dataset order."""
    return [name for name in dataset.data_vars if np.issubdtype(dataset[name].dtype, np.number)]


def variable_statistics(values: np.ndarray) -> dict[str, float]:
    """NaN-safe summary statistics (n, mean, std, min, p05, median, p95, max)."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {}
    return {
        "n": float(finite.size),
        "mean": float(finite.mean()),
        "std": float(finite.std()),
        "min": float(finite.min()),
        "p05": float(np.percentile(finite, 5)),
        "median": float(np.median(finite)),
        "p95": float(np.percentile(finite, 95)),
        "max": float(finite.max()),
    }


def compute_stats_text(dataset: xr.Dataset, max_variables: int | None = None) -> str:
    """Render one compact statistics line per numeric variable (all by default)."""
    lines: list[str] = []
    names = numeric_variables(dataset)
    selected = names if max_variables is None else names[:max_variables]
    for name in selected:
        variable = dataset[name]
        stats = variable_statistics(np.asarray(variable.values, dtype=float).ravel())
        if not stats:
            lines.append(f"{name}: no finite values")
            continue
        units = str(variable.attrs.get("units", ""))
        label = f"{name} [{units}]" if units else name
        summary = " ".join(
            [f"n={int(stats['n'])}"] + [f"{key}={_round(value)}" for key, value in stats.items() if key != "n"]
        )
        lines.append(f"{label}: {summary}")
    if max_variables is not None and len(names) > max_variables:
        lines.append(f"(+{len(names) - max_variables} more variables: {', '.join(names[max_variables:])})")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    """Format one table cell compactly (empty for NaN/NaT/None, 6 significant digits)."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    if isinstance(value, (bool, np.bool_)):
        return str(value)
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.6g}"
    if hasattr(value, "strftime"):
        # pandas.NaT advertises strftime but raises when it is called; blank
        # every datetime-like cell that pandas considers missing.
        if pd.isna(value):
            return ""
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, np.datetime64):
        return "" if np.isnat(value) else str(value)
    return str(value)


def render_sample(dataset: xr.Dataset, max_rows: int | None = None) -> tuple[str, int]:
    """Render records as pipe-delimited text (all rows and columns by default).

    ``max_rows`` optionally bounds the output; when it hides rows, a trailing
    note reports how many were not shown. Returns the text block and the
    number of rows actually rendered.
    """
    frame = dataset.to_dataframe()
    if any(name is not None for name in frame.index.names):
        frame = frame.reset_index()
    columns = [str(column) for column in frame.columns]
    shown = frame if max_rows is None else frame.head(max_rows)
    lines = [" | ".join(columns)]
    for _, row in shown.iterrows():
        lines.append(" | ".join(_cell(row[column]) for column in columns))
    if max_rows is not None:
        truncated = len(frame) - len(shown)
        if truncated > 0:
            lines.append(f"(truncated: {truncated} more rows not shown)")
    return "\n".join(lines), len(shown)


def describe_text(dataset: xr.Dataset, max_variables: int = 20, max_attributes: int = 8) -> str:
    """Render the structure of a dataset for LLM consumption."""
    dimensions = ", ".join(f"{name}={size}" for name, size in dataset.sizes.items())
    lines = [f"Dimensions: {dimensions or 'none'}"]
    coordinates = []
    for name, coord in dataset.coords.items():
        values = np.asarray(coord.values).ravel()
        if values.size and np.issubdtype(values.dtype, np.integer):
            coordinates.append(f"{name} [{int(values.min())}..{int(values.max())}]")
        elif values.size and np.issubdtype(values.dtype, np.number):
            coordinates.append(f"{name} [{_round(float(values.min()))}..{_round(float(values.max()))}]")
        elif values.size:
            coordinates.append(f"{name} [{_cell(values[0])}..{_cell(values[-1])}]")
        else:
            coordinates.append(name)
    lines.append("Coordinates: " + (", ".join(coordinates) or "none"))
    lines.append("Variables:")
    names = list(dataset.data_vars)
    for name in names[:max_variables]:
        variable = dataset[name]
        pieces = [f"  - {name} ({', '.join(str(dim) for dim in variable.dims)}) {variable.dtype}"]
        units = variable.attrs.get("units")
        long_name = variable.attrs.get("long_name")
        if units:
            pieces.append(f"[{units}]")
        if long_name:
            pieces.append(str(long_name))
        lines.append(" ".join(pieces))
    if len(names) > max_variables:
        lines.append(f"  (+{len(names) - max_variables} more variables: {', '.join(names[max_variables:])})")
    attributes = []
    for key, value in list(dataset.attrs.items())[:max_attributes]:
        text = str(value)
        attributes.append(f"{key}={text[:80]}{'…' if len(text) > 80 else ''}")
    lines.append("Global attributes: " + ("; ".join(attributes) or "none"))
    return "\n".join(lines)


async def _http_get(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> requests.Response:
    """Perform a blocking HTTP request in a worker thread."""

    def request() -> requests.Response:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=_http_timeout(),
            verify=_verify_tls(),
        )
        response.raise_for_status()
        return response

    return await asyncio.to_thread(request)


async def geocode_local(place_name: str) -> str:
    """Return a Nominatim bounding box for a place as JSON."""
    try:
        response = await _http_get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": place_name,
                "format": "json",
                "limit": "1",
                "addressdetails": "1",
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
        )
        results = response.json()
    except (requests.RequestException, ValueError) as exc:
        return json.dumps({"error": f"Geocoding failed: {exc}"})
    if not results:
        return json.dumps({"error": f"No results found for '{place_name}'."})

    south, north, west, east = (float(value) for value in results[0]["boundingbox"])
    return json.dumps({"bbox": f"{west},{south},{east},{north}"})


def run_pipeline(
    client: CedaOpenSearchClient,
    ecv: str,
    start_date: str,
    end_date: str,
    latitude: float | None,
    longitude: float | None,
    bbox: tuple[float, float, float, float] | None,
    dataset_uuid: str | None,
    dataset_index: int | None,
    max_files: int | None,
) -> list[dict[str, Any]]:
    """Find datasets and load their selected OPeNDAP data."""
    if dataset_uuid is not None:
        datasets = [{"uuid": dataset_uuid, "title": dataset_uuid}]
    else:
        datasets = client.find_datasets(ecv, start_date, end_date)
        if not datasets:
            raise CedaOpenSearchError(f"No datasets found for ECV {ecv}")
        if dataset_index is not None:
            if not 0 <= dataset_index < len(datasets):
                raise CedaOpenSearchError(f"dataset_index must be between 0 and {len(datasets) - 1}")
            datasets = [datasets[dataset_index]]

    results = []
    for dataset in datasets:
        files = [
            item for item in client.list_files(dataset["uuid"], start_date, end_date, max_files) if item["opendap"]
        ]
        if not files:
            continue
        parts = [open_subset(item["opendap"], latitude, longitude, bbox).load() for item in files[:max_files]]
        data = xr.concat(parts, dim="time") if len(parts) > 1 else parts[0]
        results.append({"uuid": dataset["uuid"], "title": dataset["title"], "data": data})
    if not results:
        raise CedaOpenSearchError(f"No OPeNDAP files for ECV {ecv} between {start_date} and {end_date}")
    return results


def inspect_dataset(dataset: xr.Dataset) -> dict[str, Any]:
    """Summarize an xarray dataset's dimensions, coordinates, variables, and attrs."""
    return {
        "dimensions": dict(dataset.sizes),
        "coordinates": list(dataset.coords.keys()),
        "variables": list(dataset.data_vars.keys()),
        "attributes": {key: str(value) for key, value in dataset.attrs.items()},
    }
