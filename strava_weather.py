#!/usr/bin/env python3
"""Add historical Open-Meteo weather to your own Strava activities.

Safe by default: `backfill` only previews changes unless --apply is provided.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv
from weather_format import (DEFAULT_DESCRIPTION_TEMPLATE, DEFAULT_TITLE_TEMPLATE,
                            format_description, make_tags, render_template, validate_template)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN_FILE = Path(os.environ.get("STRAUT_WEATHER_TOKEN_FILE", str(ROOT / ".private" / "tokens.json")))
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
OPENMETEO_API = "https://archive-api.open-meteo.com/v1/archive"
AIR_QUALITY_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
SCOPES = {"activity:read_all", "activity:write"}
START_MARKER = "--- Historical weather (Open-Meteo) ---"
TITLE_STATE_FILE = Path(os.environ.get("STRAUT_WEATHER_TITLE_STATE_FILE", str(TOKEN_FILE.parent / "title-state.json")))
END_MARKER = "--- End historical weather ---"
HOURLY = [
    "temperature_2m", "apparent_temperature", "relative_humidity_2m",
    "precipitation", "weather_code", "wind_speed_10m", "wind_direction_10m",
    "wind_gusts_10m", "dew_point_2m", "surface_pressure", "cloud_cover",
]

WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
    67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Light rain showers", 81: "Rain showers",
    82: "Heavy rain showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}


class ToolError(Exception):
    """Readable user-facing error."""


class RateLimitError(ToolError):
    """A provider has returned HTTP 429; stop further requests."""


def api_json(method: str, url: str, **kwargs: Any) -> Any:
    try:
        response = requests.request(method, url, timeout=35, **kwargs)
        if response.status_code == 429:
            raise RateLimitError(f"API rate limit reached at {urlparse(url).netloc}; stop and retry later.")
        if not response.ok:
            # Never print the request body, which can contain tokens.
            details = ""
            try:
                message = response.json()
                details = str(message.get("message", ""))[:180] if isinstance(message, dict) else ""
            except ValueError:
                pass
            raise ToolError(f"{urlparse(url).netloc} returned HTTP {response.status_code}. {details}")
        return response.json()
    except requests.RequestException as exc:
        raise ToolError(f"Network error contacting {urlparse(url).netloc}: {exc}") from exc


def save_tokens(tokens: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    try:
        with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
            f.write("\n")
        os.replace(tmp, TOKEN_FILE)
        if os.name != "nt":
            TOKEN_FILE.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)


def require_credentials() -> tuple[str, str]:
    client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
    secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id.isdigit() or not secret:
        raise ToolError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env (see .env.example).")
    return client_id, secret


def get_access_token() -> str:
    client_id, client_secret = require_credentials()
    try:
        tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        raise ToolError("No valid token file found. Run: uv run python strava_weather.py auth") from exc
    if int(tokens.get("expires_at", 0)) > time.time() + 300:
        return str(tokens["access_token"])
    fresh = api_json("POST", STRAVA_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    tokens.update({key: fresh[key] for key in ("access_token", "refresh_token", "expires_at")})
    save_tokens(tokens)
    return str(tokens["access_token"])


def authenticate(port: int = 8765, open_browser: bool = True) -> None:
    client_id, client_secret = require_credentials()
    random_state = secrets.token_urlsafe(24)
    redirect_uri = f"http://localhost:{port}/callback"
    url = "https://www.strava.com/oauth/authorize?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "force",
        "scope": ",".join(sorted(SCOPES)),
        "state": random_state,
    })
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = (parsed.path == "/callback" and query.get("state") == [random_state])
            if not valid:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid OAuth callback")
                return
            if "code" in query:
                result["code"] = query["code"][0]
                result["scope"] = query.get("scope", [""])[0]
                body = b"Strava authorization received. You can close this tab."
            else:
                result["error"] = query.get("error", ["Authorization declined"])[0]
                body = b"Authorization declined. You may close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            # Do not log OAuth authorization codes.
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise ToolError(f"Unable to listen on localhost:{port}: {exc}") from exc
    server.timeout = 1
    print("Open this URL to authorize your own Strava account:\n" + url)
    print(f"\nWaiting for local callback on port {port} (up to 2 minutes)...")
    if open_browser:
        webbrowser.open(url)
    with server:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not result:
            server.handle_request()
    if "error" in result:
        raise ToolError("Strava authorization was denied.")
    if "code" not in result:
        raise ToolError("Authorization timed out. Retry `auth` on the machine with the browser.")
    granted = set(re.split(r"[,\s]+", result["scope"].strip()))
    if not SCOPES.issubset(granted):
        raise ToolError(f"Missing scopes: {', '.join(sorted(SCOPES - granted))}. Please reauthorize.")
    tokens = api_json("POST", STRAVA_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
    })
    if not all(key in tokens for key in ("access_token", "refresh_token", "expires_at")):
        raise ToolError("Token exchange returned incomplete credentials.")
    save_tokens(tokens)
    print(f"Authorized successfully; tokens saved to {TOKEN_FILE} (keep this file private).")


class Strava:
    def __init__(self, delay: float):
        self.delay = delay
        self._last_request = 0.0

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        gap = time.monotonic() - self._last_request
        if gap < self.delay:
            time.sleep(self.delay - gap)
        token = get_access_token()  # Refreshes automatically and persists rotated refresh token.
        try:
            return api_json(method, STRAVA_API + path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        finally:
            self._last_request = time.monotonic()

    def get_activity(self, activity_id: int) -> dict:
        return self.request("GET", f"/activities/{activity_id}")

    def update_activity(self, activity_id: int, description: str, title: str | None = None) -> None:
        # Send both fields in a single request without touching privacy/gear/commute.
        data = {"description": description}
        if title is not None:
            data["name"] = title
        self.request("PUT", f"/activities/{activity_id}", data=data)

    def list_activities(self, after: date | None, before: date | None, max_pages: int):
        params: dict[str, Any] = {"per_page": 100}
        if after:
            params["after"] = int(datetime.combine(after, datetime.min.time(), timezone.utc).timestamp()) - 1
        if before:
            params["before"] = int(datetime.combine(before + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
        for page in range(1, max_pages + 1):
            batch = self.request("GET", "/athlete/activities", params={**params, "page": page})
            if not isinstance(batch, list):
                raise ToolError("Unexpected Strava activities response.")
            for activity in batch:
                yield activity
            if len(batch) < 100:
                return


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ToolError(f"Strava timestamp missing timezone: {value}")
    return dt.astimezone(timezone.utc)


def weather_at(latlng: list[float], when_utc: datetime, model: str) -> dict[str, Any]:
    if len(latlng) != 2 or not (-90 <= latlng[0] <= 90 and -180 <= latlng[1] <= 180):
        raise ToolError("Invalid activity coordinates.")
    if when_utc.date() < date(1940, 1, 1):
        raise ToolError("Open-Meteo historical archive starts in 1940.")
    hour_utc = (when_utc + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    date_str = when_utc.date().isoformat()
    params: dict[str, Any] = {
        "latitude": round(latlng[0], 5), "longitude": round(latlng[1], 5),
        "start_date": date_str, "end_date": hour_utc.date().isoformat(),
        "timezone": "UTC", "hourly": ",".join(HOURLY), "wind_speed_unit": "kmh",
    }
    if model != "best_match":
        params["models"] = model
    data = api_json("GET", OPENMETEO_API, params=params)
    hourly = data.get("hourly") or {}
    # An instantaneous value is sampled at the closest UTC hour; precipitation refers
    # to the preceding hour (as documented by Open-Meteo).
    target = hour_utc.strftime("%Y-%m-%dT%H:%M")
    times = hourly.get("time") or []
    if target not in times:
        raise ToolError(f"No weather data for {target} UTC. Try a different model/date.")
    idx = times.index(target)
    reading = {"time_utc": hour_utc, "model": model}
    for variable in HOURLY:
        values = hourly.get(variable) or []
        reading[variable] = values[idx] if idx < len(values) else None
    if reading["temperature_2m"] is None:
        raise ToolError("Archive returned no temperature for this location/time.")
    return reading


def air_quality_at(latlng: list[float], when_utc: datetime) -> dict[str, float | None]:
    """Historical European CAMS air quality (2013+), when available.

    Failure to retrieve AQI should not prevent weather updates; never fabricate an AQI.
    """
    if when_utc.date() < date(2013, 1, 1):
        return {}
    target_hour = (when_utc + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    params = {
        "latitude": round(latlng[0], 5), "longitude": round(latlng[1], 5),
        "start_date": target_hour.date().isoformat(),
        "end_date": target_hour.date().isoformat(),
        "timezone": "UTC", "hourly": "us_aqi,european_aqi",
        # Auto uses CAMS Europe for Greek activities and CAMS Global where applicable.
        "domains": "auto",
    }
    try:
        data = api_json("GET", AIR_QUALITY_API, params=params)
    except RateLimitError:
        raise
    except ToolError as exc:
        print(f"  AQI unavailable; omitting it ({exc}).")
        return {}
    hourly = data.get("hourly") or {}
    target = target_hour.strftime("%Y-%m-%dT%H:%M")
    times = hourly.get("time") or []
    if target not in times:
        return {}
    idx = times.index(target)
    return {key: ((hourly.get(key) or [])[idx] if idx < len(hourly.get(key) or []) else None)
            for key in ("us_aqi", "european_aqi")}


def cardinal(degrees: Any) -> str | None:
    """Legacy eight-point compass helper, kept for API compatibility."""
    if not isinstance(degrees, (float, int)):
        return None
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(degrees / 45) % 8]


def local_time(utc_datetime: datetime, activity: dict) -> datetime:
    # Strava exposes the event's UTC offset, including DST when applicable.
    offset_seconds = activity.get("utc_offset")
    if isinstance(offset_seconds, (int, float)):
        return utc_datetime.astimezone(timezone(timedelta(seconds=int(offset_seconds))))
    return utc_datetime  # Explicit UTC fallback is safer than guessing location.


def make_weather_payload(
    activity: dict, model: str, title_template: str = DEFAULT_TITLE_TEMPLATE,
    description_template: str | None = None, temp_unit: str = "c", wind_unit: str = "m/s",
    fetch_aqi: bool = True, include_title_temperature: bool = False,
) -> tuple[str, str]:
    """Build both activity title and description, fetching each weather sample only once."""
    start = parse_utc(activity["start_date"])
    start_coords = activity.get("start_latlng")
    if not start_coords:
        raise ToolError("Activity has no GPS start coordinates.")
    duration = max(0, int(activity.get("elapsed_time") or 0))
    end = start + timedelta(seconds=duration)
    end_coords = activity.get("end_latlng")
    def read_tags(coords: list[float], timestamp: datetime) -> dict[str, str]:
        reading = weather_at(coords, timestamp, model)
        if fetch_aqi:
            reading.update(air_quality_at(coords, timestamp))
        return make_tags(reading, timestamp, coords, local_time(timestamp, activity),
                         temp_unit, wind_unit)

    first = read_tags(start_coords, start)
    # End weather is an opt-in recipe field; default Strautomator prefix uses start only.
    end_requested = "weather.end." in (title_template + (description_template or ""))
    last = read_tags(end_coords, end) if end_coords and end_requested else None

    weather: dict[str, dict | None] = {
        "weather": first, "weather.start": first, "weather.end": last,
        # Strautomator uses midpoint conditions for >2h PRO activities.
        # We do not approximate a midpoint from a Strava summary polyline.
        "weather.mid": None,
    }
    name = activity.get("name") or "Untitled"
    # The opt-in flag affects only the default title; custom templates remain authoritative.
    if include_title_temperature and title_template == DEFAULT_TITLE_TEMPLATE:
        title_template += " ${weather.temperature}"
    title = render_template(title_template, name, weather).strip()
    if not title or len(title) > 255:
        raise ToolError("Generated activity title is empty or exceeds 255 characters.")
    # Exact user-supplied Strautomator sentence; put it FIRST in the description.
    # For custom templates, honor their placeholder layout (including start/end).
    if description_template is None:
        rendered = format_description(first)
    else:
        rendered = render_template(description_template.replace("\\n", "\n"), name, weather)
    block = rendered.strip()
    return title, block


def make_weather_block(activity: dict, model: str) -> str:
    """Compatibility entrypoint used by older callers; only returns description."""
    return make_weather_payload(activity, model)[1]


def read_title_state() -> dict[str, dict[str, str]]:
    try:
        state = json.loads(TITLE_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise ToolError("Cannot read title history file: " + str(TITLE_STATE_FILE)) from exc
    if not isinstance(state, dict):
        raise ToolError("Title history file must be a JSON object.")
    return state


def save_title_state(state: dict[str, dict[str, str]]) -> None:
    TITLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TITLE_STATE_FILE.with_suffix(".tmp")
    try:
        with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(tmp, TITLE_STATE_FILE)
        if os.name != "nt":
            TITLE_STATE_FILE.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)


def original_title(current: str, previous: dict | None) -> tuple[str, bool]:
    """Only strip our title suffix if a saved, exact match proves we wrote it."""
    if not previous:
        return current, True
    if current == previous.get("last_title"):
        return previous.get("base_name", current), True
    if current == previous.get("base_name"):
        return current, True
    return current, False  # User or another service edited the title. Leave it alone.


def merge_description(existing: str | None, block: str, refresh: bool,
                      previous_prefix: str | None = None) -> str | None:
    """Prepend weather, preserving notes and safely replacing managed content.

    v2's marker-delimited blocks can be migrated only with --refresh.
    v3's exact text is tracked in the local title-state JSON file, so a
    manually altered header is never silently overwritten or duplicated.
    """
    existing = existing or ""
    starts, ends = existing.count(START_MARKER), existing.count(END_MARKER)
    if starts != ends or starts > 1:
        raise ToolError("Existing description has malformed/duplicate weather markers; refusing to edit.")
    if previous_prefix:
        if not existing.startswith(previous_prefix):
            raise ToolError("Managed weather prefix was manually changed; refusing to overwrite or duplicate it.")
        if not refresh:
            return None
        old_text = existing[len(previous_prefix):].lstrip("\n")
    elif starts:
        if not refresh:
            return None
        pattern = re.compile(re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)
        old_text = pattern.sub("", existing, count=1).strip("\n")
    else:
        old_text = existing
    return block + ("\n\n" + old_text.lstrip("\n") if old_text.strip() else "")


def process_activity(strava: Strava, item: dict, args: argparse.Namespace) -> str:
    activity_id = item["id"]
    detail = strava.get_activity(activity_id)
    name = detail.get("name") or "Untitled"
    print(f"\n[{activity_id}] {name} — {detail.get('start_date_local', detail.get('start_date', ''))}")
    existing = detail.get("description") or ""
    if START_MARKER in existing and END_MARKER in existing and not args.refresh:
        print("  SKIP: historical weather already present (use --refresh to replace).")
        return "skipped"
    if not detail.get("start_latlng"):
        print("  SKIP: activity has no GPS coordinates.")
        return "skipped"
    try:
        state = read_title_state()
        old_state = state.get(str(activity_id))
        if old_state and old_state.get("description_prefix") and not args.refresh:
            print("  SKIP: managed weather prefix already present (use --refresh to replace).")
            return "skipped"
        base_name, safe_to_edit = original_title(name, old_state)
        weather_activity = {**detail, "name": base_name}
        generated_title, block = make_weather_payload(
            weather_activity, args.model,
            title_template=getattr(args, "title_template", DEFAULT_TITLE_TEMPLATE),
            description_template=getattr(args, "description_template", None),
            temp_unit=getattr(args, "temp_unit", "c"),
            wind_unit=getattr(args, "wind_unit", "m/s"),
            fetch_aqi=not getattr(args, "no_aqi", False),
            include_title_temperature=getattr(args, "include_title_temperature", False),
        )
        merged = merge_description(existing, block, args.refresh,
                                   old_state.get("description_prefix") if old_state else None)
        if merged is None:
            print("  SKIP: already tagged.")
            return "skipped"
        if len(merged) > 10000:
            raise ToolError("Description exceeds the conservative 10,000-character safety limit.")
        title_to_write: str | None = generated_title if safe_to_edit and generated_title != name else None
        if not safe_to_edit:
            print("  NOTICE: title changed since this tool last wrote it. Preserving the edited title.")
        print("  Title:", title_to_write if title_to_write is not None else f"(unchanged) {name}")
        print("\n".join("  " + line for line in block.splitlines()))
        if merged == existing and title_to_write is None:
            print("  SKIP: contents already up-to-date.")
            return "skipped"
        if args.apply:
            strava.update_activity(activity_id, merged, title_to_write)
            if safe_to_edit:
                state[str(activity_id)] = {"base_name": base_name, "last_title": generated_title,
                                           "description_prefix": block}
            else:
                state[str(activity_id)] = {**(old_state or {}), "description_prefix": block}
            save_title_state(state)
            print("  UPDATED on Strava.")
            return "updated"
        print("  DRY RUN: no changes made. Add --apply to write the title and description.")
        return "previewed"
    except RateLimitError:
        raise
    except (ToolError, ValueError) as exc:
        print(f"  SKIP: {exc}")
        return "skipped"


def parsed_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD") from exc


def backfill(args: argparse.Namespace) -> None:
    if args.after and args.before and args.after > args.before:
        raise ToolError("--after must not be later than --before.")
    if args.activity_id and (args.after or args.before):
        raise ToolError("Use --activity-id alone, or use --after and --before for a date range.")
    if not args.activity_id and not (args.after and args.before):
        raise ToolError("Specify both --after and --before, or a single --activity-id (prevents unbounded scans).")
    strava = Strava(delay=args.delay)
    print(f"Mode: {'APPLY changes to Strava' if args.apply else 'DRY RUN (read-only)'}; model={args.model}")
    totals = {"updated": 0, "previewed": 0, "skipped": 0}
    if args.activity_id:
        item = {"id": args.activity_id}
        outcome = process_activity(strava, item, args)
        totals[outcome] += 1
    else:
        count = 0
        for item in strava.list_activities(args.after, args.before, args.max_pages):
            if count >= args.limit:
                break
            # Manual / indoor activities are often missing coordinates.
            if not item.get("start_latlng"):
                continue
            count += 1
            outcome = process_activity(strava, item, args)
            totals[outcome] += 1
            if count >= args.limit:
                break
        if count == 0:
            print("No GPS activities found in the searched pages/date range.")
    print("\nSummary: " + ", ".join(f"{key}={value}" for key, value in totals.items()))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Add historical weather to your own Strava activity titles and descriptions.")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Authorize via local browser + Strava OAuth")
    auth.add_argument("--port", type=int, default=8765, help="Localhost callback port (default: 8765)")
    auth.add_argument("--no-browser", action="store_true", help="Print URL instead of opening your browser")
    batch = commands.add_parser("backfill", help="Preview historical weather; --apply to update Strava")
    batch.add_argument("--after", type=parsed_date, help="Inclusive UTC date YYYY-MM-DD")
    batch.add_argument("--before", type=parsed_date, help="Inclusive UTC date YYYY-MM-DD")
    batch.add_argument("--activity-id", type=int, help="Process just this activity ID")
    batch.add_argument("--limit", type=int, default=10, help="Maximum GPS activities to process (default: 10)")
    batch.add_argument("--max-pages", type=int, default=10, help="Maximum Strava listing pages, 100/page (default: 10)")
    batch.add_argument("--model", choices=["best_match", "era5", "era5_land", "cerra"], default="best_match")
    batch.add_argument("--title-template", default=DEFAULT_TITLE_TEMPLATE,
                       help="Custom title tags, e.g. '${name} ${weather.icon} ${weather.temperature}'")
    batch.add_argument("--include-title-temperature", action="store_true", default=False,
                       help="Include temperature in the default title (default: false; custom title templates are unaffected)")
    batch.add_argument("--description-template", help="Custom weather block body (supports escaped \\n or literal newlines)")
    batch.add_argument("--temp-unit", choices=["c", "f"], default="c", help="Temperature unit (default: Celsius)")
    batch.add_argument("--wind-unit", choices=["kph", "mph", "m/s"], default="m/s", help="Wind speed unit (default: m/s)")
    batch.add_argument("--no-aqi", action="store_true", help="Do not query historical air quality; AQI is omitted")
    batch.add_argument("--delay", type=float, default=2.0, help="Minimum seconds between Strava API calls (default: 2)")
    batch.add_argument("--refresh", action="store_true", help="Replace an existing block created by this tool")
    batch.add_argument("--apply", action="store_true", help="Actually write to Strava (default: dry run)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "auth":
            if not 1 <= args.port <= 65535:
                raise ToolError("--port must be between 1 and 65535")
            authenticate(port=args.port, open_browser=not args.no_browser)
        elif args.command == "backfill":
            try:
                validate_template(args.title_template)
                if args.description_template:
                    args.description_template = args.description_template.replace("\\n", "\n")
                    validate_template(args.description_template)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if args.limit < 1 or args.max_pages < 1 or args.delay < 0:
                raise ToolError("--limit and --max-pages must be >=1; --delay must be >=0")
            if args.activity_id is not None and args.activity_id <= 0:
                raise ToolError("--activity-id must be positive")
            backfill(args)
        return 0
    except (ToolError, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc or 'Interrupted'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())