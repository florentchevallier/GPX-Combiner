/*
 * GPX Combiner mobile — frontend.
 *
 * A single page, several "views" shown/hidden in JS. No framework: keeps
 * mobile traffic light and the code simple to maintain for a project of
 * this size (5-15 users).
 *
 * Deleting originals: never automated (see app.py) — just a direct link to
 * each activity on Strava, which the user deletes themselves with one tap.
 */

// Sports offered in the review-page dropdown. Deliberately short list
// (bike/walk/hike/run) — the rest of the ~40 possible types stays editable
// directly in the Strava app after upload.
const SPORT_CHOICES = [
  { value: "cycling", label: "Cycling" },
  { value: "gravel_biking", label: "Gravel biking" },
  { value: "mountain_biking", label: "Mountain biking" },
  { value: "ebikeride", label: "E-bike ride" },
  { value: "EMountainBikeRide", label: "E-mountain bike" },
  { value: "running", label: "Running" },
  { value: "trail_running", label: "Trail running" },
  { value: "walking", label: "Walking" },
  { value: "hiking", label: "Hiking" },
];

// Strava activity ("type" field) -> value in the dropdown above, to
// preselect the right sport. Subset of the equivalent table in
// core/strava_client.py (STRAVA_TYPE_TO_GPX_TYPE) — just the sports
// covered by SPORT_CHOICES.
const STRAVA_TYPE_TO_SPORT_CHOICE = {
  Ride: "cycling", VirtualRide: "cycling",
  MountainBikeRide: "mountain_biking",
  GravelRide: "gravel_biking",
  EBikeRide: "ebikeride",
  EMountainBikeRide: "EMountainBikeRide",
  Run: "running", VirtualRun: "running",
  TrailRun: "trail_running",
  Walk: "walking", Hike: "hiking",
};

const MAX_ACTIVITIES = 10;
const MAX_ACTIVITIES_PRO = 20; // Pro mode allows loading further back in history
const PAGE_SIZE = 5;

// ---------------------------------------------------------------------------
// Post-login reload (cosmetic-only fix)
// ---------------------------------------------------------------------------
// Landing here right after the Strava OAuth redirect chain, the very first
// paint can render zoomed out / "desktop-looking" even though the viewport
// meta tag is correct — a rendering-only quirk of that specific navigation
// path. One reload always fixes it. Guarded with sessionStorage so it only
// ever fires once per session, never in a loop.
(function forcePostLoginReload() {
  const params = new URLSearchParams(location.search);
  if (params.get("just_logged_in") !== "1") return;
  if (sessionStorage.getItem("gpxc_post_login_reload_done") === "1") return;
  sessionStorage.setItem("gpxc_post_login_reload_done", "1");
  setTimeout(() => {
    location.replace(location.pathname); // drop the query param, avoid a back-button loop
  }, 300);
})();

// ---------------------------------------------------------------------------
// "Install as an app" — capture the browser's install signal immediately
// ---------------------------------------------------------------------------
// Chrome can fire `beforeinstallprompt` before init()'s first network round
// trip (the /me call) resolves, so this listener has to be attached at
// script-load time, not from inside the async splash-screen setup below —
// otherwise the event fires with nobody listening and is lost for good.
let deferredInstallPrompt = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  wireInstallButton(); // in case the splash screen is already visible
});

// ---------------------------------------------------------------------------
// Strava's "recently deleted activities" recovery link
// ---------------------------------------------------------------------------
// That Strava page is desktop-only, and on Android a plain https:// link to
// strava.com gets claimed by the Strava app itself (which then says the
// page isn't available there). The `intent://` form below forces it open in
// Chrome instead — bypassing the Strava app — which at least gets the user
// to a real browser; they may still need to request the desktop site or
// open it on a computer, which the copy button covers either way. There's
// no equivalent trick on iOS (no way to reliably defeat Universal Links
// from a plain web page), so iOS keeps the normal https:// link.
const RECOVERY_URL = "https://www.strava.com/athlete/training/recently_deleted";

(function setupRecoveryLink() {
  const link = document.getElementById("recovery-link");
  if (link && /android/i.test(navigator.userAgent)) {
    link.href =
      "intent://www.strava.com/athlete/training/recently_deleted" +
      "#Intent;scheme=https;package=com.android.chrome;end";
  }

  const copyBtn = document.getElementById("copy-recovery-link");
  if (!copyBtn) return;
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(RECOVERY_URL);
    } catch (e) {
      // Clipboard API unavailable (older WebView, non-HTTPS context, etc.)
      const ta = document.createElement("textarea");
      ta.value = RECOVERY_URL;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    copyBtn.classList.add("copied");
    copyBtn.title = "Copied!";
    setTimeout(() => {
      copyBtn.classList.remove("copied");
      copyBtn.title = "Copy link";
    }, 1500);
  });
})();

function wireInstallButton() {
  if (!deferredInstallPrompt) return;
  const btn = document.getElementById("install-btn");
  if (!btn) return;
  btn.classList.remove("hidden");
  btn.onclick = async () => {
    btn.disabled = true;
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    btn.classList.add("hidden");
  };
}

const state = {
  activities: [],       // all activities loaded so far
  page: 1,
  combinedBlobText: null, // text content of the combined GPX, waiting to be uploaded
  mode: "strava",       // "strava" with a Strava session (the list can then also hold local files), "local" in guest mode
  guestMode: false,     // true once "Continue without Strava" is chosen — no
                         // Strava session exists at all, so uploading is
                         // never offered, only combining local files and
                         // downloading the result (see enterGuestHome below).
  lastUploadSucceeded: false, // set once doUpload succeeds; read by the
                         // "← Back to list" handler to start fresh instead
                         // of returning to a now-stale selection.
};

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

function showView(id) {
  document.querySelectorAll(".view").forEach(el => el.classList.add("hidden"));
  document.getElementById(id).classList.remove("hidden");
}

function formatDistance(meters) {
  return (meters / 1000).toFixed(1) + " km";
}

