"""Metra GTFS helpers.

Handles the static GTFS schedule feed (public, no API key) and the optional
GTFS-Realtime feeds (require a free Metra developer API key).

Static feed:    https://schedules.metrarail.com/gtfs/schedule.zip
Realtime feeds: https://gtfspublic.metrarr.com/gtfs/public/{tripupdates,alerts}

The static feed is downloaded once per publish and compiled into a compact
per-route index so that each refresh only has to read a small JSON file instead
of re-parsing the ~12MB stop_times.txt.
"""

import csv
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime, time as dtime, timedelta

from utils.app_utils import resolve_path
from utils.http_client import get_http_session

logger = logging.getLogger(__name__)

SCHEDULE_URL = "https://schedules.metrarail.com/gtfs/schedule.zip"
PUBLISHED_URL = "https://schedules.metrarail.com/gtfs/published.txt"
REALTIME_BASE_URL = "https://gtfspublic.metrarr.com/gtfs/public"

CACHE_DIR = resolve_path(os.path.join("plugins", "metra", ".cache"))
META_FILE = os.path.join(CACHE_DIR, "meta.json")
CHECKED_FILE = os.path.join(CACHE_DIR, "checked_at")
ROUTES_DIR = os.path.join(CACHE_DIR, "routes")

# How long to trust the cached feed before re-checking published.txt.
PUBLISHED_CHECK_SECONDS = 6 * 60 * 60

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Each compiled route index is a few hundred KB, so keep only a handful in memory
# (enough for several plugin instances on different lines).
MAX_CACHED_ROUTES = 3

_BUILD_LOCK = threading.Lock()
_MEMO = {"meta": None, "meta_mtime": None, "routes": {}}


# --------------------------------------------------------------------------- #
# Small parsing helpers
# --------------------------------------------------------------------------- #

def _read_csv(zf, name):
    """Yields dicts from a GTFS text file. Metra pads its CSV with spaces."""
    with zf.open(name) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        try:
            header = [col.strip() for col in next(reader)]
        except StopIteration:
            return
        for row in reader:
            if not row:
                continue
            yield dict(zip(header, [col.strip() for col in row]))


def parse_gtfs_time(value):
    """Converts an 'HH:MM:SS' GTFS time into seconds past service-day midnight.

    GTFS allows hours >= 24 for trips that run past midnight, so this can
    legitimately return values greater than 86400.
    """
    if not value:
        return None
    parts = value.split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _route_index_path(route_id):
    return os.path.join(ROUTES_DIR, f"{_safe_name(route_id)}.json")


# --------------------------------------------------------------------------- #
# Feed download + index build
# --------------------------------------------------------------------------- #

def _fetch_published_version(timeout=10):
    """Returns Metra's schedule publish timestamp, or None if unreachable."""
    try:
        response = get_http_session().get(PUBLISHED_URL, timeout=timeout)
        response.raise_for_status()
        return response.text.strip()
    except Exception as error:
        logger.warning(f"Unable to check Metra GTFS publish version: {error}")
        return None


def _load_meta_from_disk():
    """Reads meta.json, memoized on file mtime."""
    try:
        mtime = os.path.getmtime(META_FILE)
    except OSError:
        return None

    if _MEMO["meta"] is not None and _MEMO["meta_mtime"] == mtime:
        return _MEMO["meta"]

    try:
        with open(META_FILE) as handle:
            meta = json.load(handle)
    except (OSError, ValueError) as error:
        logger.warning(f"Metra GTFS cache metadata is unreadable: {error}")
        return None

    _MEMO["meta"] = meta
    _MEMO["meta_mtime"] = mtime
    _MEMO["routes"] = {}
    return meta


