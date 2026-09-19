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
import tempfile

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QLineEdit, QFormLayout,
)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsSettings

from strava_client import (
    StravaClient, StravaAuthError, StravaAPIError,
    build_gpx_from_activity, sanitize_filename,
)

# Downloaded activities land here silently — no "choose a folder" prompt.
# This is just an intermediate landing spot (the files get added straight to
# the main dialog's list right after), matching how the desktop app quietly
# drops Strava downloads into a GPX-temp folder next to the script rather
# than asking each time.
STRAVA_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "gpx_combiner_strava")

SETTINGS_STRAVA_PREFIX = "gpx_combiner/strava"


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
        self.secret_edit.setEchoMode(QLineEdit.Password)
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
            if dlg.exec_() != QDialog.Accepted or dlg.result is None:
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
            label = f"{date_str}  —  {a.get('name', '')}  ({a.get('type', '')}, {dist_km:.2f} km, {dur_str})"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, a["id"])
            self.list_widget.addItem(item)

        self.more_btn.setEnabled(len(activities) == self.per_page)
        self.status_label.setText("" if self.list_widget.count() else "No activities found.")

    def _update_download_label(self, *_):
        count = sum(1 for i in range(self.list_widget.count())
                    if self.list_widget.item(i).checkState() == Qt.Checked)
        self.download_btn.setText(f"Download selected ({count})")

    # -- download --
    def download_selected(self):
        selected_ids = [self.list_widget.item(i).data(Qt.UserRole)
                         for i in range(self.list_widget.count())
                         if self.list_widget.item(i).checkState() == Qt.Checked]
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
