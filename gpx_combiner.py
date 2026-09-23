#!/usr/bin/env python3
"""
GPX Combiner + Strava Import
==============================
A local desktop app (tkinter, standard library only — no pip install
required for the core app) that lets you:
  1. Combine several GPX files into one, in chronological order.
  2. Import activities directly from Strava (OAuth), convert them to GPX
     (rebuilt from the GPS/altitude/time streams of the Strava API, which
     doesn't offer a direct GPX export) and load them into the list to
     combine.
  3. Preview all loaded tracks on an OpenStreetMap-based map.

Optional dependency:
  - Drag-and-drop needs the small third-party "tkinterdnd2" package
    (pip3 install tkinterdnd2). The app runs fine without it — drag-and-drop
    is just disabled, the "+" button still works.

Requirements for Strava import:
  - An application created at https://developers.strava.com (Client ID +
    Client Secret).
  - That application's "Authorization Callback Domain" field must be set
    to: localhost

Run with:
    python3 gpx_combiner.py
"""

import os
import sys
import re
import json
import time
import math
import socket
import threading
import uuid
import calendar as cal_module
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
import ssl

# Use certifi's CA bundle for HTTPS verification when available. This matters
# most for a py2app-packaged build: it embeds its own isolated Python, which
# has no access to the system certificate store or to whatever
# "Install Certificates.command" set up for a python.org install — without
# this, HTTPS calls (Strava token exchange, etc.) fail with
# SSLCertVerificationError in a packaged app even though the same code works
# fine when run from source. Falls back to Python's default SSL behavior if
# certifi isn't installed (pip3 install certifi).
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = None


def urlopen(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)
from datetime import datetime, timedelta, timezone, date
from http.server import BaseHTTPRequestHandler, HTTPServer

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# Drag-and-drop is not built into stdlib Tkinter — it needs the small
# third-party "tkinterdnd2" package (pip install tkinterdnd2). The app
# degrades gracefully without it: the "+" button still works, drag-and-drop
# is just disabled.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(filename):
    """Resolve a bundled resource (e.g. icon.ico) both when running from
    source and when frozen by PyInstaller, which extracts bundled data files
    into a temp folder (sys._MEIPASS) rather than leaving them next to the
    script."""
    base = getattr(sys, "_MEIPASS", SCRIPT_DIR)
    return os.path.join(base, filename)
APP_VERSION = "3.7.4"
CONFIG_PATH = os.path.join(SCRIPT_DIR, "strava_config.json")
APP_CONFIG_PATH = os.path.join(SCRIPT_DIR, "app_config.json")  # app-wide settings (language...), kept
                                                                 # separate from Strava credentials
GPX_TEMP_DIR = os.path.join(SCRIPT_DIR, "GPX-temp")
TILE_CACHE_DIR = os.path.join(SCRIPT_DIR, "tile_cache")

REDIRECT_PORT = 8721
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/exchange_token"

TRKSEG_RE = re.compile(r"<trkseg\b.*?</trkseg>", re.S | re.I)
TIME_RE = re.compile(r"<time>(.*?)</time>", re.S | re.I)
TRKPT_TAG_RE = re.compile(r"<trkpt\b([^>]*)>")
LAT_ATTR_RE = re.compile(r'lat="(-?[0-9.]+)"')
LON_ATTR_RE = re.compile(r'lon="(-?[0-9.]+)"')

# Detection (presence check) and stripping (full-tag removal) for the four
# optional data types a GPX file's <extensions> may carry. Field order is
# used consistently everywhere these are displayed.
EXTENSION_FIELDS = ["hr", "cadence", "power", "temp"]
EXTENSION_FIELD_LABELS = {"hr": "HR", "cadence": "Cadence", "power": "Power", "temp": "Temp"}

EXT_FIELD_DETECT_RE = {
    "hr": re.compile(r"<gpxtpx:hr>", re.I),
    "cadence": re.compile(r"<gpxtpx:cad>", re.I),
    "power": re.compile(r"<(?:power|gpxpx:PowerInWatts)>", re.I),
    "temp": re.compile(r"<gpxtpx:atemp>", re.I),
}
EXT_FIELD_STRIP_RE = {
    "hr": re.compile(r"<gpxtpx:hr>.*?</gpxtpx:hr>", re.I | re.S),
    "cadence": re.compile(r"<gpxtpx:cad>.*?</gpxtpx:cad>", re.I | re.S),
    "power": re.compile(r"<power>.*?</power>|<gpxpx:PowerInWatts>.*?</gpxpx:PowerInWatts>", re.I | re.S),
    "temp": re.compile(r"<gpxtpx:atemp>.*?</gpxtpx:atemp>", re.I | re.S),
}
EMPTY_TPX_RE = re.compile(r"<gpxtpx:TrackPointExtension>\s*</gpxtpx:TrackPointExtension>", re.I)
EMPTY_EXT_RE = re.compile(r"<extensions>\s*</extensions>", re.I)

# Some devices (e.g. COROS, via the cluetrust "gpxdata" extension schema)
# embed a per-point CUMULATIVE distance-from-start-of-this-recording value.
# That's fine within a single original file, but becomes actively misleading
# once two separately-recorded files are spliced together (each one's
# counter restarts at 0) — unlike gpxdata:speed or gpxdata:hr, which stay
# valid per-point measurements regardless of splicing, so those are left
# alone. Always stripped when combining, not tied to the HR/cadence/power/
# temp checkboxes (which only cover the gpxtpx:/power Garmin-style schema).
GPXDATA_DISTANCE_RE = re.compile(r"<gpxdata:distance>.*?</gpxdata:distance>", re.I | re.S)

OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_TILE_SIZE = 256
OSM_MIN_ZOOM = 2
OSM_MAX_ZOOM = 18  # capped below OSM's 19 to be a considerate tile-server citizen

TRACK_COLOR_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#46b3c2", "#f032e6", "#9a8b00", "#008080", "#e6beff",
]

# ----------------------------------------------------------------------------
# Traductions
# ----------------------------------------------------------------------------