def _build_index(zip_bytes, version):
    """Compiles the GTFS zip into meta.json plus one JSON file per route."""
    os.makedirs(ROUTES_DIR, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        routes = {}
        for row in _read_csv(zf, "routes.txt"):
            route_id = row.get("route_id")
            if not route_id:
                continue
            routes[route_id] = {
                "id": route_id,
                "short_name": row.get("route_short_name") or route_id,
                "long_name": row.get("route_long_name") or route_id,
                "color": row.get("route_color") or "000000",
                "text_color": row.get("route_text_color") or "FFFFFF",
            }

        stops = {}
        for row in _read_csv(zf, "stops.txt"):
            stop_id = row.get("stop_id")
            if stop_id:
                stops[stop_id] = row.get("stop_name") or stop_id

        services = {}
        for row in _read_csv(zf, "calendar.txt"):
            service_id = row.get("service_id")
            if not service_id:
                continue
            services[service_id] = {
                "start": row.get("start_date", ""),
                "end": row.get("end_date", ""),
                "days": [row.get(day) == "1" for day in DAY_KEYS],
            }

        exceptions = {}
        for row in _read_csv(zf, "calendar_dates.txt"):
            service_id = row.get("service_id")
            day = row.get("date")
            if not service_id or not day:
                continue
            exceptions.setdefault(day, {})[service_id] = row.get("exception_type")

        trips = {}
        for row in _read_csv(zf, "trips.txt"):
            trip_id = row.get("trip_id")
            route_id = row.get("route_id")
            if not trip_id or route_id not in routes:
                continue
            trips[trip_id] = {
                "id": trip_id,
                "route": route_id,
                "service": row.get("service_id", ""),
                "headsign": row.get("trip_headsign", ""),
                "dir": row.get("direction_id", ""),
                "stops": [],
            }

        # Single streaming pass over the largest file in the feed.
        for row in _read_csv(zf, "stop_times.txt"):
            trip = trips.get(row.get("trip_id"))
            if trip is None:
                continue
            stop_id = row.get("stop_id")
            if not stop_id:
                continue
            arrival = parse_gtfs_time(row.get("arrival_time"))
            departure = parse_gtfs_time(row.get("departure_time"))
            if arrival is None and departure is None:
                continue
            try:
                sequence = int(row.get("stop_sequence") or 0)
            except ValueError:
                sequence = 0
            trip["stops"].append([
                stop_id,
                arrival if arrival is not None else departure,
                departure if departure is not None else arrival,
                sequence,
            ])

    by_route = {route_id: [] for route_id in routes}
    for trip in trips.values():
        if not trip["stops"]:
            continue
        trip["stops"].sort(key=lambda entry: entry[3])
        by_route[trip["route"]].append(trip)

    route_stops = {}
    for route_id, route_trips in by_route.items():
        if not route_trips:
            continue

        # Use the longest trip in each direction to build a station ordering,
        # preferring direction 0 (outbound from Chicago) as the anchor.
        def longest(direction):
            candidates = [t for t in route_trips if t["dir"] == direction]
            if not candidates:
                return None
            return max(candidates, key=lambda t: len(t["stops"]))

        ordered = []
        seen = set()
        anchor = longest("0") or max(route_trips, key=lambda t: len(t["stops"]))
        for stop in anchor["stops"]:
            if stop[0] not in seen:
                seen.add(stop[0])
                ordered.append(stop[0])

        other = longest("1")
        if other:
            for stop in reversed(other["stops"]):
                if stop[0] not in seen:
                    seen.add(stop[0])
                    ordered.append(stop[0])

        route_stops[route_id] = ordered

        with open(_route_index_path(route_id), "w") as handle:
            json.dump({"route_id": route_id, "trips": route_trips}, handle, separators=(",", ":"))

    meta = {
        "version": version or "",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "routes": [routes[route_id] for route_id in sorted(routes) if route_stops.get(route_id)],
        "stops": stops,
        "route_stops": route_stops,
        "services": services,
        "exceptions": exceptions,
    }

    with open(META_FILE, "w") as handle:
        json.dump(meta, handle, separators=(",", ":"))

    _MEMO["meta"] = None
    _MEMO["meta_mtime"] = None
    _MEMO["routes"] = {}

    logger.info(f"Built Metra GTFS index. | version: {version} | routes: {len(meta['routes'])}")
    return meta


def _checked_recently():
    """True if published.txt was checked within the last PUBLISHED_CHECK_SECONDS."""
    try:
        age = time.time() - os.path.getmtime(CHECKED_FILE)
    except OSError:
        return False
    return 0 <= age < PUBLISHED_CHECK_SECONDS


def _mark_checked():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CHECKED_FILE, "w") as handle:
            handle.write(datetime.now().isoformat(timespec="seconds"))
    except OSError:
        pass


