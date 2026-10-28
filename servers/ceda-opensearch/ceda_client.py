"""Client and pipeline for the CEDA archive OpenSearch and OPeNDAP services."""

import asyncio
import json
import os
from typing import Any, Self

import requests
import xarray as xr
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
}
_FORMATS = {
    "geojson": "application/geo+json",
    "atom": "application/atom+xml",
}


def _http_timeout() -> int:
    return int(os.getenv("HTTP_REQUEST_TIMEOUT", "10"))


def _verify_tls() -> bool:
    return os.getenv("HTTP_REQUEST_TLS_VERIFY", "true").lower() in {
        "true",
        "1",
        "yes",
    }


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

    def search(
        self,
        *,
        response_format: str = "geojson",
        uuid: str | None = None,
        query: str | None = None,
        max_records: int | None = None,
        start_page: int | None = None,
        start_record: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        parent_identifier: str | None = "cci",
        bbox: str | None = None,
        ecv: str | None = None,
    ) -> dict[str, Any] | str:
        """Search the archive and return GeoJSON or Atom XML."""
        if response_format not in _FORMATS:
            raise ValueError(f"response_format must be one of {sorted(_FORMATS)}, got {response_format!r}")
        values = {
            "uuid": uuid,
            "query": query,
            "max_records": max_records,
            "start_page": start_page,
            "start_record": start_record,
            "start_date": start_date,
            "end_date": end_date,
            "parent_identifier": parent_identifier,
            "bbox": bbox,
            "ecv": ecv,
        }
        params: dict[str, Any] = {"httpAccept": _FORMATS[response_format]}
        params.update({_PARAM_NAMES[k]: v for k, v in values.items() if v is not None})
        response = self._get("request", params)
        if response_format == "atom":
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise CedaOpenSearchError("Response was not valid JSON") from exc

    def find_datasets(
        self,
        ecv: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, str]]:
        """Find CCI datasets for an essential climate variable."""
        result = self.search(
            parent_identifier="cci",
            ecv=ecv,
            start_date=start_date,
            end_date=end_date,
            max_records=100,
        )
        if isinstance(result, str):
            raise CedaOpenSearchError("Expected GeoJSON from the CEDA service")
        return [
            {
                "uuid": feature["properties"]["identifier"],
                "title": feature["properties"]["title"],
            }
            for feature in result.get("features", [])
        ]

    def list_files(
        self,
        dataset_uuid: str,
        start_date: str,
        end_date: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """List a dataset's files, including their OPeNDAP URLs."""
        files: list[dict[str, Any]] = []
        start_record = 1
        while len(files) < limit:
            result = self.search(
                parent_identifier=dataset_uuid,
                start_date=start_date,
                end_date=end_date,
                max_records=min(100, limit - len(files)),
                start_record=start_record,
            )
            if isinstance(result, str):
                raise CedaOpenSearchError("Expected GeoJSON from the CEDA service")
            features = result.get("features", [])
            files.extend(_file_entry(feature) for feature in features)
            start_record += len(features)
            if not features or start_record > result.get("totalResults", 0):
                break
        return files[:limit]


def _file_entry(feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties", {})
    links = {link.get("title", "").lower(): link.get("href") for link in properties.get("links", {}).get("related", [])}
    return {
        "title": properties.get("title"),
        "date": properties.get("date"),
        "filesize": properties.get("filesize"),
        "opendap": links.get("opendap"),
    }


def open_subset(
    opendap_url: str,
    latitude: float | None,
    longitude: float | None,
    bbox: tuple[float, float, float, float] | None,
) -> xr.Dataset:
    """Open and subset a remote dataset by point or bounding box."""
    dataset = xr.open_dataset(opendap_url, engine="netcdf4")
    if latitude is not None and longitude is not None:
        return dataset.sel(lat=latitude, lon=longitude, method="nearest")
    if bbox is not None:
        west, south, east, north = bbox
        latitudes = dataset["lat"]
        latitude_slice = slice(south, north) if latitudes[0] < latitudes[-1] else slice(north, south)
        return dataset.sel(lat=latitude_slice, lon=slice(west, east))
    return dataset


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
    max_files: int,
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
