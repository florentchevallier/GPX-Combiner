import os
import sys

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon

# Make core/ (gpx_core.py etc.) importable. QGIS only ever sees this
# qgis-plugin/ folder in isolation — once it's copied or symlinked into a
# QGIS profile's plugins/ folder, anything outside it (like a sibling
# ../core/ in the repo) is no longer reachable. So look in two places:
#   1. qgis-plugin/core/ — a "core" folder living right inside this plugin
#      folder. This is what a distributed plugin .zip should contain
#      (core/*.py copied in at packaging time), and it's also what
#      `ln -s ../core core` (run from qgis-plugin/) resolves to for local
#      development, keeping /core/ at the repo root as the single source
#      of truth.
#   2. ../core/ — a sibling of this plugin folder. Only relevant if you've
#      copied/symlinked the whole qgis-plugin/ folder somewhere that still
#      has the repo's top-level core/ folder next to it.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
for _candidate in (
    os.path.join(PLUGIN_DIR, "core"),
    os.path.normpath(os.path.join(PLUGIN_DIR, "..", "core")),
):
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
        break


class GpxCombinerPlugin:
    """Registered with QGIS via classFactory() in __init__.py."""

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(PLUGIN_DIR, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, "GPX Combiner", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&GPX Combiner", self.action)

    def unload(self):
        self.iface.removePluginMenu("&GPX Combiner", self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        # Imported here rather than at module load, so a problem in the
        # dialog doesn't prevent the plugin itself from loading in QGIS.
        from .gpx_combiner_dialog import GpxCombinerDialog
        if self.dialog is None:
            self.dialog = GpxCombinerDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