TR = {
    "fr": {
        "app_title": "GPX combiner",
        "language": "Langue",
        "add_files": "Ajouter des fichiers GPX",
        "remove_selected": "Retirer la sélection",
        "clear_all": "Tout effacer",
        "import_strava": "Importer depuis Strava",
        "order_label": "Ordre chronologique détecté (du plus ancien au plus récent) :",
        "combine_save": "Combiner et enregistrer",
        "preview_btn": "Aperçu",
        "preview_title": "Aperçu des traces",
        "fit_all": "Tout afficher",
        "reload_tracks": "🔄 Recharger",
        "no_tracks_status": "Aucune trace chargée à afficher.",
        "zoom_status": "Zoom {z}",
        "legend_title": "Légende",
        "ok_btn": "OK",
        "cancel_btn": "Annuler",
        "dnd_hint": "Astuce : vous pouvez aussi glisser-déposer des fichiers GPX ici.",
        "include_fields_label": "Inclure dans l'export :",
        "upload_btn": "Envoyer sur Strava…",
        "upload_no_file_body": "Combine d'abord des fichiers pour obtenir un GPX à envoyer.",
        "open_originals_title": "Ouvrir les activités d'origine ?",
        "open_originals_body": "{n} des fichiers combinés proviennent d'activités Strava déjà existantes. "
                                "Pour éviter une erreur de doublon, tu peux les supprimer sur Strava avant "
                                "l'envoi (récupérables pendant 30 jours en cas d'erreur). Ouvrir chacune dans "
                                "un nouvel onglet du navigateur ?",
        "upload_name_title": "Nom de l'activité",
        "upload_name_prompt": "Nom à donner à l'activité sur Strava :",
        "upload_type_prompt": "Type d'activité :",
        "upload_progress_title": "Envoi vers Strava",
        "upload_status_uploading": "Envoi du fichier…",
        "upload_status_processing": "Traitement par Strava…",
        "upload_status_done": "Activité créée avec succès.",
        "upload_status_error": "Échec de l'envoi :\n{err}",
        "upload_status_timeout": "Strava met anormalement longtemps à traiter le fichier — réessaie plus tard.",
        "upload_view_on_strava": "Voir sur Strava",
        "strava_settings_btn": "⚙ Réglages Strava",
        "strava_settings_title": "Réglages Strava",
        "strava_not_configured": "Aucun identifiant Strava enregistré sur cet ordinateur.",
        "strava_client_id_label": "Client ID : {id}",
        "strava_connected_as": "Connecté en tant que : {name}",
        "strava_authorized_unknown": "Autorisé (nom du compte indisponible pour le moment).",
        "strava_not_authorized": "Identifiants enregistrés, mais autorisation pas encore effectuée.",
        "connect_strava": "Connecter…",
        "disconnect_strava": "Déconnecter et effacer les identifiants",
        "confirm_disconnect_title": "Confirmer la déconnexion",
        "confirm_disconnect_body": "Cela supprime le Client ID, le Client Secret et les jetons d'accès "
                                    "enregistrés sur cet ordinateur. Il faudra les ressaisir pour reconnecter "
                                    "Strava. Continuer ?",
        "disconnected_title": "Déconnecté",
        "disconnected_body": "Les identifiants Strava ont été effacés de cet ordinateur.",
        "strava_credentials_title": "Connexion à Strava",
        "strava_credentials_info": "Pour connecter Strava, crée une application sur developers.strava.com "
                                    "(gratuit), puis règle son « Authorization Callback Domain » sur : localhost\n\n"
                                    "Le Client ID et le Client Secret se trouvent ensuite sur la page de ton "
                                    "application (« Mon application API »).",
        "strava_credentials_missing": "Merci de renseigner le Client ID et le Client Secret.",
        "open_strava_dev_site": "Ouvrir developers.strava.com",
        "show_secret": "👁",
        "hide_secret": "🙈",
        "status_none": "Aucun fichier chargé.",
        "status_one": "1 fichier chargé — ajoutez-en au moins un second pour combiner.",
        "status_multiple": "{n} fichiers chargés, prêts à être combinés.",
        "unknown_time": "(horodatage inconnu)",
        "err_read_title": "Erreur de lecture",
        "err_read_body": "Impossible de lire {path} :\n{err}",
        "warn_notenough_title": "Pas assez de fichiers",
        "warn_notenough_body": "Ajoutez au moins deux fichiers GPX à combiner.",
        "err_base_title": "Fichier de base invalide",
        "err_base_body": "Le fichier le plus ancien ({path}) ne contient pas de balise </trkseg>.",
        "warn_skipped_title": "Fichiers ignorés",
        "warn_skipped_body": "Aucune balise <trkseg> trouvée dans :\n{files}",
        "err_nothing_title": "Rien à combiner",
        "err_nothing_body": "Aucun segment n'a pu être extrait des autres fichiers.",
        "save_title": "Enregistrer le fichier combiné",
        "err_write_title": "Erreur d'écriture",
        "err_write_body": "Impossible d'enregistrer le fichier :\n{err}",
        "done_title": "Terminé",
        "done_body": "Fichier combiné enregistré :\n{path}",
        "status_done": "Combiné avec succès → {path}",
        # Strava window
        "strava_title": "Importer depuis Strava",
        "from_label": "Du :",
        "to_label": "Au :",
        "filter_btn": "Charger",
        "clear_dates": "Effacer les dates",
        "pick_date": "📅",
        "prev_page": "◀ Page précédente",
        "next_page": "Page suivante ▶",
        "page_label": "Page {n}",
        "col_date": "Date",
        "col_name": "Nom",
        "col_type": "Sport",
        "col_distance": "Distance",
        "col_duration": "Durée",
        "col_city": "Commune",
        "col_gear": "Matériel",
        "per_page_label": "Par page :",
        "download_selected": "Télécharger la sélection ({n})",
        "loading": "Chargement…",
        "no_activities": "Aucune activité trouvée pour cette période.",
        "close": "Fermer",
        "ask_client_id": "Client ID Strava",
        "ask_client_id_prompt": "Entrez le Client ID de votre application Strava :",
        "ask_client_secret": "Client Secret Strava",
        "ask_client_secret_prompt": "Entrez le Client Secret de votre application Strava :",
        "authorizing": "Ouverture du navigateur pour autoriser l'accès à Strava…\n"
                        "Suivez les instructions puis revenez ici.",
        "auth_failed_title": "Autorisation échouée",
        "auth_failed_body": "L'autorisation Strava a échoué :\n{err}\n\n"
                             "Vérifiez que le champ « Authorization Callback Domain » de votre "
                             "application Strava est réglé sur : localhost",
        "download_done_title": "Téléchargement terminé",
        "download_done_body": "{n} activité(s) téléchargée(s) dans GPX-temp et ajoutée(s) à la liste.",
        "download_error_title": "Erreur de téléchargement",
        "download_error_body": "Impossible de récupérer le tracé de « {name} » (activité sans données GPS ?).",
        "api_error_title": "Erreur API Strava",
        "api_error_body": "{err}",
        "invalid_date_title": "Date invalide",
        "invalid_date_body": "Utilisez le format AAAA-MM-JJ.",
    },
    "en": {
        "app_title": "GPX combiner",
        "language": "Language",
        "add_files": "Add GPX files",
        "remove_selected": "Remove selection",
        "clear_all": "Clear all",
        "import_strava": "Import from Strava",
        "order_label": "Detected chronological order (oldest to newest):",
        "combine_save": "Combine and save",
        "preview_btn": "Preview",
        "preview_title": "Track preview",
        "fit_all": "Fit all",
        "reload_tracks": "🔄 Reload",
        "no_tracks_status": "No tracks loaded to display.",
        "zoom_status": "Zoom {z}",
        "legend_title": "Legend",
        "ok_btn": "OK",
        "cancel_btn": "Cancel",
        "dnd_hint": "Tip: you can also drag and drop GPX files here.",
        "include_fields_label": "Include in export:",
        "upload_btn": "Upload to Strava…",
        "upload_no_file_body": "Combine some files first to get a GPX to upload.",
        "open_originals_title": "Open the original activities?",
        "open_originals_body": "{n} of the combined files come from existing Strava activities. To avoid a "
                                "duplicate-activity error, you may want to delete them on Strava before "
                                "uploading (recoverable for 30 days if you change your mind). Open each one "
                                "in a new browser tab?",
        "upload_name_title": "Activity name",
        "upload_name_prompt": "Name to give this activity on Strava:",
        "upload_type_prompt": "Activity type:",
        "upload_progress_title": "Uploading to Strava",
        "upload_status_uploading": "Uploading the file…",
        "upload_status_processing": "Strava is processing it…",
        "upload_status_done": "Activity created successfully.",
        "upload_status_error": "Upload failed:\n{err}",
        "upload_status_timeout": "Strava is taking unusually long to process this — try again later.",
        "upload_view_on_strava": "View on Strava",
        "strava_settings_btn": "⚙ Strava settings",
        "strava_settings_title": "Strava settings",
        "strava_not_configured": "No Strava credentials saved on this computer.",
        "strava_client_id_label": "Client ID: {id}",
        "strava_connected_as": "Connected as: {name}",
        "strava_authorized_unknown": "Authorized (account name unavailable right now).",
        "strava_not_authorized": "Credentials saved, but not yet authorized.",
        "connect_strava": "Connect…",
        "disconnect_strava": "Disconnect and erase credentials",
        "confirm_disconnect_title": "Confirm disconnect",
        "confirm_disconnect_body": "This will delete the Client ID, Client Secret, and access tokens saved on "
                                    "this computer. You'll need to re-enter them to reconnect Strava. Continue?",
        "disconnected_title": "Disconnected",
        "disconnected_body": "Strava credentials have been erased from this computer.",
        "strava_credentials_title": "Connect to Strava",
        "strava_credentials_info": "To connect Strava, create an application at developers.strava.com (free), "
                                    "then set its \"Authorization Callback Domain\" to: localhost\n\n"
                                    "The Client ID and Client Secret are then shown on your application's page "
                                    "(\"My API Application\").",
        "strava_credentials_missing": "Please fill in both the Client ID and Client Secret.",
        "open_strava_dev_site": "Open developers.strava.com",
        "show_secret": "👁",
        "hide_secret": "🙈",
        "status_none": "No files loaded.",
        "status_one": "1 file loaded — add at least one more to combine.",
        "status_multiple": "{n} files loaded, ready to combine.",
        "unknown_time": "(unknown timestamp)",
        "err_read_title": "Read error",
        "err_read_body": "Could not read {path}:\n{err}",
        "warn_notenough_title": "Not enough files",
        "warn_notenough_body": "Add at least two GPX files to combine.",
        "err_base_title": "Invalid base file",
        "err_base_body": "The oldest file ({path}) does not contain a </trkseg> tag.",
        "warn_skipped_title": "Skipped files",
        "warn_skipped_body": "No <trkseg> tag found in:\n{files}",
        "err_nothing_title": "Nothing to combine",
        "err_nothing_body": "No segment could be extracted from the other files.",
        "save_title": "Save combined file",
        "err_write_title": "Write error",
        "err_write_body": "Could not save the file:\n{err}",
        "done_title": "Done",
        "done_body": "Combined file saved:\n{path}",
        "status_done": "Combined successfully → {path}",
        "strava_title": "Import from Strava",
        "from_label": "From:",
        "to_label": "To:",
        "filter_btn": "Load",
        "clear_dates": "Clear dates",
        "pick_date": "📅",
        "prev_page": "◀ Previous page",
        "next_page": "Next page ▶",
        "page_label": "Page {n}",
        "col_date": "Date",
        "col_name": "Name",
        "col_type": "Sport",
        "col_distance": "Distance",
        "col_duration": "Duration",
        "col_city": "Location",
        "col_gear": "Gear",
        "per_page_label": "Per page:",
        "download_selected": "Download selection ({n})",
        "loading": "Loading…",
        "no_activities": "No activities found for this period.",
        "close": "Close",
        "ask_client_id": "Strava Client ID",
        "ask_client_id_prompt": "Enter your Strava application's Client ID:",
        "ask_client_secret": "Strava Client Secret",
        "ask_client_secret_prompt": "Enter your Strava application's Client Secret:",
        "authorizing": "Opening your browser to authorize access to Strava…\n"
                        "Follow the instructions then come back here.",
        "auth_failed_title": "Authorization failed",
        "auth_failed_body": "Strava authorization failed:\n{err}\n\n"
                             "Check that your Strava application's \"Authorization Callback "
                             "Domain\" is set to: localhost",
        "download_done_title": "Download complete",
        "download_done_body": "{n} activity(ies) downloaded to GPX-temp and added to the list.",
        "download_error_title": "Download error",
        "download_error_body": "Could not get the track for \u201c{name}\u201d (activity without GPS data?).",
        "api_error_title": "Strava API error",
        "api_error_body": "{err}",
        "invalid_date_title": "Invalid date",
        "invalid_date_body": "Use the YYYY-MM-DD format.",
    },
    "es": {
        "app_title": "GPX combiner",
        "language": "Idioma",
        "add_files": "Añadir archivos GPX",
        "remove_selected": "Quitar selección",
        "clear_all": "Borrar todo",
        "import_strava": "Importar desde Strava",
        "order_label": "Orden cronológico detectado (del más antiguo al más reciente):",
        "combine_save": "Combinar y guardar",
        "preview_btn": "Vista previa",
        "preview_title": "Vista previa de las trazas",
        "fit_all": "Ajustar todo",
        "reload_tracks": "🔄 Recargar",
        "no_tracks_status": "No hay trazas cargadas para mostrar.",
        "zoom_status": "Zoom {z}",
        "legend_title": "Leyenda",
        "ok_btn": "Aceptar",
        "cancel_btn": "Cancelar",
        "dnd_hint": "Consejo: también puedes arrastrar y soltar archivos GPX aquí.",
        "include_fields_label": "Incluir en la exportación:",
        "upload_btn": "Subir a Strava…",
        "upload_no_file_body": "Combina primero algunos archivos para obtener un GPX que subir.",
        "open_originals_title": "¿Abrir las actividades originales?",
        "open_originals_body": "{n} de los archivos combinados provienen de actividades de Strava ya "
                                "existentes. Para evitar un error de actividad duplicada, puedes eliminarlas "
                                "en Strava antes de subir (recuperables durante 30 días si cambias de "
                                "opinión). ¿Abrir cada una en una nueva pestaña del navegador?",
        "upload_name_title": "Nombre de la actividad",
        "upload_name_prompt": "Nombre para esta actividad en Strava:",
        "upload_type_prompt": "Tipo de actividad:",
        "upload_progress_title": "Subiendo a Strava",
        "upload_status_uploading": "Subiendo el archivo…",
        "upload_status_processing": "Strava lo está procesando…",
        "upload_status_done": "Actividad creada con éxito.",
        "upload_status_error": "Error al subir:\n{err}",
        "upload_status_timeout": "Strava está tardando más de lo normal en procesar esto — inténtalo más tarde.",
        "upload_view_on_strava": "Ver en Strava",
        "strava_settings_btn": "⚙ Ajustes de Strava",
        "strava_settings_title": "Ajustes de Strava",
        "strava_not_configured": "No hay credenciales de Strava guardadas en este ordenador.",
        "strava_client_id_label": "Client ID: {id}",
        "strava_connected_as": "Conectado como: {name}",
        "strava_authorized_unknown": "Autorizado (nombre de la cuenta no disponible por ahora).",
        "strava_not_authorized": "Credenciales guardadas, pero aún no autorizadas.",
        "connect_strava": "Conectar…",
        "disconnect_strava": "Desconectar y borrar credenciales",
        "confirm_disconnect_title": "Confirmar desconexión",
        "confirm_disconnect_body": "Esto eliminará el Client ID, el Client Secret y los tokens de acceso "
                                    "guardados en este ordenador. Tendrás que volver a introducirlos para "
                                    "reconectar Strava. ¿Continuar?",
        "disconnected_title": "Desconectado",
        "disconnected_body": "Las credenciales de Strava se han borrado de este ordenador.",
        "strava_credentials_title": "Conectar con Strava",
        "strava_credentials_info": "Para conectar Strava, crea una aplicación en developers.strava.com "
                                    "(gratis) y configura su «Authorization Callback Domain» como: localhost\n\n"
                                    "El Client ID y el Client Secret aparecen luego en la página de tu "
                                    "aplicación («Mon application API»).",
        "strava_credentials_missing": "Introduce el Client ID y el Client Secret.",
        "open_strava_dev_site": "Abrir developers.strava.com",
        "show_secret": "👁",
        "hide_secret": "🙈",
        "status_none": "Ningún archivo cargado.",
        "status_one": "1 archivo cargado — añade al menos uno más para combinar.",
        "status_multiple": "{n} archivos cargados, listos para combinar.",
        "unknown_time": "(marca de tiempo desconocida)",
        "err_read_title": "Error de lectura",
        "err_read_body": "No se pudo leer {path}:\n{err}",
        "warn_notenough_title": "Faltan archivos",
        "warn_notenough_body": "Añade al menos dos archivos GPX para combinar.",
        "err_base_title": "Archivo base inválido",
        "err_base_body": "El archivo más antiguo ({path}) no contiene una etiqueta </trkseg>.",
        "warn_skipped_title": "Archivos omitidos",
        "warn_skipped_body": "No se encontró etiqueta <trkseg> en:\n{files}",
        "err_nothing_title": "Nada que combinar",
        "err_nothing_body": "No se pudo extraer ningún segmento de los otros archivos.",
        "save_title": "Guardar archivo combinado",
        "err_write_title": "Error de escritura",
        "err_write_body": "No se pudo guardar el archivo:\n{err}",
        "done_title": "Listo",
        "done_body": "Archivo combinado guardado:\n{path}",
        "status_done": "Combinado con éxito → {path}",
        "strava_title": "Importar desde Strava",
        "from_label": "Desde:",
        "to_label": "Hasta:",
        "filter_btn": "Cargar",
        "clear_dates": "Borrar fechas",
        "pick_date": "📅",
        "prev_page": "◀ Página anterior",
        "next_page": "Página siguiente ▶",
        "page_label": "Página {n}",
        "col_date": "Fecha",
        "col_name": "Nombre",
        "col_type": "Deporte",
        "col_distance": "Distancia",
        "col_duration": "Duración",
        "col_city": "Localidad",
        "col_gear": "Equipo",
        "per_page_label": "Por página:",
        "download_selected": "Descargar selección ({n})",
        "loading": "Cargando…",
        "no_activities": "No se encontraron actividades para este período.",
        "close": "Cerrar",
        "ask_client_id": "Client ID de Strava",
        "ask_client_id_prompt": "Introduce el Client ID de tu aplicación Strava:",
        "ask_client_secret": "Client Secret de Strava",
        "ask_client_secret_prompt": "Introduce el Client Secret de tu aplicación Strava:",
        "authorizing": "Abriendo el navegador para autorizar el acceso a Strava…\n"
                        "Sigue las instrucciones y vuelve aquí.",
        "auth_failed_title": "Autorización fallida",
        "auth_failed_body": "La autorización de Strava falló:\n{err}\n\n"
                             "Comprueba que el campo «Authorization Callback Domain» de tu "
                             "aplicación Strava esté configurado como: localhost",
        "download_done_title": "Descarga completa",
        "download_done_body": "{n} actividad(es) descargada(s) en GPX-temp y añadida(s) a la lista.",
        "download_error_title": "Error de descarga",
        "download_error_body": "No se pudo obtener el trazado de «{name}» (¿actividad sin datos GPS?).",
        "api_error_title": "Error de la API de Strava",
        "api_error_body": "{err}",
        "invalid_date_title": "Fecha inválida",
        "invalid_date_body": "Usa el formato AAAA-MM-DD.",
    },
    "de": {
        "app_title": "GPX combiner",
        "language": "Sprache",
        "add_files": "GPX-Dateien hinzufügen",
        "remove_selected": "Auswahl entfernen",
        "clear_all": "Alles löschen",
        "import_strava": "Von Strava importieren",
        "order_label": "Erkannte chronologische Reihenfolge (älteste zuerst):",
        "combine_save": "Kombinieren und speichern",
        "preview_btn": "Vorschau",
        "preview_title": "Streckenvorschau",
        "fit_all": "Alles anzeigen",
        "reload_tracks": "🔄 Neu laden",
        "no_tracks_status": "Keine Strecken zum Anzeigen geladen.",
        "zoom_status": "Zoom {z}",
        "legend_title": "Legende",
        "ok_btn": "OK",
        "cancel_btn": "Abbrechen",
        "dnd_hint": "Tipp: Du kannst GPX-Dateien auch per Drag & Drop hierher ziehen.",
        "include_fields_label": "In den Export einschließen:",
        "upload_btn": "Zu Strava hochladen…",
        "upload_no_file_body": "Kombiniere zuerst einige Dateien, um eine GPX-Datei zum Hochladen zu erhalten.",
        "open_originals_title": "Ursprüngliche Aktivitäten öffnen?",
        "open_originals_body": "{n} der kombinierten Dateien stammen aus bereits vorhandenen "
                                "Strava-Aktivitäten. Um einen Duplikat-Fehler zu vermeiden, kannst du sie vor "
                                "dem Hochladen auf Strava löschen (bei Bedarf 30 Tage lang wiederherstellbar). "
                                "Jede in einem neuen Browser-Tab öffnen?",
        "upload_name_title": "Name der Aktivität",
        "upload_name_prompt": "Name für diese Aktivität auf Strava:",
        "upload_type_prompt": "Aktivitätstyp:",
        "upload_progress_title": "Hochladen zu Strava",
        "upload_status_uploading": "Datei wird hochgeladen…",
        "upload_status_processing": "Strava verarbeitet die Datei…",
        "upload_status_done": "Aktivität erfolgreich erstellt.",
        "upload_status_error": "Hochladen fehlgeschlagen:\n{err}",
        "upload_status_timeout": "Strava braucht ungewöhnlich lange — versuche es später erneut.",
        "upload_view_on_strava": "Auf Strava ansehen",
        "strava_settings_btn": "⚙ Strava-Einstellungen",
        "strava_settings_title": "Strava-Einstellungen",
        "strava_not_configured": "Keine Strava-Zugangsdaten auf diesem Computer gespeichert.",
        "strava_client_id_label": "Client ID: {id}",
        "strava_connected_as": "Verbunden als: {name}",
        "strava_authorized_unknown": "Autorisiert (Kontoname derzeit nicht verfügbar).",
        "strava_not_authorized": "Zugangsdaten gespeichert, aber noch nicht autorisiert.",
        "connect_strava": "Verbinden…",
        "disconnect_strava": "Trennen und Zugangsdaten löschen",
        "confirm_disconnect_title": "Trennung bestätigen",
        "confirm_disconnect_body": "Dadurch werden Client ID, Client Secret und Zugriffstoken auf diesem "
                                    "Computer gelöscht. Du musst sie erneut eingeben, um Strava wieder zu "
                                    "verbinden. Fortfahren?",
        "disconnected_title": "Getrennt",
        "disconnected_body": "Die Strava-Zugangsdaten wurden von diesem Computer gelöscht.",
        "strava_credentials_title": "Mit Strava verbinden",
        "strava_credentials_info": "Erstelle zum Verbinden eine Anwendung auf developers.strava.com "
                                    "(kostenlos) und setze deren „Authorization Callback Domain“ auf: localhost\n\n"
                                    "Client ID und Client Secret findest du danach auf der Seite deiner "
                                    "Anwendung („Mon application API“).",
        "strava_credentials_missing": "Bitte Client ID und Client Secret ausfüllen.",
        "open_strava_dev_site": "developers.strava.com öffnen",
        "show_secret": "👁",
        "hide_secret": "🙈",
        "status_none": "Keine Dateien geladen.",
        "status_one": "1 Datei geladen — füge mindestens eine weitere hinzu, um zu kombinieren.",
        "status_multiple": "{n} Dateien geladen, bereit zum Kombinieren.",
        "unknown_time": "(unbekannter Zeitstempel)",
        "err_read_title": "Lesefehler",
        "err_read_body": "{path} konnte nicht gelesen werden:\n{err}",
        "warn_notenough_title": "Nicht genug Dateien",
        "warn_notenough_body": "Füge mindestens zwei GPX-Dateien zum Kombinieren hinzu.",
        "err_base_title": "Ungültige Basisdatei",
        "err_base_body": "Die älteste Datei ({path}) enthält kein </trkseg>-Tag.",
        "warn_skipped_title": "Übersprungene Dateien",
        "warn_skipped_body": "Kein <trkseg>-Tag gefunden in:\n{files}",
        "err_nothing_title": "Nichts zu kombinieren",
        "err_nothing_body": "Aus den anderen Dateien konnte kein Segment extrahiert werden.",
        "save_title": "Kombinierte Datei speichern",
        "err_write_title": "Schreibfehler",
        "err_write_body": "Datei konnte nicht gespeichert werden:\n{err}",
        "done_title": "Fertig",
        "done_body": "Kombinierte Datei gespeichert:\n{path}",
        "status_done": "Erfolgreich kombiniert → {path}",
        "strava_title": "Von Strava importieren",
        "from_label": "Von:",
        "to_label": "Bis:",
        "filter_btn": "Laden",
        "clear_dates": "Daten löschen",
        "pick_date": "📅",
        "prev_page": "◀ Vorherige Seite",
        "next_page": "Nächste Seite ▶",
        "page_label": "Seite {n}",
        "col_date": "Datum",
        "col_name": "Name",
        "col_type": "Sportart",
        "col_distance": "Distanz",
        "col_duration": "Dauer",
        "col_city": "Ort",
        "col_gear": "Ausrüstung",
        "per_page_label": "Pro Seite:",
        "download_selected": "Auswahl herunterladen ({n})",
        "loading": "Lädt…",
        "no_activities": "Keine Aktivitäten für diesen Zeitraum gefunden.",
        "close": "Schließen",
        "ask_client_id": "Strava Client ID",
        "ask_client_id_prompt": "Gib die Client ID deiner Strava-Anwendung ein:",
        "ask_client_secret": "Strava Client Secret",
        "ask_client_secret_prompt": "Gib das Client Secret deiner Strava-Anwendung ein:",
        "authorizing": "Der Browser wird geöffnet, um den Zugriff auf Strava zu autorisieren…\n"
                        "Folge den Anweisungen und kehre dann hierher zurück.",
        "auth_failed_title": "Autorisierung fehlgeschlagen",
        "auth_failed_body": "Die Strava-Autorisierung ist fehlgeschlagen:\n{err}\n\n"
                             "Stelle sicher, dass „Authorization Callback Domain“ deiner "
                             "Strava-Anwendung auf: localhost gesetzt ist",
        "download_done_title": "Download abgeschlossen",
        "download_done_body": "{n} Aktivität(en) nach GPX-temp heruntergeladen und zur Liste hinzugefügt.",
        "download_error_title": "Downloadfehler",
        "download_error_body": "Track von „{name}“ konnte nicht abgerufen werden (Aktivität ohne GPS-Daten?).",
        "api_error_title": "Strava-API-Fehler",
        "api_error_body": "{err}",
        "invalid_date_title": "Ungültiges Datum",
        "invalid_date_body": "Verwende das Format JJJJ-MM-TT.",
    },
}

