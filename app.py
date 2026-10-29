# -*- coding: utf-8 -*-
"""
AutoDirector Pro 1.0
====================
Automatiza la edición de podcasts multicámara: analiza qué micrófono suena
más fuerte en cada segundo y corta el video hacia esa cámara.

Novedades de la versión 2.0
---------------------------
 1. Exportación XML (formato Final Cut Pro 7 "xmeml") para abrir la línea de
    tiempo ya cortada en Premiere Pro o DaVinci Resolve.
 2. Botón CANCELAR funcional (análisis, montaje y renderizado).
 3. Aceleración por GPU (NVENC / AMF / QuickSync / VideoToolbox) con
    detección automática y fallback a CPU (libx264).
 4. Línea de tiempo visual (vista previa) con quién habla y dónde se corta.
    Se redibuja en vivo al mover los sliders.
 5. Cortes naturales: duración mínima de plano + solapamiento de audio
    (el audio de quien va a hablar entra ANTES del cambio de cámara).
 6. Filtro de voz opcional (solo cuenta la energía entre 85 y 3400 Hz).
 7. Borrador de CapCut PC (carpeta de proyecto con la línea de tiempo ya
    cortada) generado con la librería pycapcut.

Idiomas
-------
El programa está en español e inglés. El selector está arriba a la derecha; la
elección se guarda en %APPDATA%\\AutoDirectorPro\\settings.json y, la primera vez,
se usa el idioma de Windows. Todos los textos están escritos en español en el
código y T() los traduce con el diccionario EN (sección «IDIOMAS»).

Novedades de la versión 2.1
---------------------------
 8. Editar los cortes a mano: clic (o arrastre) sobre «Resultado» en la línea
    de tiempo y elegir qué cámara se ve en ese tramo.
 9. Sincronización automática de cámaras por audio (compara el sonido de cada
    archivo y recorta el inicio de los que empezaron antes).
10. Micrófonos que «oyen» al otro: regla de ventaja en dB y equilibrio de
    volumen entre micrófonos.
11. Formato vertical 9:16 (recorte con encuadre por cámara) además del horizontal.
12. «Probar 30 s»: renderiza en baja resolución el tramo con más cambios.
13. «Copiar informe de error»: deja en el portapapeles todo lo necesario para
    dar soporte (sin rutas personales).

Dependencias:
    pip install customtkinter pillow pydub moviepy numpy
    pip install pycapcut==0.0.4      (solo si vas a exportar borrador de CapCut)
    FFmpeg ya NO hace falta instalarlo: se usa el que trae imageio-ffmpeg
    (dependencia de MoviePy), y también queda dentro del .exe.

Estructura del archivo:
    1. Importaciones (con compatibilidad MoviePy v1 / v2)
    2. Constantes y utilidades generales
    3. Análisis de audio y plan de cortes (funciones puras)
    4. Audio con crossfade (J-cut)
    5. Exportación XML (FCP7 / xmeml)
    6. Detección de GPU
    7. Logger de progreso (con soporte de cancelación)
    8. Widget CameraCard
    9. Clase principal AutoDirectorApp (UI + hilo de trabajo)
   10. Punto de entrada
"""

# =============================================================================
# 1. IMPORTACIONES
# =============================================================================
import os
import sys

# En un .exe sin consola (PyInstaller --noconsole) stdout/stderr valen None y
# algunas librerías (tqdm, warnings) fallan al escribir. Se redirigen a "nada".
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import bisect
import collections
import datetime
import hashlib
import io
import json
import math
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import tkinter as tk
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np
from PIL import Image
from pydub import AudioSegment

# --- Compatibilidad MoviePy: v2 (`moviepy`) y v1 (`moviepy.editor`) ----------
try:
    from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips
except ImportError:
    from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips

# proglog es dependencia de MoviePy; lo usamos para leer el progreso del render
try:
    from proglog import ProgressBarLogger
except ImportError:  # pragma: no cover
    ProgressBarLogger = None


# =============================================================================
# 2. CONSTANTES Y UTILIDADES GENERALES
# =============================================================================
LOGO_URL = "https://i.postimg.cc/G3Vzpf2S/Hablemos-entonces.png"

# --- Versión, actualizaciones y donaciones (lo único que debes editar) ---------
APP_VERSION = "1.0.0"      # Súbela cada vez que publiques una versión nueva
UPDATE_REPO = ""           # Repositorio de GitHub donde publicas las versiones: "usuario/repositorio"
                           # (vacío = el programa no busca actualizaciones)
DONATE_URL = "https://www.paypal.com/ncp/payment/NMGRGDN7KXBD4"   # Botón de PayPal. Enlace de donaciones (Ko-fi, PayPal.me, Mercado Pago, Cafecito...)
                           # (vacío = el botón "Apoyar el proyecto" no se muestra)
INSTALLER_NAME = "Instalador-AutoDirectorPro.exe"
HELP_URL = "https://www.instagram.com/hablemos.entoncespro/"   # Botón "Ayuda" (mensaje directo)
CHUNK_MS = 1000                      # Fragmento de análisis: 1 segundo
VOICE_LOW, VOICE_HIGH = 85, 3400     # Banda de la voz humana (Hz) para el filtro de voz
ANALYSIS_RATE = 16000                # El análisis se hace en mono a 16 kHz (ahorra RAM)

# --- Sincronización de cámaras por audio -------------------------------------
SYNC_RATE = 8000                     # Frecuencia de muestreo con la que se compara el audio
ENV_HZ = 100                         # La envolvente de volumen se compara a 100 puntos por segundo
SYNC_MAX_S = 600                     # Solo se usan los primeros 10 minutos de cada pista
SYNC_MAX_LAG = 120                   # Desfase máximo que se busca (segundos)
SYNC_MIN_SCORE = 6.0                 # Confianza mínima para aceptar un desfase detectado

# --- Cortes, formato y prueba rápida -----------------------------------------
MARGIN_DEFAULT = 6                   # dB que debe ganar una voz para "dominar" cuando suenan varias
QUICK_SECONDS = 30                   # Duración de la prueba rápida
VERT_SIZE = (1080, 1920)             # Tamaño del video vertical 9:16

# --- Paleta pastel. Cada color es (modo claro, modo oscuro) -------------------
COL_BG = ("#F4F1F8", "#16141D")             # fondo de la ventana
COL_SURFACE = ("#FDFCFF", "#1F1C29")        # paneles
COL_SOFT = ("#F6F3FA", "#262335")           # tarjetas dentro de los paneles
COL_TRACK = ("#ECE8F4", "#2D2940")          # pistas vacías, barra de progreso
COL_BORDER = ("#E6E1F0", "#322E44")         # borde fino de los paneles
COL_LINE = ("#DAD4EA", "#3E3953")           # bordes de botones, interruptores apagados
COL_HOVER = ("#EFEBF7", "#2B2839")          # fondo al pasar el mouse
COL_INK = ("#3B3652", "#ECE9F5")            # texto principal
COL_MUTED = ("#8C87A6", "#9892B3")          # texto secundario
COL_ACCENT = ("#CFCAF8", "#9E93E6")         # botón principal
COL_ACCENT_HOVER = ("#C0BAF3", "#B0A6EE")
COL_ACCENT_INK = ("#2F2A4B", "#16141D")     # texto sobre el botón principal
COL_PROGRESS = ("#BDB5F2", "#7C70D0")       # parte llena de sliders y progreso
COL_KNOB = ("#A79CEB", "#B2A9F0")           # tirador de los sliders
COL_KNOB_HOVER = ("#968AE3", "#C4BCF5")
COL_WHITE = ("#FFFFFF", "#ECE9F5")          # bolita de los interruptores
COL_ROSE = ("#F7DCE1", "#4A2F3A")           # fondo suave de Cancelar / Quitar
COL_ROSE_INK = ("#8F3F52", "#F4B8C6")
COL_PEACH = ("#FBE6DA", "#4A3329")          # botón "Apoyar el proyecto"
COL_PEACH_HOVER = ("#F6D8C6", "#5A4033")
COL_PEACH_INK = ("#8A4B2B", "#F3C9B0")
COL_MINT = ("#E3F4EB", "#22352E")           # aviso de actualización
COL_MINT_HOVER = ("#D2ECDF", "#2D4A3E")
COL_MINT_INK = ("#2F5E4B", "#BFE8D5")

# Un color pastel por cámara: aparece en su tarjeta y en la línea de tiempo
CAM_COLORS = ["#B9AEF2", "#9ED9C6", "#F4BF9B", "#F2AFC4",
              "#A5CBF0", "#EDDA95", "#BCD8A0", "#D9B3EA"]
PASTEL_INK = "#3B3652"                      # texto sobre las barras pastel

FONT_BASE = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"


def ui_font(size=12, semi=False):
    """Fuente de la interfaz (Segoe UI en Windows; Tk elige otra si no existe)."""
    return ctk.CTkFont(family=FONT_SEMI if semi else FONT_BASE, size=size)


def pick(color):
    """Elige el valor claro u oscuro de un color (para el canvas, que no lo hace solo)."""
    return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]


# =============================================================================
# IDIOMAS (español / inglés)
#   Todos los textos del programa están escritos en español; T() los traduce
#   con el diccionario EN (más abajo) según el idioma activo.
# =============================================================================
LANG = "es"      # idioma activo: "es" o "en"
EN = {}           # texto en español -> texto en inglés (se llena en la sección EN)


def T(text, **kw):
    """Traduce un texto al idioma activo. Con kw rellena las {marcas} del texto."""
    out = EN.get(text, text) if LANG == "en" else text
    return out.format(**kw) if kw else out


def set_language(code):
    global LANG
    LANG = "en" if code == "en" else "es"


def settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "AutoDirectorPro", "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(**changes):
    """Guarda ajustes del usuario (el idioma). Si no se puede escribir, se ignora."""
    try:
        data = load_settings()
        data.update(changes)
        path = settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def detect_language():
    """Idioma guardado; si no hay, el del sistema (español si lo es; si no, inglés)."""
    saved = load_settings().get("language")
    if saved in ("es", "en"):
        return saved
    try:
        if os.name == "nt":
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return "es" if lang_id == 0x0A else "en"
    except Exception:
        pass
    try:
        import locale
        code = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
    except Exception:
        code = ""
    return "es" if code.startswith("es") else "en"


# --- Textos en inglés (clave = el texto en español tal como aparece en el código) ---
EN.update({
    # Cabecera y ventana
    "Cortes automáticos para podcasts con varias cámaras": "Automatic cuts for multi-camera podcasts",
    "Apoyar el proyecto": "Support the project",
    "Ayuda": "Help",
    "Claro": "Light",
    "Oscuro": "Dark",
    "Versión": "Version",
    "Buscar actualizaciones": "Check for updates",
    "Copiar informe de error": "Copy error report",
    "¡Informe copiado! Pégalo en el mensaje": "Report copied! Paste it in your message",
    "No pude copiar el informe: {e}": "Couldn't copy the report: {e}",
    "¿Copiar el informe de error para enviarlo por Instagram?":
        "Copy the error report to send it on Instagram?",
    # Actualizaciones
    "Buscando...": "Checking...",
    "No se pudo comprobar. Revisa tu conexión.": "Couldn't check. Check your connection.",
    "Ya tienes la última versión.": "You already have the latest version.",
    "Hay una versión nueva: {v}": "A new version is available: {v}",
    "Incluye mejoras y correcciones.": "Includes improvements and fixes.",
    "Actualizar ahora": "Update now",
    "Más tarde": "Later",
    "Descargando {p}%": "Downloading {p}%",
    "Abriendo instalador...": "Opening installer...",
    "No se pudo abrir el instalador: {e}": "Could not open the installer: {e}",
    "No se pudo descargar la actualización.": "The update could not be downloaded.",
    "¿Quieres abrir la página de descarga?": "Do you want to open the download page?",
    "El archivo descargado no coincide con el publicado.":
        "The downloaded file does not match the published one.",
    "Termina o cancela el trabajo en curso antes de actualizar.":
        "Finish or cancel the current job before updating.",
    # Cámaras
    "Cámaras": "Cameras",
    "Si una cámara no tiene audio, hace de plano general.":
        "A camera with no audio acts as the wide shot.",
    "Agregar cámara": "Add camera",
    "Anfitrión": "Host",
    "Invitado {n}": "Guest {n}",
    "Cámara {n}": "Camera {n}",
    "Nombre de la cámara": "Camera name",
    "Elegir video": "Choose video",
    "Falta el video": "Video missing",
    "Elegir audio": "Choose audio",
    "Audio opcional": "Optional audio",
    "Cambiar": "Change",
    "Quitar": "Remove",
    "Encuadre en vertical": "Vertical framing",
    "Izquierda": "Left",
    "Centro": "Center",
    "Derecha": "Right",
    "Selecciona el video": "Select the video",
    "Selecciona el audio del micrófono": "Select the microphone audio",
    "Video": "Video",
    "Audio": "Audio",
    "Todos": "All",
    "Debe existir al menos una cámara.": "There must be at least one camera.",
    "Todas las cámaras necesitan un video.": "Every camera needs a video.",
    # Línea de tiempo
    "Línea de tiempo": "Timeline",
    "Resultado": "Result",
    "Analiza el audio para ver quién habla y dónde caen los cortes.":
        "Analyze the audio to see who is talking and where the cuts fall.",
    "{n} cortes en {t}": "{n} cuts in {t}",
    "{n} s editados a mano": "{n} s edited by hand",
    "haz clic en «Resultado» para cambiar un tramo": "click \u201cResult\u201d to change a section",
    "Tramo {a} a {b}": "Section {a} to {b}",
    "Editor de cortes": "Cut editor",
    "Elige un momento con la barra y decide qué cámara se ve.": "Pick a moment with the bar and decide which camera is shown.",
    "Momento: {t}": "Moment: {t}",
    "Resultado en este momento": "Result at this moment",
    "Cargando imagen...": "Loading image...",
    "Sin imagen": "No image",
    "Inicio -1 s": "Start -1 s", "Inicio +1 s": "Start +1 s",
    "Fin -1 s": "End -1 s", "Fin +1 s": "End +1 s",
    "Corte anterior": "Previous cut", "Corte siguiente": "Next cut",
    "(editado a mano)": "(edited by hand)",
    "Cerrar": "Close",
    "Editar cortes con imágenes": "Edit cuts with images",
    "Primero analiza el audio (botón «Analizar y ver cortes»).": "First analyze the audio (\u201cAnalyze and see cuts\u201d button).",
    "Prueba rápida": "Quick test",
    "Preparando la prueba...": "Preparing the test...",
    "Reproducir": "Play", "Pausar": "Pause", "Repetir": "Replay",
    "Abrir en tu reproductor": "Open in your player",
    "Aquí se ve sin sonido; usa «Abrir en tu reproductor» para oírla.": "It plays without sound here; use \u201cOpen in your player\u201d to hear it.",
    "No se pudo mostrar la prueba aquí. Usa «Abrir en tu reproductor».": "The test could not be shown here. Use \u201cOpen in your player\u201d.",
    "Mostrar a «{name}»": "Show \u201c{name}\u201d",
    "Volver al corte automático (este tramo)": "Back to the automatic cut (this section)",
    "Quitar todas mis ediciones": "Remove all my edits",
    "Corte cambiado: de {a} a {b} se muestra a {name}.":
        "Cut changed: from {a} to {b} now shows {name}.",
    # Controles
    "Umbral de voz": "Voice threshold",
    "Ventaja para cambiar": "Switch margin",
    "Plano mínimo": "Minimum shot",
    "Solape de audio": "Audio overlap",
    "Generar": "Generate",
    "Ajustes": "Settings",
    "Video MP4": "MP4 video",
    "XML para Premiere o DaVinci": "XML for Premiere or DaVinci",
    "Borrador de CapCut": "CapCut draft",
    "Usar la GPU si hay": "Use the GPU if available",
    "Filtro de voz": "Voice filter",
    "Equilibrar micrófonos": "Balance microphones",
    "Sincronizar cámaras por audio": "Sync cameras by audio",
    "Horizontal 16:9": "Horizontal 16:9",
    "Vertical 9:16": "Vertical 9:16",
    "Cancelar": "Cancel",
    "Analizar y ver cortes": "Analyze and see cuts",
    "Probar 30 s": "Test 30 s",
    "Renderizar video final": "Render final video",
    "Listo.": "Ready.",
    "Cancelando...": "Cancelling...",
    "Cancelando, espera un momento...": "Cancelling, one moment...",
    # Mensajes de trabajo
    "Activa al menos «Video MP4», «XML para Premiere o DaVinci» o «Borrador de CapCut».":
        "Turn on at least \u201cMP4 video\u201d, \u201cXML for Premiere or DaVinci\u201d or \u201cCapCut draft\u201d.",
    "No encontré la carpeta de borradores de CapCut.\nSelecciónala a continuación (normalmente termina en com.lveditor.draft).":
        "I couldn't find the CapCut drafts folder.\nSelect it next (it usually ends in com.lveditor.draft).",
    "Carpeta de borradores de CapCut": "CapCut drafts folder",
    "Nombre base (se usa para el borrador y su audio)": "Base name (used for the draft and its audio)",
    "Guardar resultado": "Save result",
    "Análisis reutilizado.": "Analysis reused.",
    "Leyendo video {n}/{total}...": "Reading video {n}/{total}...",
    "Sincronizando cámaras ({i}/{n})...": "Syncing cameras ({i}/{n})...",
    "Sincronizado: {list} recortados al inicio.": "Synced: trimmed from the start: {list}.",
    "Sincronizado: las cámaras ya estaban alineadas.": "Synced: the cameras were already aligned.",
    "mic de {name} {v}": "{name} mic {v}",
    "{name}: el video no tiene audio; no se pudo sincronizar.":
        "{name}: the video has no audio; it could not be synced.",
    "{name}: no encontré sonido en común; se dejó sin mover.":
        "{name}: I found no sound in common; it was left unmoved.",
    "{name}: no pude alinear su micrófono con su cámara.":
        "{name}: I couldn't align its microphone with its camera.",
    "{name}: su micrófono no coincide con el audio de la cámara; se dejó sin mover.":
        "{name}: its microphone doesn't match the camera audio; it was left unmoved.",
    "Analizando audio de {name}...": "Analyzing audio of {name}...",
    "Mezclando audio de {name}...": "Mixing audio of {name}...",
    "Preparando audio de {name}...": "Preparing audio of {name}...",
    "Las cámaras no tienen ningún tramo en común. Revisa que sean de la misma grabación o desactiva la sincronización.":
        "The cameras have no section in common. Check that they come from the same recording, or turn off synchronization.",
    "Vista previa lista: {n} cortes. Haz clic en «Resultado» para cambiar un tramo.":
        "Preview ready: {n} cuts. Click \u201cResult\u201d to change a section.",
    "Aviso: {note}": "Note: {note}",
    "Prueba de {a} a {b}...": "Testing {a} to {b}...",
    "Prueba lista ({a} a {b}). Se muestra en una ventana de la app.":
        "Test ready ({a} to {b}). It is shown in an app window.",
    "Generando XML para Premiere / DaVinci...": "Generating XML for Premiere / DaVinci...",
    "(en horizontal; en Premiere usa Auto Reframe)": "(horizontal; in Premiere use Auto Reframe)",
    "Creando borrador de CapCut...": "Creating CapCut draft...",
    "(vertical: revisa el encuadre en CapCut)": "(vertical: check the framing in CapCut)",
    "Montando cortes de video...": "Assembling video cuts...",
    "¡Listo! Archivos generados:": "Done! Files created:",
    "Proceso cancelado.": "Process cancelled.",
    "Error durante el proceso: {e}": "Error during the process: {e}",
    "Buscando aceleración por GPU...": "Looking for GPU acceleration...",
    "Renderizando video... {p}%": "Rendering video... {p}%",
    "Renderizando con {enc}...": "Rendering with {enc}...",
    "Renderizando con CPU (libx264)...": "Rendering with CPU (libx264)...",
    "La GPU falló; reintentando con la CPU...": "The GPU failed; retrying with the CPU...",
    "No se encontró FFmpeg.": "FFmpeg was not found.",
    "No se pudo leer el audio de {name}.": "Could not read the audio of {name}.",
    "Falta la librería pycapcut en esta instalación.\nSi ejecutas el script: python -m pip install pycapcut==0.0.4\nSi usas el .exe: vuelve a generarlo con build_exe.bat":
        "The pycapcut library is missing from this installation.\nIf you run the script: python -m pip install pycapcut==0.0.4\nIf you use the .exe: rebuild it with build_exe.bat",
})


