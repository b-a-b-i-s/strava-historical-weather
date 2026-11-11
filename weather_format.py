"""Strautomator-style weather tags and recipe rendering for Open-Meteo readings.

Independent Python implementation of the field, unit and icon conventions visible
in Strautomator's public source. Strautomator recipes are user-defined: there is
no single built-in activity title or description template.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

MISSING = "N/A"
TAG_RE = re.compile(r"\$?\{([a-zA-Z][\w.]*)\}")
WEATHER_FIELDS = (
    "icon", "summary", "temperature", "feelsLike", "humidity", "dewPoint", "pressure",
    "windSpeed", "windGust", "windDirection", "precipitation", "airDensity", "aqi",
    "aqiIcon", "moon", "cloudCover", "time",
)

DEFAULT_TITLE_TEMPLATE = "${name} ${weather.icon}"
DEFAULT_DESCRIPTION_TEMPLATE = (
    "${weather.summary}, ${weather.temperature}, "
    "Feels like ${weather.feelsLike}, Humidity ${weather.humidity}, "
    "Wind ${weather.windSpeed} from ${weather.windDirection}, "
    "Precipitation ${weather.precipitation}"
)
# Keep these aliases for code that imports the older constants.
DEFAULT_START_LINE = DEFAULT_DESCRIPTION_TEMPLATE
DEFAULT_END_LINE = DEFAULT_DESCRIPTION_TEMPLATE

AQI_ICONS = {0: "🟢", 1: "🟡", 2: "🟠", 3: "🔴", 4: "🟣", 5: "🟤"}


def strautomator_aqi(us: Any, eu: Any) -> tuple[str, str]:
    """Mirror Strautomator's Open-Meteo US-first, EU-fallback AQI bucketing."""
    us_value, eu_value = _number(us), _number(eu)
    if us_value is None and eu_value is None:
        return MISSING, MISSING
    level = 0
    if us_value is not None:
        if us_value > 300: level = 5
        elif us_value > 200: level = 4
        elif us_value > 150: level = 3
        elif us_value > 100: level = 2
        elif us_value > 50: level = 1
    if level == 0 and eu_value is not None:
        if eu_value > 80: level = 5
        elif eu_value > 60: level = 4
        elif eu_value > 40: level = 3
        elif eu_value > 20: level = 2
        elif eu_value > 0: level = 1
    return str(level), AQI_ICONS[level]


def format_description(tags: dict[str, str]) -> str:
    """Render the exact Strautomator-style weather sentence, omitting unavailable AQI."""
    text = render_template(DEFAULT_DESCRIPTION_TEMPLATE, "", {"weather": tags})
    if tags.get("aqi") not in (None, MISSING, ""):
        text += f", AQI {tags['aqi']} {tags['aqiIcon']}"
    return text


def js_round(value: float) -> int:
    """Match JavaScript Math.round, used in Strautomator, for common signed values."""
    return math.floor(value + 0.5)


def compass_16(degrees: float) -> str:
    return (
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW",
        "WSW", "W", "WNW", "NW", "NNW",
    )[math.floor(degrees / 22.5 + 0.5) % 16]


def daylight(dt: datetime, latitude: float, longitude: float) -> bool:
    """Approximate apparent solar altitude, used only to select moon/sun emoji."""
    dt = dt.astimezone(timezone.utc)
    n = dt.timestamp() / 86400 + 2440587.5 - 2451545.0
    mean_longitude = (280.460 + 0.9856474 * n) % 360
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic = math.radians((mean_longitude + 1.915 * math.sin(anomaly) + 0.020 * math.sin(2 * anomaly)) % 360)
    obliquity = math.radians(23.439 - 0.0000004 * n)
    right_ascension = math.degrees(math.atan2(math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic))) % 360
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    sidereal_degrees = (280.46061837 + 360.98564736629 * n + longitude) % 360
    hour_angle = math.radians((sidereal_degrees - right_ascension + 180) % 360 - 180)
    lat_radians = math.radians(latitude)
    sine_altitude = math.sin(lat_radians) * math.sin(declination) + math.cos(lat_radians) * math.cos(declination) * math.cos(hour_angle)
    altitude = math.degrees(math.asin(max(-1, min(1, sine_altitude))))
    return altitude >= -0.833


