"""
strava_client.py — shared Strava OAuth client + GPX-building logic, used by
both the desktop app (Tkinter, /desktop/) and the QGIS plugin (PyQt,
/qgis-plugin/). Pure Python (stdlib only), no GUI dependency.

This is a host-agnostic port of the StravaClient already working in
desktop/gpx_combiner.py. The one deliberate difference: this version does
NOT persist `config` to disk itself (the desktop app's original does, to a
hardcoded strava_config.json). Persistence is the caller's job — read a
config dict from wherever makes sense for that host (a JSON file for the
desktop app, QgsSettings for the QGIS plugin), pass it in, and after any
call that may have changed tokens, save `client.config` back out.
"""

import os
import re
import json
import time
import socket
import threading
import uuid
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
import ssl
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = None


def _urlopen(request, timeout):
    url = request.full_url if hasattr(request, "full_url") else str(request)
    if urllib.parse.urlparse(url).scheme != "https":
        raise StravaAPIError("Refusing to open non-HTTPS URL")
    return urllib.request.urlopen(  # nosec B310 - scheme validated above
        request, timeout=timeout, context=SSL_CONTEXT)


REDIRECT_PORT = 8721
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/exchange_token"


class StravaAuthError(Exception):
    pass


class StravaAPIError(Exception):
    pass


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_code = params.get("code", [None])[0]
        self.server.oauth_error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Strava</h2><p>You can close this window and return "
            b"to the application.</p></body></html>"
        )

    def log_message(self, fmt, *args):
        pass


