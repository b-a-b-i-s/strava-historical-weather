# Strava Historical Weather Backfill

Small, standalone Python CLI for adding **historical Open-Meteo weather** to your own **Strava activity titles and descriptions**, with no Strautomator account, Google Cloud, or public webhook URL needed.

- Historical weather estimates from **1940 onward** (availability depends on model; ERA5 starts in 1940, ERA5-Land in 1950, CERRA in 1985 and ends in 2021).
- Uses start GPS location/time for the default weather prefix. End weather is queried only when your custom recipe contains `${weather.end.*}`; uses elapsed duration, not moving time.
- **Default description precisely follows your Strautomator recipe format**: summary, temperature, feels-like, humidity, wind in **m/s**, compass direction, precipitation, and optional AQI level + icon.
- Hourly temperature, feels-like, humidity, wind direction/speed, precipitation (from **the preceding hour**), and weather condition.
- **Historical AQI since 2013 where CAMS data exists**, using Open-Meteo Air Quality API. If missing, AQI is simply omitted; use `--no-aqi` to disable those requests.
- **Dry run by default**; `--apply` is required to modify an activity.
- Prepends the weather sentence before your existing description, keeping existing notes intact. `--refresh` replaces this tool's managed prefix and renews previously generated title suffixes.
- Stores generated titles **and the exact managed description prefix** in `.private/title-state.json` on `--apply`, so refreshes don't duplicate text. If another service changes the title or prefix afterward, the tool protects the edits.
- Refreshes Strava OAuth tokens automatically and stores them locally.
- Works on Linux, macOS, and Windows. Can run on a VPS/mini PC without any inbound public ports once you've authenticated locally.

## Installation

