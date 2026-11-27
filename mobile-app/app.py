"""
GPX Combiner mobile — Flask backend.

Reuses core/strava_client.py and core/gpx_core.py as-is — no business logic
rewritten here, just an HTTP layer + multi-user handling (each friend has
their own Strava account/token, stored in SQLite via models.py).

Deleting originals: NOT automated. The Strava API exposes no
DELETE /activities/{id} endpoint (checked against the official docs,
https://developers.strava.com/docs/reference/ — only POST/GET/PUT exist for
/activities). Scripting the website's own "Delete" button would bypass its
CSRF protection, which we've explicitly ruled out. So the app just provides
a direct link to each activity on Strava
(https://www.strava.com/activities/{id}): the user deletes it themselves
with one tap, the same way they normally would.

Expected environment variables:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    FLASK_SECRET_KEY       (to sign session cookies)
    APP_BASE_URL           (e.g. https://gpx-combiner-mobile.onrender.com)
"""

import hmac
import io
import os
import secrets
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    send_file,
    send_from_directory,
    render_template,
)

load_dotenv()  # reads mobile-app/.env if present — see .env.example

from models import init_db, upsert_user, get_user, delete_user
from core.strava_client import (
    StravaClient,
    StravaAuthError,
    StravaAPIError,
    build_gpx_from_activity,
)
from core.gpx_core import (
    combine_gpx_files,
    prepare_single_gpx,
    extract_sort_key,
    parse_gpx_time,
    list_trkpts,
    keep_trkpt_ranges,
)

STRAVA_CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
STRAVA_CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")
OAUTH_REDIRECT_URI = f"{APP_BASE_URL}/oauth/callback"

