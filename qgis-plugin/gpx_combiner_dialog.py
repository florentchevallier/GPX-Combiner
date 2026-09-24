import os
import re
import webbrowser

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton,
    QCheckBox, QFileDialog, QLabel, QMessageBox, QAbstractItemView, QShortcut,
)
from qgis.PyQt.QtGui import QColor, QKeySequence
from qgis.PyQt.QtCore import QItemSelectionModel, Qt
from qgis.core import (
    QgsVectorLayer, QgsProject, QgsRasterLayer, QgsRectangle, QgsCoordinateTransform,
    QgsLineSymbol, QgsSingleSymbolRenderer, QgsUnitTypes, QgsSettings,
    QgsSimpleLineSymbolLayer, QgsMarkerLineSymbolLayer, QgsSimpleMarkerSymbolLayer,
    QgsSimpleMarkerSymbolLayerBase, QgsMarkerSymbol,
    QgsMessageLog, Qgis,
)

# From ../core/gpx_core.py — see the sys.path setup in gpx_combiner_plugin.py.
from gpx_core import (
    extract_sort_key, detect_extension_fields, combine_gpx_files,
    EXTENSION_FIELDS, EXTENSION_FIELD_LABELS, TRACK_COLOR_PALETTE,
)
from strava_client import StravaClient, StravaAuthError, read_gpx_type

OSM_XYZ_NAME = "OpenStreetMap"
OSM_XYZ_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TRACK_LINE_WIDTH_MM = 1.6
GPX_GROUP_NAME = "GPX Combiner"
SETTINGS_LAST_OPEN_DIR = "gpx_combiner/last_open_dir"
SETTINGS_LAST_SAVE_DIR = "gpx_combiner/last_save_dir"


# Same narrowed filter as strava_dialog.py's _clean_display_text — see the
# comment there (2026-09-24) for why this is scoped to just variation
# selectors now, not whole emoji/symbol blocks. Kept as a small local copy
# here rather than imported, so this file doesn't have to eagerly load
# strava_dialog.py's Qt-heavy imports just for this.
_DISPLAY_STRIP_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\ufe00-\ufe0f]")


def _clean_display_text(text):
    return _DISPLAY_STRIP_RE.sub("", text or "")


def _read_plugin_version():
    """Reads version= from this plugin's own metadata.txt, so the window
    title can show it — handy when you have more than one install of this
    plugin around (e.g. a dev symlink and a separately installed .zip) and
    need to tell at a glance which one you're actually looking at."""
    metadata_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt")
    try:
        with open(metadata_path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"version\s*=\s*(.+)", line.strip())
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return None


class GpxCombinerDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        version = _read_plugin_version()
        self.setWindowTitle(f"GPX Combiner v{version}" if version else "GPX Combiner")
        self.resize(600, 440)
        self.files = []  # [{"path": str, "content": str, "sort_key": str}]

        layout = QVBoxLayout(self)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add GPX files…")
        add_btn.clicked.connect(self.add_files)
        btn_row.addWidget(add_btn)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self.remove_selected)
        btn_row.addWidget(remove_btn)
        load_btn = QPushButton("Load as layers")
        load_btn.clicked.connect(self.load_as_layers)
        btn_row.addWidget(load_btn)
        basemap_btn = QPushButton("Add OpenStreetMap basemap")
        basemap_btn.clicked.connect(self.add_osm_basemap)
        btn_row.addWidget(basemap_btn)
        strava_btn = QPushButton("Import from Strava…")
        strava_btn.clicked.connect(self.open_strava_import)
        btn_row.addWidget(strava_btn)
        strava_settings_btn = QPushButton("⚙ Strava settings")
        strava_settings_btn.clicked.connect(self.open_strava_settings)
        btn_row.addWidget(strava_settings_btn)
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("Detected chronological order (oldest to newest):"))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget)

        # Delete/Backspace removes the selected file(s) from this list —
        # never touches anything on disk, just the plugin's working set.
        for key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            shortcut = QShortcut(QKeySequence(key), self.list_widget)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.remove_selected)

        layout.addWidget(QLabel("Include in export:"))
        fields_row = QHBoxLayout()
        self.field_checks = {}
        for key in EXTENSION_FIELDS:
            cb = QCheckBox(EXTENSION_FIELD_LABELS[key])
            cb.setChecked(True)
            fields_row.addWidget(cb)
            self.field_checks[key] = cb
        fields_row.addStretch()
        layout.addLayout(fields_row)

        combine_btn = QPushButton("Combine and save…")
        combine_btn.clicked.connect(self.combine_and_save)
        layout.addWidget(combine_btn)

        self.upload_btn = QPushButton("Upload to Strava…")
        self.upload_btn.clicked.connect(self.upload_to_strava)
        self.upload_btn.setEnabled(False)
        layout.addWidget(self.upload_btn)

    # -- window focus --
    def _bring_to_front(self):
        """macOS/Qt quirk: after a native file dialog closes, this window
        can end up behind QGIS's main window instead of regaining focus —
        easy to mistake for a crash. Force it back to front."""
        self.raise_()
        self.activateWindow()

    # -- file list --
    def add_files(self):
        settings = QgsSettings()
        start_dir = settings.value(SETTINGS_LAST_OPEN_DIR, "")
        paths, _ = QFileDialog.getOpenFileNames(self, "Add GPX files", start_dir, "GPX files (*.gpx)")
        self._bring_to_front()
        if paths:
            settings.setValue(SETTINGS_LAST_OPEN_DIR, os.path.dirname(paths[0]))
        self.add_paths(paths)

    def add_paths(self, paths):
        """Adds GPX files to the list from a list of file paths — used both
        by the local "Add GPX files…" button and by the Strava import
        window handing back the files it just downloaded."""
        for path in paths:
            if any(f["path"] == path for f in self.files):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except OSError as e:
                QMessageBox.warning(self, "Read error", f"Could not read {path}:\n{e}")
                continue
            sort_key = extract_sort_key(content, fallback=os.path.basename(path))
            self.files.append({"path": path, "content": content, "sort_key": sort_key})
        self.files.sort(key=lambda f: f["sort_key"])
        self.refresh_list()

    def refresh_list(self):
        self.list_widget.clear()
        for i, f in enumerate(self.files, start=1):
            ext = detect_extension_fields(f["content"])
            gpx_type = read_gpx_type(f["content"])
            badges = "  ".join(EXTENSION_FIELD_LABELS[k] for k in EXTENSION_FIELDS if ext.get(k))
            extras = "   |   ".join(x for x in (gpx_type, badges) if x)
            label = f"{i}. {f['sort_key']}  —  {_clean_display_text(os.path.basename(f['path']))}"
            if extras:
                label += f"   |   {extras}"
            self.list_widget.addItem(label)
        self.upload_btn.setEnabled(len(self.files) >= 2)

    def remove_selected(self):
        """Removes the selected file(s) from this list only — never deletes
        anything on disk."""
        rows = sorted({index.row() for index in self.list_widget.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            del self.files[row]
        self.refresh_list()

    def open_strava_import(self):
        from .strava_dialog import StravaImportDialog
        dlg = StravaImportDialog(self, on_downloaded=self.add_paths)
        dlg.exec()
        self._bring_to_front()

    def open_strava_settings(self):
        from .strava_dialog import StravaSettingsDialog
        dlg = StravaSettingsDialog(self)
        dlg.exec()
        self._bring_to_front()

    # -- basemap --
    def add_osm_basemap(self):
        """Registers an "OpenStreetMap" XYZ Tiles connection (so it also
        shows up under Browser > XYZ Tiles from now on, like any basemap you
        added by hand) and adds it to the current project. QGIS stores XYZ
        connections as plain QgsSettings entries — this is the same
        mechanism the Browser panel itself uses, not a private/undocumented
        trick."""
        settings = QgsSettings()
        key_root = f"qgis/connections-xyz/{OSM_XYZ_NAME}"
        if not settings.value(f"{key_root}/url"):
            settings.setValue(f"{key_root}/url", OSM_XYZ_URL)
            settings.setValue(f"{key_root}/zmin", 0)
            settings.setValue(f"{key_root}/zmax", 19)

        uri = f"type=xyz&url={OSM_XYZ_URL.replace('{', '%7B').replace('}', '%7D')}&zmax=19&zmin=0"
        layer = QgsRasterLayer(uri, OSM_XYZ_NAME, "wms")
        if not layer.isValid():
            QMessageBox.warning(self, "GPX Combiner", "Could not add the OpenStreetMap basemap layer.")
            return
        QgsProject.instance().addMapLayer(layer, False)
        # Insert at the end of the layer tree = bottom of the drawing order,
        # so tracks added afterwards stay visible on top of the basemap.
        QgsProject.instance().layerTreeRoot().insertLayer(-1, layer)
        self.iface.mapCanvas().refresh()

    # -- map --
    def load_as_layers(self):
        """Loads each GPX's track(s) as a QGIS vector layer via the native
        OGR "GPX" driver — no custom parsing needed for display, unlike the
        desktop app's hand-rolled OSM tile canvas. Each track gets a color
        from the same palette used in the desktop app's map preview, cycling
        if there are more files than colors, at a fixed 1.6 mm line width.
        Layers are grouped under a "GPX Combiner" group, selected in the
        Layers panel, and the map is zoomed to just these layers (not the
        whole project, which could still be sitting on a world-view extent
        from a basemap or earlier layers)."""
        if not self.files:
            QMessageBox.information(self, "GPX Combiner", "Add some GPX files first.")
            return

        group = self._get_or_create_gpx_group()
        added_layers = []
        for i, f in enumerate(self.files):
            uri = f"{f['path']}|layername=tracks"
            layer = QgsVectorLayer(uri, os.path.basename(f["path"]), "ogr")
            if not layer.isValid():
                QMessageBox.warning(self, "GPX Combiner", f"Could not load {f['path']} as a layer.")
                continue

            color = TRACK_COLOR_PALETTE[i % len(TRACK_COLOR_PALETTE)]
            layer.setRenderer(QgsSingleSymbolRenderer(self._build_track_symbol(color)))
            layer.triggerRepaint()

            QgsProject.instance().addMapLayer(layer, False)
            group.insertLayer(0, layer)  # newest file on top within the group
            added_layers.append(layer)

        if added_layers:
            self._select_layers_in_panel(added_layers)
            self._zoom_to_layers(added_layers)

    def _get_or_create_gpx_group(self):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(GPX_GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, GPX_GROUP_NAME)
        return group

    @staticmethod
    def _build_track_symbol(color):
        """A colored line with small directional chevrons placed at regular
        intervals along it (rotated to follow the line), similar to the
        directional arrows GPS/routing tools commonly overlay on a track."""
        line_symbol = QgsLineSymbol()
        line_symbol.deleteSymbolLayer(0)  # drop the default layer, build our own stack

        base = QgsSimpleLineSymbolLayer(QColor(color), TRACK_LINE_WIDTH_MM)
        base.setWidthUnit(QgsUnitTypes.RenderUnit.RenderMillimeters)
        line_symbol.appendSymbolLayer(base)

        arrow = QgsSimpleMarkerSymbolLayer(QgsSimpleMarkerSymbolLayerBase.Shape.ArrowHead, size=8)
        arrow.setSizeUnit(QgsUnitTypes.RenderUnit.RenderMillimeters)
        arrow.setColor(QColor(color))
        arrow.setStrokeColor(QColor(color))
        arrow.setStrokeWidth(1)
        arrow.setStrokeWidthUnit(QgsUnitTypes.RenderUnit.RenderMillimeters)
        arrow_symbol = QgsMarkerSymbol()
        arrow_symbol.changeSymbolLayer(0, arrow)

        marker_line = QgsMarkerLineSymbolLayer(True)  # rotate markers to follow the line
        marker_line.setPlacement(QgsMarkerLineSymbolLayer.Placement.Interval)
        marker_line.setInterval(50)
        marker_line.setIntervalUnit(QgsUnitTypes.RenderUnit.RenderMillimeters)
        marker_line.setSubSymbol(arrow_symbol)
        line_symbol.appendSymbolLayer(marker_line)

        return line_symbol

    def _select_layers_in_panel(self, layers):
        layer_tree_view = self.iface.layerTreeView()
        if layer_tree_view is None:
            return
        root = QgsProject.instance().layerTreeRoot()
        model = layer_tree_view.layerTreeModel()
        selection_model = layer_tree_view.selectionModel()
        selection_model.clearSelection()
        for layer in layers:
            node = root.findLayer(layer.id())
            if node is not None:
                index = model.node2index(node)
                selection_model.select(index, QItemSelectionModel.SelectionFlag.Select)

    def _zoom_to_layers(self, layers):
        canvas = self.iface.mapCanvas()
        dest_crs = canvas.mapSettings().destinationCrs()
        combined = QgsRectangle()
        combined.setMinimal()
        for layer in layers:
            extent = layer.extent()
            if extent.isEmpty():
                continue
            if layer.crs() != dest_crs:
                transform = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
                try:
                    extent = transform.transformBoundingBox(extent)
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Could not transform extent of {layer.name()}: {e}",
                        "GPX Combiner", Qgis.MessageLevel.Warning)
                    extent = QgsRectangle()
            if extent.isEmpty():
                continue
            combined.combineExtentWith(extent)
        if combined.isEmpty():
            return
        combined.scale(1.1)  # a little padding around the tracks
        canvas.setExtent(combined)
        canvas.refresh()

    # -- combine --
    def combine_and_save(self):
        if len(self.files) < 2:
            QMessageBox.warning(self, "GPX Combiner", "Add at least two GPX files to combine.")
            return

        include = {k: cb.isChecked() for k, cb in self.field_checks.items()}
        try:
            combined, skipped = combine_gpx_files(self.files, include)
        except ValueError as e:
            QMessageBox.critical(self, "GPX Combiner", str(e))
            return
        if skipped:
            QMessageBox.warning(self, "GPX Combiner", "No <trkseg> found in:\n" + "\n".join(skipped))

        default_save_dir = QgsSettings().value(SETTINGS_LAST_SAVE_DIR, "")
        if not default_save_dir or not os.path.isdir(default_save_dir):
            default_save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        save_path, _ = QFileDialog.getSaveFileName(self, "Save combined file",
                                                    os.path.join(default_save_dir, "combined.gpx"),
                                                    "GPX files (*.gpx)")
        self._bring_to_front()
        if not save_path:
            return
        QgsSettings().setValue(SETTINGS_LAST_SAVE_DIR, os.path.dirname(save_path))
        try:
            with open(save_path, "w", encoding="utf-8") as fh:
                fh.write(combined)
        except OSError as e:
            QMessageBox.critical(self, "GPX Combiner", f"Could not save the file:\n{e}")
            return
        QMessageBox.information(self, "GPX Combiner", f"Combined file saved:\n{save_path}")

    def upload_to_strava(self):
        from .strava_dialog import (
            StravaCredentialsDialog, UploadOptionsDialog, UploadProgressDialog,
            _load_strava_config, _save_strava_config,
        )
        from strava_client import set_gpx_type
        import uuid
        import tempfile

        if len(self.files) < 2:
            QMessageBox.warning(self, "GPX Combiner", "Add at least two GPX files to combine.")
            return

        # Duplicate-avoidance: files downloaded via this plugin's own Strava
        # import are named "<name>_<activity_id>.gpx" — pull those IDs back
        # out so we can offer to open the originals for review/deletion
        # first (Strava's own upload dedupe would otherwise reject a
        # re-upload of the same recording, and there's no delete endpoint).
        original_ids = []
        for f in self.files:
            m = re.search(r"_(\d+)\.gpx$", os.path.basename(f["path"]))
            if m:
                original_ids.append(m.group(1))

        if original_ids:
            reply = QMessageBox.question(
                self, "Open the original activities?",
                f"{len(original_ids)} of the combined files come from existing Strava activities. "
                "To avoid a duplicate-activity error, you may want to delete them on Strava before "
                "uploading (recoverable for 30 days if you change your mind). Open each one in a "
                "new browser tab?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                for aid in original_ids:
                    webbrowser.open_new_tab(f"https://www.strava.com/activities/{aid}/overview")

        client = StravaClient(_load_strava_config())
        if not client.has_credentials:
            dlg = StravaCredentialsDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result is None:
                return
            client.config["client_id"], client.config["client_secret"] = dlg.result
            _save_strava_config(client.config)
        if not client.has_token:
            try:
                client.authorize_interactive()
            except StravaAuthError as e:
                QMessageBox.critical(self, "GPX Combiner", f"Strava authorization failed:\n{e}")
                return
            _save_strava_config(client.config)

        # Build fresh from whatever's currently loaded — never reuses a
        # previously-saved file, so there's no way for this to upload
        # stale/unrelated content from an earlier combine.
        include = {k: cb.isChecked() for k, cb in self.field_checks.items()}
        try:
            combined_content, skipped = combine_gpx_files(self.files, include)
        except ValueError as e:
            QMessageBox.critical(self, "GPX Combiner", str(e))
            return
        if skipped:
            QMessageBox.warning(self, "GPX Combiner", "No <trkseg> found in:\n" + "\n".join(skipped))

        default_type = read_gpx_type(combined_content)
        default_name = " + ".join(os.path.splitext(os.path.basename(f["path"]))[0] for f in self.files)

        dlg = UploadOptionsDialog(self, default_name, default_type)
        self._bring_to_front()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, gpx_type = dlg.name, dlg.gpx_type

        if gpx_type and gpx_type != default_type:
            combined_content = set_gpx_type(combined_content, gpx_type)

        temp_dir = os.path.join(tempfile.gettempdir(), "gpx_combiner_strava")
        os.makedirs(temp_dir, exist_ok=True)
        upload_path = os.path.join(temp_dir, f"upload_{uuid.uuid4().hex}.gpx")
        try:
            with open(upload_path, "w", encoding="utf-8") as fh:
                fh.write(combined_content)
        except OSError as e:
            QMessageBox.critical(self, "GPX Combiner", str(e))
            return

        dlg = UploadProgressDialog(self, client, upload_path, name)
        dlg.exec()
        _save_strava_config(client.config)