def full_moon_approx(dt: datetime) -> bool:
    reference = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    phase = ((dt.astimezone(timezone.utc) - reference).total_seconds() / 86400) % 29.5305882
    return abs(phase - 29.5305882 / 2) < 0.9


def condition(code: Any, cloud_cover: Any, precipitation: Any) -> str:
    """Map WMO weather codes onto Strautomator's icon categories."""
    if isinstance(code, (float, int)):
        code = int(code)
        if code in (95, 96, 99):
            return "Thunderstorm"
        if code in (45, 48):
            return "Fog"
        if code in (71, 73, 75, 77, 85, 86):
            return "Snow"
        if code in (56, 57, 66, 67):
            return "Sleet"
        if code in (51, 53, 55):
            return "Drizzle"
        if code in (61, 63, 65, 80, 81, 82):
            return "Rain"
        if code == 0:
            return "Clear"
        if code == 1:
            return "MostlyClear"
        if code == 2:
            return "MostlyCloudy"
        if code == 3:
            return "Cloudy"
    if isinstance(precipitation, (float, int)) and precipitation > 0:
        return "Rain" if precipitation >= 1 else "Drizzle"
    if isinstance(cloud_cover, (float, int)):
        if cloud_cover > 70:
            return "Cloudy"
        if cloud_cover > 30:
            return "MostlyCloudy"
        if cloud_cover > 10:
            return "MostlyClear"
    return "Clear"


def _number(value: Any) -> float | None:
    if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _temperature_label(value: float | None, unit: str) -> str:
    if value is None:
        return MISSING
    if unit == "f":
        return f"{js_round(value * 9 / 5 + 32)}°F"
    return f"{js_round(value)}°C"


def _wind_label(value_kph: float | None, unit: str) -> str:
    if value_kph is None:
        return MISSING
    conversions = {"kph": (1, "kph"), "mph": (1 / 1.609344, "mph"), "m/s": (1 / 3.6, "m/s")}
    multiplier, suffix = conversions[unit]
    return f"{js_round(value_kph * multiplier)} {suffix}"