# Not a real version number (mobile has none, by design — see README): just
# a discreet build counter in the footer, incremented by one on every file
# handed over, so it's obvious at a glance whether the phone is actually
# running the latest deploy or an older cached one.
BUILD_ID = "38"

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# Persistent session (30 days): without this, a Flask session expires when
# the browser closes — on mobile, that would force every friend to
# reconnect to Strava every time they open the app. session.permanent is
# set to True at login time (see oauth_callback).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Session cookie only ever sent over HTTPS when the app is served over HTTPS
# (Render), but still usable on http://localhost for local testing. Lax:
# not sent with requests made from other sites, but still sent when Strava
# redirects back to /oauth/callback (a top-level navigation).
app.config["SESSION_COOKIE_SECURE"] = APP_BASE_URL.startswith("https://")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Largest request accepted (local GPX files are sent as their full text to
# /combine, which works without a Strava account): keeps a single huge
# request from exhausting the server's memory. Generous for real tracks,
# which rarely exceed a few MB each.
MAX_REQUEST_MB = 30
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_MB * 1024 * 1024

init_db()


# ---------------------------------------------------------------------------
# Single page (the frontend handles everything else in JS, see static/app.js)
# ---------------------------------------------------------------------------
#
# The app itself is served under /app/ — deliberately NOT at the domain
# root — because that's also the installed PWA's manifest `scope`
# (static/manifest.json). Keeping /login and /oauth/callback outside that
# scope matters: see the comment on oauth_callback() below for why.

@app.route("/")
def root():
    return redirect("/app/")


@app.route("/app/")
def index():
    return render_template("index.html", build_id=BUILD_ID)


@app.route("/sw.js")
def service_worker():
    """Served from the domain root (not /static/sw.js) so its default scope
    is "/" — covering /app/. A service worker served from /static/ would
    default to a /static/-only scope and never control the app itself,
    which is enough to make Chrome consider the app not "installable" in
    the full sense (no controlling service worker), even though the
    manifest and icons are otherwise all correct."""
    return send_from_directory(app.static_folder, "sw.js")


# ---------------------------------------------------------------------------
# Authentication (Strava OAuth — one Strava account per friend)
# ---------------------------------------------------------------------------

@app.route("/login")
def login():
    """Redirects to Strava's authorization page. Each friend authorizes
    with their own account — a single Strava app (your Client ID) is
    shared by everyone, which Strava natively allows."""
    # A random `state`, kept in this browser's session and checked when
    # Strava sends the user back (see oauth_callback): proves the callback
    # answers a login started here, so nobody can slip their own Strava
    # account into someone else's browser via a crafted callback link.
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = {
        "client_id": STRAVA_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "activity:read_all,activity:write",
        "state": state,
    }
    return redirect(f"{StravaClient.AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")


def _oauth_error_page(message):
    return render_template("oauth_error.html", message=message), 400


@app.route("/oauth/callback")
def oauth_callback():
    """Strava redirects here with a `code` to exchange for tokens.

    This route (like /login) lives OUTSIDE the installed PWA's manifest
    scope ("/app/") on purpose. On Android, a URL that falls INSIDE an
    installed PWA's scope can get "link-captured": the OS hands that
    navigation to the standalone app window instead of letting the current
    browser tab load it. Since this URL carries a Strava authorization
    `code` that can only ever be exchanged once, that hand-off used to
    trigger a race — the browser tab AND the freshly-opened app both tried
    to exchange the same code, and whichever lost got Strava's
    "AuthorizationCode ... invalid" error (shown as a raw, unstyled page,
    which is what looked like a "desktop mode" bug). Keeping /login and
    /oauth/callback outside /app/ keeps the whole Strava round-trip inside
    a single browser tab; the installed app only opens at the very end, on
    the plain /app/ URL, by which point there's no code left to reuse.

    The session check below is an extra safety net for any other browser
    with similar link-capturing behaviour: if a session already exists when
    this runs, some other near-simultaneous request already completed the
    login successfully, so this one is a harmless duplicate rather than a
    real failure.
    """
    code = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        if session.get("athlete_id"):
            return redirect("/app/")
        return _oauth_error_page(
            "Authorization was cancelled or denied on Strava's side."
        )

    # Must match the state stored by /login in this browser, and is used
    # only once. A duplicate of a callback that already logged the user in
    # (see above) finds no state left: harmless, same as below.
    expected_state = session.pop("oauth_state", None)
    if not expected_state or not hmac.compare_digest(expected_state, request.args.get("state", "")):
        if session.get("athlete_id"):
            return redirect("/app/")
        return _oauth_error_page(
            "This connection link has expired or didn't start here — just try connecting again."
        )

    config = {"client_id": STRAVA_CLIENT_ID, "client_secret": STRAVA_CLIENT_SECRET}
    client = StravaClient(config)
    try:
        client._exchange_code(code)  # private method of the shared module, reused as-is
        athlete = client.get_athlete()
    except (StravaAuthError, StravaAPIError) as e:
        if session.get("athlete_id"):
            return redirect("/app/")
        return _oauth_error_page(
            "Strava didn't accept that authorization. This can happen if the "
            "connection page was opened twice — just try connecting again."
        )

    upsert_user(
        athlete_id=athlete["id"],
        firstname=athlete.get("firstname", ""),
        lastname=athlete.get("lastname", ""),
        access_token=config["access_token"],
        refresh_token=config["refresh_token"],
        expires_at=config["expires_at"],
    )
    session.permanent = True
    session["athlete_id"] = athlete["id"]
    # `just_logged_in=1` lets the frontend force a single reload shortly after
    # landing (see static/app.js). This is unrelated to the duplicate-code
    # race that /login and /oauth/callback being outside the PWA's scope
    # already fixes — it's a separate, purely cosmetic issue: the very first
    # paint of this page, reached through this exact redirect chain, can
    # render zoomed out/"desktop-looking" even though the viewport meta tag
    # is correct; one reload always fixes it.
    return redirect("/app/?just_logged_in=1")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/app/")


@app.route("/disconnect", methods=["POST"])
def disconnect():
    """"Disconnect from Strava": removes the app's access to the user's
    Strava account, erases their tokens from this server and ends the
    session. Unlike /logout (this phone only), this frees the user's place
    among the athletes Strava lets this app connect (10 for now). If Strava
    can't be reached, the local part is done anyway and the answer says so
    ("revoked": false), so the app can point to Strava's own settings.
    POST with a JSON body only, so another site can't trigger it."""
    if not request.is_json:
        return jsonify({"error": "Expected a JSON request."}), 400
    athlete_id, user = _require_login()
    client = StravaClient({
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "access_token": user["access_token"],
        "refresh_token": user["refresh_token"],
        "expires_at": user["expires_at"],
    })
    # deauthorize() is what actually removes the app from the athlete's
    # account (revoke() alone doesn't, see strava_client.py). If it fails,
    # still invalidate the token we hold, and tell the app so it can point
    # to Strava's own settings.
    revoked = True
    try:
        client.deauthorize()
    except (StravaAPIError, StravaAuthError):
        revoked = False
        try:
            client.revoke()
        except (StravaAPIError, StravaAuthError):
            pass
    delete_user(athlete_id)
    session.clear()
    return jsonify({"revoked": revoked})


@app.route("/me")
def me():
    """Returns the login state — used by the frontend to choose between the
    login screen and the main screen."""
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        return jsonify({"logged_in": False})
    user = get_user(athlete_id)
    if not user:
        session.clear()
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "firstname": user["firstname"],
        "lastname": user["lastname"],
    })


