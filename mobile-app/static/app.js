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
const PAGE_SIZE = 5;

const state = {
  activities: [],       // all activities loaded so far
  page: 1,
  combinedBlobText: null, // text content of the combined GPX, waiting to be uploaded
  mode: "strava",       // "strava" | "local" — never mixed (see the local-import section below)
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
    return;
  }
  document.getElementById("user-label").textContent =
    `Logged in as ${me.firstname} ${me.lastname}`;
  showView("view-home");
  await loadActivities();
}

async function loadActivities() {
  const res = await fetch(`/activities?page=${state.page}&per_page=${PAGE_SIZE}`);
  if (!res.ok) {
    showHomeError("Couldn't load your activities. Try again later.");
    return;
  }
  const newActivities = await res.json();
  state.activities.push(...newActivities);
  renderActivityList();

  const loadMoreBtn = document.getElementById("load-more-btn");
  if (state.activities.length >= MAX_ACTIVITIES || newActivities.length < PAGE_SIZE) {
    loadMoreBtn.classList.add("hidden");
  } else {
    loadMoreBtn.classList.remove("hidden");
  }
}

function renderActivityList() {
  const list = document.getElementById("activity-list");
  list.innerHTML = "";
  for (const activity of state.activities) {
    const li = document.createElement("li");
    li.className = "activity-item";
    const metaLine = activity._isLocal
      ? `Local GPX file · ${formatDate(activity.start_date_local)}`
      : `${escapeHtml(activity.type)} · ${formatDate(activity.start_date_local)} · ` +
        `${formatDuration(activity.moving_time)} · ${formatDistance(activity.distance)}`;
    // Local files are checked by default (the user just picked them
    // deliberately from their phone) — Strava activities stay opt-in.
    const checkedAttr = activity._isLocal ? "checked" : "";
    li.innerHTML = `
      <input type="checkbox" data-id="${activity.id}" ${checkedAttr}>
      <div class="activity-info">
        <div class="activity-name">${escapeHtml(activity.name)}</div>
        <div class="activity-meta">${metaLine}</div>
      </div>
    `;
    list.appendChild(li);
  }
  updateCombineButtonState();
}

function getSelectedActivities() {
  // Compared as strings: Strava activity ids are numbers, local-file ids
  // are strings like "local-0" — String(a.id) makes both sides match.
  const checked = new Set(
    Array.from(document.querySelectorAll("#activity-list input[type=checkbox]:checked"))
      .map(cb => cb.dataset.id)
  );
  return state.activities.filter(a => checked.has(String(a.id)));
}

function updateCombineButtonState() {
  const combineBtn = document.getElementById("combine-btn");
  combineBtn.disabled = getSelectedActivities().length < 2;
}

function showHomeError(msg) {
  const el = document.getElementById("home-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

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
// Local GPX file import — an alternative source to Strava, never mixed
// with it (replaces the activity list rather than appending to it).
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

  return { name, startDate };
}

document.getElementById("load-local-btn").addEventListener("click", () => {
  document.getElementById("local-file-input").click();
});

document.getElementById("local-file-input").addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = ""; // allow re-selecting the same file(s) later
  if (files.length === 0) return;

  clearHomeError();
  const parsed = [];
  for (const file of files) {
    const text = await file.text();
    const meta = parseLocalGpxFile(file.name, text);
    if (!meta) {
      showHomeError(`"${file.name}" doesn't look like a valid GPX file with a track date — skipped.`);
      continue;
    }
    parsed.push({
      id: `local-${parsed.length}-${file.name}`,
      name: meta.name,
      type: null,
      start_date: meta.startDate,
      start_date_local: meta.startDate,
      distance: null,
      moving_time: null,
      _isLocal: true,
      _localContent: text,
    });
  }
  if (parsed.length === 0) return;

  state.mode = "local";
  state.activities = parsed;
  renderActivityList();
  document.getElementById("load-more-btn").classList.add("hidden");
  document.getElementById("back-to-strava-btn").classList.remove("hidden");
});

document.getElementById("back-to-strava-btn").addEventListener("click", async () => {
  state.mode = "strava";
  state.activities = [];
  state.page = 1;
  clearHomeError();
  document.getElementById("back-to-strava-btn").classList.add("hidden");
  document.getElementById("load-more-btn").classList.remove("hidden");
  await loadActivities();
});

// ---------------------------------------------------------------------------
// Combine
// ---------------------------------------------------------------------------

document.getElementById("load-more-btn").addEventListener("click", async () => {
  state.page += 1;
  await loadActivities();
});

document.getElementById("activity-list").addEventListener("change", updateCombineButtonState);

