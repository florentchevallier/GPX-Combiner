"""QGIS entry point — this file (and this exact function name) is what QGIS
looks for when loading a plugin."""


def classFactory(iface):
    from .gpx_combiner_plugin import GpxCombinerPlugin
    return GpxCombinerPlugin(iface)