def make_tags(
    reading: dict[str, Any], when: datetime, coords: list[float], local_time: datetime,
    temp_unit: str = "c", wind_unit: str = "m/s",
) -> dict[str, str]:
    """Create canonical Strautomator tags from one Open-Meteo historical hourly reading."""
    temp = _number(reading.get("temperature_2m"))
    feels = _number(reading.get("apparent_temperature"))
    humidity = _number(reading.get("relative_humidity_2m"))
    pressure = _number(reading.get("surface_pressure"))
    wind_kph = _number(reading.get("wind_speed_10m"))
    gust_kph = _number(reading.get("wind_gusts_10m"))
    wind_degrees = _number(reading.get("wind_direction_10m"))
    cloud_cover = _number(reading.get("cloud_cover"))
    precipitation = _number(reading.get("precipitation"))
    icon_text = condition(reading.get("weather_code"), cloud_cover, precipitation)
    day = daylight(when, coords[0], coords[1])
    icon = {
        "Clear": "☀️" if day else "🌕" if full_moon_approx(when) else "🌙",
        "MostlyClear": "🌤️",
        "MostlyCloudy": "☁️" if day else "🌙",
        "Cloudy": "☁️" if day else "🌙",
        "Drizzle": "🌧️",
        "Rain": "🌧️",
        "Sleet": "🌨️",
        "Snow": "❄️",
        "Fog": "😶‍🌫️",
        "Thunderstorm": "⛈️",
    }[icon_text]
    temp_label = "Cool"
    if temp is not None:
        if temp > 40:
            temp_label = "Extremely warm"
        elif temp > 30:
            temp_label = "Very warm"
        elif temp > 22:
            temp_label = "Warm"
        elif temp < -10:
            temp_label = "Extremely cold"
        elif temp < 2:
            temp_label = "Very cold"
        elif temp < 12:
            temp_label = "Cold"
    friendly_condition = {"MostlyClear": "Mostly clear", "MostlyCloudy": "Mostly cloudy"}.get(icon_text, icon_text.lower())
    summary = f"{temp_label}, {friendly_condition}"
    if (wind_kph is not None and wind_kph > 72) or (gust_kph is not None and gust_kph > 144):
        summary += ", windy"
    if precipitation is not None and precipitation > 0:
        precip_type = "snow" if icon_text == "Snow" else "sleet" if icon_text == "Sleet" else "rain" if icon_text in ("Rain", "Thunderstorm") else "drizzle"
        if precipitation > 20:
            precip_type += " (heavy)"
    else:
        precip_type = "dry"
    density = MISSING
    if temp is not None and pressure is not None and humidity is not None:
        # Standard approximation in kg/m³; wind/weather display uses matching unit conventions.
        temp_kelvin = temp + 273.15
        saturation_hpa = 6.112 * math.exp(17.67 * temp / (temp + 243.5))
        vapor_pa = humidity / 100 * saturation_hpa * 100
        dry_pa = pressure * 100 - vapor_pa
        density = f"{dry_pa / (287.05 * temp_kelvin) + vapor_pa / (461.495 * temp_kelvin):.3f} kg/m³"
    aqi, aqi_icon = strautomator_aqi(reading.get("us_aqi"), reading.get("european_aqi"))
    tags = {
        "icon": icon,
        "summary": summary,
        "temperature": _temperature_label(temp, temp_unit),
        "feelsLike": _temperature_label(feels if feels is not None else temp, temp_unit),
        "humidity": f"{js_round(humidity)}%" if humidity is not None else MISSING,
        "dewPoint": _temperature_label(_number(reading.get("dew_point_2m")), temp_unit),
        "pressure": f"{js_round(pressure)} hPa" if pressure is not None else MISSING,
        "windSpeed": _wind_label(wind_kph, wind_unit),
        "windGust": _wind_label(gust_kph, wind_unit),
        "windDirection": compass_16(wind_degrees) if wind_degrees is not None else MISSING,
        "precipitation": precip_type,
        "airDensity": density,
        "cloudCover": f"{js_round(cloud_cover)}%" if cloud_cover is not None else MISSING,
        "aqi": aqi,
        "aqiIcon": aqi_icon,
        "moon": "full" if full_moon_approx(when) else "quarter",
        "time": local_time.strftime("%H:%M"),
    }
    return tags


def template_variables(template: str) -> set[str]:
    return set(TAG_RE.findall(template))


def validate_template(template: str) -> None:
    valid = {"name"}
    for prefix in ("weather", "weather.start", "weather.end", "weather.mid"):
        valid.update(f"{prefix}.{key}" for key in WEATHER_FIELDS)
    unknown = template_variables(template) - valid
    if unknown:
        raise ValueError("Unsupported template tag(s): " + ", ".join(sorted(unknown)))


def render_template(template: str, name: str, weather: dict[str, dict[str, str] | None]) -> str:
    """Render Strautomator's ${weather.icon} or UI-style {weather.icon} placeholders."""
    validate_template(template)
    mapping: dict[str, str] = {"name": name}
    for key in ("weather", "weather.start", "weather.end", "weather.mid"):
        data = weather.get(key)
        for field in WEATHER_FIELDS:
            mapping[f"{key}.{field}"] = data.get(field, MISSING) if data else MISSING
    return TAG_RE.sub(lambda match: mapping[match.group(1)], template)