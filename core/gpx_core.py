"""
gpx_core.py — shared GPX logic used by both the desktop app (Tkinter,
/desktop/) and the QGIS plugin (PyQt, /qgis-plugin/). Pure Python, no GUI
dependency, so both front-ends can import it as-is.

This is a trimmed port of the equivalent logic already working in
desktop/gpx_combiner.py — same regexes, same "stitch <trkseg> blocks in
chronological order" approach, same optional stripping of the
HR/cadence/power/temperature extension fields.
"""

import re

TRKSEG_RE = re.compile(r"<trkseg\b.*?</trkseg>", re.S | re.I)
TIME_RE = re.compile(r"<time>(.*?)</time>", re.S | re.I)

EXTENSION_FIELDS = ["hr", "cadence", "power", "temp"]
EXTENSION_FIELD_LABELS = {"hr": "HR", "cadence": "Cadence", "power": "Power", "temp": "Temp"}

# Same palette as the desktop app's map preview (MapPreviewWindow), so a
# track looks the same color whether you're looking at it there or in QGIS.
TRACK_COLOR_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#46b3c2", "#f032e6", "#9a8b00", "#008080", "#e6beff",
]

EXT_FIELD_DETECT_RE = {
    "hr": re.compile(r"<gpxtpx:hr>", re.I),
    "cadence": re.compile(r"<gpxtpx:cad>", re.I),
    "power": re.compile(r"<(?:power|gpxpx:PowerInWatts)>", re.I),
    "temp": re.compile(r"<gpxtpx:atemp>", re.I),
}
EXT_FIELD_STRIP_RE = {
    "hr": re.compile(r"<gpxtpx:hr>.*?</gpxtpx:hr>", re.I | re.S),
    "cadence": re.compile(r"<gpxtpx:cad>.*?</gpxtpx:cad>", re.I | re.S),
    "power": re.compile(r"<power>.*?</power>|<gpxpx:PowerInWatts>.*?</gpxpx:PowerInWatts>", re.I | re.S),
    "temp": re.compile(r"<gpxtpx:atemp>.*?</gpxtpx:atemp>", re.I | re.S),
}
EMPTY_TPX_RE = re.compile(r"<gpxtpx:TrackPointExtension>\s*</gpxtpx:TrackPointExtension>", re.I)
EMPTY_EXT_RE = re.compile(r"<extensions>\s*</extensions>", re.I)

# Some devices (e.g. COROS, via the cluetrust "gpxdata" extension schema)
# embed a per-point CUMULATIVE distance-from-start-of-this-recording value.
# That's fine within a single original file, but becomes actively misleading
# once two separately-recorded files are spliced together (each one's
# counter restarts at 0) — unlike gpxdata:speed or gpxdata:hr, which stay
# valid per-point measurements regardless of splicing, so those are left
# alone. Always stripped when combining, not tied to the HR/cadence/power/
# temp checkboxes (which only cover the gpxtpx:/power Garmin-style schema).
GPXDATA_DISTANCE_RE = re.compile(r"<gpxdata:distance>.*?</gpxdata:distance>", re.I | re.S)


def extract_sort_key(content, fallback):
    """First <time> found in the file, or a fallback (e.g. filename) if none."""
    m = TIME_RE.search(content)
    return m.group(1).strip() if m else fallback


def extract_first_trkseg(content):
    """The first <trkseg>...</trkseg> block (tags included), or None."""
    m = TRKSEG_RE.search(content)
    return m.group(0) if m else None


def detect_extension_fields(content):
    """Which of hr/cadence/power/temp this GPX's trkpts actually carry."""
    return {key: bool(rx.search(content)) for key, rx in EXT_FIELD_DETECT_RE.items()}


def strip_extension_fields(content, include):
    """Remove the extension tags for any field where include[field] is False,
    always remove gpxdata:distance (see GPXDATA_DISTANCE_RE above), then
    clean up any now-empty wrapper tags left behind."""
    for key, rx in EXT_FIELD_STRIP_RE.items():
        if not include.get(key, True):
            content = rx.sub("", content)
    content = GPXDATA_DISTANCE_RE.sub("", content)
    content = EMPTY_TPX_RE.sub("", content)
    content = EMPTY_EXT_RE.sub("", content)
    return content


def combine_gpx_files(files, include=None):
    """
    Combine GPX files chronologically into one, the same way the desktop
    app does: the first (oldest) file is the base, and every other file's
    <trkseg> block is inserted right after the base's own </trkseg> — so the
    result is one <trk> with several <trkseg> children (valid GPX 1.1).

    files: list of {"path": str, "content": str}, already sorted
           chronologically by the caller (see extract_sort_key).
    include: optional {"hr": bool, "cadence": bool, "power": bool,
              "temp": bool} — fields not included are stripped from the
              output. Defaults to keeping everything.

    Returns (combined_gpx_text, skipped_paths) — skipped_paths lists any
    file (besides the base) that had no <trkseg> and was left out.
    Raises ValueError if fewer than 2 files are given, or if the base file
    has no </trkseg>.
    """
    if len(files) < 2:
        raise ValueError("Need at least two files to combine")
    if include is None:
        include = {k: True for k in EXTENSION_FIELDS}

    base_content = strip_extension_fields(files[0]["content"], include)
    marker = "</trkseg>"
    idx = base_content.find(marker)
    if idx == -1:
        raise ValueError(f"Base file has no </trkseg>: {files[0]['path']}")
    insert_pos = idx + len(marker)

    extra_segments = []
    skipped = []
    for f in files[1:]:
        seg = extract_first_trkseg(f["content"])
        if seg is None:
            skipped.append(f["path"])
            continue
        extra_segments.append(strip_extension_fields(seg, include))

    combined = base_content[:insert_pos] + "\n" + "\n".join(extra_segments) + base_content[insert_pos:]
    return combined, skipped