class _DualStackHTTPServer(HTTPServer):
    """Listens on both IPv4 and IPv6 loopback, since browsers may resolve
    'localhost' to either depending on the OS."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class StravaClient:
    TOKEN_URL = "https://www.strava.com/oauth/token"  # nosec B105 - public endpoint, not a secret
    AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
    API_BASE = "https://www.strava.com/api/v3"

    def __init__(self, config):
        """config: a plain dict, mutated in place as tokens are obtained/
        refreshed. Persisting it is the caller's responsibility."""
        self.config = config

    @property
    def has_credentials(self):
        return bool(self.config.get("client_id") and self.config.get("client_secret"))

    @property
    def has_token(self):
        return bool(self.config.get("refresh_token"))

    # -- OAuth --
    def authorize_interactive(self):
        """Runs the full OAuth authorization-code flow, blocking until done."""
        params = {
            "client_id": self.config["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "activity:read_all,activity:write",
        }
        url = f"{self.AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

        try:
            httpd = _DualStackHTTPServer(("::", REDIRECT_PORT), _CallbackHandler)
        except OSError:
            try:
                httpd = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
            except OSError as e:
                raise StravaAuthError(f"port {REDIRECT_PORT} unavailable ({e})")

        httpd.oauth_code = None
        httpd.oauth_error = None
        httpd.timeout = 15

        webbrowser.open(url)

        deadline = time.time() + 120
        while httpd.oauth_code is None and httpd.oauth_error is None and time.time() < deadline:
            httpd.handle_request()
        httpd.server_close()

        if httpd.oauth_error:
            raise StravaAuthError(httpd.oauth_error)
        if not httpd.oauth_code:
            raise StravaAuthError("timeout / no response received")

        self._exchange_code(httpd.oauth_code)

    def _exchange_code(self, code):
        data = urllib.parse.urlencode({
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(self.TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with _urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAuthError(e.read().decode())
        self.config["refresh_token"] = payload["refresh_token"]
        self.config["access_token"] = payload["access_token"]
        self.config["expires_at"] = payload["expires_at"]

    def _ensure_fresh_token(self):
        if self.config.get("access_token") and self.config.get("expires_at", 0) > time.time() + 60:
            return
        data = urllib.parse.urlencode({
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": self.config["refresh_token"],
        }).encode()
        req = urllib.request.Request(self.TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with _urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAuthError(e.read().decode())
        self.config["refresh_token"] = payload["refresh_token"]
        self.config["access_token"] = payload["access_token"]
        self.config["expires_at"] = payload["expires_at"]

    # -- API calls --
    def _get(self, path, params=None):
        self._ensure_fresh_token()
        url = f"{self.API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self.config['access_token']}")
        try:
            with _urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAPIError(f"{e.code} {e.read().decode()}")
        except urllib.error.URLError as e:
            raise StravaAPIError(str(e))

    def get_athlete(self):
        return self._get("/athlete")

    def list_activities(self, page, per_page=10, before=None, after=None):
        params = {"page": page, "per_page": per_page}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        return self._get("/athlete/activities", params)

    def get_streams(self, activity_id):
        return self._get(
            f"/activities/{activity_id}/streams",
            {"keys": "latlng,altitude,time,heartrate,cadence,watts,temp", "key_by_type": "true"},
        )

    def get_gear(self, gear_id):
        return self._get(f"/gear/{gear_id}")

    # -- upload (requires the activity:write scope — see authorize_interactive) --
    def upload_gpx(self, filepath, name=None, description=None):
        """Uploads a GPX file as a new Strava activity. Returns the Strava
        upload id (NOT the final activity id — Strava processes uploads
        asynchronously; poll check_upload() with the returned id until it
        reports an activity_id or an error)."""
        self._ensure_fresh_token()
        boundary = uuid.uuid4().hex
        fields = {"data_type": "gpx"}
        if name:
            fields["name"] = name
        if description:
            fields["description"] = description

        with open(filepath, "rb") as f:
            file_bytes = f.read()

        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        filename = os.path.basename(filepath)
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f'Content-Type: application/gpx+xml\r\n\r\n').encode() + file_bytes + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(f"{self.API_BASE}/uploads", data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.config['access_token']}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with _urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAPIError(f"{e.code} {e.read().decode()}")
        return payload["id"]

    def check_upload(self, upload_id):
        """One status check — {'status': ..., 'activity_id': ... or None,
        'error': ... or None}. Caller is responsible for polling/pacing."""
        return self._get(f"/uploads/{upload_id}")


# ----------------------------------------------------------------------------
# Building a GPX file from a Strava activity + its streams
# ----------------------------------------------------------------------------

# Strava's own GPX exports use these lowercase values for <type>. Falls back
# to the lowercased raw Strava type for anything not in this table.
# Confirmed directly against real Strava-exported GPX files (2026-09-23):
# gravel_biking, ebikeride, mountain_biking, EMountainBikeRide (yes, mixed
# case — that inconsistency is Strava's own, not a typo here), trail_running.
STRAVA_TYPE_TO_GPX_TYPE = {
    "Ride": "cycling", "VirtualRide": "cycling", "Velomobile": "cycling", "Handcycle": "cycling",
    "MountainBikeRide": "mountain_biking",
    "GravelRide": "gravel_biking",
    "EBikeRide": "ebikeride",
    "EMountainBikeRide": "EMountainBikeRide",
    "Run": "running", "VirtualRun": "running",
    "TrailRun": "trail_running",
    "Walk": "walking", "Hike": "hiking",
    "Swim": "swimming",
    "AlpineSki": "skiing", "BackcountrySki": "skiing", "NordicSki": "skiing", "RollerSki": "skiing",
    "Snowboard": "snowboarding", "Snowshoe": "snowshoeing", "IceSkate": "ice skating",
    "InlineSkate": "inline skating", "Skateboard": "skateboarding",
    "RockClimbing": "rock climbing", "Rowing": "rowing", "Canoeing": "canoeing",
    "Kayaking": "kayaking", "StandUpPaddling": "stand up paddling", "Surfing": "surfing",
    "Kitesurf": "kitesurfing", "Windsurf": "windsurfing", "Sail": "sailing",
    "WeightTraining": "weight training", "Workout": "workout", "Crossfit": "crossfit",
    "Yoga": "yoga", "Elliptical": "elliptical", "StairStepper": "stair stepper",
    "Golf": "golf", "Soccer": "soccer", "Wheelchair": "wheelchair",
}


def strava_activity_gpx_type(activity):
    raw = activity.get("type") or ""
    return STRAVA_TYPE_TO_GPX_TYPE.get(raw, raw.lower()) if raw else ""


# For the upload dropdown: mirrors the order Strava's own type-picker uses
# (cycling/running/walking variants, then winter sports) for the entries we
# have equivalents for, then appends the rest of our table alphabetically.
# Strava's /uploads endpoint no longer accepts a separate activity_type
# form field (checked against the current official API reference) — Strava
# derives the type by reading the GPX file's own <type> tag, so choosing a
# type means rewriting that tag before upload, not adding a request
# parameter.
_TYPE_PRIORITY = [
    "cycling", "walking", "running", "trail_running", "gravel_biking", "mountain_biking",
    "swimming", "hiking", "ebikeride", "EMountainBikeRide", "skiing", "workout",
]
GPX_TYPE_CHOICES = _TYPE_PRIORITY + sorted(set(STRAVA_TYPE_TO_GPX_TYPE.values()) - set(_TYPE_PRIORITY))

TYPE_TAG_RE = re.compile(r"<type>(.*?)</type>", re.I | re.S)


def read_gpx_type(content):
    m = TYPE_TAG_RE.search(content)
    return m.group(1).strip() if m else ""


def set_gpx_type(content, new_type):
    """Replaces the <type> tag's content, or inserts one right after
    </name> if the file doesn't have one yet."""
    if TYPE_TAG_RE.search(content):
        return TYPE_TAG_RE.sub(f"<type>{escape_xml(new_type)}</type>", content, count=1)
    name_end = content.find("</name>")
    if name_end == -1:
        return content
    insert_pos = name_end + len("</name>")
    return content[:insert_pos] + f"\n    <type>{escape_xml(new_type)}</type>" + content[insert_pos:]


def escape_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


NAME_TAG_RE = re.compile(r"<name>(.*?)</name>", re.I | re.S)
GPX_PROJECT_URL = "https://github.com/florentchevallier/GPX-Combiner"


def set_gpx_name(content, new_name):
    """Replaces every <name> tag (metadata and/or track) with new_name —
    used after combining, so the file reflects the combined activity's
    name rather than whichever source file happened to sort first. Falls
    back to inserting one right after the opening <trk> tag if the file
    doesn't have one yet."""
    escaped = escape_xml(new_name)
    if NAME_TAG_RE.search(content):
        return NAME_TAG_RE.sub(lambda m: f"<name>{escaped}</name>", content)
    trk_match = re.search(r"<trk\b[^>]*>", content, re.I)
    if not trk_match:
        return content  # no <trk> to anchor on — leave the file untouched
    insert_pos = trk_match.end()
    return content[:insert_pos] + f"\n    <name>{escaped}</name>" + content[insert_pos:]


def set_gpx_creator(content, creator="GPX Combiner"):
    """Sets the <gpx creator="..."> attribute as the FIRST attribute right
    after the tag name — matching the layout real Strava exports use
    (<gpx creator="StravaGPX" version="1.1" ...>) — regardless of where an
    existing creator attribute sat, since a combined file no longer
    corresponds to any single recording device/app."""
    stripped = re.sub(r'\s+creator="[^"]*"', "", content, count=1, flags=re.I)
    return re.sub(r"<gpx\b", f'<gpx creator="{escape_xml(creator)}"', stripped, count=1)


def set_gpx_description(content, description):
    """Adds our project link and a description inside <metadata>, right
    before <time> if the file already has one (matching real Strava
    exports' layout) — or creates a <metadata> block (with no <time>,
    since none exists yet) right before <trk> if the file has no metadata
    at all."""
    signature = (
        f'<link href="{GPX_PROJECT_URL}">\n'
        '      <text>GPX Combiner</text>\n'
        '    </link>\n'
        f'    <desc>{escape_xml(description)}</desc>'
    )

    # Inserting right after the opening <metadata> tag naturally lands
    # before <time> when the file has one (real Strava/device exports
    # almost always do), and is still valid GPX when it doesn't.
    metadata_open = re.search(r"<metadata\b[^>]*>", content, re.I)
    if metadata_open:
        insert_pos = metadata_open.end()
        return content[:insert_pos] + f"\n    {signature}" + content[insert_pos:]

    trk_match = re.search(r"<trk\b[^>]*>", content, re.I)
    if not trk_match:
        return content  # no <trk> to anchor on — leave the file untouched
    block = f"<metadata>\n    {signature}\n  </metadata>\n  "
    return content[:trk_match.start()] + block + content[trk_match.start():]


def sanitize_filename(name):
    """Strips anything that isn't a letter, digit, space, hyphen,
    underscore, parenthesis or "+" (kept since it's the separator this app
    itself inserts between combined activity names) — drops emoji,
    quotes, #, commas, apostrophes, colons, etc., which otherwise make
    some OSes refuse to save the file. Runs of whitespace collapse to a
    single space."""
    cleaned = re.sub(r"[^\w\s\-()+]", "", name, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "activity"


def build_gpx_from_activity(activity, streams):
    """Build a single GPX (one <trk><trkseg>) from a Strava activity + its
    streams — matches the format Strava's own GPX exports use, including the
    <power>/<gpxtpx:...> extensions, so a combined file round-trips cleanly
    if re-uploaded to Strava. Returns None if the activity has no GPS data."""
    latlng = streams.get("latlng", {}).get("data")
    if not latlng:
        return None
    altitude = streams.get("altitude", {}).get("data") or [None] * len(latlng)
    time_offsets = streams.get("time", {}).get("data") or list(range(len(latlng)))
    heartrate = streams.get("heartrate", {}).get("data") or [None] * len(latlng)
    cadence = streams.get("cadence", {}).get("data") or [None] * len(latlng)
    watts = streams.get("watts", {}).get("data") or [None] * len(latlng)
    temp = streams.get("temp", {}).get("data") or [None] * len(latlng)

    start_str = activity.get("start_date")  # e.g. "2026-09-09T09:04:00Z"
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        start_dt = datetime.now(timezone.utc)

    gpx_type = strava_activity_gpx_type(activity)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="GPX Combiner" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        'http://www.topografix.com/GPX/1/1/gpx.xsd">',
        "  <trk>",
        f"    <name>{escape_xml(activity.get('name', 'Activity'))}</name>",
    ]
    if gpx_type:
        lines.append(f"    <type>{escape_xml(gpx_type)}</type>")
    lines.append("    <trkseg>")

    for (lat, lon), ele, t, hr, cad, w, tp in zip(latlng, altitude, time_offsets,
                                                    heartrate, cadence, watts, temp):
        pt_time = (start_dt + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ele_tag = f"<ele>{ele}</ele>" if ele is not None else ""

        tpx_parts = []
        if tp is not None:
            tpx_parts.append(f"<gpxtpx:atemp>{tp}</gpxtpx:atemp>")
        if hr is not None:
            tpx_parts.append(f"<gpxtpx:hr>{hr}</gpxtpx:hr>")
        if cad is not None:
            tpx_parts.append(f"<gpxtpx:cad>{cad}</gpxtpx:cad>")

        ext_parts = []
        if w is not None:
            ext_parts.append(f"<power>{w}</power>")
        if tpx_parts:
            ext_parts.append(f"<gpxtpx:TrackPointExtension>{''.join(tpx_parts)}</gpxtpx:TrackPointExtension>")
        ext_tag = f"<extensions>{''.join(ext_parts)}</extensions>" if ext_parts else ""

        lines.append(f'      <trkpt lat="{lat}" lon="{lon}">{ele_tag}<time>{pt_time}</time>{ext_tag}</trkpt>')

    lines.append("    </trkseg>")
    lines.append("  </trk>")
    lines.append("</gpx>")
    return "\n".join(lines)