function formatDuration(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h${String(m).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// A time of day from a timestamp in ms (shown in the phone's own time
// zone), or "—" when the point has no time.
function formatClock(ms, withSeconds = true) {
  if (ms == null) return "—";
  const opts = { hour: "2-digit", minute: "2-digit" };
  if (withSeconds) opts.second = "2-digit";
  return new Date(ms).toLocaleTimeString("en-GB", opts);
}

function formatDate(isoLocal) {
  const d = new Date(isoLocal);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// ---------------------------------------------------------------------------
// Login / home screen
// ---------------------------------------------------------------------------

async function init() {
  const res = await fetch("/me");
  const me = await res.json();
  if (!me.logged_in) {
    showView("view-splash");
    setupInstallHint();
    return;
  }
  document.getElementById("user-label").textContent =
    `Logged in as ${me.firstname} ${me.lastname}`;
  showView("view-home");
  await loadActivities();
}

// ---------------------------------------------------------------------------
// "Continue without Strava" — local-files-only guest flow
// ---------------------------------------------------------------------------
// No Strava account, no login at all: just load local GPX files, combine,
// and download the result. /combine accepts local_files without a session
// (see app.py); /upload still requires one, so the upload button is simply
// never shown in this mode — there's nothing to upload to.

function enterGuestHome() {
  state.guestMode = true;
  state.mode = "local";
  state.activities = [];

  document.getElementById("user-label").textContent =
    "Local mode — no Strava account connected.";
  document.getElementById("activity-list-heading").textContent = "Files to combine";
  document.getElementById("day-btn").classList.add("hidden");
  document.getElementById("disconnect-btn").classList.add("hidden");
  document.getElementById("load-more-btn").classList.add("hidden");
  document.getElementById("activities-max-hint").classList.add("hidden");
  document.getElementById("logout-link").classList.add("hidden");
  document.getElementById("connect-strava-link").classList.remove("hidden");

  renderActivityList();
  updateCombineButtonState();
  closeHomeMap();
  showView("view-home");
}

document.getElementById("guest-btn").addEventListener("click", enterGuestHome);

// ---------------------------------------------------------------------------
// "Install as an app" hint (splash screen only, first-time visitors)
// ---------------------------------------------------------------------------

function isRunningStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true; // legacy iOS Safari flag
}

function setupInstallHint() {
  if (isRunningStandalone()) return; // already installed — nothing to suggest

  const hint = document.getElementById("install-hint");
  const instructions = document.getElementById("install-instructions");
  hint.classList.remove("hidden");

  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (isIOS) {
    // No install API on iOS Safari — only the manual Share-sheet route exists.
    instructions.textContent = 'Tap the Share icon, then "Add to Home Screen".';
    instructions.classList.remove("hidden");
    return;
  }

  // Android/Chrome: pick up an already-captured install prompt (see the
  // top of this file), or wait for one to arrive. Either way, also show the
  // manual fallback — Chrome's menu often bundles "Install" together with
  // "Create shortcut" under one entry, and only the former actually installs
  // the app (the shortcut option just opens the page in a browser tab).
  wireInstallButton();
  instructions.textContent =
    'Or open Chrome\'s menu (⋮) → "Install app". If it offers both ' +
    '"Install" and "Create shortcut", choose Install.';
  instructions.classList.remove("hidden");
}

// All items live in one list (state.activities), newest first, each tagged
// with the section it is shown in (activity._section): "recent" (the latest
// Strava activities), "day:YYYY-MM-DD" (a day picked with 📅) or "local"
// (GPX files from the phone). Ticked items can be combined across sections.
// Compared as real dates: Strava's and local files' times aren't always
// written in the same format.
function sortActivitiesNewestFirst() {
  const t = a => Date.parse(a.start_date) || 0;
  state.activities.sort((a, b) => t(b) - t(a));
}

// The activities cap (10, or 20 in Pro mode) counts the recent activities
// only — local files and days picked with 📅 don't use it up.
function stravaActivityCount() {
  return state.activities.filter(a => a._section === "recent").length;
}

async function loadActivities() {
  const res = await fetch(`/activities?page=${state.page}&per_page=${PAGE_SIZE}`);
  if (!res.ok) {
    showHomeError("Couldn't load your activities. Try again later.");
    return;
  }
  const newActivities = await res.json();
  // An activity already shown (in a day picked with 📅) stays where it is.
  const shown = new Set(state.activities.map(a => String(a.id)));
  for (const a of newActivities) {
    if (shown.has(String(a.id))) continue;
    a._section = "recent";
    state.activities.push(a);
  }
  sortActivitiesNewestFirst();
  renderActivityList();

  // Pro mode allows loading two extra pages (10 -> 20) — same idea as the
  // extra HR/cadence/power/temp fields it unlocks, for people who want to
  // dig further into their history.
  const proMode = document.getElementById("pro-mode-toggle").checked;
  const maxActivities = proMode ? MAX_ACTIVITIES_PRO : MAX_ACTIVITIES;
  const reachedMax = stravaActivityCount() >= maxActivities;
  const loadMoreBtn = document.getElementById("load-more-btn");
  const maxHint = document.getElementById("activities-max-hint");
  if (reachedMax || newActivities.length < PAGE_SIZE) {
    loadMoreBtn.classList.add("hidden");
    // Only worth pointing to the desktop app when there's a real reason to
    // think older activities exist beyond this cap (a full last page) —
    // not when Strava itself just ran out.
    maxHint.classList.toggle("hidden", !(reachedMax && newActivities.length === PAGE_SIZE));
  } else {
    loadMoreBtn.classList.remove("hidden");
    maxHint.classList.add("hidden");
  }
}

function activityItem(activity, isChecked) {
  const li = document.createElement("li");
  li.className = "activity-item";
  const metaLine = activity._isLocal
    ? `Local GPX file · ${formatDate(activity.start_date_local)}`
    : `${escapeHtml(activity.type)} · ${formatDate(activity.start_date_local)} · ` +
      `${formatDuration(activity.moving_time)} · ${formatDistance(activity.distance)}`;
  const id = String(activity.id);
  const t = activity._trim;
  const trimLine = t
    ? `<div class="activity-trim">✂️ Trimmed: ${formatClock(t.startTime, false)} → ${formatClock(t.endTime, false)}` +
      `${t.ranges.length > 1 ? ` · ${t.ranges.length} parts` : ""}</div>`
    : "";
  li.innerHTML = `
    <input type="checkbox" data-id="${escapeHtml(id)}" ${isChecked ? "checked" : ""}>
    <div class="activity-info">
      <div class="activity-name">${escapeHtml(activity.name)}</div>
      <div class="activity-meta">${metaLine}</div>
      ${trimLine}
    </div>
    <button type="button" class="trim-btn" data-trim-id="${escapeHtml(id)}"
            title="Trim" aria-label="Trim ${escapeHtml(activity.name)}">✂️</button>
  `;
  return li;
}

// "2026-11-12" -> "12 Nov 2026" (read as a calendar day, not a UTC instant,
// so it never shows the day before in a western time zone).
function formatDay(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// A titled list under the recent activities, with a ✕ to remove it.
function listSection(key, title) {
  const section = document.createElement("section");
  section.className = "list-section";
  section.dataset.section = key;
  section.innerHTML = `
    <div class="list-section-header">
      <h2>${escapeHtml(title)}</h2>
      <button type="button" class="section-remove" data-section="${escapeHtml(key)}"
              title="Remove from the list" aria-label="Remove ${escapeHtml(title)} from the list">✕</button>
    </div>
    <ul class="activity-list"></ul>
  `;
  return section;
}

function renderActivityList() {
  const home = document.getElementById("view-home");
  const recentList = document.getElementById("activity-list");
  const extra = document.getElementById("extra-sections");
  // The lists are redrawn each time (more activities loaded, files or a day
  // added), so remember what was ticked to tick it again.
  const boxes = Array.from(home.querySelectorAll(".activity-list input[type=checkbox]"));
  const known = new Set(boxes.map(cb => cb.dataset.id));
  const wasChecked = new Set(boxes.filter(cb => cb.checked).map(cb => cb.dataset.id));
  // Local files are checked by default when they first appear (the user
  // just picked them deliberately from their phone) — Strava activities
  // stay opt-in.
  const item = (a) => {
    const id = String(a.id);
    return activityItem(a, wasChecked.has(id) || (a._isLocal && !known.has(id)));
  };
  recentList.innerHTML = "";
  extra.innerHTML = "";

  if (state.guestMode) {
    // no Strava at all: the files are the whole list, no sections
    state.activities.forEach(a => recentList.appendChild(item(a)));
  } else {
    state.activities.filter(a => a._section === "recent").forEach(a => recentList.appendChild(item(a)));
    const days = [...new Set(state.activities.map(a => a._section).filter(s => s && s.startsWith("day:")))]
      .sort().reverse();
    for (const key of days) {
      const section = listSection(key, formatDay(key.slice(4)));
      const ul = section.querySelector("ul");
      state.activities.filter(a => a._section === key).forEach(a => ul.appendChild(item(a)));
      extra.appendChild(section);
    }
    const locals = state.activities.filter(a => a._section === "local");
    if (locals.length > 0) {
      const section = listSection("local", "Local GPX files");
      const ul = section.querySelector("ul");
      locals.forEach(a => ul.appendChild(item(a)));
      extra.appendChild(section);
    }
  }
  updateCombineButtonState();
}

function getSelectedActivities() {
  // Compared as strings: Strava activity ids are numbers, local-file ids
  // are strings like "local-3" — String(a.id) makes both sides match.
  const checked = new Set(
    Array.from(document.querySelectorAll("#view-home .activity-list input[type=checkbox]:checked"))
      .map(cb => cb.dataset.id)
  );
  return state.activities.filter(a => checked.has(String(a.id)));
}

// One selected item is allowed too: nothing to combine then, the item just
// goes on to the review screen by itself (a local GPX to upload, or a copy
// of an existing activity) — the button says so.
function combineButtonLabel(selected) {
  if (selected.length !== 1) return "Combine selected activities";
  return selected[0]._isLocal ? "Continue with selected file" : "Continue with selected activity";
}

function updateCombineButtonState() {
  const combineBtn = document.getElementById("combine-btn");
  const selected = getSelectedActivities();
  combineBtn.disabled = selected.length < 1;
  combineBtn.textContent = combineButtonLabel(selected);
}

function showHomeError(msg) {
  const el = document.getElementById("home-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Map preview (Leaflet + OSM tiles)
// ---------------------------------------------------------------------------
// Two independent toggles: one on the selection screen (one track per
// selected activity, distinct colors, before anything is combined) and one
// on the review screen (the single already-combined track). Each opens on
// first click and closes on the second; content is (re)computed fresh every
// time it opens rather than kept live in sync with the checkboxes, since
// that's simpler and matches how the rest of this app already works.
//
// No green anywhere in this palette — it disappears against OSM's parks and
// forests. (Note: #ffff33 is a pale yellow and can also wash out against
// some light tile colors — worth an eye on real tiles, but left as chosen.)
const TRACK_COLORS = ["#e41a1c", "#377eb8", "#984ea3", "#ff7f00", "#ffff33", "#f781bf"];

const mapInstances = {}; // "home" | "review" -> Leaflet map, created lazily

function ensureMap(containerId, key) {
  if (!mapInstances[key]) {
    mapInstances[key] = L.map(containerId);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(mapInstances[key]);
  }
  return mapInstances[key];
}

// Removes everything drawn on a map — tracks, dots, labels — but not the
// map tiles themselves.
function clearMapLayers(map) {
  map.eachLayer((layer) => {
    if (!(layer instanceof L.TileLayer)) map.removeLayer(layer);
  });
}

// Start = filled dot in the track's color; finish = white-filled dot with a
// colored ring — a simple way to tell the two ends apart without needing
// custom marker icons.
// points: [[lat, lon], ...], or a list of such parts for a track trimmed
// into several parts (drawn as one line with gaps).
function drawTrack(map, points, color) {
  if (!points || points.length === 0) return null;
  const parts = Array.isArray(points[0][0]) ? points : [points];
  const flat = parts.flat();
  if (flat.length < 2) return null;
  const line = L.polyline(parts, { color, weight: 4 }).addTo(map);
  L.circleMarker(flat[0], { radius: 6, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 }).addTo(map);
  L.circleMarker(flat[flat.length - 1], { radius: 6, color, weight: 2, fillColor: "#fff", fillOpacity: 1 }).addTo(map);
  return line;
}

function extractPointsFromGpxText(gpxText) {
  const doc = new DOMParser().parseFromString(gpxText, "application/xml");
  const points = [];
  doc.querySelectorAll("trkpt").forEach((pt) => {
    const lat = parseFloat(pt.getAttribute("lat"));
    const lon = parseFloat(pt.getAttribute("lon"));
    if (!Number.isNaN(lat) && !Number.isNaN(lon)) points.push([lat, lon]);
  });
  return points;
}

async function fetchStravaTrackPoints(activityId) {
  const res = await fetch(`/activities/${activityId}/track`);
  const data = await res.json().catch(() => ({}));
  return data.points || [];
}

function renderMapLegend(containerId, entries) {
  let legend = document.getElementById(`${containerId}-legend`);
  if (legend) legend.remove();
  if (entries.length === 0) return;
  legend = document.createElement("div");
  legend.id = `${containerId}-legend`;
  legend.className = "map-legend";
  for (const { name, color } of entries) {
    const item = document.createElement("div");
    item.className = "map-legend-item";
    const dot = document.createElement("span");
    dot.className = "map-legend-dot";
    dot.style.background = color;
    item.appendChild(dot);
    item.append(name);
    legend.appendChild(item);
  }
  document.getElementById(containerId).insertAdjacentElement("afterend", legend);
}

function closeHomeMap() {
  homeMapOpen = false;
  document.getElementById("home-map").classList.add("hidden");
  const legend = document.getElementById("home-map-legend");
  if (legend) legend.remove();
}

let homeMapOpen = false;
document.getElementById("map-toggle-home-btn").addEventListener("click", async () => {
  if (homeMapOpen) {
    closeHomeMap();
    return;
  }

  const selected = getSelectedActivities();
  if (selected.length === 0) {
    showHomeError("Select at least one activity to preview on the map.");
    return;
  }
  clearHomeError();

  homeMapOpen = true;
  const container = document.getElementById("home-map");
  container.classList.remove("hidden");
  const map = ensureMap("home-map", "home");
  requestAnimationFrame(() => map.invalidateSize());
  clearMapLayers(map);

  const lines = [];
  const legendEntries = [];
  for (let i = 0; i < selected.length; i++) {
    const activity = selected[i];
    const color = TRACK_COLORS[i % TRACK_COLORS.length];
    // a trimmed item: its kept parts, each drawn on its own
    const points = activity._trim && activity._track
      ? activity._trim.ranges.map(([i, j]) => activity._track.points.slice(i, j + 1).filter(Boolean))
        .filter(p => p.length > 0)
      : activity._isLocal
        ? extractPointsFromGpxText(activity._localContent)
        : await fetchStravaTrackPoints(activity.id);
    const line = drawTrack(map, points, color);
    if (line) {
      lines.push(line);
      legendEntries.push({ name: activity.name, color });
    }
  }
  renderMapLegend("home-map", legendEntries);
  if (lines.length > 0) {
    map.fitBounds(L.featureGroup(lines).getBounds().pad(0.1));
  }
});

let reviewMapOpen = false;
function closeReviewMap() {
  reviewMapOpen = false;
  document.getElementById("review-map").classList.add("hidden");
}

document.getElementById("map-toggle-review-btn").addEventListener("click", () => {
  if (reviewMapOpen) {
    closeReviewMap();
    return;
  }
  reviewMapOpen = true;
  const container = document.getElementById("review-map");
  container.classList.remove("hidden");
  const map = ensureMap("review-map", "review");
  requestAnimationFrame(() => map.invalidateSize());
  clearMapLayers(map);

  const points = extractPointsFromGpxText(state.combinedBlobText);
  const line = drawTrack(map, points, TRACK_COLORS[0]);
  if (line) map.fitBounds(line.getBounds().pad(0.1));
});

function clearHomeError() {
  document.getElementById("home-error").classList.add("hidden");
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Pro mode
// ---------------------------------------------------------------------------

document.getElementById("pro-mode-toggle").addEventListener("change", (e) => {
  document.getElementById("pro-mode-panel").classList.toggle("hidden", !e.target.checked);
  // Turning pro mode on raises the activities cap (10 -> 20) — if the
  // normal cap was already hit, "Load 5 more" needs to reappear so the two
  // extra pages are actually reachable.
  if (e.target.checked && state.mode === "strava" &&
      stravaActivityCount() >= MAX_ACTIVITIES && stravaActivityCount() < MAX_ACTIVITIES_PRO) {
    document.getElementById("load-more-btn").classList.remove("hidden");
    document.getElementById("activities-max-hint").classList.add("hidden");
  }
});

function getIncludeSettings() {
  const proMode = document.getElementById("pro-mode-toggle").checked;
  if (!proMode) return null; // null => combine_gpx_files keeps everything by default
  return {
    hr: document.getElementById("include-hr").checked,
    cadence: document.getElementById("include-cadence").checked,
    power: document.getElementById("include-power").checked,
    temp: document.getElementById("include-temp").checked,
  };
}

// ---------------------------------------------------------------------------
// Local GPX file import — with a Strava session, the files are added in a
// "Local GPX files" section of their own and can be combined with the
// Strava activities. In guest mode (no Strava at all) they are the whole
// list, replaced on each import.
// ---------------------------------------------------------------------------

// Reads the file name and start date out of a GPX file, the same way the
// desktop app locates a track's start time: <metadata><time> if present,
// else the first <trkpt><time>. Returns null if the file isn't valid XML.
function parseLocalGpxFile(filename, text) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (doc.querySelector("parsererror")) return null;

  const nameEl = doc.querySelector("metadata > name") || doc.querySelector("trk > name");
  const name = (nameEl && nameEl.textContent.trim()) || filename.replace(/\.gpx$/i, "");

  const timeEl = doc.querySelector("metadata > time") || doc.querySelector("trkpt > time");
  const startDate = timeEl ? timeEl.textContent.trim() : null;
  if (!startDate) return null; // no usable date to sort/name by

  // Many GPX files (Strava's own exports, and files this app previously
  // produced) carry a <type> tag using the same values as SPORT_CHOICES
  // (e.g. "running", "cycling") — read it so the sport dropdown can be
  // preselected, same as it is for Strava-sourced activities.
  const typeEl = doc.querySelector("trk > type");
  const gpxType = typeEl ? typeEl.textContent.trim() : null;

  return { name, startDate, gpxType };
}

let localFileCounter = 0; // gives each imported file its own id in the list

document.getElementById("load-local-btn").addEventListener("click", () => {
  document.getElementById("local-file-input").click();
});

document.getElementById("local-file-input").addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = ""; // allow re-selecting the same file(s) later
  if (files.length === 0) return;

  clearHomeError();
  // With a Strava session, a file already in the list isn't added twice.
  const alreadyThere = new Set(
    state.guestMode ? [] : state.activities.filter(a => a._isLocal).map(a => a._localKey));
  const parsed = [];
  for (const file of files) {
    const text = await file.text();
    const meta = parseLocalGpxFile(file.name, text);
    if (!meta) {
      showHomeError(`"${file.name}" doesn't look like a valid GPX file with a track date — skipped.`);
      continue;
    }
    const localKey = `${file.name}|${meta.startDate}`;
    if (alreadyThere.has(localKey)) continue;
    alreadyThere.add(localKey);
    localFileCounter += 1;
    parsed.push({
      id: `local-${localFileCounter}`,
      _localKey: localKey,
      name: meta.name,
      type: null,
      start_date: meta.startDate,
      start_date_local: meta.startDate,
      distance: null,
      moving_time: null,
      _isLocal: true,
      _section: "local",
      _localContent: text,
      _gpxType: meta.gpxType,
    });
  }
  if (parsed.length === 0) return;

  if (state.guestMode) {
    state.activities = parsed;
  } else {
    state.activities.push(...parsed);
  }
  sortActivitiesNewestFirst();
  renderActivityList();
  closeHomeMap(); // selection just changed — any preview open is now stale
});

// ---------------------------------------------------------------------------
// Trim screen — keep only part of one activity or file
// ---------------------------------------------------------------------------
// Each item of the list can be trimmed on its own, before combining. The
// pauses found in the track split it into segments, each with a box to
// tick to keep it (Before / After on each pause tick or untick them all in
// one go); the two handles, or the ‹ › buttons point by point, then fine-
// tune the start of the first kept segment and the end of the last one.
// What's kept is stored on the item (activity._trim: ranges of point
// indexes) and only applied by the server when combining or sending (see
// _apply_trim in app.py) — nothing is changed before that, and the
// original activity or file never is.

// A pause splits the track when the recording stopped for this long, or
// jumped this far between two points. Same thresholds as find_pauses() in
// core/gpx_core.py.
const SEGMENT_MIN_PAUSE_SECONDS = 120;
const PAUSE_MIN_JUMP_METERS = 200;
const SNAP_PIXELS = 12; // a dragged handle sticks to a segment edge this close

// "Color each segment": one color per segment, used in turn — saturated
// enough to stand out on the OSM map, with no green (lost among parks) and
// no yellow (lost among roads), ordered so that neighbors always contrast.
const SEGMENT_COLORS = ["#e63946", "#1d4ed8", "#f97316", "#9333ea", "#0891b2", "#db2777", "#334155", "#7c2d12"];
let colorSegments = false; // kept while the app stays open
const segmentColor = k => SEGMENT_COLORS[k % SEGMENT_COLORS.length];

const trim = {
  activity: null, // item being trimmed
  track: null,    // its points and times (see loadTrackForTrim)
  cumDist: null,  // distance from the first point to each point, in meters
  segments: [],   // [{start, end}] point indexes, split at the pauses
  pauses: [],     // pause k sits between segments k and k + 1
  checked: [],    // one box per segment
  start: 0,       // index of the first point kept (handle)
  end: 0,         // index of the last point kept (handle)
  layers: null,   // map layers, redrawn as things change
};

function haversineMeters(a, b) {
  const rad = x => (x * Math.PI) / 180;
  const dLat = rad(b[0] - a[0]);
  const dLon = rad(b[1] - a[1]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
}

// One entry per track point, in file order — the same order and count the
// server uses (list_trkpts / keep_trkpt_ranges), so indexes chosen here
// match there. A point without a readable position or time keeps its
// place (null).
function parseTrackFromGpxText(gpxText) {
  const doc = new DOMParser().parseFromString(gpxText, "application/xml");
  const points = [];
  const times = [];
  doc.querySelectorAll("trkpt").forEach((pt) => {
    const lat = parseFloat(pt.getAttribute("lat"));
    const lon = parseFloat(pt.getAttribute("lon"));
    points.push(Number.isNaN(lat) || Number.isNaN(lon) ? null : [lat, lon]);
    const timeEl = pt.querySelector("time");
    const ms = timeEl ? Date.parse(timeEl.textContent.trim()) : NaN;
    times.push(Number.isNaN(ms) ? null : ms);
  });
  return { points, times };
}

// {points: [[lat, lon] or null, ...], times: [ms or null, ...]} for one
// item, fetched once and kept on it. For a Strava activity the server
// sends each point's time as seconds from the start.
async function loadTrackForTrim(activity) {
  if (activity._track) return activity._track;
  let track;
  if (activity._isLocal) {
    track = parseTrackFromGpxText(activity._localContent);
  } else {
    const res = await fetch(`/activities/${activity.id}/track`);
    if (!res.ok) throw new Error("track not loaded");
    const data = await res.json();
    const points = data.points || [];
    const offsets = data.times || [];
    const startMs = Date.parse(activity.start_date);
    track = {
      points,
      times: points.map((_, i) =>
        offsets[i] != null && !Number.isNaN(startMs) ? startMs + offsets[i] * 1000 : null),
    };
  }
  activity._track = track;
  return track;
}

// Splits the track at its pauses: two consecutive points far apart in time
// or on the ground. Returns {segments, pauses}.
function splitTrackAtPauses(track) {
  const { points, times } = track;
  const segments = [];
  const pauses = [];
  let segStart = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const gap = times[i] != null && times[i + 1] != null ? (times[i + 1] - times[i]) / 1000 : null;
    const jump = points[i] && points[i + 1] ? haversineMeters(points[i], points[i + 1]) : null;
    if ((gap != null && gap >= SEGMENT_MIN_PAUSE_SECONDS) || (jump != null && jump >= PAUSE_MIN_JUMP_METERS)) {
      segments.push({ start: segStart, end: i });
      pauses.push({ index: i, gap, jump });
      segStart = i + 1;
    }
  }
  segments.push({ start: segStart, end: points.length - 1 });
  return { segments, pauses };
}

// The parts actually kept: each ticked segment, cut to the handles. Two
// ticked segments next to each other make one part (the pause between
// them is kept).
function keptRanges() {
  const ranges = [];
  trim.segments.forEach((seg, k) => {
    if (!trim.checked[k]) return;
    const a = Math.max(seg.start, trim.start);
    const b = Math.min(seg.end, trim.end);
    if (a > b) return;
    const prev = ranges[ranges.length - 1];
    if (prev && prev[1] + 1 === a) prev[1] = b;
    else ranges.push([a, b]);
  });
  return ranges;
}

function formatGap(seconds) {
  return seconds < 60 ? `${Math.round(seconds)} s` : `${Math.round(seconds / 60)} min`;
}

function formatPauseLine(p) {
  const parts = [];
  if (p.gap != null) parts.push(`${formatGap(p.gap)} pause`);
  if (p.jump != null && p.jump >= PAUSE_MIN_JUMP_METERS) {
    parts.push(p.jump < 1000 ? `${Math.round(p.jump)} m jump` : `${(p.jump / 1000).toFixed(1)} km jump`);
  }
  return parts.join(" · ") || "Pause";
}

function showTrimError(msg) {
  const el = document.getElementById("trim-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function setTrimControlsEnabled(enabled) {
  document.querySelectorAll("#view-trim .trim-nudge, #trim-apply-btn, #trim-reset-btn")
    .forEach((el) => { el.disabled = !enabled; });
}

async function openTrimScreen(activity) {
  clearHomeError();
  closeHomeMap();
  trim.activity = activity;
  trim.track = null;
  trim.layers = null;
  document.getElementById("trim-title").textContent = `Trim "${activity.name}"`;
  document.getElementById("trim-error").classList.add("hidden");
  document.getElementById("trim-times").textContent = "Loading the track…";
  document.getElementById("trim-stats").textContent = "";
  document.getElementById("trim-segments").innerHTML = "";
  document.getElementById("trim-segments-heading").classList.add("hidden");
  document.querySelectorAll(".trim-tick").forEach(el => el.remove());
  document.getElementById("trim-color-row").classList.add("hidden");
  document.getElementById("trim-color-toggle").checked = colorSegments;
  setTrimControlsEnabled(false);
  const hasMap = typeof L !== "undefined";
  if (hasMap) clearMapLayers(ensureMap("trim-map", "trim"));
  showView("view-trim");
  window.scrollTo(0, 0);

  let track;
  try {
    track = await loadTrackForTrim(activity);
  } catch (e) {
    document.getElementById("trim-times").textContent = "";
    showTrimError("Couldn't load this track. Try again later.");
    return;
  }
  if (trim.activity !== activity) return; // left the screen meanwhile
  const n = track.points.length;
  if (n < 2) {
    document.getElementById("trim-times").textContent = "";
    showTrimError("This track has too few points to trim.");
    return;
  }

  trim.track = track;
  trim.cumDist = [0];
  let lastPt = null;
  for (let i = 0; i < n; i++) {
    const pt = track.points[i];
    if (i > 0) trim.cumDist.push(trim.cumDist[i - 1] + (pt && lastPt ? haversineMeters(lastPt, pt) : 0));
    if (pt) lastPt = pt;
  }
  const { segments, pauses } = splitTrackAtPauses(track);
  trim.segments = segments;
  trim.pauses = pauses;
  const saved = activity._trim;
  if (saved && saved.checked && saved.checked.length === segments.length) {
    trim.checked = saved.checked.slice();
    trim.start = saved.start;
    trim.end = Math.min(saved.end, n - 1);
  } else {
    trim.checked = segments.map(() => true);
    trim.start = 0;
    trim.end = n - 1;
  }

  for (const id of ["trim-start", "trim-end"]) {
    document.getElementById(id).max = String(n - 1);
  }
  renderTrimTicks();
  setTrimControlsEnabled(true);

  if (hasMap) {
    const map = ensureMap("trim-map", "trim");
    const all = track.points.filter(Boolean);
    const color = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#fc4c02";
    trim.layers = {
      // the part left out: grey and dashed, under the part kept
      removed: L.polyline(all, { color: "#888", weight: 3, opacity: 0.8, dashArray: "4 6" }).addTo(map),
      kept: L.polyline([], { color, weight: 5 }).addTo(map),
      colored: L.layerGroup().addTo(map), // used instead of "kept" with colors on
      startDot: L.circleMarker(all[0], { radius: 7, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 }).addTo(map),
      endDot: L.circleMarker(all[all.length - 1], { radius: 7, color, weight: 2, fillColor: "#fff", fillOpacity: 1 }).addTo(map),
      labels: [],
    };
    if (segments.length > 1) {
      segments.forEach((seg, k) => {
        const first = track.points.slice(seg.start, seg.end + 1).find(Boolean);
        if (!first) return;
        trim.layers.labels.push(L.marker(first, {
          icon: L.divIcon({ className: "trim-seg-label", html: String(k + 1), iconSize: [20, 20] }),
          interactive: false,
        }).addTo(map));
      });
    }
    requestAnimationFrame(() => {
      map.invalidateSize();
      map.fitBounds(trim.layers.removed.getBounds().pad(0.1));
    });
  }

  renderTrimSegments();
  updateTrimView();
}

// Small marks on the slider where the pauses are — where a dragged handle
// sticks.
function renderTrimTicks() {
  const slider = document.querySelector(".trim-slider");
  slider.querySelectorAll(".trim-tick").forEach(el => el.remove());
  const n = trim.track.points.length;
  for (const p of trim.pauses) {
    const tick = document.createElement("div");
    tick.className = "trim-tick";
    tick.style.left = `calc(13px + (100% - 26px) * ${(p.index + 0.5) / (n - 1)})`;
    slider.appendChild(tick);
  }
}

function renderTrimSegments() {
  const list = document.getElementById("trim-segments");
  const heading = document.getElementById("trim-segments-heading");
  const { track, segments, pauses } = trim;
  list.innerHTML = "";
  heading.classList.remove("hidden");
  if (segments.length === 1) {
    heading.textContent = `No pause of ${formatGap(SEGMENT_MIN_PAUSE_SECONDS)} or more in this track`;
    return;
  }
  heading.textContent = `Segments (split at pauses of ${formatGap(SEGMENT_MIN_PAUSE_SECONDS)} or more)`;
  document.getElementById("trim-color-row").classList.remove("hidden");
  segments.forEach((seg, k) => {
    const li = document.createElement("li");
    li.className = "trim-seg";
    const dist = formatDistance(trim.cumDist[seg.end] - trim.cumDist[seg.start]);
    li.innerHTML = `
      <label>
        <input type="checkbox" data-seg="${k}" ${trim.checked[k] ? "checked" : ""}>
        <span class="trim-seg-num">${k + 1}</span>
        <span>${formatClock(track.times[seg.start], false)} → ${formatClock(track.times[seg.end], false)} · ${dist}</span>
      </label>
    `;
    list.appendChild(li);
    if (k < pauses.length) {
      const pli = document.createElement("li");
      pli.className = "trim-pause";
      pli.innerHTML = `
        <span class="trim-pause-info">${escapeHtml(formatPauseLine(pauses[k]))}</span>
        <button type="button" class="btn btn-small" data-keep="before" data-pause="${k}">Before</button>
        <button type="button" class="btn btn-small" data-keep="after" data-pause="${k}">After</button>
      `;
      list.appendChild(pli);
    }
  });
}

// After ticking or unticking segments, the handles go back to the edges of
// the first and last ticked segments.
function fitHandlesToCheckedSegments() {
  const ks = trim.checked.map((c, k) => (c ? k : -1)).filter(k => k >= 0);
  if (ks.length === 0) return;
  trim.start = trim.segments[ks[0]].start;
  trim.end = trim.segments[ks[ks.length - 1]].end;
  if (trim.end - trim.start < 1) {
    // a single one-point segment: widen to a valid range for the handles
    if (trim.end < trim.track.points.length - 1) trim.end += 1;
    else trim.start -= 1;
  }
}

function updateTrimView() {
  const { track, start, end } = trim;
  if (!track) return;
  const n = track.points.length;
  const startInput = document.getElementById("trim-start");
  const endInput = document.getElementById("trim-end");
  startInput.value = String(start);
  endInput.value = String(end);
  // When the two handles meet, the one that can still move goes on top.
  startInput.style.zIndex = start > (n - 1) / 2 ? "3" : "2";
  endInput.style.zIndex = start > (n - 1) / 2 ? "2" : "3";

  const fill = document.getElementById("trim-slider-fill");
  const a = start / (n - 1);
  const b = end / (n - 1);
  fill.style.left = `calc(13px + (100% - 26px) * ${a})`;
  fill.style.width = `calc((100% - 26px) * ${b - a})`;

  document.querySelectorAll("#trim-segments input[data-seg]").forEach((cb) => {
    cb.checked = trim.checked[Number(cb.dataset.seg)];
  });

  const ranges = keptRanges();
  const kept = ranges.reduce((s, [i, j]) => s + j - i + 1, 0);
  const applyBtn = document.getElementById("trim-apply-btn");
  applyBtn.disabled = kept < 2;
  if (kept < 2) {
    document.getElementById("trim-times").textContent = "Nothing kept";
    document.getElementById("trim-stats").textContent = "Tick at least one segment.";
  } else {
    const first = ranges[0][0];
    const last = ranges[ranges.length - 1][1];
    document.getElementById("trim-times").textContent =
      `${formatClock(track.times[first])} → ${formatClock(track.times[last])}`;
    let dist = 0;
    let ms = 0;
    let timed = true;
    for (const [i, j] of ranges) {
      dist += trim.cumDist[j] - trim.cumDist[i];
      if (track.times[i] != null && track.times[j] != null) ms += track.times[j] - track.times[i];
      else timed = false;
    }
    const whole = ranges.length === 1 && first === 0 && last === n - 1;
    const stats = [formatDistance(dist)];
    if (timed) stats.push(formatDuration(ms / 1000));
    if (ranges.length > 1) stats.push(`${ranges.length} parts`);
    document.getElementById("trim-stats").textContent =
      stats.join(" · ") + (whole ? " · whole track" : " kept");
  }

  if (trim.layers) {
    const parts = ranges.map(([i, j]) => track.points.slice(i, j + 1).filter(Boolean)).filter(p => p.length > 0);
    trim.layers.kept.setLatLngs(colorSegments ? [] : parts);
    trim.layers.colored.clearLayers();
    if (colorSegments) {
      // each kept segment (cut to the handles) in its own color
      trim.segments.forEach((seg, k) => {
        if (!trim.checked[k]) return;
        const a = Math.max(seg.start, start);
        const b = Math.min(seg.end, end);
        const pts = a <= b ? track.points.slice(a, b + 1).filter(Boolean) : [];
        if (pts.length > 0) L.polyline(pts, { color: segmentColor(k), weight: 5 }).addTo(trim.layers.colored);
      });
    }
    if (parts.length > 0) {
      trim.layers.startDot.setLatLng(parts[0][0]);
      const lastPart = parts[parts.length - 1];
      trim.layers.endDot.setLatLng(lastPart[lastPart.length - 1]);
    }
    trim.layers.labels.forEach((m, k) => {
      const el = m.getElement();
      if (!el) return;
      el.classList.toggle("trim-seg-label-off", !trim.checked[k]);
      el.style.background = trim.checked[k] && colorSegments ? segmentColor(k) : "";
    });
  }
  document.querySelectorAll("#trim-segments .trim-seg-num").forEach((el, k) => {
    el.style.background = colorSegments ? segmentColor(k) : "";
  });
}

// A dragged handle sticks to the nearest segment edge when it comes within
// a few pixels of it: a start handle to where a segment starts, an end
// handle to where one ends.
function snapToSegmentEdge(value, edges) {
  const n = trim.track.points.length;
  const width = document.querySelector(".trim-slider").clientWidth - 26;
  const pxPerPoint = width / (n - 1);
  let best = value;
  let bestPx = SNAP_PIXELS + 1;
  for (const edge of edges) {
    const px = Math.abs(edge - value) * pxPerPoint;
    if (px < bestPx) { best = edge; bestPx = px; }
  }
  return bestPx <= SNAP_PIXELS ? best : value;
}

document.getElementById("trim-start").addEventListener("input", (e) => {
  if (!trim.track) return;
  const v = snapToSegmentEdge(Number(e.target.value), trim.segments.map(s => s.start));
  trim.start = Math.min(v, trim.end - 1);
  updateTrimView();
});

document.getElementById("trim-end").addEventListener("input", (e) => {
  if (!trim.track) return;
  const v = snapToSegmentEdge(Number(e.target.value), trim.segments.map(s => s.end));
  trim.end = Math.max(v, trim.start + 1);
  updateTrimView();
});

// ‹ › buttons: one point per tap — no sticking, so a handle can always move
// off a segment edge. Held down, they repeat, faster and faster.
function nudgeHandle(which, delta) {
  if (!trim.track) return;
  const last = trim.track.points.length - 1;
  if (which === "start") {
    trim.start = Math.max(0, Math.min(trim.start + delta, trim.end - 1));
  } else {
    trim.end = Math.min(last, Math.max(trim.end + delta, trim.start + 1));
  }
  updateTrimView();
}

document.querySelectorAll(".trim-nudge").forEach((btn) => {
  const which = btn.dataset.handle;
  const dir = Number(btn.dataset.dir);
  let timer = null;
  let repeats = 0;
  const stop = () => { clearTimeout(timer); timer = null; };
  const tick = () => {
    repeats += 1;
    nudgeHandle(which, dir * (repeats < 15 ? 1 : repeats < 40 ? 5 : 20));
    timer = setTimeout(tick, 70);
  };
  btn.addEventListener("pointerdown", (e) => {
    if (btn.disabled) return;
    e.preventDefault();
    stop();
    repeats = 0;
    nudgeHandle(which, dir);
    timer = setTimeout(tick, 400);
  });
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) btn.addEventListener(ev, stop);
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
  // Keyboard (Enter / Space): one step per press.
  btn.addEventListener("click", (e) => { if (e.detail === 0) nudgeHandle(which, dir); });
});

document.getElementById("trim-segments").addEventListener("change", (e) => {
  const cb = e.target.closest("input[data-seg]");
  if (!cb || !trim.track) return;
  trim.checked[Number(cb.dataset.seg)] = cb.checked;
  fitHandlesToCheckedSegments();
  updateTrimView();
});

document.getElementById("trim-segments").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-keep]");
  if (!btn || !trim.track) return;
  const k = Number(btn.dataset.pause);
  const before = btn.dataset.keep === "before";
  trim.checked = trim.segments.map((_, i) => (before ? i <= k : i > k));
  fitHandlesToCheckedSegments();
  updateTrimView();
});