def ensure_feed(force=False, allow_download=True):
    """Returns feed metadata, downloading/rebuilding the cache when stale.

    Args:
        force: Always re-download, ignoring the cached publish version.
        allow_download: When False, only an already-cached feed is returned.
    """
    meta = _load_meta_from_disk()

    # Only hit the network when the cache is old enough to be worth checking.
    if meta and not force and _checked_recently():
        return meta

    if not allow_download:
        return meta

    with _BUILD_LOCK:
        # Another thread may have rebuilt while we waited on the lock.
        meta = _load_meta_from_disk()
        if meta and not force and _checked_recently():
            return meta

        version = _fetch_published_version()

        if meta and not force:
            if version is None or version == meta.get("version"):
                # Feed is unchanged, or Metra is unreachable and the cache is
                # still the best data we have.
                if version is not None:
                    _mark_checked()
                return meta

        logger.info("Downloading Metra GTFS static schedule.")
        try:
            response = get_http_session().get(SCHEDULE_URL, timeout=60)
            response.raise_for_status()
        except Exception as error:
            if meta:
                logger.warning(f"Metra GTFS download failed, using cached feed: {error}")
                return meta
            raise RuntimeError(f"Unable to download the Metra GTFS schedule: {error}")

        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            rebuilt = _build_index(response.content, version)
        except Exception as error:
            if meta:
                logger.warning(f"Metra GTFS index build failed, using cached feed: {error}")
                return meta
            raise RuntimeError(f"Unable to parse the Metra GTFS schedule: {error}")

        _mark_checked()
        return rebuilt


def load_route_index(route_id):
    """Loads (and memoizes) the compiled trip index for a single route."""
    cached = _MEMO["routes"].get(route_id)
    if cached is not None:
        return cached

    path = _route_index_path(route_id)
    if not os.path.isfile(path):
        return None

    try:
        with open(path) as handle:
            index = json.load(handle)
    except (OSError, ValueError) as error:
        logger.warning(f"Metra route index for '{route_id}' is unreadable: {error}")
        return None

    routes = _MEMO["routes"]
    while len(routes) >= MAX_CACHED_ROUTES:
        routes.pop(next(iter(routes)))
    routes[route_id] = index
    return index


# --------------------------------------------------------------------------- #
# Schedule queries
# --------------------------------------------------------------------------- #

def active_service_ids(meta, service_date):
    """Returns the set of service ids running on a given calendar date."""
    key = service_date.strftime("%Y%m%d")
    weekday = service_date.weekday()

    active = set()
    for service_id, service in meta.get("services", {}).items():
        start = service.get("start", "")
        end = service.get("end", "")
        if start and key < start:
            continue
        if end and key > end:
            continue
        days = service.get("days") or []
        if weekday < len(days) and days[weekday]:
            active.add(service_id)

    for service_id, exception_type in meta.get("exceptions", {}).get(key, {}).items():
        if exception_type == "1":
            active.add(service_id)
        elif exception_type == "2":
            active.discard(service_id)

    return active


def _service_midnight(tz, service_date):
    """Midnight for a service date in the device timezone (DST safe)."""
    naive = datetime.combine(service_date, dtime.min)
    localize = getattr(tz, "localize", None)
    if localize:
        return localize(naive)
    return naive.replace(tzinfo=tz)


def _shift(tz, base_dt, seconds):
    shifted = base_dt + timedelta(seconds=seconds)
    normalize = getattr(tz, "normalize", None)
    return normalize(shifted) if normalize else shifted


def find_departures(meta, route_index, origin_id, destination_id, now, tz, limit=5):
    """Finds the next scheduled trips from origin to destination after `now`.

    Searches yesterday/today/tomorrow service days so that post-midnight trips
    (GTFS hours >= 24) and next-morning trips are both handled.
    """
    trips = (route_index or {}).get("trips", [])
    departures = []
    seen = set()

    for day_offset in (-1, 0, 1):
        service_date = (now + timedelta(days=day_offset)).date()
        services = active_service_ids(meta, service_date)
        if not services:
            continue
        midnight = _service_midnight(tz, service_date)

        for trip in trips:
            if trip.get("service") not in services:
                continue

            origin_stop = None
            destination_stop = None
            for stop in trip.get("stops", []):
                if origin_stop is None and stop[0] == origin_id:
                    origin_stop = stop
                elif origin_stop is not None and stop[0] == destination_id:
                    destination_stop = stop
                    break
            if origin_stop is None or destination_stop is None:
                continue

            depart_at = _shift(tz, midnight, origin_stop[2])
            if depart_at < now:
                continue

            key = (trip["id"], service_date.isoformat())
            if key in seen:
                continue
            seen.add(key)

            arrive_at = _shift(tz, midnight, destination_stop[1])
            departures.append({
                "trip_id": trip["id"],
                "service_date": service_date,
                "headsign": trip.get("headsign", ""),
                "train": train_number(trip["id"]),
                "origin_stop_id": origin_id,
                "destination_stop_id": destination_id,
                "scheduled_departure": depart_at,
                "scheduled_arrival": arrive_at,
                "departure": depart_at,
                "arrival": arrive_at,
                "delay_minutes": 0,
                "cancelled": False,
                "realtime": False,
            })

    departures.sort(key=lambda item: item["scheduled_departure"])
    return departures[:limit]


