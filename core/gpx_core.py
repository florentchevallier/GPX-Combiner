"""
gpx_core.py — shared GPX logic used by both the desktop app (Tkinter,
/desktop/) and the QGIS plugin (PyQt, /qgis-plugin/). Pure Python, no GUI
dependency, so both front-ends can import it as-is.

This is a trimmed port of the equivalent logic already working in
desktop/gpx_combiner.py — same regexes, same "stitch <trkseg> blocks in
chronological order" approach, same optional stripping of the
HR/cadence/power/temperature extension fields.
"""

import math
import re
from datetime import datetime, timedelta, timezone

TRKSEG_RE = re.compile(r"<trkseg\b.*?</trkseg>", re.S | re.I)
TIME_RE = re.compile(r"<time>(.*?)</time>", re.S | re.I)
FIRST_TRKPT_RE = re.compile(r"<trkpt\b[^>]*?(?:/>|>.*?</trkpt>)", re.S | re.I)

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


def shift_first_trkpt_time(content, delta_seconds=-1):
    """Nudges the time of the track's very first point by delta_seconds.
    Strava's duplicate-activity detection matches an upload against
    existing activities by exact start time; when the combined file's first
    point is identical to one of its own source activities (the usual
    case), this tiny, imperceptible offset is enough to stop Strava
    flagging it as a duplicate. Targets the first <trkpt> itself, not the
    file's first <time> (which, in Strava's own exports and most device
    files, is the one in <metadata>); a <metadata> time equal to the
    original start is moved along with it so the two stay consistent.
    Leaves the file untouched if that time can't be parsed."""
    pt = FIRST_TRKPT_RE.search(content)
    if not pt:
        return content
    m = TIME_RE.search(pt.group(0))
    if not m:
        return content
    original = m.group(1).strip()
    try:
        dt = datetime.strptime(original, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return content
    shifted = (dt + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start, end = pt.start() + m.start(1), pt.start() + m.end(1)
    content = content[:start] + shifted + content[end:]
    header = TIME_RE.search(content[:pt.start()])
    if header and header.group(1).strip() == original:
        a, b = header.span(1)
        content = content[:a] + shifted + content[b:]
    return content


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
    combined = shift_first_trkpt_time(combined)
    return combined, skipped


def prepare_single_gpx(content, include=None):
    """The one-file counterpart of combine_gpx_files(): readies a single
    GPX to be sent on its own — same optional stripping of the
    HR/cadence/power/temp fields, same one-second nudge of the first point
    (so a copy of an activity that still exists on Strava isn't rejected
    as its duplicate). Raises ValueError if the file has no track."""
    if extract_first_trkseg(content) is None:
        raise ValueError("File has no track (<trkseg>)")
    if include is None:
        include = {k: True for k in EXTENSION_FIELDS}
    return shift_first_trkpt_time(strip_extension_fields(content, include))


# ----------------------------------------------------------------------------
# Trimming a single GPX (keeping only a portion) and finding its pauses
# ----------------------------------------------------------------------------

# One whole track point, either self-closing (<trkpt .../>) or with children.
TRKPT_BLOCK_RE = re.compile(r"<trkpt\b[^>]*?(?:/>|>.*?</trkpt>)", re.S | re.I)
LAT_RE = re.compile(r"""\blat\s*=\s*["']([^"']+)["']""", re.I)
LON_RE = re.compile(r"""\blon\s*=\s*["']([^"']+)["']""", re.I)
EMPTY_TRKSEG_RE = re.compile(r"[ \t]*<trkseg\b[^>]*>\s*</trkseg>[ \t]*\n?", re.I)
ISO_TIME_RE = re.compile(
    r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", re.I)

# Defaults for find_pauses(): a first setting, to be tuned with real use.
PAUSE_MIN_GAP_SECONDS = 120
PAUSE_MIN_JUMP_METERS = 200


def parse_gpx_time(value):
    """A GPX <time> value as an aware UTC datetime, or None if it can't be
    read. Accepts "Z" or a numeric offset, with or without fractional
    seconds (dropped); a time with no zone at all is taken as UTC."""
    m = ISO_TIME_RE.search(value or "")
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()[:6]), tzinfo=timezone.utc)
    except ValueError:
        return None
    zone = m.group(7)
    if zone and zone.upper() != "Z":
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        dt = dt - offset if zone[0] == "+" else dt + offset
    return dt


def haversine_meters(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def list_trkpts(content):
    """Every track point of the file, in file order and across all
    <trkseg> blocks: a list of {"lat": float, "lon": float,
    "time": datetime or None}. A point's position in this list is the
    index that find_pauses() and trim_gpx() work with. Points whose lat/lon
    can't be read are still listed (lat/lon None) so indexes stay aligned
    with the file."""
    points = []
    for m in TRKPT_BLOCK_RE.finditer(content):
        block = m.group(0)
        tag = block[:block.find(">") + 1]
        lat_m, lon_m = LAT_RE.search(tag), LON_RE.search(tag)
        try:
            lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        except (AttributeError, ValueError):
            lat = lon = None
        time_m = TIME_RE.search(block)
        points.append({"lat": lat, "lon": lon,
                       "time": parse_gpx_time(time_m.group(1)) if time_m else None})
    return points


def find_pauses(content, min_gap_seconds=PAUSE_MIN_GAP_SECONDS,
                min_jump_meters=PAUSE_MIN_JUMP_METERS):
    """Candidate cut points: every place where two consecutive track
    points are at least min_gap_seconds apart in time OR at least
    min_jump_meters apart on the ground (a recording paused during a bus
    ride shows both; a stop on the spot shows only the first).

    Returns a list of {"index": i, "gap_seconds": float or None,
    "jump_meters": float or None, "time_before": datetime or None,
    "time_after": datetime or None}, where i is the index (see
    list_trkpts) of the last point BEFORE the pause — so the part before
    is points 0..i and the part after starts at i + 1. Longest gaps first
    (a jump only breaks a tie, or ranks pauses with no times). Never cuts
    anything by itself."""
    points = list_trkpts(content)
    pauses = []
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        gap = None
        if a["time"] and b["time"]:
            gap = (b["time"] - a["time"]).total_seconds()
        jump = None
        if None not in (a["lat"], a["lon"], b["lat"], b["lon"]):
            jump = haversine_meters(a["lat"], a["lon"], b["lat"], b["lon"])
        if (gap is not None and gap >= min_gap_seconds) or (jump is not None and jump >= min_jump_meters):
            pauses.append({"index": i, "gap_seconds": gap, "jump_meters": jump,
                           "time_before": a["time"], "time_after": b["time"]})
    pauses.sort(key=lambda p: (-(p["gap_seconds"] or 0), -(p["jump_meters"] or 0)))
    return pauses


def trim_gpx(content, start_index, end_index):
    """Keeps only track points start_index..end_index (both included,
    indexes as in list_trkpts) — see keep_trkpt_ranges()."""
    return keep_trkpt_ranges(content, [(start_index, end_index)])


def keep_trkpt_ranges(content, ranges):
    """Keeps only the track points inside the given ranges — a list of
    (start, end) index pairs, both included, indexes as in list_trkpts —
    and removes the others, leaving everything else in the file untouched
    (header, metadata, name, type, extensions of the kept points). Several
    ranges keep several parts of the track, e.g. the segments before and
    after a pause but not the pause itself; the parts stay in one track,
    with a gap where points were removed (as if the recording had been
    paused there). A <trkseg> left with no point is removed too, and a
    <metadata> <time>, if any, is set to the first kept point's time.
    Raises ValueError if a range is invalid, ranges overlap, or fewer than
    2 points are kept in all."""
    matches = list(TRKPT_BLOCK_RE.finditer(content))
    total = len(matches)
    ranges = sorted((int(a), int(b)) for a, b in ranges)
    if not ranges:
        raise ValueError("No range to keep")
    for k, (a, b) in enumerate(ranges):
        if not (0 <= a <= b < total):
            raise ValueError(f"Invalid range {a}..{b} for {total} track points")
        if k > 0 and a <= ranges[k - 1][1]:
            raise ValueError("Ranges overlap")
    if sum(b - a + 1 for a, b in ranges) < 2:
        raise ValueError("A trimmed track needs at least 2 points")
    keep = [False] * total
    for a, b in ranges:
        for i in range(a, b + 1):
            keep[i] = True

    out, pos = [], 0
    for i, m in enumerate(matches):
        if keep[i]:
            continue
        # Drop the point together with its own line (leading indent and
        # trailing newline), so no blank lines are left behind.
        begin = m.start()
        while begin > pos and content[begin - 1] in " \t":
            begin -= 1
        end = m.end()
        while end < len(content) and content[end] in " \t":
            end += 1
        if end < len(content) and content[end] == "\r":
            end += 1
        if end < len(content) and content[end] == "\n":
            end += 1
        out.append(content[pos:begin])
        pos = end
    out.append(content[pos:])
    trimmed = EMPTY_TRKSEG_RE.sub("", "".join(out))

    # A file-level <time> (the one in <metadata>, before the first point)
    # states when the track starts — this app sorts files by it. Once the
    # beginning is cut off it would be stale, so align it with the first
    # point that's left.
    first_pt = TRKPT_BLOCK_RE.search(trimmed)
    header_time = TIME_RE.search(trimmed[:first_pt.start()])
    first_pt_time = TIME_RE.search(first_pt.group(0))
    if header_time and first_pt_time:
        a, b = header_time.span(1)
        trimmed = trimmed[:a] + first_pt_time.group(1).strip() + trimmed[b:]
    return trimmed