document.getElementById("trim-color-toggle").addEventListener("change", (e) => {
  colorSegments = e.target.checked;
  updateTrimView();
});

document.getElementById("trim-reset-btn").addEventListener("click", () => {
  if (!trim.track) return;
  trim.checked = trim.segments.map(() => true);
  trim.start = 0;
  trim.end = trim.track.points.length - 1;
  updateTrimView();
});

function leaveTrimScreen() {
  trim.activity = null;
  trim.track = null;
  trim.layers = null;
  showView("view-home");
}

document.getElementById("trim-cancel-btn").addEventListener("click", leaveTrimScreen);

document.getElementById("trim-apply-btn").addEventListener("click", () => {
  const { activity, track } = trim;
  if (!activity || !track) return;
  const ranges = keptRanges();
  const n = track.points.length;
  const whole = ranges.length === 1 && ranges[0][0] === 0 && ranges[0][1] === n - 1;
  activity._trim = whole ? null : {
    ranges,
    // to reopen the screen as it was left
    checked: trim.checked.slice(),
    start: trim.start,
    end: trim.end,
    // for the line shown in the list
    startTime: track.times[ranges[0][0]],
    endTime: track.times[ranges[ranges.length - 1][1]],
  };
  leaveTrimScreen();
  renderActivityList();
  // Trimming an item means it's going to be used: tick it.
  const box = document.querySelector(`#view-home .activity-list input[data-id="${CSS.escape(String(activity.id))}"]`);
  if (box) box.checked = true;
  updateCombineButtonState();
});

