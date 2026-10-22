#!/usr/bin/env python3
"""
Clima Pi 2 - predicción meteorológica local que combina 11 modelos
==================================================================
Pensado para Raspberry Pi 5 (funciona en cualquier PC con Python 3.9+).

Qué hace:
  * Descarga gratis (Open-Meteo, sin clave) los pronósticos de 11 modelos
    meteorológicos de todo el mundo para tu ciudad: ECMWF IFS, ECMWF AIFS (IA),
    GFS, ICON, Météo-France, UK Met Office, GEM, JMA, KMA, CMA y BOM.
  * Entrena el "Modelo Pi": aprende cuánto se equivoca cada modelo en tu zona
    (comparando ~2 años de sus pronósticos con lo que pasó, según ERA5) y los
    combina dándole más peso a los que mejor aciertan. Técnica tipo MOS.
  * Te deja apuntar lo que dicen las noticias para mañana y lleva el marcador
    "Pi contra la tele".
  * Pestañas: Ahora · Pronóstico · Por horas · Sol y Luna · Precisión · Clima histórico.
  * Se queda funcionando: actualiza cada hora y reentrena cada día.

Ejecutar:
    python3 clima_pi.py
Si falta algo:
    sudo apt install python3-tk python3-numpy

La primera vez descarga mucho historial (unos minutos, respetando los límites de
Open-Meteo). Después solo descarga lo nuevo. Datos en ~/.clima_pi/
Datos meteorológicos: Open-Meteo.com (CC BY 4.0).
"""

import bisect
import importlib.util
import json
import math
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta, timezone

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

try:
    import numpy as np
except ImportError:              # se avisa en la ventana
    np = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# ================================================================ ajustes
# (clave, nombre, identificadores de Open-Meteo a probar en orden, color)
MODELS = [
    ("ecmwf", "ECMWF IFS (Europa)", ["ecmwf_ifs025", "ecmwf_ifs"], "#60a5fa"),
    ("aifs", "ECMWF AIFS (IA)", ["ecmwf_aifs025_single", "ecmwf_aifs025"], "#22d3ee"),
    ("gfs", "GFS (EE. UU.)", ["ncep_gfs_seamless", "gfs_seamless"], "#f59e0b"),
    ("icon", "ICON (Alemania)", ["dwd_icon_seamless", "icon_seamless"], "#c084fc"),
    ("mf", "Météo-France", ["meteofrance_seamless"], "#f472b6"),
    ("ukmo", "UK Met Office", ["ukmo_seamless"], "#fb7185"),
    ("gem", "GEM (Canadá)", ["cmc_gem_seamless", "gem_seamless"], "#a3e635"),
    ("jma", "JMA (Japón)", ["jma_seamless"], "#fbbf24"),
    ("kma", "KMA (Corea)", ["kma_seamless"], "#34d399"),
    ("cma", "CMA (China)", ["cma_grapes_global"], "#f87171"),
    ("bom", "BOM (Australia)", ["bom_access_global"], "#818cf8"),
]
APP = ("app", "App típica (Open-Meteo)", ["best_match"], "#e2e8f0")
NAMES = {k: n for k, n, _, _ in MODELS + [APP]}
COLORS = {k: c for k, _, _, c in MODELS + [APP]}

LEADS = range(1, 8)                  # días de antelación
FORECAST_EVERY_S = 3600              # actualizar pronóstico cada hora
RETRAIN_EVERY_S = 24 * 3600          # reentrenar cada día
PREV_RUNS_START = date(2024, 1, 15)  # archivo de pronósticos pasados
HISTORY_START = date(1950, 1, 1)
RATE_PER_MIN = 420                   # Open-Meteo permite 600 llamadas/minuto

URL_GEO = "https://geocoding-api.open-meteo.com/v1/search"
URL_FORECAST = "https://api.open-meteo.com/v1/forecast"
URL_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
URL_PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"

EXTRA_HOURLY = ("temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,"
                "precipitation_probability,precipitation,weather_code,cloud_cover,pressure_msl,"
                "wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index,visibility,is_day")
EXTRA_DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
               "daylight_duration,uv_index_max,precipitation_sum,precipitation_probability_max,"
               "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant")
EXTRA_CURRENT = ("temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                 "cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
                 "precipitation,is_day")
SAFE_HOURLY = ("temperature_2m,apparent_temperature,relative_humidity_2m,"
               "precipitation_probability,precipitation,weather_code,cloud_cover,"
               "wind_speed_10m,wind_direction_10m,wind_gusts_10m")
SAFE_DAILY = "weather_code,sunrise,sunset,precipitation_sum"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".clima_pi")

BG = "#111418"
PANEL = "#1b2027"
FG = "#e6e6e6"
MUTED = "#8a94a3"
GRID = "#2a313b"
PI_C = "#4ade80"
BAND_C = "#24402f"
CONS_C = "#cbd5e1"
CLIM_C = "#8a94a3"
WARN = "#f59e0b"
BAD = "#ef4444"
COLD = "#38bdf8"
MOON_C = "#f1f5f9"
FONT = "DejaVu Sans"

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_L = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]
RUMBOS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
          "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
WMO = {0: "Despejado", 1: "Casi despejado", 2: "Parcialmente nublado", 3: "Nublado",
       45: "Niebla", 48: "Niebla con escarcha", 51: "Llovizna débil", 53: "Llovizna",
       55: "Llovizna intensa", 56: "Llovizna helada", 57: "Llovizna helada intensa",
       61: "Lluvia débil", 63: "Lluvia", 65: "Lluvia fuerte", 66: "Lluvia helada",
       67: "Lluvia helada fuerte", 71: "Nieve débil", 73: "Nieve", 75: "Nieve fuerte",
       77: "Granos de nieve", 80: "Chubascos débiles", 81: "Chubascos", 82: "Chubascos fuertes",
       85: "Chubascos de nieve", 86: "Chubascos de nieve fuertes", 95: "Tormenta",
       96: "Tormenta con granizo", 99: "Tormenta con granizo fuerte"}


def wmo(code):
    return WMO.get(int(code), "—") if code is not None else "—"


def rumbo(deg):
    return RUMBOS[int((deg % 360) / 22.5 + 0.5) % 16] if deg is not None else "—"


# ================================================================ registro en vivo
LOG_Q = queue.Queue()            # lo que el programa va haciendo, para la pestaña «En vivo»
STATS = {"bytes": 0, "calls": 0, "weights": deque(), "lock": threading.Lock()}
SHORT = {"ecmwf": "ECMWF", "aifs": "AIFS (IA)", "gfs": "GFS", "icon": "ICON", "mf": "M-France",
         "ukmo": "UKMO", "gem": "GEM", "jma": "JMA", "kma": "KMA", "cma": "CMA", "bom": "BOM",
         "app": "App"}


def log(msg, kind="calc"):
    LOG_Q.put((time.time(), kind, msg))


def _record_call(weight, nbytes):
    with STATS["lock"]:
        STATS["bytes"] += nbytes
        STATS["calls"] += 1
        now = time.time()
        STATS["weights"].append((now, weight))
        while STATS["weights"] and now - STATS["weights"][0][0] > 86400:
            STATS["weights"].popleft()


def api_usage():
    """(peso usado en el último minuto, en la última hora, en las últimas 24 h)."""
    now = time.time()
    with STATS["lock"]:
        w = list(STATS["weights"])
    return (sum(x for t, x in w if now - t <= 60), sum(x for t, x in w if now - t <= 3600),
            sum(x for _, x in w))


# ================================================================ red
class Throttle:
    """Mantiene el uso por debajo del límite por minuto de Open-Meteo."""

    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.events = deque()

    def wait(self, weight):
        with self.lock:
            while True:
                now = time.time()
                while self.events and now - self.events[0][0] > 60:
                    self.events.popleft()
                used = sum(w for _, w in self.events)
                if not self.events or used + weight <= self.limit:
                    break
                pause = min(10.0, 61 - (now - self.events[0][0]))
                log(f"PAUSA {pause:.0f} s: {used:.0f} de {self.limit} llamadas usadas en el último "
                    f"minuto (límite gratuito de Open-Meteo)", "warn")
                time.sleep(pause)
            self.events.append((time.time(), weight))


THROTTLE = Throttle(RATE_PER_MIN)