def resource_path(name):
    """Ruta de un archivo incluido en el programa (funciona en el .exe y en el script)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# =============================================================================
# ACTUALIZACIONES (se publica un version.json en GitHub; el programa lo lee)
# =============================================================================
USER_AGENT = {"User-Agent": "AutoDirectorPro"}


def parse_version(text):
    """'2.10.1' -> (2, 10, 1). Tolera una 'v' delante y partes que no son números."""
    parts = []
    for p in str(text).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


def is_https(url):
    return isinstance(url, str) and url.lower().startswith("https://")


def update_manifest_url():
    if not UPDATE_REPO:
        return ""
    return f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/version.json"


def fetch_update_info():
    """Lee version.json y devuelve los datos de la última versión publicada."""
    req = urllib.request.Request(update_manifest_url(), headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8-sig"))
    repo_url = f"https://github.com/{UPDATE_REPO}/releases/latest"
    return {
        "version": str(data["version"]).strip(),
        "notes": str(data.get("notes", ""))[:300],
        "notes_en": str(data.get("notes_en", ""))[:300],
        "installer_url": data.get("installer_url") or f"{repo_url}/download/{INSTALLER_NAME}",
        "page_url": data.get("page_url") or repo_url,
        "sha256": str(data.get("sha256", "")).strip().lower(),
        "donate_url": data.get("donate_url", ""),
    }


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url, dest, progress):
    """Descarga `url` en `dest` avisando el avance (0..1) con `progress`."""
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total)


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Rectángulo con esquinas redondeadas para tk.Canvas."""
    if x2 - x1 < 3 or y2 - y1 < 3:
        return canvas.create_rectangle(x1, y1, x2, y2, **kw)
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Cancelled(Exception):
    """Se lanza cuando el usuario pulsa 'Cancelar'."""


def sub_clip(clip, start, end):
    """Corta un clip: MoviePy v2 usa `subclipped`, MoviePy v1 usa `subclip`."""
    if hasattr(clip, "subclipped"):
        return clip.subclipped(start, end)
    return clip.subclip(start, end)


def clip_with_audio(clip, audio):
    """Asigna audio a un clip: v2 usa `with_audio`, v1 usa `set_audio`."""
    if hasattr(clip, "with_audio"):
        return clip.with_audio(audio)
    return clip.set_audio(audio)