Requires Python 3.11+ and optional [`uv`](https://docs.astral.sh/uv/).

```bash
unzip strava-historical-weather-v4.zip
cd strava-historical-weather
cp .env.example .env
```

On Windows, use `copy .env.example .env` instead of `cp`. Fill in your own Strava credentials in `.env`.

`uv` (recommended):

```bash
uv sync
uv run python strava_weather.py auth
```

Alternatively:

```bash
python -m pip install 'requests>=2.32,<3' 'python-dotenv>=1.0,<2'
python strava_weather.py auth
```

## One-time Strava application setup

1. Create your own Strava API application: <https://www.strava.com/settings/api>. Strava currently requires a subscription to create a new application. A single-player personal app works fine.
2. Set **Authorization Callback Domain** to **`localhost`** (just the host, not an `http://` URL).
3. Copy **Client ID** and **Client Secret** into `.env`.
4. Run `uv run python strava_weather.py auth` **on the same machine where you open your browser**.
5. Approve the `activity:read_all` and `activity:write` permissions. The app receives the OAuth callback at `http://localhost:8765/callback` and saves `.private/tokens.json`.

Secrets and tokens are excluded from Git via `.gitignore`. **Do not publish or share them.** If running headlessly on a remote server, authorize on your local computer and securely transfer `.env` and `.private/tokens.json` to the same project paths on the remote host. It will then refresh tokens without running a callback server. Avoid running copies concurrently against the same refresh token.

## Usage

Test a single activity (read-only preview):

```bash
uv run python strava_weather.py backfill --activity-id 1234567890
```

Actually update that activity:

```bash
uv run python strava_weather.py backfill --activity-id 1234567890 --apply
```

Preview up to 10 GPS activities from a historical period:

```bash
uv run python strava_weather.py backfill --after 2019-01-01 --before 2019-12-31
```

Update up to 20 activities in the same period:

```bash
uv run python strava_weather.py backfill --after 2019-01-01 --before 2019-12-31 --limit 20 --apply
```

For a consistent long-term dataset, use ERA5-Land (from 1950):

```bash
uv run python strava_weather.py backfill --after 2012-01-01 --before 2012-12-31 --model era5_land --limit 20 --apply
```

For dates starting in 1940, use `--model era5`. The default `best_match` selects the best available model, which may differ by date.

To replace weather previously added **by this tool**:

```bash
uv run python strava_weather.py backfill --activity-id 1234567890 --refresh --apply
```

Use `--max-pages 20` to scan more Strava listing pages; pagination is capped by default to protect rate limits. The date arguments refer to **UTC dates**, while display times use the activity's local offset when Strava provides it. Work through older years in smaller date-range batches to avoid rate limits. If the API returns HTTP 429, the program stops without automatically retrying. Check your Strava API dashboard before running further requests.

## Strautomator-compatible title and description templates

Strautomator **does not have one fixed weather format**: its automation recipes insert user-selected fields into custom activity titles/descriptions. This tool follows the same field names, whole-number unit conventions, weather emoji and English summaries. It lets you use the same template placeholders to reproduce your particular recipe.

**Default title:**

```text
${name} ${weather.icon} ${weather.temperature}
```

Example (illustrative): `Morning Run 🌤️ 18°C`.

**Default description (prepended):**

```text
Cool, cloudy, 15°C, Feels like 15°C, Humidity 79%, Wind 1 m/s from N, Precipitation dry, AQI 1 🟡

[Your existing activity description stays here]
```

The generated description does not append a source line. AQI is shown **only if historical data is available**, and its color/level follows Strautomator's 0–5 classification. For activities older than the available air-quality archive (Europe: 2013 onward), that last `AQI ...` clause is omitted. Temperature defaults to °C and wind to **m/s**.


You can copy a title/description recipe from Strautomator using `${weather.*}` placeholders (or its UI's `{weather.*}` syntax). Supported fields:

| Tag | Example output |
|---|---|
| `${name}` | Original title (preserved by default) |
| `${weather.icon}` | 🌤️ |
| `${weather.summary}` | Cool, mostly cloudy |
| `${weather.temperature}` | 18°C |
| `${weather.feelsLike}` | 17°C |
| `${weather.humidity}` | 62% |
| `${weather.windSpeed}` | 3 m/s |
| `${weather.windDirection}` | NW |
| `${weather.windGust}` | 6 m/s |
| `${weather.precipitation}` | dry, rain, snow, etc. |
| `${weather.dewPoint}` / `${weather.pressure}` | 10°C / 1012 hPa |
| `${weather.start.icon}` / `${weather.end.temperature}` | Start/end-specific values |

Also available: `${weather.airDensity}`, `${weather.cloudCover}`, `${weather.aqi}`, and `${weather.aqiIcon}`. Historical AQI is looked up separately when available, otherwise shows `N/A` in custom templates (and is omitted in the default sentence). `${weather.mid.*}` is currently `N/A` because no midpoint GPS/time sample is collected. The unqualified `${weather.*}` uses **start** weather, rather than Strautomator's optional PRO midpoint. The icon/summary interpretation may differ for some WMO weather codes or astronomical edge cases, because Open-Meteo's archive datasets and Strautomator's supported weather APIs are not identical.

**Example custom recipe for both fields:**

```bash
uv run python strava_weather.py backfill --activity-id 1234567890 \
  --title-template '${name} ${weather.icon} ${weather.temperature}' \
  --description-template 'Start: ${weather.start.summary}, ${weather.start.temperature} / End: ${weather.end.summary}, ${weather.end.temperature}'
```

For a multi-line custom description, use literal newlines or escaped `\n` in `--description-template`. For Fahrenheit and mph, add `--temp-unit f --wind-unit mph`. The default is **°C** and **m/s**.

Run with no `--apply` first to inspect the **new title and description**. When you're happy, add `--apply`.

To reformat an activity previously handled by an older version, including to **remove the v3 source line**, use:

```bash
uv run python strava_weather.py backfill --activity-id 1234567890 --refresh
uv run python strava_weather.py backfill --activity-id 1234567890 --refresh --apply
```

The first run previews the changes. Refresh can migrate the marker-delimited block added by **v1/v2**; v3 and v4 store the exact prefix in `.private/title-state.json` and refuses to replace it if someone manually changes that prefix. **Keep this state file when upgrading/moving the tool.** It does not remove weather text written by Strautomator itself. Check the preview to avoid preexisting prefixes or title suffixes added by another application.

## Privacy and limitations

- GPS coordinates and timestamps are sent to Open-Meteo for weather and optional AQI lookup; Strava is contacted only for your activity list/details and for updates you explicitly approve with `--apply`.
- Only activities with GPS start coordinates can be processed. The default sentence uses the **start** location, and optional custom templates can request the end location. Point samples do **not** describe conditions along an entire route; they are estimates.
- Weather is based on model/reanalysis grid cells, not personal weather-station observations. Local rainfall, mountain microclimates, and short-lived wind conditions may differ.
- The API samples the nearest hour. Open-Meteo precipitation is an accumulation for the **preceding hour**, not a claim of rain at the exact activity minute.
- Strava only exposes a subset of activities based on your granted API permissions. Rate limits depend on your app's access tier.
- For `--refresh`, v1/v2 marker blocks are migrated and v3 description prefixes are replaced only when they exactly match the saved `.private/title-state.json` state. Other description text remains unchanged. Back up especially valuable descriptions first. The tool checks a conservative 10,000-character maximum and refuses to change descriptions exceeding it.
- Open-Meteo's free API is for **non-commercial use**, and its weather data is under **CC BY 4.0**, which requires attribution when shared. The generated Strava description **no longer includes a source line**, as requested. When publishing weather data, arrange appropriate attribution outside the generated description (for example, a clearly discoverable credit on your Strava profile), bearing in mind that Open-Meteo specifically requests a link next to displayed data. See [Open-Meteo's terms](https://open-meteo.com/en/terms) and [data license](https://creativecommons.org/licenses/by/4.0/) for the requirements.
- API calls may fail temporarily, and not every weather variable exists in every model. Missing AQI does not block weather processing; a rate-limit response stops processing. Such activities can be retried.

## Testing (no API credentials needed)

```bash
uv run python -m unittest discover -s tests -v
```

## Official reference docs

- [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api)
- [Open-Meteo Air Quality API (historical CAMS data)](https://open-meteo.com/en/docs/air-quality-api)
- [Open-Meteo terms](https://open-meteo.com/en/terms)
- [Strava API authentication](https://developers.strava.com/docs/authentication/)
- [Strava activity endpoints](https://developers.strava.com/docs/reference/)

## Docker (optional self-hosting)

After completing the `auth` step **locally**, the same project directory can run on a NAS, mini PC, or VPS using Docker. Ensure `.env` and `.private/tokens.json` are present on that host, and keep them private.

```bash
docker compose build
# Preview one historical activity:
docker compose run --rm weather backfill --activity-id 1234567890
# Update only after checking the preview:
docker compose run --rm weather backfill --activity-id 1234567890 --apply
```

No ports need to be exposed for backfilling. You can invoke `docker compose run` via a host cron job if you want ongoing processing; pass a bounded date range each run. Docker `auth` is **not** offered because the localhost callback would be inside the container—authorize on a desktop first and copy the token file.