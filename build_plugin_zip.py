#!/usr/bin/env python3
"""
build_plugin_zip.py — packages qgis-plugin/ into an installable .zip.

Run from the repo root:

    python3 build_plugin_zip.py

What it does (and why):
  1. Stages a clean copy in build/gpx_combiner/ — the folder is named
     "gpx_combiner" (not "qgis-plugin") because QGIS imports a plugin by its
     folder name, and that name must be a valid Python identifier (no
     hyphens) — this must be the top-level folder name inside the zip too.
  2. Copies core/*.py into build/gpx_combiner/core/ as real files (not the
     symlink used for local dev) — a zip can't carry a symlink that points
     at your machine, so the shared core modules have to be physically
     inside the plugin folder for a distributed build.
  3. Skips dev cruft (__pycache__, .pyc, .DS_Store, .git) so none of it
     ends up in the zip.
  4. Zips build/gpx_combiner/ into dist/gpx_combiner-<version>.zip, with
     "gpx_combiner/" as the top-level path inside the archive (required by
     QGIS's "Install from ZIP").

dist/ is used for consistency with the desktop app's py2app/PyInstaller
builds, which already land there and are already in .gitignore alongside
build/.
"""

import os
import re
import shutil
import zipfile

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
QGIS_PLUGIN_DIR = os.path.join(REPO_ROOT, "qgis-plugin")
CORE_DIR = os.path.join(REPO_ROOT, "core")
BUILD_DIR = os.path.join(REPO_ROOT, "build")
DIST_DIR = os.path.join(REPO_ROOT, "dist")

PLUGIN_FOLDER_NAME = "gpx_combiner"  # must be a valid Python identifier

# Anything matching these is left out of the staged copy.
SKIP_NAMES = {"__pycache__", ".DS_Store", ".git", ".gitignore"}
SKIP_SUFFIXES = (".pyc", ".pyo")


def _copytree_clean(src, dst):
    """shutil.copytree, but skipping dev cruft (see SKIP_* above)."""
    def _ignore(dir_path, names):
        return [n for n in names
                if n in SKIP_NAMES or n.endswith(SKIP_SUFFIXES)]
    shutil.copytree(src, dst, ignore=_ignore)


def read_version(metadata_path):
    with open(metadata_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"version\s*=\s*(.+)", line.strip())
            if m:
                return m.group(1).strip()
    return "0.0.0"


def main():
    if not os.path.isdir(QGIS_PLUGIN_DIR):
        raise SystemExit(f"Expected to find {QGIS_PLUGIN_DIR} — run this from the repo root.")
    if not os.path.isdir(CORE_DIR):
        raise SystemExit(f"Expected to find {CORE_DIR} — run this from the repo root.")

    stage_dir = os.path.join(BUILD_DIR, PLUGIN_FOLDER_NAME)
    if os.path.exists(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(BUILD_DIR, exist_ok=True)

    # 1. Stage the plugin's own files (following any symlink used for local dev).
    _copytree_clean(QGIS_PLUGIN_DIR, stage_dir)
    # The dev "core" symlink inside qgis-plugin/, if present, gets copied by
    # copytree as a symlink by default — replace it with a real copy.
    staged_core_link = os.path.join(stage_dir, "core")
    if os.path.islink(staged_core_link) or os.path.isdir(staged_core_link):
        if os.path.islink(staged_core_link):
            os.remove(staged_core_link)
        elif os.path.isdir(staged_core_link):
            shutil.rmtree(staged_core_link)

    # 2. Copy the real core/ in as physical files.
    _copytree_clean(CORE_DIR, staged_core_link)

    version = read_version(os.path.join(stage_dir, "metadata.txt"))
    os.makedirs(DIST_DIR, exist_ok=True)
    zip_path = os.path.join(DIST_DIR, f"{PLUGIN_FOLDER_NAME}-{version}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    # 3. Zip it with "gpx_combiner/..." as the path inside the archive.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(stage_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_NAMES]
            for name in files:
                if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                    continue
                file_path = os.path.join(root, name)
                arcname = os.path.join(
                    PLUGIN_FOLDER_NAME,
                    os.path.relpath(file_path, stage_dir),
                )
                zf.write(file_path, arcname)

    print(f"Built: {zip_path}")
    print("Test it with QGIS's Plugins > Manage and Install Plugins > Install from ZIP.")


if __name__ == "__main__":
    main()