def est_weight(params):
    """Peso aproximado de una petición según Open-Meteo (10 variables × 2 semanas = 1)."""
    nv = sum(len(str(params[k]).split(",")) for k in ("hourly", "daily", "current") if params.get(k))
    if params.get("start_date"):
        days = (date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
    else:
        days = int(params.get("past_days", 0)) + int(params.get("forecast_days", 7))
    return max(1.0, nv / 10) * max(1.0, days / 14)


def get_json(base, params, timeout=90):
    """GET a Open-Meteo y devuelve el JSON. Reintenta si falla la red o hay límite."""
    weight = 0.0
    host = base.split("//")[-1].split("/")[0]
    if "geocoding" not in base:
        weight = est_weight(params)
        THROTTLE.wait(weight)
    nv = sum(len(str(params[k]).split(",")) for k in ("hourly", "daily", "current") if params.get(k))
    span = (f"{params['start_date']} → {params['end_date']}" if params.get("start_date") else
            f"{params.get('past_days', 0)} días atrás + {params.get('forecast_days', 7)} adelante"
            if "forecast_days" in params else "")
    log(f"→ {host}  {params.get('models', '')}  {nv} variable{'s' if nv != 1 else ''}  {span}  "
        f"(peso ≈ {weight:.1f})", "net")
    url = base + "?" + urllib.parse.urlencode(params, safe=",")
    req = urllib.request.Request(url, headers={"User-Agent": "clima-pi/2.0"})
    for attempt in (1, 2, 3):
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            _record_call(weight, len(raw))
            log(f"← {len(raw) / 1024:,.0f} KB en {1000 * (time.perf_counter() - t0):,.0f} ms", "net")
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read().decode("utf-8")).get("reason", "")
            except Exception:
                reason = ""
            if e.code == 429 and attempt < 3:          # límite: esperar y reintentar
                log("← 429: demasiadas peticiones; espero 65 s y reintento", "warn")
                time.sleep(65)
                continue
            log(f"← error {e.code}: {reason or e.reason}", "err")
            raise RuntimeError(f"Open-Meteo respondió {e.code}: {reason or e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 3:
                log(f"← sin conexión: {e}", "err")
                raise RuntimeError(f"sin conexión con Open-Meteo ({e})")
            log(f"← fallo de red ({e}); reintento {attempt + 1}/3 en 5 s", "warn")
            time.sleep(5)


def online(timeout=8):
    """Comprobación rápida de red (al encender el Pi la wifi puede tardar en conectar)."""
    try:
        socket.create_connection(("api.open-meteo.com", 443), timeout=timeout).close()
        return True
    except OSError:
        log("Sin conexión con api.open-meteo.com (¿wifi aún conectando?)", "warn")
        return False


def series(resp, section, var, model=None):
    """Extrae {timestamp: valor} de una respuesta de Open-Meteo (timeformat=unixtime)."""
    blk = (resp or {}).get(section) or {}
    times = blk.get("time") or []
    vals = blk.get(var)
    if vals is None and model:
        vals = blk.get(f"{var}_{model}")
    if vals is None:
        return {}
    return {int(t): float(v) for t, v in zip(times, vals) if v is not None}


def geocode(name):
    r = get_json(URL_GEO, {"name": name, "count": 6, "language": "es", "format": "json"})
    return r.get("results") or []


def _ll(loc):
    return {"latitude": f"{loc['latitude']:.4f}", "longitude": f"{loc['longitude']:.4f}"}


# ---------------------------------------------------------------- historial en disco
def loc_dir(loc):
    d = os.path.join(DATA_DIR, f"{loc['latitude']:.3f}_{loc['longitude']:.3f}")
    os.makedirs(d, exist_ok=True)
    return d


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def merge_section(old, new):
    if not old:
        return new
    keys = (set(old) | set(new)) - {"time"}
    m = {k: {} for k in keys}
    for src in (old, new):
        times = src.get("time") or []
        for k in keys:
            vals = src.get(k)
            if vals is None:
                continue
            d = m[k]
            for t, v in zip(times, vals):
                if v is not None or t not in d:
                    d[t] = v
    times = sorted(set().union(*[d.keys() for d in m.values()]))
    out = {"time": times}
    for k in keys:
        out[k] = [m[k].get(t) for t in times]
    return out


def update_history(path, section, full_start, end, fetch, chunk_days, max_age):
    """Historial incremental: la 1ª vez lo descarga por trozos; luego solo lo nuevo."""
    data = _load_json(path) or {}
    sec = data.get(section)
    name = os.path.basename(path)
    if sec and sec.get("time"):
        last = datetime.fromtimestamp(sec["time"][-1], timezone.utc).date()
        if time.time() - os.path.getmtime(path) < max_age or last >= end:
            log(f"Disco: {name} al día ({len(sec['time']):,} registros hasta {last})")
            return data
        start = max(full_start, last - timedelta(days=3))
        log(f"Disco: {name} llega hasta {last}; descargo solo lo nuevo ({start} → {end})")
    else:
        start, sec = full_start, None
        n = (end - start).days + 1
        log(f"Disco: {name} no existe; descargo {n:,} días en {math.ceil(n / chunk_days)} trozo(s)")
    while start <= end:
        stop = min(end, start + timedelta(days=chunk_days - 1))
        part = (fetch(start, stop) or {}).get(section) or {}
        sec = merge_section(sec, part) if part.get("time") else sec
        data = {section: sec or {"time": []}}
        _save_json(path, data)                     # guarda el progreso por si se corta
        start = stop + timedelta(days=1)
    return data


def era5_fetch(loc, hourly=None, daily=None):
    def fetch(start, stop):
        p = dict(_ll(loc), start_date=start.isoformat(), end_date=stop.isoformat(),
                 timeformat="unixtime", models="era5")
        if hourly:
            p["hourly"] = hourly
        if daily:
            p["daily"] = daily
        try:
            return get_json(URL_ARCHIVE, p, timeout=180)
        except RuntimeError:
            p.pop("models")
            return get_json(URL_ARCHIVE, p, timeout=180)
    return fetch


def era5_hourly(loc):
    end = date.today() - timedelta(days=6)
    return update_history(os.path.join(loc_dir(loc), "era5_horario.json"), "hourly",
                          end - timedelta(days=3652), end,
                          era5_fetch(loc, hourly="temperature_2m"), 1500, 20 * 3600)


def era5_daily(loc):
    end = date.today() - timedelta(days=6)
    return update_history(os.path.join(loc_dir(loc), "era5_diario.json"), "daily",
                          HISTORY_START, end, era5_fetch(loc, daily="temperature_2m_mean"),
                          2400, 6 * 86400)


def prev_runs(loc, key, ids):
    """Pronósticos pasados de un modelo (1..7 días de antelación). Prueba cada id."""
    end = date.today() - timedelta(days=6)
    start = max(PREV_RUNS_START, end - timedelta(days=640))
    hourly = ",".join(f"temperature_2m_previous_day{L}" for L in LEADS)
    last_err = None
    for mid in ids:
        path = os.path.join(loc_dir(loc), f"pasados_{key}_{mid}.json")

        def fetch(a, b, mid=mid):
            return get_json(URL_PREV, dict(_ll(loc), start_date=a.isoformat(), end_date=b.isoformat(),
                                           hourly=hourly, models=mid, timeformat="unixtime"),
                            timeout=180)
        try:
            data = update_history(path, "hourly", start, end, fetch, 400, 20 * 3600)
        except RuntimeError as e:
            last_err = e
            continue
        if len(series(data, "hourly", "temperature_2m_previous_day1")) > 24 * 45:
            return data
        last_err = RuntimeError("sin datos para esta zona")
    raise last_err or RuntimeError("sin datos")


RESOLVED = {}   # clave de modelo -> id que funcionó para el pronóstico actual


def live_forecast(loc, key, ids):
    order = [RESOLVED[key]] + [i for i in ids if i != RESOLVED[key]] if key in RESOLVED else ids
    last_err = None
    for mid in order:
        try:
            r = get_json(URL_FORECAST, dict(_ll(loc), hourly="temperature_2m", models=mid,
                                            forecast_days=8, timeformat="unixtime"))
        except RuntimeError as e:
            last_err = e
            continue
        if len(series(r, "hourly", "temperature_2m", mid)) >= 24:
            RESOLVED[key] = mid
            return r, mid
        last_err = RuntimeError("sin datos para esta zona")
    raise last_err or RuntimeError("sin datos")


def extras_forecast(loc):
    """Toda la batería de datos del modelo 'best match': humedad, viento, UV, sol…"""
    base = dict(_ll(loc), timeformat="unixtime", timezone=loc.get("timezone") or "auto",
                past_days=7, forecast_days=8)
    try:
        return get_json(URL_FORECAST, dict(base, hourly=EXTRA_HOURLY, daily=EXTRA_DAILY,
                                           current=EXTRA_CURRENT))
    except RuntimeError as e:
        if "400" not in str(e):
            raise
        return get_json(URL_FORECAST, dict(base, hourly=SAFE_HOURLY, daily=SAFE_DAILY))


# ---------------------------------------------------------------- segundo plano, arranque e icono
APP_ID = "clima-pi"
APP_VERSION = "2.5"                         # si arranca una versión distinta, sustituye a la que corre
WM_CLASS = "Clima-pi"                       # Tk pone en mayúscula la 1ª letra de className
TRAY_PKGS = "gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pil"


def sock_path():
    # /run/user/<uid> es la carpeta estándar para estos canales (ruta corta: límite de 107 caracteres)
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not os.path.isdir(base):
        base = DATA_DIR
    return os.path.join(base, f"{APP_ID}.sock")


def state_path():
    return os.path.join(DATA_DIR, "estado.json")


def script_path():
    """Ruta de este archivo, si está guardado (desde una pestaña sin guardar de Thonny no hay)."""
    try:
        p = os.path.abspath(__file__)
    except NameError:
        return None
    return p if os.path.isfile(p) else None


def send_cmd(cmd, timeout=2.0):
    """Manda una orden a la copia de Clima Pi que ya está funcionando."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path())
        s.sendall((cmd + "\n").encode())
        return s.recv(64).decode().strip()
    finally:
        s.close()


def instance_running(cmd):
    if not hasattr(socket, "AF_UNIX"):
        return False
    try:
        return send_cmd(cmd) == "ok"
    except OSError:
        return False


def start_command_server(q):
    """Escucha órdenes (show/update/quit/ping) del icono de la barra o de un 2º arranque."""
    if not hasattr(socket, "AF_UNIX"):
        return None
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        os.unlink(sock_path())
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path())
    srv.listen(4)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    cmd = conn.recv(256).decode().strip()
                    if cmd:
                        q.put(("cmd", cmd, None))
                        conn.sendall((f"ok {APP_VERSION}\n" if cmd == "version" else "ok\n").encode())
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()
    return srv


def have_pil():
    return importlib.util.find_spec("PIL") is not None


def tray_available():
    """¿Están gi y la librería AyatanaAppIndicator3 (la del icono de la barra)?"""
    try:
        if importlib.util.find_spec("gi") is None:
            return False
    except (ImportError, ValueError):
        return False
    import glob
    return bool(glob.glob("/usr/lib/*/girepository-1.0/AyatanaAppIndicator3-0.1.typelib") +
                glob.glob("/usr/lib/girepository-1.0/AyatanaAppIndicator3-0.1.typelib"))


def _font(size, bold=True):
    from PIL import ImageFont
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def weather_color(code):
    """Color del icono según el tiempo de hoy."""
    if code is None:
        return (75, 85, 99)
    code = int(code)
    if code >= 95:
        return (126, 34, 206)       # tormenta
    if code >= 71 and code not in (80, 81, 82):
        return (100, 116, 139)      # nieve
    if code >= 51:
        return (37, 99, 235)        # lluvia / llovizna
    if code >= 45:
        return (107, 114, 128)      # niebla
    if code == 3:
        return (100, 116, 139)      # nublado
    if code == 2:
        return (202, 138, 4)        # parcialmente nublado
    return (234, 88, 12)            # despejado


def make_temp_icon(temp, code, size=64):
    """Icono para la barra: la temperatura sobre un color que resume el tiempo de hoy."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((1, 1, size - 2, size - 2), radius=size // 5, fill=weather_color(code))
    text = "--" if temp is None else f"{round(temp):d}°"
    fs = int(size * 0.62)
    while fs > 10:
        f = _font(fs)
        box = d.textbbox((0, 0), text, font=f)
        if box[2] - box[0] <= size - 8:
            break
        fs -= 2
    box = d.textbbox((0, 0), text, font=f)
    x = (size - (box[2] - box[0])) / 2 - box[0]
    y = (size - (box[3] - box[1])) / 2 - box[1]
    d.text((x, y), text, font=f, fill="white")
    return img


def make_app_icon(path, size=256):
    """Icono de la aplicación (sol y nube) para el menú y la barra de tareas."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=48 * s, fill=(17, 20, 24))
    d.ellipse((60 * s, 44 * s, 156 * s, 140 * s), fill=(250, 204, 21))
    for x0, y0, x1, y1 in ((52, 118, 152, 206), (110, 92, 214, 196), (40, 150, 222, 212)):
        d.ellipse((x0 * s, y0 * s, x1 * s, y1 * s), fill=(241, 245, 249))
    d.rounded_rectangle((46 * s, 170 * s, 216 * s, 212 * s), radius=21 * s, fill=(241, 245, 249))
    d.rounded_rectangle((70 * s, 222 * s, 186 * s, 232 * s), radius=5 * s, fill=(74, 222, 128))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return path


def desktop_paths():
    home = os.path.expanduser("~")
    return {"menu": os.path.join(home, ".local", "share", "applications", f"{APP_ID}.desktop"),
            "autostart": os.path.join(home, ".config", "autostart", f"{APP_ID}.desktop"),
            "icon": os.path.join(DATA_DIR, f"{APP_ID}.png")}


def _desktop_entry(path, icon, background):
    exe = sys.executable or "python3"
    extra = " --background" if background else ""
    lines = ["[Desktop Entry]", "Type=Application", "Name=Clima Pi",
             "Comment=Pronóstico del tiempo que combina 11 modelos meteorológicos",
             f'Exec="{exe}" "{path}"{extra}', f"Icon={icon}", "Terminal=false",
             "Categories=Utility;Science;", f"StartupWMClass={WM_CLASS}", "StartupNotify=false"]
    if background:
        lines += ["X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-Delay=10"]
    return "\n".join(lines) + "\n"


def install_menu_entry():
    """Pone Clima Pi en el menú de aplicaciones con su icono (se actualiza si mueves el archivo)."""
    path = script_path()
    if not path:
        return False
    p = desktop_paths()
    if have_pil() and not os.path.exists(p["icon"]):
        make_app_icon(p["icon"])
    content = _desktop_entry(path, p["icon"], False)
    try:
        with open(p["menu"]) as f:
            if f.read() == content:
                return True
    except OSError:
        pass
    os.makedirs(os.path.dirname(p["menu"]), exist_ok=True)
    with open(p["menu"], "w") as f:
        f.write(content)
    return True


def set_autostart(enabled):
    p = desktop_paths()
    if not enabled:
        try:
            os.remove(p["autostart"])
        except FileNotFoundError:
            pass
        return
    path = script_path()
    if not path:
        raise RuntimeError("guarda primero el programa como archivo (Archivo → Guardar como… "
                           "clima_pi.py) y ábrelo desde ahí")
    if have_pil() and not os.path.exists(p["icon"]):
        make_app_icon(p["icon"])
    os.makedirs(os.path.dirname(p["autostart"]), exist_ok=True)
    with open(p["autostart"], "w") as f:
        f.write(_desktop_entry(path, p["icon"], True))


def autostart_enabled():
    return os.path.exists(desktop_paths()["autostart"])


# ---------------------------------------------------------------- icono en la barra (proceso aparte)
def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_tray(parent_pid=None):
    """Proceso del icono de la barra de tareas. Lee estado.json y manda órdenes por el socket.

    Usa AyatanaAppIndicator directamente. La barra de Raspberry Pi OS (wf-panel-pi) busca el
    icono POR NOMBRE dentro de la carpeta que indica el programa (IconThemePath); una ruta de
    archivo como nombre (lo que hace pystray) no la entiende y el icono sale vacío."""
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AI
    except (ImportError, ValueError) as e:
        print(f"Falta la librería del icono ({e}). Instálala con: sudo apt install {TRAY_PKGS}",
              file=sys.stderr)
        sys.exit(2)

    # Iconos en la carpeta estándar del usuario. wf-panel-pi (item.cpp) ignora la carpeta propia
    # que indica un programa (usa add_resource_path, que es para recursos internos) y busca el
    # nombre en el tema de iconos normal; GTK solo vuelve a leer esa carpeta si cambia la fecha
    # de ~/.local/share/icons y han pasado 5 s, así que tocamos esa fecha y esperamos 7 s.
    icons_root = os.path.join(os.path.expanduser("~"), ".local", "share", "icons")
    icon_dir = os.path.join(icons_root, "hicolor", "64x64", "apps")
    os.makedirs(icon_dir, exist_ok=True)
    for f in os.listdir(icon_dir):                  # restos de arranques anteriores
        if f.startswith(f"{APP_ID}-t"):
            try:
                os.remove(os.path.join(icon_dir, f))
            except OSError:
                pass
    st = {"data": {}, "mtime": 0.0, "icon": None, "n": 0, "fails": 0, "ind": None}

    def new_icon(d):
        """Guarda el icono con un nombre nuevo (con el mismo nombre la barra no lo refrescaría)."""
        st["n"] += 1
        name = f"{APP_ID}-t{int(time.time())}-{st['n']}"
        try:
            make_temp_icon(d.get("temp"), (d.get("today") or {}).get("code"), 64).save(
                os.path.join(icon_dir, name + ".png"))
            os.utime(icons_root)                    # avisa a GTK de que hay iconos nuevos
        except Exception as e:                      # sin Pillow: icono genérico del tema
            print(f"sin icono dinámico: {e}", file=sys.stderr)
            return "weather-few-clouds"
        return name

    def apply_icon(name):
        """Se llama 7 s después de guardar el archivo, cuando la barra ya puede encontrarlo."""
        old, st["icon"] = st["icon"], name
        if st["ind"] is None:
            ind = AI.Indicator.new(APP_ID, name, AI.IndicatorCategory.APPLICATION_STATUS)
            ind.set_title(title())
            ind.set_menu(menu)
            ind.set_status(AI.IndicatorStatus.ACTIVE)
            st["ind"] = ind
            print(f"icono en la barra activo: {icon_dir}/{name}.png", file=sys.stderr, flush=True)
        else:
            st["ind"].set_icon_full(name, title())
            st["ind"].set_title(title())
        if old and old.startswith(f"{APP_ID}-t"):
            def remove_old(p=os.path.join(icon_dir, old + ".png")):
                try:
                    os.remove(p)
                except OSError:
                    pass
                return False
            GLib.timeout_add_seconds(60, remove_old)
        return False

    def schedule_icon():
        name = new_icon(st["data"])
        GLib.timeout_add_seconds(7, lambda: apply_icon(name))

    def load():
        try:
            m = os.path.getmtime(state_path())
        except OSError:
            return False
        if m == st["mtime"]:
            return False
        d = _load_json(state_path())
        if d is None:
            return False
        st["data"], st["mtime"] = d, m
        return True

    def lines():
        d = st["data"]
        if not d:
            return ["Clima Pi: esperando el primer pronóstico…"]
        t, tm = d.get("today") or {}, d.get("tomorrow") or {}
        return [
            f"{d.get('loc', '')}: {fmt(d.get('temp'), '{:.0f}', ' °C')} ahora · {d.get('desc', '')}",
            f"Hoy: máx. {fmt(t.get('max'), suffix='°')} · mín. {fmt(t.get('min'), suffix='°')} · "
            f"{t.get('desc', '')} · lluvia {fmt(t.get('rain_p'), suffix=' %')}",
            f"Hoy a las 9h {fmt(t.get('h9'), suffix='°')} · 15h {fmt(t.get('h15'), suffix='°')} · "
            f"21h {fmt(t.get('h21'), suffix='°')}",
            f"Mañana: {fmt(tm.get('max'), suffix='°')} / {fmt(tm.get('min'), suffix='°')} · {tm.get('desc', '')}",
            f"Amanecer {d.get('sunrise', '—')} · atardecer {d.get('sunset', '—')}",
            f"Actualizado {d.get('updated_hhmm', '—')}",
        ]

    def title():
        d = st["data"]
        if not d:
            return "Clima Pi"
        t = d.get("today") or {}
        return (f"Clima Pi · {d.get('loc', '')}: {fmt(d.get('temp'), '{:.0f}', ' °C')} · hoy "
                f"{fmt(t.get('max'), suffix='°')}/{fmt(t.get('min'), suffix='°')}")

    def send(c):
        def act(_item):
            try:
                send_cmd(c)
            except OSError:
                pass
            if c == "quit":
                Gtk.main_quit()
        return act

    load()
    menu = Gtk.Menu()
    info_items = []
    for _ in range(6):
        it = Gtk.MenuItem(label="")
        it.set_sensitive(False)
        menu.append(it)
        info_items.append(it)
    menu.append(Gtk.SeparatorMenuItem())
    for label, c in (("Abrir Clima Pi", "show"), ("Actualizar ahora", "update"),
                     ("Salir de Clima Pi", "quit")):
        it = Gtk.MenuItem(label=label)
        it.connect("activate", send(c))
        menu.append(it)

    def refresh_menu():
        ls = lines()
        for i, it in enumerate(info_items):
            it.set_label(ls[i] if i < len(ls) else "")
            it.set_visible(i < len(ls))

    refresh_menu()
    menu.show_all()
    refresh_menu()
    schedule_icon()

    def tick():
        if load():
            refresh_menu()
            schedule_icon()
        alive = pid_alive(parent_pid) if parent_pid else instance_running("ping")
        st["fails"] = 0 if alive else st["fails"] + 1
        if st["fails"] >= 2:                        # la aplicación se cerró: quitar el icono
            Gtk.main_quit()
            return False
        return True

    GLib.timeout_add_seconds(5, tick)
    Gtk.main()


# ---------------------------------------------------------------- configuración
def load_config():
    return (_load_json(os.path.join(DATA_DIR, "config.json")) or {}).get("location")


def save_config(loc):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "config.json"), "w") as f:
        json.dump({"location": loc}, f, ensure_ascii=False, indent=1)


def get_tz(loc):
    if ZoneInfo:
        try:
            return ZoneInfo(loc.get("timezone") or "UTC")
        except Exception:
            pass
    return timezone.utc


def loc_name(loc):
    return ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)


def midnight(d, tz):
    return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


# ================================================================ Sol y Luna
RAD = math.pi / 180


def _rev(x):
    return x % 360.0


def _kepler(M, e):
    E = M + e / RAD * math.sin(M * RAD) * (1 + e * math.cos(M * RAD))
    for _ in range(6):
        E = E - (E - e / RAD * math.sin(E * RAD) - M) / (1 - e * math.cos(E * RAD))
    return E


def _sun(d):
    w = 282.9404 + 4.70935e-5 * d
    e = 0.016709 - 1.151e-9 * d
    M = _rev(356.0470 + 0.9856002585 * d)
    E = _kepler(M, e)
    v = math.atan2(math.sqrt(1 - e * e) * math.sin(E * RAD), math.cos(E * RAD) - e) / RAD
    return _rev(v + w), M, w


def _moon(d):
    """Posición de la Luna (método de P. Schlyter, error ~1 minuto en salida/puesta)."""
    N = 125.1228 - 0.0529538083 * d
    i, w, a, e = 5.1454, 318.0634 + 0.1643573223 * d, 60.2666, 0.054900
    M = _rev(115.3654 + 13.0649929509 * d)
    E = _kepler(M, e)
    xv = a * (math.cos(E * RAD) - e)
    yv = a * math.sqrt(1 - e * e) * math.sin(E * RAD)
    v = math.atan2(yv, xv) / RAD
    r = math.hypot(xv, yv)
    vw, Nr, ir = (v + w) * RAD, N * RAD, i * RAD
    xh = r * (math.cos(Nr) * math.cos(vw) - math.sin(Nr) * math.sin(vw) * math.cos(ir))
    yh = r * (math.sin(Nr) * math.cos(vw) + math.cos(Nr) * math.sin(vw) * math.cos(ir))
    zh = r * math.sin(vw) * math.sin(ir)
    lon = math.atan2(yh, xh) / RAD
    lat = math.atan2(zh, math.hypot(xh, yh)) / RAD
    slon, Ms, ws = _sun(d)
    Lm = M + w + N
    D = Lm - (Ms + ws)
    F = Lm - N

    def s(x):
        return math.sin(x * RAD)
    lon += (-1.274 * s(M - 2 * D) + 0.658 * s(2 * D) - 0.186 * s(Ms) - 0.059 * s(2 * M - 2 * D)
            - 0.057 * s(M - 2 * D + Ms) + 0.053 * s(M + 2 * D) + 0.046 * s(2 * D - Ms)
            + 0.041 * s(M - Ms) - 0.035 * s(D) - 0.031 * s(M + Ms) - 0.015 * s(2 * F - 2 * D)
            + 0.011 * s(M - 4 * D))
    lat += (-0.173 * s(F - 2 * D) - 0.055 * s(M - F - 2 * D) - 0.046 * s(M + F - 2 * D)
            + 0.033 * s(F + 2 * D) + 0.017 * s(2 * M + F))
    return _rev(lon), lat, slon


def _days(ts):
    return ts / 86400.0 + 2440587.5 - 2451543.5


def _altitude(lon_ecl, lat_ecl, ts, lat, lon):
    d = _days(ts)
    ecl = (23.4393 - 3.563e-7 * d) * RAD
    lo, la = lon_ecl * RAD, lat_ecl * RAD
    x = math.cos(lo) * math.cos(la)
    y = math.sin(lo) * math.cos(la)
    z = math.sin(la)
    yq = y * math.cos(ecl) - z * math.sin(ecl)
    zq = y * math.sin(ecl) + z * math.cos(ecl)
    ra = math.atan2(yq, x) / RAD
    dec = math.atan2(zq, math.hypot(x, yq))
    gmst = _rev(280.46061837 + 360.98564736629 * (ts / 86400.0 + 2440587.5 - 2451545.0))
    ha = (gmst + lon - ra) * RAD
    p = lat * RAD
    return math.asin(math.sin(p) * math.sin(dec) + math.cos(p) * math.cos(dec) * math.cos(ha)) / RAD


def moon_alt(ts, lat, lon):
    ml, mb, _ = _moon(_days(ts))
    return _altitude(ml, mb, ts, lat, lon)


def sun_alt(ts, lat, lon):
    sl, _, _ = _sun(_days(ts))
    return _altitude(sl, 0.0, ts, lat, lon)


def moon_phase(ts):
    """(iluminación 0..1, elongación 0..360: 0 = nueva, 180 = llena)."""
    ml, mb, sl = _moon(_days(ts))
    return (1 - math.cos(mb * RAD) * math.cos((ml - sl) * RAD)) / 2, _rev(ml - sl)


def phase_name(el):
    if el < 7 or el >= 353:
        return "Luna nueva"
    for limit, name in ((83, "Creciente"), (97, "Cuarto creciente"), (173, "Gibosa creciente"),
                        (187, "Luna llena"), (263, "Gibosa menguante"), (277, "Cuarto menguante")):
        if el < limit:
            return name
    return "Menguante"


EVALS = [0]     # contador de posiciones calculadas (para la pestaña «En vivo»)


def _rise_set(f, t0, h0, step=1200):
    f0 = f

    def f(t):
        EVALS[0] += 1
        return f0(t)
    rise = sett = None
    prev_t, prev = t0, f(t0) - h0
    t = t0 + step
    while t <= t0 + 86400 + 1e-6:
        cur = f(t) - h0
        if (prev < 0) != (cur < 0):
            a, b, fa = prev_t, t, prev
            for _ in range(18):
                m = (a + b) / 2
                fm = f(m) - h0
                if (fa < 0) != (fm < 0):
                    b = m
                else:
                    a, fa = m, fm
            tc = (a + b) / 2
            if cur > prev and rise is None:
                rise = tc
            elif cur < prev and sett is None:
                sett = tc
        prev_t, prev = t, cur
        t += step
    return rise, sett


def moon_rise_set(t0, lat, lon):
    return _rise_set(lambda t: moon_alt(t, lat, lon), t0, 0.125)


def sun_rise_set(t0, lat, lon):
    return _rise_set(lambda t: sun_alt(t, lat, lon), t0, -0.833)


def civil_twilight(t0, lat, lon):
    """Alba y anochecer civil: el Sol a 6° bajo el horizonte (aún hay luz para estar fuera)."""
    return _rise_set(lambda t: sun_alt(t, lat, lon), t0, -6.0)


def next_phases(ts, days=40):
    names = {0: "Luna nueva", 90: "Cuarto creciente", 180: "Luna llena", 270: "Cuarto menguante"}
    out = []
    t, prev = ts, moon_phase(ts)[1]
    while t < ts + days * 86400:
        t2 = t + 6 * 3600
        cur = moon_phase(t2)[1]
        for target in names:
            a = _rev(prev - target + 180) - 180
            b = _rev(cur - target + 180) - 180
            if a < 0 <= b and b - a < 90:
                lo, hi = t, t2
                for _ in range(22):
                    m = (lo + hi) / 2
                    if _rev(moon_phase(m)[1] - target + 180) - 180 < 0:
                        lo = m
                    else:
                        hi = m
                out.append(((lo + hi) / 2, names[target]))
        t, prev = t2, cur
    return out


# ================================================================ modelo
def local_parts(ts, tz):
    """Día del año (0..365), hora local y nº de día local para un array de timestamps."""
    ts = np.asarray(ts, dtype=np.int64)
    days = ts // 86400
    uniq, inv = np.unique(days, return_inverse=True)
    offs = np.array([tz.utcoffset(datetime.fromtimestamp(int(d) * 86400 + 43200, timezone.utc))
                     .total_seconds() for d in uniq], dtype=np.int64)
    lt = ts + offs[inv]
    hour = ((lt // 3600) % 24).astype(int)
    dayno = lt // 86400
    dd = dayno.astype("datetime64[D]")
    doy = (dd - dd.astype("datetime64[Y]")).astype(int)
    return np.clip(doy, 0, 365), hour, dayno


def build_climatology(truth, tz):
    ts = np.array(sorted(truth))
    temp = np.array([truth[t] for t in ts])
    doy, hour, _ = local_parts(ts, tz)
    s = np.zeros((366, 24))
    c = np.zeros((366, 24))
    np.add.at(s, (doy, hour), temp)
    np.add.at(c, (doy, hour), 1)
    S = sum(np.roll(s, k, axis=0) for k in range(-7, 8))
    C = sum(np.roll(c, k, axis=0) for k in range(-7, 8))
    clim = S / np.maximum(C, 1)
    clim[C == 0] = np.nan
    return clim


def impute(M):
    """Rellena huecos (modelo sin dato a esa hora) con la media de los demás modelos."""
    M = M.copy()
    mean = row_mean(M)
    rows, cols = np.where(~np.isfinite(M))
    M[rows, cols] = mean[rows]
    return M


def row_mean(M):
    cnt = np.isfinite(M).sum(axis=1)
    s = np.where(np.isfinite(M), M, 0).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)


def features(Mi, doy, hour, clim):
    mean = Mi.mean(axis=1)
    spread = Mi.std(axis=1) if Mi.shape[1] > 1 else np.zeros(len(Mi))
    a = 2 * np.pi * hour / 24.0
    b = 2 * np.pi * doy / 365.25
    extra = np.column_stack([clim[doy, hour], np.sin(a), np.cos(a), np.sin(b), np.cos(b),
                             mean * np.sin(a), mean * np.cos(a), spread])
    return np.column_stack([Mi, extra])


class Ridge:
    """Regresión lineal con regularización (ridge), solo con numpy."""

    def fit(self, X, y, lam=2.0):
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        A = Z.T @ Z + lam * np.eye(Z.shape[1])
        A[0, 0] -= lam
        self.w = np.linalg.solve(A, Z.T @ y)
        return self

    def predict(self, X):
        return self.w[0] + ((X - self.mu) / self.sd) @ self.w[1:]

    def to_dict(self):
        return {"mu": self.mu.tolist(), "sd": self.sd.tolist(), "w": self.w.tolist()}

    @classmethod
    def from_dict(cls, d):
        r = cls()
        r.mu, r.sd, r.w = (np.array(d[k], dtype=float) for k in ("mu", "sd", "w"))
        return r


# ---------------------------------------------------------------- guardar lo aprendido
MODEL_VERSION = 2
MODEL_SIGNATURE = [k for k, *_ in MODELS]       # si cambia la lista de modelos, se reentrena


def _nan_to_none(a):
    return [[None if not math.isfinite(v) else round(float(v), 3) for v in row] for row in a]


def save_model(loc, tr):
    """Guarda el Modelo Pi entrenado para no empezar de cero en el próximo arranque."""
    leads = {}
    for L, info in tr["leads"].items():
        leads[str(L)] = {k: v for k, v in info.items() if k not in ("model", "daily")}
        leads[str(L)]["model"] = info["model"].to_dict()
        leads[str(L)]["daily"] = {k: m.to_dict() for k, m in info.get("daily", {}).items()}
    data = {"version": MODEL_VERSION, "signature": MODEL_SIGNATURE, "trained_at": tr["trained_at"],
            "start": tr["start"], "models": tr["models"], "clim": _nan_to_none(tr["clim"]),
            "leads": leads}
    _save_json(os.path.join(loc_dir(loc), "modelo_pi.json"), data)


def load_model(loc):
    d = _load_json(os.path.join(loc_dir(loc), "modelo_pi.json"))
    if not d or d.get("version") != MODEL_VERSION or d.get("signature") != MODEL_SIGNATURE:
        return None
    try:
        leads = {}
        for L, info in d["leads"].items():
            x = dict(info)
            x["model"] = Ridge.from_dict(info["model"])
            x["daily"] = {k: Ridge.from_dict(m) for k, m in info.get("daily", {}).items()}
            leads[int(L)] = x
        return {"clim": np.array(d["clim"], dtype=float), "leads": leads,
                "trained_at": d["trained_at"], "start": d["start"], "models": d["models"]}
    except (KeyError, TypeError, ValueError):
        return None


def daily_extremes_mae(dayno, y, pred, min_hours=20):
    """Error medio de la máxima y la mínima diarias (como las que da la tele)."""
    emax, emin = [], []
    order = np.argsort(dayno, kind="stable")
    dn, yy, pp = dayno[order], y[order], pred[order]
    bounds = np.flatnonzero(np.diff(dn)) + 1
    for a, b in zip(np.r_[0, bounds], np.r_[bounds, len(dn)]):
        if b - a < min_hours or not np.isfinite(pp[a:b]).all():
            continue
        emax.append(abs(pp[a:b].max() - yy[a:b].max()))
        emin.append(abs(pp[a:b].min() - yy[a:b].min()))
    if not emax:
        return None, None, 0
    return float(np.mean(emax)), float(np.mean(emin)), len(emax)


def daily_table(dayno, Mi, y, climv, doy, min_hours=20):
    """Agrupa por día local: máx./mín. de cada modelo, de la realidad y de lo normal."""
    bounds = np.flatnonzero(np.diff(dayno)) + 1
    rows = []
    for a, b in zip(np.r_[0, bounds], np.r_[bounds, len(dayno)]):
        if b - a < min_hours:
            continue
        rows.append((dayno[a], Mi[a:b].max(axis=0), Mi[a:b].min(axis=0), y[a:b].max(), y[a:b].min(),
                     np.nanmax(climv[a:b]), np.nanmin(climv[a:b]), doy[a]))
    if not rows:
        return None
    return {"day": np.array([r[0] for r in rows]), "Fmax": np.vstack([r[1] for r in rows]),
            "Fmin": np.vstack([r[2] for r in rows]), "ymax": np.array([r[3] for r in rows]),
            "ymin": np.array([r[4] for r in rows]), "cmax": np.array([r[5] for r in rows]),
            "cmin": np.array([r[6] for r in rows]), "doy": np.array([r[7] for r in rows])}


def daily_features(F, c, doy):
    b = 2 * np.pi * doy / 365.25
    spread = F.std(axis=1) if F.shape[1] > 1 else np.zeros(len(F))
    return np.column_stack([F, F.mean(axis=1), spread, c, np.sin(b), np.cos(b)])


def train(era5_resp, prev, app_resp, tz):
    t_all = time.perf_counter()
    truth = series(era5_resp, "hourly", "temperature_2m")
    if len(truth) < 24 * 365:
        raise RuntimeError("historial ERA5 insuficiente")
    log(f"ENTRENAMIENTO: {len(truth):,} horas de temperatura real (ERA5) y {len(prev)} modelos", "train")
    t0 = time.perf_counter()
    clim = build_climatology(truth, tz)
    log(f"  Normal para cada día y hora: tabla 366 × 24 promediando ±7 días "
        f"({1000 * (time.perf_counter() - t0):.0f} ms)", "train")
    out = {"clim": clim, "leads": {}, "trained_at": time.time(), "start": None,
           "models": [k for k, *_ in MODELS if k in prev]}
    truth_ts = np.array(sorted(truth))
    truth_v = np.array([truth[t] for t in truth_ts])

    for L in LEADS:
        var = f"temperature_2m_previous_day{L}"
        fc = {k: series(prev[k], "hourly", var) for k in prev}
        use = [k for k, *_ in MODELS if len(fc.get(k, {})) > 24 * 45]
        if not use:
            continue
        M = np.full((len(truth_ts), len(use)), np.nan)
        for j, k in enumerate(use):
            d = fc[k]
            M[:, j] = [d.get(int(t), np.nan) for t in truth_ts]
        need = max(1, (len(use) + 1) // 2)
        ok = np.isfinite(M).sum(axis=1) >= need
        if ok.sum() < 24 * 60:
            continue
        ts, y, M = truth_ts[ok], truth_v[ok], M[ok]
        doy, hour, dayno = local_parts(ts, tz)
        Mi = impute(M)
        X = features(Mi, doy, hour, clim)
        good = np.isfinite(X).all(axis=1) & np.isfinite(y)
        ts, y, M, Mi, X, doy, hour, dayno = (a[good] for a in (ts, y, M, Mi, X, doy, hour, dayno))
        n = len(y)
        cut = int(n * 0.8)
        cut = int(np.searchsorted(dayno, dayno[cut]))          # cortar en un cambio de día

        t0 = time.perf_counter()
        pred = Ridge().fit(X[:cut], y[:cut]).predict(X[cut:])
        t_fit = 1000 * (time.perf_counter() - t0)
        yt, dt = y[cut:], dayno[cut:]
        cons = row_mean(M[cut:])
        clim_t = clim[doy[cut:], hour[cut:]]
        methods = {"pi": pred, "consenso": cons, "clim": clim_t}
        for j, k in enumerate(use):
            methods[k] = M[cut:, j]
        if app_resp:
            a = series(app_resp, "hourly", var)
            methods["app"] = np.array([a.get(int(t), np.nan) for t in ts[cut:]])

        mae, mae_max, mae_min = {}, {}, {}
        for k, p in methods.items():
            m = np.isfinite(p)
            if m.sum() < 24 * 7:
                continue
            mae[k] = float(np.mean(np.abs(p[m] - yt[m])))
            mx, mn, nd = daily_extremes_mae(dt, yt, p)
            if mx is not None:
                mae_max[k], mae_min[k] = mx, mn

        # --- modelos propios de máxima y mínima del día (lo que dan las noticias)
        daily_models = {}
        dt_ = daily_table(dayno, Mi, y, clim[doy, hour], doy)
        if dt_ is not None and len(dt_["day"]) >= 60:
            dcut = int(np.searchsorted(dt_["day"], dayno[cut]))
            if 20 <= dcut < len(dt_["day"]) - 10:
                for kind, store in (("max", mae_max), ("min", mae_min)):
                    Xd = daily_features(dt_["F" + kind], dt_["c" + kind], dt_["doy"])
                    yd = dt_["y" + kind]
                    if not np.isfinite(Xd).all():
                        continue
                    pdk = Ridge().fit(Xd[:dcut], yd[:dcut], lam=4.0).predict(Xd[dcut:])
                    store["pi"] = float(np.mean(np.abs(pdk - yd[dcut:])))
                    daily_models[kind] = Ridge().fit(Xd, yd, lam=4.0)

        out["leads"][L] = {"model": Ridge().fit(X, y), "use": use, "mae": mae,
                           "mae_max": mae_max, "mae_min": mae_min, "n": n, "daily": daily_models,
                           "n_test": n - cut, "test_from": int(ts[cut]), "test_to": int(ts[-1])}
        best = min((k for k in mae if k in NAMES and k != "app"), key=mae.get, default=None)
        log(f"  {L} día(s) de antelación: {n:,} horas × {X.shape[1]} variables "
            f"({len(use)} modelos + 8 correcciones) → sistema {X.shape[1] + 1}×{X.shape[1] + 1} "
            f"resuelto en {t_fit:.0f} ms", "train")
        log(f"      prueba con {n - cut:,} horas no vistas: Pi {mae['pi']:.2f} °C · media "
            f"{mae.get('consenso', float('nan')):.2f} · mejor modelo "
            f"{SHORT.get(best, best)} {mae.get(best, float('nan')):.2f}"
            + (f" · máx. diaria Pi {mae_max['pi']:.2f} / mín. {mae_min['pi']:.2f}"
               if "pi" in mae_max and "pi" in mae_min else ""), "train")
        out["start"] = int(ts[0]) if out["start"] is None else min(out["start"], int(ts[0]))

    if not out["leads"]:
        raise RuntimeError("no hay suficientes pronósticos pasados para entrenar")
    log(f"ENTRENAMIENTO terminado en {time.perf_counter() - t_all:.1f} s", "train")
    return out


# ---------------------------------------------------------------- pronóstico
def make_forecast(live, extras, trained, tz, now, loc):
    temps = {k: series(r, "hourly", "temperature_2m") for k, r in live.items()}
    temps = {k: v for k, v in temps.items() if v}
    xs = {v: series(extras, "hourly", v) for v in EXTRA_HOURLY.split(",")} if extras else {}
    xd = {v: series(extras, "daily", v) for v in EXTRA_DAILY.split(",")} if extras else {}
    order = [k for k, *_ in MODELS if k in temps]
    if not order and not xs.get("temperature_2m"):
        raise RuntimeError("los pronósticos llegaron vacíos")

    all_ts = sorted(set().union(*[temps[k].keys() for k in order], xs.get("temperature_2m", {}).keys()))
    fut = np.array([t for t in all_ts if now < t <= now + 7 * 86400], dtype=np.int64)

    # --- Modelo Pi
    pi = {}
    if trained and fut.size and order:
        leads = trained["leads"]
        lead_arr = np.clip((fut - now) // 86400 + 1, 1, 7).astype(int)
        doy, hour, _ = local_parts(fut, tz)
        for L in LEADS:
            idx = np.where(lead_arr == L)[0]
            if not idx.size:
                continue
            info = leads.get(L) or leads[min(leads, key=lambda k: abs(k - L))]
            use = info["use"]
            M = np.full((len(idx), len(use)), np.nan)
            for j, k in enumerate(use):
                d = temps.get(k, {})
                M[:, j] = [d.get(int(fut[i]), np.nan) for i in idx]
            enough = np.isfinite(M).sum(axis=1) >= max(1, (len(use) + 1) // 2)
            if not enough.any():
                continue
            Mi = impute(M)
            X = features(Mi, doy[idx], hour[idx], trained["clim"])
            with np.errstate(invalid="ignore"):
                p = info["model"].predict(X)
            ok_p = p[np.isfinite(p)]
            if ok_p.size:
                log(f"PREDICCIÓN +{L} día(s): {int(enough.sum())} horas × {len(use)} modelos → "
                    f"{ok_p.min():.1f} a {ok_p.max():.1f} °C (media de los modelos "
                    f"{np.nanmin(row_mean(M)):.1f} a {np.nanmax(row_mean(M)):.1f})")
            for kk, i in enumerate(idx):
                if enough[kk] and np.isfinite(p[kk]):
                    pi[int(fut[i])] = float(p[kk])
    band = {}
    for t in fut:
        t = int(t)
        vals = [temps[k][t] for k in order if t in temps[k]]
        if vals:
            band[t] = (min(vals), max(vals))
            if t not in pi:
                pi[t] = sum(vals) / len(vals)
        elif t not in pi and t in xs.get("temperature_2m", {}):
            pi[t] = xs["temperature_2m"][t]

    # --- observado (análisis del modelo best match)
    obs_src = xs.get("temperature_2m") or (temps[order[0]] if order else {})
    obs = {t: v for t, v in obs_src.items() if now - 7 * 86400 <= t <= now}

    # --- climatología para la ventana mostrada
    window = sorted(set(obs) | set(pi))
    clim_line = {}
    if trained and window:
        d_, h_, _ = local_parts(window, tz)
        cv = trained["clim"][d_, h_]
        clim_line = {t: float(v) for t, v in zip(window, cv) if np.isfinite(v)}

    # --- tabla por horas y resumen por día
    today = datetime.fromtimestamp(now, tz).date()
    hourly_rows, days = {}, []
    for i in range(7):
        d = today + timedelta(days=i)
        t0 = midnight(d, tz)
        rows = []
        for h in range(24):
            t = int(t0 + h * 3600)
            past = t <= now
            temp = obs.get(t) if past else pi.get(t)
            if temp is None:
                continue
            bm = xs.get("temperature_2m", {}).get(t)
            app = xs.get("apparent_temperature", {}).get(t)
            feels = app + (temp - bm) if app is not None and bm is not None else app
            rows.append({k: xs.get(k, {}).get(t) for k in xs} | {
                "t": t, "temp": temp, "feels": feels, "past": past, "band": band.get(t)})
        hourly_rows[d.isoformat()] = rows
        if not rows:
            continue
        temps_d = [r["temp"] for r in rows]

        def at(hh):
            r = next((r for r in rows if datetime.fromtimestamp(r["t"], tz).hour == hh), None)
            return r["temp"] if r else None
        cl = [clim_line[r["t"]] for r in rows if r["t"] in clim_line]
        dts = int(t0)
        days.append({
            "date": d, "max": max(temps_d), "min": min(temps_d),
            "h9": at(9), "h15": at(15), "h21": at(21),
            "code": xd.get("weather_code", {}).get(dts),
            "rain": xd.get("precipitation_sum", {}).get(dts),
            "rain_p": xd.get("precipitation_probability_max", {}).get(dts),
            "wind": xd.get("wind_speed_10m_max", {}).get(dts),
            "gust": xd.get("wind_gusts_10m_max", {}).get(dts),
            "wind_dir": xd.get("wind_direction_10m_dominant", {}).get(dts),
            "uv": xd.get("uv_index_max", {}).get(dts),
            "sunrise": xd.get("sunrise", {}).get(dts),
            "sunset": xd.get("sunset", {}).get(dts),
            "daylight": xd.get("daylight_duration", {}).get(dts),
            "anom": (sum(temps_d) / len(temps_d) - sum(cl) / len(cl)) if len(cl) >= 12 else None,
            "t0": dts,
        })

    # --- máxima y mínima con los modelos diarios propios (desde mañana)
    if trained:
        for dd in days:
            ahead = (dd["date"] - today).days
            if ahead < 1:
                continue
            info = trained["leads"].get(max(1, min(7, ahead)))
            if not info or not info.get("daily"):
                continue
            use = info["use"]
            hrs = np.array([int(dd["t0"] + h * 3600) for h in range(24)], dtype=np.int64)
            M = np.full((24, len(use)), np.nan)
            for j, k in enumerate(use):
                d = temps.get(k, {})
                M[:, j] = [d.get(int(t), np.nan) for t in hrs]
            ok = np.isfinite(M).sum(axis=1) >= max(1, (len(use) + 1) // 2)
            if ok.sum() < 20:
                continue
            Mi = impute(M[ok])
            d_, h_, _ = local_parts(hrs[ok], tz)
            cv = trained["clim"][d_, h_]
            parts = []
            for kind in ("max", "min"):
                mdl = info["daily"].get(kind)
                if mdl is None:
                    continue
                F = (Mi.max(axis=0) if kind == "max" else Mi.min(axis=0))[None, :]
                c = np.array([np.nanmax(cv) if kind == "max" else np.nanmin(cv)])
                Xd = daily_features(F, c, np.array([d_[0]]))
                if np.isfinite(Xd).all():
                    dd[kind] = float(mdl.predict(Xd)[0])
                    parts.append(f"{'máx' if kind == 'max' else 'mín'} {dd[kind]:.1f} °C "
                                 f"(modelos {F.min():.1f}–{F.max():.1f}, normal {c[0]:.1f})")
            if parts:
                log(f"  {DIAS[dd['date'].weekday()]} {dd['date'].day}: " + " · ".join(parts))

    # --- Sol y Luna
    lat, lon = loc["latitude"], loc["longitude"]
    t_astro = time.perf_counter()
    EVALS[0] = 0
    for dd in days:
        dd["civil_dawn"], dd["civil_dusk"] = civil_twilight(dd["t0"], lat, lon)
        mr, ms = moon_rise_set(dd["t0"], lat, lon)
        il, el = moon_phase(dd["t0"] + 43200)
        dd.update(moonrise=mr, moonset=ms, illum=il, elong=el, phase=phase_name(el))
        if dd["sunrise"] is None or dd["sunset"] is None:
            sr, ss = sun_rise_set(dd["t0"], lat, lon)
            dd["sunrise"] = dd["sunrise"] or sr
            dd["sunset"] = dd["sunset"] or ss
            if dd["daylight"] is None and sr and ss:
                dd["daylight"] = ss - sr
    if days:
        log(f"SOL Y LUNA: {len(days)} días · {EVALS[0]:,} posiciones del Sol y la Luna calculadas "
            f"para encontrar alba, salida y puesta en {1000 * (time.perf_counter() - t_astro):.0f} ms")

    # --- condiciones actuales
    cur = (extras or {}).get("current") or {}
    if not cur and xs:
        tnow = max((t for t in xs.get("temperature_2m", {}) if t <= now), default=None)
        cur = {k: xs[k].get(tnow) for k in xs} if tnow else {}
        cur["time"] = tnow
    nxt = min((t for t in xs.get("precipitation_probability", {}) if t > now), default=None)
    cur = dict(cur)
    cur["rain_p_next"] = xs.get("precipitation_probability", {}).get(nxt) if nxt else None
    cur["uv_now"] = xs.get("uv_index", {}).get(
        max((t for t in xs.get("uv_index", {}) if t <= now), default=-1))
    cur["visibility"] = xs.get("visibility", {}).get(
        max((t for t in xs.get("visibility", {}) if t <= now), default=-1))
    cur["dew_point"] = xs.get("dew_point_2m", {}).get(
        max((t for t in xs.get("dew_point_2m", {}) if t <= now), default=-1))

    return {"now": now, "temps": {k: {t: v for t, v in temps[k].items() if t > now} for k in order},
            "pi": pi, "band": band, "obs": obs, "clim": clim_line, "hours": hourly_rows,
            "days": days, "current": cur, "models": order, "trained": bool(trained),
            "phases": next_phases(now, 40), "extras": bool(xs)}


CORR_NAMES = ["Normal para la fecha", "Hora del día", "Hora del día", "Época del año", "Época del año",
              "Ajuste día/noche", "Ajuste día/noche", "Desacuerdo entre modelos"]


def explain_hour(r, trained, tz, t):
    """Desglose exacto de la predicción del Pi para la hora t.

    El ridge calcula  Pi = constante + Σ peso_i × dato_i ; aquí se devuelve cada término."""
    vals = {k: r["temps"].get(k, {}).get(t) for k in r["models"]}
    have = [v for v in vals.values() if v is not None]
    if not have:
        return None
    if not trained:
        n = len(have)
        return {"t": t, "lead": None, "const": 0.0, "corr": [], "total": sum(have) / n,
                "sum_w": 1.0, "rows": [(SHORT.get(k, k), v, 1 / n, v / n, True)
                                       for k, v in vals.items() if v is not None]}
    now = r["now"]
    L = int(min(7, max(1, (t - now) // 86400 + 1)))
    leads = trained["leads"]
    info = leads.get(L) or leads[min(leads, key=lambda k: abs(k - L))]
    use, mdl = info["use"], info["model"]
    M = np.array([[vals.get(k) if vals.get(k) is not None else np.nan for k in use]], dtype=float)
    if np.isfinite(M).sum() < max(1, (len(use) + 1) // 2):
        return None
    Mi = impute(M)
    doy, hour, _ = local_parts([t], tz)
    X = features(Mi, doy, hour, trained["clim"])[0]
    b = mdl.w[1:] / mdl.sd                                   # peso en °C por °C
    const = float(mdl.w[0] - np.sum(mdl.w[1:] * mdl.mu / mdl.sd))
    contrib = b * X
    n = len(use)
    rows = [(SHORT.get(k, k), float(X[j]), float(b[j]), float(contrib[j]), bool(np.isfinite(M[0, j])))
            for j, k in enumerate(use)]
    groups = {}
    for i, name in enumerate(CORR_NAMES):
        g = groups.setdefault(name, [0.0, []])
        g[0] += float(contrib[n + i])
        g[1].append(float(X[n + i]))
    corr = [(name, v, c) for name, (c, v) in groups.items()]
    return {"t": t, "lead": L, "rows": rows, "corr": corr, "const": const,
            "total": const + float(contrib.sum()), "sum_w": float(b[:n].sum()),
            "hour": int(hour[0]), "doy": int(doy[0])}


# ---------------------------------------------------------------- base de datos local
def db(loc):
    con = sqlite3.connect(os.path.join(loc_dir(loc), "predicciones.db"))
    con.execute("CREATE TABLE IF NOT EXISTS pred(issue INTEGER, valid INTEGER, temp REAL,"
                " PRIMARY KEY(issue, valid))")
    con.execute("CREATE INDEX IF NOT EXISTS pred_valid ON pred(valid, issue)")
    con.execute("CREATE TABLE IF NOT EXISTS tele(day TEXT PRIMARY KEY, tv_max REAL, tv_min REAL,"
                " pi_max REAL, pi_min REAL, saved INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS obs_day(day TEXT PRIMARY KEY, tmax REAL, tmin REAL)")
    return con


def log_predictions(loc, now, pi):
    """Guarda las predicciones; devuelve lo que el Pi predijo (≥20 h antes) para los últimos 3 días."""
    con = db(loc)
    try:
        issue = int(now) // 3600 * 3600
        con.executemany("INSERT OR REPLACE INTO pred VALUES (?,?,?)",
                        [(issue, t, v) for t, v in pi.items()])
        con.execute("DELETE FROM pred WHERE valid < ?", (issue - 30 * 86400,))
        total = con.execute("SELECT COUNT(*) FROM pred").fetchone()[0]
        log(f"BASE DE DATOS: guardadas {len(pi)} predicciones horarias ({total:,} en total, "
            f"últimos 30 días) para comprobar después cuánto acertó el Pi")
        rows = con.execute(
            "SELECT p.valid, p.temp FROM pred p WHERE p.valid BETWEEN ? AND ? AND p.issue = "
            "(SELECT MAX(q.issue) FROM pred q WHERE q.valid = p.valid AND q.issue <= p.valid - 72000)",
            (issue - 3 * 86400, issue)).fetchall()
        con.commit()
    finally:
        con.close()
    return {int(t): float(v) for t, v in rows}


def store_observed_days(loc, obs, tz, now):
    """Guarda máxima y mínima observadas de los días ya terminados."""
    by_day = {}
    for t, v in obs.items():
        by_day.setdefault(datetime.fromtimestamp(t, tz).date(), []).append(v)
    today = datetime.fromtimestamp(now, tz).date()
    con = db(loc)
    try:
        for d, vals in by_day.items():
            if d < today and len(vals) >= 22:
                con.execute("INSERT OR REPLACE INTO obs_day VALUES (?,?,?)",
                            (d.isoformat(), max(vals), min(vals)))
        con.commit()
    finally:
        con.close()


def save_tele(loc, day, tv_max, tv_min, pi_max, pi_min):
    con = db(loc)
    try:
        con.execute("INSERT OR REPLACE INTO tele VALUES (?,?,?,?,?,?)",
                    (day, tv_max, tv_min, pi_max, pi_min, int(time.time())))
        con.commit()
    finally:
        con.close()


def tele_score(loc):
    con = db(loc)
    try:
        rows = con.execute("SELECT t.day, t.tv_max, t.tv_min, t.pi_max, t.pi_min, o.tmax, o.tmin "
                           "FROM tele t JOIN obs_day o ON o.day = t.day ORDER BY t.day").fetchall()
        pending = con.execute("SELECT COUNT(*) FROM tele t LEFT JOIN obs_day o ON o.day = t.day "
                              "WHERE o.day IS NULL").fetchone()[0]
    finally:
        con.close()
    s = {"n": 0, "pi": 0, "tv": 0, "tie": 0, "pending": pending, "rows": rows}
    if not rows:
        return s
    etv, epi = [], []
    for _, tvx, tvn, pix, pin, ox, on in rows:
        a = (abs(tvx - ox) + abs(tvn - on)) / 2
        b = (abs(pix - ox) + abs(pin - on)) / 2
        etv.append(a)
        epi.append(b)
        if abs(a - b) < 0.05:
            s["tie"] += 1
        elif b < a:
            s["pi"] += 1
        else:
            s["tv"] += 1
    s.update(n=len(rows), err_tv=sum(etv) / len(etv), err_pi=sum(epi) / len(epi))
    return s


def climate_history(resp):
    d = series(resp, "daily", "temperature_2m_mean")
    years, by_month = {}, {}
    for t, v in d.items():
        dt = datetime.fromtimestamp(t, timezone.utc)
        years.setdefault(dt.year, []).append(v)
        by_month.setdefault((dt.year, dt.month), []).append(v)
    annual = [(y, sum(v) / len(v)) for y, v in sorted(years.items()) if len(v) >= 360]
    if len(annual) < 10:
        return None
    ys = np.array([a[0] for a in annual], float)
    ms = np.array([a[1] for a in annual])
    slope, icpt = np.polyfit(ys, ms, 1)
    last = int(ys[-1])
    recent_years = range(last - 4, last + 1)
    normal, recent = [], []
    for m in range(1, 13):
        n = [x for (y, mm), v in by_month.items() if mm == m and 1991 <= y <= 2020 for x in v]
        r = [x for (y, mm), v in by_month.items() if mm == m and y in recent_years for x in v]
        normal.append(sum(n) / len(n) if n else None)
        recent.append(sum(r) / len(r) if r else None)
    return {"annual": annual, "slope": float(slope), "icpt": float(icpt),
            "normal": normal, "recent": recent, "recent_label": f"{last - 4}–{last}",
            "warmest": max(annual, key=lambda a: a[1]),
            "coldest": min(annual, key=lambda a: a[1])}


# ================================================================ gráficos
def nice_ticks(lo, hi, n=5):
    if not math.isfinite(lo) or not math.isfinite(hi):
        lo, hi = 0, 1
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    ticks, v = [], start
    while v <= end + step * 1e-6:
        ticks.append(round(v, 10))
        v += step
    return ticks


def draw_moon(c, cx, cy, r, illum, elong, south=False, tag=""):
    """Dibuja la Luna con la parte iluminada que corresponde a la fase."""
    c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#2f3743", outline="#3b4452", tags=tag)
    side = 1 if elong < 180 else -1          # creciente: iluminada a la derecha (hemisferio norte)
    if south:
        side = -side
    k = illum
    pts = []
    n = 36
    for i in range(n + 1):                    # borde iluminado
        y = -r + 2 * r * i / n
        pts += [cx + side * math.sqrt(max(0, r * r - y * y)), cy + y]
    for i in range(n, -1, -1):                # terminador
        y = -r + 2 * r * i / n
        pts += [cx + side * (1 - 2 * k) * math.sqrt(max(0, r * r - y * y)), cy + y]
    if k > 0.01:
        c.create_polygon(*pts, fill=MOON_C, outline="", tags=tag)


class Chart:
    """Gráficos de líneas (con banda y tooltip) y de barras en un Canvas de Tk."""
    M = (54, 18, 34, 30)

    def __init__(self, parent, height):
        self.c = tk.Canvas(parent, bg=PANEL, height=height, highlightthickness=0)
        self.spec = None
        self.geo = None
        self.placeholder = "Esperando datos…"
        self.f_small = tkfont.Font(family=FONT, size=9)
        self.f_bold = tkfont.Font(family=FONT, size=9, weight="bold")
        self.c.bind("<Configure>", lambda e: self.redraw())
        self.c.bind("<Motion>", self._motion)
        self.c.bind("<Leave>", lambda e: self.c.delete("hover"))

    def show(self, spec):
        for se in spec.get("series", []):
            if spec.get("kind") == "line":
                if se.get("band") is not None:
                    se["_look"] = {t: (lo, hi) for t, lo, hi in se["band"]}
                else:
                    se["_look"] = {x: y for x, y in se["pts"] if y is not None}
        self.spec = spec
        self.redraw()

    def message(self, text):
        self.spec = None
        self.placeholder = text
        self.redraw()

    def redraw(self):
        c = self.c
        c.delete("all")
        self.geo = None
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        if not self.spec:
            c.create_text(w / 2, h / 2, text=self.placeholder, fill=MUTED, font=(FONT, 11),
                          justify="center")
            return
        if self.spec.get("title"):
            c.create_text(self.M[0], 8, anchor="nw", text=self.spec["title"],
                          fill=MUTED, font=self.f_small)
        if self.spec["kind"] == "line":
            self._line(w, h)
        else:
            self._bars(w, h)

    def _legend(self, items, w):
        c = self.c
        x_right = w - self.M[1]
        rows, row, width = [], [], 0
        title_w = self.f_small.measure(self.spec.get("title", "")) + 30
        avail = max(200, w - self.M[0] - self.M[1] - title_w)
        for label, color, dash in items:
            iw = 22 + self.f_small.measure(label) + 14
            if row and width + iw > avail:
                rows.append(row)
                row, width = [], 0
            row.append((label, color, dash, iw))
            width += iw
        if row:
            rows.append(row)
        for ri, row in enumerate(rows):
            x = x_right - sum(it[3] for it in row)
            y = 10 + ri * 14
            for label, color, dash, iw in row:
                c.create_line(x, y, x + 16, y, fill=color, width=4 if dash == "band" else 3,
                              dash=() if dash in (None, "band") else dash)
                c.create_text(x + 21, y, anchor="w", text=label, fill=FG, font=self.f_small)
                x += iw
        return len(rows)

    def _line(self, w, h):
        s, c = self.spec, self.c
        L, R, T, B = self.M
        items = [(se["label"], se.get("legend_color", se["color"]),
                  "band" if se.get("band") is not None else se.get("dash"))
                 for se in s["series"] if se.get("label") and not se.get("no_legend")]
        T = max(T, 14 * self._legend(items, w) + 16)
        ys = [p[1] for se in s["series"] if se.get("band") is None for p in se["pts"] if p[1] is not None]
        ys += [v for se in s["series"] if se.get("band") is not None for _, lo, hi in se["band"] for v in (lo, hi)]
        xs = [p[0] for se in s["series"] if se.get("band") is None for p in se["pts"] if p[1] is not None]
        if not ys:
            c.create_text(w / 2, h / 2, text="Sin datos", fill=MUTED, font=(FONT, 11))
            return
        x0, x1 = s.get("xrange") or (min(xs), max(xs))
        if x1 <= x0:
            x1 = x0 + 1
        yt = nice_ticks(min(ys), max(ys), 5)
        y0, y1 = yt[0], yt[-1]

        def X(x):
            return L + (x - x0) / (x1 - x0) * (w - L - R)

        def Y(y):
            return h - B - (y - y0) / (y1 - y0) * (h - T - B)

        unit = s.get("unit", "")
        for v in yt:
            c.create_line(L, Y(v), w - R, Y(v), fill=GRID)
            c.create_text(L - 6, Y(v), anchor="e", text=f"{v:g}{unit}", fill=MUTED, font=self.f_small)

        if s.get("xkind") == "time":
            tz = s["tz"]
            d = datetime.fromtimestamp(x0, tz).date()
            while True:
                mid = midnight(d, tz)
                if mid > x1:
                    break
                if mid >= x0:
                    c.create_line(X(mid), T, X(mid), h - B, fill=GRID)
                noon = mid + 43200
                if x0 <= noon <= x1:
                    c.create_text(X(noon), h - B + 14, text=f"{DIAS[d.weekday()]} {d.day}",
                                  fill=MUTED, font=self.f_small)
                d += timedelta(days=1)
        else:
            tk_ = nice_ticks(x0, x1, 8)
            step = max(1, tk_[1] - tk_[0])
            v = math.ceil(x0 / step) * step
            while v <= x1:
                c.create_line(X(v), T, X(v), h - B, fill=GRID)
                c.create_text(X(v), h - B + 14, text=f"{int(v)}", fill=MUTED, font=self.f_small)
                v += step

        for se in s["series"]:                       # bandas primero (al fondo)
            if se.get("band") is None:
                continue
            seg = []
            last = None
            for t, lo, hi in se["band"] + [(None, None, None)]:
                if t is None or (last is not None and t - last > 3 * 3600) or not (x0 <= t <= x1):
                    if len(seg) >= 2:
                        poly = [(X(a), Y(b)) for a, b, _ in seg] + [(X(a), Y(c_)) for a, _, c_ in reversed(seg)]
                        c.create_polygon(*[v for p in poly for v in p], fill=se["color"], outline="")
                    seg = []
                if t is not None and x0 <= t <= x1:
                    seg.append((t, hi, lo))
                last = t

        if s.get("now") is not None:
            xn = X(s["now"])
            c.create_line(xn, T, xn, h - B, fill=WARN, dash=(3, 3))
            c.create_text(xn + 4, T + 2, anchor="nw", text="ahora", fill=WARN, font=self.f_small)

        for se in s["series"]:
            if se.get("band") is not None:
                continue
            seg = []
            for x, y in se["pts"] + [(None, None)]:
                if y is None or x is None or not (x0 <= x <= x1):
                    if len(seg) >= 4:
                        c.create_line(*seg, fill=se["color"], width=se.get("width", 1.5),
                                      dash=se.get("dash") or ())
                    elif len(seg) == 2:
                        c.create_oval(seg[0] - 2, seg[1] - 2, seg[0] + 2, seg[1] + 2,
                                      fill=se["color"], outline="")
                    seg = []
                else:
                    seg += [X(x), Y(y)]
        self.geo = (x0, x1, y0, y1, L, R, T, B, w, h)

    def _motion(self, e):
        c = self.c
        c.delete("hover")
        s, g = self.spec, self.geo
        if not s or not g or s.get("kind") != "line" or s.get("xkind") != "time":
            return
        x0, x1, y0, y1, L, R, T, B, w, h = g
        if not (L <= e.x <= w - R and T <= e.y <= h - B):
            return
        t = x0 + (e.x - L) / (w - L - R) * (x1 - x0)
        t = int(round(t / 3600) * 3600)
        lines = []
        for se in s["series"]:
            if not se.get("label") or se.get("no_hover"):
                continue
            v = se.get("_look", {}).get(t)
            if v is None:
                continue
            if isinstance(v, tuple):
                lines.append((se["label"], se.get("legend_color", se["color"]), f"{v[0]:.1f}–{v[1]:.1f}°"))
            else:
                lines.append((se["label"], se["color"], f"{v:.1f}°"))
        if not lines:
            return
        xp = L + (t - x0) / (x1 - x0) * (w - L - R)
        c.create_line(xp, T, xp, h - B, fill=MUTED, tags="hover")
        dt = datetime.fromtimestamp(t, s["tz"])
        head = f"{DIAS_L[dt.weekday()]} {dt.day} · {dt:%H}:00"
        width = max([self.f_bold.measure(head)] +
                    [self.f_small.measure(f"{a}  {b}") + 18 for a, _, b in lines]) + 16
        height = 22 + 16 * len(lines)
        bx = e.x + 14 if e.x + 14 + width < w else e.x - 14 - width
        by = min(max(T, e.y - height / 2), h - B - height)
        c.create_rectangle(bx, by, bx + width, by + height, fill=BG, outline=GRID, tags="hover")
        c.create_text(bx + 8, by + 6, anchor="nw", text=head, fill=FG, font=self.f_bold, tags="hover")
        for i, (lab, col, val) in enumerate(lines):
            yy = by + 24 + 16 * i
            c.create_rectangle(bx + 8, yy + 3, bx + 16, yy + 11, fill=col, outline="", tags="hover")
            c.create_text(bx + 22, yy, anchor="nw", text=lab, fill=MUTED, font=self.f_small, tags="hover")
            c.create_text(bx + width - 8, yy, anchor="ne", text=val, fill=FG, font=self.f_small, tags="hover")

    def _bars(self, w, h):
        s, c = self.spec, self.c
        L, R, T, B = self.M
        if s.get("rotate"):
            B += 34
        T = max(T, 14 * self._legend([(se["label"], se["color"], None) for se in s["series"]], w) + 16)
        vals = [v for se in s["series"] for v in se["values"] if v is not None]
        if not vals:
            c.create_text(w / 2, h / 2, text="Sin datos", fill=MUTED, font=(FONT, 11))
            return
        yt = nice_ticks(min(0, min(vals)), max(vals), 5)
        y0, y1 = yt[0], yt[-1]

        def Y(y):
            return h - B - (y - y0) / (y1 - y0) * (h - T - B)

        unit = s.get("unit", "")
        for v in yt:
            c.create_line(L, Y(v), w - R, Y(v), fill=GRID)
            c.create_text(L - 6, Y(v), anchor="e", text=f"{v:g}{unit}", fill=MUTED, font=self.f_small)
        groups = s["groups"]
        gw = (w - L - R) / len(groups)
        bw = gw * 0.82 / len(s["series"])
        for gi, g in enumerate(groups):
            if s.get("rotate"):
                c.create_text(L + gw * (gi + 0.5) + 4, h - B + 6, text=g, fill=MUTED,
                              font=self.f_small, anchor="ne", angle=35)
            else:
                c.create_text(L + gw * (gi + 0.5), h - B + 14, text=g, fill=MUTED, font=self.f_small)
            for si, se in enumerate(s["series"]):
                v = se["values"][gi]
                if v is None:
                    continue
                x = L + gi * gw + gw * 0.09 + si * bw
                c.create_rectangle(x, Y(max(v, 0)), x + bw - 1, Y(min(v, 0)),
                                   fill=se["color"], outline="")


def to_pts(d, t0, t1, gap=3 * 3600):
    pts, last = [], None
    for t in sorted(k for k in d if t0 <= k <= t1):
        if last is not None and t - last > gap:
            pts.append((t, None))
        pts.append((t, d[t]))
        last = t
    return pts


def fmt(v, f="{:.0f}", suffix=""):
    return "—" if v is None else f.format(v) + suffix


def hhmm(ts, tz):
    return "—" if ts is None else datetime.fromtimestamp(ts, tz).strftime("%H:%M")


# ================================================================ interfaz
class App:
    def __init__(self, root, background=False):
        self.root = root
        self.background = background
        self.tray_proc = None
        self.tray_warned = False
        self.fail_count = 0
        root.title("Clima Pi")
        root.configure(bg=BG)
        root.geometry("1120x840")
        root.minsize(940, 720)
        self.q = queue.Queue()
        self.busy = False
        self.pending = None
        self.loc = None
        self.tz = timezone.utc
        self.trained = None
        self.trained_at = 0.0
        self.forecast_at = 0.0
        self.warnings = []
        self.last = None
        self.f_small = tkfont.Font(family=FONT, size=9)
        self._style()
        self._build()
        self._cpu_prev = (time.time(), time.process_time())
        self.log_pending = []
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_window_icon()
        try:
            self.server = start_command_server(self.q)
        except OSError as e:
            self.server = None
            log(f"No se pudo abrir el canal de órdenes ({e})", "warn")
        try:
            if install_menu_entry():
                log("Clima Pi está en el menú de aplicaciones (con su icono)")
        except OSError as e:
            log(f"No se pudo crear la entrada del menú: {e}", "warn")
        self.start_tray()
        if background:
            if self.tray_proc:
                root.withdraw()
                log("Arranque en segundo plano: la ventana está oculta; ábrela desde el icono de la barra")
            else:
                root.iconify()
                log("Arranque en segundo plano: ventana minimizada en la barra de tareas")
        self._poll()
        self._scheduler()
        self._live_tick()
        loc = load_config()
        if loc:
            self.set_location(loc)
        else:
            self.set_status("Escribe tu ciudad arriba y pulsa Buscar ciudad.", FG)
            if background:
                self.show_window()

    # ---------- segundo plano
    def _set_window_icon(self):
        try:
            p = desktop_paths()["icon"]
            if have_pil() and not os.path.exists(p):
                make_app_icon(p)
            if os.path.exists(p):
                self._icon_img = tk.PhotoImage(file=p).subsample(4, 4)
                self.root.iconphoto(True, self._icon_img)
        except Exception as e:
            log(f"Sin icono de ventana ({e})", "warn")

    def start_tray(self):
        path = script_path()
        if not path:
            log("Icono de la barra desactivado: guarda el programa como archivo y ábrelo desde ahí", "warn")
            return
        if not tray_available():
            log(f"Para tener el icono con el tiempo en la barra instala: sudo apt install {TRAY_PKGS}", "warn")
            return
        try:
            logf = open(os.path.join(DATA_DIR, "icono_barra.log"), "a")
            self.tray_proc = subprocess.Popen([sys.executable, path, "--tray", str(os.getpid())], stdout=logf,
                                              stderr=subprocess.STDOUT, start_new_session=True)
            log(f"Icono de la barra iniciado (proceso {self.tray_proc.pid})")
        except OSError as e:
            log(f"No se pudo iniciar el icono de la barra: {e}", "warn")
            self.tray_proc = None

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.after(150, self.root.focus_force)

    def on_close(self):
        """Cerrar la ventana no detiene Clima Pi: sigue actualizando en segundo plano."""
        if self.tray_proc and self.tray_proc.poll() is None:
            self.root.withdraw()
            log("Ventana oculta: Clima Pi sigue funcionando (ábrelo desde el icono de la barra; "
                "para cerrarlo del todo usa «Salir»)")
        else:
            self.root.iconify()
            log("Ventana minimizada: Clima Pi sigue funcionando en la barra de tareas")

    def quit_app(self):
        log("Saliendo de Clima Pi")
        if self.tray_proc and self.tray_proc.poll() is None:
            self.tray_proc.terminate()
        if self.server:
            try:
                self.server.close()
                os.unlink(sock_path())
            except OSError:
                pass
        self.root.destroy()

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
            if self.autostart_var.get():
                self.set_status("Listo: Clima Pi arrancará solo al encender el Pi (en segundo plano).", PI_C)
                log(f"Arranque automático activado: {desktop_paths()['autostart']}")
            else:
                self.set_status("Arranque automático desactivado.", FG)
                log("Arranque automático desactivado")
        except Exception as e:
            self.autostart_var.set(autostart_enabled())
            self.set_status(f"No se pudo cambiar el arranque automático: {e}", BAD)

    def write_state(self, r):
        """Resumen para el icono de la barra (temperatura y pronóstico de hoy)."""
        c, days, tz = r["current"], r["days"], self.tz

        def day(d):
            if not d:
                return {}
            return {"max": d["max"], "min": d["min"], "code": d["code"], "desc": wmo(d["code"]),
                    "rain_p": d["rain_p"], "rain": d["rain"], "h9": d["h9"], "h15": d["h15"],
                    "h21": d["h21"]}
        state = {"loc": self.loc.get("name"), "temp": c.get("temperature_2m"),
                 "desc": wmo(c.get("weather_code")), "today": day(days[0] if days else None),
                 "tomorrow": day(days[1] if len(days) > 1 else None),
                 "sunrise": hhmm(days[0]["sunrise"], tz) if days else "—",
                 "sunset": hhmm(days[0]["sunset"], tz) if days else "—",
                 "updated": time.time(), "updated_hhmm": datetime.now(tz).strftime("%H:%M")}
        try:
            _save_json(state_path(), state)
        except OSError:
            pass
        t = c.get("temperature_2m")
        self.root.title(f"Clima Pi · {self.loc.get('name', '')} {fmt(t, '{:.0f}', ' °C')}")

    # ---------- estilo y construcción
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                     padding=(14, 6), font=(FONT, 10), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", GRID)], foreground=[("selected", FG)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                     rowheight=22, font=(FONT, 9), borderwidth=0)
        st.configure("Treeview.Heading", background=GRID, foreground=FG, font=(FONT, 9, "bold"),
                     relief="flat")
        st.map("Treeview", background=[("selected", "#2f6f4a")])
        st.map("Treeview.Heading", background=[("active", GRID)])
        st.configure("Vertical.TScrollbar", background=GRID, troughcolor=PANEL, borderwidth=0)

    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=FG,
                         activebackground=GRID, activeforeground=FG, relief="flat",
                         padx=12, pady=3, font=(FONT, 10), highlightthickness=0)

    def _lbl(self, parent, text="", fg=FG, size=10, bold=False, bg=PANEL, **kw):
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=(FONT, size, "bold") if bold else (FONT, size), **kw)

    def _wrap(self, lbl):
        lbl.bind("<Configure>", lambda e: lbl.config(wraplength=max(200, e.width - 10)))

    def _tree(self, parent, cols, height):
        frame = tk.Frame(parent, bg=BG)
        tv = ttk.Treeview(frame, columns=[c for c, _, _ in cols], show="headings", height=height)
        for c, title, width in cols:
            tv.heading(c, text=title)
            tv.column(c, width=width, anchor="center", stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("past", foreground=MUTED)
        tv.tag_configure("night", background="#161a20")
        tv.tag_configure("best", foreground=PI_C)
        return frame, tv

    def _build(self):
        r = self.root
        self.top = tk.Frame(r, bg=BG)
        self.top.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(self.top, text="Clima Pi", bg=BG, fg=FG, font=(FONT, 16, "bold")).pack(side="left")
        self.city_var = tk.StringVar()
        e = tk.Entry(self.top, textvariable=self.city_var, width=24, bg=PANEL, fg=FG,
                     insertbackground=FG, relief="flat", font=(FONT, 11))
        e.pack(side="left", padx=(18, 6), ipady=4)
        e.bind("<Return>", lambda ev: self.search())
        self._btn(self.top, "Buscar ciudad", self.search).pack(side="left")
        self._btn(self.top, "Salir", self.quit_app).pack(side="right", padx=(6, 0))
        self._btn(self.top, "Actualizar ahora",
                  lambda: self.request_update(retrain=False)).pack(side="right")
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(self.top, text="Iniciar con el Pi", variable=self.autostart_var,
                       command=self.toggle_autostart, bg=BG, fg=FG, selectcolor=PANEL,
                       activebackground=BG, activeforeground=FG, font=(FONT, 10),
                       highlightthickness=0).pack(side="right", padx=10)
        self.results = tk.Frame(r, bg=BG)
        self.info = tk.Frame(r, bg=BG)
        self.info.pack(fill="x", padx=12, pady=(2, 6))
        self.loc_lbl = tk.Label(self.info, text="", bg=BG, fg=FG, font=(FONT, 11, "bold"))
        self.loc_lbl.pack(side="left")
        self.status = tk.Label(self.info, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="e")
        self.status.pack(side="right", fill="x", expand=True)

        self.nb = nb = ttk.Notebook(r)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        tk.Label(r, text="Datos: Open-Meteo.com (ERA5 y modelos de ECMWF, NOAA, DWD, Météo-France, "
                         "UK Met Office, ECCC, JMA, KMA, CMA, BOM) · CC BY 4.0",
                 bg=BG, fg=MUTED, font=(FONT, 8)).pack(side="bottom", anchor="e", padx=12, pady=(0, 4))
        self._tab_now(nb)
        self._tab_forecast(nb)
        self._tab_hours(nb)
        self._tab_astro(nb)
        self._tab_precision(nb)
        self._tab_history(nb)
        self._tab_live(nb)

    def _tab_now(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="Ahora")
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x", pady=(8, 6))
        left = tk.Frame(top, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.now_temp = self._lbl(left, "—", size=40, bold=True)
        self.now_temp.pack(anchor="w", padx=14, pady=(10, 0))
        self.now_desc = self._lbl(left, "", size=13)
        self.now_desc.pack(anchor="w", padx=14)
        self.now_feel = self._lbl(left, "", fg=MUTED, size=10)
        self.now_feel.pack(anchor="w", padx=14, pady=(0, 6))
        grid = tk.Frame(left, bg=PANEL)
        grid.pack(fill="x", padx=10, pady=(0, 10))
        self.now_tiles = {}
        names = [("hum", "Humedad"), ("dew", "Punto de rocío"), ("pres", "Presión"),
                 ("wind", "Viento"), ("gust", "Ráfagas"), ("cloud", "Nubes"),
                 ("rainp", "Prob. lluvia (próx. hora)"), ("uv", "Índice UV"), ("vis", "Visibilidad")]
        for i, (k, title) in enumerate(names):
            cell = tk.Frame(grid, bg=GRID)
            cell.grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)
            self._lbl(cell, title, fg=MUTED, size=8, bg=GRID).pack(anchor="w", padx=8, pady=(4, 0))
            v = self._lbl(cell, "—", size=12, bold=True, bg=GRID)
            v.pack(anchor="w", padx=8, pady=(0, 4))
            self.now_tiles[k] = v
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="n")

        right = tk.Frame(top, bg=PANEL, width=380)
        right.pack(side="left", fill="y")
        self._lbl(right, "Sol", fg=MUTED, bold=True).pack(anchor="w", padx=12, pady=(10, 0))
        self.sun_canvas = tk.Canvas(right, bg=PANEL, width=360, height=110, highlightthickness=0)
        self.sun_canvas.pack(padx=10)
        self.sun_lbl = self._lbl(right, "", size=10, justify="left")
        self.sun_lbl.pack(anchor="w", padx=12)
        self._lbl(right, "Luna", fg=MUTED, bold=True).pack(anchor="w", padx=12, pady=(10, 0))
        mrow = tk.Frame(right, bg=PANEL)
        mrow.pack(fill="x", padx=10, pady=(4, 10))
        self.moon_canvas = tk.Canvas(mrow, bg=PANEL, width=86, height=86, highlightthickness=0)
        self.moon_canvas.pack(side="left")
        self.moon_lbl = self._lbl(mrow, "", size=10, justify="left")
        self.moon_lbl.pack(side="left", padx=10, anchor="n")

        self._lbl(f, "Próximas 24 horas", fg=MUTED, bold=True, bg=BG).pack(anchor="w", pady=(4, 2))
        self.strip = tk.Frame(f, bg=BG)
        self.strip.pack(fill="x")
        self.now_chart = Chart(f, 180)
        self.now_chart.c.pack(fill="both", expand=True, pady=(8, 4))

    def _tab_forecast(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="Pronóstico")
        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", pady=(6, 2))
        self.show_models = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Ver cada modelo por separado", variable=self.show_models,
                       command=lambda: self.last and self.draw_forecast_chart(self.last),
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=(FONT, 9), highlightthickness=0).pack(side="left")
        self._lbl(bar, "Pasa el ratón por la gráfica para ver la temperatura de cada hora.",
                  fg=MUTED, size=9, bg=BG).pack(side="right")
        self.fc_chart = Chart(f, 300)
        self.fc_chart.c.pack(fill="both", expand=True)
        self.cards = tk.Frame(f, bg=BG)
        self.cards.pack(fill="x", pady=(8, 0))

        tele = tk.Frame(f, bg=PANEL)
        tele.pack(fill="x", pady=(8, 0))
        row = tk.Frame(tele, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(8, 2))
        self.tele_title = self._lbl(row, "¿Le ganamos a las noticias? Apunta lo que dicen para mañana:",
                                    bold=True)
        self.tele_title.pack(side="left")
        self.tv_max = tk.StringVar()
        self.tv_min = tk.StringVar()
        for label, var in (("Máx", self.tv_max), ("Mín", self.tv_min)):
            self._lbl(row, label, fg=MUTED).pack(side="left", padx=(12, 4))
            tk.Entry(row, textvariable=var, width=5, bg=GRID, fg=FG, insertbackground=FG,
                     relief="flat", font=(FONT, 11), justify="center").pack(side="left", ipady=2)
        self._btn(row, "Guardar", self.save_tv).pack(side="left", padx=10)
        self.tele_lbl = self._lbl(tele, "", fg=MUTED, size=9, justify="left", anchor="w")
        self.tele_lbl.pack(fill="x", padx=10, pady=(0, 8))
        self._wrap(self.tele_lbl)
        self.fc_note = self._lbl(f, "", fg=MUTED, size=9, bg=BG, justify="left", anchor="w")
        self.fc_note.pack(fill="x", pady=(4, 0))
        self._wrap(self.fc_note)

    def _tab_hours(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="Por horas")
        self.day_bar = tk.Frame(f, bg=BG)
        self.day_bar.pack(fill="x", pady=(8, 6))
        self.day_var = tk.StringVar()
        cols = [("hora", "Hora", 52), ("cielo", "Cielo", 150), ("temp", "Temp.", 58),
                ("feel", "Sensac.", 62), ("rango", "Rango", 74), ("pp", "% lluvia", 66),
                ("mm", "Lluvia", 62), ("hum", "Hum.", 54), ("rocio", "Rocío", 52),
                ("viento", "Viento km/h", 90), ("raf", "Ráfagas", 62), ("nubes", "Nubes", 54),
                ("uv", "UV", 36), ("pres", "hPa", 52), ("vis", "Visib.", 58)]
        frame, self.hour_tree = self._tree(f, cols, 22)
        frame.pack(fill="both", expand=True)
        self.hours_note = self._lbl(f, "Gris = horas ya pasadas (dato observado). La temperatura del "
                                       "futuro es la del Modelo Pi; el resto de datos, del mejor modelo "
                                       "para tu zona.", fg=MUTED, size=9, bg=BG, anchor="w", justify="left")
        self.hours_note.pack(fill="x", pady=(4, 0))
        self._wrap(self.hours_note)

    def _tab_astro(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="Sol y Luna")
        self.moons = tk.Canvas(f, bg=PANEL, height=130, highlightthickness=0)
        self.moons.pack(fill="x", pady=(8, 6))
        self.moons.bind("<Configure>", lambda e: self.last and self.draw_moons(self.last))
        cols = [("dia", "Día", 100), ("alba", "Alba civil", 76), ("amanecer", "Amanecer", 76),
                ("atardecer", "Atardecer", 76), ("anochecer", "Anochecer civil", 104),
                ("luz", "Horas de luz", 92), ("uv", "UV máx.", 56), ("salida", "Sale Luna", 76),
                ("puesta", "Se pone Luna", 92), ("fase", "Fase", 130), ("ilum", "Iluminada", 72)]
        frame, self.astro_tree = self._tree(f, cols, 8)
        frame.pack(fill="x")
        self.phase_lbl = self._lbl(f, "", size=10, bg=BG, justify="left", anchor="w")
        self.phase_lbl.pack(fill="x", pady=8)

    def _tab_precision(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="Precisión")
        self.acc_chart = Chart(f, 260)
        self.acc_chart.c.pack(fill="both", expand=True, pady=(8, 6))
        cols = [("pos", "#", 30), ("modelo", "Modelo", 190), ("d1", "1 día", 70), ("d3", "3 días", 70),
                ("d5", "5 días", 70), ("d7", "7 días", 70), ("mx", "Máx. de mañana", 110),
                ("mn", "Mín. de mañana", 110)]
        frame, self.rank_tree = self._tree(f, cols, 9)
        frame.pack(fill="x")
        self.acc_text = self._lbl(f, "", size=10, bg=BG, justify="left", anchor="w")
        self.acc_text.pack(fill="x", pady=(6, 0))
        self._wrap(self.acc_text)

    def _tab_history(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="Clima histórico")
        self.hist_chart = Chart(f, 260)
        self.hist_chart.c.pack(fill="both", expand=True, pady=(8, 0))
        self.hist_text = self._lbl(f, "", size=10, bg=BG, justify="left", anchor="w")
        self.hist_text.pack(fill="x", pady=6)
        self._wrap(self.hist_text)
        self.month_chart = Chart(f, 220)
        self.month_chart.c.pack(fill="both", expand=True)

    def _tab_live(self, nb):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text="En vivo")
        tiles = tk.Frame(f, bg=BG)
        tiles.pack(fill="x", pady=(8, 6))
        self.live_tiles = {}
        for i, (k, title) in enumerate([("cpu", "CPU de Clima Pi"), ("ram", "Memoria"),
                                        ("temp", "Temp. del Pi"), ("min", "API: último minuto"),
                                        ("day", "API: últimas 24 h"), ("net", "Descargado"),
                                        ("next", "Próxima actualización"), ("age", "Modelo entrenado")]):
            cell = tk.Frame(tiles, bg=PANEL)
            cell.grid(row=i // 4, column=i % 4, sticky="nsew", padx=3, pady=2)
            tiles.columnconfigure(i % 4, weight=1, uniform="l")
            self._lbl(cell, title, fg=MUTED, size=8).pack(side="left", padx=(10, 6), pady=6)
            v = self._lbl(cell, "—", size=11, bold=True)
            v.pack(side="left", pady=6)
            self.live_tiles[k] = v

        mid = tk.Frame(f, bg=BG)
        mid.pack(fill="both", expand=True)
        left = tk.Frame(mid, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        head = tk.Frame(left, bg=PANEL)
        head.pack(fill="x", padx=10, pady=(8, 0))
        self._lbl(head, "Cómo calcula el Pi la temperatura de:", fg=MUTED, bold=True).pack(side="left")
        self.explain_when = self._lbl(head, "", bold=True)
        self.explain_when.pack(side="left", padx=6)
        self.explain_scale = tk.Scale(left, from_=1, to=168, orient="horizontal", showvalue=0,
                                      command=lambda v: self.show_explain(), bg="#3f6e52", fg=FG,
                                      troughcolor=GRID, highlightthickness=0, sliderrelief="flat",
                                      bd=0, activebackground=PI_C, length=300, width=14,
                                      sliderlength=34)
        self.explain_scale.pack(fill="x", padx=10, pady=(2, 0))
        self.explain_total = self._lbl(left, "", size=12, bold=True, fg=PI_C, justify="left", anchor="w")
        self.explain_total.pack(fill="x", padx=10)
        cols = [("comp", "Componente", 170), ("dato", "Dato de entrada", 110),
                ("peso", "× Peso", 70), ("aporta", "= Aporta", 80)]
        frame, self.explain_tree = self._tree(left, cols, 10)
        frame.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        self.explain_tree.tag_configure("sum", foreground=PI_C)
        self.explain_tree.tag_configure("corr", foreground=CONS_C)
        self.explain_tree.tag_configure("missing", foreground=WARN)

        right = tk.Frame(mid, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)
        self.weights_chart = Chart(right, 250)
        self.weights_chart.c.pack(fill="both", expand=True)
        self.weights_note = self._lbl(right, "", fg=MUTED, size=9, justify="left", anchor="w")
        self.weights_note.pack(fill="x", padx=10, pady=(0, 8))
        self._wrap(self.weights_note)

        bar = tk.Frame(f, bg=BG)
        bar.pack(fill="x", pady=(8, 2))
        self._lbl(bar, "Registro en vivo de todo lo que hace el programa", fg=MUTED, bold=True,
                  bg=BG).pack(side="left")
        self.log_pause = tk.BooleanVar(value=False)
        self._btn(bar, "Limpiar", self._clear_log).pack(side="right")
        tk.Checkbutton(bar, text="Pausar", variable=self.log_pause, command=self._drain_log,
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=(FONT, 9), highlightthickness=0).pack(side="right", padx=8)
        logf = tk.Frame(f, bg=BG)
        logf.pack(fill="both", expand=True)
        self.log_text = tk.Text(logf, height=11, bg="#0b0e12", fg=FG, relief="flat", wrap="none",
                                font=("DejaVu Sans Mono", 9), state="disabled", padx=6, pady=4,
                                highlightthickness=0)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, col in (("time", MUTED), ("net", COLD), ("train", PI_C), ("calc", FG),
                         ("warn", WARN), ("err", BAD), ("head", "#fde68a")):
            self.log_text.tag_configure(tag, foreground=col)

    # ---------- registro y estadísticas en vivo
    def _drain_log(self):
        items = []
        try:
            while len(items) < 500:
                items.append(LOG_Q.get_nowait())
        except queue.Empty:
            pass
        self.log_pending.extend(items)
        if self.log_pause.get() or not self.log_pending:
            return
        t = self.log_text
        at_end = t.yview()[1] > 0.98
        t.config(state="normal")
        for ts, kind, msg in self.log_pending:
            t.insert("end", datetime.fromtimestamp(ts).strftime("%H:%M:%S  "), "time")
            t.insert("end", msg + "\n", kind)
        self.log_pending = []
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 3000:
            t.delete("1.0", f"{lines - 3000}.0")
        t.config(state="disabled")
        if at_end:
            t.see("end")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _live_tick(self):
        T = self.live_tiles
        now, cpu = time.time(), time.process_time()
        pw, pc = self._cpu_prev
        self._cpu_prev = (now, cpu)
        if now > pw:
            n = threading.active_count()
            T["cpu"].config(text=f"{100 * (cpu - pc) / (now - pw):.0f} % · {n} hilo{'s' if n != 1 else ''}")
        rss = None
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) / 1024
        except OSError:
            pass
        T["ram"].config(text=f"{rss:,.0f} MB" if rss else "n/d")
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as fh:
                tc = int(fh.read()) / 1000
            T["temp"].config(text=f"{tc:.1f} °C", fg=PI_C if tc < 60 else WARN if tc < 75 else BAD)
        except (OSError, ValueError):
            T["temp"].config(text="n/d")
        m, h, d = api_usage()
        T["min"].config(text=f"{m:.0f} / {RATE_PER_MIN}", fg=WARN if m > RATE_PER_MIN * 0.8 else FG)
        T["day"].config(text=f"{d:,.0f} / 10.000", fg=WARN if d > 8000 else FG)
        T["net"].config(text=f"{STATS['bytes'] / 1e6:,.1f} MB · {STATS['calls']} pet.")
        if self.busy:
            T["next"].config(text="trabajando…", fg=PI_C)
        elif self.loc:
            left = max(0, self.forecast_at + FORECAST_EVERY_S - now)
            T["next"].config(text=f"en {int(left // 60):02d}:{int(left % 60):02d}", fg=FG)
        if self.trained:
            age = now - self.trained["trained_at"]
            T["age"].config(text=f"hace {int(age // 3600)} h {int(age % 3600 // 60)} min")
        if self.tray_proc and self.tray_proc.poll() is not None and not self.tray_warned:
            self.tray_warned = True
            tail = ""
            try:
                with open(os.path.join(DATA_DIR, "icono_barra.log")) as fh:
                    tail = fh.read().strip().splitlines()[-1]
            except (OSError, IndexError):
                pass
            log(f"El icono de la barra se cerró (código {self.tray_proc.returncode}). {tail}", "warn")
            if self.root.state() == "withdrawn":
                self.show_window()                 # sin icono no habría forma de volver a abrirla
        self.root.after(1000, self._live_tick)

    # ---------- desglose de una predicción
    def show_explain(self):
        r = self.last
        if not r or not r["pi"]:
            return
        tz = self.tz
        fut = sorted(t for t in r["pi"] if t > r["now"])
        if not fut:
            return
        self.explain_scale.config(to=len(fut))
        t = fut[min(len(fut), int(self.explain_scale.get())) - 1]
        dt = datetime.fromtimestamp(t, tz)
        self.explain_when.config(text=f"{DIAS_L[dt.weekday()]} {dt.day} a las {dt:%H}:00 "
                                      f"(dentro de {round((t - r['now']) / 3600)} h)")
        e = explain_hour(r, self.trained, tz, t)
        tv = self.explain_tree
        tv.delete(*tv.get_children())
        if not e:
            self.explain_total.config(text="Sin datos de los modelos para esa hora.")
            return
        for name, x, w, c, real in e["rows"]:
            tv.insert("", "end", tags=[] if real else ["missing"], values=(
                f"Pronóstico de {name}" if real else f"{name} (sin dato: media)",
                f"{x:.1f} °C", f"{w:+.3f}", f"{c:+.2f} °C"))
        for name, xs, c in e["corr"]:
            if name == "Normal para la fecha":
                dato = f"{xs[0]:.1f} °C"
            elif name == "Hora del día":
                dato = f"{e['hour']:02d}:00"
            elif name == "Época del año":
                dato = f"día {e['doy'] + 1} del año"
            elif name == "Desacuerdo entre modelos":
                dato = f"±{xs[0]:.1f} °C"
            else:
                dato = "media × hora"
            tv.insert("", "end", tags=["corr"], values=(name, dato, "", f"{c:+.2f} °C"))
        if e["lead"]:
            tv.insert("", "end", tags=["corr"], values=("Constante aprendida", "", "", f"{e['const']:+.2f} °C"))
        tv.insert("", "end", tags=["sum"], values=("TOTAL = Modelo Pi", "", "", f"{e['total']:.2f} °C"))
        cons = [x for _, x, _, _, real in e["rows"] if real]
        txt = f"Modelo Pi = {e['total']:.1f} °C"
        if cons:
            txt += f"   ·   media simple de los modelos {sum(cons) / len(cons):.1f} °C"
        self.explain_total.config(text=txt)

        if not e["lead"]:
            self.weights_chart.message("El Modelo Pi aún no está entrenado:\n"
                                       "de momento todos los modelos pesan lo mismo.")
            self.weights_note.config(text="")
            return
        info = self.trained["leads"].get(e["lead"])
        if info:
            mae = info["mae"]
            self.weights_chart.show({
                "kind": "bars", "unit": "", "groups": [n for n, *_ in e["rows"]], "rotate": True,
                "title": f"Peso de cada modelo a {e['lead']} día(s) vista",
                "series": [{"label": "Peso aprendido", "color": PI_C, "values": [w for _, _, w, _, _ in e["rows"]]}]})
            best = min((k for k in mae if k in NAMES and k != "app"), key=mae.get, default=None)
            self.weights_note.config(text=(
                f"Los pesos suman {e['sum_w']:.2f}: el Pi reparte la confianza entre los modelos según lo que "
                f"acertó cada uno en tu zona durante el entrenamiento ({info['n']:,} horas). Un peso negativo "
                f"corrige un error que ese modelo comparte con otros. Las correcciones ajustan sesgos por hora, "
                f"época del año y desacuerdo entre modelos. Mejor modelo solo a este plazo: "
                f"{SHORT.get(best, best)} ({mae.get(best, 0):.2f} °C de error); Pi {mae['pi']:.2f} °C."))

    # ---------- utilidades
    def set_status(self, text, color=MUTED):
        self.status.config(text=text, fg=color)

    def key(self):
        return None if not self.loc else (round(self.loc["latitude"], 3), round(self.loc["longitude"], 3))

    # ---------- búsqueda de ciudad
    def search(self):
        name = self.city_var.get().strip()
        if not name:
            return
        self.set_status(f"Buscando «{name}»…")

        def job():
            try:
                self.q.put(("geo", geocode(name), None))
            except Exception as e:
                self.q.put(("geoerr", str(e), None))
        threading.Thread(target=job, daemon=True).start()

    def show_results(self, results):
        for w in self.results.winfo_children():
            w.destroy()
        if not results:
            self.results.pack_forget()
            self.set_status("No encontré esa ciudad. Prueba con «Ciudad, País».", WARN)
            return
        if len(results) == 1:
            self.choose(results[0])
            return
        tk.Label(self.results, text="¿Cuál?", bg=BG, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=(0, 6))
        for res in results:
            self._btn(self.results, loc_name(res), lambda res=res: self.choose(res)).pack(side="left", padx=2)
        self.results.pack(fill="x", padx=12, pady=(0, 4), after=self.top)
        self.set_status("Elige la ubicación correcta.", FG)

    def choose(self, res):
        for w in self.results.winfo_children():
            w.destroy()
        self.results.pack_forget()
        loc = {k: res.get(k) for k in ("name", "latitude", "longitude", "country", "admin1", "timezone")}
        save_config(loc)
        self.set_location(loc)

    def set_location(self, loc):
        self.loc = loc
        self.tz = get_tz(loc)
        self.trained = None
        self.trained_at = 0.0
        self.forecast_at = 0.0
        self.last = None
        RESOLVED.clear()
        self.loc_lbl.config(text=loc_name(loc))
        for w in list(self.cards.winfo_children()) + list(self.strip.winfo_children()):
            w.destroy()
        first = not os.path.exists(os.path.join(loc_dir(loc), "era5_horario.json"))
        self.fc_chart.message("Preparando datos…" + ("\nLa primera vez descarga ~2 años de pronósticos "
                                                    "de 11 modelos: tarda unos minutos." if first else ""))
        self.acc_chart.message("Esperando al entrenamiento…")
        self.hist_chart.message("El historial desde 1950 se descarga al final.")
        self.month_chart.message("")
        for lbl in (self.acc_text, self.hist_text, self.tele_lbl, self.fc_note, self.phase_lbl):
            lbl.config(text="")
        saved = load_model(loc)                     # lo aprendido en arranques anteriores
        if saved:
            log(f"Modelo Pi cargado del disco: {len(saved['models'])} modelos, entrenado el "
                f"{datetime.fromtimestamp(saved['trained_at'], self.tz):%d/%m %H:%M}", "train")
            self.trained = saved
            self.trained_at = saved["trained_at"]
            self.show_precision(saved)
            when = datetime.fromtimestamp(saved["trained_at"], self.tz)
            self.fc_chart.message(f"Modelo Pi cargado (entrenado el {when:%d/%m a las %H:%M}).\n"
                                  "Descargando el pronóstico actual…")
        self.request_update(retrain=time.time() - self.trained_at > RETRAIN_EVERY_S)

    # ---------- trabajo en segundo plano
    def request_update(self, retrain):
        if not self.loc:
            return
        retrain = retrain or self.trained is None
        if self.busy:
            self.pending = bool(self.pending) or retrain
            return
        self.busy = True
        self.warnings = []
        now = time.time()
        self.forecast_at = now
        if retrain:
            self.trained_at = now
        threading.Thread(target=self._job, args=(dict(self.loc), self.key(), retrain, self.trained,
                                                 self.hist_chart.spec is None),
                         daemon=True).start()

    def _job(self, loc, key, retrain, trained, need_history=False):
        q = self.q
        t_job = time.perf_counter()
        log(f"══ ACTUALIZACIÓN {'con reentrenamiento ' if retrain else ''}· {loc_name(loc)} "
            f"({datetime.now(get_tz(loc)):%d/%m %H:%M})", "head")
        try:
            if not online():
                raise RuntimeError("sin conexión a internet")
            tz = get_tz(loc)
            if retrain:
                try:
                    q.put(("status", "Descargando historial ERA5 por horas…", key))
                    era5 = era5_hourly(loc)
                    prev, app = {}, None
                    allm = MODELS + [APP]
                    for i, (k, name, ids, _) in enumerate(allm, 1):
                        q.put(("status", f"Pronósticos pasados de {name} ({i}/{len(allm)})…", key))
                        try:
                            data = prev_runs(loc, k, ids)
                            if k == "app":
                                app = data
                            else:
                                prev[k] = data
                        except RuntimeError as e:
                            q.put(("warn", f"{name}: {e}", key))
                    q.put(("status", f"Entrenando el Modelo Pi con {len(prev)} modelos…", key))
                    trained = train(era5, prev, app, tz)
                    q.put(("trained", trained, key))
                    try:
                        save_model(loc, trained)
                        log("Modelo Pi guardado en disco (modelo_pi.json): el próximo arranque no "
                            "empezará de cero", "train")
                    except OSError as e:
                        q.put(("warn", f"no se pudo guardar el modelo: {e}", key))
                except Exception as e:
                    trained = None
                    q.put(("train_failed", str(e), key))

            live = {}
            for i, (k, name, ids, _) in enumerate(MODELS, 1):
                q.put(("status", f"Pronóstico actual de {name} ({i}/{len(MODELS)})…", key))
                try:
                    live[k], _ = live_forecast(loc, k, ids)
                except RuntimeError as e:
                    q.put(("warn", f"{name}: {e}", key))
            q.put(("status", "Humedad, viento, UV, sol…", key))
            try:
                extras = extras_forecast(loc)
            except RuntimeError as e:
                extras = None
                q.put(("warn", f"datos extra: {e}", key))
            if not live and not extras:
                raise RuntimeError("no se pudo descargar ningún pronóstico")
            now = time.time()
            res = make_forecast(live, extras, trained, tz, now, loc)
            try:
                res["pi_past"] = log_predictions(loc, now, res["pi"])
                store_observed_days(loc, res["obs"], tz, now)
                res["tele"] = tele_score(loc)
            except Exception:
                res.setdefault("pi_past", {})
            q.put(("forecast", res, key))

            if retrain:
                try:
                    q.put(("status", "Descargando clima diario desde 1950 (solo la 1ª vez tarda)…", key))
                    q.put(("history", climate_history(era5_daily(loc)), key))
                except Exception as e:
                    q.put(("history_err", str(e), key))
            elif need_history:                      # arranque con modelo guardado: usar el disco
                cachedh = _load_json(os.path.join(loc_dir(loc), "era5_diario.json"))
                if cachedh:
                    q.put(("history", climate_history(cachedh), key))
            log(f"══ Actualización terminada en {time.perf_counter() - t_job:.1f} s", "head")
            q.put(("done", None, key))
        except Exception as e:
            log(f"ERROR: {e}", "err")
            q.put(("error", str(e), key))
        finally:
            q.put(("idle", None, key))

    def _poll(self):
        try:
            while True:
                kind, data, key = self.q.get_nowait()
                self._handle(kind, data, key)
        except queue.Empty:
            pass
        self._drain_log()
        self.root.after(200, self._poll)

    def _handle(self, kind, data, key):
        if kind == "cmd":
            if data == "show":
                self.show_window()
            elif data == "update":
                self.request_update(retrain=False)
            elif data == "quit":
                self.quit_app()
            return
        if kind == "geo":
            self.show_results(data)
            return
        if kind == "geoerr":
            self.set_status(f"Error al buscar: {data}", BAD)
            return
        if kind == "idle":
            self.busy = False
            if self.pending is not None:
                r, self.pending = self.pending, None
                self.request_update(r)
            return
        if key != self.key():
            return
        if kind == "status":
            self.set_status(data)
        elif kind == "warn":
            if data not in self.warnings:
                self.warnings.append(data)
        elif kind == "trained":
            self.trained = data
            self.show_precision(data)
            self.show_explain()
        elif kind == "train_failed":
            self.trained_at = time.time() - RETRAIN_EVERY_S + 1800
            self.acc_chart.message(f"No se pudo entrenar: {data}\nLo reintento en 30 minutos.")
            self.warnings.append(f"entrenamiento: {data}")
        elif kind == "history":
            self.show_history(data)
        elif kind == "history_err":
            self.hist_chart.message(f"No se pudo descargar el historial: {data}")
        elif kind == "forecast":
            self.last = data
            self.fail_count = 0
            self.show_all(data)
            self.write_state(data)
            self._status_done()
        elif kind == "done":
            self._status_done()
        elif kind == "error":
            # al encender el Pi la red puede tardar: reintentos rápidos al principio
            self.fail_count += 1
            delay = 60 if self.last is None and self.fail_count <= 6 else 600
            self.forecast_at = time.time() - FORECAST_EVERY_S + delay
            if self.trained is None:
                self.trained_at = time.time() - RETRAIN_EVERY_S + delay
            self.set_status(f"Error: {data} (reintento en {delay // 60} min)", BAD)
            if self.fc_chart.spec is None:
                self.fc_chart.message(f"No hay datos todavía: {data}.\n"
                                      f"¿Está el Pi conectado a internet? Lo reintento en {delay // 60} min.")

    def _status_done(self):
        nxt = datetime.fromtimestamp(self.forecast_at + FORECAST_EVERY_S, self.tz)
        msg = f"Actualizado {datetime.now(self.tz):%H:%M} · próxima {nxt:%H:%M}"
        if self.last:
            msg += f" · {len(self.last['models'])} modelos"
        if self.warnings:
            short = "; ".join(w.split(":")[0] for w in self.warnings)
            self.set_status(msg + f" · sin datos de: {short}", WARN)
        else:
            self.set_status(msg)

    def _scheduler(self):
        if self.loc and not self.busy:
            now = time.time()
            if now - self.trained_at > RETRAIN_EVERY_S:
                self.request_update(retrain=True)
            elif now - self.forecast_at > FORECAST_EVERY_S:
                self.request_update(retrain=False)
        self.root.after(30_000, self._scheduler)

    # ================================================================ pintar
    def show_all(self, r):
        self.show_now(r)
        self.draw_forecast_chart(r)
        self.show_cards(r)
        self.show_tele(r)
        self.show_day_buttons(r)
        self.show_astro(r)
        self.show_explain()

    # ---------- Ahora
    def show_now(self, r):
        tz, c = self.tz, r["current"]
        t = c.get("temperature_2m")
        self.now_temp.config(text=fmt(t, "{:.1f}", " °C"))
        self.now_desc.config(text=wmo(c.get("weather_code")))
        feels = c.get("apparent_temperature")
        when = f" · {hhmm(c.get('time'), tz)}" if c.get("time") else ""
        self.now_feel.config(text=f"Sensación térmica {fmt(feels, '{:.1f}', ' °C')}{when}")
        T = self.now_tiles
        T["hum"].config(text=fmt(c.get("relative_humidity_2m"), suffix=" %"))
        T["dew"].config(text=fmt(c.get("dew_point"), "{:.1f}", " °C"))
        T["pres"].config(text=fmt(c.get("pressure_msl"), suffix=" hPa"))
        T["wind"].config(text=f"{fmt(c.get('wind_speed_10m'), suffix=' km/h')} {rumbo(c.get('wind_direction_10m'))}")
        T["gust"].config(text=fmt(c.get("wind_gusts_10m"), suffix=" km/h"))
        T["cloud"].config(text=fmt(c.get("cloud_cover"), suffix=" %"))
        T["rainp"].config(text=fmt(c.get("rain_p_next"), suffix=" %"))
        uv = c.get("uv_now")
        T["uv"].config(text=fmt(uv, "{:.1f}"), fg=BAD if uv and uv >= 8 else WARN if uv and uv >= 6 else FG)
        vis = c.get("visibility")
        T["vis"].config(text="—" if vis is None else f"{vis / 1000:.0f} km")

        # sol
        today = r["days"][0] if r["days"] else None
        sc = self.sun_canvas
        sc.delete("all")
        if today and today["sunrise"] and today["sunset"]:
            sr, ss = today["sunrise"], today["sunset"]
            w, h = 360, 110
            cx, base, rad = w / 2, h - 16, 140
            sc.create_line(10, base, w - 10, base, fill=GRID)
            sc.create_arc(cx - rad, base - 86, cx + rad, base + 86, start=0, extent=180,
                          style="arc", outline=GRID, width=2)
            frac = (r["now"] - sr) / (ss - sr)
            if 0 <= frac <= 1:
                ang = math.pi * (1 - frac)
                x, y = cx + rad * math.cos(ang), base - 86 * math.sin(ang)
                sc.create_oval(x - 9, y - 9, x + 9, y + 9, fill=WARN, outline="")
            sc.create_text(cx - rad, base + 9, text=hhmm(sr, tz), fill=FG, font=(FONT, 9))
            sc.create_text(cx + rad, base + 9, text=hhmm(ss, tz), fill=FG, font=(FONT, 9))
            light = today.get("daylight")
            self.sun_lbl.config(text=(
                f"Amanecer {hhmm(sr, tz)} · Atardecer {hhmm(ss, tz)}\n"
                f"Alba civil {hhmm(today.get('civil_dawn'), tz)} · "
                f"Anochecer civil {hhmm(today.get('civil_dusk'), tz)}\n"
                f"Horas de luz: {int(light // 3600)} h {int(light % 3600 // 60)} min"
                + ("  (de noche)" if not 0 <= frac <= 1 else "")) if light else "")
        # luna
        mc = self.moon_canvas
        mc.delete("all")
        il, el = moon_phase(r["now"])
        draw_moon(mc, 43, 43, 38, il, el, south=self.loc["latitude"] < 0)
        nxt = {}
        for t_, name in r["phases"]:
            nxt.setdefault(name, t_)

        def dname(ts):
            d = datetime.fromtimestamp(ts, tz)
            return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"
        lines = [f"{phase_name(el)} · {il * 100:.0f} % iluminada"]
        if today:
            lines.append(f"Sale {hhmm(today['moonrise'], tz)} · se pone {hhmm(today['moonset'], tz)}")
        if "Luna llena" in nxt:
            lines.append(f"Próxima luna llena: {dname(nxt['Luna llena'])}")
        if "Luna nueva" in nxt:
            lines.append(f"Próxima luna nueva: {dname(nxt['Luna nueva'])}")
        self.moon_lbl.config(text="\n".join(lines))

        # gráfica de las próximas 48 h
        t0, t1 = r["now"] - 12 * 3600, r["now"] + 48 * 3600
        se = []
        if r["band"]:
            se.append({"label": "Rango de los modelos", "color": BAND_C, "legend_color": "#3f6e52",
                       "band": [(t, lo, hi) for t, (lo, hi) in sorted(r["band"].items()) if t <= t1], "pts": []})
        if r["clim"]:
            se.append({"label": "Normal", "color": CLIM_C, "width": 1.5, "dash": (4, 4),
                       "pts": to_pts(r["clim"], t0, t1)})
        se.append({"label": "Observado", "color": FG, "width": 2, "pts": to_pts(r["obs"], t0, t1)})
        se.append({"label": "Modelo Pi", "color": PI_C, "width": 3, "pts": to_pts(r["pi"], t0, t1)})
        self.now_chart.show({"kind": "line", "series": se, "xkind": "time", "tz": tz,
                             "xrange": (t0, t1), "now": r["now"], "unit": "°",
                             "title": "Próximas 48 horas (°C) · pasa el ratón para ver cada hora"})

        # próximas 24 h
        for w in self.strip.winfo_children():
            w.destroy()
        rows = [x for d in r["hours"].values() for x in d if not x["past"]]
        rows = [x for x in rows if x["t"] <= r["now"] + 24 * 3600][::3][:8]
        for i, x in enumerate(rows):
            cell = tk.Frame(self.strip, bg=PANEL)
            cell.grid(row=0, column=i, sticky="nsew", padx=3)
            self.strip.columnconfigure(i, weight=1, uniform="s")
            self._lbl(cell, datetime.fromtimestamp(x["t"], tz).strftime("%H:%M"),
                      fg=MUTED, size=9, bold=True).pack(pady=(6, 0))
            self._lbl(cell, f"{x['temp']:.0f}°", size=16, bold=True).pack()
            self._lbl(cell, wmo(x.get("weather_code")), fg=MUTED, size=8, wraplength=110).pack()
            pp = x.get("precipitation_probability")
            self._lbl(cell, fmt(pp, suffix=" % lluvia"), fg=COLD if pp and pp >= 40 else MUTED,
                      size=8).pack(pady=(0, 6))

    # ---------- Pronóstico
    def draw_forecast_chart(self, r):
        tz, now = self.tz, r["now"]
        t0, t1 = now - 3 * 86400, now + 7 * 86400
        se = []
        if r["band"]:
            se.append({"label": "Rango de los modelos", "color": BAND_C, "legend_color": "#3f6e52",
                       "band": [(t, lo, hi) for t, (lo, hi) in sorted(r["band"].items())], "pts": []})
        if r["clim"]:
            se.append({"label": "Normal para la fecha", "color": CLIM_C, "width": 1.5,
                       "dash": (4, 4), "pts": to_pts(r["clim"], t0, t1)})
        if self.show_models.get():
            for k in r["models"]:
                se.append({"label": NAMES[k].split(" (")[0], "color": COLORS[k], "width": 1,
                           "pts": to_pts(r["temps"][k], t0, t1), "no_legend": False})
        se.append({"label": "Observado", "color": FG, "width": 2, "pts": to_pts(r["obs"], t0, t1)})
        if r.get("pi_past"):
            se.append({"label": "Pi predijo (1 día antes)", "color": PI_C, "width": 1.5,
                       "dash": (2, 3), "pts": to_pts(r["pi_past"], t0, t1)})
        se.append({"label": "Modelo Pi", "color": PI_C, "width": 3, "pts": to_pts(r["pi"], t0, t1)})
        self.fc_chart.show({"kind": "line", "series": se, "xkind": "time", "tz": tz,
                            "xrange": (t0, t1), "now": now, "unit": "°",
                            "title": "Temperatura (°C) · últimos 3 días y próximos 7"})

    def show_cards(self, r):
        tz = self.tz
        for w in self.cards.winfo_children():
            w.destroy()
        today = datetime.fromtimestamp(r["now"], tz).date()
        for i, d in enumerate(r["days"][:7]):
            card = tk.Frame(self.cards, bg=PANEL, cursor="hand2")
            card.grid(row=0, column=i, sticky="nsew", padx=3)
            self.cards.columnconfigure(i, weight=1, uniform="d")
            title = "Hoy" if d["date"] == today else f"{DIAS[d['date'].weekday()]} {d['date'].day}"
            widgets = [self._lbl(card, title, fg=MUTED, size=9, bold=True)]
            widgets[-1].pack(anchor="w", padx=8, pady=(6, 0))
            row = tk.Frame(card, bg=PANEL)
            row.pack(anchor="w", padx=8)
            widgets += [self._lbl(row, f"{d['max']:.0f}°", fg=WARN, size=15, bold=True),
                        self._lbl(row, f" {d['min']:.0f}°", fg=COLD, size=12)]
            widgets[-2].pack(side="left")
            widgets[-1].pack(side="left", anchor="s", pady=(0, 2))
            info = [(wmo(d["code"]), FG),
                    (f"9h {fmt(d['h9'], suffix='°')} · 15h {fmt(d['h15'], suffix='°')} · 21h {fmt(d['h21'], suffix='°')}", MUTED),
                    (f"lluvia {fmt(d['rain'], '{:.1f}', ' mm')} · {fmt(d['rain_p'], suffix=' %')}",
                     COLD if (d["rain_p"] or 0) >= 50 else MUTED),
                    (f"viento {fmt(d['wind'], suffix='')}–{fmt(d['gust'], suffix=' km/h')}", MUTED)]
            if d["anom"] is not None:
                an = d["anom"]
                info.append((f"{an:+.1f}° vs normal", WARN if an > 1.5 else COLD if an < -1.5 else MUTED))
            info_lbls = []
            for text, col in info:
                lb = self._lbl(card, text, fg=col, size=8, wraplength=120, justify="left")
                lb.pack(anchor="w", padx=8)
                widgets.append(lb)
                info_lbls.append(lb)
            card.bind("<Configure>", lambda e, ls=info_lbls: [l.config(wraplength=max(60, e.width - 16))
                                                              for l in ls], add="+")
            tk.Frame(card, bg=PANEL, height=4).pack()
            for wdg in [card, row] + widgets:
                wdg.bind("<Button-1>", lambda e, dd=d["date"]: self.open_day(dd))

        n = len(r["models"])
        note = (f"Modelo Pi = combinación aprendida de {n} modelos (ECMWF, GFS, ICON y otros) con el "
                f"historial de tu ubicación." if r["trained"] else
                f"El Modelo Pi aún no está entrenado: de momento muestra la media de {n} modelos.")
        past = r.get("pi_past") or {}
        both = [t for t in past if t in r["obs"]]
        if len(both) >= 12:
            err = sum(abs(past[t] - r["obs"][t]) for t in both) / len(both)
            note += f"  Últimas {len(both)} horas: el Pi, prediciendo con 1 día de antelación, falló de media {err:.1f} °C."
        note += "  Pulsa un día para verlo hora a hora."
        self.fc_note.config(text=note)

    def open_day(self, d):
        self.day_var.set(d.isoformat())
        self.fill_hours()
        self.nb.select(2)

    # ---------- Pi contra la tele
    def tomorrow(self):
        return datetime.fromtimestamp(time.time(), self.tz).date() + timedelta(days=1)

    def save_tv(self):
        if not self.last:
            self.set_status("Espera a que cargue el pronóstico.", WARN)
            return
        try:
            mx = float(self.tv_max.get().replace(",", "."))
            mn = float(self.tv_min.get().replace(",", "."))
        except ValueError:
            self.set_status("Escribe la máxima y la mínima como números (ej. 24 y 12).", WARN)
            return
        tm = self.tomorrow()
        day = next((d for d in self.last["days"] if d["date"] == tm), None)
        if not day:
            self.set_status("Aún no tengo pronóstico para mañana.", WARN)
            return
        save_tele(self.loc, tm.isoformat(), mx, mn, day["max"], day["min"])
        self.tv_max.set("")
        self.tv_min.set("")
        self.last["tele"] = tele_score(self.loc)
        self.show_tele(self.last)
        self.set_status(f"Guardado. Pi dice {day['max']:.0f}°/{day['min']:.0f}°, "
                        f"la tele {mx:.0f}°/{mn:.0f}°. Mañana por la noche veremos quién acierta.", FG)

    def show_tele(self, r):
        tm = self.tomorrow()
        self.tele_title.config(text=f"¿Le ganamos a las noticias? Apunta lo que dicen para mañana "
                                    f"({DIAS[tm.weekday()]} {tm.day}):")
        s = r.get("tele") or {}
        if not s or not s.get("n"):
            txt = ("Marcador vacío: apunta la máxima y la mínima que dan las noticias (o la app del "
                   "móvil) para mañana. Cuando pase el día compararé ambas con lo que ocurrió.")
            if s.get("pending"):
                txt += f"  Pendientes de comprobar: {s['pending']} día(s)."
        else:
            txt = (f"Marcador en {s['n']} día(s): Pi {s['pi']} · Noticias {s['tv']} · Empates {s['tie']}.  "
                   f"Error medio de máx./mín.: Pi {s['err_pi']:.1f} °C, noticias {s['err_tv']:.1f} °C.")
            if s.get("pending"):
                txt += f"  Pendientes: {s['pending']}."
        self.tele_lbl.config(text=txt)

    # ---------- Por horas
    def show_day_buttons(self, r):
        for w in self.day_bar.winfo_children():
            w.destroy()
        today = datetime.fromtimestamp(r["now"], self.tz).date()
        valid = [d for d, rows in r["hours"].items() if rows]
        if self.day_var.get() not in valid:
            self.day_var.set(today.isoformat())
        for iso in valid:
            d = date.fromisoformat(iso)
            text = "Hoy" if d == today else f"{DIAS[d.weekday()]} {d.day}"
            tk.Radiobutton(self.day_bar, text=text, value=iso, variable=self.day_var,
                           command=self.fill_hours, indicatoron=0, bg=PANEL, fg=FG,
                           selectcolor="#2f6f4a", activebackground=GRID, activeforeground=FG,
                           relief="flat", offrelief="flat", bd=0, highlightthickness=0,
                           padx=12, pady=4, font=(FONT, 10)).pack(side="left", padx=2)
        self.fill_hours()

    def fill_hours(self):
        tv = self.hour_tree
        tv.delete(*tv.get_children())
        if not self.last:
            return
        for x in self.last["hours"].get(self.day_var.get(), []):
            dt = datetime.fromtimestamp(x["t"], self.tz)
            band = x.get("band")
            tags = []
            if x["past"]:
                tags.append("past")
            if x.get("is_day") == 0:
                tags.append("night")
            vis = x.get("visibility")
            tv.insert("", "end", tags=tags, values=(
                dt.strftime("%H:%M"), wmo(x.get("weather_code")), f"{x['temp']:.1f}°",
                fmt(x.get("feels"), "{:.0f}", "°"),
                f"{band[0]:.0f}–{band[1]:.0f}°" if band else "—",
                fmt(x.get("precipitation_probability"), suffix=" %"),
                fmt(x.get("precipitation"), "{:.1f}", " mm"),
                fmt(x.get("relative_humidity_2m"), suffix=" %"),
                fmt(x.get("dew_point_2m"), "{:.0f}", "°"),
                f"{fmt(x.get('wind_speed_10m'))} {rumbo(x.get('wind_direction_10m'))}",
                fmt(x.get("wind_gusts_10m")),
                fmt(x.get("cloud_cover"), suffix=" %"),
                fmt(x.get("uv_index"), "{:.0f}"),
                fmt(x.get("pressure_msl")),
                "—" if vis is None else f"{vis / 1000:.0f} km"))

    # ---------- Sol y Luna
    def show_astro(self, r):
        tz = self.tz
        tv = self.astro_tree
        tv.delete(*tv.get_children())
        today = datetime.fromtimestamp(r["now"], tz).date()
        for d in r["days"]:
            light = d.get("daylight")
            name = "Hoy" if d["date"] == today else f"{DIAS_L[d['date'].weekday()]} {d['date'].day}"
            tv.insert("", "end", values=(
                name, hhmm(d.get("civil_dawn"), tz), hhmm(d["sunrise"], tz), hhmm(d["sunset"], tz),
                hhmm(d.get("civil_dusk"), tz),
                f"{int(light // 3600)} h {int(light % 3600 // 60):02d} min" if light else "—",
                fmt(d.get("uv"), "{:.0f}"), hhmm(d["moonrise"], tz), hhmm(d["moonset"], tz),
                d["phase"], f"{d['illum'] * 100:.0f} %"))
        lines = ["Próximas fases de la Luna:"]
        for t, name in r["phases"][:6]:
            dt = datetime.fromtimestamp(t, tz)
            lines.append(f"  • {name}: {DIAS_L[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]} a las {dt:%H:%M}")
        lines.append("Alba y anochecer civil: el Sol está a 6° bajo el horizonte; hay luz natural "
                     "suficiente para estar fuera sin linternas.")
        lines.append("Crepúsculo civil y Luna calculados en el propio Pi (precisión ~1 min).")
        self.phase_lbl.config(text="\n".join(lines))
        self.draw_moons(r)

    def draw_moons(self, r):
        c = self.moons
        c.delete("all")
        w = c.winfo_width()
        days = r["days"][:7]
        if w < 100 or not days:
            return
        gw = w / len(days)
        south = self.loc["latitude"] < 0
        today = datetime.fromtimestamp(r["now"], self.tz).date()
        for i, d in enumerate(days):
            cx = gw * (i + 0.5)
            draw_moon(c, cx, 52, 32, d["illum"], d["elong"], south)
            label = "Hoy" if d["date"] == today else f"{DIAS[d['date'].weekday()]} {d['date'].day}"
            c.create_text(cx, 10, text=label, fill=MUTED, font=(FONT, 9, "bold"))
            c.create_text(cx, 100, text=d["phase"], fill=FG, font=(FONT, 9))
            c.create_text(cx, 116, text=f"{d['illum'] * 100:.0f} %", fill=MUTED, font=(FONT, 9))

    # ---------- Precisión
    def show_precision(self, tr):
        leads = sorted(tr["leads"])
        groups = [f"{L} día" if L == 1 else f"{L} días" for L in leads]
        avg = {}
        for L in leads:
            for k, v in tr["leads"][L]["mae"].items():
                avg.setdefault(k, []).append(v)
        avg = {k: sum(v) / len(v) for k, v in avg.items()}
        models = sorted([k for k in avg if k in NAMES and k != "app"], key=avg.get)
        series_ = [("pi", "Modelo Pi", PI_C), ("consenso", "Media de modelos", CONS_C)]
        if "app" in avg:
            series_.append(("app", "App típica", "#94a3b8"))
        series_ += [(k, NAMES[k].split(" (")[0], COLORS[k]) for k in models[:3]]
        series_.append(("clim", "Normal (climatología)", "#4b5563"))
        self.acc_chart.show({"kind": "bars", "groups": groups, "unit": "°",
                             "title": "Error medio en temperatura (°C) · más bajo = mejor",
                             "series": [{"label": lab, "color": col,
                                         "values": [tr["leads"][L]["mae"].get(k) for L in leads]}
                                        for k, lab, col in series_]})

        tv = self.rank_tree
        tv.delete(*tv.get_children())
        ranking = sorted([k for k in avg if k != "clim"], key=avg.get)
        label = {"pi": "★ Modelo Pi", "consenso": "Media de modelos", **NAMES}
        l1 = tr["leads"].get(1, tr["leads"][leads[0]])
        for pos, k in enumerate(ranking, 1):
            vals = [fmt(tr["leads"].get(L, {}).get("mae", {}).get(k), "{:.2f}", "°") for L in (1, 3, 5, 7)]
            tv.insert("", "end", tags=["best"] if k == "pi" else [], values=(
                pos, label.get(k, k), *vals,
                fmt(l1["mae_max"].get(k), "{:.2f}", "°"), fmt(l1["mae_min"].get(k), "{:.2f}", "°")))

        lines = []
        label2 = {"consenso": "la media de modelos", "app": "la app típica",
                  **{k: NAMES[k].split(" (")[0] for k in NAMES}}

        def verdict(pi, other):
            if pi < other - 0.04:
                return f"el Pi gana ({100 * (other - pi) / other:.0f} % menos error)"
            if pi <= other + 0.04:
                return "empate técnico"
            return "aquí el Pi aún pierde"
        for kind, title in (("mae_max", "Máxima"), ("mae_min", "Mínima")):
            d = l1[kind]
            rivals = {k: v for k, v in d.items() if k not in ("pi", "clim")}
            if "pi" in d and rivals:
                best = min(rivals, key=rivals.get)
                txt = (f"{title} de mañana: Modelo Pi {d['pi']:.2f} °C de error medio; mejor rival "
                       f"({label2.get(best, best)}) {rivals[best]:.2f} °C → {verdict(d['pi'], rivals[best])}.")
                if "app" in rivals and best != "app":
                    txt += f" App típica: {rivals['app']:.2f} °C."
                lines.append(txt)
        f = datetime.fromtimestamp(l1["test_from"], self.tz)
        t = datetime.fromtimestamp(l1["test_to"], self.tz)
        lines.append(f"Prueba justa: del {f:%d/%m/%Y} al {t:%d/%m/%Y}, con datos que el Pi no vio al "
                     f"entrenar. Referencia: reanálisis ERA5. Las noticias suelen basarse en estos "
                     f"mismos modelos; para la comparación directa usa el marcador de la pestaña Pronóstico.")
        self.acc_text.config(text="\n".join(lines))

    # ---------- Clima histórico
    def show_history(self, h):
        if not h:
            self.hist_chart.message("No hay suficiente historial para esta ubicación.")
            return
        ys = [a[0] for a in h["annual"]]
        trend = [(y, h["slope"] * y + h["icpt"]) for y in (ys[0], ys[-1])]
        self.hist_chart.show({"kind": "line", "xkind": "year", "unit": "°",
                              "title": "Temperatura media anual (°C)",
                              "series": [{"label": "Media anual", "color": FG, "width": 2, "pts": h["annual"]},
                                         {"label": "Tendencia", "color": BAD, "width": 2,
                                          "dash": (6, 4), "pts": trend}]})
        total = h["slope"] * (ys[-1] - ys[0])
        w, c = h["warmest"], h["coldest"]
        self.hist_text.config(text=(
            f"Tendencia {ys[0]}–{ys[-1]}: {h['slope'] * 10:+.2f} °C por década ({total:+.1f} °C en total). "
            f" Año más cálido: {w[0]} ({w[1]:.1f} °C) · más frío: {c[0]} ({c[1]:.1f} °C)."))
        self.month_chart.show({"kind": "bars", "groups": MESES, "unit": "°",
                               "title": "Temperatura media por mes (°C)",
                               "series": [{"label": "Normal 1991–2020", "color": CLIM_C, "values": h["normal"]},
                                          {"label": f"Últimos 5 años ({h['recent_label']})", "color": WARN,
                                           "values": h["recent"]}]})


def main():
    args = sys.argv[1:]
    if "--tray" in args:                         # proceso del icono de la barra
        i = args.index("--tray")
        parent = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else None
        run_tray(parent)
        return
    background = "--background" in args
    if hasattr(socket, "AF_UNIX"):
        try:
            reply = send_cmd("version")
        except OSError:
            reply = None
        if reply:
            running = reply[3:].strip() if reply.startswith("ok ") else "anterior"
            if running == APP_VERSION:
                if not background:
                    send_cmd("show")
                    print("Clima Pi ya está funcionando; he traído su ventana al frente.")
                return
            # hay otra versión funcionando (p. ej. la que arrancó al encender): cerrarla y seguir
            print(f"Cerrando la versión {running} de Clima Pi para arrancar la {APP_VERSION}…")
            try:
                send_cmd("quit")
            except OSError:
                pass
            for _ in range(40):
                if not instance_running("ping"):
                    break
                time.sleep(0.25)
            time.sleep(1)
    root = tk.Tk(className=APP_ID)
    if np is None:
        root.title("Clima Pi")
        tk.Label(root, text="Falta numpy.\nInstálalo con:\n\nsudo apt install python3-numpy",
                 font=(FONT, 12), padx=30, pady=30).pack()
        root.mainloop()
        return
    App(root, background=background)
    root.mainloop()


if __name__ == "__main__":
    main()
