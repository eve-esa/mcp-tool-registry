"""Offline tests for the self-contained CEDA MCP server and client."""

import asyncio
import json
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import ceda_client
import numpy as np
import requests
import xarray as xr
from ceda_client import (
    CedaOpenSearchClient,
    CedaOpenSearchError,
    _file_entry,
    compute_stats_text,
    describe_text,
    geocode_local,
    inspect_dataset,
    open_subset,
    parse_when,
    render_sample,
    run_extraction,
    run_pipeline,
    variable_statistics,
)

import server

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

        result = client.search(query="sea", max_records=2, start_date="2020-01-01T00:00:00Z", parent_identifier="cci")

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

    def test_ceda_run_returns_all_records(self):
        result = self._invoke(bbox="1,2,3,4")
        self.assertEqual(len(result["records"]), 2)
    def test_ceda_run_truncates_results(self):
        dataset = xr.Dataset(
            {"sm": ("time", [float(value) for value in range(1200)])},
            coords={"time": list(range(1200))},
        )
        pipeline_results = [{"uuid": "u1", "title": "T", "data": dataset}]

        async def call():
            with (
                patch.object(server, "run_pipeline", return_value=pipeline_results),
                patch.object(server, "CedaOpenSearchClient"),
            ):
                return await server.ceda_run("SM", "start", "end", bbox="1,2,3,4")

        result = asyncio.run(call())
        self.assertEqual(len(result["records"]), 1000)
        self.assertTrue(result["truncated"])

    def test_ceda_run_uses_configured_default_file_limit(self):
        async def call():
            with (
                patch.object(server, "run_pipeline") as pipeline,
                patch.object(server, "CedaOpenSearchClient"),
            ):
                pipeline.return_value = [
                    {"uuid": "u1", "title": "T", "data": xr.Dataset({"sm": ("time", [0.1])}, coords={"time": [0]})},
                ]
                await server.ceda_run("SM", "start", "end", bbox="1,2,3,4")
                return pipeline

        pipeline = asyncio.run(call())
        self.assertEqual(pipeline.call_args.args[9], 5)

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

    def test_limit_from_env(self):
        cases = {
            "CEDA_TEST_UNSET": (None, 50),
            "CEDA_TEST_ZERO": ("0", None),
            "CEDA_TEST_NEGATIVE": ("-2", None),
            "CEDA_TEST_BOGUS": ("bogus", 50),
            "CEDA_TEST_POSITIVE": ("17", 17),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name=name):
                if raw is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = raw
                try:
                    self.assertEqual(server._limit_from_env(name, 50), expected)
                finally:
                    os.environ.pop(name, None)

    def test_tool_metadata_and_registration(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual(
            [tool.name for tool in tools],
            ["ceda_run", "ceda_search", "ceda_describe", "ceda_files", "ceda_extract"],
        )
        self.assertTrue(tools[0].title)
        self.assertTrue(tools[0].annotations.readOnlyHint)
        self.assertTrue(tools[0].annotations.openWorldHint)


class ParseWhenTests(unittest.TestCase):
    NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def test_iso_dates_are_normalized_to_utc(self):
        self.assertEqual(parse_when("2020-06-01T00:00:00Z"), "2020-06-01T00:00:00Z")
        self.assertEqual(parse_when("2020-06-01"), "2020-06-01T00:00:00Z")
        self.assertEqual(parse_when("2020-06-01T02:00:00+02:00"), "2020-06-01T00:00:00Z")

    def test_partial_and_fuzzy_dates(self):
        self.assertEqual(parse_when("June 2020"), "2020-06-01T00:00:00Z")
        self.assertEqual(parse_when("2020-06"), "2020-06-01T00:00:00Z")
        self.assertEqual(parse_when("25 June 2020"), "2020-06-25T00:00:00Z")

    def test_relative_and_keyword_dates(self):
        self.assertEqual(parse_when("3 days ago", now=self.NOW), "2026-10-26T12:00:00Z")
        self.assertEqual(parse_when("2 weeks ago", now=self.NOW), "2026-10-15T12:00:00Z")
        self.assertEqual(parse_when("yesterday", now=self.NOW), "2026-10-28T12:00:00Z")
        self.assertEqual(parse_when("today", now=self.NOW), "2026-10-29T12:00:00Z")
        self.assertEqual(parse_when("this month", now=self.NOW), "2026-10-01T00:00:00Z")
        self.assertEqual(parse_when("this year", now=self.NOW), "2026-01-01T00:00:00Z")

    def test_invalid_dates_raise_value_error(self):
        with self.assertRaisesRegex(ValueError, "Date is empty"):
            parse_when("   ")
        with self.assertRaisesRegex(ValueError, "Could not understand"):
            parse_when("gibberish nonsense", now=self.NOW)


class SearchFilterTests(unittest.TestCase):
    def _client(self):
        client = CedaOpenSearchClient(base_url="http://example.test", timeout=3, verify=False)
        client._session = MagicMock()
        return client

    def test_archive_facets_are_forwarded(self):
        client = self._client()
        client._session.get.return_value = _response({"features": []})

        client.search(project="cmip5", sensor="AVHRR", data_type="AOD", file_format="NetCDF")

        params = client._session.get.call_args.kwargs["params"]
        for key, value in (
            ("project", "cmip5"),
            ("sensor", "AVHRR"),
            ("dataType", "AOD"),
            ("fileFormat", "NetCDF"),
        ):
            self.assertEqual(params[key], value)

    def test_unknown_filters_are_rejected(self):
        client = CedaOpenSearchClient(base_url="http://example.test")
        with self.assertRaisesRegex(ValueError, "Unknown search filters"):
            client.search(wavelength="10um")

    def test_search_datasets_returns_uuid_title_date(self):
        client = self._client()
        client._session.get.return_value = _response(
            {"features": [{"properties": {"identifier": "u1", "title": "T", "date": "2020"}}]}
        )

        entries = client.search_datasets(project="cci")

        self.assertEqual(entries, [{"uuid": "u1", "title": "T", "date": "2020"}])

    def test_list_files_works_without_dates(self):
        client = self._client()
        client._session.get.return_value = _response({"features": [FILE_FEATURE], "totalResults": 1})

        files = client.list_files("u1", limit=1)

        self.assertEqual(files, [FILE_ENTRY])
        params = client._session.get.call_args.kwargs["params"]
        self.assertNotIn("startDate", params)
        self.assertNotIn("endDate", params)


class StatisticsTests(unittest.TestCase):
    def test_variable_statistics_skip_nan(self):
        stats = variable_statistics(np.array([1.0, 2.0, 3.0, np.nan]))
        self.assertEqual(stats["n"], 3)
        self.assertAlmostEqual(stats["mean"], 2.0)
        self.assertAlmostEqual(stats["std"], np.std([1.0, 2.0, 3.0]))
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["max"], 3.0)
        self.assertAlmostEqual(stats["median"], 2.0)

    def test_variable_statistics_on_all_nan_returns_empty(self):
        self.assertEqual(variable_statistics(np.array([np.nan, np.nan])), {})

    def test_compute_stats_text_renders_units_and_caps_variables(self):
        dataset = xr.Dataset(
            {
                "a": (("t",), [1.0, 2.0], {"units": "K"}),
                "text": (("t",), ["x", "y"]),
                "b": (("t",), [3.0, 4.0]),
            }
        )

        text = compute_stats_text(dataset, max_variables=1)

        self.assertIn("a [K]: n=2 mean=1.5", text)
        self.assertIn("more variables: b", text)
        self.assertNotIn("text:", text)

    def test_render_sample_caps_rows_and_reports_truncation(self):
        dataset = xr.Dataset({"v": (("t",), list(range(120)))})

        text, rendered = render_sample(dataset, max_rows=50)

        self.assertEqual(rendered, 50)
        self.assertIn("(truncated: 70 more rows not shown)", text)
        self.assertEqual(len(text.splitlines()), 52)  # header + 50 rows + note

    def test_render_sample_renders_all_rows_by_default(self):
        dataset = xr.Dataset({"v": (("t",), list(range(120)))})

        text, rendered = render_sample(dataset)

        self.assertEqual(rendered, 120)
        self.assertNotIn("(truncated:", text)
        self.assertEqual(len(text.splitlines()), 121)  # header + 120 rows

    def test_render_sample_blanks_nan_cells(self):
        dataset = xr.Dataset({"v": (("t",), [np.nan, 1.5])})

        text, rendered = render_sample(dataset, max_rows=50)

        self.assertEqual(rendered, 2)
        self.assertEqual(text.splitlines(), ["t | v", "0 | ", "1 | 1.5"])

    def test_render_sample_blanks_nat_time_bound_cells(self):
        """Regression: v09.2 soil-moisture time_bnds fills decode to pandas.NaT,
        whose strftime() raises when called."""
        bounds = np.array(
            [["2016-07-13", "2016-07-14"], ["2016-07-14", "NaT"]],
            dtype="datetime64[ns]",
        )
        dataset = xr.Dataset(
            {"sm": (("time",), [0.306, 0.31])},
            coords={
                "time": np.array(["2016-07-14", "2016-07-15"], dtype="datetime64[ns]"),
                "time_bnds": (("time", "bnds"), bounds),
            },
        )

        text, rendered = render_sample(dataset, max_rows=50)

        self.assertEqual(rendered, 4)
        self.assertNotIn("NaT", text)
        self.assertIn("0.306", text)


