"""Datos meteorológicos sintéticos con el formato de las respuestas de Open-Meteo.

Sirven para probar Clima Pi sin red: una «realidad» (ERA5) con ciclo anual, ciclo diario y
tiempo cambiante, y 11 modelos que la pronostican con defectos realistas:
  * sesgo propio (uno va siempre 1 °C por encima, otro por debajo…)
  * sesgo día/noche (subestiman la máxima o la mínima) que cambia con la estación
  * error que crece con la antelación, en parte común a todos los modelos
  * huecos, un modelo que empieza tarde y otro que cambia de versión a mitad
"""
from datetime import date, datetime, timezone

import numpy as np

KEYS = ["ecmwf", "aifs", "gfs", "icon", "mf", "ukmo", "gem", "jma", "kma", "cma", "bom"]
LEADS = range(0, 8)


def lead_var(L):
    return "temperature_2m" if L == 0 else f"temperature_2m_previous_day{L}"


def _ar(rng, n, phi, sd):
    """Ruido AR(1) con desviación típica estacionaria `sd`."""
    e = rng.normal(0, sd * np.sqrt(1 - phi * phi), n)
    out = np.empty(n)
    acc = rng.normal(0, sd)
    for i in range(n):
        acc = phi * acc + e[i]
        out[i] = acc
    return out