LANGUAGE_NAMES = {"fr": "Français", "en": "English", "es": "Español", "de": "Deutsch"}


# ----------------------------------------------------------------------------
# Simple clickable date picker (stdlib only — no tkcalendar dependency)
# ----------------------------------------------------------------------------

class DatePicker(ttk.Frame):
    """An optional date field: editable entry (type AAAA-MM-JJ directly) + a
    button opening a small calendar popup. Empty by default.
    .get() returns the raw entry text (validated by the caller)."""

    def __init__(self, master, t, width=10):
        super().__init__(master)
        self.t = t
        self.entry_var = tk.StringVar(value="")
        self.entry = ttk.Entry(self, textvariable=self.entry_var, width=width)
        self.entry.pack(side="left")
        self.btn = ttk.Button(self, text="📅", width=3, command=self._open_popup)
        self.btn.pack(side="left", padx=(2, 0))
        self.clear_btn = ttk.Button(self, text="✕", width=2, command=self.clear)
        self.clear_btn.pack(side="left", padx=(2, 0))
        self._popup = None
        self._view_year = None
        self._view_month = None
        self._edit_mode = False

    def get(self):
        return self.entry_var.get().strip()

    def clear(self):
        self.entry_var.set("")

    def _open_popup(self):
        if self._popup is not None:
            self._popup.destroy()

        today = date.today()
        value = self.get()
        if value:
            try:
                d = datetime.strptime(value, "%Y-%m-%d").date()
                self._view_year, self._view_month = d.year, d.month
            except ValueError:
                self._view_year, self._view_month = today.year, today.month
        else:
            self._view_year, self._view_month = today.year, today.month

        self._edit_mode = False
        self._popup = tk.Toplevel(self)
        self._popup.transient(self.winfo_toplevel())
        self._popup.resizable(False, False)
        try:
            self._popup.attributes("-topmost", True)
        except tk.TclError:
            pass
        x = self.btn.winfo_rootx()
        y = self.btn.winfo_rooty() + self.btn.winfo_height()
        self._popup.geometry(f"+{x}+{y}")
        self._popup.bind("<Escape>", lambda e: self._close_popup())
        self._draw_calendar()

    def _close_popup(self):
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None

    def _draw_calendar(self):
        for child in self._popup.winfo_children():
            child.destroy()

        nav = ttk.Frame(self._popup, padding=4)
        nav.pack(fill="x")

        if self._edit_mode:
            ttk.Button(nav, text="◀◀", width=3, command=lambda: self._nudge_year(-1)).pack(side="left")
            self._month_combo = ttk.Combobox(nav, state="readonly", width=9,
                                              values=list(cal_module.month_name)[1:])
            self._month_combo.current(self._view_month - 1)
            self._month_combo.pack(side="left", padx=2)
            self._year_spin = ttk.Spinbox(nav, from_=1970, to=2100, width=6)
            self._year_spin.set(str(self._view_year))
            self._year_spin.pack(side="left", padx=2)
            ttk.Button(nav, text="✓", width=3, command=self._apply_month_year).pack(side="left")
            ttk.Button(nav, text="▶▶", width=3, command=lambda: self._nudge_year(1)).pack(side="left")
        else:
            ttk.Button(nav, text="◀", width=2, command=self._prev_month).pack(side="left")
            label = ttk.Label(nav, text=f"{cal_module.month_name[self._view_month]} {self._view_year}",
                               width=16, anchor="center", cursor="hand2")
            label.pack(side="left", expand=True)
            label.bind("<Button-1>", lambda e: self._enter_edit_mode())
            ttk.Button(nav, text="▶", width=2, command=self._next_month).pack(side="left")

        grid = ttk.Frame(self._popup, padding=4)
        grid.pack()

        if not self._edit_mode:
            days_short = [cal_module.day_abbr[i][:2] for i in range(7)]
            for col, d in enumerate(days_short):
                ttk.Label(grid, text=d, width=3, anchor="center",
                          font=("TkDefaultFont", 8, "bold")).grid(row=0, column=col)

            month_days = cal_module.monthcalendar(self._view_year, self._view_month)
            for r, week in enumerate(month_days, start=1):
                for c, day in enumerate(week):
                    if day == 0:
                        ttk.Label(grid, text="", width=3).grid(row=r, column=c)
                    else:
                        b = ttk.Button(grid, text=str(day), width=3,
                                        command=lambda d=day: self._select_day(d))
                        b.grid(row=r, column=c, padx=1, pady=1)

    def _enter_edit_mode(self):
        self._edit_mode = True
        self._draw_calendar()

    def _apply_month_year(self):
        try:
            self._view_year = int(self._year_spin.get())
        except ValueError:
            pass
        self._view_month = self._month_combo.current() + 1
        self._edit_mode = False
        self._draw_calendar()

    def _nudge_year(self, delta):
        self._view_year += delta
        self._year_spin.delete(0, "end")
        self._year_spin.insert(0, str(self._view_year))

    def _prev_month(self):
        self._view_month -= 1
        if self._view_month == 0:
            self._view_month = 12
            self._view_year -= 1
        self._draw_calendar()

    def _next_month(self):
        self._view_month += 1
        if self._view_month == 13:
            self._view_month = 1
            self._view_year += 1
        self._draw_calendar()

    def _select_day(self, day):
        self.entry_var.set(f"{self._view_year:04d}-{self._view_month:02d}-{day:02d}")
        self._close_popup()