def even(v):
    """Entero par más cercano hacia abajo (H.264 exige ancho y alto pares)."""
    return max(2, int(v) // 2 * 2)


def reframe_clip(clip, out_size, crop_x=0.5):
    """
    Recorta el clip a la proporción de `out_size` (la zona elegida con `crop_x`:
    0 = izquierda, 0.5 = centro, 1 = derecha) y lo escala a ese tamaño.
    Compatible con MoviePy v1 (`crop`, `resize`) y v2 (`cropped`, `resized`).
    """
    w, h = (int(v) for v in clip.size)
    ow, oh = out_size
    ratio = ow / oh
    crop = getattr(clip, "cropped", None) or clip.crop
    if w / h > ratio + 0.01:                          # demasiado ancho: se recortan los lados
        cw = even(round(h * ratio))
        x1 = int(round((w - cw) * min(1.0, max(0.0, crop_x))))
        clip = crop(x1=x1, x2=x1 + cw)
    elif w / h < ratio - 0.01:                        # demasiado alto: se recorta arriba y abajo
        ch = even(round(w / ratio))
        y1 = (h - ch) // 2
        clip = crop(y1=y1, y2=y1 + ch)
    if tuple(int(v) for v in clip.size) != (ow, oh):
        resize = getattr(clip, "resized", None) or clip.resize
        clip = resize((ow, oh))
    return clip


def open_file(path):
    """Abre un archivo con el programa predeterminado del sistema."""
    try:
        if os.name == "nt":
            os.startfile(path)                        # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def short_name(path, max_len=34):
    """Nombre de archivo abreviado para mostrarlo en la UI."""
    name = os.path.basename(path)
    return name if len(name) <= max_len else name[: max_len - 3] + "..."


def fmt_time(seconds):
    """Segundos -> 'mm:ss' (o 'h:mm:ss')."""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# =============================================================================
# 3. ANÁLISIS DE AUDIO Y PLAN DE CORTES (funciones puras, sin UI)
# =============================================================================
def seconds_levels(seg, voice_filter=False, check=None):
    """
    Devuelve una lista con el nivel en dBFS de cada fragmento de 1 s.

    - Sin filtro: dBFS = 20*log10(rms) con muestras normalizadas a [-1, 1]
      (equivale al `dBFS` de pydub).
    - Con filtro de voz: solo se cuenta la energía entre 85 y 3400 Hz (FFT),
      lo que reduce golpes graves en la mesa y ruido de alta frecuencia.
    """
    fr = seg.frame_rate
    samples = np.asarray(seg.get_array_of_samples(), dtype=np.float32)
    if seg.channels > 1:
        samples = samples.reshape(-1, seg.channels).mean(axis=1)
    samples = samples / float(1 << (8 * seg.sample_width - 1))

    n = int(fr * CHUNK_MS / 1000)
    total = int(math.ceil(len(samples) / n)) if len(samples) else 0
    mask_cache = {}
    out = []
    for k in range(total):
        if check and k % 200 == 0:
            check()  # Permite cancelar durante análisis largos
        x = samples[k * n:(k + 1) * n]
        if len(x) == 0:
            out.append(float("-inf"))
            continue
        if voice_filter:
            if len(x) not in mask_cache:
                freqs = np.fft.rfftfreq(len(x), 1.0 / fr)
                mask_cache[len(x)] = (freqs >= VOICE_LOW) & (freqs <= VOICE_HIGH)
            spec = np.fft.rfft(x)
            # Teorema de Parseval: potencia media de la banda seleccionada
            power = 2.0 * float(np.sum(np.abs(spec[mask_cache[len(x)]]) ** 2)) / (len(x) ** 2)
        else:
            power = float(np.mean(x * x))
        out.append(10.0 * math.log10(power) if power > 1e-12 else float("-inf"))
    return out


def balance_gains(levels, limit=12.0):
    """
    Ganancia (dB) que iguala el volumen "al hablar" de cada micrófono.
    Se compara el percentil 90 de cada pista contra la mediana de todas; así
    un micrófono con más ganancia no gana siempre solo por sonar más fuerte.
    """
    p90 = {}
    for idx, lv in levels.items():
        vals = [v for v in lv if v > -70]
        if len(vals) >= 5:
            p90[idx] = float(np.percentile(vals, 90))
    if len(p90) < 2:
        return {idx: 0.0 for idx in levels}
    ref = float(np.median(list(p90.values())))
    return {idx: max(-limit, min(limit, ref - p90.get(idx, ref))) for idx in levels}


def compute_plan(levels, n_chunks, threshold, general_idx, min_cut, total_s,
                 margin=None, gains=None):
    """
    Decide la cámara de cada segundo y devuelve la lista de segmentos
    [(cam_idx, inicio_s, fin_s), ...] ya agrupados y sin micro-cortes.

    Reglas:
      * habla una sola persona (> umbral)  -> su cámara
      * hablan varias a la vez             -> si una domina por `margin` dB o más
                                              (el otro micrófono solo la "escucha"),
                                              su cámara; si no, plano general
      * no habla nadie                     -> se mantiene el plano anterior
      * ningún plano dura menos de `min_cut` segundos (se fusiona con el vecino
        más largo)
    `margin=None` desactiva la regla de dominio (varias voces = plano general).
    `gains` (dB por micrófono) iguala los volúmenes antes de comparar.
    """
    gains = gains or {}
    decisions = []
    current = general_idx
    for s in range(n_chunks):
        active = [idx for idx, lv in levels.items() if s < len(lv) and lv[s] > threshold]
        if len(active) == 1:
            current = active[0]
        elif len(active) > 1:
            current = general_idx
            if margin is not None:
                ranked = sorted(active, key=lambda i: levels[i][s] + gains.get(i, 0.0), reverse=True)
                top = levels[ranked[0]][s] + gains.get(ranked[0], 0.0)
                second = levels[ranked[1]][s] + gains.get(ranked[1], 0.0)
                if top - second >= margin:
                    current = ranked[0]
        decisions.append(current)

    # Agrupar segundos consecutivos con la misma cámara
    runs = []  # [cam, inicio, fin]
    for s, cam in enumerate(decisions):
        if runs and runs[-1][0] == cam:
            runs[-1][2] = s + 1
        else:
            runs.append([cam, s, s + 1])
    if not runs:
        runs = [[general_idx, 0, 1]]

    # Eliminar planos más cortos que `min_cut` (evita marear al espectador)
    while len(runs) > 1:
        short = [i for i, r in enumerate(runs) if r[2] - r[1] < min_cut]
        if not short:
            break
        i = min(short, key=lambda k: runs[k][2] - runs[k][1])  # el más corto primero
        if i == 0:
            j = 1
        elif i == len(runs) - 1:
            j = i - 1
        else:
            prev_len = runs[i - 1][2] - runs[i - 1][1]
            next_len = runs[i + 1][2] - runs[i + 1][1]
            j = i - 1 if prev_len >= next_len else i + 1
        runs[i][0] = runs[j][0]          # el plano corto adopta la cámara vecina
        merged = []
        for r in runs:                   # volver a unir vecinos iguales
            if merged and merged[-1][0] == r[0]:
                merged[-1][2] = r[2]
            else:
                merged.append(r)
        runs = merged

    return [(c, float(a), min(float(b), total_s)) for c, a, b in runs]


def apply_overrides(segs, overrides, total_s):
    """
    Aplica las ediciones manuales {segundo: cámara} sobre el plan automático.
    Las ediciones mandan: no se les aplica el "plano mínimo".
    """
    if not overrides:
        return segs
    n = max(1, int(math.ceil(total_s)))
    cam = [None] * n
    for c, a, b in segs:
        for s in range(int(round(a)), min(n, int(math.ceil(b - 1e-6)))):
            cam[s] = c
    for s, c in overrides.items():
        if 0 <= s < n:
            cam[s] = c
    last = segs[0][0] if segs else 0
    for s in range(n):                      # por si algún segundo quedó vacío
        if cam[s] is None:
            cam[s] = last
        last = cam[s]
    runs = []
    for s, c in enumerate(cam):
        if runs and runs[-1][0] == c:
            runs[-1][2] = s + 1
        else:
            runs.append([c, s, s + 1])
    return [(c, float(a), min(float(b), total_s)) for c, a, b in runs]


def make_plan(a, threshold, min_cut, margin, balance, overrides):
    """Plan completo a partir de un análisis: automático + ediciones manuales."""
    gains = a.get("gains") if balance else None
    segs = compute_plan(a["levels"], a["n_chunks"], threshold, a["general_idx"],
                        min_cut, a["total_s"], margin, gains)
    return apply_overrides(segs, overrides, a["total_s"])


def pick_window(segs, total_s, length=QUICK_SECONDS):
    """Tramo de `length` s con más cambios de cámara (donde mejor se ve si los ajustes sirven)."""
    if total_s <= length:
        return 0.0, float(total_s)
    cuts = sorted(s for _, s, _ in segs[1:])
    best_t, best_k = 0, -1
    for t0 in range(0, int(total_s - length) + 1):
        k = bisect.bisect_left(cuts, t0 + length) - bisect.bisect_right(cuts, t0)
        if k > best_k:
            best_t, best_k = t0, k
    return float(best_t), float(best_t + length)


def clip_segments(segs, t0, t1):
    """Recorta el plan al tramo [t0, t1] y lo reinicia en 0."""
    out = []
    for c, a, b in segs:
        s, e = max(a, t0), min(b, t1)
        if e - s > 1e-6:
            out.append((c, s - t0, e - t0))
    return out


# --- Sincronización de cámaras por audio -------------------------------------
def audio_envelope(seg):
    """Envolvente de volumen (100 puntos por segundo, en escala logarítmica y normalizada)."""
    x = np.asarray(seg.get_array_of_samples(), dtype=np.float32)
    if seg.channels > 1:
        x = x.reshape(-1, seg.channels).mean(axis=1)
    block = max(1, seg.frame_rate // ENV_HZ)
    n = len(x) // block
    if n < 10:
        return np.zeros(0, dtype=np.float32)
    env = np.abs(x[:n * block]).reshape(n, block).mean(axis=1) / float(1 << (8 * seg.sample_width - 1))
    env = np.log(env + 1e-4)
    win = ENV_HZ * 2                                   # quita la tendencia lenta (2 s)
    if n > win:
        env = env - np.convolve(env, np.ones(win) / win, mode="same")
    env = env - float(env.mean())
    sd = float(env.std())
    return (env / sd).astype(np.float32) if sd > 1e-6 else np.zeros(n, dtype=np.float32)


def estimate_delay(ref_env, env, max_lag_s=SYNC_MAX_LAG):
    """
    Cuántos segundos DESPUÉS de `ref_env` empezó a grabar `env` (negativo si
    empezó antes) y qué tan fiable es (cuanto mayor, mejor). Correlación
    cruzada por FFT sobre la envolvente de volumen.
    """
    if len(ref_env) < 5 * ENV_HZ or len(env) < 5 * ENV_HZ:
        return 0.0, 0.0
    n = 1 << int(math.ceil(math.log2(len(ref_env) + len(env))))
    corr = np.fft.irfft(np.fft.rfft(ref_env, n) * np.conj(np.fft.rfft(env, n)), n)
    maxlag = min(int(max_lag_s * ENV_HZ), n // 2 - 1)
    cand = np.concatenate([corr[n - maxlag:], corr[:maxlag + 1]])      # desfases -maxlag..+maxlag
    k = int(np.argmax(cand))
    score = (float(cand[k]) - float(cand.mean())) / (float(cand.std()) + 1e-9)
    return (k - maxlag) / ENV_HZ, score


def compute_sync(cams, check=None, report=None):
    """
    Calcula cuánto hay que "saltar" al inicio de cada archivo para que todos
    arranquen en el mismo instante.
      voff[i] -> segundos que se saltan del video i
      aoff[i] -> segundos que se saltan del audio externo de la cámara i
    Referencia: el audio incrustado de la primera cámara que tenga audio. Cada
    micrófono externo se alinea con el audio de su propia cámara.
    Devuelve (voff, aoff, notas).
    """
    check = check or (lambda: None)
    n = len(cams)
    notes = []

    def env_of(path):
        try:
            seg = read_audio(path, SYNC_RATE, 1, duration=SYNC_MAX_S).set_sample_width(2)
            env = audio_envelope(seg)
            return env if len(env) >= 5 * ENV_HZ else None
        except Cancelled:
            raise
        except Exception:
            return None

    env_v = {}
    for i, c in enumerate(cams):
        check()
        if report:
            report(i / max(1, n), T("Sincronizando cámaras ({i}/{n})...", i=i + 1, n=n))
        env_v[i] = env_of(c["video"])

    ref = next((i for i in range(n) if env_v[i] is not None), None)
    delay_v = {}
    for i, c in enumerate(cams):
        if ref is None or i == ref:
            delay_v[i] = 0.0
            continue
        if env_v[i] is None:
            notes.append(T("{name}: el video no tiene audio; no se pudo sincronizar.", name=c["name"]))
            delay_v[i] = 0.0
            continue
        d, score = estimate_delay(env_v[ref], env_v[i])
        if score >= SYNC_MIN_SCORE:
            delay_v[i] = d
        else:
            delay_v[i] = 0.0
            notes.append(T("{name}: no encontré sonido en común; se dejó sin mover.", name=c["name"]))

    delay_a = {}
    for i, c in enumerate(cams):
        if not c["audio"]:
            continue
        check()
        env_a = env_of(c["audio"])
        if env_a is None or env_v[i] is None:
            delay_a[i] = delay_v[i]
            notes.append(T("{name}: no pude alinear su micrófono con su cámara.", name=c["name"]))
            continue
        d, score = estimate_delay(env_v[i], env_a)
        if score >= SYNC_MIN_SCORE:
            delay_a[i] = delay_v[i] + d
        else:
            delay_a[i] = delay_v[i]
            notes.append(T("{name}: su micrófono no coincide con el audio de la cámara; se dejó sin mover.", name=c["name"]))

    latest = max([0.0] + list(delay_v.values()) + list(delay_a.values()))
    voff = {i: max(0.0, latest - delay_v[i]) for i in range(n)}
    aoff = {i: max(0.0, latest - delay_a[i]) for i in delay_a}
    return voff, aoff, notes


# =============================================================================
# 4. AUDIO CON CROSSFADE (J-CUT)
# =============================================================================
def read_audio(path, rate=None, channels=None, start=0.0, duration=None):
    """
    Decodifica cualquier audio/video a WAV con el FFmpeg que incluye MoviePy
    (imageio-ffmpeg) y lo carga en pydub. Así no hace falta instalar FFmpeg ni
    ffprobe en el sistema, requisito para que el .exe sea autónomo.
    `start` salta los primeros segundos y `duration` limita cuánto se lee.
    """
    ff = moviepy_ffmpeg()
    if not ff:
        raise RuntimeError(T("No se encontró FFmpeg."))
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = [ff, "-y", "-loglevel", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", path, "-vn", "-acodec", "pcm_s16le"]
    if channels:
        cmd += ["-ac", str(channels)]
    if rate:
        cmd += ["-ar", str(rate)]
    cmd.append(tmp)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode != 0:
            detail = r.stderr.decode(errors="ignore")[-300:]
            raise RuntimeError(T("No se pudo leer el audio de {name}.", name=os.path.basename(path)) + "\n" + detail)
        return AudioSegment.from_wav(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_full_audio(path, start=0.0, duration=None):
    """Carga un audio en formato uniforme (44.1 kHz, estéreo, 16 bits)."""
    return read_audio(path, 44100, 2, start, duration).set_sample_width(2)


def assemble_with_crossfades(pieces, xfs):
    """
    Une las piezas de audio. `xfs[i]` es el solape (ms) entre la pieza i-1 y la
    pieza i: esa zona se mezcla con crossfade. Para no copiar el audio entero
    en cada unión (cuadrático), se arma una lista de trozos y se concatenan
    los bytes una sola vez.
    """
    parts = []
    for i, p in enumerate(pieces):
        xf_in = xfs[i]
        xf_out = xfs[i + 1] if i + 1 < len(pieces) else 0
        if i > 0 and xf_in > 0:
            prev = pieces[i - 1]
            tail = prev[len(prev) - xf_in:]
            head = p[:xf_in]
            parts.append(tail.append(head, crossfade=xf_in))
        parts.append(p[xf_in: max(xf_in, len(p) - xf_out)])
    data = b"".join(x.raw_data for x in parts)
    return AudioSegment(data=data, sample_width=2, frame_rate=44100, channels=2)


# =============================================================================
# 5. EXPORTACIÓN XML (Final Cut Pro 7 "xmeml" v4)
#    Lo leen Premiere Pro y DaVinci Resolve (File > Import > Timeline).
# =============================================================================
def build_fcp7_xml(xml_path, seq_name, cameras, infos, segments, total_s,
                   overlap_s, use_embedded, audio_len, voff=None, aoff=None):
    # voff / aoff: segundos que se saltan al inicio de cada video / audio externo
    # (resultado de la sincronización por audio). Sin sincronizar valen 0.
    voff, aoff = voff or {}, aoff or {}
    fps = infos[0]["fps"] or 30.0
    tb = int(round(fps))
    ntsc = abs(fps - tb * 1000 / 1001) < abs(fps - tb)   # 29.97 / 23.976 / 59.94
    fps_eff = tb * 1000 / 1001 if ntsc else float(tb)
    width, height = infos[0]["size"]

    def fr(sec):                       # segundos -> fotogramas
        return int(round(sec * fps_eff))

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, **attrs)
        if text is not None:
            el.text = str(text)
        return el

    def add_rate(parent):
        r = sub(parent, "rate")
        sub(r, "timebase", tb)
        sub(r, "ntsc", "TRUE" if ntsc else "FALSE")

    root = ET.Element("xmeml", version="4")
    seq = sub(root, "sequence", id="sequence-1")
    sub(seq, "name", seq_name)
    sub(seq, "duration", fr(total_s))
    add_rate(seq)
    tc = sub(seq, "timecode")
    add_rate(tc)
    sub(tc, "string", "00:00:00:00")
    sub(tc, "frame", 0)
    sub(tc, "displayformat", "NDF")

    media = sub(seq, "media")

    # ---- Video -----------------------------------------------------------
    video = sub(media, "video")
    fmt = sub(video, "format")
    sc = sub(fmt, "samplecharacteristics")
    add_rate(sc)
    sub(sc, "width", width)
    sub(sc, "height", height)
    sub(sc, "pixelaspectratio", "square")
    sub(sc, "fielddominance", "none")
    vtrack = sub(video, "track")

    defined = set()   # ids de <file> ya definidos (las repeticiones solo referencian)

    def add_file(parent, fid, path, dur_s, size, with_video, with_audio):
        f = sub(parent, "file", id=fid)
        if fid in defined:
            return f
        defined.add(fid)
        sub(f, "name", os.path.basename(path))
        sub(f, "pathurl", Path(path).resolve().as_uri())
        add_rate(f)
        sub(f, "duration", fr(dur_s))
        tcf = sub(f, "timecode")
        add_rate(tcf)
        sub(tcf, "string", "00:00:00:00")
        sub(tcf, "frame", 0)
        sub(tcf, "displayformat", "NDF")
        m = sub(f, "media")
        if with_video:
            v = sub(m, "video")
            s = sub(v, "samplecharacteristics")
            add_rate(s)
            sub(s, "width", size[0])
            sub(s, "height", size[1])
            sub(s, "pixelaspectratio", "square")
        if with_audio:
            a = sub(m, "audio")
            s = sub(a, "samplecharacteristics")
            sub(s, "depth", 16)
            sub(s, "samplerate", 48000)
            sub(a, "channelcount", 2)
        return f

    for i, (ci, a, b) in enumerate(segments):
        item = sub(vtrack, "clipitem", id=f"clipitem-v{i + 1}")
        sub(item, "name", cameras[ci]["name"])
        sub(item, "enabled", "TRUE")
        sub(item, "duration", fr(infos[ci]["duration"]))
        add_rate(item)
        sub(item, "start", fr(a))
        sub(item, "end", fr(b))
        o = voff.get(ci, 0.0)        # punto de entrada en el archivo original
        sub(item, "in", fr(a + o))
        sub(item, "out", fr(b + o))
        add_file(item, f"file-v{ci}", cameras[ci]["video"], infos[ci]["duration"],
                 infos[ci]["size"], True, True)

    # ---- Audio -----------------------------------------------------------
    audio = sub(media, "audio")
    sub(audio, "numOutputChannels", 2)
    afmt = sub(audio, "format")
    asc = sub(afmt, "samplecharacteristics")
    sub(asc, "depth", 16)
    sub(asc, "samplerate", 48000)

    if use_embedded:
        # Una pista con el audio de cada cámara; el audio entra `overlap_s`
        # antes que el video (J-cut). En el XML van sin solape: en el editor
        # basta con aplicar una transición de audio por defecto.
        starts = [0.0] + [max(0.0, a - overlap_s) for (_, a, _) in segments[1:]]
        ends = starts[1:] + [total_s]
        atrack = sub(audio, "track", premiereTrackType="Stereo")
        for i, (ci, _, _) in enumerate(segments):
            item = sub(atrack, "clipitem", id=f"clipitem-a{i + 1}")
            sub(item, "name", cameras[ci]["name"])
            sub(item, "enabled", "TRUE")
            sub(item, "duration", fr(infos[ci]["duration"]))
            add_rate(item)
            sub(item, "start", fr(starts[i]))
            sub(item, "end", fr(ends[i]))
            sub(item, "in", fr(starts[i] + voff.get(ci, 0.0)))
            sub(item, "out", fr(ends[i] + voff.get(ci, 0.0)))
            add_file(item, f"file-v{ci}", cameras[ci]["video"], infos[ci]["duration"],
                     infos[ci]["size"], True, True)
            st = sub(item, "sourcetrack")
            sub(st, "mediatype", "audio")
            sub(st, "trackindex", 1)
    else:
        # Una pista por micrófono, completa y sin cortar: así el editor puede
        # ajustar niveles; el sonido es continuo y no "salta" entre cámaras.
        for ci, cam in enumerate(cameras):
            if not cam["audio"]:
                continue
            atrack = sub(audio, "track", premiereTrackType="Stereo")
            item = sub(atrack, "clipitem", id=f"clipitem-a{ci + 1}")
            sub(item, "name", f"{cam['name']} (mic)")
            sub(item, "enabled", "TRUE")
            sub(item, "duration", fr(audio_len[ci]))
            add_rate(item)
            sub(item, "start", 0)
            sub(item, "end", fr(total_s))
            sub(item, "in", fr(aoff.get(ci, 0.0)))
            sub(item, "out", fr(aoff.get(ci, 0.0) + total_s))
            add_file(item, f"file-a{ci}", cam["audio"], audio_len[ci],
                     infos[ci]["size"], False, True)
            st = sub(item, "sourcetrack")
            sub(st, "mediatype", "audio")
            sub(st, "trackindex", 1)

    if hasattr(ET, "indent"):
        ET.indent(root)
    text = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + ET.tostring(root, encoding="unicode")
    Path(xml_path).write_text(text, encoding="utf-8")


# =============================================================================
# 5b. EXPORTACIÓN A BORRADOR DE CAPCUT (librería pycapcut 0.0.4)
#     Crea una carpeta de proyecto dentro de la carpeta de borradores de
#     CapCut con los cortes ya puestos. Después lo exportas desde CapCut.
# =============================================================================
def default_capcut_drafts_dir():
    """Carpeta de borradores de CapCut en Windows (vacía si no existe)."""
    local = os.environ.get("LOCALAPPDATA", "")
    cand = os.path.join(local, "CapCut", "User Data", "Projects", "com.lveditor.draft")
    return cand if os.path.isdir(cand) else ""


def export_capcut_draft(drafts_dir, name, cameras, infos, segments, total_s,
                        final_audio_path, voff=None, aoff=None, vertical=False):
    """
    Video: una pista por cámara, con un clip (silenciado) por cada tramo en que se ve.
    Pistas de audio:
      * con audios externos  -> una pista por micrófono, completa
      * con audio incrustado -> el audio final ya armado (J-cut + crossfade)
    `voff` / `aoff`: segundos que se saltan al inicio de cada video / audio
    externo (sincronización). `vertical`: lienzo 1080x1920 y cada plano
    ampliado para llenar el alto, recortado según `crop_x` de la cámara.
    Devuelve la ruta del borrador creado.
    """
    voff, aoff = voff or {}, aoff or {}
    try:
        import pycapcut as cc
    except ImportError:
        raise RuntimeError(T("Falta la librería pycapcut en esta instalación.\n"
                           "Si ejecutas el script: python -m pip install pycapcut==0.0.4\n"
                           "Si usas el .exe: vuelve a generarlo con build_exe.bat"))

    SEC = 1_000_000                                   # CapCut trabaja en microsegundos
    width, height = VERT_SIZE if vertical else infos[0]["size"]
    folder = cc.DraftFolder(drafts_dir)
    try:
        script = folder.create_draft(name, width, height, allow_replace=True)
    except TypeError:                                 # versiones sin allow_replace
        script = folder.create_draft(name, width, height)

    # ---- Video: una pista por cámara ----------------------------------------
    # Cada cámara vive en su propia pista y solo ocupa los tramos donde se ve.
    # Los clips apuntan al archivo completo, así que en CapCut puedes alargar
    # un clip, moverlo o cambiar un tramo a otra cámara sin perder material.
    track_of = {}
    for ci in sorted({c for c, _, _ in segments}):
        track_of[ci] = f"cam{ci + 1}_{cameras[ci]['name']}"
        script.add_track(cc.TrackType.video, track_of[ci])
    for ci, s_, e_ in segments:
        s_us, e_us = int(round(s_ * SEC)), int(round(e_ * SEC))
        rng = cc.Timerange(s_us, e_us - s_us)
        src_us = int(round((s_ + voff.get(ci, 0.0)) * SEC))
        src = cc.Timerange(src_us, e_us - s_us)
        kwargs = {}
        if vertical:
            # Escala para que el alto llene el lienzo; el desplazamiento
            # (en medios anchos de lienzo) deja centrada la zona elegida.
            vw, vh = infos[ci]["size"]
            sc = max(1.0, (VERT_SIZE[1] * vw) / (VERT_SIZE[0] * vh))
            cx = float(cameras[ci].get("crop_x", 0.5))
            kwargs["clip_settings"] = cc.ClipSettings(
                scale_x=sc, scale_y=sc, transform_x=(0.5 - cx) * 2 * sc)
        try:
            seg = cc.VideoSegment(cameras[ci]["video"], rng, source_timerange=src,
                                  volume=0.0, **kwargs)
        except TypeError:                             # versiones sin parámetro volume
            seg = cc.VideoSegment(cameras[ci]["video"], rng, source_timerange=src, **kwargs)
            seg.volume = 0.0
        script.add_segment(seg, track_of[ci])

    # ---- Audio -------------------------------------------------------------
    total_us = int(round(total_s * SEC))
    audio_files = []
    if final_audio_path:
        audio_files.append(("audio_final", final_audio_path))
    else:
        audio_files = [(f"mic_{ci + 1}", c["audio"]) for ci, c in enumerate(cameras) if c["audio"]]
    for track_name, path in audio_files:
        script.add_track(cc.TrackType.audio, track_name)
        ci = int(track_name.split("_")[1]) - 1 if track_name.startswith("mic_") else None
        a_us = int(round(aoff.get(ci, 0.0) * SEC)) if ci is not None else 0
        script.add_segment(
            cc.AudioSegment(path, cc.Timerange(0, total_us),
                            source_timerange=cc.Timerange(a_us, total_us)), track_name)

    script.save()
    draft_dir = os.path.join(drafts_dir, name)

    # CapCut 8.7 / 9.x lee `draft_info.json`; pycapcut solo escribe
    # `draft_content.json`. Se deja una copia con el nombre nuevo (si no
    # hace falta, CapCut simplemente la ignora).
    src = os.path.join(draft_dir, "draft_content.json")
    dst = os.path.join(draft_dir, "draft_info.json")
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copyfile(src, dst)
    return draft_dir


# =============================================================================
# 6. DETECCIÓN DE GPU
# =============================================================================
# (etiqueta, codec, parámetros extra de FFmpeg).
# Los parámetros extra se colocan DESPUÉS de `-preset ultrafast` y lo
# sobrescriben, porque "ultrafast" solo existe en libx264.
GPU_CANDIDATES = [
    ("NVIDIA NVENC", "h264_nvenc", ["-preset", "p1", "-b:v", "8M"]),
    ("NVIDIA NVENC (FFmpeg antiguo)", "h264_nvenc", ["-preset", "fast", "-b:v", "8M"]),
    ("AMD AMF", "h264_amf", ["-quality", "speed", "-b:v", "8M"]),
    ("Intel QuickSync", "h264_qsv", ["-preset", "veryfast", "-b:v", "8M"]),
    ("Apple VideoToolbox", "h264_videotoolbox", ["-b:v", "8M"]),
]


def moviepy_ffmpeg():
    """Ruta del FFmpeg que usa MoviePy (para probar el mismo binario)."""
    exe = None
    try:
        from moviepy.config import FFMPEG_BINARY
        exe = FFMPEG_BINARY
    except Exception:
        try:
            from moviepy.config import get_setting
            exe = get_setting("FFMPEG_BINARY")
        except Exception:
            exe = None
    if not exe or exe == "ffmpeg-imageio":
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = shutil.which("ffmpeg")
    return exe


def detect_gpu_encoder():
    """
    Busca un codificador por hardware y lo PRUEBA con un mini-encode real
    (que el codec aparezca en la lista no garantiza que haya GPU compatible).
    Devuelve dict(label, codec, params) o None.
    """
    ff = moviepy_ffmpeg()
    if not ff:
        return None
    flags = 0x08000000 if os.name == "nt" else 0   # No abrir ventana de consola en Windows
    try:
        listing = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, stdin=subprocess.DEVNULL,
                                 text=True, timeout=15, creationflags=flags).stdout
    except Exception:
        return None
    for label, codec, params in GPU_CANDIDATES:
        if codec not in listing:
            continue
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "color=c=black:s=320x240:d=0.2", "-c:v", codec,
               "-preset", "ultrafast", *params, "-pix_fmt", "yuv420p", "-f", "null", "-"]
        try:
            if subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=25,
                              creationflags=flags).returncode == 0:
                return {"label": label, "codec": codec, "params": params}
        except Exception:
            continue
    return None


# =============================================================================
# 7. LOGGER DE PROGRESO PARA MOVIEPY (con cancelación)
# =============================================================================
if ProgressBarLogger is not None:

    class RenderLogger(ProgressBarLogger):
        """Convierte el progreso de write_videofile en 0..1 y permite cancelar."""

        def __init__(self, progress_cb, cancel_event):
            super().__init__(init_state=None, bars=None, ignored_bars=None,
                             logged_bars="all", min_time_interval=0.3,
                             ignore_bars_under=0)
            # OJO: no llamar `callback` a estos atributos; proglog ya usa ese nombre.
            self.progress_cb = progress_cb
            self.cancel_event = cancel_event

        def bars_callback(self, bar, attr, value, old_value=None):
            if self.cancel_event.is_set():
                raise Cancelled()    # Interrumpe write_videofile
            # MoviePy v1 usa la barra "t"; v2 usa "frame_index".
            if bar in ("t", "frame_index") and attr == "index":
                total = self.bars[bar].get("total") or 0
                if total:
                    self.progress_cb(min(1.0, value / total))
else:
    RenderLogger = None


# =============================================================================
# 8. WIDGET: TARJETA DE CÁMARA
# =============================================================================
class CameraCard(ctk.CTkFrame):
    """
    Tarjeta de una cámara: punto de color, nombre editable, video, audio
    opcional y botón para quitarla. El punto tiene el mismo color que la barra
    de esa cámara en la línea de tiempo.
    NOTA: ningún widget recibe padx/pady en su constructor (bug de CTk);
    los márgenes se aplican solo en .grid() / .pack().
    """

    def __init__(self, master, index, on_delete, on_change):
        super().__init__(master, fg_color=COL_SOFT, corner_radius=16)
        self.index = index
        self.on_delete = on_delete
        self.on_change = on_change      # Avisa a la app para invalidar el análisis
        self.video_path = ""
        self.audio_path = ""

        self.columnconfigure(1, weight=1, minsize=150)

        # --- Una sola fila: punto, nombre, video, audio y quitar -------------
        self.dot = ctk.CTkFrame(self, width=10, height=10, corner_radius=5,
                                fg_color=CAM_COLORS[(index - 1) % len(CAM_COLORS)])
        self.dot.grid(row=0, column=0, padx=(18, 10), pady=16)

        self.name_var = ctk.StringVar(
            value=T("Anfitrión") if index == 1 else T("Invitado {n}", n=index - 1))
        self.name_entry = ctk.CTkEntry(
            self, textvariable=self.name_var, placeholder_text="Nombre de la cámara",
            height=34, corner_radius=10, border_width=0, fg_color=COL_SURFACE,
            text_color=COL_INK, placeholder_text_color=COL_MUTED, font=ui_font(13, True))
        self.name_entry.grid(row=0, column=1, padx=(0, 16), pady=14, sticky="ew")

        self.video_btn, self.video_lbl = self._file_cell(
            2, "Elegir video", self.pick_video, "Falta el video")
        self.audio_btn, self.audio_lbl = self._file_cell(
            3, "Elegir audio", self.pick_audio, "Audio opcional")

        self.delete_btn = ctk.CTkButton(
            self, text="Quitar", width=64, height=30, corner_radius=10,
            fg_color="transparent", hover_color=COL_ROSE, text_color=COL_MUTED,
            font=ui_font(12), command=lambda: self.on_delete(self))
        self.delete_btn.grid(row=0, column=4, padx=(0, 12), pady=14)

        # --- Encuadre para el formato vertical (solo visible en modo vertical) --
        self.crop_row = ctk.CTkFrame(self, fg_color="transparent")
        self.crop_row.grid(row=1, column=1, columnspan=4, padx=(0, 16), pady=(0, 12), sticky="ew")
        self.crop_row.columnconfigure(1, weight=1)
        self.crop_title = ctk.CTkLabel(self.crop_row, text="Encuadre en vertical",
                                       text_color=COL_MUTED, font=ui_font(11))
        self.crop_title.grid(row=0, column=0, padx=(0, 12), sticky="w")
        self.crop_slider = ctk.CTkSlider(
            self.crop_row, from_=0, to=1, number_of_steps=20, height=14,
            fg_color=COL_LINE, progress_color=COL_LINE, button_color=COL_KNOB,
            button_hover_color=COL_KNOB_HOVER, command=self._on_crop)
        self.crop_slider.set(0.5)
        self.crop_slider.grid(row=0, column=1, sticky="ew")
        self.crop_lbl = ctk.CTkLabel(self.crop_row, text="Centro", text_color=COL_INK,
                                     font=ui_font(11), width=70, anchor="e")
        self.crop_lbl.grid(row=0, column=2, padx=(12, 0))
        self.crop_row.grid_remove()
        self.apply_language()

    def _default_names(self):
        """Nombres que pone el programa por defecto (en cualquier idioma) para esta tarjeta."""
        k = self.index - 1
        return {"Anfitrión", EN.get("Anfitrión", "Anfitrión"), f"Invitado {k}",
                EN.get("Invitado {n}", "Invitado {n}").format(n=k)}

    def apply_language(self):
        """Pone todos los textos de la tarjeta en el idioma activo."""
        if self.name_var.get().strip() in self._default_names():
            self.name_var.set(T("Anfitrión") if self.index == 1
                              else T("Invitado {n}", n=self.index - 1))
        self.name_entry.configure(placeholder_text=T("Nombre de la cámara"))
        self.delete_btn.configure(text=T("Quitar"))
        self.crop_title.configure(text=T("Encuadre en vertical"))
        for path, btn, lbl, pick_text, empty in (
                (self.video_path, self.video_btn, self.video_lbl, "Elegir video", "Falta el video"),
                (self.audio_path, self.audio_btn, self.audio_lbl, "Elegir audio", "Audio opcional")):
            btn.configure(text=T("Cambiar") if path else T(pick_text))
            lbl.configure(text=short_name(path, 17) if path else T(empty))
        self._on_crop(self.crop_slider.get())

    def _on_crop(self, value):
        value = float(value)
        self.crop_lbl.configure(
            text=T("Izquierda") if value < 0.4 else T("Derecha") if value > 0.6 else T("Centro"))

    def show_crop(self, visible):
        """Muestra u oculta el control de encuadre (según el formato elegido)."""
        if visible:
            self.crop_row.grid()
        else:
            self.crop_row.grid_remove()

    def _file_cell(self, col, button_text, command, empty_text):
        """Un botón de archivo con el nombre del archivo elegido al lado."""
        cell = ctk.CTkFrame(self, fg_color="transparent")
        cell.grid(row=0, column=col, padx=(0, 14), pady=14)
        btn = ctk.CTkButton(
            cell, text=button_text, width=100, height=30, corner_radius=10,
            fg_color="transparent", border_width=1, border_color=COL_LINE,
            hover_color=COL_HOVER, text_color=COL_INK, font=ui_font(12), command=command)
        btn.grid(row=0, column=0)
        lbl = ctk.CTkLabel(cell, text=empty_text, text_color=COL_MUTED, width=112,
                           font=ui_font(11), anchor="w")
        lbl.grid(row=0, column=1, padx=(8, 0))
        return btn, lbl

    def pick_video(self):
        path = filedialog.askopenfilename(
            title=T("Selecciona el video"),
            filetypes=[(T("Video"), "*.mp4 *.mov *.mkv *.avi"), (T("Todos"), "*.*")])
        if path:
            self.video_path = path
            self.video_lbl.configure(text=short_name(path, 17), text_color=COL_INK)
            self.video_btn.configure(text=T("Cambiar"))
            self.on_change()

    def pick_audio(self):
        path = filedialog.askopenfilename(
            title=T("Selecciona el audio del micrófono"),
            filetypes=[(T("Audio"), "*.wav *.mp3 *.m4a *.flac"), (T("Todos"), "*.*")])
        if path:
            self.audio_path = path
            self.audio_lbl.configure(text=short_name(path, 17), text_color=COL_INK)
            self.audio_btn.configure(text=T("Cambiar"))
            self.on_change()

    def set_index(self, index):
        """Renumera la tarjeta (y su color) cuando se quita otra cámara."""
        self.index = index
        self.dot.configure(fg_color=CAM_COLORS[(index - 1) % len(CAM_COLORS)])

    def get_data(self):
        return {
            "id": self.index,
            "name": self.name_var.get().strip() or T("Cámara {n}", n=self.index),
            "video": self.video_path,
            "audio": self.audio_path,
            "crop_x": float(self.crop_slider.get()),
        }


# =============================================================================
# 9. CLASE PRINCIPAL (INTERFAZ + HILO DE TRABAJO)
# =============================================================================
# =============================================================================
# IMÁGENES DE LAS CÁMARAS Y VENTANAS DE EDICIÓN / PRUEBA
# =============================================================================
def grab_frame(path, t, width=320):
    """Un fotograma (imagen PIL) del video en el segundo `t`; None si no se pudo."""
    ff = moviepy_ffmpeg()
    if not ff or not path:
        return None
    flags = 0x08000000 if os.name == "nt" else 0
    cmd = [ff, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={int(width)}:-2", "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                             timeout=30, creationflags=flags).stdout
        if not out:
            return None
        img = Image.open(io.BytesIO(out))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def vertical_box(w, h, crop_x):
    """Zona (x1, x2) que se conserva al pasar un fotograma a 9:16."""
    ratio = VERT_SIZE[0] / VERT_SIZE[1]
    if w / h <= ratio + 0.01:
        return 0, w
    cw = even(round(h * ratio))
    x1 = int(round((w - cw) * min(1.0, max(0.0, crop_x))))
    return x1, x1 + cw


class CutEditor(ctk.CTkToplevel):
    """Ventana para editar cortes viendo la imagen de cada cámara y el resultado."""
    THUMB_W = 200

    def __init__(self, app, t=0.0, rng=None):
        super().__init__(app, fg_color=COL_BG)
        self.app = app
        self.title(T("Editor de cortes"))
        self.geometry("1040x700")
        self.minsize(980, 600)
        self.transient(app)
        a = app.analysis
        self.total = max(1.0, float(a["total_s"]))
        self.names = list(a["names"])
        datas = [c.get_data() for c in app.cards]
        self.paths = [d["video"] for d in datas]
        self.voff = dict(a["voff"])
        self.durs = [i["duration"] for i in a["infos"]]
        self.t = 0.0
        self.rng = (0, 1)
        self._gen = 0
        self._cache = {}
        self._lock = threading.Lock()
        self._fresh = {}
        self._want = 0
        self._imgs = {}
        self._fetch_id = None
        self._poll_id = None
        self._closed = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, padx=24, pady=(18, 4), sticky="ew")
        head.columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=T("Elige un momento con la barra y decide qué cámara se ve."),
                     text_color=COL_INK, font=ui_font(14, True), anchor="w"
                     ).grid(row=0, column=0, sticky="w")
        self.time_lbl = ctk.CTkLabel(head, text="", text_color=COL_MUTED, font=ui_font(12))
        self.time_lbl.grid(row=0, column=1, sticky="e")

        sl = ctk.CTkFrame(self, fg_color="transparent")
        sl.grid(row=1, column=0, padx=24, pady=(4, 6), sticky="ew")
        sl.columnconfigure(1, weight=1)
        self._btn(sl, "-1 s", 56, lambda: self._nudge(-1)).grid(row=0, column=0, padx=(0, 8))
        self.slider = ctk.CTkSlider(
            sl, from_=0, to=self.total, number_of_steps=max(1, int(self.total)), height=18,
            fg_color=COL_TRACK, progress_color=COL_PROGRESS, button_color=COL_KNOB,
            button_hover_color=COL_KNOB_HOVER, command=self._on_slider)
        self.slider.grid(row=0, column=1, sticky="ew")
        self._btn(sl, "+1 s", 56, lambda: self._nudge(1)).grid(row=0, column=2, padx=(8, 0))

        body = ctk.CTkFrame(self, fg_color=COL_SURFACE, border_width=1, border_color=COL_BORDER,
                            corner_radius=22)
        body.grid(row=2, column=0, padx=24, pady=6, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, padx=16, pady=16, sticky="n")
        ctk.CTkLabel(left, text=T("Resultado en este momento"), text_color=COL_INK,
                     font=ui_font(13, True)).grid(row=0, column=0, sticky="w")
        self.res_lbl = ctk.CTkLabel(left, text=T("Cargando imagen..."), text_color=COL_MUTED,
                                    fg_color=COL_SOFT, corner_radius=12, width=460, height=259)
        self.res_lbl.grid(row=1, column=0, pady=(8, 6))
        self.res_name = ctk.CTkLabel(left, text="", text_color=COL_MUTED, font=ui_font(12))
        self.res_name.grid(row=2, column=0, sticky="w")

        grid = ctk.CTkScrollableFrame(body, fg_color="transparent", corner_radius=0)
        grid.grid(row=0, column=1, padx=(0, 10), pady=12, sticky="nsew")
        self.thumbs = []
        for i, name in enumerate(self.names):
            card = ctk.CTkFrame(grid, fg_color=COL_SOFT, border_width=3,
                                border_color=COL_BORDER, corner_radius=14)
            card.grid(row=i // 2, column=i % 2, padx=5, pady=5, sticky="n")
            img = ctk.CTkLabel(card, text=T("Cargando imagen..."), text_color=COL_MUTED,
                               width=self.THUMB_W, height=int(self.THUMB_W * 9 / 16), cursor="hand2")
            img.grid(row=0, column=0, padx=8, pady=(8, 4))
            img.bind("<Button-1>", lambda e, i=i: self._apply(i))
            btn = ctk.CTkButton(
                card, text=T("Mostrar a «{name}»", name=name), height=30, corner_radius=10,
                fg_color=CAM_COLORS[i % len(CAM_COLORS)], hover_color=COL_ACCENT_HOVER,
                text_color=PASTEL_INK, font=ui_font(12, True), command=lambda i=i: self._apply(i))
            btn.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
            self.thumbs.append((card, img))

        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.grid(row=3, column=0, padx=24, pady=(6, 0), sticky="ew")
        self.range_lbl = ctk.CTkLabel(nav, text="", text_color=COL_INK, font=ui_font(12, True))
        self.range_lbl.grid(row=0, column=0, padx=(0, 14))
        col = 1
        for text, cmd in ((T("Inicio -1 s"), lambda: self._step_range(0, -1)),
                          (T("Inicio +1 s"), lambda: self._step_range(0, 1)),
                          (T("Fin -1 s"), lambda: self._step_range(1, -1)),
                          (T("Fin +1 s"), lambda: self._step_range(1, 1))):
            self._btn(nav, text, 84, cmd).grid(row=0, column=col, padx=(0, 6))
            col += 1
        self._btn(nav, T("Corte anterior"), 112, lambda: self._jump(-1)).grid(row=0, column=col, padx=(14, 6))
        self._btn(nav, T("Corte siguiente"), 116, lambda: self._jump(1)).grid(row=0, column=col + 1)

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=4, column=0, padx=24, pady=(8, 16), sticky="ew")
        foot.columnconfigure(2, weight=1)
        self.auto_btn = self._btn(foot, T("Volver al corte automático (este tramo)"), 280, self._auto_here)
        self.auto_btn.grid(row=0, column=0, padx=(0, 8))
        self.clear_btn = self._btn(foot, T("Quitar todas mis ediciones"), 200, self._auto_all)
        self.clear_btn.grid(row=0, column=1)
        ctk.CTkButton(foot, text=T("Cerrar"), width=100, height=34, corner_radius=12,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER,
                      text_color=COL_ACCENT_INK, font=ui_font(12, True),
                      command=self.close).grid(row=0, column=3)

        self.protocol("WM_DELETE_WINDOW", self.close)
        ico = resource_path("icon.ico")
        if os.name == "nt" and os.path.exists(ico):
            self.after(300, lambda: self._safe(lambda: self.iconbitmap(ico)))
        self.go(t, rng)

    # ---- utilidades --------------------------------------------------------
    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception:
            pass

    def _btn(self, parent, text, width, cmd):
        return ctk.CTkButton(parent, text=text, width=width, height=34, corner_radius=12,
                             fg_color="transparent", border_width=1, border_color=COL_LINE,
                             hover_color=COL_HOVER, text_color=COL_INK,
                             text_color_disabled=COL_MUTED, font=ui_font(12), command=cmd)

    def _segs(self):
        return self.app._plan_now()

    def _seg_at(self, t):
        segs = self._segs()
        for s in segs:
            if s[1] <= t < s[2] + 1e-6:
                return s
        return segs[-1]

    def _seg_range(self, t):
        s = self._seg_at(t)
        n = int(math.ceil(self.total))
        a, b = int(round(s[1])), int(math.ceil(s[2] - 1e-6))
        a, b = max(0, a), min(n, b)
        if b - a < 1:
            b = min(n, a + 1)
            a = max(0, b - 1)
        return a, b

    # ---- navegación --------------------------------------------------------
    def go(self, t, rng=None):
        """Va al segundo `t`; `rng` fija el rango a editar (si no, el tramo de ese momento)."""
        self.t = min(max(float(t), 0.0), self.total)
        if rng is not None:
            self.rng = (int(rng[0]), int(rng[1]))
        elif not (self.rng[0] <= self.t < self.rng[1]):
            self.rng = self._seg_range(self.t)
        self.slider.set(self.t)
        self._refresh_labels()
        self._schedule_fetch()

    def _on_slider(self, value):
        self.go(float(value))

    def _nudge(self, d):
        self.go(self.t + d)

    def _jump(self, d):
        starts = [s[1] for s in self._segs()]
        if d < 0:
            prev = [x for x in starts if x < self.t - 0.05]
            self.go(prev[-1] if prev else 0.0)
        else:
            nxt = [x for x in starts if x > self.t + 0.05]
            self.go(nxt[0] if nxt else self.total)

    def _step_range(self, which, d):
        a, b = self.rng
        n = int(math.ceil(self.total))
        if which == 0:
            a = min(max(0, a + d), b - 1)
        else:
            b = max(min(n, b + d), a + 1)
        self.rng = (a, b)
        self._refresh_labels()

    # ---- edición -----------------------------------------------------------
    def _apply(self, cam):
        a, b = self.rng
        self.app._set_override(a, b, cam)
        self._refresh_labels()
        self._schedule_fetch(result_only=True)

    def _auto_here(self):
        self.app._clear_override(*self.rng)
        self._refresh_labels()
        self._schedule_fetch(result_only=True)

    def _auto_all(self):
        self.app._clear_override(0, 10 ** 9)
        self._refresh_labels()
        self._schedule_fetch(result_only=True)

    def _chosen(self):
        return self._seg_at(self.t)[0]

    def _refresh_labels(self):
        a, b = self.rng
        self.time_lbl.configure(text=T("Momento: {t}", t=fmt_time(self.t)))
        edited = any(s in self.app.overrides for s in range(a, b))
        self.range_lbl.configure(text=T("Tramo {a} a {b}", a=fmt_time(a), b=fmt_time(b)) +
                                 ("  " + T("(editado a mano)") if edited else ""))
        self.auto_btn.configure(state="normal" if edited else "disabled")
        self.clear_btn.configure(state="normal" if self.app.overrides else "disabled")
        chosen = self._chosen()
        for i, (card, _) in enumerate(self.thumbs):
            card.configure(border_color=CAM_COLORS[i % len(CAM_COLORS)] if i == chosen else COL_BORDER)
        self.res_name.configure(text=self.names[chosen])
        self.app._set_playhead(self.t, self.rng)

    # ---- imágenes (se piden en hilos; se muestran en el hilo principal) -----
    def _schedule_fetch(self, result_only=False):
        if self._fetch_id:
            self.after_cancel(self._fetch_id)
        self._fetch_id = self.after(130, lambda: self._fetch(result_only))

    def _fetch(self, result_only=False):
        self._fetch_id = None
        if self._closed:
            return
        self._gen += 1
        gen = self._gen
        chosen = self._chosen()
        vertical = self.app.vertical
        crop = {}
        for i, c in enumerate(self.app.cards):
            crop[i] = c.get_data().get("crop_x", 0.5)
        jobs = [("R", chosen, 720 if vertical else 460)]
        if not result_only:
            jobs += [(i, i, self.THUMB_W) for i in range(len(self.paths))]
        with self._lock:
            self._fresh = {}
            self._want = len(jobs)
        for key, cam, width in jobs:
            threading.Thread(target=self._work, args=(gen, key, cam, width, vertical, crop.get(cam, 0.5)),
                             daemon=True).start()
        if self._poll_id is None:
            self._poll_id = self.after(60, self._poll)

    def _work(self, gen, key, cam, width, vertical, crop_x):
        t = min(self.voff.get(cam, 0.0) + self.t, max(0.0, self.durs[cam] - 0.1))
        ck = (cam, int(t * 4), width)
        img = self._cache.get(ck)
        if img is None:
            img = grab_frame(self.paths[cam], t, width)
            if img is not None:
                if len(self._cache) > 120:
                    self._cache.clear()
                self._cache[ck] = img
        if img is not None and vertical:
            x1, x2 = vertical_box(img.width, img.height, crop_x)
            if key == "R":
                img = img.crop((x1, 0, x2, img.height))
            else:
                from PIL import ImageDraw
                img = img.copy()
                ImageDraw.Draw(img).rectangle((x1, 0, x2 - 1, img.height - 1), outline="#FFFFFF", width=3)
        with self._lock:
            if gen == self._gen:
                self._fresh[key] = img

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        with self._lock:
            got, want = dict(self._fresh), self._want
        for key, img in got.items():
            if key in self._imgs and self._imgs[key][0] is img:
                continue
            lbl = self.res_lbl if key == "R" else self.thumbs[key][1]
            if img is None:
                lbl.configure(image=None, text=T("Sin imagen"))
                self._imgs[key] = (img, None)
            else:
                ci = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                lbl.configure(image=ci, text="")
                self._imgs[key] = (img, ci)
        if len(got) < want:
            self._poll_id = self.after(60, self._poll)

    def close(self):
        self._closed = True
        for ident in (self._fetch_id, self._poll_id):
            if ident:
                self._safe(lambda: self.after_cancel(ident))
        self.app._editor_closed(self)
        self.destroy()