class Mundo:
    def __init__(self, start=date(2023, 6, 1), days=760, seed=1, tz_hours=1):
        rng = np.random.default_rng(seed)
        t0 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        self.ts = t0 + 3600 * np.arange(days * 24, dtype=np.int64)
        n = len(self.ts)
        lt = self.ts + tz_hours * 3600
        hour = (lt // 3600) % 24
        doy = ((lt // 86400) - (t0 // 86400)) % 365 + 152           # empieza en junio
        season = np.cos(2 * np.pi * (doy - 200) / 365.25)            # +1 en verano
        diurnal = np.cos(2 * np.pi * (hour - 15) / 24)               # +1 a las 15 h
        self.truth = (14 + 9 * season + (5 + 2 * season) * diurnal
                      + _ar(rng, n, 0.996, 3.0) + rng.normal(0, 0.3, n))
        self.hour, self.season, self.diurnal = hour, season, diurnal
        # humedad: baja por la tarde, sube de noche, con rachas húmedas y secas
        self.hum = np.clip(65 - 3 * (5 + 2 * season) * diurnal + _ar(rng, n, 0.99, 12), 12, 100)
        # el sensor del patio no es la casilla del modelo: le da el sol por la tarde, de noche
        # se enfría más, y mide con algo de ruido
        self.sensor_temp = (self.truth + 1.3 * np.maximum(diurnal, 0) - 0.6 * np.maximum(-diurnal, 0)
                            + rng.normal(0, 0.15, n))
        self.fc = {}
        skill = dict(zip(KEYS, [0.55, 0.6, 0.85, 0.7, 0.75, 0.7, 0.9, 0.85, 0.9, 1.0, 1.0]))
        bias = dict(zip(KEYS, [0.3, -0.4, 1.0, -0.6, 0.2, -0.2, 0.8, -1.0, 0.5, 1.4, -0.8]))
        dbias = dict(zip(KEYS, [-0.5, -0.8, -1.2, 0.4, -0.3, 0.6, -1.0, 0.8, -0.6, 1.2, 0.3]))
        for L in LEADS:
            shared = _ar(rng, n, 0.97, 0.35 + 0.35 * L)              # error común a todos
            for k in KEYS:
                own = _ar(rng, n, 0.95, skill[k] * (0.35 + 0.3 * L))
                b = bias[k] + np.zeros(n)
                if k == "gfs":                                       # cambio de versión a mitad
                    b[n // 2:] -= 1.2
                f = (self.truth + b + dbias[k] * (1 + 0.5 * season) * diurnal + shared + own)
                gap = rng.random(n // 24) < 0.04                      # días sueltos sin dato
                f[np.repeat(gap, 24)[:n]] = np.nan
                if k == "bom":                                       # empieza tarde
                    f[: n // 3] = np.nan
                self.fc[(k, L)] = f

    # ---------------------------------------------------------- respuestas tipo Open-Meteo
    @staticmethod
    def _blk(ts, **cols):
        out = {"time": [int(t) for t in ts]}
        for k, v in cols.items():
            out[k] = [None if not np.isfinite(x) else round(float(x), 2) for x in v]
        return out

    def era5(self, upto):
        m = self.ts <= upto
        return {"hourly": self._blk(self.ts[m], temperature_2m=self.truth[m])}

    def prev(self, key, upto):
        m = self.ts <= upto
        return {"hourly": self._blk(self.ts[m], **{lead_var(L): self.fc[(key, L)][m] for L in LEADS})}

    def prev_all(self, upto):
        return {k: self.prev(k, upto) for k in KEYS}

    def app(self, upto):
        """La «app típica» (best match): la media de tres modelos."""
        m = self.ts <= upto
        cols = {lead_var(L): np.nanmean([self.fc[(k, L)][m] for k in ("ecmwf", "icon", "gfs")], axis=0)
                for L in LEADS}
        return {"hourly": self._blk(self.ts[m], **cols)}

    def live(self, now, keys=KEYS):
        """Pronóstico «actual» emitido en `now`: a x días vista, mezcla de las antelaciones."""
        out = {}
        sel = (self.ts > now - 86400) & (self.ts <= now + 8 * 86400)
        ts = self.ts[sel]
        x = np.clip((ts - now) / 86400.0, 0, 7)
        lo = np.floor(x).astype(int)
        hi = np.minimum(lo + 1, 7)
        w = x - lo
        for k in keys:
            F = np.vstack([self.fc[(k, L)][sel] for L in LEADS])
            v = (1 - w) * F[lo, np.arange(len(ts))] + w * F[hi, np.arange(len(ts))]
            out[k] = {"hourly": self._blk(ts, temperature_2m=v)}
        return out

    def extras(self, now):
        """Datos «best match» de la API de pronóstico (con 7 días pasados)."""
        sel = (self.ts > now - 7 * 86400) & (self.ts <= now + 8 * 86400)
        ts = self.ts[sel]
        t = self.truth[sel] + 0.3
        n = len(ts)
        hourly = self._blk(ts, temperature_2m=t, apparent_temperature=t - 1.5,
                           relative_humidity_2m=np.full(n, 60.0), dew_point_2m=t - 8,
                           precipitation_probability=np.full(n, 20.0), precipitation=np.zeros(n),
                           weather_code=np.full(n, 2.0), cloud_cover=np.full(n, 40.0),
                           pressure_msl=np.full(n, 1015.0), wind_speed_10m=np.full(n, 10.0),
                           wind_direction_10m=np.full(n, 270.0), wind_gusts_10m=np.full(n, 20.0),
                           uv_index=np.full(n, 3.0), visibility=np.full(n, 20000.0),
                           is_day=np.ones(n))
        return {"hourly": hourly, "current": {"time": int(now), "temperature_2m": float(t[0])}}

    def sensor_rows(self, start, stop, every=300, seed=7):
        """Lecturas del DHT22 cada `every` s entre start y stop (con huecos y algún valor absurdo)."""
        rng = np.random.default_rng(seed)
        out = []
        for ts in range(int(start), int(stop), every):
            i = (ts - int(self.ts[0])) / 3600.0
            if not 0 <= i < len(self.ts) - 1:
                continue
            j = int(i)
            f = i - j
            t = (1 - f) * self.sensor_temp[j] + f * self.sensor_temp[j + 1] + rng.normal(0, 0.1)
            h = (1 - f) * self.hum[j] + f * self.hum[j + 1]
            if rng.random() < 0.03:                   # lectura perdida
                continue
            out.append((ts, float(t), float(h)))
        return out