# ----------------------------------------------------------------------------
# GPX helpers
# ----------------------------------------------------------------------------

def extract_sort_key(content, fallback):
    m = TIME_RE.search(content)
    return m.group(1).strip() if m else fallback


def extract_first_trkseg(content):
    m = TRKSEG_RE.search(content)
    return m.group(0) if m else None


def detect_extension_fields(content):
    """Which of hr/cadence/power/temp are present in this GPX's extensions."""
    return {key: bool(rx.search(content)) for key, rx in EXT_FIELD_DETECT_RE.items()}


def strip_extension_fields(content, include):
    """Remove the extension tags for any field where include[field] is False,
    always remove gpxdata:distance (see GPXDATA_DISTANCE_RE above — it's not
    tied to the checkboxes since it's a different device schema and always
    wrong once combined), then clean up any now-empty
    <gpxtpx:TrackPointExtension>/<extensions> wrapper tags left behind."""
    for key, rx in EXT_FIELD_STRIP_RE.items():
        if not include.get(key, True):
            content = rx.sub("", content)
    content = GPXDATA_DISTANCE_RE.sub("", content)
    content = EMPTY_TPX_RE.sub("", content)
    content = EMPTY_EXT_RE.sub("", content)
    return content


def parse_trkpts(content):
    """Extract every (lat, lon) point from a GPX file's <trkpt> tags,
    regardless of attribute order."""
    points = []
    for m in TRKPT_TAG_RE.finditer(content):
        attrs = m.group(1)
        latm = LAT_ATTR_RE.search(attrs)
        lonm = LON_ATTR_RE.search(attrs)
        if latm and lonm:
            try:
                points.append((float(latm.group(1)), float(lonm.group(1))))
            except ValueError:
                continue
    return points


def lonlat_to_pixel(lon, lat, zoom):
    """Web Mercator projection: (lon, lat) -> global pixel coords at a zoom level."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n * OSM_TILE_SIZE
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * OSM_TILE_SIZE
    return x, y


def pixel_to_lonlat(x, y, zoom):
    """Inverse of lonlat_to_pixel: global pixel coords -> (lon, lat)."""
    n = 2.0 ** zoom
    lon = x / (n * OSM_TILE_SIZE) * 360.0 - 180.0
    y_frac = y / (n * OSM_TILE_SIZE)
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y_frac)))
    return lon, math.degrees(lat_rad)


def build_gpx_from_activity(activity, streams):
    """Build a single GPX (one <trk><trkseg>) from a Strava activity + its
    streams, including power/heart rate/cadence/temperature as extensions —
    using the exact same tags Strava itself writes in its own GPX exports
    (bare <power>, plus the Garmin gpxtpx:TrackPointExtension block), so a
    file combined from these exports re-uploads to Strava the same way."""
    latlng = streams.get("latlng", {}).get("data")
    if not latlng:
        return None
    n = len(latlng)
    altitude = streams.get("altitude", {}).get("data") or [None] * n
    time_offsets = streams.get("time", {}).get("data") or list(range(n))
    heartrate = streams.get("heartrate", {}).get("data") or [None] * n
    cadence = streams.get("cadence", {}).get("data") or [None] * n
    watts = streams.get("watts", {}).get("data") or [None] * n
    temp = streams.get("temp", {}).get("data") or [None] * n

    start_str = activity.get("start_date")  # e.g. "2026-09-09T09:04:00Z"
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        start_dt = datetime.now(timezone.utc)

    gpx_type = strava_activity_gpx_type(activity)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="GPX Combiner" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        'http://www.topografix.com/GPX/1/1/gpx.xsd">',
        "  <trk>",
        f"    <name>{escape_xml(activity.get('name', 'Activity'))}</name>",
    ]
    if gpx_type:
        lines.append(f"    <type>{escape_xml(gpx_type)}</type>")
    lines.append("    <trkseg>")
    for (lat, lon), ele, t, hr, cad, w, temp_v in zip(latlng, altitude, time_offsets,
                                                        heartrate, cadence, watts, temp):
        pt_time = (start_dt + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ")

        body = ""
        if ele is not None:
            body += f"<ele>{ele}</ele>"
        body += f"<time>{pt_time}</time>"

        tpx_parts = []
        if temp_v is not None:
            tpx_parts.append(f"<gpxtpx:atemp>{temp_v}</gpxtpx:atemp>")
        if hr is not None:
            tpx_parts.append(f"<gpxtpx:hr>{hr}</gpxtpx:hr>")
        if cad is not None:
            tpx_parts.append(f"<gpxtpx:cad>{cad}</gpxtpx:cad>")

        ext_parts = []
        if w is not None:
            ext_parts.append(f"<power>{round(w)}</power>")
        if tpx_parts:
            ext_parts.append("<gpxtpx:TrackPointExtension>" + "".join(tpx_parts) + "</gpxtpx:TrackPointExtension>")
        if ext_parts:
            body += "<extensions>" + "".join(ext_parts) + "</extensions>"

        lines.append(f'      <trkpt lat="{lat}" lon="{lon}">{body}</trkpt>')
    lines.append("    </trkseg>")
    lines.append("  </trk>")
    lines.append("</gpx>")
    return "\n".join(lines)


def escape_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


# Strava's own GPX exports use these lowercase values for <type>. Falls back
# to the lowercased raw Strava type for anything not in this table.
# Confirmed directly against real Strava-exported GPX files (2026-09-23):
# gravel_biking, ebikeride, mountain_biking, EMountainBikeRide (yes, mixed
# case — that inconsistency is Strava's own, not a typo here), trail_running.
STRAVA_TYPE_TO_GPX_TYPE = {
    "Ride": "cycling", "VirtualRide": "cycling", "Velomobile": "cycling", "Handcycle": "cycling",
    "MountainBikeRide": "mountain_biking",
    "GravelRide": "gravel_biking",
    "EBikeRide": "ebikeride",
    "EMountainBikeRide": "EMountainBikeRide",
    "Run": "running", "VirtualRun": "running",
    "TrailRun": "trail_running",
    "Walk": "walking", "Hike": "hiking",
    "Swim": "swimming",
    "AlpineSki": "skiing", "BackcountrySki": "skiing", "NordicSki": "skiing", "RollerSki": "skiing",
    "Snowboard": "snowboarding", "Snowshoe": "snowshoeing", "IceSkate": "ice skating",
    "InlineSkate": "inline skating", "Skateboard": "skateboarding",
    "RockClimbing": "rock climbing", "Rowing": "rowing", "Canoeing": "canoeing",
    "Kayaking": "kayaking", "StandUpPaddling": "stand up paddling", "Surfing": "surfing",
    "Kitesurf": "kitesurfing", "Windsurf": "windsurfing", "Sail": "sailing",
    "WeightTraining": "weight training", "Workout": "workout", "Crossfit": "crossfit",
    "Yoga": "yoga", "Elliptical": "elliptical", "StairStepper": "stair stepper",
    "Golf": "golf", "Soccer": "soccer", "Wheelchair": "wheelchair",
}


def strava_activity_gpx_type(activity):
    raw = activity.get("type") or ""
    return STRAVA_TYPE_TO_GPX_TYPE.get(raw, raw.lower()) if raw else ""


# For the upload dropdown: Strava's own type-picker (as of this writing)
# leads with cycling/running/walking variants, then winter sports, before
# the long tail of less common activities — this mirrors that ordering for
# the entries we have equivalents for, then appends the rest of our table
# alphabetically. Strava's /uploads endpoint no longer accepts a separate
# activity_type form field (checked against the current official API
# reference) — Strava derives the type by reading the GPX file's own
# <type> tag, so choosing a type here means rewriting that tag before
# upload, not adding a request parameter.
_TYPE_PRIORITY = [
    "cycling", "walking", "running", "trail_running", "gravel_biking", "mountain_biking",
    "swimming", "hiking", "ebikeride", "EMountainBikeRide", "skiing", "workout",
]
GPX_TYPE_CHOICES = _TYPE_PRIORITY + sorted(set(STRAVA_TYPE_TO_GPX_TYPE.values()) - set(_TYPE_PRIORITY))

TYPE_TAG_RE = re.compile(r"<type>(.*?)</type>", re.I | re.S)


def read_gpx_type(content):
    m = TYPE_TAG_RE.search(content)
    return m.group(1).strip() if m else ""


def set_gpx_type(content, new_type):
    """Replaces the <type> tag's content, or inserts one right after
    </name> if the file doesn't have one yet."""
    if TYPE_TAG_RE.search(content):
        return TYPE_TAG_RE.sub(f"<type>{escape_xml(new_type)}</type>", content, count=1)
    name_end = content.find("</name>")
    if name_end == -1:
        return content  # no <name> to anchor on — leave the file untouched
    insert_pos = name_end + len("</name>")
    return content[:insert_pos] + f"\n    <type>{escape_xml(new_type)}</type>" + content[insert_pos:]


def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "activity"


_geocode_cache = {}
_last_geocode_time = [0.0]


def reverse_geocode_city(lat, lon):
    """Best-effort reverse geocoding of a start point to a city/town name via
    the free Nominatim (OpenStreetMap) API. Returns '' if nothing usable is
    found. Respects Nominatim's 1 request/second usage policy and caches
    results (rounded to ~100 m) to avoid repeat lookups."""
    key = (round(lat, 3), round(lon, 3))
    if key in _geocode_cache:
        return _geocode_cache[key]

    elapsed = time.time() - _last_geocode_time[0]
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_geocode_time[0] = time.time()

    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?format=jsonv2&lat={lat}&lon={lon}&zoom=12&addressdetails=1"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "GPXCombiner/1.0 (local desktop app)"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return ""

    addr = data.get("address", {})
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("county") or "")
    _geocode_cache[key] = city
    return city


# ----------------------------------------------------------------------------
# Strava API client
# ----------------------------------------------------------------------------

# Strips C0/C1 control characters plus the Unicode LINE SEPARATOR (U+2028)
# and PARAGRAPH SEPARATOR (U+2029) — these act as invisible line breaks and
# are the likely cause of some activity names rendering garbled in a
# single-line widget (the name gets split across many tiny sublines).
# Ordinary characters — pipes, quotes, emoji — are left untouched.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029]")


def clean_display_text(text):
    return _CONTROL_CHARS_RE.sub("", text or "")


class StravaAuthError(Exception):
    pass


class StravaAPIError(Exception):
    pass


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.oauth_code = params.get("code", [None])[0]
        self.server.oauth_error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body><h2>Strava</h2><p>You can close this window and return "
            "to the application.</p></body></html>".encode("utf-8")
        )

    def log_message(self, fmt, *args):
        pass  # silence default logging