class ClipPlayer(ctk.CTkToplevel):
    """Reproduce la prueba rápida dentro de la app (imagen a 10 cuadros/s; sonido en Windows)."""
    FPS = 10

    def __init__(self, app, mp4, aspect):
        super().__init__(app, fg_color=COL_BG)
        self.app = app
        self.mp4 = mp4
        self.title(T("Prueba rápida"))
        self.transient(app)
        if aspect < 1:                                   # vertical
            self.fw, self.fh = 270, 480
        else:
            self.fw, self.fh = 480, even(480 / aspect)
        self.geometry(f"{max(self.fw + 80, 560)}x{self.fh + 190}")
        self.resizable(False, False)
        self.frames = []
        self.wav = None
        self.slice = None
        self.ready = False
        self.playing = False
        self.pos = 0
        self._t0 = 0.0
        self._closed = False
        self._proc = None
        self._tick_id = None
        self._ref = None

        self.columnconfigure(0, weight=1)
        self.view = ctk.CTkLabel(self, text=T("Preparando la prueba..."), text_color=COL_MUTED,
                                 fg_color=COL_SOFT, corner_radius=14, width=self.fw, height=self.fh)
        self.view.grid(row=0, column=0, padx=24, pady=(20, 8))
        self.slider = ctk.CTkSlider(self, from_=0, to=1, number_of_steps=1, height=16,
                                    fg_color=COL_TRACK, progress_color=COL_PROGRESS,
                                    button_color=COL_KNOB, button_hover_color=COL_KNOB_HOVER,
                                    command=self._seek, state="disabled")
        self.slider.grid(row=1, column=0, padx=30, pady=(0, 4), sticky="ew")
        self.time_lbl = ctk.CTkLabel(self, text="", text_color=COL_MUTED, font=ui_font(11))
        self.time_lbl.grid(row=2, column=0)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=3, column=0, pady=(6, 14))
        self.play_btn = ctk.CTkButton(bar, text=T("Reproducir"), width=120, height=36, corner_radius=12,
                                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER,
                                      text_color=COL_ACCENT_INK, text_color_disabled=COL_MUTED,
                                      font=ui_font(12, True), state="disabled", command=self._toggle)
        self.play_btn.grid(row=0, column=0, padx=4)
        ctk.CTkButton(bar, text=T("Abrir en tu reproductor"), width=170, height=36, corner_radius=12,
                      fg_color="transparent", border_width=1, border_color=COL_LINE,
                      hover_color=COL_HOVER, text_color=COL_INK, font=ui_font(12),
                      command=lambda: open_file(self.mp4)).grid(row=0, column=1, padx=4)
        ctk.CTkButton(bar, text=T("Cerrar"), width=90, height=36, corner_radius=12,
                      fg_color="transparent", border_width=1, border_color=COL_LINE,
                      hover_color=COL_HOVER, text_color=COL_INK, font=ui_font(12),
                      command=self.close).grid(row=0, column=2, padx=4)
        if os.name != "nt":
            ctk.CTkLabel(self, text=T("Aquí se ve sin sonido; usa «Abrir en tu reproductor» para oírla."),
                         text_color=COL_MUTED, font=ui_font(11)).grid(row=4, column=0, pady=(0, 10))

        self.protocol("WM_DELETE_WINDOW", self.close)
        ico = resource_path("icon.ico")
        if os.name == "nt" and os.path.exists(ico):
            self.after(300, lambda: CutEditor._safe(lambda: self.iconbitmap(ico)))
        threading.Thread(target=self._load, daemon=True).start()
        self.after(150, self._wait_ready)

    # ---- carga (hilo): fotogramas y sonido ---------------------------------
    def _load(self):
        ff = moviepy_ffmpeg()
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            if os.name == "nt":
                fd, wav = tempfile.mkstemp(suffix=".wav", prefix="AutoDirectorPro_snd_")
                os.close(fd)
                subprocess.run([ff, "-v", "error", "-y", "-i", self.mp4, "-vn", "-ac", "2", "-ar", "44100",
                                "-acodec", "pcm_s16le", wav], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=120, creationflags=flags)
                self.wav = wav
            size = self.fw * self.fh * 3
            self._proc = subprocess.Popen(
                [ff, "-v", "error", "-i", self.mp4, "-vf", f"fps={self.FPS},scale={self.fw}:{self.fh}",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=flags)
            while not self._closed:
                buf = self._proc.stdout.read(size)
                if len(buf) < size:
                    break
                self.frames.append(buf)
            self._proc.wait()
        except Exception:
            pass
        self.ready = True

    def _wait_ready(self):
        if self._closed:
            return
        if not self.ready:
            self.time_lbl.configure(text=f"{len(self.frames) / self.FPS:.0f} s")
            self.after(150, self._wait_ready)
            return
        if not self.frames:
            self.view.configure(text=T("No se pudo mostrar la prueba aquí. Usa «Abrir en tu reproductor»."))
            return
        self.slider.configure(to=max(1, len(self.frames) - 1),
                              number_of_steps=max(1, len(self.frames) - 1), state="normal")
        self.play_btn.configure(state="normal")
        self._show(0)
        self._play(0)

    # ---- reproducción ------------------------------------------------------
    def _show(self, idx):
        idx = min(max(0, idx), len(self.frames) - 1)
        self.pos = idx
        img = Image.frombytes("RGB", (self.fw, self.fh), self.frames[idx])
        ci = ctk.CTkImage(light_image=img, dark_image=img, size=(self.fw, self.fh))
        self.view.configure(image=ci, text="")
        self._ref = ci
        self.slider.set(idx)
        self.time_lbl.configure(
            text=f"{idx / self.FPS:.1f} s / {len(self.frames) / self.FPS:.1f} s")

    def _audio_stop(self):
        if os.name == "nt":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def _audio_start(self, seconds):
        """Reproduce el sonido desde `seconds` (corta una copia del WAV desde ahí)."""
        if os.name != "nt" or not self.wav or not os.path.exists(self.wav):
            return
        try:
            import wave
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
            with wave.open(self.wav, "rb") as w:
                w.setpos(min(int(seconds * w.getframerate()), max(0, w.getnframes() - 1)))
                data = w.readframes(w.getnframes())
                params = w.getparams()
            if self.slice is None:
                fd, self.slice = tempfile.mkstemp(suffix=".wav", prefix="AutoDirectorPro_snd_")
                os.close(fd)
            with wave.open(self.slice, "wb") as o:
                o.setparams(params)
                o.writeframes(data)
            winsound.PlaySound(self.slice, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass

    def _play(self, idx):
        if idx >= len(self.frames) - 1:
            idx = 0
        self.playing = True
        self.play_btn.configure(text=T("Pausar"))
        self._t0 = time.perf_counter() - idx / self.FPS
        self._audio_start(idx / self.FPS)
        self._tick()

    def _pause(self):
        self.playing = False
        self._audio_stop()
        self.play_btn.configure(text=T("Reproducir"))

    def _toggle(self):
        if not self.frames:
            return
        if self.playing:
            self._pause()
        else:
            self._play(self.pos)

    def _seek(self, value):
        if not self.frames:
            return
        idx = int(round(value))
        self._show(idx)
        if self.playing:
            self._t0 = time.perf_counter() - idx / self.FPS
            self._audio_start(idx / self.FPS)

    def _tick(self):
        if self._closed or not self.playing:
            return
        idx = int((time.perf_counter() - self._t0) * self.FPS)
        if idx >= len(self.frames):
            self.playing = False
            self._audio_stop()
            self._show(len(self.frames) - 1)
            self.play_btn.configure(text=T("Repetir"))
            return
        if idx != self.pos:
            self._show(idx)
        self._tick_id = self.after(25, self._tick)

    def close(self):
        self._closed = True
        self.playing = False
        self._audio_stop()
        if self._tick_id:
            CutEditor._safe(lambda: self.after_cancel(self._tick_id))
        if self._proc is not None:
            CutEditor._safe(self._proc.kill)
        for f in (self.wav, self.slice):
            if f:
                CutEditor._safe(lambda f=f: os.remove(f))
        self.frames = []
        self.app._player = None
        self.destroy()


class AutoDirectorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        set_language(detect_language())          # Idioma guardado o el del sistema
        self._i18n = []                          # (widget, opción, texto en español) para cambiar de idioma
        self.vertical = False                    # ¿formato vertical elegido?
        self.title("AutoDirector Pro | Hablemos Entonces Producciones")
        self.geometry("980x800")
        self.minsize(860, 700)
        self.configure(fg_color=COL_BG)
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.cards = []                          # CameraCard activas
        self.busy = False                        # ¿hay un trabajo en curso?
        self.cancel_event = threading.Event()    # Señal de cancelación para el hilo
        self.analysis = None                     # Último análisis (cache para la vista previa)
        self.donate_url = DONATE_URL             # Puede cambiarlo version.json sin actualizar el programa
        self._checking = False                   # ¿se está buscando una actualización?
        self._update_info = None                 # Datos de la actualización disponible
        self.overrides = {}                      # Ediciones manuales {segundo: índice de cámara}
        self._pv = None                          # Geometría de la línea de tiempo (para los clics)
        self._drag = None                        # Arrastre en curso sobre la línea de tiempo
        self._sel = None                         # Tramo seleccionado (inicio_s, fin_s)
        self._editor = None                      # Ventana del editor de cortes con imágenes
        self._player = None                      # Ventana de la prueba rápida
        self._playhead = None                    # Momento que se está mirando (segundos)
        self.log = collections.deque(maxlen=250)  # Historial para el informe de error
        self.last_error = ""                     # Último error con detalle técnico
        self.gpu_label = "sin comprobar"         # Codificador de GPU detectado

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)           # La lista de cámaras absorbe el espacio

        self._build_header()
        self._build_camera_area()
        self._build_preview()
        self._build_controls()

        self.add_camera()                        # Mínimo 1 cámara
        self._apply_language()                   # Pone todos los textos en el idioma activo
        self._load_logo_async()
        self._set_window_icon()
        self.after(2000, self._check_updates)    # Busca actualizaciones poco después de abrir

    def _set_window_icon(self):
        """Icono de la ventana y de la barra de tareas (solo Windows)."""
        ico = resource_path("icon.ico")
        if os.name != "nt" or not os.path.exists(ico):
            return

        def apply():
            try:
                self.iconbitmap(ico)
            except Exception:
                pass
        # CustomTkinter pone su propio icono ~200 ms después de abrir: se lo cambia después
        self.after(300, apply)

    # ---- Cabecera ----------------------------------------------------------
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=28, pady=(20, 8), sticky="ew")
        header.columnconfigure(1, weight=1)

        self.logo_label = ctk.CTkLabel(header, text="", width=64, height=64)
        self.logo_label.grid(row=0, column=0, rowspan=2, padx=(0, 16))

        ctk.CTkLabel(header, text="AutoDirector Pro", text_color=COL_INK,
                     font=ui_font(23, True), anchor="w"
                     ).grid(row=0, column=1, sticky="sw")
        self.subtitle_lbl = ctk.CTkLabel(
            header, text="Cortes automáticos para podcasts con varias cámaras",
            text_color=COL_MUTED, font=ui_font(12), anchor="w")
        self.subtitle_lbl.grid(row=1, column=1, sticky="nw")
        self._reg(self.subtitle_lbl)

        # Columna derecha: donar + tema, y debajo la versión
        right = ctk.CTkFrame(header, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=2, sticky="e")

        self.donate_btn = ctk.CTkButton(
            right, text="Apoyar el proyecto", width=148, height=30, corner_radius=15,
            fg_color=COL_PEACH, hover_color=COL_PEACH_HOVER, text_color=COL_PEACH_INK,
            font=ui_font(12, True), command=self._open_donate)
        self.donate_btn.grid(row=0, column=0, padx=(0, 10), sticky="e")
        self._reg(self.donate_btn)

        self.help_btn = ctk.CTkButton(
            right, text="Ayuda", width=70, height=30, corner_radius=15,
            fg_color="transparent", border_width=1, border_color=COL_LINE,
            hover_color=COL_HOVER, text_color=COL_INK, font=ui_font(12),
            command=lambda: webbrowser.open(HELP_URL))
        self.help_btn.grid(row=0, column=1, padx=(0, 10), sticky="e")
        self._reg(self.help_btn)

        # Selector de tema (Claro / Oscuro)
        self.theme_seg = ctk.CTkSegmentedButton(
            right, values=["Claro", "Oscuro"], command=self._toggle_theme,
            height=30, corner_radius=15, fg_color=COL_TRACK,
            selected_color=COL_SURFACE, selected_hover_color=COL_SURFACE,
            unselected_color=COL_TRACK, unselected_hover_color=COL_HOVER,
            text_color=COL_INK, font=ui_font(11))
        self.theme_seg.set("Claro")
        self.theme_seg.grid(row=0, column=2, sticky="e")

        self._version_text = self._make_version_text()
        self.version_btn = ctk.CTkButton(
            right, text=self._version_text, height=22, width=10, corner_radius=8,
            fg_color="transparent", hover_color=COL_HOVER, text_color=COL_MUTED,
            font=ui_font(11), command=lambda: self._check_updates(manual=True),
            state="normal" if UPDATE_REPO else "disabled", text_color_disabled=COL_MUTED)
        self.version_btn.grid(row=1, column=0, columnspan=2, pady=(4, 0), sticky="e")

        # Selector de idioma (se recuerda para la próxima vez)
        self.lang_seg = ctk.CTkSegmentedButton(
            right, values=["Español", "English"], command=self._on_language,
            height=24, corner_radius=12, fg_color=COL_TRACK,
            selected_color=COL_SURFACE, selected_hover_color=COL_SURFACE,
            unselected_color=COL_TRACK, unselected_hover_color=COL_HOVER,
            text_color=COL_INK, font=ui_font(11))
        self.lang_seg.set("Español")
        self.lang_seg.grid(row=1, column=2, pady=(6, 0), sticky="e")

        # Informe de error: copia al portapapeles todo lo que hace falta para dar soporte
        self.report_btn = ctk.CTkButton(
            right, text="Copiar informe de error", height=22, width=10, corner_radius=8,
            fg_color="transparent", hover_color=COL_HOVER, text_color=COL_MUTED,
            font=ui_font(11), command=self.copy_report)
        self.report_btn.grid(row=2, column=0, columnspan=3, pady=(0, 0), sticky="e")
        self._reg(self.report_btn)

        self._build_update_bar(header)
        self._refresh_donate()

    # ---- Idioma ---------------------------------------------------------------
    def _reg(self, widget, option="text"):
        """Registra un widget para que su texto cambie con el idioma (guarda el texto en español)."""
        self._i18n.append((widget, option, widget.cget(option)))
        return widget

    def _make_version_text(self):
        return (f"{T('Versión')} {APP_VERSION}   {T('Buscar actualizaciones')}"
                if UPDATE_REPO else f"{T('Versión')} {APP_VERSION}")

    def _on_language(self, value):
        set_language("en" if value == "English" else "es")
        save_settings(language=LANG)
        self._apply_language()

    def _apply_language(self):
        """Pone TODOS los textos en el idioma activo (también al cambiarlo en marcha)."""
        for widget, option, src in self._i18n:
            try:
                widget.configure(**{option: T(src)})
            except Exception:
                pass
        dark = ctk.get_appearance_mode() == "Dark"
        self.theme_seg.configure(values=[T("Claro"), T("Oscuro")])
        self.theme_seg.set(T("Oscuro") if dark else T("Claro"))
        self.fmt_seg.configure(values=[T("Horizontal 16:9"), T("Vertical 9:16")])
        self.fmt_seg.set(T("Vertical 9:16") if self.vertical else T("Horizontal 16:9"))
        self.lang_seg.set("English" if LANG == "en" else "Español")
        self._version_text = self._make_version_text()
        self.version_btn.configure(text=self._version_text)
        for card in self.cards:
            card.apply_language()
        if self.status_label.cget("text") in ("Listo.", "Ready."):
            self.status_label.configure(text=T("Listo."))
        if self._update_info:
            self._show_update(self._update_info)
        self._on_params_changed()
        self._draw_preview()

    # ---- Donaciones -----------------------------------------------------------
    def _refresh_donate(self):
        """Muestra el botón de donar solo si hay un enlace configurado."""
        if is_https(self.donate_url):
            self.donate_btn.grid()
        else:
            self.donate_btn.grid_remove()

    def _open_donate(self):
        if is_https(self.donate_url):
            webbrowser.open(self.donate_url)

    # ---- Aviso de actualización -------------------------------------------------
    def _build_update_bar(self, header):
        bar = ctk.CTkFrame(header, fg_color=COL_MINT, corner_radius=14)
        bar.grid(row=2, column=0, columnspan=3, pady=(14, 0), sticky="ew")
        bar.columnconfigure(0, weight=1)

        self.update_title = ctk.CTkLabel(bar, text="", text_color=COL_MINT_INK,
                                         font=ui_font(12, True), anchor="w")
        self.update_title.grid(row=0, column=0, padx=(18, 8), pady=(10, 0), sticky="w")
        self.update_notes = ctk.CTkLabel(bar, text="", text_color=COL_MINT_INK, font=ui_font(11),
                                         anchor="w", justify="left", wraplength=520)
        self.update_notes.grid(row=1, column=0, padx=(18, 8), pady=(0, 10), sticky="w")

        self.update_now_btn = ctk.CTkButton(
            bar, text="Actualizar ahora", width=130, height=32, corner_radius=10,
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER, text_color=COL_ACCENT_INK,
            text_color_disabled=COL_MUTED, font=ui_font(12, True), command=self._do_update)
        self.update_now_btn.grid(row=0, column=1, rowspan=2, padx=(8, 6), pady=10)

        self.update_later_btn = ctk.CTkButton(
            bar, text="Más tarde", width=90, height=32, corner_radius=10,
            fg_color="transparent", hover_color=COL_MINT_HOVER, text_color=COL_MINT_INK,
            font=ui_font(12), command=self._hide_update)
        self.update_later_btn.grid(row=0, column=2, rowspan=2, padx=(0, 12), pady=10)
        self._reg(self.update_later_btn)

        self.update_bar = bar
        bar.grid_remove()                        # Oculto hasta que haya una versión nueva

    def _hide_update(self):
        self.update_bar.grid_remove()

    def _flash_version_text(self, text):
        """Muestra un mensaje breve en el texto de la versión y luego lo restaura."""
        self.version_btn.configure(text=text)
        self.after(4000, lambda: self.version_btn.configure(text=self._version_text))

    def _check_updates(self, manual=False):
        """
        Busca una versión nueva en segundo plano. El hilo solo descarga el
        version.json; todo lo que toca la ventana se hace en el hilo principal.
        """
        if not UPDATE_REPO or self._checking:
            return
        self._checking = True
        if manual:
            self.version_btn.configure(text=T("Buscando..."))
        box = {}

        def worker():
            try:
                box["info"] = fetch_update_info()
            except Exception:
                pass
            box["done"] = True

        def poll():
            if not box.get("done"):
                self.after(200, poll)
                return
            self._checking = False
            info = box.get("info")
            if info is None:
                if manual:
                    self._flash_version_text(T("No se pudo comprobar. Revisa tu conexión."))
                else:
                    self.version_btn.configure(text=self._version_text)
                return
            if is_https(info.get("donate_url")):
                self.donate_url = info["donate_url"]
                self._refresh_donate()
            if is_newer(info["version"], APP_VERSION):
                self._show_update(info)
                self.version_btn.configure(text=self._version_text)
            elif manual:
                self._flash_version_text(T("Ya tienes la última versión."))

        threading.Thread(target=worker, daemon=True).start()
        self.after(200, poll)

    def _show_update(self, info):
        self._update_info = info
        notes = (info.get("notes_en") if LANG == "en" else "") or info["notes"]
        self.update_title.configure(text=T("Hay una versión nueva: {v}", v=info["version"]))
        self.update_notes.configure(text=notes or T("Incluye mejoras y correcciones."))
        self.update_now_btn.configure(state="normal", text=T("Actualizar ahora"))
        self.update_bar.grid()

    def _do_update(self):
        info = self._update_info
        if not info:
            return
        if self.busy:
            messagebox.showinfo("AutoDirector Pro",
                                T("Termina o cancela el trabajo en curso antes de actualizar."))
            return
        # Solo el programa instalado (.exe de Windows) se actualiza solo; en otro
        # caso se abre la página de descarga.
        if not (getattr(sys, "frozen", False) and os.name == "nt" and is_https(info["installer_url"])):
            webbrowser.open(info["page_url"])
            return

        dest_dir = os.path.join(tempfile.gettempdir(), "AutoDirectorPro-Update")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, INSTALLER_NAME)
        self.update_now_btn.configure(state="disabled", text=T("Descargando {p}%", p=0))
        box = {"pct": 0.0}

        def worker():
            try:
                download_file(info["installer_url"], dest, lambda f: box.__setitem__("pct", f))
                if info["sha256"] and sha256_of(dest) != info["sha256"]:
                    raise RuntimeError(T("El archivo descargado no coincide con el publicado."))
                box["ok"] = True
            except Exception as exc:  # noqa: BLE001
                box["error"] = str(exc)
            box["done"] = True

        def poll():
            if not box.get("done"):
                self.update_now_btn.configure(text=T("Descargando {p}%", p=int(box["pct"] * 100)))
                self.after(250, poll)
                return
            if box.get("ok"):
                self.update_now_btn.configure(text=T("Abriendo instalador..."))
                try:
                    os.startfile(dest)          # Abre el instalador (Windows)
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror("AutoDirector Pro", T("No se pudo abrir el instalador: {e}", e=exc))
                    self.update_now_btn.configure(state="normal", text=T("Actualizar ahora"))
                    return
                self.after(800, self.destroy)    # Se cierra para que el instalador pueda reemplazarlo
            else:
                self.update_now_btn.configure(state="normal", text=T("Actualizar ahora"))
                if messagebox.askyesno("AutoDirector Pro",
                                       T("No se pudo descargar la actualización.") + f"\n{box.get('error', '')}\n\n"
                                       + T("¿Quieres abrir la página de descarga?")):
                    webbrowser.open(info["page_url"])

        threading.Thread(target=worker, daemon=True).start()
        self.after(250, poll)

    def _toggle_theme(self, value):
        """
        Cambia entre claro y oscuro con un fundido suave: se toma una captura de
        la ventana, se cambia el tema por debajo y la captura se desvanece.
        Si algo falla (captura no disponible, etc.) el cambio es directo.
        """
        mode = "dark" if value == T("Oscuro") else "light"
        if ctk.get_appearance_mode().lower() == mode:
            return
        self._cancel_fade()
        overlay = None
        try:
            overlay = self._snapshot_overlay()
        except Exception:
            overlay = None

        ctk.set_appearance_mode(mode)
        self._draw_preview()                     # El canvas no cambia de color solo
        if overlay is not None:
            self.update_idletasks()              # Que el tema nuevo ya esté dibujado debajo
            self._fade_win = overlay
            self._fade_step(overlay, 0)

    def _snapshot_overlay(self):
        """Ventana sin bordes con una captura de la app, encima de ella."""
        from PIL import ImageGrab, ImageTk
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 50 or self.state() == "iconic":
            return None
        x, y = self.winfo_rootx(), self.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.geometry(f"{w}x{h}+{x}+{y}")
        photo = ImageTk.PhotoImage(img)
        label = tk.Label(top, image=photo, bd=0, highlightthickness=0)
        label.image = photo                      # Evita que se borre la imagen
        label.pack()
        top.attributes("-topmost", True)
        top.attributes("-alpha", 1.0)
        top.update()                             # Se dibuja antes de cambiar el tema
        return top

    def _fade_step(self, win, step, steps=16):
        """Un paso del desvanecimiento (curva suave: empieza y termina despacio)."""
        if win is not getattr(self, "_fade_win", None):
            return                               # Lo canceló un cambio de tema más reciente
        try:
            t = step / steps
            alpha = 1.0 - t * t * (3 - 2 * t)
            win.attributes("-alpha", max(0.0, alpha))
            if step >= steps:
                self._cancel_fade()
                return
        except tk.TclError:
            self._cancel_fade()
            return
        self.after(20, lambda: self._fade_step(win, step + 1, steps))

    def _cancel_fade(self):
        win = getattr(self, "_fade_win", None)
        self._fade_win = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def _load_logo_async(self):
        """
        Muestra el logo. Si el programa lo trae incluido (logo.png) se carga al
        instante; si no, se descarga de internet en un hilo, que solo baja los
        bytes: la imagen se crea siempre en el hilo principal (tkinter no es
        seguro desde otros hilos).
        """
        local = resource_path("logo.png")
        if os.path.exists(local):
            try:
                self._show_logo(Image.open(local))
                return
            except Exception:
                pass

        box = {}

        def worker():
            try:
                req = urllib.request.Request(LOGO_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    box["data"] = resp.read()
            except Exception:
                pass
            box["done"] = True

        def poll():
            if not box.get("done"):
                self.after(150, poll)
                return
            try:
                self._show_logo(Image.open(io.BytesIO(box["data"])))
            except Exception:
                self.logo_label.grid_remove()    # Sin internet: se oculta el espacio del logo

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, poll)

    def _show_logo(self, img):
        img = img.convert("RGBA")
        img.thumbnail((150, 64))
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self._logo_ref = ctk_img                 # Evita que lo borre el recolector de basura
        self.logo_label.configure(image=ctk_img, text="", width=img.size[0])

    # ---- Cámaras (con scroll) ----------------------------------------------
    def _build_camera_area(self):
        box = ctk.CTkFrame(self, fg_color=COL_SURFACE, border_width=1,
                           border_color=COL_BORDER, corner_radius=22)
        box.grid(row=1, column=0, padx=24, pady=6, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        top = ctk.CTkFrame(box, fg_color="transparent")
        top.grid(row=0, column=0, padx=20, pady=(16, 6), sticky="ew")
        top.columnconfigure(1, weight=1)
        cams_title = ctk.CTkLabel(top, text="Cámaras", text_color=COL_INK, font=ui_font(15, True))
        cams_title.grid(row=0, column=0, sticky="w")
        cams_hint = ctk.CTkLabel(top, text="Si una cámara no tiene audio, hace de plano general.",
                                 text_color=COL_MUTED, font=ui_font(11), anchor="w")
        cams_hint.grid(row=0, column=1, padx=(14, 0), sticky="w")
        self._reg(cams_title)
        self._reg(cams_hint)
        self.add_btn = ctk.CTkButton(
            top, text="Agregar cámara", width=128, height=30, corner_radius=10,
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER, text_color=COL_ACCENT_INK,
            font=ui_font(12, True), command=self.add_camera)
        self.add_btn.grid(row=0, column=2, sticky="e")
        self._reg(self.add_btn)

        self.cam_frame = ctk.CTkScrollableFrame(
            box, fg_color="transparent",
            scrollbar_button_color=COL_LINE, scrollbar_button_hover_color=COL_MUTED)
        self.cam_frame.grid(row=1, column=0, padx=10, pady=(0, 12), sticky="nsew")
        self.cam_frame.columnconfigure(0, weight=1)

    def add_camera(self):
        card = CameraCard(self.cam_frame, len(self.cards) + 1, self.remove_camera, self.invalidate)
        card.grid(row=len(self.cards), column=0, padx=8, pady=5, sticky="ew")
        card.show_crop(self._is_vertical())
        self.cards.append(card)
        self.invalidate()

    def remove_camera(self, card):
        if len(self.cards) <= 1:
            messagebox.showwarning("AutoDirector Pro", T("Debe existir al menos una cámara."))
            return
        self.cards.remove(card)
        card.destroy()
        for i, c in enumerate(self.cards, start=1):
            c.set_index(i)
            c.grid(row=i - 1, column=0, padx=8, pady=5, sticky="ew")
        self.invalidate()

    def invalidate(self):
        """Si cambian cámaras o archivos, el análisis guardado deja de valer."""
        self.analysis = None
        self.overrides = {}              # Las ediciones manuales dependían de ese análisis
        self._sel = None
        self._playhead = None
        if getattr(self, "_editor", None) is not None:
            try:
                self._editor.close()
            except Exception:
                pass
        if hasattr(self, "canvas"):
            self._draw_preview()

    def _is_vertical(self):
        return self.vertical

    def _on_format(self, value=None):
        if value is None:
            value = self.fmt_seg.get()
        self.vertical = (value == T("Vertical 9:16"))
        vertical = self.vertical
        for card in self.cards:
            card.show_crop(vertical)

    # ---- Línea de tiempo visual (vista previa) -----------------------------
    def _build_preview(self):
        pv = ctk.CTkFrame(self, fg_color=COL_SURFACE, border_width=1,
                          border_color=COL_BORDER, corner_radius=22)
        pv.grid(row=2, column=0, padx=24, pady=6, sticky="ew")
        pv.columnconfigure(0, weight=1)

        tl_title = ctk.CTkLabel(pv, text="Línea de tiempo", text_color=COL_INK, font=ui_font(15, True))
        tl_title.grid(row=0, column=0, padx=20, pady=(16, 0), sticky="w")
        self._reg(tl_title)
        self.preview_info = ctk.CTkLabel(pv, text="", text_color=COL_MUTED, font=ui_font(12))
        self.preview_info.grid(row=0, column=0, padx=20, pady=(16, 0), sticky="e")

        self.canvas = tk.Canvas(pv, height=96, highlightthickness=0, bd=0)
        self.canvas.grid(row=1, column=0, padx=18, pady=(6, 16), sticky="ew")
        self.canvas.bind("<Configure>", lambda e: self._draw_preview())
        # Edición de cortes: clic en un tramo del "Resultado" o arrastrar para elegir un rango
        self.canvas.bind("<ButtonPress-1>", self._on_cut_press)
        self.canvas.bind("<B1-Motion>", self._on_cut_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_cut_release)
        self.canvas.bind("<Motion>", self._on_cut_hover)
        self.canvas.bind("<Button-3>", self._on_cut_right)
        self.edit_btn = ctk.CTkButton(
            pv, text="Editar cortes con imágenes", width=210, height=34, corner_radius=12,
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER, text_color=COL_ACCENT_INK,
            font=ui_font(12, True), command=lambda: self._open_editor(self._playhead or 0.0))
        self.edit_btn.grid(row=2, column=0, padx=20, pady=(0, 14), sticky="e")
        self._reg(self.edit_btn)

    # ---- Edición manual de cortes ------------------------------------------
    def _over_result(self, y):
        pv = self._pv
        return bool(pv) and pv["y1"] - 4 <= y <= pv["y2"] + 4

    def _t_at(self, x):
        pv = self._pv
        t = (x - pv["left"]) / pv["span"] * pv["total"]
        return min(max(t, 0.0), pv["total"])

    def _on_cut_hover(self, e):
        self.canvas.configure(cursor="hand2" if (not self.busy and self._over_result(e.y)) else "")

    def _on_cut_press(self, e):
        self._sel = None
        self._drag = None
        if self.busy or not self._over_result(e.y):
            self._draw_preview()
            return
        self._drag = [self._t_at(e.x), self._t_at(e.x), e.x]
        self._sel = (self._drag[0], self._drag[1])
        self._draw_preview()

    def _on_cut_drag(self, e):
        if self._drag:
            self._drag[1] = self._t_at(e.x)
            self._sel = tuple(sorted((self._drag[0], self._drag[1])))
            self._draw_preview()

    def _on_cut_release(self, e):
        d, self._drag = self._drag, None
        if not d or not self._pv:
            return
        total = self._pv["total"]
        if abs(e.x - d[2]) < 5:                       # clic simple: el tramo que está debajo
            seg = next((s for s in self._pv["segs"] if s[1] <= d[0] < s[2] + 1e-6), None)
            if not seg:
                self._sel = None
                self._draw_preview()
                return
            a, b = int(round(seg[1])), int(math.ceil(seg[2] - 1e-6))
        else:                                         # arrastre: rango redondeado a segundos
            t0, t1 = sorted((d[0], self._t_at(e.x)))
            a, b = int(math.floor(t0)), int(math.ceil(t1))
        n = int(math.ceil(total))
        a, b = max(0, a), min(n, b)
        if b - a < 1:
            b = min(n, a + 1)
            a = max(0, b - 1)
        self._sel = (float(a), float(b))
        self._draw_preview()
        self._open_editor((a + b) / 2.0 if abs(e.x - d[2]) >= 5 else d[0], (a, b))

    def _on_cut_right(self, e):
        """Clic derecho: menú rápido para el tramo (sin abrir el editor)."""
        if self.busy or not self._pv or not self._over_result(e.y):
            return
        t = self._t_at(e.x)
        seg = next((s for s in self._pv["segs"] if s[1] <= t < s[2] + 1e-6), None)
        if seg:
            a, b = int(round(seg[1])), max(int(round(seg[1])) + 1, int(math.ceil(seg[2] - 1e-6)))
            self._sel = (float(a), float(b))
            self._draw_preview()
            self._show_cut_menu(e, a, b)

    def _open_editor(self, t, rng=None):
        if not self.analysis:
            messagebox.showinfo("AutoDirector Pro",
                                T("Primero analiza el audio (botón «Analizar y ver cortes»)."))
            return
        if self.busy:
            return
        if self._editor is not None and self._editor.winfo_exists():
            self._editor.go(t, rng)
            self._editor.lift()
            self._editor.focus_force()
            return
        self._editor = CutEditor(self, t, rng)

    def _editor_closed(self, ed):
        if self._editor is ed:
            self._editor = None
        self._playhead = None
        self._sel = None
        if hasattr(self, "canvas"):
            self._draw_preview()

    def _set_playhead(self, t, rng):
        self._playhead = t
        self._sel = (float(rng[0]), float(rng[1]))
        self._draw_preview()

    def _open_player(self, mp4, aspect):
        if self._player is not None:
            try:
                self._player.close()
            except Exception:
                pass
        try:
            self._player = ClipPlayer(self, mp4, aspect)
        except Exception:
            self._player = None
            open_file(mp4)

    def _show_cut_menu(self, e, a, b):
        names = self.analysis["names"] if self.analysis else []
        menu = tk.Menu(self, tearoff=0, font=(FONT_BASE, 10), bd=0,
                       bg=pick(COL_SURFACE), fg=pick(COL_INK),
                       activebackground=pick(COL_ACCENT), activeforeground=pick(COL_ACCENT_INK))
        menu.add_command(label=T("Tramo {a} a {b}", a=fmt_time(a), b=fmt_time(b)), state="disabled")
        menu.add_separator()
        for i, name in enumerate(names):
            menu.add_command(label=T("Mostrar a «{name}»", name=name),
                             command=lambda i=i: self._set_override(a, b, i))
        menu.add_separator()
        has_here = any(s in self.overrides for s in range(a, b))
        menu.add_command(label=T("Volver al corte automático (este tramo)"),
                         state="normal" if has_here else "disabled",
                         command=lambda: self._clear_override(a, b))
        menu.add_command(label=T("Quitar todas mis ediciones"),
                         state="normal" if self.overrides else "disabled",
                         command=lambda: self._clear_override(0, 10 ** 9))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _set_override(self, a, b, cam):
        for s in range(a, b):
            self.overrides[s] = cam
        self._sel = None
        self._draw_preview()
        self.status_label.configure(
            text=T("Corte cambiado: de {a} a {b} se muestra a {name}.",
                   a=fmt_time(a), b=fmt_time(b), name=self.analysis["names"][cam]))

    def _clear_override(self, a, b):
        for s in [s for s in self.overrides if a <= s < b]:
            del self.overrides[s]
        self._sel = None
        self._draw_preview()

    def _plan_now(self):
        """Plan de cortes con los controles actuales (hilo principal)."""
        return make_plan(self.analysis, float(self.thr_slider.get()),
                         int(round(self.cut_slider.get())), float(self.margin_slider.get()),
                         bool(self.sw_balance.get()), self.overrides)

    def _draw_preview(self):
        c = self.canvas
        bg, track = pick(COL_SURFACE), pick(COL_TRACK)
        ink, muted, line = pick(COL_INK), pick(COL_MUTED), pick(COL_LINE)
        c.configure(bg=bg)
        c.delete("all")
        width = max(c.winfo_width(), 300)
        a = self.analysis

        if not a:
            self._pv = None
            self._set_canvas_height(96)
            c.create_text(width / 2, 48, fill=muted, font=(FONT_BASE, 11),
                          text=T("Analiza el audio para ver quién habla y dónde caen los cortes."))
            self.preview_info.configure(text="")
            return

        thr = float(self.thr_slider.get())
        segs = self._plan_now()
        total = max(a["total_s"], 1.0)
        left, right = 128, width - 8
        span = max(1, right - left)

        def x(t):
            return left + min(t, total) / total * span

        lane_h, gap, final_h = 12, 12, 26
        cams = list(a["levels"].keys())
        y = 6
        y_final = y + len(cams) * (lane_h + gap) + 4
        y_end = y_final + final_h

        # Una pista por micrófono: barras redondeadas donde esa persona habla
        for lane in cams:
            color = CAM_COLORS[lane % len(CAM_COLORS)]
            cy = y + lane_h / 2
            c.create_oval(6, cy - 4, 14, cy + 4, fill=color, width=0)
            c.create_text(22, cy, anchor="w", fill=ink, font=(FONT_BASE, 10),
                          text=a["names"][lane][:14])
            round_rect(c, left, y, right, y + lane_h, 6, fill=track, outline="")
            lv = a["levels"][lane]
            s = 0
            while s < a["n_chunks"]:
                if s < len(lv) and lv[s] > thr:
                    e = s
                    while e < a["n_chunks"] and e < len(lv) and lv[e] > thr:
                        e += 1
                    round_rect(c, x(s), y, max(x(e), x(s) + 3), y + lane_h, 6, fill=color, outline="")
                    s = e
                else:
                    s += 1
            y += lane_h + gap

        # Resultado: el plano que se verá en cada momento
        c.create_text(6, y_final + final_h / 2, anchor="w", fill=ink,
                      font=(FONT_SEMI, 10), text=T("Resultado"))
        round_rect(c, left, y_final, right, y_end, 9, fill=track, outline="")
        for ci, s, e in segs:
            x1, x2 = x(s) + 1, x(e) - 1
            round_rect(c, x1, y_final, x2, y_end, 9, fill=CAM_COLORS[ci % len(CAM_COLORS)], outline="")
            if x2 - x1 > 80:
                c.create_text((x1 + x2) / 2, y_final + final_h / 2, fill=PASTEL_INK,
                              font=(FONT_BASE, 9), text=a["names"][ci][:14])

        # Marcas discretas en cada punto de corte
        for _, s, _ in segs[1:]:
            c.create_line(x(s), 2, x(s), y_end + 4, fill=line, dash=(2, 4))

        # Ediciones manuales: raya fina bajo el resultado
        edited = sorted(self.overrides)
        i = 0
        while i < len(edited):
            j = i
            while j + 1 < len(edited) and edited[j + 1] == edited[j] + 1:
                j += 1
            c.create_line(x(edited[i]) + 1, y_end + 5, x(edited[j] + 1) - 1, y_end + 5,
                          fill=ink, width=2, capstyle="round")
            i = j + 1

        # Momento que se mira en el editor
        if self._playhead is not None:
            c.create_line(x(self._playhead), 0, x(self._playhead), y_end + 6, fill=ink, width=2)

        # Tramo seleccionado
        if self._sel:
            c.create_rectangle(x(self._sel[0]), y_final - 3, x(self._sel[1]), y_end + 3,
                               outline=ink, width=2, dash=(4, 3))

        self._pv = {"left": left, "span": span, "total": total,
                    "y1": y_final, "y2": y_end, "segs": segs}

        # Eje de tiempo
        step = next((t for t in (10, 30, 60, 120, 300, 600, 1800, 3600) if t / total * span >= 70), 3600)
        t = 0
        while t <= total:
            c.create_text(x(t), y_end + 16, fill=muted, font=(FONT_BASE, 9), text=fmt_time(t))
            t += step

        self._set_canvas_height(int(y_end + 28))
        info = T("{n} cortes en {t}", n=len(segs), t=fmt_time(a["total_s"]))
        if self.overrides:
            info += " · " + T("{n} s editados a mano", n=len(self.overrides))
        else:
            info += " · " + T("haz clic en «Resultado» para cambiar un tramo")
        self.preview_info.configure(text=info)

    def _set_canvas_height(self, h):
        if int(float(self.canvas.cget("height"))) != h:
            self.canvas.configure(height=h)

    # ---- Controles inferiores ---------------------------------------------
    def _build_controls(self):
        panel = ctk.CTkFrame(self, fg_color=COL_SURFACE, border_width=1,
                             border_color=COL_BORDER, corner_radius=22)
        panel.grid(row=3, column=0, padx=24, pady=(6, 22), sticky="ew")
        panel.columnconfigure(0, weight=1)

        # --- Cuatro sliders: umbral, ventaja, plano mínimo, solape de audio ---
        sliders = ctk.CTkFrame(panel, fg_color="transparent")
        sliders.grid(row=0, column=0, padx=20, pady=(18, 4), sticky="ew")
        for col in range(4):
            sliders.columnconfigure(col, weight=1, uniform="sl")

        def make_slider(col, title, from_, to, steps, initial):
            cell = ctk.CTkFrame(sliders, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="ew",
                      padx=(0 if col == 0 else 10, 0 if col == 3 else 10))
            cell.columnconfigure(0, weight=1)
            title_lbl = ctk.CTkLabel(cell, text=title, text_color=COL_INK, font=ui_font(12, True))
            title_lbl.grid(row=0, column=0, sticky="w")
            self._reg(title_lbl)
            value = ctk.CTkLabel(cell, text="", text_color=COL_MUTED, font=ui_font(12))
            value.grid(row=0, column=1, sticky="e")
            slider = ctk.CTkSlider(
                cell, from_=from_, to=to, number_of_steps=steps, height=16,
                fg_color=COL_TRACK, progress_color=COL_PROGRESS,
                button_color=COL_KNOB, button_hover_color=COL_KNOB_HOVER,
                command=self._on_params_changed)
            slider.set(initial)
            slider.grid(row=1, column=0, columnspan=2, pady=(8, 0), sticky="ew")
            return slider, value

        self.thr_slider, self.thr_label = make_slider(0, "Umbral de voz", -60, -15, 45, -40)
        self.margin_slider, self.margin_label = make_slider(
            1, "Ventaja para cambiar", 0, 15, 15, MARGIN_DEFAULT)
        self.cut_slider, self.cut_label = make_slider(2, "Plano mínimo", 1, 6, 5, 2)
        self.ov_slider, self.ov_label = make_slider(3, "Solape de audio", 0, 1.5, 6, 0.5)

        # --- Interruptores: qué generar y ajustes ----------------------------
        opts = ctk.CTkFrame(panel, fg_color="transparent")
        opts.grid(row=1, column=0, padx=20, pady=(10, 2), sticky="ew")

        def make_switch(text, row, col, command=None):
            sw = ctk.CTkSwitch(
                opts, text=text, text_color=COL_INK, font=ui_font(12),
                switch_width=34, switch_height=18, fg_color=COL_LINE,
                progress_color=COL_PROGRESS, button_color=COL_WHITE,
                button_hover_color=COL_WHITE, command=command)
            sw.grid(row=row, column=col, padx=(0, 24), pady=4, sticky="w")
            self._reg(sw)
            return sw

        gen_lbl = ctk.CTkLabel(opts, text="Generar", text_color=COL_MUTED, font=ui_font(11),
                               width=62, anchor="w")
        gen_lbl.grid(row=0, column=0, sticky="w")
        self._reg(gen_lbl)
        self.sw_mp4 = make_switch("Video MP4", 0, 1)
        self.sw_xml = make_switch("XML para Premiere o DaVinci", 0, 2)
        self.sw_capcut = make_switch("Borrador de CapCut", 0, 3)

        set_lbl = ctk.CTkLabel(opts, text="Ajustes", text_color=COL_MUTED, font=ui_font(11),
                               width=62, anchor="w")
        set_lbl.grid(row=1, column=0, sticky="w")
        self._reg(set_lbl)
        self.sw_gpu = make_switch("Usar la GPU si hay", 1, 1)
        self.sw_voice = make_switch("Filtro de voz", 1, 2, command=self.invalidate)
        self.sw_balance = make_switch("Equilibrar micrófonos", 1, 3, command=self._on_params_changed)
        self.sw_sync = make_switch("Sincronizar cámaras por audio", 1, 4, command=self.invalidate)
        for sw in (self.sw_mp4, self.sw_xml, self.sw_capcut, self.sw_gpu,
                   self.sw_balance, self.sw_sync):
            sw.select()

        opts.columnconfigure(4, weight=1)
        self.fmt_seg = ctk.CTkSegmentedButton(
            opts, values=["Horizontal 16:9", "Vertical 9:16"], command=self._on_format,
            height=28, width=250, corner_radius=14, fg_color=COL_TRACK,
            selected_color=COL_ACCENT, selected_hover_color=COL_ACCENT_HOVER,
            unselected_color=COL_TRACK, unselected_hover_color=COL_HOVER,
            text_color=COL_INK, font=ui_font(11))
        self.fmt_seg.set("Horizontal 16:9")
        self.fmt_seg.grid(row=0, column=4, pady=4, sticky="e")      # Formato del video final
        self._on_params_changed()

        # --- Progreso y estado -----------------------------------------------
        self.progress = ctk.CTkProgressBar(panel, height=6, corner_radius=3,
                                           fg_color=COL_TRACK, progress_color=COL_PROGRESS)
        self.progress.set(0)
        self.progress.grid(row=2, column=0, padx=20, pady=(12, 4), sticky="ew")

        self.status_label = ctk.CTkLabel(panel, text="Listo.", text_color=COL_MUTED,
                                         font=ui_font(11), anchor="w")
        self.status_label.grid(row=3, column=0, padx=20, pady=(0, 2), sticky="ew")

        # --- Botones: la acción principal a la derecha ------------------------
        btns = ctk.CTkFrame(panel, fg_color="transparent")
        btns.grid(row=4, column=0, padx=20, pady=(8, 18), sticky="ew")
        btns.columnconfigure(0, weight=1)

        self.cancel_btn = ctk.CTkButton(
            btns, text="Cancelar", width=96, height=40, corner_radius=12, state="disabled",
            fg_color="transparent", hover_color=COL_ROSE, text_color=COL_ROSE_INK,
            text_color_disabled=COL_MUTED, font=ui_font(12, True), command=self.cancel_job)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8))
        self._reg(self.cancel_btn)

        self.preview_btn = ctk.CTkButton(
            btns, text="Analizar y ver cortes", width=160, height=40, corner_radius=12,
            fg_color="transparent", border_width=1, border_color=COL_LINE,
            hover_color=COL_HOVER, text_color=COL_INK, text_color_disabled=COL_MUTED,
            font=ui_font(12, True), command=lambda: self.start_job("preview"))
        self.preview_btn.grid(row=0, column=2, padx=(0, 8))
        self._reg(self.preview_btn)

        self.quick_btn = ctk.CTkButton(
            btns, text=f"Probar {QUICK_SECONDS} s", width=120, height=40, corner_radius=12,
            fg_color="transparent", border_width=1, border_color=COL_LINE,
            hover_color=COL_HOVER, text_color=COL_INK, text_color_disabled=COL_MUTED,
            font=ui_font(12, True), command=lambda: self.start_job("quick"))
        self.quick_btn.grid(row=0, column=3, padx=(0, 8))
        self._reg(self.quick_btn)

        self.render_btn = ctk.CTkButton(
            btns, text="Renderizar video final", width=190, height=40, corner_radius=12,
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER, text_color=COL_ACCENT_INK,
            text_color_disabled=COL_MUTED, font=ui_font(13, True),
            command=lambda: self.start_job("render"))
        self.render_btn.grid(row=0, column=4)
        self._reg(self.render_btn)

    def _on_params_changed(self, _value=None):
        self.thr_label.configure(text=f"{int(round(self.thr_slider.get()))} dBFS")
        self.margin_label.configure(text=f"{int(round(self.margin_slider.get()))} dB")
        self.cut_label.configure(text=f"{int(round(self.cut_slider.get()))} s")
        self.ov_label.configure(text=f"{self.ov_slider.get():.2f} s")
        if self.analysis:
            self._draw_preview()     # Recalcula los cortes en vivo (es instantáneo)

    # ---- Actualizaciones seguras desde el hilo (siempre vía self.after) ----
    def _log(self, text):
        """Guarda una línea con la hora para el informe de error (seguro entre hilos)."""
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def set_progress(self, frac, text=None, log=True):
        if text is not None and log:
            self._log(text)

        def _update():
            self.progress.set(max(0.0, min(1.0, frac)))
            if text is not None:
                self.status_label.configure(text=text)
        self.after(0, _update)

    def set_status(self, text):
        self._log(text)
        self.after(0, lambda: self.status_label.configure(text=text))

    def _set_analysis(self, analysis):
        self.analysis = analysis
        self._draw_preview()

    def _finish(self, kind, msg, popup=True):
        """kind: 'ok' | 'error' | 'cancel'. Restaura los botones y avisa al usuario."""
        self._log(f"[{kind}] {msg.splitlines()[0]}")

        def _update():
            self.busy = False
            self.preview_btn.configure(state="normal")
            self.quick_btn.configure(state="normal")
            self.render_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled", text=T("Cancelar"))
            self.status_label.configure(text=msg.splitlines()[0])
            if kind == "ok":
                self.progress.set(1.0)
                if popup:
                    messagebox.showinfo("AutoDirector Pro", msg)
            elif kind == "cancel":
                self.progress.set(0)
            else:
                if messagebox.askyesno(
                        "AutoDirector Pro",
                        msg + "\n\n" + T("¿Copiar el informe de error para enviarlo por Instagram?")):
                    self.copy_report()
        self.after(0, _update)

    # ---- Informe de error (para dar soporte) -------------------------------
    def build_report(self):
        """Texto con todo lo necesario para diagnosticar un problema (sin rutas personales)."""
        def ver(mod):
            try:
                module = __import__(mod)
            except Exception:
                return "no instalado"
            return getattr(module, "__version__", "instalado")

        ff = moviepy_ffmpeg()
        ff_ver = "no encontrado"
        if ff:
            try:
                out = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=10,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=0x08000000 if os.name == "nt" else 0).stdout
                ff_ver = out.splitlines()[0][:70] if out else "sin respuesta"
            except Exception:
                ff_ver = "no responde"

        lines = [
            f"INFORME DE AUTODIRECTOR PRO  ({datetime.datetime.now():%Y-%m-%d %H:%M})",
            f"Versión del programa: {APP_VERSION}",
            f"Sistema: {platform.platform()}",
            f"Python {platform.python_version()} | customtkinter {ver('customtkinter')} | "
            f"moviepy {ver('moviepy')} | numpy {ver('numpy')} | pycapcut {ver('pycapcut')}",
            f"FFmpeg: {ff_ver}",
            f"GPU: {self.gpu_label}",
            "",
            "AJUSTES",
            f"  umbral {int(round(self.thr_slider.get()))} dBFS | ventaja {int(round(self.margin_slider.get()))} dB | "
            f"plano mínimo {int(round(self.cut_slider.get()))} s | solape {self.ov_slider.get():.2f} s",
            f"  formato: {self.fmt_seg.get()} | filtro de voz: {bool(self.sw_voice.get())} | "
            f"equilibrar: {bool(self.sw_balance.get())} | sincronizar: {bool(self.sw_sync.get())} | "
            f"GPU: {bool(self.sw_gpu.get())}",
            f"  generar: MP4 {bool(self.sw_mp4.get())} | XML {bool(self.sw_xml.get())} | "
            f"CapCut {bool(self.sw_capcut.get())}",
            "",
            "CÁMARAS",
        ]
        for c in self.cards:
            d = c.get_data()
            lines.append(f"  {d['name']}: video={os.path.basename(d['video']) or '-'} | "
                         f"audio={os.path.basename(d['audio']) or '-'}")
        a = self.analysis
        if a:
            lines += ["", "ANÁLISIS",
                      f"  duración útil {a['total_s']:.1f} s | audio incrustado: {a['use_embedded']}",
                      "  saltos de sincronización (s): " + ", ".join(
                          f"{a['names'][i]} video {v:.2f}" + (f" audio {a['aoff'][i]:.2f}" if i in a['aoff'] else "")
                          for i, v in a["voff"].items()),
                      "  ajuste de volumen (dB): " + ", ".join(
                          f"{a['names'][i]} {g:+.1f}" for i, g in a["gains"].items())]
            for note in a.get("sync_notes", []):
                lines.append(f"  aviso: {note}")
            lines.append(f"  ediciones manuales: {len(self.overrides)} s")
        lines += ["", "ÚLTIMOS EVENTOS"] + [f"  {x}" for x in list(self.log)[-40:]]
        if self.last_error:
            lines += ["", "ÚLTIMO ERROR", self.last_error.strip()]
        return "\n".join(lines)

    def copy_report(self):
        try:
            text = self.build_report()
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()                                  # El portapapeles queda disponible aunque se cierre la app
        except Exception as exc:
            messagebox.showerror("AutoDirector Pro", T("No pude copiar el informe: {e}", e=exc))
            return
        self.report_btn.configure(text=T("¡Informe copiado! Pégalo en el mensaje"))
        self.after(4000, lambda: self.report_btn.configure(text=T("Copiar informe de error")))

    # ---- Inicio / cancelación de trabajos ---------------------------------
    def start_job(self, mode):
        """mode: 'preview' (solo análisis), 'quick' (prueba de 30 s) o 'render' (exporta XML, CapCut y/o MP4)."""
        if self.busy:
            return
        cams = [c.get_data() for c in self.cards]
        if any(not cam["video"] for cam in cams):
            messagebox.showwarning("AutoDirector Pro", T("Todas las cámaras necesitan un video."))
            return

        # Se leen TODOS los valores de la UI aquí (hilo principal); el hilo
        # de trabajo solo usa este diccionario.
        p = {
            "cameras": cams,
            "threshold": float(self.thr_slider.get()),
            "min_cut": int(round(self.cut_slider.get())),
            "overlap": float(self.ov_slider.get()),
            "voice_filter": bool(self.sw_voice.get()),
            "margin": float(self.margin_slider.get()),
            "balance": bool(self.sw_balance.get()),
            "sync": bool(self.sw_sync.get()),
            "vertical": self._is_vertical(),
            "overrides": dict(self.overrides),
            "do_mp4": bool(self.sw_mp4.get()),
            "do_xml": bool(self.sw_xml.get()),
            "do_capcut": bool(self.sw_capcut.get()),
            "use_gpu": bool(self.sw_gpu.get()),
            "scale": 1.0,
        }

        if mode == "quick":
            # Prueba rápida: solo MP4 en baja resolución, en una carpeta temporal
            for old in Path(tempfile.gettempdir()).glob("AutoDirectorPro_prueba_*.mp4"):
                try:
                    old.unlink()
                except OSError:
                    pass
            p.update(do_mp4=True, do_xml=False, do_capcut=False, scale=0.5,
                     mp4_path=os.path.join(tempfile.gettempdir(),
                                           f"AutoDirectorPro_prueba_{int(time.time())}.mp4"))

        if mode == "render":
            if not (p["do_mp4"] or p["do_xml"] or p["do_capcut"]):
                messagebox.showwarning("AutoDirector Pro",
                                       T("Activa al menos «Video MP4», «XML para Premiere o DaVinci» o «Borrador de CapCut»."))
                return

            # Carpeta de borradores de CapCut (se detecta o se pide una vez)
            if p["do_capcut"]:
                drafts = default_capcut_drafts_dir()
                if not drafts:
                    messagebox.showinfo(
                        "AutoDirector Pro",
                        T("No encontré la carpeta de borradores de CapCut.\n"
                          "Selecciónala a continuación (normalmente termina en com.lveditor.draft)."))
                    drafts = filedialog.askdirectory(title=T("Carpeta de borradores de CapCut"))
                    if not drafts:
                        return
                p["capcut_dir"] = drafts

            if p["do_mp4"]:
                ext, label = ".mp4", "MP4"
            elif p["do_xml"]:
                ext, label = ".xml", "XML"
            else:
                ext, label = ".wav", T("Nombre base (se usa para el borrador y su audio)")

            path = filedialog.asksaveasfilename(
                title=T("Guardar resultado"), defaultextension=ext,
                filetypes=[(label, f"*{ext}")], initialfile="podcast_final" + ext)
            if not path:
                return
            base = os.path.splitext(path)[0]
            p["mp4_path"] = base + ".mp4"
            p["xml_path"] = base + ".xml"
            p["wav_path"] = base + "_audio.wav"        # Audio final persistente (borrador CapCut)
            p["seq_name"] = os.path.basename(base)

        self.busy = True
        self.cancel_event.clear()
        self._sel = None
        self.preview_btn.configure(state="disabled")
        self.quick_btn.configure(state="disabled")
        self.render_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.set(0)

        # Análisis y render van en un hilo aparte para no congelar la UI
        threading.Thread(target=self._job_worker, args=(p, mode), daemon=True).start()

    def cancel_job(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_btn.configure(state="disabled", text=T("Cancelando..."))
            self.status_label.configure(text=T("Cancelando, espera un momento..."))

    def _check(self):
        """Punto de control: aborta el trabajo si se pulsó Cancelar."""
        if self.cancel_event.is_set():
            raise Cancelled()

    # =========================================================================
    # MOTOR DE EDICIÓN (se ejecuta en un hilo separado)
    # =========================================================================
    def _ensure_analysis(self, p):
        """Analiza el audio (o reutiliza el análisis guardado si nada cambió)."""
        cams = p["cameras"]
        key = (tuple((c["video"], c["audio"]) for c in cams), p["voice_filter"], p["sync"])
        if self.analysis and self.analysis["key"] == key:
            self.set_progress(0.35, T("Análisis reutilizado."))
            return self.analysis

        # Cámaras con audio asignado = micrófonos. Plano General: primera
        # cámara SIN audio asignado; si todas tienen, la Cámara 1.
        use_embedded = not any(c["audio"] for c in cams)
        general_idx = 0 if use_embedded else next((i for i, c in enumerate(cams) if not c["audio"]), 0)

        # Metadatos de cada video (duración, fps, resolución)
        infos = []
        for n, c in enumerate(cams, start=1):
            self._check()
            self.set_progress(0.02 + 0.03 * n / len(cams), T("Leyendo video {n}/{total}...", n=n, total=len(cams)))
            clip = VideoFileClip(c["video"])
            try:
                infos.append({"duration": float(clip.duration), "fps": float(clip.fps or 30),
                              "size": tuple(int(v) for v in clip.size)})
            finally:
                clip.close()

        # Sincronización por audio: cuántos segundos se saltan al inicio de cada archivo
        voff = {i: 0.0 for i in range(len(cams))}
        aoff = {i: 0.0 for i, c in enumerate(cams) if c["audio"]}
        sync_notes = []
        if p["sync"] and (len(cams) > 1 or any(c["audio"] for c in cams)):
            voff, aoff, sync_notes = compute_sync(
                cams, self._check, lambda f, t: self.set_progress(0.05 + 0.07 * f, t))
            moved = [f"{cams[i]['name']} {v:.2f} s" for i, v in voff.items() if v > 0.005]
            moved += [T("mic de {name} {v}", name=cams[i]["name"], v=f"{v:.2f} s")
                      for i, v in aoff.items() if v > 0.005]
            self.set_status(T("Sincronizado: {list} recortados al inicio.", list=", ".join(moved))
                            if moved else T("Sincronizado: las cámaras ya estaban alineadas."))
            for note in sync_notes:
                self._log("aviso: " + note)

        # Nivel dBFS por segundo de cada pista (ya alineada con la línea de tiempo común)
        src_off = voff if use_embedded else aoff
        sources = ([(i, c["video"]) for i, c in enumerate(cams)] if use_embedded
                   else [(i, c["audio"]) for i, c in enumerate(cams) if c["audio"]])
        levels, audio_len, usable = {}, {}, []
        for n, (idx, path) in enumerate(sources):
            self._check()
            self.set_progress(0.12 + 0.23 * n / len(sources),
                              T("Analizando audio de {name}...", name=cams[idx]["name"]))
            seg = read_audio(path, ANALYSIS_RATE, 1, src_off[idx]).set_sample_width(2)
            usable.append(len(seg) / 1000.0)                  # segundos útiles ya alineados
            audio_len[idx] = len(seg) / 1000.0 + src_off[idx]  # duración total del archivo
            levels[idx] = seconds_levels(seg, p["voice_filter"], self._check)
            del seg

        total_s = min([i["duration"] - voff[n] for n, i in enumerate(infos)] + usable)
        if total_s < 1:
            raise RuntimeError(T("Las cámaras no tienen ningún tramo en común. "
                                 "Revisa que sean de la misma grabación o desactiva la sincronización."))
        return {
            "key": key, "use_embedded": use_embedded, "general_idx": general_idx,
            "infos": infos, "levels": levels, "audio_len": audio_len,
            "total_s": total_s, "n_chunks": max(1, math.ceil(total_s)),
            "names": [c["name"] for c in cams],
            "voff": voff, "aoff": aoff, "sync_notes": sync_notes,
            "gains": balance_gains(levels),
        }

    def _build_audio(self, p, a, segs, t0=0.0, length=None):
        """
        Construye el audio final (pydub): mezcla de micrófonos o J-cut con crossfade.
        `t0` y `length` limitan el trabajo a un tramo (prueba rápida); `segs`
        ya viene medido desde el inicio de ese tramo.
        """
        cams = p["cameras"]
        length = a["total_s"] if length is None else length
        total_ms = int(length * 1000)

        if not a["use_embedded"]:
            # Audios externos: se mezclan en una sola pista continua
            ext = [i for i, c in enumerate(cams) if c["audio"]]
            mix = None
            for n, idx in enumerate(ext):
                self._check()
                self.set_progress(0.38 + 0.12 * n / len(ext), T("Mezclando audio de {name}...", name=cams[idx]["name"]))
                seg = load_full_audio(cams[idx]["audio"], a["aoff"].get(idx, 0.0) + t0, length + 0.5)
                if mix is None:
                    mix = seg
                else:
                    if len(seg) > len(mix):
                        mix, seg = seg, mix
                    mix = mix.overlay(seg)
            return mix[:total_ms]

        # Audio incrustado: cada plano usa el audio de su cámara y el audio de
        # la cámara entrante empieza `overlap` ms ANTES del corte de video.
        ov_ms = min(int(p["overlap"] * 1000), p["min_cut"] * 1000)
        n = len(segs)
        starts, ends, xfs = [], [], []
        for i, (_, a_s, b_s) in enumerate(segs):
            b_ms = int(round(a_s * 1000))
            start = 0 if i == 0 else max(0, b_ms - ov_ms)
            starts.append(start)
            xfs.append(b_ms - start if i > 0 else 0)
            ends.append(total_ms if i == n - 1 else int(round(b_s * 1000)))

        pieces = [None] * n
        used = sorted({c for c, _, _ in segs})
        for k, idx in enumerate(used):
            self._check()
            self.set_progress(0.38 + 0.12 * k / len(used), T("Preparando audio de {name}...", name=cams[idx]["name"]))
            seg = load_full_audio(cams[idx]["video"], a["voff"].get(idx, 0.0) + t0, length + 0.5)
            for i, (ci, _, _) in enumerate(segs):
                if ci == idx:
                    pieces[i] = seg[starts[i]:ends[i]]
            del seg
        return assemble_with_crossfades(pieces, xfs)

    def _job_worker(self, p, mode):
        clips = {}
        final = None
        audio_clip = None
        tmp_wav = None
        mp4_started = False
        cancelled = False
        try:
            # ---- Análisis + plan de cortes ---------------------------------
            a = self._ensure_analysis(p)
            self.after(0, lambda: self._set_analysis(a))
            segs = make_plan(a, p["threshold"], p["min_cut"], p["margin"],
                             p["balance"], p["overrides"])

            if mode == "preview":
                note = " ".join(a["sync_notes"])
                self._finish("ok", T("Vista previa lista: {n} cortes. "
                                     "Haz clic en «Resultado» para cambiar un tramo.", n=len(segs)) +
                             (" " + T("Aviso: {note}", note=note) if note else ""), popup=False)
                return

            # Prueba rápida: solo un tramo corto (el que tiene más cambios de cámara)
            quick = (mode == "quick")
            if quick:
                t0, t1 = pick_window(segs, a["total_s"])
                segs = clip_segments(segs, t0, t1)
                length = t1 - t0
                self.set_status(T("Prueba de {a} a {b}...", a=fmt_time(t0), b=fmt_time(t1)))
            else:
                t0, length = 0.0, a["total_s"]

            outputs = []

            # ---- Exportación XML (rápida, no destructiva) -------------------
            if p["do_xml"]:
                self._check()
                self.set_progress(0.36, T("Generando XML para Premiere / DaVinci..."))
                build_fcp7_xml(p["xml_path"], p["seq_name"], p["cameras"], a["infos"], segs,
                               a["total_s"], p["overlap"], a["use_embedded"], a["audio_len"],
                               a["voff"], a["aoff"])
                outputs.append(p["xml_path"] + (" " + T("(en horizontal; en Premiere usa Auto Reframe)")
                                                if p["vertical"] else ""))

            # ---- Audio final (si hace falta para el MP4 o para CapCut) -------
            # Con audios externos el borrador de CapCut usa los archivos
            # originales; solo con audio incrustado hay que construir el audio.
            capcut_needs_audio = p["do_capcut"] and a["use_embedded"]
            audio_path = None
            if p["do_mp4"] or capcut_needs_audio:
                audio = self._build_audio(p, a, segs, t0, length)
                self._check()
                if capcut_needs_audio:
                    audio.export(p["wav_path"], format="wav")     # persistente
                    audio_path = p["wav_path"]
                    outputs.append(p["wav_path"])
                else:
                    tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
                    os.close(tmp_fd)
                    audio.export(tmp_wav, format="wav")
                    audio_path = tmp_wav
                del audio

            # ---- Borrador de CapCut ---------------------------------------------
            if p["do_capcut"]:
                self._check()
                self.set_progress(0.50, T("Creando borrador de CapCut..."))
                draft_path = export_capcut_draft(
                    p["capcut_dir"], p["seq_name"], p["cameras"], a["infos"], segs,
                    a["total_s"], audio_path if a["use_embedded"] else None,
                    a["voff"], a["aoff"], p["vertical"])
                outputs.append(draft_path + (" " + T("(vertical: revisa el encuadre en CapCut)")
                                             if p["vertical"] else ""))

            # ---- Renderizado MP4 ---------------------------------------------
            if p["do_mp4"]:
                self._check()
                self.set_progress(0.52, T("Montando cortes de video..."))

                for ci in sorted({c for c, _, _ in segs}):
                    clips[ci] = VideoFileClip(p["cameras"][ci]["video"])

                # Tamaño de salida: vertical 9:16, o el de la primera cámara si es prueba rápida
                scale, out_size = p["scale"], None
                if p["vertical"]:
                    out_size = (even(VERT_SIZE[0] * scale), even(VERT_SIZE[1] * scale))
                elif scale != 1.0:
                    bw, bh = a["infos"][0]["size"]
                    out_size = (even(bw * scale), even(bh * scale))

                pieces = []
                for ci, s, e in segs:
                    off = a["voff"].get(ci, 0.0) + t0               # tiempo en el archivo original
                    piece = sub_clip(clips[ci], off + s, min(off + e, a["infos"][ci]["duration"]))
                    if out_size:
                        piece = reframe_clip(piece, out_size,
                                             p["cameras"][ci].get("crop_x", 0.5) if p["vertical"] else 0.5)
                    pieces.append(piece)
                sizes = {tuple(cl.size) for cl in clips.values()}
                # Si las cámaras tienen resoluciones distintas, "compose" las unifica
                same = out_size is not None or len(sizes) == 1
                final = concatenate_videoclips(pieces, method="chain" if same else "compose")
                audio_clip = AudioFileClip(audio_path)
                final = clip_with_audio(final, audio_clip)

                mp4_started = True
                self._write(final, p["mp4_path"], 0.55, p["use_gpu"])
                outputs.append(p["mp4_path"])

            if quick:
                self._finish("ok", T("Prueba lista ({a} a {b}). Se muestra en una ventana de la app.",
                                     a=fmt_time(t0), b=fmt_time(t0 + length)), popup=False)
                aspect = (VERT_SIZE[0] / VERT_SIZE[1]) if p["vertical"] else (
                    a["infos"][0]["size"][0] / a["infos"][0]["size"][1])
                self.after(0, lambda: self._open_player(p["mp4_path"], aspect))
            else:
                self._finish("ok", T("¡Listo! Archivos generados:") + "\n" + "\n".join(outputs))

        except Cancelled:
            cancelled = True
            self._finish("cancel", T("Proceso cancelado."))
        except Exception as exc:
            self.last_error = traceback.format_exc()
            self._log("ERROR: " + self.last_error.strip().splitlines()[-1])
            self._finish("error", T("Error durante el proceso: {e}", e=exc))
        finally:
            for obj in [final, audio_clip] + list(clips.values()):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except Exception:
                    pass
            if cancelled and mp4_started:      # Borra el MP4 incompleto
                try:
                    os.remove(p["mp4_path"])
                except Exception:
                    pass

    def _write(self, clip, out_path, base_progress, use_gpu):
        """
        Exporta el MP4. Parámetros obligatorios: preset="ultrafast", threads=4,
        fps=30. Si hay GPU compatible se usa su codec; si falla, se reintenta
        automáticamente con la CPU (libx264).
        """
        encoders = []
        if use_gpu:
            self.set_status(T("Buscando aceleración por GPU..."))
            gpu = detect_gpu_encoder()
            self.gpu_label = gpu["label"] if gpu else "no se detectó (se usa la CPU)"
            if gpu:
                encoders.append(gpu)
        encoders.append(None)            # None = CPU (libx264)

        def on_progress(frac):
            self.set_progress(base_progress + (1 - base_progress) * frac,
                              T("Renderizando video... {p}%", p=int(frac * 100)), log=False)

        temp_audio = os.path.join(
            tempfile.gettempdir(),
            f"AutoDirectorPro_audio_{os.getpid()}_{int(time.time() * 1000)}.m4a")

        for enc in encoders:
            self._check()
            kwargs = dict(
                codec=enc["codec"] if enc else "libx264",
                audio_codec="aac",
                preset="ultrafast",      # <- obligatorio
                threads=4,               # <- obligatorio
                fps=30,                  # <- obligatorio
                # MoviePy crea un audio temporal junto al programa (carpeta actual),
                # y ahí Windows puede negar el permiso. Se manda a la carpeta temporal.
                temp_audiofile=temp_audio,
            )
            if enc:
                # Se añaden después de -preset ultrafast y lo sobrescriben
                kwargs["ffmpeg_params"] = enc["params"]
            if RenderLogger is not None:
                kwargs["logger"] = RenderLogger(on_progress, self.cancel_event)

            self.set_progress(base_progress,
                              T("Renderizando con {enc}...", enc=enc["label"]) if enc
                              else T("Renderizando con CPU (libx264)..."))
            try:
                clip.write_videofile(out_path, **kwargs)
                return
            except Cancelled:
                raise
            except Exception:
                if enc is None:
                    raise                 # Ya era el último intento (CPU)
                self.set_status(T("La GPU falló; reintentando con la CPU..."))
            finally:
                try:
                    if os.path.exists(temp_audio):
                        os.remove(temp_audio)      # Por si el render se interrumpió
                except OSError:
                    pass


# =============================================================================
# 10. PUNTO DE ENTRADA
# =============================================================================
def run_selftest(report_path):
    """
    Autoprueba para el .exe: comprueba que TODO lo necesario quedó empaquetado
    (FFmpeg, pycapcut, MediaInfo, lectura de audio y creación de un borrador).
    Escribe el resultado en `report_path`. Devuelve 0 si todo está bien.
    Uso:  AutoDirectorPro.exe --selftest
    """
    lines, ok = [], True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"OK    {name}" + (f" -> {detail}" if detail else ""))
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"FALLO {name}: {exc}")

    work = tempfile.mkdtemp(prefix="autodirector_selftest_")
    ff = moviepy_ffmpeg()
    flags = 0x08000000 if os.name == "nt" else 0
    video = os.path.join(work, "t.mp4")

    def t_ffmpeg():
        out = subprocess.run([ff, "-version"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, creationflags=flags).stdout
        return out.splitlines()[0][:60]

    def t_make_video():
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc=s=320x240:d=2:r=30", "-f", "lavfi", "-i", "sine=d=2",
                            "-shortest", "-pix_fmt", "yuv420p", video],
                           capture_output=True, stdin=subprocess.DEVNULL, creationflags=flags)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="ignore")[-200:])

    def t_audio():
        seg = read_audio(video, ANALYSIS_RATE, 1)
        return f"{len(seg)} ms"

    def t_levels():
        seg = read_audio(video, ANALYSIS_RATE, 1).set_sample_width(2)
        return f"{len(seconds_levels(seg, True))} fragmentos"

    def t_moviepy():
        clip = VideoFileClip(video)
        try:
            return f"{clip.duration:.1f} s"
        finally:
            clip.close()

    def t_mediainfo():
        import pymediainfo
        if not pymediainfo.MediaInfo.can_parse():
            raise RuntimeError("MediaInfo no está disponible")

    def t_capcut():
        cams = [{"name": "A", "video": video, "audio": ""}]
        infos = [{"size": (320, 240), "duration": 2.0}]
        path = export_capcut_draft(work, "selftest", cams, infos, [(0, 0.0, 2.0)], 2.0, None)
        if not os.path.exists(os.path.join(path, "draft_content.json")):
            raise RuntimeError("no se creó draft_content.json")

    def t_xml():
        cams = [{"name": "A", "video": video, "audio": ""}]
        infos = [{"duration": 2.0, "fps": 30.0, "size": (320, 240)}]
        build_fcp7_xml(os.path.join(work, "t.xml"), "t", cams, infos, [(0, 0.0, 2.0)],
                       2.0, 0.5, True, {})

    def t_assets():
        missing = [n for n in ("icon.ico", "logo.png") if not os.path.exists(resource_path(n))]
        if missing:
            raise RuntimeError("faltan en el programa: " + ", ".join(missing))

    def t_update_logic():
        assert is_newer("2.1.0", "2.0.0") and is_newer("2.10.0", "2.9.0")
        assert not is_newer("2.0.0", "2.0.0") and not is_newer("1.9.9", "2.0.0")
        probe = os.path.join(work, "probe.bin")
        with open(probe, "wb") as fh:
            fh.write(b"autodirector")
        if sha256_of(probe) != hashlib.sha256(b"autodirector").hexdigest():
            raise RuntimeError("sha256 incorrecto")

    def t_sync():
        rng = np.random.default_rng(7)
        rate = 8000

        def speech(seconds):
            x = np.zeros(int(seconds * rate), dtype=np.float32)
            t = 0.0
            while t < seconds:
                on, off = rng.uniform(0.3, 2.0), rng.uniform(0.2, 1.5)
                x[int(t * rate):int(min(seconds, t + on) * rate)] = rng.normal(0, 0.2, int(min(seconds, t + on) * rate) - int(t * rate))
                t += on + off
            return x

        def env(x):
            seg = AudioSegment((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes(),
                               sample_width=2, frame_rate=rate, channels=1)
            return audio_envelope(seg)

        base = speech(120)
        e0 = env(base)
        e1 = env(base[int(3.5 * rate):])                  # la 2.ª cámara empezó 3,5 s más tarde
        d, score = estimate_delay(e0, e1)
        if abs(d - 3.5) > 0.03 or score < SYNC_MIN_SCORE:
            raise RuntimeError(f"desfase {d:.2f} s (esperado 3.50), confianza {score:.1f}")
        return f"desfase {d:.2f} s, confianza {score:.0f}"

    def t_plan():
        lv = {0: [-20] * 5 + [-60] * 5, 1: [-30] * 5 + [-60] * 5}      # el mic 2 "oye" al 1
        plan = compute_plan(lv, 10, -40, 2, 1, 10, 6)
        if plan[0][0] != 0:
            raise RuntimeError(f"la regla de dominio falló: {plan}")
        edited = apply_overrides(plan, {3: 1, 4: 1}, 10)
        if [c for c, _, _ in edited] != [0, 1, 0] or edited[1][1:] != (3.0, 5.0):
            raise RuntimeError(f"edición manual incorrecta: {edited}")
        if pick_window([(0, 0.0, 100.0)], 100.0) != (0.0, 30.0):
            raise RuntimeError("ventana de prueba incorrecta")

    def t_reframe():
        clip = VideoFileClip(video)
        try:
            out = reframe_clip(clip, (180, 320), 0.5)
            if tuple(int(v) for v in out.size) != (180, 320):
                raise RuntimeError(f"tamaño {out.size}")
            out.get_frame(0.5)
            return "320x240 -> 180x320"
        finally:
            clip.close()

    step("Icono y logo incluidos", t_assets)
    step("Sincronización por audio", t_sync)
    step("Reglas de cortes y ediciones", t_plan)
    step("Lógica de actualizaciones", t_update_logic)
    step("FFmpeg incluido", t_ffmpeg)
    step("Crear video de prueba", t_make_video)
    step("Leer audio (sin ffprobe)", t_audio)
    step("Análisis de niveles (numpy)", t_levels)
    step("MoviePy abre el video", t_moviepy)
    step("Recorte vertical (MoviePy)", t_reframe)
    step("MediaInfo (pymediainfo)", t_mediainfo)
    step("Borrador de CapCut (pycapcut)", t_capcut)
    step("Exportar XML (Premiere/DaVinci)", t_xml)

    lines.append("")
    lines.append("RESULTADO: TODO CORRECTO" if ok else "RESULTADO: HAY FALLOS (ver arriba)")
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        idx = sys.argv.index("--selftest")
        report = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "selftest.txt"
        sys.exit(run_selftest(report))
    app = AutoDirectorApp()
    app.mainloop()
