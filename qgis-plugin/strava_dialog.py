"""
strava_dialog.py — Strava import window for the QGIS plugin: credentials
entry, OAuth authorization, listing the most recent activities, and
downloading the selected ones as GPX files handed back to the main
GPX Combiner dialog.

Scope of this first version (deliberately not full parity with the desktop
app's Strava window yet — that one grew incrementally over many rounds too):
- Shows the most recent activities, "Load more" instead of full pagination
  with a page-size choice and a date-range filter.
- No reverse-geocoded "city" column.
- No dedicated Strava settings/disconnect window — if you ever need to
  clear saved credentials, run this in the QGIS Python console:
      from qgis.core import QgsSettings; QgsSettings().remove("gpx_combiner/strava")
These can be added later the same way the desktop app's Strava import grew.
"""

import os
import re
import time
import tempfile
import webbrowser

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QLineEdit, QFormLayout, QComboBox,
)
from qgis.PyQt.QtCore import Qt, QObject, QThread, pyqtSignal
from qgis.core import QgsSettings

from strava_client import (
    StravaClient, StravaAuthError, StravaAPIError,
    build_gpx_from_activity, sanitize_filename, GPX_TYPE_CHOICES,
)

# Downloaded activities land here silently — no "choose a folder" prompt.
# This is just an intermediate landing spot (the files get added straight to
# the main dialog's list right after), matching how the desktop app quietly
# drops Strava downloads into a GPX-temp folder next to the script rather
# than asking each time.
STRAVA_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "gpx_combiner_strava")

SETTINGS_STRAVA_PREFIX = "gpx_combiner/strava"

# Strips characters that break single-line label/list-item rendering:
#   - C0 control chars + LINE/PARAGRAPH SEPARATOR (U+2028/29): invisible line
#     breaks that can split one row's text across several tiny sublines.
#   - Variation selectors (U+FE00-FE0F): the invisible modifier that forces
#     color/emoji presentation of the preceding character (e.g. the U+FE0F
#     in ☀️/☁️/🌧️). Narrowed to just this (2026-09-24) after an earlier,
#     broader version of this filter (stripping whole emoji/symbol blocks)
#     turned out to remove plenty of emoji that actually rendered fine —
#     the variation selector specifically is the more likely real culprit
#     for garbled rows, a known troublemaker in some Qt/font combinations.
#     Base emoji/symbol codepoints are otherwise left alone.
# Ordinary characters — pipes, quotes, accented letters — are left untouched.
_DISPLAY_STRIP_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\ufe00-\ufe0f]")


def _clean_display_text(text):
    return _DISPLAY_STRIP_RE.sub("", text or "")


def _load_strava_config():
    settings = QgsSettings()
    config = {}
    for key in ("client_id", "client_secret", "refresh_token", "access_token"):
        value = settings.value(f"{SETTINGS_STRAVA_PREFIX}/{key}", None)
        if value:
            config[key] = value
    expires_at = settings.value(f"{SETTINGS_STRAVA_PREFIX}/expires_at", None)
    if expires_at is not None:
        try:
            config["expires_at"] = int(expires_at)
        except (TypeError, ValueError):
            pass
    return config


def _save_strava_config(config):
    settings = QgsSettings()
    for key, value in config.items():
        settings.setValue(f"{SETTINGS_STRAVA_PREFIX}/{key}", value)