def train_number(trip_id):
    """Extracts the rider-facing train number from a Metra trip id.

    'UP-NW_UNW626_V3_D' -> '626'
    """
    for part in str(trip_id).split("_"):
        digits = re.sub(r"\D", "", part)
        if digits:
            return digits.lstrip("0") or digits
    return str(trip_id)


# --------------------------------------------------------------------------- #
# GTFS-Realtime (optional, requires an API key)
# --------------------------------------------------------------------------- #

def _load_realtime_feed(endpoint, api_key, timeout=15):
    """Fetches and parses a GTFS-Realtime protobuf feed."""
    try:
        from google.transit import gtfs_realtime_pb2
    except ImportError:
        logger.warning(
            "gtfs-realtime-bindings is not installed; showing scheduled times only. "
            "Install it with: pip install gtfs-realtime-bindings"
        )
        return None

    url = f"{REALTIME_BASE_URL}/{endpoint}"
    try:
        response = get_http_session().get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
        )
        response.raise_for_status()
    except Exception as error:
        logger.warning(f"Metra realtime feed '{endpoint}' unavailable: {error}")
        return None

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(response.content)
    except Exception as error:
        logger.warning(f"Unable to parse Metra realtime feed '{endpoint}': {error}")
        return None
    return feed


def apply_trip_updates(departures, api_key, tz):
    """Overlays realtime predictions onto scheduled departures, in place.

    Returns True if realtime data was successfully applied to at least one trip.
    """
    if not departures or not api_key:
        return False

    feed = _load_realtime_feed("tripupdates", api_key)
    if feed is None:
        return False

    wanted = {item["trip_id"] for item in departures}
    updates = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip_id = trip_update.trip.trip_id
        if trip_id in wanted:
            updates[trip_id] = trip_update

    if not updates:
        return False

    applied = False
    for departure in departures:
        trip_update = updates.get(departure["trip_id"])
        if trip_update is None:
            continue

        # schedule_relationship 3 == CANCELED
        if trip_update.trip.schedule_relationship == 3:
            departure["cancelled"] = True
            departure["realtime"] = True
            applied = True
            continue

        for stop_time_update in trip_update.stop_time_update:
            stop_id = stop_time_update.stop_id
            if stop_id not in (departure["origin_stop_id"], departure["destination_stop_id"]):
                continue

            # schedule_relationship 1 == SKIPPED
            if stop_time_update.schedule_relationship == 1:
                if stop_id == departure["origin_stop_id"]:
                    departure["cancelled"] = True
                    departure["realtime"] = True
                    applied = True
                continue

            is_origin = stop_id == departure["origin_stop_id"]
            event = stop_time_update.departure if is_origin else stop_time_update.arrival
            if not event.ListFields():
                event = stop_time_update.arrival if is_origin else stop_time_update.departure
            if not event.ListFields():
                continue

            scheduled = departure["scheduled_departure"] if is_origin else departure["scheduled_arrival"]
            if event.time:
                predicted = datetime.fromtimestamp(event.time, tz=tz)
            else:
                predicted = scheduled + timedelta(seconds=event.delay)

            if is_origin:
                departure["departure"] = predicted
                departure["delay_minutes"] = round(
                    (predicted - departure["scheduled_departure"]).total_seconds() / 60
                )
            else:
                departure["arrival"] = predicted

            departure["realtime"] = True
            applied = True

    return applied


def fetch_alerts(route_id, api_key, now, limit=2):
    """Returns active service alert headlines for a route."""
    if not api_key:
        return []

    feed = _load_realtime_feed("alerts", api_key)
    if feed is None:
        return []

    messages = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert

        entities = alert.informed_entity
        if entities and not any(informed.route_id == route_id for informed in entities):
            continue

        header = ""
        for translation in alert.header_text.translation:
            header = translation.text
            break
        header = " ".join(header.split())
        if header and header not in messages:
            messages.append(header)
        if len(messages) >= limit:
            break

    return messages
