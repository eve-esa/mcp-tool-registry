"""Offline tests for the self-contained CEDA MCP server and client."""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

import requests
import xarray as xr

import server
from ceda_client import (
    CedaOpenSearchClient,
    CedaOpenSearchError,
    _file_entry,
    geocode_local,
    inspect_dataset,
    open_subset,
    run_pipeline,
)

FILE_FEATURE = {
    "properties": {
        "title": "f.nc",
        "date": "d",
        "filesize": 1,
        "links": {
            "related": [
                {"title": "Download", "href": "http://download"},
                {"title": "OPeNDAP", "href": "http://opendap"},
            ]
        },
    }
}
FILE_ENTRY = {
    "title": "f.nc",
    "date": "d",
    "filesize": 1,
    "opendap": "http://opendap",
}
NOMINATIM = [
    {
        "display_name": "Rome, Italy",
        "lat": "41.9",
        "lon": "12.5",
        "boundingbox": ["41.8", "42.0", "12.4", "12.6"],
    }
]


def _response(json_data=None, text="", json_error=False):
    response = MagicMock()
    response.text = text
    if json_error:
        response.json.side_effect = ValueError("invalid JSON")
    else:
        response.json.return_value = json_data
    return response


def _dataset(latitude=(40.0, 50.0)):
    return xr.Dataset(
        {"sm": (("lat", "lon"), [[1.0, 2.0], [3.0, 4.0]])},
        coords={"lat": list(latitude), "lon": [10.0, 20.0]},
    )


def _pipeline_client(datasets=None, files=None):
    client = MagicMock()
    client.find_datasets.return_value = [{"uuid": "u1", "title": "T"}] if datasets is None else datasets
    client.list_files.return_value = [FILE_ENTRY] if files is None else files
    return client