class _DualStackHTTPServer(HTTPServer):
    """Listens on both IPv4 and IPv6 loopback, since browsers may resolve
    'localhost' to either depending on the OS (common cause of a local OAuth
    callback silently failing to connect on macOS)."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class StravaClient:
    TOKEN_URL = "https://www.strava.com/oauth/token"
    AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
    API_BASE = "https://www.strava.com/api/v3"

    def __init__(self, config):
        self.config = config  # dict, persisted to CONFIG_PATH by caller

    def save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.config, f)

    @property
    def has_credentials(self):
        return bool(self.config.get("client_id") and self.config.get("client_secret"))

    @property
    def has_token(self):
        return bool(self.config.get("refresh_token"))

    def authorize_interactive(self):
        """Runs the full OAuth authorization-code flow, blocking until done."""
        params = {
            "client_id": self.config["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "activity:read_all,activity:write",
        }
        url = f"{self.AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

        try:
            httpd = _DualStackHTTPServer(("::", REDIRECT_PORT), _CallbackHandler)
        except OSError:
            # IPv6 unavailable on this system — fall back to plain IPv4.
            try:
                httpd = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
            except OSError as e:
                raise StravaAuthError(f"port {REDIRECT_PORT} unavailable ({e})")
        httpd.oauth_code = None
        httpd.oauth_error = None
        httpd.timeout = 15

        webbrowser.open(url)

        deadline = time.time() + 120
        while httpd.oauth_code is None and httpd.oauth_error is None and time.time() < deadline:
            httpd.handle_request()  # returns after one request, or after httpd.timeout seconds
        httpd.server_close()

        if httpd.oauth_error:
            raise StravaAuthError(httpd.oauth_error)
        if not httpd.oauth_code:
            raise StravaAuthError("timeout / no response received")

        self._exchange_code(httpd.oauth_code)

    def _exchange_code(self, code):
        data = urllib.parse.urlencode({
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(self.TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAuthError(e.read().decode())
        self.config["refresh_token"] = payload["refresh_token"]
        self.config["access_token"] = payload["access_token"]
        self.config["expires_at"] = payload["expires_at"]
        self.save()

    def _ensure_fresh_token(self):
        if self.config.get("access_token") and self.config.get("expires_at", 0) > time.time() + 60:
            return
        data = urllib.parse.urlencode({
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": self.config["refresh_token"],
        }).encode()
        req = urllib.request.Request(self.TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAuthError(e.read().decode())
        self.config["refresh_token"] = payload["refresh_token"]
        self.config["access_token"] = payload["access_token"]
        self.config["expires_at"] = payload["expires_at"]
        self.save()

    def _get(self, path, params=None):
        self._ensure_fresh_token()
        url = f"{self.API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self.config['access_token']}")
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAPIError(f"{e.code} {e.read().decode()}")
        except urllib.error.URLError as e:
            raise StravaAPIError(str(e))

    def list_activities(self, page, per_page=10, before=None, after=None):
        params = {"page": page, "per_page": per_page}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        return self._get("/athlete/activities", params)

    def get_streams(self, activity_id):
        return self._get(
            f"/activities/{activity_id}/streams",
            {"keys": "latlng,altitude,time,heartrate,cadence,watts,temp", "key_by_type": "true"},
        )

    def get_gear(self, gear_id):
        return self._get(f"/gear/{gear_id}")

    # -- upload (requires the activity:write scope — see authorize_interactive) --
    def upload_gpx(self, filepath, name=None, description=None):
        """Uploads a GPX file as a new Strava activity. Returns the Strava
        upload id (NOT the final activity id — Strava processes uploads
        asynchronously; poll check_upload() with the returned id until it
        reports an activity_id or an error)."""
        self._ensure_fresh_token()
        boundary = uuid.uuid4().hex
        fields = {"data_type": "gpx"}
        if name:
            fields["name"] = name
        if description:
            fields["description"] = description

        with open(filepath, "rb") as f:
            file_bytes = f.read()

        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        filename = os.path.basename(filepath)
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f'Content-Type: application/gpx+xml\r\n\r\n').encode() + file_bytes + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(f"{self.API_BASE}/uploads", data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.config['access_token']}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise StravaAPIError(f"{e.code} {e.read().decode()}")
        return payload["id"]

    def check_upload(self, upload_id):
        """One status check — {'status': ..., 'activity_id': ... or None,
        'error': ... or None}. Caller is responsible for polling/pacing."""
        return self._get(f"/uploads/{upload_id}")


# ----------------------------------------------------------------------------
# Strava import window
# ----------------------------------------------------------------------------

class StravaImportWindow(tk.Toplevel):
    # Fixed PIXEL widths (not character-based ttk "width=") shared by the
    # header row and every activity row, so bold header text and regular row
    # text line up exactly regardless of the font's per-character width.
    COL_CHECKBOX_PX = 28
    COL_WIDTHS_PX = {"date": 100, "name": 190, "type": 80, "distance": 90, "duration": 90,
                      "city": 130, "gear": 120}

    def __init__(self, master_app, client):
        super().__init__(master_app.root)
        self.master_app = master_app
        self.t = master_app.t
        self.title(self.t("strava_title"))
        self.geometry("940x520")
        self.minsize(900, 420)

        self.client = client
        self.current_page = 1
        self.selected_ids = set()
        self.activity_cache = {}
        self.rows = []
        self._render_generation = 0
        self._city_vars = {}

        self._build_ui()

        if not self.client.has_token:
            self.after(200, self._authorize_then_load)
        else:
            self.after(200, self.load_page)

    def _make_cell(self, parent, width_px, **label_kwargs):
        """A fixed-pixel-width container with a Label inside — used for both
        the header and each row, so columns line up regardless of font/weight."""
        cell = tk.Frame(parent, width=width_px, height=22)
        cell.pack(side="left")
        cell.pack_propagate(False)
        label = ttk.Label(cell, **label_kwargs)
        label.pack(side="left", anchor="w")
        return label

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text=self.t("from_label")).pack(side="left")
        self.from_picker = DatePicker(top, self.t)
        self.from_picker.pack(side="left", padx=(2, 10))

        ttk.Label(top, text=self.t("to_label")).pack(side="left")
        self.to_picker = DatePicker(top, self.t)
        self.to_picker.pack(side="left", padx=(2, 10))

        ttk.Button(top, text=self.t("filter_btn"), command=self.apply_filter).pack(side="left")

        self.list_frame = ttk.Frame(self, padding=(10, 0))
        self.list_frame.pack(fill="both", expand=True)

        header = ttk.Frame(self.list_frame)
        header.pack(fill="x")
        # Fixed-pixel spacer matching the checkbox column of every row.
        tk.Frame(header, width=self.COL_CHECKBOX_PX, height=1).pack(side="left")
        for key, text in [("date", self.t("col_date")), ("name", self.t("col_name")),
                           ("type", self.t("col_type")), ("distance", self.t("col_distance")),
                           ("duration", self.t("col_duration")), ("city", self.t("col_city")),
                           ("gear", self.t("col_gear"))]:
            self._make_cell(header, self.COL_WIDTHS_PX[key],
                             text=text, font=("TkDefaultFont", 9, "bold"))

        self.rows_canvas = tk.Canvas(self.list_frame, highlightthickness=0)
        rows_scrollbar = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.rows_canvas.yview)
        self.rows_canvas.configure(yscrollcommand=rows_scrollbar.set)
        self.rows_canvas.pack(side="left", fill="both", expand=True, pady=4)
        rows_scrollbar.pack(side="right", fill="y", pady=4)

        self.rows_frame = ttk.Frame(self.rows_canvas)
        self._rows_window = self.rows_canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")

        # Keep the scrollable region in sync with the row list's actual size,
        # and make the inner frame track the canvas's width (only the height
        # should ever need to scroll).
        self.rows_frame.bind(
            "<Configure>",
            lambda e: self.rows_canvas.configure(scrollregion=self.rows_canvas.bbox("all")),
        )
        self.rows_canvas.bind(
            "<Configure>",
            lambda e: self.rows_canvas.itemconfigure(self._rows_window, width=e.width),
        )

        def _on_mousewheel(event):
            delta = event.delta
            if sys.platform == "darwin":
                self.rows_canvas.yview_scroll(-delta, "units")
            else:
                self.rows_canvas.yview_scroll(-int(delta / 120), "units")

        def _bind_mousewheel(_event=None):
            self.rows_canvas.bind_all("<MouseWheel>", _on_mousewheel)
            self.rows_canvas.bind_all("<Button-4>", lambda e: self.rows_canvas.yview_scroll(-3, "units"))
            self.rows_canvas.bind_all("<Button-5>", lambda e: self.rows_canvas.yview_scroll(3, "units"))

        def _unbind_mousewheel(_event=None):
            self.rows_canvas.unbind_all("<MouseWheel>")
            self.rows_canvas.unbind_all("<Button-4>")
            self.rows_canvas.unbind_all("<Button-5>")

        # Only capture the mouse wheel while the cursor is actually over the
        # list, so scrolling elsewhere in the window (or in other windows)
        # isn't hijacked by this canvas.
        self.rows_canvas.bind("<Enter>", _bind_mousewheel)
        self.rows_canvas.bind("<Leave>", _unbind_mousewheel)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.list_frame, textvariable=self.status_var, foreground="#555").pack(anchor="w")

        pag = ttk.Frame(self, padding=10)
        pag.pack(fill="x")
        self.prev_btn = ttk.Button(pag, text=self.t("prev_page"), command=self.prev_page)
        self.prev_btn.pack(side="left")
        self.page_var = tk.StringVar(value=self.t("page_label").format(n=1))
        ttk.Label(pag, textvariable=self.page_var).pack(side="left", padx=10)
        self.next_btn = ttk.Button(pag, text=self.t("next_page"), command=self.next_page)
        self.next_btn.pack(side="left")

        ttk.Label(pag, text=self.t("per_page_label")).pack(side="left", padx=(20, 4))
        self.per_page_var = tk.StringVar(value="10")
        per_page_combo = ttk.Combobox(pag, state="readonly", width=4, textvariable=self.per_page_var,
                                       values=["10", "25", "50"])
        per_page_combo.pack(side="left")
        per_page_combo.bind("<<ComboboxSelected>>", self._on_per_page_change)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        self.download_btn = ttk.Button(bottom, text=self.t("download_selected").format(n=0),
                                        command=self.download_selected)
        self.download_btn.pack(side="right")
        ttk.Button(bottom, text=self.t("close"), command=self.destroy).pack(side="right", padx=6)

    def _authorize_then_load(self):
        messagebox.showinfo(self.t("strava_title"), self.t("authorizing"), parent=self)
        try:
            self.client.authorize_interactive()
        except StravaAuthError as e:
            messagebox.showerror(self.t("auth_failed_title"),
                                  self.t("auth_failed_body").format(err=e), parent=self)
            self.destroy()
            return
        self.load_page()

    def _parse_date(self, s):
        if not s.strip():
            return None
        try:
            dt = datetime.strptime(s.strip(), "%Y-%m-%d")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            messagebox.showerror(self.t("invalid_date_title"), self.t("invalid_date_body"), parent=self)
            raise

    def apply_filter(self):
        self.current_page = 1
        self.load_page()

    def _on_per_page_change(self, event=None):
        self.current_page = 1
        self.load_page()

    def prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self.load_page()

    def next_page(self):
        self.current_page += 1
        self.load_page()

    def load_page(self):
        try:
            after = self._parse_date(self.from_picker.get())
            before = self._parse_date(self.to_picker.get())
        except ValueError:
            return
        if before is not None:
            before += 86400

        per_page = int(self.per_page_var.get())

        self.status_var.set(self.t("loading"))
        self.update_idletasks()

        try:
            activities = self.client.list_activities(self.current_page, per_page=per_page,
                                                       before=before, after=after)
        except (StravaAPIError, StravaAuthError) as e:
            messagebox.showerror(self.t("api_error_title"), self.t("api_error_body").format(err=e), parent=self)
            self.status_var.set("")
            return

        for a in activities:
            self.activity_cache[a["id"]] = a

        self._render_rows(activities)
        self.page_var.set(self.t("page_label").format(n=self.current_page))
        self.prev_btn.state(["!disabled"] if self.current_page > 1 else ["disabled"])
        self.next_btn.state(["!disabled"] if len(activities) == per_page else ["disabled"])
        self.status_var.set("" if activities else self.t("no_activities"))

    def _render_rows(self, activities):
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.rows = []
        self._render_generation += 1
        generation = self._render_generation
        # Kept as an instance attribute (not just a local var) so the StringVars
        # stay alive for the whole life of the window — otherwise Python garbage
        # collects them once the background thread finishes, which silently
        # clears the labels' text.
        self._city_vars = {}
        city_vars = self._city_vars
        self._gear_vars = {}
        gear_vars = self._gear_vars
        if not hasattr(self, "_gear_name_cache"):
            self._gear_name_cache = {}  # gear_id -> name, shared across pages/renders

        for a in activities:
            row = ttk.Frame(self.rows_frame)
            row.pack(fill="x", pady=1)

            var = tk.BooleanVar(value=a["id"] in self.selected_ids)

            def on_toggle(aid=a["id"], var=var):
                if var.get():
                    self.selected_ids.add(aid)
                else:
                    self.selected_ids.discard(aid)
                self._update_download_label()

            # Same fixed-pixel-width spacer as the header, containing the
            # actual checkbox (rather than relying on the checkbox's own
            # native/theme-dependent width matching a separate header spacer).
            cb_cell = tk.Frame(row, width=self.COL_CHECKBOX_PX, height=22)
            cb_cell.pack(side="left")
            cb_cell.pack_propagate(False)
            cb = ttk.Checkbutton(cb_cell, variable=var, command=on_toggle)
            cb.pack(side="left")
            self.rows.append((var, a["id"]))

            date_str = (a.get("start_date_local") or a.get("start_date") or "")[:10]
            dist_km = (a.get("distance") or 0) / 1000
            dur_s = a.get("moving_time") or 0
            dur_str = f"{dur_s // 3600:02d}:{(dur_s % 3600) // 60:02d}:{dur_s % 60:02d}"

            self._make_cell(row, self.COL_WIDTHS_PX["date"], text=date_str)
            self._make_cell(row, self.COL_WIDTHS_PX["name"], text=clean_display_text(a.get("name", "")))
            self._make_cell(row, self.COL_WIDTHS_PX["type"], text=a.get("type", ""))
            self._make_cell(row, self.COL_WIDTHS_PX["distance"], text=f"{dist_km:.2f} km")
            self._make_cell(row, self.COL_WIDTHS_PX["duration"], text=dur_str)

            city_var = tk.StringVar(value=a.get("location_city") or "…")
            self._make_cell(row, self.COL_WIDTHS_PX["city"], textvariable=city_var)
            city_vars[a["id"]] = city_var

            gear_var = tk.StringVar(value="" if not a.get("gear_id") else "…")
            self._make_cell(row, self.COL_WIDTHS_PX["gear"], textvariable=gear_var)
            gear_vars[a["id"]] = gear_var

        self._update_download_label()

        # Resolve city names in the background (Strava rarely fills location_city,
        # so most rows need a reverse-geocoding lookup, which is a network call).
        threading.Thread(
            target=self._fill_cities_worker,
            args=(activities, city_vars, generation),
            daemon=True,
        ).start()
        # Same idea for gear: the activity list only gives a gear_id, so the
        # human-readable name needs one extra API call per *unique* gear_id
        # (cached — the same bike/shoe recurs across many activities).
        threading.Thread(
            target=self._fill_gear_worker,
            args=(activities, gear_vars, generation),
            daemon=True,
        ).start()

    def _fill_cities_worker(self, activities, city_vars, generation):
        for a in activities:
            city = a.get("location_city")
            if not city:
                latlng = a.get("start_latlng")
                if latlng and len(latlng) == 2:
                    city = reverse_geocode_city(latlng[0], latlng[1])
                else:
                    city = ""
            self.after(0, self._set_city_label, a["id"], city_vars.get(a["id"]), city or "—", generation)

    def _set_city_label(self, activity_id, city_var, city_text, generation):
        if generation != self._render_generation or city_var is None:
            return
        try:
            city_var.set(city_text)
        except tk.TclError:
            pass  # window/row was closed/rebuilt in the meantime

    def _fill_gear_worker(self, activities, gear_vars, generation):
        for a in activities:
            gear_id = a.get("gear_id")
            if not gear_id:
                continue
            name = self._gear_name_cache.get(gear_id)
            if name is None:
                try:
                    name = self.client.get_gear(gear_id).get("name") or gear_id
                except (StravaAPIError, StravaAuthError):
                    name = gear_id  # fall back to the raw id rather than leaving "…" forever
                self._gear_name_cache[gear_id] = name
            self.after(0, self._set_gear_label, gear_vars.get(a["id"]), name, generation)

    def _set_gear_label(self, gear_var, name, generation):
        if generation != self._render_generation or gear_var is None:
            return
        try:
            gear_var.set(name)
        except tk.TclError:
            pass

    def _update_download_label(self):
        self.download_btn.config(text=self.t("download_selected").format(n=len(self.selected_ids)))

    def download_selected(self):
        if not self.selected_ids:
            return
        os.makedirs(GPX_TEMP_DIR, exist_ok=True)

        saved_paths = []
        for aid in list(self.selected_ids):
            activity = self.activity_cache.get(aid)
            if not activity:
                continue
            try:
                streams = self.client.get_streams(aid)
            except (StravaAPIError, StravaAuthError) as e:
                messagebox.showerror(self.t("api_error_title"), self.t("api_error_body").format(err=e), parent=self)
                continue

            gpx_text = build_gpx_from_activity(activity, streams)
            if gpx_text is None:
                messagebox.showwarning(self.t("download_error_title"),
                                        self.t("download_error_body").format(name=activity.get("name", aid)),
                                        parent=self)
                continue

            fname = f"{sanitize_filename(activity.get('name', str(aid)))}_{aid}.gpx"
            fpath = os.path.join(GPX_TEMP_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(gpx_text)
            saved_paths.append(fpath)

        if saved_paths:
            self.master_app.add_files_from_paths(saved_paths)
        self.destroy()


# ----------------------------------------------------------------------------
# Map preview window (OSM tiles + overlaid tracks)
# ----------------------------------------------------------------------------

class MapPreviewWindow(tk.Toplevel):
    """A map view, attached next to the main window, showing all currently
    loaded GPX tracks overlaid on OpenStreetMap tiles — each track in its own
    color, with a start (circle) and end (square) marker. Supports drag-to-pan
    and zoom (buttons, mouse wheel, or trackpad).

    Tiles are cached to disk (tile_cache/) so re-opening the preview or
    revisiting an area doesn't re-download tiles — please be considerate of
    the free OpenStreetMap tile server if you change OSM_TILE_URL."""

    def __init__(self, master_app):
        super().__init__(master_app.root)
        self.master_app = master_app
        self.t = master_app.t
        self.title(self.t("preview_title"))

        mw = master_app.root
        mw.update_idletasks()
        x = mw.winfo_rootx() + mw.winfo_width() + 8
        y = mw.winfo_rooty()
        self.geometry(f"820x700+{x}+{y}")
        self.minsize(480, 360)

        controls = ttk.Frame(self, padding=6)
        controls.pack(fill="x")
        ttk.Button(controls, text="−", width=3, command=self.zoom_out).pack(side="left")
        ttk.Button(controls, text="+", width=3, command=self.zoom_in).pack(side="left", padx=(4, 10))
        ttk.Button(controls, text=self.t("fit_all"), command=self.fit_bounds).pack(side="left")
        ttk.Button(controls, text=self.t("reload_tracks"), command=self.reload_tracks).pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.status_var, foreground="#555").pack(side="left", padx=12)
        ttk.Label(controls, text="© OpenStreetMap contributors", foreground="#888").pack(side="right")

        self.canvas = tk.Canvas(self, background="#dcdcdc", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.legend_frame = ttk.Frame(self, padding=8, width=190)
        self.legend_frame.pack(side="right", fill="y")
        self.legend_frame.pack_propagate(False)
        self.legend_title = ttk.Label(self.legend_frame, font=("TkDefaultFont", 9, "bold"))
        self.legend_title.pack(anchor="w", pady=(0, 6))
        self.legend_rows_frame = ttk.Frame(self.legend_frame)
        self.legend_rows_frame.pack(fill="both", expand=True)

        self.tile_images = {}  # kept alive across a redraw so Tk doesn't drop them
        self.tracks = []
        self.center_lon, self.center_lat = 11.5820, 48.1351  # fallback: Munich
        self.zoom = 12
        self._drag_origin = None
        self._redraw_generation = 0
        self._resize_job = None

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)      # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self.zoom_in())   # Linux scroll up
        self.canvas.bind("<Button-5>", lambda e: self.zoom_out())  # Linux scroll down

        self.reload_tracks()

    # -- track loading --
    def reload_tracks(self):
        self.tracks = []
        for i, f in enumerate(self.master_app.files):
            points = parse_trkpts(f["content"])
            if points:
                color = TRACK_COLOR_PALETTE[i % len(TRACK_COLOR_PALETTE)]
                self.tracks.append({"points": points, "color": color, "label": os.path.basename(f["path"])})
        if not self.tracks:
            self.status_var.set(self.t("no_tracks_status"))
        self._rebuild_legend()
        self.fit_bounds()

    def _rebuild_legend(self):
        self.legend_title.config(text=self.t("legend_title"))
        for child in self.legend_rows_frame.winfo_children():
            child.destroy()
        for tr in self.tracks:
            row = ttk.Frame(self.legend_rows_frame)
            row.pack(fill="x", pady=2, anchor="w")
            swatch = tk.Canvas(row, width=14, height=14, highlightthickness=0)
            swatch.create_oval(1, 1, 13, 13, fill=tr["color"], outline="")
            swatch.pack(side="left", padx=(0, 6))
            ttk.Label(row, text=tr["label"], wraplength=150, justify="left").pack(side="left", fill="x")

    # -- view control --
    def fit_bounds(self):
        pts = [p for tr in self.tracks for p in tr["points"]]
        if pts:
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            self.center_lat = (min_lat + max_lat) / 2
            self.center_lon = (min_lon + max_lon) / 2

            self.canvas.update_idletasks()
            w = max(self.canvas.winfo_width(), 400)
            h = max(self.canvas.winfo_height(), 300)
            chosen = OSM_MIN_ZOOM
            for z in range(OSM_MAX_ZOOM, OSM_MIN_ZOOM - 1, -1):
                x0, y0 = lonlat_to_pixel(min_lon, max_lat, z)
                x1, y1 = lonlat_to_pixel(max_lon, min_lat, z)
                if abs(x1 - x0) <= w * 0.85 and abs(y1 - y0) <= h * 0.85:
                    chosen = z
                    break
            self.zoom = chosen
        self._redraw()

    def zoom_in(self):
        if self.zoom < OSM_MAX_ZOOM:
            self.zoom += 1
            self._redraw()

    def zoom_out(self):
        if self.zoom > OSM_MIN_ZOOM:
            self.zoom -= 1
            self._redraw()

    def _on_mousewheel(self, event):
        (self.zoom_in if event.delta > 0 else self.zoom_out)()

    def _on_drag_start(self, event):
        self._drag_origin = (event.x, event.y, self.center_lon, self.center_lat)

    def _on_drag_move(self, event):
        if not self._drag_origin:
            return
        ox, oy, olon, olat = self._drag_origin
        cx, cy = lonlat_to_pixel(olon, olat, self.zoom)
        nx, ny = cx - (event.x - ox), cy - (event.y - oy)
        self.center_lon, self.center_lat = pixel_to_lonlat(nx, ny, self.zoom)
        self._redraw()

    def _on_resize(self, event):
        # Debounce: redraw once the window has settled, not on every pixel of a drag-resize.
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self._redraw)

    # -- drawing --
    def _redraw(self):
        self.canvas.delete("all")
        self.tile_images = {}
        self._redraw_generation += 1
        gen = self._redraw_generation

        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 10 or h < 10:
            return

        cx, cy = lonlat_to_pixel(self.center_lon, self.center_lat, self.zoom)
        top_left_x = cx - w / 2
        top_left_y = cy - h / 2

        n_tiles = 2 ** self.zoom
        tile_min_x = int(top_left_x // OSM_TILE_SIZE)
        tile_max_x = int((top_left_x + w) // OSM_TILE_SIZE)
        tile_min_y = max(0, int(top_left_y // OSM_TILE_SIZE))
        tile_max_y = min(n_tiles - 1, int((top_left_y + h) // OSM_TILE_SIZE))

        for tx in range(tile_min_x, tile_max_x + 1):
            for ty in range(tile_min_y, tile_max_y + 1):
                px = tx * OSM_TILE_SIZE - top_left_x
                py = ty * OSM_TILE_SIZE - top_left_y
                self._place_tile(self.zoom, tx % n_tiles, ty, px, py, gen)

        # Three separate passes so stacking order is guaranteed regardless of
        # how many tracks there are: lines at the bottom, then every start
        # marker, then every finish flag on top of ALL of them (so a finish
        # point is never hidden under another track's start marker).
        track_screen_points = []  # cached per-track screen coords, reused below
        for tr in self.tracks:
            coords = []
            for lat, lon in tr["points"]:
                px, py = lonlat_to_pixel(lon, lat, self.zoom)
                coords.extend([px - top_left_x, py - top_left_y])
            track_screen_points.append(coords)
            if len(coords) >= 4:
                self.canvas.create_line(*coords, fill=tr["color"], width=3,
                                         capstyle="round", joinstyle="round", tags="track")

        for tr, coords in zip(self.tracks, track_screen_points):
            if len(coords) >= 2:
                self._draw_start_marker(coords[0], coords[1], tr["color"])

        for tr, coords in zip(self.tracks, track_screen_points):
            if len(coords) >= 2:
                self._draw_finish_marker(coords[-2], coords[-1])

        self.status_var.set(self.t("zoom_status").format(z=self.zoom) if self.tracks else self.t("no_tracks_status"))

    def _draw_start_marker(self, x, y, color):
        r = 6
        self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="white", width=2,
                                 tags="marker-start")

    def _draw_finish_marker(self, x, y):
        # A slightly larger font so the flag reads clearly, with the colored
        # start dot able to show through its transparent corners underneath.
        self.canvas.create_text(x, y, text="🏁", font=("TkDefaultFont", 18), anchor="center",
                                 tags="marker-finish")

    # -- tiles: disk cache + background download --
    def _place_tile(self, z, x, y, px, py, gen):
        cache_path = os.path.join(TILE_CACHE_DIR, str(z), str(x), f"{y}.png")
        if os.path.exists(cache_path):
            self._draw_tile_image(cache_path, px, py)
        else:
            threading.Thread(target=self._download_tile, args=(z, x, y, px, py, gen, cache_path),
                              daemon=True).start()

    def _download_tile(self, z, x, y, px, py, gen, cache_path):
        url = OSM_TILE_URL.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": "GPXCombiner/1.0 (local desktop app)"})
        try:
            with urlopen(req, timeout=10) as resp:
                data = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "wb") as f:
                f.write(data)
        except OSError:
            pass
        self.after(0, self._on_tile_ready, cache_path, px, py, gen)

    def _on_tile_ready(self, cache_path, px, py, gen):
        if gen != self._redraw_generation:
            return  # the view has moved on; discard this now-irrelevant tile
        try:
            self._draw_tile_image(cache_path, px, py)
        except tk.TclError:
            pass  # window/canvas was closed in the meantime

    def _draw_tile_image(self, cache_path, px, py):
        try:
            img = tk.PhotoImage(file=cache_path)
        except tk.TclError:
            return
        self.tile_images[(px, py, id(img))] = img  # keep a reference alive
        item_id = self.canvas.create_image(px, py, anchor="nw", image=img, tags="tile")
        self.canvas.tag_lower(item_id)  # tiles always stay under tracks/markers


# ----------------------------------------------------------------------------
# Strava credentials dialog + settings/disconnect window
# ----------------------------------------------------------------------------

class StravaCredentialsDialog(tk.Toplevel):
    """Modal dialog asking for the Strava Client ID + Client Secret in one
    window, with an explanation of where to find them. Sets .result to
    (client_id, client_secret) on OK, or leaves it None if cancelled/closed."""

    def __init__(self, master, t):
        super().__init__(master)
        self.t = t
        self.result = None
        self.title(self.t("strava_credentials_title"))
        self.resizable(False, False)
        self.transient(master)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=self.t("strava_credentials_info"), justify="left",
                  wraplength=380).pack(anchor="w", pady=(0, 10))

        link = ttk.Label(frame, text=self.t("open_strava_dev_site"), foreground="#1a73e8", cursor="hand2")
        link.pack(anchor="w", pady=(0, 14))
        link.bind("<Button-1>", lambda e: webbrowser.open("https://www.strava.com/settings/api"))

        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text=self.t("ask_client_id")).grid(row=0, column=0, sticky="w", pady=4)
        self.id_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.id_var, width=26).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form, text=self.t("ask_client_secret")).grid(row=1, column=0, sticky="w", pady=4)
        self.secret_var = tk.StringVar()
        self.secret_entry = ttk.Entry(form, textvariable=self.secret_var, width=26, show="•")
        self.secret_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self._secret_shown = False
        self.toggle_btn = ttk.Button(form, text=self.t("show_secret"), width=3, command=self._toggle_secret)
        self.toggle_btn.grid(row=1, column=2, padx=(4, 0))

        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text=self.t("cancel_btn"), command=self._on_cancel).pack(side="right")
        ttk.Button(btns, text=self.t("ok_btn"), command=self._on_ok).pack(side="right", padx=(0, 6))

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self._on_cancel())

        self.grab_set()
        self.id_entry_focus_target = form
        form.winfo_children()[1].focus_set()

    def _toggle_secret(self):
        self._secret_shown = not self._secret_shown
        self.secret_entry.config(show="" if self._secret_shown else "•")
        self.toggle_btn.config(text=self.t("hide_secret") if self._secret_shown else self.t("show_secret"))

    def _on_ok(self):
        cid = self.id_var.get().strip()
        secret = self.secret_var.get().strip()
        if not cid or not secret:
            messagebox.showwarning(self.t("strava_credentials_title"),
                                    self.t("strava_credentials_missing"), parent=self)
            return
        self.result = (cid, secret)
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


class StravaSettingsWindow(tk.Toplevel):
    """Shows the currently saved Strava account (if any) and lets the user
    fully erase the saved Client ID / Client Secret / tokens."""

    def __init__(self, master_app):
        super().__init__(master_app.root)
        self.master_app = master_app
        self.t = master_app.t
        self.title(self.t("strava_settings_title"))
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        self.status_label = ttk.Label(frame, justify="left", wraplength=360)
        self.status_label.pack(anchor="w", pady=(0, 14))
        self.status_label.config(text=self.t("loading"))

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        self.connect_btn = ttk.Button(btns, text=self.t("connect_strava"), command=self._connect)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(btns, text=self.t("disconnect_strava"), command=self._disconnect)
        self.disconnect_btn.pack(side="left", padx=(6, 0))
        ttk.Button(btns, text=self.t("close"), command=self.destroy).pack(side="right")

        self.after(50, self._refresh_status)

    def _refresh_status(self):
        config = self.master_app.strava_config
        if not config.get("client_id"):
            self.status_label.config(text=self.t("strava_not_configured"))
            self.disconnect_btn.state(["disabled"])
            self.connect_btn.state(["!disabled"])
            return

        self.connect_btn.state(["disabled"])
        self.disconnect_btn.state(["!disabled"])
        lines = [self.t("strava_client_id_label").format(id=config.get("client_id"))]
        if config.get("refresh_token"):
            name = self._fetch_athlete_name(config)
            lines.append(self.t("strava_connected_as").format(name=name) if name
                         else self.t("strava_authorized_unknown"))
        else:
            lines.append(self.t("strava_not_authorized"))
        self.status_label.config(text="\n".join(lines))

    def _connect(self):
        client = self.master_app.get_strava_client()
        if client is None:
            return  # user cancelled the credentials dialog again — stay put, no error
        if not client.has_token:
            try:
                client.authorize_interactive()
            except StravaAuthError as e:
                messagebox.showerror(self.t("auth_failed_title"),
                                      self.t("auth_failed_body").format(err=e), parent=self)
        self._refresh_status()

    def _fetch_athlete_name(self, config):
        try:
            client = StravaClient(dict(config))
            data = client._get("/athlete")
        except Exception:
            return None
        name = f"{data.get('firstname', '')} {data.get('lastname', '')}".strip()
        return name or None

    def _disconnect(self):
        if not messagebox.askyesno(self.t("confirm_disconnect_title"), self.t("confirm_disconnect_body"),
                                    parent=self):
            return
        try:
            if os.path.exists(CONFIG_PATH):
                os.remove(CONFIG_PATH)
        except OSError as e:
            messagebox.showerror(self.t("strava_settings_title"), str(e), parent=self)
            return
        self.master_app.strava_config = {}
        messagebox.showinfo(self.t("disconnected_title"), self.t("disconnected_body"), parent=self)
        self.destroy()


# ----------------------------------------------------------------------------
# Upload progress window
# ----------------------------------------------------------------------------

class UploadOptionsDialog(tk.Toplevel):
    """Asks for the activity name and its Strava type together, the type
    pre-selected from whatever the combined GPX's own <type> tag already
    says. Sets .result to (name, gpx_type) on OK, leaves it None on Cancel."""

    def __init__(self, master_app, default_name, default_type):
        super().__init__(master_app.root)
        self.t = master_app.t
        self.result = None
        self.title(self.t("upload_name_title"))
        self.resizable(False, False)
        self.transient(master_app.root)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text=self.t("upload_name_prompt")).grid(row=0, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=default_name)
        ttk.Entry(form, textvariable=self.name_var, width=30).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form, text=self.t("upload_type_prompt")).grid(row=1, column=0, sticky="w", pady=4)
        self.type_var = tk.StringVar(value=default_type if default_type in GPX_TYPE_CHOICES else "")
        type_combo = ttk.Combobox(form, textvariable=self.type_var, values=GPX_TYPE_CHOICES,
                                   state="readonly", width=27)
        type_combo.grid(row=1, column=1, sticky="ew", pady=4)

        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text=self.t("cancel_btn"), command=self._on_cancel).pack(side="right")
        ttk.Button(btns, text=self.t("ok_btn"), command=self._on_ok).pack(side="right", padx=(0, 6))

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.grab_set()

    def _on_ok(self):
        self.result = (self.name_var.get().strip(), self.type_var.get().strip())
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


class UploadProgressWindow(tk.Toplevel):
    """Uploads a GPX file to Strava in a background thread, polling
    check_upload() until Strava finishes processing it (or reports an
    error, e.g. a duplicate of an existing activity)."""

    def __init__(self, master_app, client, filepath, name):
        super().__init__(master_app.root)
        self.master_app = master_app
        self.t = master_app.t
        self.title(self.t("upload_progress_title"))
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # ignore close while uploading

        frame = ttk.Frame(self, padding=16)
        frame.pack()
        self.status_var = tk.StringVar(value=self.t("upload_status_uploading"))
        ttk.Label(frame, textvariable=self.status_var, wraplength=320, justify="left").pack()

        btn_row = ttk.Frame(frame)
        btn_row.pack(pady=(14, 0))
        self.view_btn = ttk.Button(btn_row, text=self.t("upload_view_on_strava"), command=self._open_activity)
        self.close_btn = ttk.Button(btn_row, text=self.t("close"), command=self._close, state="disabled")
        self.close_btn.pack(side="right")
        self._activity_id = None

        threading.Thread(target=self._run, args=(client, filepath, name), daemon=True).start()

    def _run(self, client, filepath, name):
        try:
            upload_id = client.upload_gpx(filepath, name=name)
        except (StravaAPIError, StravaAuthError, OSError) as e:
            self.after(0, self._on_error, str(e))
            return

        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            try:
                status = client.check_upload(upload_id)
            except (StravaAPIError, StravaAuthError) as e:
                self.after(0, self._on_error, str(e))
                return
            if status.get("error"):
                self.after(0, self._on_error, status["error"])
                return
            if status.get("activity_id"):
                self.after(0, self._on_success, status["activity_id"])
                return
            self.after(0, self._on_progress, status.get("status", ""))

        self.after(0, self._on_error, self.t("upload_status_timeout"))

    def _on_progress(self, status_text):
        self.status_var.set(status_text or self.t("upload_status_processing"))

    def _on_success(self, activity_id):
        self._activity_id = activity_id
        self.status_var.set(self.t("upload_status_done"))
        self.view_btn.pack(side="left")
        self.close_btn.state(["!disabled"])
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _on_error(self, err):
        self.status_var.set(self.t("upload_status_error").format(err=err))
        self.close_btn.state(["!disabled"])
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _open_activity(self):
        if self._activity_id:
            webbrowser.open_new_tab(f"https://www.strava.com/activities/{self._activity_id}/overview")

    def _close(self):
        self.destroy()


# ----------------------------------------------------------------------------
# Main app
# ----------------------------------------------------------------------------

class GpxCombinerApp:
    def __init__(self, root):
        self.root = root
        self.app_config = self._load_json(APP_CONFIG_PATH)
        self.lang = self.app_config.get("language") if self.app_config.get("language") in TR else "en"
        self.files = []
        self.strava_config = self._load_json(CONFIG_PATH)
        self._preview_window = None

        self._build_ui()
        self._retranslate()

    def t(self, key):
        return TR[self.lang][key]

    def _load_json(self, path):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_app_config(self):
        try:
            with open(APP_CONFIG_PATH, "w") as f:
                json.dump(self.app_config, f)
        except OSError:
            pass

    def get_strava_client(self):
        """Returns a ready StravaClient, prompting for credentials if needed.
        Returns None if the user cancels the credentials dialog — callers
        must check for this instead of proceeding (this was the bug behind
        the old "Cancel still continues, then times out" behavior)."""
        client = StravaClient(self.strava_config)
        if not client.has_credentials:
            dlg = StravaCredentialsDialog(self.root, self.t)
            self.root.wait_window(dlg)
            if dlg.result is None:
                return None
            client.config["client_id"], client.config["client_secret"] = dlg.result
            client.save()
        return client

    def _build_ui(self):
        self.root.geometry("1125x550")
        self.root.minsize(760, 400)
        self.root.resizable(True, True)

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        self.btn_add = ttk.Button(top, command=self.add_files)
        self.btn_add.pack(side="left")
        self.btn_remove = ttk.Button(top, command=self.remove_selected)
        self.btn_remove.pack(side="left", padx=6)
        self.btn_clear = ttk.Button(top, command=self.clear_all)
        self.btn_clear.pack(side="left")
        self.btn_strava = ttk.Button(top, command=self.open_strava_import)
        self.btn_strava.pack(side="left", padx=6)
        self.btn_strava_settings = ttk.Button(top, command=self.open_strava_settings)
        self.btn_strava_settings.pack(side="left")
        self.btn_preview = ttk.Button(top, command=self.open_preview)
        self.btn_preview.pack(side="left", padx=(6, 0))

        lang_frame = ttk.Frame(top)
        lang_frame.pack(side="right")
        self.lang_label = ttk.Label(lang_frame)
        self.lang_label.pack(side="left", padx=(0, 4))
        self.lang_combo = ttk.Combobox(lang_frame, state="readonly", width=10,
                                        values=list(LANGUAGE_NAMES.values()))
        self.lang_combo.set(LANGUAGE_NAMES[self.lang])
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_change)
        self.lang_combo.pack(side="left")

        mid = ttk.Frame(self.root, padding=(10, 0))
        mid.pack(fill="both", expand=True)

        self.order_label = ttk.Label(mid)
        self.order_label.pack(anchor="w")

        self.listbox = tk.Listbox(mid, height=14)
        self.listbox.pack(fill="both", expand=True, pady=(4, 0))

        self.dnd_hint_label = ttk.Label(mid, foreground="#888", font=("TkDefaultFont", 9, "italic"))
        self.dnd_hint_label.pack(anchor="w", pady=(4, 0))

        if DND_AVAILABLE:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop_files)
            self.listbox.drop_target_register(DND_FILES)
            self.listbox.dnd_bind("<<Drop>>", self._on_drop_files)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill="x")
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var, foreground="#555").pack(anchor="w")

        fields_row = ttk.Frame(bottom)
        fields_row.pack(anchor="e", pady=(6, 0))
        self.include_fields_label = ttk.Label(fields_row)
        self.include_fields_label.pack(side="left", padx=(0, 8))
        self.include_field_vars = {}
        self.include_field_checks = {}
        for key in EXTENSION_FIELDS:
            var = tk.BooleanVar(value=True)
            self.include_field_vars[key] = var
            cb = ttk.Checkbutton(fields_row, text=EXTENSION_FIELD_LABELS[key], variable=var)
            cb.pack(side="left", padx=(0, 10))
            self.include_field_checks[key] = cb

        self.btn_combine = ttk.Button(bottom, command=self.combine_and_save)
        self.btn_combine.pack(anchor="e", pady=(8, 0))

        self.btn_upload = ttk.Button(bottom, command=self.upload_to_strava, state="disabled")
        self.btn_upload.pack(anchor="e", pady=(4, 0))

    def _on_language_change(self, event=None):
        name_to_code = {v: k for k, v in LANGUAGE_NAMES.items()}
        self.lang = name_to_code[self.lang_combo.get()]
        self.app_config["language"] = self.lang
        self._save_app_config()
        self._retranslate()

    def _retranslate(self):
        self.root.title(f"{self.t('app_title')} v{APP_VERSION}")
        self.btn_add.config(text=self.t("add_files"))
        self.btn_remove.config(text=self.t("remove_selected"))
        self.btn_clear.config(text=self.t("clear_all"))
        self.btn_strava.config(text=self.t("import_strava"))
        self.btn_strava_settings.config(text=self.t("strava_settings_btn"))
        self.btn_preview.config(text=self.t("preview_btn"))
        self.lang_label.config(text=self.t("language"))
        self.order_label.config(text=self.t("order_label"))
        self.btn_combine.config(text=self.t("combine_save"))
        self.btn_upload.config(text=self.t("upload_btn"))
        self.dnd_hint_label.config(text=self.t("dnd_hint") if DND_AVAILABLE else "")
        self.include_fields_label.config(text=self.t("include_fields_label"))
        self._refresh_status()

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title=self.t("add_files"),
            filetypes=[("GPX", "*.gpx"), ("*", "*.*")],
        )
        if paths:
            self.add_files_from_paths(paths)

    def _on_drop_files(self, event):
        # event.data is a Tcl list of paths (braces around any path containing spaces)
        paths = self.root.tk.splitlist(event.data)
        gpx_paths = [p for p in paths if p.lower().endswith(".gpx")]
        if gpx_paths:
            self.add_files_from_paths(gpx_paths)

    def add_files_from_paths(self, paths):
        for path in paths:
            if any(f["path"] == path for f in self.files):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                messagebox.showerror(self.t("err_read_title"),
                                      self.t("err_read_body").format(path=path, err=e))
                continue
            sort_key = extract_sort_key(content, fallback=os.path.basename(path))
            ext_fields = detect_extension_fields(content)
            self.files.append({"path": path, "content": content, "sort_key": sort_key,
                                "ext_fields": ext_fields})
        self.refresh_list()

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        sel_indices = set(sel)
        self.files = [f for i, f in enumerate(self.files) if i not in sel_indices]
        self.refresh_list()

    def clear_all(self):
        self.files = []
        self.refresh_list()

    def refresh_list(self):
        self.files.sort(key=lambda f: f["sort_key"])
        self.listbox.delete(0, tk.END)
        for i, f in enumerate(self.files, start=1):
            label = f["sort_key"] or self.t("unknown_time")
            line = f"{i}. {label}  —  {f['path']}"
            gpx_type = read_gpx_type(f["content"])
            badges = "  ".join(f"{EXTENSION_FIELD_LABELS[k]} ✅"
                                for k in EXTENSION_FIELDS if f.get("ext_fields", {}).get(k))
            extras = "   |   ".join(x for x in (gpx_type, badges) if x)
            if extras:
                line += f"   |   {extras}"
            self.listbox.insert(tk.END, line)
        self._refresh_status()
        self.btn_upload.state(["!disabled"] if len(self.files) >= 2 else ["disabled"])
        if self._preview_window is not None and self._preview_window.winfo_exists():
            self._preview_window.reload_tracks()

    def _refresh_status(self):
        n = len(self.files)
        if n == 0:
            self.status_var.set(self.t("status_none"))
        elif n == 1:
            self.status_var.set(self.t("status_one"))
        else:
            self.status_var.set(self.t("status_multiple").format(n=n))

    def open_strava_import(self):
        client = self.get_strava_client()
        if client is None:
            return  # user cancelled the credentials dialog
        StravaImportWindow(self, client)

    def open_strava_settings(self):
        StravaSettingsWindow(self)

    def open_preview(self):
        if self._preview_window is not None and self._preview_window.winfo_exists():
            self._preview_window.reload_tracks()
            self._preview_window.lift()
            self._preview_window.focus_force()
        else:
            self._preview_window = MapPreviewWindow(self)

    def _build_combined_gpx(self):
        """Builds the combined GPX text from the current self.files + the
        include-checkboxes, without touching disk. Used by both "Combine
        and save" (writes wherever the user chooses) and "Upload to Strava"
        (always builds fresh into a throwaway temp file at click time, so
        an upload can never be stale relative to whatever's currently
        loaded). Returns None (after showing its own error dialog) on
        failure."""
        if len(self.files) < 2:
            messagebox.showwarning(self.t("warn_notenough_title"), self.t("warn_notenough_body"))
            return None

        include = {k: v.get() for k, v in self.include_field_vars.items()}

        base = self.files[0]
        base_content = strip_extension_fields(base["content"], include)
        marker = "</trkseg>"
        idx = base_content.find(marker)
        if idx == -1:
            messagebox.showerror(self.t("err_base_title"),
                                  self.t("err_base_body").format(path=base["path"]))
            return None
        insert_pos = idx + len(marker)

        extra_segments, skipped = [], []
        for f in self.files[1:]:
            seg = extract_first_trkseg(f["content"])
            if seg is None:
                skipped.append(f["path"])
                continue
            extra_segments.append(strip_extension_fields(seg, include))

        if skipped:
            messagebox.showwarning(self.t("warn_skipped_title"),
                                    self.t("warn_skipped_body").format(files="\n".join(skipped)))

        if not extra_segments:
            messagebox.showerror(self.t("err_nothing_title"), self.t("err_nothing_body"))
            return None

        return base_content[:insert_pos] + "\n" + "\n".join(extra_segments) + base_content[insert_pos:]

    def combine_and_save(self):
        combined = self._build_combined_gpx()
        if combined is None:
            return

        save_path = filedialog.asksaveasfilename(
            title=self.t("save_title"), defaultextension=".gpx",
            initialfile="combined.gpx", filetypes=[("GPX", "*.gpx")],
        )
        if not save_path:
            return
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(combined)
        except OSError as e:
            messagebox.showerror(self.t("err_write_title"), self.t("err_write_body").format(err=e))
            return

        messagebox.showinfo(self.t("done_title"), self.t("done_body").format(path=save_path))
        self.status_var.set(self.t("status_done").format(path=save_path))

    def upload_to_strava(self):
        # Duplicate-avoidance: files this app itself downloaded from Strava
        # are named "<name>_<activity_id>.gpx" — pull those IDs back out so
        # we can offer to open the originals for review/deletion first
        # (Strava's own upload dedupe would otherwise reject a re-upload of
        # the same recording, and deleting isn't possible via the API).
        original_ids = []
        for f in self.files:
            m = re.search(r"_(\d+)\.gpx$", os.path.basename(f["path"]))
            if m:
                original_ids.append(m.group(1))

        if original_ids:
            if messagebox.askyesno(self.t("open_originals_title"),
                                    self.t("open_originals_body").format(n=len(original_ids))):
                for aid in original_ids:
                    webbrowser.open_new_tab(f"https://www.strava.com/activities/{aid}/overview")

        client = self.get_strava_client()
        if client is None:
            return  # user cancelled the credentials dialog
        if not client.has_token:
            try:
                client.authorize_interactive()
            except StravaAuthError as e:
                messagebox.showerror(self.t("auth_failed_title"),
                                      self.t("auth_failed_body").format(err=e))
                return

        # Build fresh from whatever's currently loaded — never reuses a
        # previously-saved file, so there's no way for this to upload
        # stale/unrelated content from an earlier combine.
        combined_content = self._build_combined_gpx()
        if combined_content is None:
            return
        default_type = read_gpx_type(combined_content)
        default_name = " + ".join(os.path.splitext(os.path.basename(f["path"]))[0] for f in self.files)

        dlg = UploadOptionsDialog(self, default_name, default_type)
        self.root.wait_window(dlg)
        if dlg.result is None:
            return  # cancelled
        name, gpx_type = dlg.result

        if gpx_type and gpx_type != default_type:
            combined_content = set_gpx_type(combined_content, gpx_type)

        os.makedirs(GPX_TEMP_DIR, exist_ok=True)
        upload_path = os.path.join(GPX_TEMP_DIR, f"upload_{uuid.uuid4().hex}.gpx")
        try:
            with open(upload_path, "w", encoding="utf-8") as f:
                f.write(combined_content)
        except OSError as e:
            messagebox.showerror(self.t("strava_title"), str(e))
            return

        UploadProgressWindow(self, client, upload_path, name)


def main():
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    try:
        style = ttk.Style()
        # "aqua" is Tk's native macOS theme (real NSButton/NSPopUpButton look —
        # built into Tk, so no extra weight). Previously this was overridden
        # with "clam", which replaces the native Mac widgets with a generic
        # cross-platform look. Prefer aqua when available; fall back to clam
        # elsewhere (Windows/Linux) for a slightly more modern flat look than
        # the default "default" theme.
        if "aqua" in style.theme_names():
            style.theme_use("aqua")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass

    if sys.platform == "win32":
        ico_path = resource_path("icon.ico")
        if os.path.exists(ico_path):
            try:
                root.iconbitmap(ico_path)
            except tk.TclError:
                pass  # e.g. a malformed .ico — window still works, just without an icon

    GpxCombinerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