document.getElementById("view-home").addEventListener("click", (e) => {
  const btn = e.target.closest(".activity-list .trim-btn");
  if (!btn) return;
  const activity = state.activities.find(a => String(a.id) === btn.dataset.trimId);
  if (activity) openTrimScreen(activity);
});

// ---------------------------------------------------------------------------
// "Disconnect from Strava" — unlike "Log out", frees the user's place among
// the athletes Strava lets this app connect (see /disconnect in app.py)
// ---------------------------------------------------------------------------

document.getElementById("disconnect-btn").addEventListener("click", async () => {
  const ok = confirm(
    "This removes GPX Combiner's access to your Strava account and erases it " +
    "from the app. You can reconnect anytime.\n\nDisconnect from Strava?");
  if (!ok) return;
  const btn = document.getElementById("disconnect-btn");
  btn.disabled = true;
  try {
    const res = await fetch("/disconnect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.revoked === false) {
      alert("You're disconnected from the app, but Strava couldn't be reached. " +
            "To remove the access on Strava's side, go to Settings → My Apps " +
            "on the Strava website.");
    } else if (!res.ok) {
      alert("Couldn't disconnect. Please try again later.");
      btn.disabled = false;
      return;
    }
  } catch (e) {
    alert("Couldn't disconnect. Please try again later.");
    btn.disabled = false;
    return;
  }
  location.href = "/app/"; // back to the login screen
});

