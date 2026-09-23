"""
py2app build script for GPX Combiner.

Usage (run from the same folder as gpx_combiner.py):

    # Fast "alias" build for testing — symlinks to your source files,
    # rebuilds instantly when you edit gpx_combiner.py. Use this first.
    python3 py2app_setup.py py2app -A

    # Real standalone build — copies everything needed into a self-contained
    # .app, ready to share/move to another Mac. Slower, do this once you've
    # confirmed the alias build works.
    python3 py2app_setup.py py2app

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
        "CFBundleShortVersionString": "3.7.4",
        "CFBundleVersion": "3.7.4",
        "NSHumanReadableCopyright": "",
        # Without an Apple Developer certificate + notarization, macOS
        # Gatekeeper will warn on first launch (right-click > Open bypasses
        # this once). This is expected for an unsigned personal build.
    },
    # IMPORTANT: list whole packages here (not just "includes"), so py2app
    # copies their non-Python resource files too — this matters especially
    # for tkinterdnd2 (its bundled Tcl "tkdnd" extension files) and certifi
    # (its cacert.pem CA bundle) — both would otherwise be silently left out,
    # breaking drag-and-drop / HTTPS calls in the packaged app even though
    # they work fine when running the .py directly.
    "packages": ["tkinter"],
}

# Strongly recommended: certifi provides a CA bundle the app can use for
# HTTPS regardless of what certificates (if any) are set up on the machine
# it runs on — without it, a packaged build is prone to
# SSLCertVerificationError on Strava/API calls even when the exact same code
# works fine when run from source with a normal Python install.
try:
    import certifi  # noqa: F401
    OPTIONS["packages"].append("certifi")
except ImportError:
    print("WARNING: certifi not installed (pip3 install certifi) — the packaged app "
          "may fail HTTPS requests with a certificate error on some machines.")

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