class CedaClientTests(unittest.TestCase):
    def test_search_builds_request_and_returns_geojson(self):
        client = CedaOpenSearchClient(base_url="http://example.test/os/", timeout=3, verify=False)
        client._session = MagicMock()
        client._session.get.return_value = _response({"features": []})

        result = client.search(query="sea", max_records=2, start_date="2020-01-01T00:00:00Z")

        self.assertEqual(result, {"features": []})
        args, kwargs = client._session.get.call_args
        self.assertEqual(args[0], "http://example.test/os/request")
        self.assertEqual(
            kwargs["params"],
            {
                "httpAccept": "application/geo+json",
                "query": "sea",
                "maximumRecords": 2,
                "startDate": "2020-01-01T00:00:00Z",
                "parentIdentifier": "cci",
            },
        )
        self.assertEqual(kwargs["timeout"], 3)
        self.assertFalse(kwargs["verify"])

    def test_search_atom_returns_raw_xml(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        client._session = MagicMock()
        client._session.get.return_value = _response(text="<feed/>")

        self.assertEqual(client.search(response_format="atom"), "<feed/>")
        self.assertEqual(
            client._session.get.call_args.kwargs["params"]["httpAccept"],
            "application/atom+xml",
        )

    def test_search_rejects_unknown_format_and_invalid_json(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        with self.assertRaises(ValueError):
            client.search(response_format="csv")

        client._session = MagicMock()
        client._session.get.return_value = _response(json_error=True)
        with self.assertRaisesRegex(CedaOpenSearchError, "valid JSON"):
            client.search()

    def test_search_wraps_http_errors(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        client._session = MagicMock()
        client._session.get.side_effect = requests.ConnectionError("down")

        with self.assertRaisesRegex(CedaOpenSearchError, "Request to"):
            client.search()

    def test_client_defaults_to_ceda_endpoint(self):
        self.assertTrue(CedaOpenSearchClient().base_url.startswith("https://archive.opensearch.ceda.ac.uk"))

    def test_file_entry_extracts_opendap_link(self):
        self.assertEqual(
            _file_entry(FILE_FEATURE),
            {**FILE_ENTRY},
        )

    def test_find_datasets_returns_uuid_and_title(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        response = {"features": [{"properties": {"identifier": "u1", "title": "T"}}]}
        with patch.object(client, "search", return_value=response) as search:
            found = client.find_datasets("SOILMOISTURE", "start", "end")

        self.assertEqual(found, [{"uuid": "u1", "title": "T"}])
        self.assertEqual(search.call_args.kwargs["ecv"], "SOILMOISTURE")
        self.assertEqual(search.call_args.kwargs["start_date"], "start")
        self.assertEqual(search.call_args.kwargs["end_date"], "end")

    def test_find_datasets_rejects_atom_response(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        with (
            patch.object(client, "search", return_value="<feed/>"),
            self.assertRaisesRegex(CedaOpenSearchError, "Expected GeoJSON"),
        ):
            client.find_datasets("SM", "start", "end")

    def test_list_files_paginates_and_extracts_opendap(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        pages = [
            {"features": [FILE_FEATURE], "totalResults": 2},
            {"features": [FILE_FEATURE], "totalResults": 2},
        ]
        with patch.object(client, "search", side_effect=pages) as search:
            files = client.list_files("u1", "start", "end", limit=2)

        self.assertEqual(files, [FILE_ENTRY, FILE_ENTRY])
        self.assertEqual(search.call_args_list[1].kwargs["start_record"], 2)
        self.assertEqual(search.call_args.kwargs["parent_identifier"], "u1")

    def test_list_files_stops_on_empty_page_and_respects_limit(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        with patch.object(client, "search", return_value={"features": []}):
            self.assertEqual(client.list_files("u1", "start", "end", 2), [])

        page = {"features": [FILE_FEATURE] * 2, "totalResults": 100}
        with patch.object(client, "search", return_value=page) as search:
            files = client.list_files("u1", "start", "end", limit=3)
        self.assertEqual(len(files), 3)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args.kwargs["max_records"], 1)

    def test_open_subset_selects_nearest_point(self):
        with patch("xarray.open_dataset", return_value=_dataset()):
            result = open_subset("http://opendap", 49.0, 11.0, None)
        self.assertEqual(float(result["sm"]), 3.0)

    def test_open_subset_selects_ascending_and_descending_latitude_bbox(self):
        for latitudes in ((40.0, 50.0), (50.0, 40.0)):
            with self.subTest(latitudes=latitudes):
                with patch("xarray.open_dataset", return_value=_dataset(latitudes)):
                    result = open_subset("http://opendap", None, None, (5, 35, 15, 45))
                self.assertEqual(result["sm"].shape, (1, 1))

    def test_open_subset_without_selection_returns_dataset(self):
        with patch("xarray.open_dataset", return_value=_dataset()):
            self.assertIn("sm", open_subset("http://opendap", None, None, None))

    def test_geocode_local_returns_bbox_and_sends_request_headers(self):
        response = _response(NOMINATIM)
        with patch("ceda_client.requests.get", return_value=response) as get:
            result = json.loads(asyncio.run(geocode_local("Rome")))

        self.assertEqual(result["bbox"], "12.4,41.8,12.6,42.0")
        self.assertEqual(get.call_args.args[0], "https://nominatim.openstreetmap.org/search")
        self.assertEqual(get.call_args.kwargs["params"]["q"], "Rome")
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])

    def test_geocode_local_reports_empty_results_and_http_errors(self):
        with patch("ceda_client.requests.get", return_value=_response([])):
            empty = json.loads(asyncio.run(geocode_local("Unknown")))
        self.assertIn("No results", empty["error"])

        with patch(
            "ceda_client.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ):
            error = json.loads(asyncio.run(geocode_local("Rome")))
        self.assertIn("Geocoding failed", error["error"])

    def test_run_pipeline_concatenates_files(self):
        client = _pipeline_client(files=[FILE_ENTRY, FILE_ENTRY])
        parts = [
            xr.Dataset({"sm": ("time", [1.0])}, coords={"time": [1]}),
            xr.Dataset({"sm": ("time", [2.0])}, coords={"time": [2]}),
        ]
        with patch("ceda_client.open_subset") as subset:
            subset.return_value.load.side_effect = parts
            result = run_pipeline(client, "SM", "start", "end", 1.0, 2.0, None, None, None, 5)

        self.assertEqual(result[0]["data"].sizes["time"], 2)
        client.list_files.assert_called_once_with("u1", "start", "end", 5)
        self.assertEqual(subset.call_args.args, ("http://opendap", 1.0, 2.0, None))

    def test_run_pipeline_visits_datasets_and_skips_empty_files(self):
        datasets = [{"uuid": f"u{i}", "title": f"T{i}"} for i in range(3)]
        client = _pipeline_client(datasets=datasets, files=[FILE_ENTRY])
        client.list_files.side_effect = [[FILE_ENTRY], [], [FILE_ENTRY]]
        with patch("ceda_client.open_subset") as subset:
            subset.return_value.load.return_value = xr.Dataset({"sm": ("time", [1.0])}, coords={"time": [1]})
            result = run_pipeline(client, "SM", "start", "end", None, None, None, None, None, 5)

        self.assertEqual([item["uuid"] for item in result], ["u0", "u2"])
        self.assertEqual(
            [call.args[0] for call in client.list_files.call_args_list],
            ["u0", "u1", "u2"],
        )

    def test_run_pipeline_selects_dataset_index_and_uuid(self):
        client = _pipeline_client(datasets=[{"uuid": f"u{i}", "title": f"T{i}"} for i in range(3)])
        with patch("ceda_client.open_subset") as subset:
            subset.return_value.load.return_value = "data"
            result = run_pipeline(client, "SM", "s", "e", None, None, None, None, 1, 5)
        self.assertEqual([item["uuid"] for item in result], ["u1"])

        client = _pipeline_client()
        with patch("ceda_client.open_subset") as subset:
            subset.return_value.load.return_value = "data"
            result = run_pipeline(client, "SM", "s", "e", None, None, None, "u9", None, 5)
        self.assertEqual(result[0]["uuid"], "u9")
        client.find_datasets.assert_not_called()

    def test_run_pipeline_reports_empty_dataset_files_and_bad_index(self):
        with self.assertRaisesRegex(CedaOpenSearchError, "No datasets"):
            run_pipeline(
                _pipeline_client(datasets=[]),
                "SM",
                "s",
                "e",
                None,
                None,
                None,
                None,
                None,
                5,
            )

        with self.assertRaisesRegex(CedaOpenSearchError, "No OPeNDAP"):
            run_pipeline(
                _pipeline_client(files=[{**FILE_ENTRY, "opendap": None}]),
                "SM",
                "s",
                "e",
                None,
                None,
                None,
                None,
                None,
                5,
            )

        with self.assertRaisesRegex(CedaOpenSearchError, "dataset_index"):
            run_pipeline(
                _pipeline_client(),
                "SM",
                "s",
                "e",
                None,
                None,
                None,
                None,
                5,
                5,
            )

    def test_inspect_dataset_summarizes_data(self):
        dataset = xr.Dataset(
            {"sm": ("time", [0.1, 0.2])},
            coords={"time": [0, 1]},
            attrs={"source": "test"},
        )
        self.assertEqual(
            inspect_dataset(dataset),
            {
                "dimensions": {"time": 2},
                "coordinates": ["time"],
                "variables": ["sm"],
                "attributes": {"source": "test"},
            },
        )


class CedaToolTests(unittest.TestCase):
    def _invoke(self, **kwargs):
        dataset = xr.Dataset(
            {"sm": ("time", [float("nan"), 0.3])},
            coords={"time": [0, 1], "lat": 41.9},
        )
        pipeline_results = [{"uuid": "u1", "title": "T", "data": dataset}]

        async def call():
            with (
                patch.object(server, "run_pipeline", return_value=pipeline_results),
                patch.object(server, "CedaOpenSearchClient"),
            ):
                return await server.ceda_run("SM", "start", "end", **kwargs)

        return asyncio.run(call())

    def test_ceda_run_returns_inspection_and_nan_as_null(self):
        result = self._invoke(latitude=41.9, longitude=12.5)
        self.assertEqual(result["datasets"][0]["inspect"]["variables"], ["sm"])
        self.assertEqual(result["datasets"][0]["inspect"]["dimensions"], {"time": 2})
        self.assertIsNone(result["records"][0]["sm"])
        self.assertEqual(result["records"][1]["sm"], 0.3)
        self.assertFalse(result["truncated"])

    def test_ceda_run_geocodes_place(self):
        async def call():
            with (
                patch.object(
                    server,
                    "geocode_local",
                    return_value=json.dumps({"bbox": "1,2,3,4"}),
                ),
                patch.object(server, "run_pipeline") as pipeline,
                patch.object(server, "CedaOpenSearchClient"),
            ):
                pipeline.return_value = [
                    {
                        "uuid": "u1",
                        "title": "T",
                        "data": xr.Dataset({"sm": ("time", [0.3])}, coords={"time": [0]}),
                    }
                ]
                result = await server.ceda_run("SM", "s", "e", place="Rome")
                return result, pipeline

        result, pipeline = asyncio.run(call())
        self.assertEqual(result["bbox"], "1,2,3,4")
        self.assertEqual(pipeline.call_args.args[6], (1.0, 2.0, 3.0, 4.0))

    def test_ceda_run_truncates_results(self):
        with patch.object(server, "MAX_RESULT_ROWS", 1):
            result = self._invoke(bbox="1,2,3,4")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["records"]), 1)

    def test_ceda_run_rejects_invalid_area_and_file_limit(self):
        invalid = (
            {},
            {"latitude": 1.0},
            {"latitude": 1.0, "longitude": 2.0, "bbox": "1,2,3,4"},
            {"bbox": "1,2,3"},
            {"bbox": "west,south,east,north"},
            {"bbox": "1,2,3,4", "max_files": 0},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                asyncio.run(server.ceda_run("SM", "s", "e", **kwargs))

    def test_ceda_run_reports_geocoder_and_pipeline_errors(self):
        async def geocode_error():
            with patch.object(
                server,
                "geocode_local",
                return_value=json.dumps({"error": "geocoder unavailable"}),
            ):
                await server.ceda_run("SM", "s", "e", place="Rome")

        with self.assertRaisesRegex(ValueError, "geocoder unavailable"):
            asyncio.run(geocode_error())

        async def pipeline_error():
            with (
                patch.object(server, "run_pipeline", side_effect=CedaOpenSearchError("offline")),
                patch.object(server, "CedaOpenSearchClient"),
            ):
                await server.ceda_run("SM", "s", "e", bbox="1,2,3,4")

        with self.assertRaisesRegex(ValueError, "offline"):
            asyncio.run(pipeline_error())

    def test_tool_metadata_and_registration(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual([tool.name for tool in tools], ["ceda_run"])
        self.assertTrue(tools[0].title)
        self.assertTrue(tools[0].annotations.readOnlyHint)
        self.assertTrue(tools[0].annotations.openWorldHint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
