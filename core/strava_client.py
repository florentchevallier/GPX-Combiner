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

import re
import json
import time
import socket
import threading
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
    return urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)


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
    TOKEN_URL = "https://www.strava.com/oauth/token"
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
            "scope": "activity:read_all",
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


# ----------------------------------------------------------------------------
# Building a GPX file from a Strava activity + its streams
# ----------------------------------------------------------------------------

# Strava's own GPX exports use these lowercase values for <type>. Falls back
# to the lowercased raw Strava type for anything not in this table.
STRAVA_TYPE_TO_GPX_TYPE = {
    "Ride": "cycling", "VirtualRide": "cycling", "EBikeRide": "cycling",
    "MountainBikeRide": "cycling", "GravelRide": "cycling", "Velomobile": "cycling",
    "Handcycle": "cycling",
    "Run": "running", "VirtualRun": "running", "TrailRun": "running",
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


def escape_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "activity"


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
