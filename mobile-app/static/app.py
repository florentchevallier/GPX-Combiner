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

import io
import os
import tempfile
import time
import urllib.parse
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, request, jsonify, session, redirect, send_file, render_template

load_dotenv()  # reads mobile-app/.env if present — see .env.example

from models import init_db, upsert_user, get_user
from core.strava_client import (
    StravaClient,
    StravaAuthError,
    StravaAPIError,
    build_gpx_from_activity,
)
from core.gpx_core import combine_gpx_files

STRAVA_CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
STRAVA_CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")
OAUTH_REDIRECT_URI = f"{APP_BASE_URL}/oauth/callback"

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# Persistent session (30 days): without this, a Flask session expires when
# the browser closes — on mobile, that would force every friend to
# reconnect to Strava every time they open the app. session.permanent is
# set to True at login time (see oauth_callback).
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

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
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Authentication (Strava OAuth — one Strava account per friend)
# ---------------------------------------------------------------------------

@app.route("/login")
def login():
    """Redirects to Strava's authorization page. Each friend authorizes
    with their own account — a single Strava app (your Client ID) is
    shared by everyone, which Strava natively allows."""
    params = {
        "client_id": STRAVA_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "activity:read_all,activity:write",
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
    return redirect("/app/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/app/")


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


@app.errorhandler(StravaAPIError)
def handle_strava_api_error(e):
    return jsonify({"error": f"Strava error: {e}"}), 502


# ---------------------------------------------------------------------------
# GPX Combiner features
# ---------------------------------------------------------------------------

@app.route("/activities", methods=["GET"])
def list_activities():
    """Lists the connected user's most recent Strava activities. Each
    activity includes `strava_url`, so the frontend can offer a direct
    link for manual deletion on Strava."""
    athlete_id, user = _require_login()
    per_page = int(request.args.get("per_page", 25))
    page = int(request.args.get("page", 1))
    with _ClientSession(athlete_id, user) as client:
        activities = client.list_activities(page=page, per_page=per_page)

    for a in activities:
        a["strava_url"] = f"https://www.strava.com/activities/{a['id']}"
    return jsonify(activities)


@app.route("/combine", methods=["POST"])
def combine():
    """Combines either Strava activities or locally-imported GPX files —
    never both at once (the frontend only ever sends one or the other).

    Strava mode: receives a list of activities (the full objects as returned
    by /activities), downloads each one's streams, rebuilds a GPX per
    activity (build_gpx_from_activity), then combines them chronologically
    (combine_gpx_files) — exactly like the desktop app does.

    Local-files mode: the frontend has already read the GPX files from the
    phone's storage and sends their raw content directly — no Strava call
    needed, just combine_gpx_files.

    Expected JSON body (one of):
        {
          "activities": [ {...activity as returned by /activities...}, ... ],
          "include": {"hr": true, "cadence": true, "power": true, "temp": true}  // optional
        }
        {
          "local_files": [ {"name": "...", "content": "<gpx>...</gpx>"}, ... ],
          "include": {...}  // optional, same as above
        }
    """
    athlete_id, user = _require_login()
    body = request.get_json()
    include = body.get("include")  # None => combine_gpx_files keeps everything by default

    if "local_files" in body:
        local_files = body["local_files"]
        files = [{"path": f["name"], "content": f["content"]} for f in local_files]
        # No Strava data involved here, so no chronological re-sort: the
        # frontend already sorted these by the <time> tag it parsed from
        # each file before sending them.
    else:
        activities = body["activities"]
        # combine_gpx_files expects files already sorted chronologically
        # (oldest first) — same logic as the date sort on desktop.
        activities_sorted = sorted(activities, key=lambda a: a.get("start_date", ""))

        files = []
        with _ClientSession(athlete_id, user) as client:
            for activity in activities_sorted:
                streams = client.get_streams(activity["id"])
                gpx_text = build_gpx_from_activity(activity, streams)
                if gpx_text is None:
                    continue  # no GPS data — skipped, same as on desktop
                files.append({"path": f"{activity['id']}.gpx", "content": gpx_text})

    if len(files) < 2:
        return jsonify({
            "error": "Need at least two GPX files with GPS data to combine."
        }), 400

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