// ---------------------------------------------------------------------------
// One day's activities — 📅 next to Pro mode
// ---------------------------------------------------------------------------
// Picks a day in the phone's own calendar and adds that day's Strava
// activities in a section of their own — to reach an activity older than
// the recent ones, and combine it with anything else in the list.

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function showDayNotice(msg) {
  const el = document.getElementById("day-notice");
  el.textContent = msg;
  el.classList.toggle("hidden", !msg);
}

document.getElementById("day-btn").addEventListener("click", () => {
  const input = document.getElementById("day-input");
  input.max = todayIso(); // no future days
  showDayNotice("");
  try {
    input.showPicker();
  } catch (e) {
    // older browsers: focusing the field opens their own picker
    input.focus();
    input.click();
  }
});

document.getElementById("day-input").addEventListener("change", (e) => {
  const day = e.target.value; // "YYYY-MM-DD", empty if cleared
  e.target.value = "";        // so picking the same day again still fires
  if (day) loadDay(day);
});

async function loadDay(day) {
  const key = `day:${day}`;
  const existing = document.querySelector(`#extra-sections [data-section="${key}"]`);
  if (existing) {
    existing.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  // Strava filters on the moment an activity started, but "that day"
  // means the day where it happened, in its own time zone — the same day
  // Strava shows. So ask for a wider window (time zones run from -12 h to
  // +14 h) and keep the activities whose local date is the day picked.
  const [y, m, d] = day.split("-").map(Number);
  const midnight = Date.UTC(y, m - 1, d) / 1000;
  const after = midnight - 14 * 3600;
  const before = midnight + 24 * 3600 + 12 * 3600;

  const btn = document.getElementById("day-btn");
  btn.disabled = true;
  showDayNotice(`Loading the activities of ${formatDay(day)}…`);
  try {
    const res = await fetch(`/activities?after=${after}&before=${before}&per_page=50`);
    if (!res.ok) throw new Error("not loaded");
    const all = await res.json();
    const ofThatDay = all.filter(a => (a.start_date_local || "").slice(0, 10) === day);
    const shown = new Set(state.activities.map(a => String(a.id)));
    const added = ofThatDay.filter(a => !shown.has(String(a.id)));
    if (ofThatDay.length === 0) {
      showDayNotice(`No Strava activity on ${formatDay(day)}.`);
      return;
    }
    if (added.length === 0) {
      showDayNotice(`The activities of ${formatDay(day)} are already in the list.`);
      return;
    }
    added.forEach((a) => { a._section = key; });
    state.activities.push(...added);
    sortActivitiesNewestFirst();
    renderActivityList();
    closeHomeMap();
    showDayNotice("");
    document.querySelector(`#extra-sections [data-section="${key}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showDayNotice("Couldn't load the activities of that day. Try again later.");
  } finally {
    btn.disabled = false;
  }
}

// ✕ on a section: takes its items out of the list (with their trims).
document.getElementById("extra-sections").addEventListener("click", (e) => {
  const btn = e.target.closest(".section-remove");
  if (!btn) return;
  const key = btn.dataset.section;
  state.activities = state.activities.filter(a => a._section !== key);
  renderActivityList();
  closeHomeMap();
});

// ---------------------------------------------------------------------------
// Combine
// ---------------------------------------------------------------------------

document.getElementById("load-more-btn").addEventListener("click", async () => {
  state.page += 1;
  await loadActivities();
});

document.getElementById("view-home").addEventListener("change", (e) => {
  if (e.target.closest(".activity-list")) updateCombineButtonState();
});

document.getElementById("combine-btn").addEventListener("click", async () => {
  clearHomeError();
  const selected = getSelectedActivities();
  if (selected.length < 1) return;

  const combineBtn = document.getElementById("combine-btn");
  combineBtn.disabled = true;
  combineBtn.textContent = selected.length === 1 ? "Preparing…" : "Combining…";

  try {
    // Strava activities and local files can be mixed: each kind goes in
    // its own list, the server puts everything in chronological order.
    // A trimmed item carries the part to keep (see the trim screen); the
    // track data cached for that screen stays on the phone.
    const trimOf = a => (a._trim ? { ranges: a._trim.ranges } : null);
    const body = {
      activities: selected.filter(a => !a._isLocal).map((a) => {
        const { _track, _trim, ...activity } = a;
        return { ...activity, trim: trimOf(a) };
      }),
      local_files: selected.filter(a => a._isLocal)
        .map(a => ({ name: a.name, content: a._localContent, trim: trimOf(a) })),
      include: getIncludeSettings(),
    };

    const res = await fetch("/combine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showHomeError(err.error || (selected.length === 1 ? "Preparing the file failed." : "Combining failed."));
      return;
    }

    state.combinedBlobText = await res.text();
    openReviewScreen(selected);
  } finally {
    updateCombineButtonState();
  }
});

// ---------------------------------------------------------------------------
// Review before upload
// ---------------------------------------------------------------------------

function openReviewScreen(selectedActivities) {
  closeHomeMap();
  closeReviewMap(); // fresh combine result — any previous preview is stale

  // Default name: names concatenated in chronological order, joined by
  // " + " — same convention as the desktop app.
  const startTime = a => Date.parse(a.start_date) || 0;
  const sorted = [...selectedActivities].sort((a, b) => startTime(a) - startTime(b));
  document.getElementById("activity-name").value = sorted.map(a => a.name).join(" + ");

  // Manual-deletion links to each source activity — only for the sources
  // that are actual Strava activities. Nothing to delete for a local GPX
  // file, so the whole block is hidden when there are only those.
  const hint = document.getElementById("delete-originals-hint");
  const linksList = document.getElementById("source-activity-links");
  linksList.innerHTML = "";
  const stravaSources = sorted.filter(a => !a._isLocal);
  if (stravaSources.length === 0) {
    hint.classList.add("hidden");
    linksList.classList.add("hidden");
  } else {
    hint.classList.remove("hidden");
    linksList.classList.remove("hidden");
    for (const activity of stravaSources) {
      const li = document.createElement("li");

      const check = document.createElement("span");
      check.className = "visited-check";
      check.textContent = "✴️";

      const a = document.createElement("a");
      a.href = activity.strava_url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = `View "${activity.name}" on Strava`;
      a.addEventListener("click", () => { check.textContent = "✅"; });

      li.appendChild(check);
      li.appendChild(a);
      linksList.appendChild(li);
    }
  }

  // Preselected sport: the earliest activity's, since that's the one that
  // determines the combined file's <type> (see combine_gpx_files
  // server-side — the first file's <type> tag survives, the others' is
  // lost).
  const select = document.getElementById("activity-type");
  select.innerHTML = "";
  for (const choice of SPORT_CHOICES) {
    const opt = document.createElement("option");
    opt.value = choice.value;
    opt.textContent = choice.label;
    select.appendChild(opt);
  }
  const earliest = sorted[0];
  if (earliest._isLocal) {
    // Local files carry their own <type> tag (from Strava's own exports or
    // a previous run of this app), already using the same values as
    // SPORT_CHOICES — use it directly if recognized, else leave the first
    // option selected for the user to pick manually.
    const validValues = new Set(SPORT_CHOICES.map(c => c.value));
    if (earliest._gpxType && validValues.has(earliest._gpxType)) {
      select.value = earliest._gpxType;
    }
  } else {
    const defaultSport = STRAVA_TYPE_TO_SPORT_CHOICE[earliest.type];
    if (defaultSport) select.value = defaultSport;
  }

  document.getElementById("review-error").classList.add("hidden");
  document.getElementById("review-status").classList.add("hidden");
  document.getElementById("retry-upload-btn").classList.add("hidden");

  // Reset any leftover success state from a previous combine on this screen.
  state.lastUploadSucceeded = false;
  const uploadBtn = document.getElementById("upload-btn");
  uploadBtn.disabled = false;
  uploadBtn.textContent = "Upload to Strava";
  uploadBtn.classList.remove("btn-success");
  uploadBtn.classList.add("btn-primary");
  // No Strava session in guest mode — nothing to upload to, download only.
  uploadBtn.classList.toggle("hidden", state.guestMode);
  document.getElementById("view-combined-link").classList.add("hidden");

  showView("view-review");
}

document.getElementById("back-to-home-btn").addEventListener("click", () => {
  if (state.lastUploadSucceeded) {
    // After a successful upload, going "back" should start fresh rather
    // than return to a now-stale selection (this mirrors the old separate
    // "Combine more activities" success screen's behavior).
    state.activities = [];
    state.page = 1;
    state.combinedBlobText = null;
    state.mode = "strava";
    state.lastUploadSucceeded = false;
    closeHomeMap();
    showDayNotice("");
    document.getElementById("load-more-btn").classList.remove("hidden");
      showView("view-home");
    loadActivities();
  } else {
    showView("view-home");
  }
});

// Replaces the content of the <type>...</type> tag in the combined GPX with
// the sport chosen on this page — a simple text replacement, no server
// round-trip needed since the file is plain text.
function applySelectedSportToGpx(gpxText, sportValue) {
  if (/<type>.*?<\/type>/is.test(gpxText)) {
    return gpxText.replace(/<type>.*?<\/type>/is, `<type>${sportValue}</type>`);
  }
  return gpxText.replace("</name>", `</name>\n    <type>${sportValue}</type>`);
}

// Escapes the 3 characters that are reserved in XML text content (quotes
// don't need escaping outside of attribute values).
function escapeXmlText(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Replaces every <name>...</name> tag in the GPX (metadata and/or track)
// with the activity name chosen on this page, so the file itself — not
// just the separate "name" field sent to Strava's upload API — reflects
// what the user typed, including for the locally-downloaded copy.
function applyNameToGpx(gpxText, name) {
  const escaped = escapeXmlText(name);
  return gpxText.replace(/<name>.*?<\/name>/gis, `<name>${escaped}</name>`);
}

// Strips anything that isn't a letter, digit, space, hyphen, underscore,
// parenthesis or "+" — drops emoji, quotes, #, commas, apostrophes,
// colons, etc., which otherwise make some OSes/browsers refuse to save
// the file. "+" is kept: it's the separator this app itself inserts
// between combined activity names. Runs of whitespace (which stripping
// out emoji etc. can leave behind) collapse to a single space.
function sanitizeFilename(name) {
  const cleaned = name
    .replace(/[^\p{L}\p{N}\s\-_()+]/gu, "")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned || "combined-activity";
}

// Sets the <gpx creator="..."> attribute as the FIRST attribute right
// after the tag name — matching the layout real Strava exports use
// (<gpx creator="StravaGPX" version="1.1" ...>) — regardless of where an
// existing creator attribute sat, since a combined file no longer
// corresponds to any single recording device.
function applyCreatorToGpx(gpxText) {
  const stripped = gpxText.replace(/\s+creator="[^"]*"/i, "");
  return stripped.replace(/<gpx\b/i, `<gpx creator="GPX Combiner"`);
}

const GPX_PROJECT_URL = "https://github.com/florentchevallier/GPX-Combiner";

// Adds our project link and a description inside <metadata>, right before
// <time> if the file already has one (matching real Strava exports'
// layout) — or creates a <metadata> block (with no <time>, since none
// exists yet) right before <trk> if the file has no metadata at all.
function applyMetadataSignatureToGpx(gpxText, description) {
  const signature =
    `<link href="${GPX_PROJECT_URL}">\n` +
    `      <text>GPX Combiner</text>\n` +
    `    </link>\n` +
    `    <desc>${escapeXmlText(description)}</desc>`;

  // Inserting right after the opening <metadata> tag naturally lands
  // before <time> when the file has one (real Strava/device exports
  // almost always do), and is still valid GPX when it doesn't.
  const metadataOpen = gpxText.match(/<metadata\b[^>]*>/i);
  if (metadataOpen) {
    const openTagEnd = metadataOpen.index + metadataOpen[0].length;
    return gpxText.slice(0, openTagEnd) + `\n    ${signature}` + gpxText.slice(openTagEnd);
  }

  const trkMatch = gpxText.match(/<trk\b[^>]*>/i);
  if (!trkMatch) return gpxText; // no <trk> to anchor on — leave untouched
  const block = `<metadata>\n    ${signature}\n  </metadata>\n  `;
  return gpxText.slice(0, trkMatch.index) + block + gpxText.slice(trkMatch.index);
}

// Builds the final GPX text (sport + name + creator + our project
// signature applied) and the plain activity name, shared by both the
// download button and the Strava upload so the file content is always
// consistent between the two.
function buildFinalGpx() {
  const name = document.getElementById("activity-name").value.trim() || "Combined activity";
  const sport = document.getElementById("activity-type").value;
  let gpxText = applySelectedSportToGpx(state.combinedBlobText, sport);
  gpxText = applyNameToGpx(gpxText, name);
  gpxText = applyCreatorToGpx(gpxText);
  gpxText = applyMetadataSignatureToGpx(gpxText, "Created with GPX Combiner");
  return { gpxText, name };
}

// Saves the combined GPX straight to the phone/computer's downloads,
// with the currently chosen name and sport applied — available both
// before and after uploading to Strava (the name/sport fields and
// state.combinedBlobText are never cleared until "Combine more
// activities" is pressed, so this works from either screen).
function downloadCombinedGpx() {
  const { gpxText, name } = buildFinalGpx();

  const blob = new Blob([gpxText], { type: "application/gpx+xml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${sanitizeFilename(name)}.gpx`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

document.getElementById("download-btn").addEventListener("click", downloadCombinedGpx);

async function doUpload() {
  const { gpxText, name } = buildFinalGpx();

  const uploadBtn = document.getElementById("upload-btn");
  const retryBtn = document.getElementById("retry-upload-btn");
  const errorEl = document.getElementById("review-error");
  const statusEl = document.getElementById("review-status");

  uploadBtn.disabled = true;
  retryBtn.classList.add("hidden");
  errorEl.classList.add("hidden");
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Uploading… (Strava can take a few seconds to process the file)";

  let succeeded = false;

  const formData = new FormData();
  formData.append("file", new Blob([gpxText], { type: "application/gpx+xml" }), "combined.gpx");
  formData.append("name", name);

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    const payload = await res.json().catch(() => ({}));

    if (res.ok && payload.activity_id) {
      succeeded = true;
      onUploadSuccess(payload.activity_id);
      return;
    }

    statusEl.classList.add("hidden");
    const message = payload.error || "Upload failed.";
    if (message.toLowerCase().includes("duplicate")) {
      // Strava's raw error contains a technical HTML link to the existing
      // activity (and sometimes a badly-encoded name) — not useful to show
      // as-is, replaced entirely with a clear message.
      errorEl.textContent =
        "The upload failed because of a duplicate. If you just deleted " +
        "the original activity on Strava, wait a moment (Strava can take " +
        "a little while to notice) then try again. The combined file " +
        "hasn't been lost — nothing to redo.";
      const rawEl = document.createElement("div");
      rawEl.className = "hint";
      rawEl.style.marginTop = "8px";
      rawEl.style.fontSize = "0.75rem";
      // Strava's raw message starts with the filename we uploaded (our own
      // temp file's random name, e.g. "tmpth0efhik.gpx duplicate of ...") —
      // meaningless to the user, so it's stripped before display.
      const cleanedMessage = message
        .replace(/<[^>]*>/g, "")
        .replace(/^\S+\.gpx\s+/i, "");
      rawEl.textContent = "Strava message: " + cleanedMessage;
      errorEl.appendChild(rawEl);
      retryBtn.classList.remove("hidden");
    } else {
      errorEl.textContent = message;
    }
    errorEl.classList.remove("hidden");
  } catch (e) {
    statusEl.classList.add("hidden");
    errorEl.textContent = "Connection lost during upload. Please try again.";
    errorEl.classList.remove("hidden");
    retryBtn.classList.remove("hidden");
  } finally {
    if (!succeeded) uploadBtn.disabled = false;
  }
}

document.getElementById("upload-btn").addEventListener("click", doUpload);
document.getElementById("retry-upload-btn").addEventListener("click", doUpload);

// ---------------------------------------------------------------------------
// Success — shown in place on the review screen rather than a separate
// screen, so the source-activity links (needed to go delete the originals)
// stay visible and reachable right alongside it.
// ---------------------------------------------------------------------------

function onUploadSuccess(activityId) {
  document.getElementById("review-status").classList.add("hidden");

  const uploadBtn = document.getElementById("upload-btn");
  uploadBtn.textContent = "Success! Sent to Strava ✓";
  uploadBtn.classList.remove("btn-primary");
  uploadBtn.classList.add("btn-success");

  const link = document.getElementById("view-combined-link");
  link.href = `https://www.strava.com/activities/${activityId}`;
  link.classList.remove("hidden");

  state.lastUploadSucceeded = true;
}

init();
