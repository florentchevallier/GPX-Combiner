# Changelog

This summarizes what changed in each version, for both the desktop app (`gpx_combiner.py`) and the QGIS plugin (`qgis-plugin/`). Reconstructed from development notes rather than from git tags — if a detail below doesn't quite match your commit history, the code itself (and its version number shown in the window title) is the source of truth.

## Desktop app

### 3.6.x
- Scrollable Strava activity list, so choosing 25/50 per page no longer grows the window past screen height and out of reach of the "per page" control
- `</trkseg>` / `<trkseg>` now on separate lines when combining, for easier diffing/collapsing in a text editor
- `gpxdata:distance` (a cumulative per-recording distance some devices embed, e.g. COROS) is now always stripped when combining — it becomes misleading once two separate recordings are spliced together. `gpxdata:speed` and `gpxdata:hr` are left untouched, since those stay valid per-point regardless of splicing
- Removed the "download complete" popup after a Strava import — the list updating is confirmation enough; the "file saved" popup after combining is unchanged

### 3.5.x
- Fixed Strava activity table header misalignment (rewritten to use fixed pixel-width columns instead of character-based `width=`, which drifted out of sync between the bold header and regular-weight rows)
- Fixed a bug where the "started near" city column would go blank after a page fully loaded (a Tkinter StringVar garbage-collection issue — the variables weren't being kept alive)
- 10/25/50-per-page selector for Strava activities
- "Started near" column via reverse geocoding (OpenStreetMap/Nominatim), resolved in the background so the UI doesn't freeze
- Per-file HR/Cadence/Power/Temp badges, and checkboxes to include/exclude each field when combining
- Strava settings window: shows the connected account, lets you disconnect and erase saved credentials; fixed the "Connect" button not triggering authorization, and the credentials dialog's Cancel button leaving the app in a broken state
- Switched to `certifi` for HTTPS certificate verification — fixes Strava calls failing with a certificate error in a packaged (py2app) build, which has no access to the system's certificate store
- Windows: the app icon now appears in the title bar and taskbar (previously only on the `.exe` file itself)

### 3.4
- `<type>` tag (e.g. `cycling`, `running`) added to GPX files downloaded from Strava, matching Strava's own export format, mapped from Strava's activity type
- Default interface language switched to English, with the last-used language remembered between launches (`app_config.json`, separate from `strava_config.json`)

### 3.3
- Strava-downloaded GPX files now include heart rate, cadence, power, and temperature (previously altitude only), written in the same `<extensions>` format Strava's own exports use, for clean re-upload compatibility

### 3.2
- Drag-and-drop support for adding GPX files (optional `tkinterdnd2` dependency; falls back gracefully to the "Add files" button if not installed)
- Strava credentials now requested in a single combined dialog (Client ID + Secret together) with an explanation of where to find them, instead of two separate prompts
- Fixed: cancelling the credentials dialog used to continue anyway and fail with a timeout instead of stopping cleanly

### 3.1
- Map preview: legend showing each loaded file's color, start markers drawn under finish markers (🏁) so a finish point is never hidden under another track's start dot

### Earlier (unversioned)
- Combine multiple GPX files chronologically into one
- Import activities from Strava (OAuth, browse/select/download as GPX)
- Map preview on an OpenStreetMap basemap, with per-track colors
- Multi-language interface (French, English, Spanish, German)

## QGIS plugin

### 0.2.0-beta
- Strava import: credentials dialog, OAuth authorization, browse recent activities, download selected ones as GPX (HR/cadence/power/temperature included) — downloads land silently in a system temp folder, no save-location prompt
- "Add OpenStreetMap basemap" button, registering a real XYZ Tiles connection (visible in the Browser panel afterwards too), not just a one-off layer
- Loaded tracks are colored (same palette as the desktop app's map preview), styled with directional arrow markers along each line, grouped under a "GPX Combiner" layer group, auto-selected and zoomed-to after loading
- "Remove selected" button and Delete/Backspace key support for removing files from the list (never touches the file on disk)
- Remembers the last folder used for adding files and for saving the combined output (`QgsSettings`), defaulting to `~/Downloads` until one is set
- Fixed the plugin window occasionally dropping behind the QGIS main window after a native file dialog closes (macOS/Qt quirk)
- Custom app icon
- Shares its core GPX-combining logic with the desktop app via `core/gpx_core.py`, and its Strava client via `core/strava_client.py` — both plain Python, no GUI dependency

### 0.1.0-prototype
- Initial proof of concept: add GPX files, load their tracks as QGIS layers via the native GPX/OGR driver, combine and export with HR/Cadence/Power/Temp checkboxes