class StravaCredentialsDialog(QDialog):
    """Modal: asks for Client ID + Client Secret, with an explanation of
    where to find them. Sets .result to (client_id, client_secret) on OK,
    leaves it None if cancelled."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result = None
        self.setWindowTitle("Connect to Strava")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        info = QLabel(
            'To connect Strava, create an application at '
            '<a href="https://www.strava.com/settings/api">strava.com/settings/api</a> '
            '(free), then set its "Authorization Callback Domain" to: <b>localhost</b>'
            "<br><br>The Client ID and Client Secret are then shown on your "
            "application's page."
        )
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Client ID:", self.id_edit)
        form.addRow("Client Secret:", self.secret_edit)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _on_ok(self):
        cid = self.id_edit.text().strip()
        secret = self.secret_edit.text().strip()
        if not cid or not secret:
            QMessageBox.warning(self, "GPX Combiner", "Please fill in both the Client ID and Client Secret.")
            return
        self.result = (cid, secret)
        self.accept()


class StravaSettingsDialog(QDialog):
    """Shows the currently saved Strava account (if any) and lets the user
    disconnect — erasing the saved Client ID/Secret and tokens from
    QgsSettings — so a revoked/invalid token (e.g. after removing the
    app's access from Strava's own site) doesn't leave the plugin stuck
    with no way back to a fresh connection."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Strava settings")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect…")
        self.connect_btn.clicked.connect(self._connect)
        btn_row.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect and erase credentials")
        self.disconnect_btn.clicked.connect(self._disconnect)
        btn_row.addWidget(self.disconnect_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._refresh_status()

    def _refresh_status(self):
        config = _load_strava_config()
        if not config.get("client_id"):
            self.status_label.setText("No Strava credentials saved on this computer.")
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            return

        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        lines = [f"Client ID: {config.get('client_id')}"]
        if config.get("refresh_token"):
            name = self._fetch_athlete_name(config)
            lines.append(f"Connected as: {name}" if name
                         else "Authorized, but the saved token appears to be invalid "
                              "(e.g. access was removed on Strava's side) — disconnect and "
                              "reconnect to fix this.")
        else:
            lines.append("Credentials saved, but not yet authorized.")
        self.status_label.setText("\n".join(lines))

    def _fetch_athlete_name(self, config):
        try:
            client = StravaClient(dict(config))
            data = client.get_athlete()
        except Exception:
            return None
        name = f"{data.get('firstname', '')} {data.get('lastname', '')}".strip()
        return name or None

    def _connect(self):
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
        self._refresh_status()

    def _disconnect(self):
        reply = QMessageBox.question(
            self, "Confirm disconnect",
            "This will delete the Client ID, Client Secret, and access tokens saved on this "
            "computer. You'll need to re-enter them to reconnect Strava. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        settings = QgsSettings()
        settings.remove(SETTINGS_STRAVA_PREFIX)
        QMessageBox.information(self, "GPX Combiner", "Strava credentials have been erased from this computer.")
        self._refresh_status()


class StravaImportDialog(QDialog):
    """Lists recent Strava activities with checkboxes; downloads the
    selected ones as GPX files and hands their paths to on_downloaded(paths)
    — the main dialog passes its own add-to-list method in for that."""

    def __init__(self, parent, on_downloaded):
        super().__init__(parent)
        self.on_downloaded = on_downloaded
        self.setWindowTitle("Import from Strava")
        self.resize(600, 460)

        self.client = None
        self.page = 1
        self.per_page = 10
        self.activity_cache = {}
        self._gear_cache = {}  # gear_id -> name, shared across pages

        layout = QVBoxLayout(self)
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        self.list_widget = QListWidget()
        self.list_widget.itemChanged.connect(self._update_download_label)
        layout.addWidget(self.list_widget)

        nav_row = QHBoxLayout()
        self.more_btn = QPushButton("Load more")
        self.more_btn.clicked.connect(self.load_more)
        self.more_btn.setEnabled(False)
        nav_row.addWidget(self.more_btn)
        nav_row.addStretch()
        layout.addLayout(nav_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        self.download_btn = QPushButton("Download selected (0)")
        self.download_btn.clicked.connect(self.download_selected)
        btn_row.addWidget(self.download_btn)
        layout.addLayout(btn_row)

        self._ensure_client_and_load()

    # -- setup / auth --
    def _ensure_client_and_load(self):
        client = StravaClient(_load_strava_config())
        if not client.has_credentials:
            dlg = StravaCredentialsDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result is None:
                self.reject()
                return
            client.config["client_id"], client.config["client_secret"] = dlg.result
            _save_strava_config(client.config)

        self.client = client

        if not self.client.has_token:
            self.status_label.setText("Opening your browser to authorize access to Strava…")
            try:
                self.client.authorize_interactive()
            except StravaAuthError as e:
                QMessageBox.critical(
                    self, "GPX Combiner",
                    f"Strava authorization failed:\n{e}\n\n"
                    'Check that your Strava application\'s "Authorization Callback '
                    'Domain" is set to: localhost')
                self.reject()
                return
            _save_strava_config(self.client.config)

        self.load_page()

    # -- activity listing --
    def load_more(self):
        self.page += 1
        self.load_page(append=True)

    def load_page(self, append=False):
        self.status_label.setText("Loading…")
        try:
            activities = self.client.list_activities(self.page, per_page=self.per_page)
        except (StravaAPIError, StravaAuthError) as e:
            QMessageBox.critical(self, "GPX Combiner", f"Strava API error:\n{e}")
            self.status_label.setText("")
            return
        finally:
            _save_strava_config(self.client.config)  # the access token may have just refreshed

        if not append:
            self.list_widget.clear()

        for a in activities:
            self.activity_cache[a["id"]] = a
            dist_km = (a.get("distance") or 0) / 1000
            dur_s = a.get("moving_time") or 0
            dur_str = f"{dur_s // 3600:02d}:{(dur_s % 3600) // 60:02d}:{dur_s % 60:02d}"
            date_str = (a.get("start_date_local") or a.get("start_date") or "")[:10]
            gear_name = self._resolve_gear(a.get("gear_id"))
            activity_name = _clean_display_text(a.get("name", ""))
            label = f"{date_str}  —  {activity_name}  ({a.get('type', '')}, {dist_km:.2f} km, {dur_str})"
            if gear_name:
                label += f"  [{gear_name}]"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, a["id"])
            self.list_widget.addItem(item)

        self.more_btn.setEnabled(len(activities) == self.per_page)
        self.status_label.setText("" if self.list_widget.count() else "No activities found.")

    def _resolve_gear(self, gear_id):
        """Best-effort gear name lookup, cached by gear_id. Fetched
        synchronously (simpler than the desktop app's background-thread
        version) — only the very first activity using a given piece of gear
        in this session causes a brief pause; every repeat is instant from
        cache. Silently falls back to the raw gear_id (or nothing) on any
        API error, e.g. if the current token's scope doesn't cover it."""
        if not gear_id:
            return None
        if gear_id in self._gear_cache:
            return self._gear_cache[gear_id]
        try:
            name = self.client.get_gear(gear_id).get("name") or gear_id
        except (StravaAPIError, StravaAuthError):
            name = gear_id
        self._gear_cache[gear_id] = name
        return name

    def _update_download_label(self, *_):
        count = sum(1 for i in range(self.list_widget.count())
                    if self.list_widget.item(i).checkState() == Qt.CheckState.Checked)
        self.download_btn.setText(f"Download selected ({count})")

    # -- download --
    def download_selected(self):
        selected_ids = [self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
                         for i in range(self.list_widget.count())
                         if self.list_widget.item(i).checkState() == Qt.CheckState.Checked]
        if not selected_ids:
            return

        try:
            os.makedirs(STRAVA_DOWNLOAD_DIR, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "GPX Combiner", f"Could not create the download folder:\n{e}")
            return
        save_dir = STRAVA_DOWNLOAD_DIR

        saved_paths = []
        for aid in selected_ids:
            activity = self.activity_cache.get(aid)
            if not activity:
                continue
            try:
                streams = self.client.get_streams(aid)
            except (StravaAPIError, StravaAuthError) as e:
                QMessageBox.warning(self, "GPX Combiner",
                                     f"Strava API error for \u201c{activity.get('name', aid)}\u201d:\n{e}")
                continue

            gpx_text = build_gpx_from_activity(activity, streams)
            if gpx_text is None:
                QMessageBox.warning(self, "GPX Combiner",
                                     f"Could not get the track for \u201c{activity.get('name', aid)}\u201d "
                                     "(activity without GPS data?).")
                continue

            fname = f"{sanitize_filename(activity.get('name', str(aid)))}_{aid}.gpx"
            fpath = os.path.join(save_dir, fname)
            try:
                with open(fpath, "w", encoding="utf-8") as fh:
                    fh.write(gpx_text)
            except OSError as e:
                QMessageBox.warning(self, "GPX Combiner", f"Could not save {fname}:\n{e}")
                continue
            saved_paths.append(fpath)

        _save_strava_config(self.client.config)

        if saved_paths:
            self.on_downloaded(saved_paths)
            self.accept()


class UploadOptionsDialog(QDialog):
    """Asks for the activity name and its Strava type together, the type
    pre-selected from whatever the combined GPX's own <type> tag already
    says. Read .name / .gpx_type after exec() == QDialog.DialogCode.Accepted."""

    def __init__(self, parent, default_name, default_type):
        super().__init__(parent)
        self.setWindowTitle("Activity name")
        self.setMinimumWidth(360)
        self.name = None
        self.gpx_type = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(default_name)
        form.addRow("Name to give this activity on Strava:", self.name_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(GPX_TYPE_CHOICES)
        if default_type in GPX_TYPE_CHOICES:
            self.type_combo.setCurrentText(default_type)
        form.addRow("Activity type:", self.type_combo)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _on_ok(self):
        self.name = self.name_edit.text().strip()
        self.gpx_type = self.type_combo.currentText()
        self.accept()


class _UploadWorker(QObject):
    """Runs upload_gpx() + the check_upload() polling loop on a background
    QThread. Uses Qt signals (not direct widget calls) to report back, since
    those are the thread-safe way to reach UI code living on the main
    thread."""
    progress = pyqtSignal(str)
    success = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, client, filepath, name):
        super().__init__()
        self.client = client
        self.filepath = filepath
        self.name = name

    def run(self):
        try:
            upload_id = self.client.upload_gpx(self.filepath, name=self.name)
        except (StravaAPIError, StravaAuthError, OSError) as e:
            self.error.emit(str(e))
            return

        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            try:
                status = self.client.check_upload(upload_id)
            except (StravaAPIError, StravaAuthError) as e:
                self.error.emit(str(e))
                return
            if status.get("error"):
                self.error.emit(status["error"])
                return
            if status.get("activity_id"):
                self.success.emit(str(status["activity_id"]))
                return
            self.progress.emit(status.get("status", ""))

        self.error.emit("Strava is taking unusually long to process this — try again later.")


class UploadProgressDialog(QDialog):
    """Shown while a combined GPX is being uploaded to Strava; updates live
    as the background worker reports progress, the final activity, or an
    error (e.g. Strava detecting a duplicate)."""

    def __init__(self, parent, client, filepath, name):
        super().__init__(parent)
        self.setWindowTitle("Uploading to Strava")
        self.setMinimumWidth(360)
        self._activity_id = None

        layout = QVBoxLayout(self)
        self.status_label = QLabel("Uploading the file…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        self.view_btn = QPushButton("View on Strava")
        self.view_btn.clicked.connect(self._open_activity)
        self.view_btn.hide()
        btn_row.addWidget(self.view_btn)
        btn_row.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        self.close_btn.setEnabled(False)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

        self._thread = QThread(self)
        self._worker = _UploadWorker(client, filepath, name)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.success.connect(self._on_success)
        self._worker.error.connect(self._on_error)
        self._worker.success.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, text):
        self.status_label.setText(text or "Strava is processing it…")

    def _on_success(self, activity_id):
        self._activity_id = activity_id
        self.status_label.setText("Activity created successfully.")
        self.view_btn.show()
        self.close_btn.setEnabled(True)

    def _on_error(self, err):
        self.status_label.setText(f"Upload failed:\n{err}")
        self.close_btn.setEnabled(True)

    def _open_activity(self):
        if self._activity_id:
            webbrowser.open_new_tab(f"https://www.strava.com/activities/{self._activity_id}/overview")

    def closeEvent(self, event):
        # Ignore the window's own close button while the upload is still
        # running — close_btn (the one way to close cleanly) stays disabled
        # until the worker reports success or an error.
        if self.close_btn.isEnabled():
            event.accept()
        else:
            event.ignore()