document.getElementById("combine-btn").addEventListener("click", async () => {
  clearHomeError();
  const selected = getSelectedActivities();
  if (selected.length < 2) return;

  const combineBtn = document.getElementById("combine-btn");
  combineBtn.disabled = true;
  combineBtn.textContent = "Combining…";

  try {
    const body = state.mode === "local"
      ? { local_files: selected.map(a => ({ name: a.name, content: a._localContent })),
          include: getIncludeSettings() }
      : { activities: selected, include: getIncludeSettings() };

    const res = await fetch("/combine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showHomeError(err.error || "Combining failed.");
      return;
    }

    state.combinedBlobText = await res.text();
    openReviewScreen(selected);
  } finally {
    combineBtn.disabled = false;
    combineBtn.textContent = "Combine selected activities";
    updateCombineButtonState();
  }
});

// ---------------------------------------------------------------------------
// Review before upload
// ---------------------------------------------------------------------------

function openReviewScreen(selectedActivities) {
  // Default name: names concatenated in chronological order, joined by
  // " + " — same convention as the desktop app.
  const sorted = [...selectedActivities].sort((a, b) => a.start_date.localeCompare(b.start_date));
  document.getElementById("activity-name").value = sorted.map(a => a.name).join(" + ");

  // Manual-deletion links to each source activity — only relevant when the
  // sources are actual Strava activities. Nothing to delete for a local
  // GPX import, so that whole block is hidden in that mode.
  const hint = document.getElementById("delete-originals-hint");
  const linksList = document.getElementById("source-activity-links");
  linksList.innerHTML = "";
  if (state.mode === "local") {
    hint.classList.add("hidden");
    linksList.classList.add("hidden");
  } else {
    hint.classList.remove("hidden");
    linksList.classList.remove("hidden");
    for (const activity of sorted) {
      const li = document.createElement("li");

      const check = document.createElement("span");
      check.className = "visited-check";

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
  // lost). Local files have no Strava "type" to map from, so the dropdown
  // just keeps its first option and the user picks manually.
  const select = document.getElementById("activity-type");
  select.innerHTML = "";
  for (const choice of SPORT_CHOICES) {
    const opt = document.createElement("option");
    opt.value = choice.value;
    opt.textContent = choice.label;
    select.appendChild(opt);
  }
  if (state.mode !== "local") {
    const earliest = sorted[0];
    const defaultSport = STRAVA_TYPE_TO_SPORT_CHOICE[earliest.type];
    if (defaultSport) select.value = defaultSport;
  }

  document.getElementById("review-error").classList.add("hidden");
  document.getElementById("review-status").classList.add("hidden");
  document.getElementById("retry-upload-btn").classList.add("hidden");
  showView("view-review");
}

document.getElementById("back-to-home-btn").addEventListener("click", () => {
  showView("view-home");
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

async function doUpload() {
  const name = document.getElementById("activity-name").value.trim() || "Combined activity";
  const sport = document.getElementById("activity-type").value;
  const gpxText = applySelectedSportToGpx(state.combinedBlobText, sport);

  const uploadBtn = document.getElementById("upload-btn");
  const retryBtn = document.getElementById("retry-upload-btn");
  const errorEl = document.getElementById("review-error");
  const statusEl = document.getElementById("review-status");

  uploadBtn.disabled = true;
  retryBtn.classList.add("hidden");
  errorEl.classList.add("hidden");
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Uploading… (Strava can take a few seconds to process the file)";

  const formData = new FormData();
  formData.append("file", new Blob([gpxText], { type: "application/gpx+xml" }), "combined.gpx");
  formData.append("name", name);

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    const payload = await res.json().catch(() => ({}));

    if (res.ok && payload.activity_id) {
      showSuccess(payload.activity_id);
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
      rawEl.textContent = "Strava message: " + message.replace(/<[^>]*>/g, "");
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
    uploadBtn.disabled = false;
  }
}

document.getElementById("upload-btn").addEventListener("click", doUpload);
document.getElementById("retry-upload-btn").addEventListener("click", doUpload);

// ---------------------------------------------------------------------------
// Success
// ---------------------------------------------------------------------------

function showSuccess(activityId) {
  document.getElementById("success-link").href = `https://www.strava.com/activities/${activityId}`;
  showView("view-success");
}

document.getElementById("restart-btn").addEventListener("click", () => {
  // Reset state and go back to home, without forcing a reconnect. Always
  // starts fresh on the Strava list, even if the previous combine used
  // local files.
  state.activities = [];
  state.page = 1;
  state.combinedBlobText = null;
  state.mode = "strava";
  document.getElementById("load-more-btn").classList.remove("hidden");
  document.getElementById("back-to-strava-btn").classList.add("hidden");
  showView("view-home");
  loadActivities();
});

init();
