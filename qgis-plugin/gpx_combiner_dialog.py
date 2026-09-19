import os

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
)

# From ../core/gpx_core.py — see the sys.path setup in gpx_combiner_plugin.py.
from gpx_core import (
    extract_sort_key, detect_extension_fields, combine_gpx_files,
    EXTENSION_FIELDS, EXTENSION_FIELD_LABELS, TRACK_COLOR_PALETTE,
)

OSM_XYZ_NAME = "OpenStreetMap"
OSM_XYZ_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TRACK_LINE_WIDTH_MM = 1.6
GPX_GROUP_NAME = "GPX Combiner"
SETTINGS_LAST_OPEN_DIR = "gpx_combiner/last_open_dir"
SETTINGS_LAST_SAVE_DIR = "gpx_combiner/last_save_dir"


class GpxCombinerDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("GPX Combiner")
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
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("Detected chronological order (oldest to newest):"))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.list_widget)

        # Delete/Backspace removes the selected file(s) from this list —
        # never touches anything on disk, just the plugin's working set.
        for key in (Qt.Key_Delete, Qt.Key_Backspace):
            shortcut = QShortcut(QKeySequence(key), self.list_widget)
            shortcut.setContext(Qt.WidgetShortcut)
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
            badges = "  ".join(EXTENSION_FIELD_LABELS[k] for k in EXTENSION_FIELDS if ext.get(k))
            label = f"{i}. {f['sort_key']}  —  {os.path.basename(f['path'])}"
            if badges:
                label += f"   |   {badges}"
            self.list_widget.addItem(label)

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
        dlg.exec_()
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
        base.setWidthUnit(QgsUnitTypes.RenderMillimeters)
        line_symbol.appendSymbolLayer(base)

        arrow = QgsSimpleMarkerSymbolLayer(QgsSimpleMarkerSymbolLayerBase.ArrowHead, size=8)
        arrow.setSizeUnit(QgsUnitTypes.RenderMillimeters)
        arrow.setColor(QColor(color))
        arrow.setStrokeColor(QColor(color))
        arrow.setStrokeWidth(1)
        arrow.setStrokeWidthUnit(QgsUnitTypes.RenderMillimeters)
        arrow_symbol = QgsMarkerSymbol()
        arrow_symbol.changeSymbolLayer(0, arrow)

        marker_line = QgsMarkerLineSymbolLayer(True)  # rotate markers to follow the line
        marker_line.setPlacement(QgsMarkerLineSymbolLayer.Interval)
        marker_line.setInterval(50)
        marker_line.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
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
                selection_model.select(index, QItemSelectionModel.Select)

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
                except Exception:
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
