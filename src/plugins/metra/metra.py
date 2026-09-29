import json
import logging
from datetime import datetime, timedelta

import pytz

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.metra import gtfs

logger = logging.getLogger(__name__)

DEFAULT_LINE = "UP-NW"
DEFAULT_ORIGIN = "EDISONPK"
DEFAULT_DESTINATION = "OTC"
DEFAULT_DEPARTURE_COUNT = 4
DEFAULT_WALK_MINUTES = 10

MAX_DEPARTURES = 8


class Metra(BasePlugin):
    """Departure board for a Metra commuter rail trip.

    Scheduled times come from Metra's public GTFS static feed. When a Metra
    developer API key is configured (METRA_API_KEY), realtime delays,
    cancellations and service alerts are layered on top.
    """

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        template_params["api_key"] = {
            "required": False,
            "service": "Metra GTFS Realtime (optional)",
            "expected_key": "METRA_API_KEY",
        }

        lines, stops_by_line = self._load_line_options()
        template_params["metra_lines"] = lines
        template_params["metra_stops_json"] = json.dumps(stops_by_line)
        template_params["metra_defaults"] = {
            "line": DEFAULT_LINE,
            "origin": DEFAULT_ORIGIN,
            "destination": DEFAULT_DESTINATION,
            "departureCount": DEFAULT_DEPARTURE_COUNT,
            "walkMinutes": DEFAULT_WALK_MINUTES,
        }
        return template_params

    def generate_image(self, settings, device_config):
        line = (settings.get("metraLine") or "").strip()
        origin_id = (settings.get("originStop") or "").strip()
        destination_id = (settings.get("destinationStop") or "").strip()

        if not line:
            raise RuntimeError("Metra line is required.")
        if not origin_id or not destination_id:
            raise RuntimeError("Origin and destination stations are required.")
        if origin_id == destination_id:
            raise RuntimeError("Origin and destination stations must be different.")

        departure_count = self._as_int(settings.get("departureCount"), DEFAULT_DEPARTURE_COUNT, 1, MAX_DEPARTURES)
        walk_minutes = self._as_int(settings.get("walkMinutes"), DEFAULT_WALK_MINUTES, 0, 180)

        tz = pytz.timezone(device_config.get_config("timezone", default="America/Chicago"))
        now = datetime.now(tz)

        meta = gtfs.ensure_feed()
        if not meta:
            raise RuntimeError("Metra schedule data is unavailable. Check the device's network connection.")

        route_index = gtfs.load_route_index(line)
        if not route_index:
            raise RuntimeError(f"No Metra schedule data found for line '{line}'.")

        departures = gtfs.find_departures(
            meta, route_index, origin_id, destination_id, now, tz, limit=departure_count
        )
        if not departures:
            raise RuntimeError(
                f"No upcoming {line} trains found from "
                f"{self._stop_name(meta, origin_id)} to {self._stop_name(meta, destination_id)}."
            )

        api_key = device_config.load_env_key("METRA_API_KEY")
        use_realtime = settings.get("useRealtime", "true") != "false"
        realtime_active = False
        alerts = []

        if api_key and use_realtime:
            try:
                realtime_active = gtfs.apply_trip_updates(departures, api_key, tz)
            except Exception as error:
                logger.warning(f"Metra realtime trip updates failed: {error}")
            if settings.get("showAlerts", "true") != "false":
                try:
                    alerts = gtfs.fetch_alerts(line, api_key, now)
                except Exception as error:
                    logger.warning(f"Metra realtime alerts failed: {error}")

        departures.sort(key=lambda item: item["departure"])
        rows = [self._build_row(item, now, walk_minutes) for item in departures]
        featured = next((row for row in rows if not row["cancelled"] and row["leave_in"] >= 0), None)
        if featured is None:
            featured = next((row for row in rows if not row["cancelled"]), rows[0])
        featured["featured"] = True

        route = self._route_meta(meta, line)
        hero = self._build_hero(featured, walk_minutes)
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params = {
            "route": route,
            "origin_name": self._stop_name(meta, origin_id),
            "destination_name": self._stop_name(meta, destination_id),
            "departures": rows,
            "featured": featured,
            "hero": hero,
            "walk_minutes": walk_minutes,
            "alerts": alerts,
            "realtime": realtime_active,
            "realtime_available": bool(api_key) and use_realtime,
            "updated_at": self._format_time(now),
            "compact": len(rows) > 4,
            "narrow": dimensions[0] < dimensions[1],
            "plugin_settings": settings,
        }

        return self.render_image(dimensions, "metra.html", "metra.css", template_params)

    # ----------------------------------------------------------------- #
    # Helpers
    # ----------------------------------------------------------------- #

    def _build_row(self, departure, now, walk_minutes):
        leave_at = departure["departure"] - self._minutes(walk_minutes)
        depart_in = int((departure["departure"] - now).total_seconds() // 60)
        leave_in = int((leave_at - now).total_seconds() // 60)
        duration = int((departure["arrival"] - departure["departure"]).total_seconds() // 60)
        delay = departure["delay_minutes"]

        if departure["cancelled"]:
            status = "Cancelled"
            status_kind = "cancelled"
        elif delay >= 1:
            status = f"+{delay} min"
            status_kind = "late"
        elif delay <= -1:
            status = f"{delay} min"
            status_kind = "early"
        elif departure["realtime"]:
            status = "On time"
            status_kind = "ontime"
        else:
            status = "Scheduled"
            status_kind = "scheduled"

        return {
            "train": departure["train"],
            "headsign": departure["headsign"],
            "depart": self._format_time(departure["departure"]),
            "arrive": self._format_time(departure["arrival"]),
            "scheduled_depart": self._format_time(departure["scheduled_departure"]),
            "duration": duration,
            "depart_in": depart_in,
            "leave_in": leave_in,
            "leave_at": self._format_time(leave_at),
            "status": status,
            "status_kind": status_kind,
            # A cancelled row is struck through in full, so the original time
            # would only add noise there.
            "show_scheduled": bool(delay) and not departure["cancelled"],
            "cancelled": departure["cancelled"],
            "realtime": departure["realtime"],
            "delay": delay,
            "featured": False,
        }

    @staticmethod
    def _build_hero(featured, walk_minutes):
        """Describes the headline 'when do I need to leave' state.

        Shows a wall-clock time rather than a countdown so the board stays
        accurate between refreshes.
        """
        if featured["cancelled"]:
            return {"kind": "cancelled", "text": "\u2014", "label": "Next train cancelled"}

        if featured["leave_in"] < 0:
            return {"kind": "leave_now", "text": "Now", "label": "Head out"}

        return {
            "kind": "leave_at",
            "time": featured["leave_at"],
            "label": "Out the door" if walk_minutes else "Departs",
        }

    def _load_line_options(self):
        """Builds the line/station dropdown data for the settings page."""
        try:
            meta = gtfs.ensure_feed()
        except Exception as error:
            logger.warning(f"Unable to load Metra GTFS feed for settings page: {error}")
            meta = None

        if not meta:
            return [], {}

        stop_names = meta.get("stops", {})
        lines = []
        stops_by_line = {}
        for route in meta.get("routes", []):
            route_id = route["id"]
            stop_ids = meta.get("route_stops", {}).get(route_id) or []
            if not stop_ids:
                continue
            lines.append({
                "id": route_id,
                "name": f"{route['short_name']} \u2013 {route['long_name']}",
            })
            stops_by_line[route_id] = [
                {"id": stop_id, "name": stop_names.get(stop_id, stop_id)} for stop_id in stop_ids
            ]
        return lines, stops_by_line

    @staticmethod
    def _route_meta(meta, line):
        for route in meta.get("routes", []):
            if route["id"] == line:
                return route
        return {"id": line, "short_name": line, "long_name": line, "color": "000000", "text_color": "FFFFFF"}

    @staticmethod
    def _stop_name(meta, stop_id):
        return meta.get("stops", {}).get(stop_id, stop_id)

    @staticmethod
    def _format_time(value):
        """Splits a time into '7:42' + 'a' so the meridiem can be styled smaller."""
        if not hasattr(value, "strftime"):
            return {"hm": "", "ap": ""}
        return {
            "hm": value.strftime("%-I:%M"),
            "ap": "a" if value.hour < 12 else "p",
        }

    @staticmethod
    def _minutes(count):
        return timedelta(minutes=count)

    @staticmethod
    def _as_int(value, default, minimum, maximum):
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, parsed))