class _ClientSession:
    """Builds a StravaClient from the tokens stored for the connected user,
    and saves any refreshed tokens back on exit (StravaClient mutates its
    `config` in place on a refresh)."""

    def __init__(self, athlete_id, user_row):
        self.athlete_id = athlete_id
        self.user_row = user_row
        self.client = StravaClient({
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "access_token": user_row["access_token"],
            "refresh_token": user_row["refresh_token"],
            "expires_at": user_row["expires_at"],
        })

    def __enter__(self):
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Always persist (a refresh may have happened during the call)
        cfg = self.client.config
        upsert_user(
            athlete_id=self.athlete_id,
            firstname=self.user_row["firstname"],
            lastname=self.user_row["lastname"],
            access_token=cfg["access_token"],
            refresh_token=cfg["refresh_token"],
            expires_at=cfg["expires_at"],
        )
        return False  # don't swallow exceptions


def _require_login():
    athlete_id = session.get("athlete_id")
    if not athlete_id:
        raise PermissionError("Not logged in")
    user = get_user(athlete_id)
    if not user:
        raise PermissionError("Unknown user — please log in again")
    return athlete_id, user


@app.errorhandler(PermissionError)
def handle_permission_error(e):
    return jsonify({"error": str(e)}), 401


@app.errorhandler(413)
def handle_too_large(e):
    return jsonify({"error": f"Too much data at once (over {MAX_REQUEST_MB} MB). Try with fewer or smaller files."}), 413


@app.errorhandler(StravaAPIError)
def handle_strava_api_error(e):
    return jsonify({"error": f"Strava error: {e}"}), 502


# ---------------------------------------------------------------------------
# GPX Combiner features
# ---------------------------------------------------------------------------

@app.route("/activities", methods=["GET"])
def list_activities():
    """Lists the connected user's most recent Strava activities — or, with
    `after` and/or `before` (Unix timestamps, in seconds), those that
    started in that window (used to load one chosen day). Each activity
    includes `strava_url`, so the frontend can offer a direct link for
    manual deletion on Strava."""
    athlete_id, user = _require_login()
    per_page = int(request.args.get("per_page", 25))
    page = int(request.args.get("page", 1))
    after = request.args.get("after", type=int)
    before = request.args.get("before", type=int)
    with _ClientSession(athlete_id, user) as client:
        activities = client.list_activities(page=page, per_page=per_page, before=before, after=after)

    for a in activities:
        a["strava_url"] = f"https://www.strava.com/activities/{a['id']}"
    return jsonify(activities)


@app.route("/activities/<int:activity_id>/track")
def activity_track(activity_id):
    """The lat/lon points of one Strava activity, with each point's time as
    seconds from the start — used by the map preview and the trim screen on
    the selection screen, before anything is combined. Cheaper than reusing
    /combine's build_gpx_from_activity for this: no full GPX file needed.
    The times come with the same Strava response as the points, so they
    cost no extra request."""
    athlete_id, user = _require_login()
    with _ClientSession(athlete_id, user) as client:
        streams = client.get_streams(activity_id)
    points = streams.get("latlng", {}).get("data") or []
    times = streams.get("time", {}).get("data") or []
    return jsonify({"points": points, "times": times})


def _apply_trim(content, trim):
    """Keeps only the parts of one file chosen on the trim screen: trim is
    {"ranges": [[i, j], ...]}, each range a first and last track point to
    keep (both included), indexes as keep_trkpt_ranges() counts them — the
    phone counts them the same way. Several ranges keep several segments,
    e.g. around a pause. The last range is capped to the file's last point,
    as a safety net should the two counts ever differ by a few points at
    the end. Raises ValueError if the ranges don't fit."""
    if not trim:
        return content
    last = len(list_trkpts(content)) - 1
    ranges = [[int(a), int(b)] for a, b in trim.get("ranges") or []]
    if ranges and ranges[-1][1] > last:
        ranges[-1][1] = last
    return keep_trkpt_ranges(content, ranges)


