from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from weather_format import daylight, make_tags, render_template, compass_16, format_description, strautomator_aqi

import strava_weather as sw


class ParsingTests(unittest.TestCase):
    def test_utc_conversion(self):
        dt = sw.parse_utc("2020-01-01T10:30:00Z")
        self.assertEqual(dt.hour, 10)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_wind_cardinal(self):
        self.assertEqual(sw.cardinal(315), "NW")
        self.assertEqual(sw.cardinal(359), "N")

    def test_merge_preserves_original(self):
        old = "A nice run!\nLine 2"
        merged = sw.merge_description(old, "BLOCK", refresh=False)
        self.assertEqual(merged, "BLOCK\n\n" + old)

    def test_merge_skips_previous(self):
        tagged = "intro\n" + sw.START_MARKER + "\nold\n" + sw.END_MARKER
        self.assertIsNone(sw.merge_description(tagged, "new", refresh=False))

    def test_merge_refresh_only_managed_part(self):
        tagged = "Intro\n" + sw.START_MARKER + "\nold\n" + sw.END_MARKER + "\nClosing"
        block = sw.START_MARKER + "\nnew\n" + sw.END_MARKER
        expected = block + "\n\nIntro\n\nClosing"
        self.assertEqual(sw.merge_description(tagged, block, refresh=True), expected)

    def test_malformed_markers_not_modified(self):
        with self.assertRaises(sw.ToolError):
            sw.merge_description(sw.START_MARKER + " missing end", "new", refresh=True)

    @patch("strava_weather.api_json")
    def test_weather_extract_hour_utc(self, mock_api):
        mock_api.return_value = {"hourly": {
            "time": ["2020-01-01T11:00", "2020-01-01T12:00"],
            "temperature_2m": [15, 16], "relative_humidity_2m": [60, 55],
        }}
        reading = sw.weather_at([37.98, 23.72], datetime(2020, 1, 1, 11, 40, tzinfo=timezone.utc), "era5")
        self.assertEqual(reading["temperature_2m"], 16)
        self.assertEqual(mock_api.call_args.kwargs["params"]["timezone"], "UTC")
        self.assertEqual(mock_api.call_args.kwargs["params"]["models"], "era5")

    @patch("strava_weather.api_json")
    def test_rounded_hour_rolls_into_next_utc_date(self, mock_api):
        mock_api.return_value = {"hourly": {
            "time": ["2020-01-01T23:00", "2020-01-02T00:00"],
            "temperature_2m": [15, 16],
        }}
        reading = sw.weather_at([37.98, 23.72], datetime(2020, 1, 1, 23, 55, tzinfo=timezone.utc), "best_match")
        self.assertEqual(reading["temperature_2m"], 16)
        self.assertEqual(mock_api.call_args.kwargs["params"]["start_date"], "2020-01-01")
        self.assertEqual(mock_api.call_args.kwargs["params"]["end_date"], "2020-01-02")

    @patch("strava_weather.air_quality_at", return_value={})
    @patch("strava_weather.weather_at")
    def test_activity_weather_across_midnight(self, mock_weather, _mock_aqi):
        mock_weather.return_value = {"weather_code": 0, "temperature_2m": 20, "precipitation": 0}
        activity = {"start_date": "2020-01-01T23:45:00Z", "elapsed_time": 3600,
                    "start_latlng": [37.98, 23.72], "end_latlng": [37.99, 23.73], "utc_offset": 7200}
        block = sw.make_weather_block(activity, "best_match")
        self.assertTrue(block.startswith("Cool, clear, 20°C, Feels like 20°C"))
        self.assertNotIn("Source: Open-Meteo.com", block)
        self.assertEqual(mock_weather.call_count, 1)  # Default prefix needs start only.
        sw.make_weather_payload(activity, "best_match",
                                description_template="${weather.start.summary} / ${weather.end.summary}")
        self.assertEqual(mock_weather.call_count, 3)  # Two extra reads (start + end).
        self.assertEqual(mock_weather.call_args_list[2].args[1].date().isoformat(), "2020-01-02")

    @patch("strava_weather.air_quality_at", return_value={})
    @patch("strava_weather.weather_at")
    def test_short_activity_uses_start_only(self, mock_weather, _mock_aqi):
        mock_weather.return_value = {"weather_code": 2, "temperature_2m": 19}
        block = sw.make_weather_block({"start_date": "2020-01-01T10:00:00Z", "elapsed_time": 300,
                                       "start_latlng": [38.0, 23.0]}, "era5")
        self.assertNotIn("\nEnd ", block)
        self.assertEqual(mock_weather.call_count, 1)

    @patch("strava_weather.make_weather_payload")
    def test_dry_run_never_updates_strava(self, mock_weather):
        mock_weather.return_value = ("Test run ☀️ 20°C", sw.START_MARKER + "\nmocked data\n" + sw.END_MARKER)
        class Stub:
            updates = []
            def get_activity(self, _id):
                return {"id": 1, "name": "Test run", "description": "Original note",
                        "start_latlng": [38.0, 23.0], "start_date": "2020-01-01T10:00:00Z"}
            def update_activity(self, activity_id, description, title=None):
                self.updates.append((activity_id, description, title))
        client = Stub()
        args = argparse.Namespace(model="best_match", refresh=False, apply=False)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sw, "TITLE_STATE_FILE", Path(temp) / "title-state.json"):
                outcome = sw.process_activity(client, {"id": 1}, args)
                self.assertEqual(outcome, "previewed")
                self.assertEqual(client.updates, [])
                args.apply = True
                outcome = sw.process_activity(client, {"id": 1}, args)
                self.assertEqual(outcome, "updated")
                self.assertEqual(client.updates[0][0], 1)
                self.assertIn("Original note", client.updates[0][1])
                self.assertEqual(client.updates[0][2], "Test run ☀️ 20°C")
                state = sw.read_title_state()
                self.assertEqual(state["1"]["base_name"], "Test run")

    @patch("strava_weather.air_quality_at", return_value={})
    @patch("strava_weather.weather_at")
    def test_refresh_replaces_title_suffix_not_duplicate(self, mock_weather, _mock_aqi):
        mock_weather.return_value = {
            "weather_code": 0, "temperature_2m": 20, "precipitation": 0,
            "wind_speed_10m": 8, "relative_humidity_2m": 60,
        }
        class Stub:
            def __init__(self):
                self.detail = {"id": 2, "name": "Afternoon Ride", "description": "Personal notes",
                               "start_latlng": [38.0, 23.0], "start_date": "2020-07-01T12:00:00Z",
                               "elapsed_time": 400}
                self.updates = []
            def get_activity(self, _id):
                return dict(self.detail)
            def update_activity(self, activity_id, description, title=None):
                self.updates.append((activity_id, description, title))
                self.detail["description"] = description
                if title is not None:
                    self.detail["name"] = title
        client = Stub()
        args = argparse.Namespace(model="best_match", refresh=False, apply=True)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sw, "TITLE_STATE_FILE", Path(temp) / "title-state.json"):
                self.assertEqual(sw.process_activity(client, {"id": 2}, args), "updated")
                initial = client.detail["name"]
                self.assertEqual(initial, "Afternoon Ride ☀️")
                self.assertEqual(sw.process_activity(client, {"id": 2}, args), "skipped")
                args.refresh = True
                self.assertEqual(sw.process_activity(client, {"id": 2}, args), "skipped")
                self.assertEqual(client.detail["name"], initial)
                self.assertEqual(len(client.updates), 1)
                # Enable temperature, then disable it: refresh must replace the managed suffix.
                args.include_title_temperature = True
                self.assertEqual(sw.process_activity(client, {"id": 2}, args), "updated")
                self.assertEqual(client.detail["name"], "Afternoon Ride ☀️ 20°C")
                args.include_title_temperature = False
                self.assertEqual(sw.process_activity(client, {"id": 2}, args), "updated")
                self.assertEqual(client.detail["name"], "Afternoon Ride ☀️")
                # External changes to a title are preserved even while refreshing weather.
                client.detail["name"] = "My manually edited ride"
                mock_weather.return_value["temperature_2m"] = 21
                self.assertEqual(sw.process_activity(client, {"id": 2}, args), "updated")
                self.assertEqual(client.detail["name"], "My manually edited ride")
                self.assertIn("21°C", client.detail["description"])

    def test_title_state_protects_external_edit(self):
        self.assertEqual(sw.original_title("My Ride ☀️ 20°C", {
            "base_name": "My Ride", "last_title": "My Ride ☀️ 20°C"}), ("My Ride", True))
        self.assertEqual(sw.original_title("My custom title", {
            "base_name": "My Ride", "last_title": "My Ride ☀️ 20°C"}), ("My custom title", False))

    def test_strautomator_templates(self):
        when = datetime(2020, 6, 1, 12, 0, tzinfo=timezone.utc)
        reading = {"temperature_2m": 23.5, "apparent_temperature": 24.4,
                   "relative_humidity_2m": 63.5, "wind_speed_10m": 13.6,
                   "wind_direction_10m": 350, "weather_code": 0, "precipitation": 0}
        tags = make_tags(reading, when, [38, 23], when)
        self.assertEqual(tags["icon"], "☀️")
        self.assertEqual(tags["temperature"], "24°C")
        self.assertEqual(tags["humidity"], "64%")
        self.assertEqual(tags["windDirection"], "N")
        self.assertEqual(tags["windSpeed"], "4 m/s")
        self.assertEqual(tags["precipitation"], "dry")
        self.assertEqual(render_template("${name} {weather.icon} ${weather.temperature}",
                                         "Morning ride", {"weather": tags}), "Morning ride ☀️ 24°C")
        self.assertEqual(render_template("${weather.start.summary}", "", {"weather.start": tags}), "Warm, clear")

    def test_exact_user_description(self):
        when = datetime(2020, 5, 4, 10, 0, tzinfo=timezone.utc)
        reading = {"temperature_2m": 15, "apparent_temperature": 15,
                   "relative_humidity_2m": 79, "wind_speed_10m": 3.6,
                   "wind_direction_10m": 0, "weather_code": 3, "precipitation": 0,
                   "us_aqi": 68, "european_aqi": 15}
        tags = make_tags(reading, when, [38, 23], when)
        self.assertEqual(format_description(tags),
                         "Cool, cloudy, 15°C, Feels like 15°C, Humidity 79%, "
                         "Wind 1 m/s from N, Precipitation dry, AQI 1 🟡")

    def test_aqi_missing_omits_aqi_instead_of_fabrication(self):
        when = datetime(1992, 1, 1, 10, 0, tzinfo=timezone.utc)
        tags = make_tags({"temperature_2m": 15, "weather_code": 3}, when, [38, 23], when)
        self.assertNotIn("AQI", format_description(tags))
        self.assertEqual(strautomator_aqi(None, None), ("N/A", "N/A"))

    @patch("strava_weather.api_json")
    def test_aqi_historical_request_and_mapping(self, mock_api):
        mock_api.return_value = {"hourly": {
            "time": ["2020-01-02T00:00"], "us_aqi": [51], "european_aqi": [12]
        }}
        when = datetime(2020, 1, 1, 23, 45, tzinfo=timezone.utc)
        result = sw.air_quality_at([38, 23], when)
        self.assertEqual(result["us_aqi"], 51)
        self.assertEqual(mock_api.call_args.kwargs["params"]["start_date"], "2020-01-02")

    @patch("strava_weather.api_json")
    def test_aqi_not_requested_before_2013(self, mock_api):
        when = datetime(1992, 1, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(sw.air_quality_at([38, 23], when), {})
        mock_api.assert_not_called()

    def test_refresh_v3_prefix_and_preserve_notes(self):
        old = "Cool, cloudy, 15°C\nSource: Open-Meteo.com (CC BY 4.0)"
        new = "Cool, clear, 18°C"
        desc = old + "\n\nMy own notes"
        self.assertIsNone(sw.merge_description(desc, new, refresh=False, previous_prefix=old))
        self.assertEqual(sw.merge_description(desc, new, refresh=True, previous_prefix=old),
                         new + "\n\nMy own notes")
        with self.assertRaises(sw.ToolError):
            sw.merge_description("Manually edited " + desc, new, refresh=True, previous_prefix=old)


    @patch("strava_weather.air_quality_at", return_value={"us_aqi": 68, "european_aqi": 15})
    @patch("strava_weather.weather_at")
    def test_default_payload_has_exact_weather_line_without_source(self, mock_weather, _mock_aqi):
        mock_weather.return_value = {
            "temperature_2m": 15, "apparent_temperature": 15,
            "relative_humidity_2m": 79, "wind_speed_10m": 3.6,
            "wind_direction_10m": 0, "weather_code": 3, "precipitation": 0,
        }
        activity = {"name": "Morning Run", "start_date": "2020-05-04T10:00:00Z",
                    "start_latlng": [38, 23], "elapsed_time": 3600}
        title, description = sw.make_weather_payload(activity, "best_match")
        self.assertEqual(description, "Cool, cloudy, 15°C, Feels like 15°C, Humidity 79%, "
                         "Wind 1 m/s from N, Precipitation dry, AQI 1 🟡")
        self.assertNotIn("Open-Meteo.com", description)
        self.assertEqual(title, "Morning Run ☁️")

    @patch("strava_weather.air_quality_at", return_value={})
    @patch("strava_weather.weather_at")
    def test_opt_in_title_temperature_and_custom_title(self, mock_weather, _mock_aqi):
        mock_weather.return_value = {"temperature_2m": 15, "weather_code": 3}
        activity = {"name": "Morning Run", "start_date": "2020-05-04T10:00:00Z",
                    "start_latlng": [38, 23], "elapsed_time": 3600}
        default, _ = sw.make_weather_payload(activity, "best_match")
        with_temp, _ = sw.make_weather_payload(activity, "best_match", include_title_temperature=True)
        custom, _ = sw.make_weather_payload(activity, "best_match",
                                            title_template="${name}: ${weather.summary}",
                                            include_title_temperature=True)
        self.assertEqual(default, "Morning Run ☁️")
        self.assertEqual(with_temp, "Morning Run ☁️ 15°C")
        self.assertEqual(custom, "Morning Run: Cool, cloudy")

    def test_title_temperature_cli_flag_defaults_off(self):
        parser = sw.make_parser()
        default = parser.parse_args(["backfill", "--activity-id", "123"])
        enabled = parser.parse_args(["backfill", "--activity-id", "123", "--include-title-temperature"])
        self.assertFalse(default.include_title_temperature)
        self.assertTrue(enabled.include_title_temperature)

    @patch("strava_weather.air_quality_at", return_value={})
    @patch("strava_weather.weather_at")
    def test_custom_template_does_not_append_source(self, mock_weather, _mock_aqi):
        mock_weather.return_value = {"temperature_2m": 15, "weather_code": 0}
        activity = {"name": "Morning Run", "start_date": "2020-05-04T10:00:00Z",
                    "start_latlng": [38, 23], "elapsed_time": 3600}
        _, description = sw.make_weather_payload(activity, "best_match",
                                                  description_template="${weather.temperature}")
        self.assertEqual(description, "15°C")

    def test_daylight_and_16_point_compass(self):
        self.assertTrue(daylight(datetime(2020, 6, 1, 12, tzinfo=timezone.utc), 38, 23))
        self.assertFalse(daylight(datetime(2020, 6, 1, 0, tzinfo=timezone.utc), 38, 23))
        self.assertEqual(compass_16(25), "NNE")

    def test_unknown_tags_do_not_silently_survive(self):
        with self.assertRaises(ValueError):
            render_template("{weather.imaginary}", "", {})

    def test_endpoints_cannot_be_unbounded(self):
        ns = argparse.Namespace(after=None, before=None, activity_id=None,
                                delay=0.0, model="era5", apply=False, limit=10, max_pages=1, refresh=False)
        with self.assertRaises(sw.ToolError):
            sw.backfill(ns)


if __name__ == "__main__":
    unittest.main()