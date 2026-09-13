"""
py2app build script for GPX Combiner.

Usage (run from the same folder as gpx_combiner.py):

    # Fast "alias" build for testing — symlinks to your source files,
    # rebuilds instantly when you edit gpx_combiner.py. Use this first.
    python3 setup.py py2app -A

    # Real standalone build — copies everything needed into a self-contained
    # .app, ready to share/move to another Mac. Slower, do this once you've
    # confirmed the alias build works.
    python3 setup.py py2app

Either way, the .app ends up in dist/GPX Combiner.app
"""

from setuptools import setup

APP = ["gpx_combiner.py"]
DATA_FILES = []

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "icon.icns",
    "plist": {
        "CFBundleName": "GPX Combiner",
        "CFBundleDisplayName": "GPX Combiner",
        "CFBundleShortVersionString": "3.5",
        "CFBundleVersion": "3.5",
        "NSHumanReadableCopyright": "",
        # Without an Apple Developer certificate + notarization, macOS
        # Gatekeeper will warn on first launch (right-click > Open bypasses
        # this once). This is expected for an unsigned personal build.
    },
    # IMPORTANT: list whole packages here (not just "includes"), so py2app
    # copies their non-Python resource files too — this matters especially
    # for tkinterdnd2, whose bundled Tcl "tkdnd" extension files would
    # otherwise be silently left out and drag-and-drop would fail in the
    # packaged app even though it works when running the .py directly.
    "packages": ["tkinter"],
}

# Only ask py2app to bundle tkinterdnd2 if it's actually installed —
# otherwise the build fails outright instead of just disabling drag-and-drop
# the way the app itself does at runtime.
try:
    import tkinterdnd2  # noqa: F401
    OPTIONS["packages"].append("tkinterdnd2")
except ImportError:
    print("Note: tkinterdnd2 not installed — building without drag-and-drop support.")

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