@app.route("/combine", methods=["POST"])
def combine():
    """Combines Strava activities, locally-imported GPX files, or a mix of
    both. A single item is accepted too: it is returned on its own, readied
    for sending (prepare_single_gpx) instead of being combined.

    Strava activities: receives the full objects as returned by
    /activities, downloads each one's streams and rebuilds a GPX per
    activity (build_gpx_from_activity).

    Local files: the frontend has already read the GPX files from the
    phone's storage and sends their raw content directly — no Strava call
    needed.

    Everything is then put in chronological order and combined
    (combine_gpx_files) — exactly like the desktop app does.

    Each item can carry a "trim" ({"ranges": [[i, j], ...]}, see
    _apply_trim) set on the trim screen: only those parts of it are then
    used.

    Expected JSON body (either key, or both):
        {
          "activities": [ {...activity as returned by /activities..., "trim": {...} or null}, ... ],
          "local_files": [ {"name": "...", "content": "<gpx>...</gpx>", "trim": {...} or null}, ... ],
          "include": {"hr": true, "cadence": true, "power": true, "temp": true}  // optional
        }

    A request with local files only needs no Strava account at all (the
    "Continue without Strava" guest flow) — login is only required when
    there are Strava activities to fetch, the only part that actually calls
    the Strava API.
    """
    body = request.get_json()
    include = body.get("include")  # None => combine_gpx_files keeps everything by default

    try:
        files = [{"path": f["name"], "content": _apply_trim(f["content"], f.get("trim"))}
                 for f in body.get("local_files") or []]

        activities = body.get("activities") or []
        if activities:
            athlete_id, user = _require_login()
            with _ClientSession(athlete_id, user) as client:
                for activity in activities:
                    streams = client.get_streams(activity["id"])
                    gpx_text = build_gpx_from_activity(activity, streams)
                    if gpx_text is None:
                        continue  # no GPS data — skipped, same as on desktop
                    files.append({"path": f"{activity['id']}.gpx",
                                  "content": _apply_trim(gpx_text, activity.get("trim"))})
    except ValueError:
        return jsonify({"error": "A trimmed part no longer fits its track. Open the trim screen again and re-apply."}), 400

    # combine_gpx_files expects files already sorted chronologically (oldest
    # first). Sorted here on each file's own start time, read the same way
    # for both sources and compared as real dates (local files don't all
    # write their times in the same format). A file whose time can't be
    # read goes last, in the order received.
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    files.sort(key=lambda f: parse_gpx_time(extract_sort_key(f["content"], "")) or far_future)

    if not files:
        return jsonify({"error": "No GPS data found in the selection."}), 400

    if len(files) == 1:
        # A single item: nothing to combine, the file is just readied to be
        # sent on its own (e.g. a local GPX to upload, or a copy of an
        # existing activity).
        try:
            combined_text, skipped = prepare_single_gpx(files[0]["content"], include=include), []
        except ValueError:
            return jsonify({"error": "No GPS track found in this file."}), 400
    else:
        combined_text, skipped = combine_gpx_files(files, include=include)

    buffer = io.BytesIO(combined_text.encode("utf-8"))
    response = send_file(
        buffer,
        as_attachment=True,
        download_name="combined.gpx",
        mimetype="application/gpx+xml",
    )
    if skipped:
        # Files with no usable <trkseg>, silently dropped by
        # combine_gpx_files — let the frontend know anyway.
        response.headers["X-Skipped-Files"] = ",".join(skipped)
    return response


@app.route("/upload", methods=["POST"])
def upload():
    """Uploads a combined GPX to Strava, then waits (polls) for Strava to
    finish processing it before responding."""
    athlete_id, user = _require_login()
    file = request.files["file"]
    name = request.form.get("name", "Combined activity")

    with tempfile.NamedTemporaryFile(suffix=".gpx", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        with _ClientSession(athlete_id, user) as client:
            upload_id = client.upload_gpx(tmp_path, name=name, description="Created with GPX Combiner")
            deadline = time.time() + 60
            while time.time() < deadline:
                status = client.check_upload(upload_id)
                if status.get("activity_id"):
                    return jsonify(status)
                if status.get("error"):
                    return jsonify(status), 502
                time.sleep(2)
            return jsonify({
                "error": "Strava is taking longer than expected to process the upload."
            }), 504
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    app.run(debug=True)