class DescribeTextTests(unittest.TestCase):
    def test_describe_renders_structure_and_truncates_attributes(self):
        dataset = xr.Dataset(
            {"sm": (("time", "lat"), [[1.0], [2.0]], {"units": "m3 m-3", "long_name": "Soil Moisture"})},
            coords={"time": [0, 1], "lat": [41.0]},
            attrs={"institution": "x" * 120},
        )

        text = describe_text(dataset)

        self.assertIn("Dimensions: time=2, lat=1", text)
        self.assertIn("time [0..1]", text)
        self.assertIn("lat [41.0..41.0]", text)
        self.assertIn("sm (time, lat) float64 [m3 m-3] Soil Moisture", text)
        self.assertIn(f"institution={'x' * 80}…", text)


class FlexibleCoordinateTests(unittest.TestCase):
    def test_open_subset_matches_latitude_longitude_naming(self):
        dataset = xr.Dataset(
            {"sm": (("latitude", "longitude"), [[1.0, 2.0], [3.0, 4.0]])},
            coords={"latitude": [40.0, 50.0], "longitude": [10.0, 20.0]},
        )
        with patch("xarray.open_dataset", return_value=dataset):
            result = open_subset("http://opendap", 41.0, 11.0, None)
        self.assertEqual(float(result["sm"]), 1.0)

    def test_open_subset_returns_dataset_without_spatial_coords(self):
        dataset = xr.Dataset({"index": (("t",), [1.0, 2.0])})
        with patch("xarray.open_dataset", return_value=dataset):
            result = open_subset("http://opendap", 41.0, 11.0, (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(list(result["index"].values), [1.0, 2.0])


class RunExtractionTests(unittest.TestCase):
    def _client(self, datasets):
        client = MagicMock()
        client.search_datasets.return_value = datasets
        client.list_files.return_value = [FILE_ENTRY]
        return client

    def test_extraction_with_filters_variables_and_index(self):
        client = self._client([{"uuid": f"u{i}", "title": f"T{i}"} for i in range(3)])
        data = xr.Dataset({"sm": (("time",), [0.1]), "flag": (("time",), [1.0])})
        with patch.object(ceda_client, "open_subset", return_value=data) as subset:
            results = run_extraction(
                client,
                dataset_index=1,
                max_files=2,
                variables=["sm"],
                query="soil",
                start_date="2020-01-01T00:00:00Z",
            )

        self.assertEqual([item["uuid"] for item in results], ["u1"])
        self.assertEqual(list(results[0]["data"].data_vars), ["sm"])
        self.assertEqual(subset.call_count, 1)
        kwargs = client.search_datasets.call_args.kwargs
        self.assertEqual(kwargs["query"], "soil")
        self.assertEqual(kwargs["start_date"], "2020-01-01T00:00:00Z")
        self.assertEqual(client.list_files.call_args.args, ("u1", "2020-01-01T00:00:00Z", None, 2))

    def test_extraction_rejects_unknown_variables_and_bad_index(self):
        client = self._client([{"uuid": "u0", "title": "T"}])
        data = xr.Dataset({"sm": (("time",), [0.1])})
        with patch.object(ceda_client, "open_subset", return_value=data):
            with self.assertRaisesRegex(ValueError, "None of"):
                run_extraction(client, variables=["rainfall"])
        with self.assertRaisesRegex(CedaOpenSearchError, "dataset_index"):
            run_extraction(client, dataset_index=9)

    def test_extraction_direct_uuid_skips_search(self):
        client = MagicMock()
        client.list_files.return_value = [FILE_ENTRY]
        data = xr.Dataset({"sm": (("time",), [0.1])})
        with patch.object(ceda_client, "open_subset", return_value=data):
            results = run_extraction(client, dataset_uuid="u9")
        client.search_datasets.assert_not_called()
        self.assertEqual(results[0]["uuid"], "u9")

    def test_extraction_concatenates_multiple_files_along_time(self):
        client = self._client([{"uuid": "u0", "title": "T"}])
        client.list_files.return_value = [FILE_ENTRY, FILE_ENTRY]
        parts = [
            xr.Dataset({"sm": (("time",), [0.1])}, coords={"time": [0]}),
            xr.Dataset({"sm": (("time",), [0.2])}, coords={"time": [1]}),
        ]
        with patch.object(ceda_client, "open_subset", side_effect=parts):
            results = run_extraction(client, dataset_uuid="u0", max_files=2)
        self.assertEqual(len(results[0]["data"]["time"]), 2)
        self.assertEqual([float(v) for v in results[0]["data"]["sm"]], [0.1, 0.2])


class CedaSearchToolTests(unittest.TestCase):
    def test_search_lists_entries_and_forwards_filters(self):
        async def call():
            with patch.object(server, "CedaOpenSearchClient") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.search_datasets.return_value = [{"uuid": "u1", "title": "Soil moisture", "date": "2020"}]
                text = await server.ceda_search(query="soil", project=" cci ", max_results=500)
                return text, client

        text, client = asyncio.run(call())
        self.assertIn("[0] u1 | Soil moisture | 2020", text)
        kwargs = client.search_datasets.call_args.kwargs
        self.assertEqual(kwargs["query"], "soil")
        self.assertEqual(kwargs["project"], "cci")
        self.assertEqual(kwargs["max_records"], 100)

    def test_search_lists_up_to_requested_amount_when_cap_disabled(self):
        async def call():
            with patch.object(server, "CedaOpenSearchClient") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.search_datasets.return_value = []
                with patch.object(server, "MAX_ELEMENTS", None):
                    await server.ceda_search(query="soil", max_results=500)
                return client.search_datasets.call_args.kwargs

        self.assertEqual(asyncio.run(call())["max_records"], 500)

    def test_search_geocodes_place_into_bbox_filter(self):
        async def call():
            with (
                patch.object(server, "geocode_local", return_value=json.dumps({"bbox": "1,2,3,4"})),
                patch.object(server, "CedaOpenSearchClient") as client_cls,
            ):
                client_cls.return_value.__enter__.return_value.search_datasets.return_value = []
                text = await server.ceda_search(place="Rome")
                return text, client_cls.return_value.__enter__.return_value

        text, client = asyncio.run(call())
        self.assertEqual(text, "No CEDA datasets matched the search.")
        self.assertEqual(client.search_datasets.call_args.kwargs["bbox"], "1,2,3,4")

    def test_search_parses_natural_language_dates(self):
        async def call():
            with patch.object(server, "CedaOpenSearchClient") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.search_datasets.return_value = []
                await server.ceda_search(start_date="June 2020", end_date="August 2020")
                return client.search_datasets.call_args.kwargs

        kwargs = asyncio.run(call())
        self.assertEqual(kwargs["start_date"], "2020-06-01T00:00:00Z")
        self.assertEqual(kwargs["end_date"], "2020-08-01T00:00:00Z")

    def test_search_rejects_multiple_area_selectors(self):
        with self.assertRaises(ValueError):
            asyncio.run(server.ceda_search(place="Rome", bbox="1,2,3,4"))


class CedaDatasetToolTests(unittest.TestCase):
    def test_describe_renders_structure_with_source(self):
        async def call():
            with (
                patch.object(server, "CedaOpenSearchClient") as client_cls,
                patch.object(server, "open_subset") as subset,
            ):
                client_cls.return_value.__enter__.return_value.list_files.return_value = [FILE_ENTRY]
                subset.return_value.load.return_value = _dataset()
                return await server.ceda_describe("u1")

        text = asyncio.run(call())
        self.assertIn("Dataset u1", text)
        self.assertIn("OPeNDAP source: http://opendap", text)
        self.assertIn("Dimensions: lat=2, lon=2", text)

    def test_files_lists_sizes_and_opendap_availability(self):
        async def call():
            with patch.object(server, "CedaOpenSearchClient") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.list_files.return_value = [FILE_ENTRY, {**FILE_ENTRY, "opendap": None}]
                text = await server.ceda_files("u1", start_date="June 2020")
                return text, client

        text, client = asyncio.run(call())
        self.assertEqual(client.list_files.call_args.args, ("u1", "2020-06-01T00:00:00Z", None, 100))
        self.assertIn("[0] f.nc | d | 1.0 B | opendap=yes", text)
        self.assertIn("opendap=no", text)

    def test_files_honours_explicit_and_disabled_limits(self):
        async def call(limit, elements):
            with patch.object(server, "CedaOpenSearchClient") as client_cls:
                client = client_cls.return_value.__enter__.return_value
                client.list_files.return_value = []
                with patch.object(server, "MAX_ELEMENTS", elements):
                    await server.ceda_files("u1", start_date="June 2020", limit=limit)
                return client.list_files.call_args.args

        self.assertEqual(asyncio.run(call(limit=7, elements=None))[-1], 7)
        self.assertIsNone(asyncio.run(call(limit=None, elements=None))[-1])

    def test_extract_renders_stats_and_capped_sample(self):
        data = xr.Dataset({"sm": (("t",), [value / 10 for value in range(120)])})

        async def call():
            with (
                patch.object(server, "run_extraction", return_value=[{"uuid": "u1", "title": "T", "data": data}]),
                patch.object(server, "CedaOpenSearchClient"),
            ):
                return await server.ceda_extract(dataset_uuid="u1")

        text = asyncio.run(call())
        self.assertIn("Dataset: T (u1)", text)
        self.assertIn("sm: n=120 mean=", text)
        self.assertIn("p95=", text)
        self.assertIn("Sample (first 100 rows, pipe-delimited):", text)
        self.assertIn("(truncated: 20 more rows not shown)", text)

    def test_extract_renders_full_sample_when_cap_disabled(self):
        data = xr.Dataset({"sm": (("t",), [value / 10 for value in range(60)])})

        async def call():
            with (
                patch.object(server, "run_extraction", return_value=[{"uuid": "u1", "title": "T", "data": data}]),
                patch.object(server, "CedaOpenSearchClient"),
                patch.object(server, "MAX_ELEMENTS", None),
            ):
                return await server.ceda_extract(dataset_uuid="u1")

        text = asyncio.run(call())
        self.assertIn("Sample (60 rows, pipe-delimited):", text)
        self.assertNotIn("(truncated:", text)

    def test_extract_uses_configured_default_file_limit(self):
        async def call():
            with (
                patch.object(server, "run_extraction", return_value=[]) as extraction,
                patch.object(server, "CedaOpenSearchClient"),
            ):
                await server.ceda_extract(dataset_uuid="u1")
                return extraction.call_args.kwargs

        self.assertEqual(asyncio.run(call())["max_files"], 5)

    def test_extract_forwards_filters_variables_and_area(self):
        async def call():
            with (
                patch.object(server, "run_extraction", return_value=[]) as extraction,
                patch.object(server, "CedaOpenSearchClient"),
            ):
                await server.ceda_extract(query="soil", variables="sm, moisture", bbox="1,2,3,4")
                return extraction.call_args.kwargs

        kwargs = asyncio.run(call())
        self.assertEqual(kwargs["variables"], ["sm", "moisture"])
        self.assertEqual(kwargs["query"], "soil")
        self.assertEqual(kwargs["bbox"], "1,2,3,4")
        self.assertEqual(kwargs["area"], (1.0, 2.0, 3.0, 4.0))

    def test_extract_validates_inputs(self):
        with self.assertRaises(ValueError):
            asyncio.run(server.ceda_extract(max_files=0))
        with self.assertRaises(ValueError):
            asyncio.run(server.ceda_extract(place="Rome", bbox="1,2,3,4"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
