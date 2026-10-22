"""Pruebas de confiabilidad de Clima Pi (sin red, sin ventana y sin sensor de verdad).

    python3 -m unittest discover -s tests -v

Usan datos sintéticos con el formato de Open-Meteo (tests/sintetico.py) y un DHT22 simulado
con archivos (como los que crea Linux en /sys)."""
import contextlib
import importlib.util
import io
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("clima_pi", os.path.join(os.path.dirname(HERE), "clima_pi.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)
from sintetico import AI_KEYS, KEYS, Mundo  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    MADRID = ZoneInfo("Europe/Madrid")
except Exception:                              # sin base de zonas horarias
    MADRID = None

D = 86400
LOC = {"latitude": 40.4168, "longitude": -3.7038, "timezone": "Europe/Madrid", "name": "Madrid"}


def drain():
    while not cp.LOG_Q.empty():
        cp.LOG_Q.get_nowait()


class TempData(unittest.TestCase):
    """Cada prueba con su propia carpeta ~/.clima_pi."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._old = cp.DATA_DIR, cp.IIO_BASE
        cp.DATA_DIR = self.dir
        cp.IIO_BASE = os.path.join(self.dir, "sin_iio")

    def tearDown(self):
        cp.DATA_DIR, cp.IIO_BASE = self._old
        shutil.rmtree(self.dir, ignore_errors=True)
        drain()


# =========================================================================== precisión
@unittest.skipIf(MADRID is None, "sin zonas horarias")
class TestPrecision(TempData):
    """El Modelo Pi tiene que mejorar a los modelos, y hacerlo sin hacer trampas."""

    @classmethod
    def setUpClass(cls):
        cls.W = Mundo(days=760, seed=3)
        cls.upto = int(cls.W.ts[0] + 700 * D)
        cls.tr = cp.train(cls.W.era5(cls.upto), cls.W.prev_all(cls.upto), cls.W.app(cls.upto), MADRID)
        drain()

    def test_entrena_todas_las_antelaciones_incluida_la_de_hoy(self):
        self.assertEqual(sorted(self.tr["leads"]), list(range(8)))

    def test_pi_mejora_la_media_y_al_mejor_modelo(self):
        for L, info in self.tr["leads"].items():
            m = info["mae"]
            best = min(m[k] for k in KEYS if k in m)
            # el mismo día los modelos ya aciertan mucho: ahí el Pi empata con la media (±10 %)
            self.assertLess(m["pi"], m["consenso"] * (1.1 if L == 0 else 1.0), f"antelación {L}")
            self.assertLess(m["pi"], best * (1.1 if L == 0 else 1.02), f"antelación {L}")
            self.assertLess(m["pi"], m["clim"], f"antelación {L}")

    def test_error_crece_con_la_antelacion(self):
        e = [self.tr["leads"][L]["mae"]["pi"] for L in range(8)]
        self.assertLess(e[0], e[1])
        self.assertLess(e[1], e[4])
        self.assertLess(e[4], e[7] * 1.1)

    def test_prueba_justa_sin_mirar_el_futuro(self):
        info = self.tr["leads"][1]
        self.assertGreater(info["n_test"], 24 * 60)
        self.assertLess(info["test_from"], info["test_to"])
        self.assertLessEqual(info["test_to"], self.upto)
        self.assertLess(info["q10"], 0)
        self.assertGreater(info["q90"], 0)

    def test_maxima_y_minima_diarias(self):
        info = self.tr["leads"][1]
        self.assertIn("max", info["daily"])
        self.assertIn("min", info["daily"])
        for kind in ("mae_max", "mae_min"):
            best = min(v for k, v in info[kind].items() if k in KEYS)
            self.assertLess(info[kind]["pi"], best * 1.05, kind)

    def test_mezcla_de_antelaciones(self):
        lo, hi, w = cp.lead_mix([0, 0.25, 1.0, 1.5, 6.9, 9.0, -1], range(8))
        self.assertEqual(lo.tolist(), [0, 0, 1, 1, 6, 7, 0])
        self.assertEqual(hi.tolist(), [0, 1, 1, 2, 7, 7, 0])
        np.testing.assert_allclose(w, [0, 0.25, 0, 0.5, 0.9, 0, 0], atol=1e-9)
        lo, hi, w = cp.lead_mix([2.5], [1, 4])                 # faltan antelaciones
        self.assertEqual((lo[0], hi[0]), (1, 4))
        self.assertAlmostEqual(w[0], 0.5)

    def test_prediccion_a_medio_camino_es_la_media_de_las_dos(self):
        now = self.upto + 3 * D
        live = self.W.live(now)
        temps = {k: cp.series(r, "hourly", "temperature_2m") for k, r in live.items()}
        t = np.array([now + int(1.5 * D)], dtype=np.int64)
        p = cp.predict_pi(self.tr, temps, t, now, MADRID)[0]
        doy, hour, _ = cp.local_parts(t, MADRID)
        p1 = cp.predict_lead(self.tr["leads"][1], temps, t, doy, hour, self.tr["clim"])[0][0]
        p2 = cp.predict_lead(self.tr["leads"][2], temps, t, doy, hour, self.tr["clim"])[0][0]
        self.assertAlmostEqual(p, (p1 + p2) / 2, places=6)

    def test_pronostico_en_vivo_mejor_que_la_media_de_modelos(self):
        truth = dict(zip(self.W.ts.tolist(), self.W.truth.tolist()))
        e_pi, e_cons = [], []
        for now in range(self.upto + 3600 * 7, self.upto + 40 * D, 2 * D):
            live = self.W.live(now)
            r = cp.make_forecast(live, self.W.extras(now), self.tr, MADRID, now, LOC)
            for t, v in r["pi"].items():
                vals = [r["temps"][k][t] for k in r["models"] if t in r["temps"][k]]
                e_pi.append(abs(v - truth[t]))
                e_cons.append(abs(sum(vals) / len(vals) - truth[t]))
            drain()
        self.assertLess(np.mean(e_pi), np.mean(e_cons) * 0.97)

    def test_desglose_suma_exactamente_la_prediccion(self):
        now = self.upto + 5 * D + 1234
        live = self.W.live(now)
        live["cma"]["hourly"]["temperature_2m"][40:80] = [None] * 40      # huecos de un modelo
        r = cp.make_forecast(live, self.W.extras(now), self.tr, MADRID, now, LOC)
        n = 0
        for t, v in sorted(r["pi"].items()):
            e = cp.explain_hour(r, self.tr, MADRID, t)
            if e is None:
                continue
            self.assertAlmostEqual(e["total"], v, places=6, msg=f"hora {t}")
            n += 1
        self.assertGreater(n, 150)

    def test_guardar_y_cargar_da_lo_mismo(self):
        cp.save_model(LOC, self.tr)
        tr2 = cp.load_model(LOC)
        self.assertIsNotNone(tr2)
        now = self.upto + 2 * D
        a = cp.make_forecast(self.W.live(now), self.W.extras(now), self.tr, MADRID, now, LOC)
        b = cp.make_forecast(self.W.live(now), self.W.extras(now), tr2, MADRID, now, LOC)
        for t in a["pi"]:
            self.assertAlmostEqual(a["pi"][t], b["pi"][t], places=2)

    def test_modelo_de_version_anterior_se_ignora(self):
        cp.save_model(LOC, self.tr)
        path = os.path.join(cp.loc_dir(LOC), "modelo_pi.json")
        with open(path) as f:
            d = json.load(f)
        d["version"] = 2
        with open(path, "w") as f:
            json.dump(d, f)
        self.assertIsNone(cp.load_model(LOC))
        with open(path, "w") as f:
            f.write("{roto")
        self.assertIsNone(cp.load_model(LOC))

    def test_pronostico_completo(self):
        now = self.upto + 10 * D + 600
        r = cp.make_forecast(self.W.live(now), self.W.extras(now), self.tr, MADRID, now, LOC)
        self.assertEqual(len(r["days"]), 7)
        self.assertGreaterEqual(len(r["pi"]), 166)
        self.assertTrue(all(math.isfinite(v) for v in r["pi"].values()))
        for d in r["days"]:
            self.assertLessEqual(d["min"], d["max"])
            self.assertIsNotNone(d["sunrise"])
            self.assertIsNotNone(d["sunset"])
        self.assertEqual(r["pi_raw"], r["pi"])                 # sin sensor no se corrige
        self.assertFalse(r["corrected"])

    def test_sin_modelo_entrenado_ni_datos_extra(self):
        now = self.upto + D
        r = cp.make_forecast(self.W.live(now), None, None, MADRID, now, LOC)
        self.assertGreater(len(r["pi"]), 100)
        r = cp.make_forecast({}, self.W.extras(now), None, MADRID, now, LOC)
        self.assertGreater(len(r["pi"]), 100)
        with self.assertRaises(RuntimeError):
            cp.make_forecast({}, None, None, MADRID, now, LOC)

    def test_un_modelo_que_falta_entero_no_rompe_nada(self):
        now = self.upto + D
        keys = [k for k in KEYS if k not in ("ecmwf", "gfs", "icon")]
        r = cp.make_forecast(self.W.live(now, keys), self.W.extras(now), self.tr, MADRID, now, LOC)
        self.assertGreater(len(r["pi"]), 160)

    def test_backtest_sin_red(self):
        d = cp.loc_dir(LOC)
        cp.save_config(LOC)
        cp._save_json(os.path.join(d, "era5_horario.json"), self.W.era5(self.upto))
        for k, _, ids, _ in cp.MODELS:
            if k in KEYS:
                cp._save_json(os.path.join(d, f"pasados_{k}_{ids[0]}.json"), self.W.prev(k, self.upto))
        out = []
        tr = cp.backtest(out=out.append)
        self.assertEqual(sorted(tr["leads"]), list(range(8)))
        self.assertTrue(any("Prueba de precisión" in x for x in out))


class TestRelleno(unittest.TestCase):
    """Modelos que solo dan un dato cada 6 h (los de IA de NOAA)."""

    def test_rellena_con_el_ciclo_de_los_demas(self):
        ts = 3600 * np.arange(48, dtype=np.int64)
        others = 10 + 5 * np.sin(2 * np.pi * np.arange(48) / 24)
        ia = others + 1.0                                     # va siempre 1 °C por encima
        ia[np.arange(48) % 6 != 0] = np.nan
        M = np.column_stack([others, others + 0.2, ia])
        F = cp.fill_sparse(M, ts)
        np.testing.assert_allclose(F[:43, 2], (others + 0.1)[:43] + 0.9, atol=1e-9)
        self.assertTrue(np.isnan(F[43:, 2]).all())            # después del último dato: no inventa
        np.testing.assert_array_equal(F[:, :2], M[:, :2])     # a los demás no los toca

    def test_huecos_largos_y_datos_completos(self):
        ts = 3600 * np.arange(30, dtype=np.int64)
        M = np.column_stack([np.arange(30.0), np.arange(30.0) + 1])
        M[5:20, 1] = np.nan                                   # 15 h sin datos: no se rellena
        self.assertTrue(np.isnan(cp.fill_sparse(M, ts)[5:20, 1]).all())
        full = np.column_stack([np.arange(30.0), np.arange(30.0)])
        np.testing.assert_array_equal(cp.fill_sparse(full, ts), full)

    def test_dias_con_datos(self):
        self.assertEqual(cp.data_days({0: 1, 6 * 3600: 1, 86400: 2}), 2)

    def test_estan_en_la_lista(self):
        ids = {k: ids for k, _, ids, _ in cp.MODELS}
        self.assertEqual(ids["aigfs"], ["ncep_aigfs025"])
        self.assertEqual(ids["hgefs"], ["ncep_hgefs025_ensemble_mean"])
        self.assertEqual(len(cp.MODELS), 13)


@unittest.skipIf(MADRID is None, "sin zonas horarias")
class TestModelosIA(TempData):
    """Si los modelos de IA aciertan algo que los demás fallan a la vez, el Pi lo aprovecha."""

    @classmethod
    def setUpClass(cls):
        cls.W = Mundo(days=760, seed=3, ai=True, ai_fix=0.35)
        cls.upto = int(cls.W.ts[0] + 700 * D)
        prev = cls.W.prev_all(cls.upto)
        args = (cls.W.era5(cls.upto),)
        cls.con = cp.train(*args, prev, cls.W.app(cls.upto), MADRID)
        cls.sin = cp.train(*args, {k: v for k, v in prev.items() if k in KEYS}, cls.W.app(cls.upto), MADRID)
        with mock.patch.object(cp, "fill_sparse", lambda M, ts, **k: np.array(M, float)):
            cls.relleno_viejo = cp.train(*args, prev, cls.W.app(cls.upto), MADRID)
        drain()

    def test_se_usan_aunque_tengan_poco_historial(self):
        for L in (1, 4, 7):
            for k in AI_KEYS:
                self.assertIn(k, self.con["leads"][L]["use"])

    def test_mejoran_el_pronostico(self):
        for L in (3, 5, 7):
            self.assertLess(self.con["leads"][L]["mae"]["pi"], self.sin["leads"][L]["mae"]["pi"] * 0.97, L)

    def test_el_relleno_de_6_horas_importa(self):
        e = lambda tr: np.mean([tr["leads"][L]["mae"]["pi"] for L in range(1, 8)])   # noqa: E731
        self.assertLess(e(self.con), e(self.relleno_viejo) * 0.97)

    def test_desglose_exacto_con_modelos_de_6_horas(self):
        now = self.upto + 3 * D + 1800
        r = cp.make_forecast(self.W.live(now), self.W.extras(now), self.con, MADRID, now, LOC)
        self.assertIn("aigfs", r["models"])
        n = 0
        for t, v in sorted(r["pi"].items())[:120]:
            e = cp.explain_hour(r, self.con, MADRID, t)
            if e:
                self.assertAlmostEqual(e["total"], v, places=6)
                n += 1
        self.assertGreater(n, 100)
        drain()


class TestRidge(unittest.TestCase):
    def test_igual_que_minimos_cuadrados(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(500, 4)) * [1, 10, 0.1, 3] + [5, -2, 0, 1]
        y = X @ [0.5, -0.2, 3, 1] + 7 + rng.normal(0, 0.01, 500)
        r = cp.Ridge().fit(X, y, lam=1e-8)
        ref = np.linalg.lstsq(np.column_stack([np.ones(500), X]), y, rcond=None)[0]
        np.testing.assert_allclose(r.predict(X), np.column_stack([np.ones(500), X]) @ ref, atol=1e-6)
        const, b = r.raw()
        np.testing.assert_allclose(const + X @ b, r.predict(X), atol=1e-9)

    def test_columna_constante_y_repetida(self):
        X = np.column_stack([np.ones(50), np.arange(50), np.arange(50)])
        r = cp.Ridge().fit(X, np.arange(50) * 2.0)
        self.assertTrue(np.isfinite(r.predict(X)).all())

    def test_relleno_de_huecos(self):
        M = np.array([[1.0, np.nan, 3.0], [np.nan, np.nan, np.nan]])
        np.testing.assert_allclose(cp.impute(M)[0], [1, 2, 3])
        self.assertTrue(np.isnan(cp.impute(M)[1]).all())


@unittest.skipIf(MADRID is None, "sin zonas horarias")
class TestHoraLocal(unittest.TestCase):
    def test_hora_local_exacta_todo_el_anio_con_cambios_de_hora(self):
        t0 = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        ts = t0 + 3600 * np.arange(366 * 24)
        doy, hour, dayno = cp.local_parts(ts, MADRID)
        ref = [datetime.fromtimestamp(int(t), MADRID) for t in ts]
        self.assertEqual(hour.tolist(), [d.hour for d in ref])
        self.assertEqual(doy.tolist(), [d.timetuple().tm_yday - 1 for d in ref])
        self.assertEqual(len(set(dayno.tolist())), 367)   # 1 ene 00:00 UTC ya es día 1 en Madrid

    def test_dia_del_cambio_de_hora(self):
        t0 = int(datetime(2024, 3, 31, 0, tzinfo=timezone.utc).timestamp())
        _, hour, _ = cp.local_parts(t0 + 3600 * np.arange(4), MADRID)
        self.assertEqual(hour.tolist(), [1, 3, 4, 5])      # a las 2 pasan a ser las 3


# =========================================================================== Sol y Luna
@unittest.skipIf(MADRID is None, "sin zonas horarias")
class TestAstronomia(unittest.TestCase):
    """Comparado con las horas publicadas (timeanddate.com / USNO)."""

    def check(self, lat, lon, d, tzname, sunrise, sunset, tol_min=3):
        tz = ZoneInfo(tzname)
        sr, ss = cp.sun_rise_set(cp.midnight(d, tz), lat, lon)
        for got, want in ((sr, sunrise), (ss, sunset)):
            h, m = map(int, want.split(":"))
            exp = datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()
            self.assertLess(abs(got - exp), tol_min * 60, f"{d} {want}")

    def test_salida_y_puesta_del_sol(self):
        self.check(40.4168, -3.7038, date(2024, 6, 21), "Europe/Madrid", "06:44", "21:48")
        self.check(51.5074, -0.1278, date(2024, 12, 21), "Europe/London", "08:04", "15:53")
        self.check(-33.8688, 151.2093, date(2024, 1, 1), "Australia/Sydney", "05:47", "20:09")

    def test_sol_de_medianoche_y_noche_polar(self):
        tz = ZoneInfo("Europe/Oslo")
        self.assertEqual(cp.sun_rise_set(cp.midnight(date(2024, 6, 21), tz), 69.65, 18.96), (None, None))
        self.assertEqual(cp.sun_rise_set(cp.midnight(date(2024, 12, 21), tz), 69.65, 18.96), (None, None))
        dawn, dusk = cp.civil_twilight(cp.midnight(date(2024, 12, 21), tz), 69.65, 18.96)
        self.assertIsNotNone(dawn)                       # hay algo de luz a mediodía
        self.assertIsNotNone(dusk)

    def test_fases_de_la_luna(self):
        pub = [("2024-01-11 11:57", "Luna nueva"), ("2024-01-18 03:53", "Cuarto creciente"),
               ("2024-01-25 17:54", "Luna llena"), ("2024-02-02 23:18", "Cuarto menguante"),
               ("2024-03-10 09:00", "Luna nueva"), ("2024-04-08 18:21", "Luna nueva"),
               ("2024-04-23 23:49", "Luna llena")]
        got = cp.next_phases(datetime(2024, 1, 5, tzinfo=timezone.utc).timestamp(), 110)
        for when, name in pub:
            t = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
            near = min((g for g in got if g[1] == name), key=lambda g: abs(g[0] - t))
            self.assertLess(abs(near[0] - t), 20 * 60, f"{name} {when}")

    def test_salida_de_la_luna_es_cuando_cruza_el_horizonte(self):
        t0 = cp.midnight(date(2024, 6, 21), MADRID)
        for t in cp.moon_rise_set(t0, 40.4, -3.7):
            if t is not None:
                self.assertAlmostEqual(cp.moon_alt(t, 40.4, -3.7), 0.125, delta=0.05)

    def test_iluminacion(self):
        full = datetime(2024, 1, 25, 17, 54, tzinfo=timezone.utc).timestamp()
        self.assertGreater(cp.moon_phase(full)[0], 0.99)
        self.assertLess(cp.moon_phase(full - 14.77 * D)[0], 0.01)


# =========================================================================== red y disco
class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, reason="x"):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(json.dumps({"reason": reason}).encode()))


class TestRed(unittest.TestCase):
    URL = "https://api.open-meteo.com/v1/forecast"
    P = {"latitude": "1", "longitude": "2", "hourly": "temperature_2m", "forecast_days": 7}

    def run_seq(self, seq):
        calls = []

        def fake(req, timeout=0):
            calls.append(req)
            x = seq[len(calls) - 1]
            if isinstance(x, BaseException):
                raise x
            return FakeResp(x)
        with mock.patch.object(cp.urllib.request, "urlopen", fake), \
                mock.patch.object(cp.time, "sleep") as sl:
            try:
                return cp.get_json(self.URL, dict(self.P)), len(calls), sl
            finally:
                drain()

    def test_bien_a_la_primera(self):
        r, n, _ = self.run_seq([b'{"ok": 1}'])
        self.assertEqual((r, n), ({"ok": 1}, 1))

    def test_reintenta_errores_del_servidor(self):
        r, n, _ = self.run_seq([http_error(503), http_error(502), b'{"ok": 2}'])
        self.assertEqual((r, n), ({"ok": 2}, 3))

    def test_limite_429_espera_y_reintenta(self):
        r, n, sl = self.run_seq([http_error(429), b'{"ok": 3}'])
        self.assertEqual(r, {"ok": 3})
        sl.assert_called_with(65)

    def test_respuesta_cortada_o_corrupta_se_reintenta(self):
        r, n, _ = self.run_seq([b'{"hourly": {"time": [1, 2', b'{"ok": 4}'])
        self.assertEqual((r, n), ({"ok": 4}, 2))

    def test_sin_red_da_error_claro(self):
        with self.assertRaisesRegex(RuntimeError, "sin conexión"):
            self.run_seq([urllib.error.URLError("caído")] * 3)

    def test_peticion_mala_no_se_reintenta(self):
        with self.assertRaisesRegex(RuntimeError, "400: Parámetro"):
            self.run_seq([http_error(400, "Parámetro malo")])

    def test_peso_de_peticiones(self):
        self.assertEqual(cp.est_weight({"hourly": "a", "forecast_days": 7}), 1.0)
        w = cp.est_weight({"hourly": ",".join(cp.lead_var(L) for L in cp.LEADS),
                           "start_date": "2024-01-01", "end_date": "2025-02-03"})
        self.assertAlmostEqual(w, 400 / 14)
        self.assertLess(w * 12, cp.RATE_PER_MIN)          # 12 modelos caben en un minuto

    def test_limitador_espera_cuando_toca(self):
        th = cp.Throttle(10)
        clock = [1000.0]
        with mock.patch.object(cp.time, "time", lambda: clock[0]), \
                mock.patch.object(cp.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            th.wait(6)
            th.wait(4)
            self.assertEqual(clock[0], 1000.0)               # aún cabe
            th.wait(5)
            self.assertGreater(clock[0], 1060.0)             # tuvo que esperar al minuto siguiente
        drain()

    def test_series(self):
        resp = {"hourly": {"time": [1, 2, 3], "temperature_2m_gfs": [1.5, None, 2]}}
        self.assertEqual(cp.series(resp, "hourly", "temperature_2m", "gfs"), {1: 1.5, 3: 2.0})
        self.assertEqual(cp.series(None, "hourly", "x"), {})


class TestHistorial(TempData):
    def test_unir_trozos(self):
        a = {"time": [1, 2, 3], "t": [1.0, None, 3.0]}
        b = {"time": [3, 4], "t": [None, 4.0], "u": [9.0, 9.0]}
        m = cp.merge_section(a, b)
        self.assertEqual(m["time"], [1, 2, 3, 4])
        self.assertEqual(m["t"], [1.0, None, 3.0, 4.0])     # un hueco nuevo no borra lo que había
        self.assertEqual(m["u"], [None, None, 9.0, 9.0])

    def fetcher(self, fail_after=None, extra=()):
        calls = []

        def fetch(a, b):
            calls.append((a, b))
            if fail_after is not None and len(calls) > fail_after:
                raise RuntimeError("se cortó")
            t0 = int(datetime(a.year, a.month, a.day, tzinfo=timezone.utc).timestamp())
            n = (b - a).days + 1
            ts = [t0 + 86400 * i for i in range(n)]
            return {"daily": {"time": ts, "v": [1.0] * n, **{k: [2.0] * n for k in extra}}}
        return fetch, calls

    def test_descarga_por_trozos_y_luego_solo_lo_nuevo(self):
        path = os.path.join(self.dir, "h.json")
        fetch, calls = self.fetcher()
        start, end = date(2020, 1, 1), date(2020, 12, 31)
        cp.update_history(path, "daily", start, end, fetch, 100, 3600)
        self.assertEqual(len(calls), 4)
        cp.update_history(path, "daily", start, end, fetch, 100, 3600)
        self.assertEqual(len(calls), 4)                    # ya estaba al día
        os.utime(path, (0, 0))
        cp.update_history(path, "daily", start, end + timedelta(days=10), fetch, 100, 3600)
        self.assertEqual(calls[-1][0], end - timedelta(days=3))   # solo lo nuevo (+3 días de margen)
        drain()

    def test_descarga_cortada_a_medias_se_continua(self):
        path = os.path.join(self.dir, "h.json")
        start, end = date(2020, 1, 1), date(2020, 12, 31)
        fetch, _ = self.fetcher(fail_after=1)
        with self.assertRaises(RuntimeError):
            cp.update_history(path, "daily", start, end, fetch, 100, 3600)
        fetch, calls = self.fetcher()
        data = cp.update_history(path, "daily", start, end, fetch, 100, 3600)   # el archivo es reciente
        self.assertTrue(calls, "una descarga a medias no puede darse por buena")
        self.assertEqual(len(data["daily"]["time"]), 366)
        drain()

    def test_archivo_de_version_anterior_se_descarga_de_nuevo(self):
        path = os.path.join(self.dir, "h.json")
        start, end = date(2020, 1, 1), date(2020, 3, 1)
        fetch, _ = self.fetcher()
        cp.update_history(path, "daily", start, end, fetch, 100, 3600)
        fetch, calls = self.fetcher(extra=("w",))
        data = cp.update_history(path, "daily", start, end, fetch, 100, 3600, required=("v", "w"))
        self.assertEqual(calls[0][0], start)
        self.assertIn("w", data["daily"])
        drain()

    def test_guardado_atomico(self):
        path = os.path.join(self.dir, "x.json")
        cp._save_json(path, {"a": 1})
        with mock.patch.object(cp.json, "dump", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                cp._save_json(path, {"a": 2})
        self.assertEqual(cp._load_json(path), {"a": 1})      # el bueno sigue entero

    def test_config(self):
        cp.save_config(LOC)
        self.assertEqual(cp.load_config()["name"], "Madrid")
        cp.save_settings(offline=True)
        self.assertTrue(cp.settings()["offline"])
        self.assertEqual(cp.load_config()["name"], "Madrid")  # no se pierde la ubicación


class TestBaseDeDatos(TempData):
    def test_predicciones_pasadas_y_marcador(self):
        t0 = 1_700_000_000 // 3600 * 3600
        cp.log_predictions(LOC, t0, {t0 + 3600 * h: 10.0 + h for h in range(1, 48)})
        later = cp.log_predictions(LOC, t0 + 30 * 3600, {t0 + 3600 * h: 0.0 for h in range(31, 60)})
        self.assertEqual(later[t0 + 25 * 3600], 35.0)        # emitida con ≥ 20 h de antelación
        self.assertNotIn(t0 + 10 * 3600, later)              # solo 10 h antes: no cuenta
        cp.save_tele(LOC, "2024-01-01", 12, 3, 11, 4)
        cp.save_tele(LOC, "2024-01-02", None, None, 11, 4)   # mal guardado: no debe romper
        con = cp.db(LOC)
        con.execute("INSERT INTO obs_day VALUES ('2024-01-01', 11, 4), ('2024-01-02', 11, 4)")
        con.commit()
        con.close()
        s = cp.tele_score(LOC)
        self.assertEqual((s["n"], s["pi"], s["tv"]), (1, 1, 0))
        self.assertAlmostEqual(s["err_tv"], 1.0)

    @unittest.skipIf(MADRID is None, "sin zonas horarias")
    def test_dias_observados(self):
        t0 = int(datetime(2024, 5, 1, tzinfo=MADRID).timestamp())
        obs = {t0 + 3600 * h: float(h % 24) for h in range(24 * 3)}
        cp.store_observed_days(LOC, obs, MADRID, t0 + 2.5 * D)
        con = cp.db(LOC)
        rows = con.execute("SELECT * FROM obs_day ORDER BY day").fetchall()
        con.close()
        self.assertEqual(rows, [("2024-05-01", 23.0, 0.0), ("2024-05-02", 23.0, 0.0)])


class TestClimaHistorico(unittest.TestCase):
    def test_tendencia(self):
        t0 = int(datetime(1950, 1, 1, tzinfo=timezone.utc).timestamp())
        n = 365 * 70
        ts = t0 + D * np.arange(n)
        years = 1950 + np.arange(n) / 365.25
        v = 15 + 0.02 * (years - 1950) + 8 * np.cos(2 * np.pi * np.arange(n) / 365.25)
        h = cp.climate_history({"daily": {"time": ts.tolist(), "temperature_2m_mean": v.tolist()}})
        self.assertAlmostEqual(h["slope"] * 10, 0.2, delta=0.02)
        self.assertEqual(len(h["normal"]), 12)

    def test_poco_historial(self):
        self.assertIsNone(cp.climate_history({"daily": {"time": [0], "temperature_2m_mean": [1]}}))

    def test_marcas_del_eje(self):
        self.assertEqual(cp.nice_ticks(0, 10, 5), [0, 2, 4, 6, 8, 10])
        self.assertEqual(cp.nice_ticks(5, 5), [4, 4.5, 5, 5.5, 6])
        self.assertTrue(cp.nice_ticks(float("nan"), 1))


# =========================================================================== almanaque
@unittest.skipIf(MADRID is None, "sin zonas horarias")
class TestAlmanaque(TempData):
    """Historial diario desde 1950 (como lo da ERA5 con días de tu hora) con récords puestos a mano."""

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(0)
        days = []
        d = date(1950, 1, 1)
        while d <= date(2025, 12, 31):
            days.append(d)
            d += timedelta(days=1)
        doy = np.array([x.timetuple().tm_yday for x in days])
        yr = np.array([x.year for x in days])
        tmax = 20 + 10 * np.cos(2 * np.pi * (doy - 200) / 365.25) + 0.02 * (yr - 1950) + rng.normal(0, 2, len(days))
        tmin = tmax - 10 + rng.normal(0, 1, len(days))
        rain = np.where(rng.random(len(days)) < 0.2, rng.exponential(6, len(days)), 0.0)
        idx = {x: i for i, x in enumerate(days)}
        tmax[idx[date(2017, 7, 13)]] = 45.0
        tmin[idx[date(1956, 2, 2)]] = -15.0
        rain[idx[date(1989, 10, 22)]] = 120.0
        cls.tmax, cls.days, cls.yr, cls.doy = tmax, days, yr, doy
        cls.resp = {"daily": {"time": [int(cp.midnight(x, MADRID)) for x in days],
                              "temperature_2m_max": tmax.round(2).tolist(),
                              "temperature_2m_min": tmin.round(2).tolist(),
                              "temperature_2m_mean": ((tmax + tmin) / 2).round(2).tolist(),
                              "precipitation_sum": rain.round(2).tolist()}}
        cls.arch = cp.daily_archive(cls.resp, MADRID)
        cls.normals, cls.label = cp.normals_table(cls.arch)

    def test_fechas_de_tu_hora(self):
        # la medianoche del 1 de enero de 1950 en Madrid es el 31 de diciembre a las 23 h en UTC
        self.assertEqual(min(self.arch), date(1950, 1, 1))
        self.assertEqual(len(self.arch), len(self.days))
        self.assertEqual(self.arch[date(2017, 7, 13)]["tmax"], 45.0)
        h = cp.climate_history(self.resp, MADRID)
        self.assertEqual(h["annual"][0][0], 1950)
        self.assertAlmostEqual(h["slope"], 0.02, delta=0.005)

    def test_lo_normal(self):
        self.assertEqual(self.label, "1991–2020")
        sel = [(1991 <= y <= 2020) and abs(dy - date(2001, 7, 13).timetuple().tm_yday) <= 3
               for y, dy in zip(self.yr, self.doy)]
        self.assertAlmostEqual(self.normals[(7, 13)]["tmax"], float(np.mean(self.tmax[sel])), delta=0.3)
        self.assertIn((2, 29), self.normals)
        self.assertTrue(0 <= self.normals[(10, 22)]["rain_days"] <= 1)

    def test_records(self):
        self.assertEqual(cp.day_records(self.arch, 7, 13)["max_high"], (45.0, 2017))
        self.assertEqual(cp.day_records(self.arch, 2, 2)["min_low"], (-15.0, 1956))
        self.assertEqual(cp.day_records(self.arch, 10, 22)["rain"], (120.0, 1989))
        r = cp.day_records(self.arch, 2, 29)
        self.assertEqual(r["n"], 19)                       # años bisiestos de 1952 a 2024
        self.assertEqual(cp.rank_of(self.arch, date(2017, 7, 13), "tmax"), (1, 76))

    def test_mes_y_misma_fecha_cada_anio(self):
        self.assertEqual(len(cp.month_view(self.arch, self.normals, 2024, 2)), 29)
        mv = cp.month_view(self.arch, self.normals, 2023, 2)
        self.assertEqual(len(mv), 28)
        self.assertTrue(all(x[1] is not None and x[4] is not None for x in mv))
        self.assertEqual(len(cp.same_day_series(self.arch, 7, 13)), 76)
        self.assertTrue(all(y % 4 == 0 for y, *_ in cp.same_day_series(self.arch, 2, 29)))
        future = cp.month_view(self.arch, self.normals, 2030, 1)
        self.assertTrue(all(x[1] is None and x[4] is not None for x in future))

    def test_historial_antiguo_solo_con_media(self):
        old = {"daily": {"time": self.resp["daily"]["time"][:800],
                         "temperature_2m_mean": self.resp["daily"]["temperature_2m_mean"][:800]}}
        arch = cp.daily_archive(old, MADRID)
        self.assertIsNone(cp.day_records(arch, 1, 1))
        self.assertEqual(cp.normals_table(arch)[0], {})

    def test_sol_y_luna_de_cualquier_fecha(self):
        a = cp.astro_day(40.4168, -3.7038, MADRID, date(2024, 6, 21))
        sr = datetime.fromtimestamp(a["sunrise"], MADRID)
        self.assertEqual((sr.hour, sr.minute // 5), (6, 44 // 5))
        self.assertIsNone(a["polar"])
        self.assertIn("Luna llena", a["next"])
        oslo = ZoneInfo("Europe/Oslo")
        self.assertEqual(cp.astro_day(69.65, 18.96, oslo, date(2024, 6, 21))["polar"], "day")
        self.assertEqual(cp.astro_day(69.65, 18.96, oslo, date(2024, 12, 21))["polar"], "night")
        self.assertEqual(cp.astro_day(-33.87, 151.21, ZoneInfo("Australia/Sydney"), date(1950, 1, 1))["phase"]
                         in ("Luna nueva", "Creciente", "Cuarto creciente", "Gibosa creciente", "Luna llena",
                             "Gibosa menguante", "Cuarto menguante", "Menguante"), True)

    def test_lo_que_guardo_clima_pi_ese_dia(self):
        d = date(2026, 3, 4)
        con = cp.db(LOC)
        con.execute("INSERT INTO obs_day VALUES (?, 18.5, 6.0)", (d.isoformat(),))
        con.commit()
        con.close()
        cp.save_tele(LOC, d.isoformat(), 19, 5, 18, 6)
        t0 = int(cp.midnight(d, MADRID))
        cp.store_sensor(LOC, [(t0 + 300 * i, 5 + 10 * math.sin(math.pi * i / 288), 60) for i in range(288)])
        r = cp.local_day(LOC, MADRID, d)
        self.assertEqual(r["obs"], (18.5, 6.0))
        self.assertEqual(r["tele"], (19, 5, 18, 6))
        self.assertAlmostEqual(r["sensor"][0], 15, delta=0.3)
        self.assertEqual(cp.local_day(LOC, MADRID, date(2026, 3, 5)), {})

    def test_exportar_csv(self):
        path = os.path.join(self.dir, "h.csv")
        self.assertEqual(cp.export_archive_csv(self.arch, path), len(self.days))
        with open(path) as f:
            rows = f.read().splitlines()
        self.assertEqual(rows[0], "fecha,max_C,min_C,media_C,lluvia_mm")
        self.assertEqual(len(rows), len(self.days) + 1)
        self.assertTrue(any(r.startswith("2017-07-13,45.0,") for r in rows))

    def test_estado_del_historial(self):
        hoy = date(2026, 10, 22)
        self.assertEqual(cp.archive_status({}, hoy), "vacío")
        old = cp.daily_archive({"daily": {"time": self.resp["daily"]["time"],
                                          "temperature_2m_mean": self.resp["daily"]["temperature_2m_mean"]}}, MADRID)
        self.assertEqual(cp.archive_status(old, hoy), "antiguo")
        self.assertEqual(cp.archive_status(self.arch, hoy), "atrasado")       # acaba en 2025
        self.assertEqual(cp.archive_status(self.arch, date(2026, 1, 25)), "ok")

    def _run_job(self, online, need_archive):
        """Una actualización normal (sin reentrenar), como la de cada hora."""
        import queue
        W = Mundo(start=date.today() - timedelta(days=20), days=40, seed=2)
        app = cp.App.__new__(cp.App)
        app.q = queue.Queue()
        calls = []

        def era5(loc):
            calls.append(loc)
            return self.resp
        with mock.patch.object(cp, "online", lambda timeout=8: online), \
                mock.patch.object(cp, "live_forecast", side_effect=RuntimeError("sin datos")), \
                mock.patch.object(cp, "extras_forecast", lambda loc: W.extras(time.time())), \
                mock.patch.object(cp, "era5_daily", era5):
            app._job(LOC, "k", False, None, need_archive)
        drain()
        msgs = []
        while not app.q.empty():
            msgs.append(app.q.get_nowait())
        return msgs, calls

    def test_con_historial_de_la_version_anterior_se_descarga_sin_esperar_a_reentrenar(self):
        """Lo que pasó en Zinacantepec: modelo ya entrenado, historial de la 2.7 → almanaque vacío."""
        msgs, calls = self._run_job(online=True, need_archive=True)
        self.assertEqual(len(calls), 1)
        hist = [d for k, d, _ in msgs if k == "history"]
        self.assertTrue(hist)
        self.assertEqual(cp.archive_status(hist[0][1], date(2026, 1, 25)), "ok")
        self.assertTrue(hist[0][2])                         # descargado (no el del disco)
        self.assertIn("forecast", [k for k, *_ in msgs])

    def test_historial_bueno_no_se_vuelve_a_pedir(self):
        msgs, calls = self._run_job(online=True, need_archive=False)
        self.assertEqual(calls, [])
        self.assertNotIn("history", [k for k, *_ in msgs])

    def test_sin_internet_lo_dice_y_lo_reintenta_luego(self):
        msgs, calls = self._run_job(online=False, need_archive=True)
        self.assertEqual(calls, [])
        self.assertIn(("history_err", "sin conexión a internet"), [(k, d) for k, d, _ in msgs])

    def test_descarga_con_tu_hora_y_todas_las_variables(self):
        calls = []

        def fake(base, params, timeout=90):
            calls.append(params)
            a = date.fromisoformat(params["start_date"])
            n = (date.fromisoformat(params["end_date"]) - a).days + 1
            ts = [int(cp.midnight(a + timedelta(days=i), MADRID)) for i in range(n)]
            return {"daily": {"time": ts, **{v: [1.0] * n for v in params["daily"].split(",")}}}
        path = os.path.join(cp.loc_dir(LOC), "era5_diario.json")
        cp._save_json(path, {"daily": {"time": [0], "temperature_2m_mean": [1.0]}})   # de la 2.6
        with mock.patch.object(cp, "get_json", fake):
            data = cp.era5_daily(LOC)
        self.assertEqual(calls[0]["timezone"], "Europe/Madrid")
        self.assertEqual(calls[0]["daily"], cp.ARCHIVE_DAILY)
        self.assertEqual(calls[0]["start_date"], cp.HISTORY_START.isoformat())   # se descargó de nuevo
        self.assertIn("temperature_2m_max", data["daily"])
        drain()


# =========================================================================== sensor DHT22
class FakeDHT:
    """Un DHT22 de mentira con los archivos que crea el controlador dht11 de Linux."""

    def __init__(self, base):
        self.dev = os.path.join(base, "iio:device0")
        os.makedirs(self.dev)
        with open(os.path.join(self.dev, "name"), "w") as f:
            f.write("dht11@4\n")
        self.set(21.5, 48.2)

    def set(self, t, h):
        for name, v in (("in_temp_input", t), ("in_humidityrelative_input", h)):
            p = os.path.join(self.dev, name)
            if v is None:
                if os.path.exists(p):
                    os.remove(p)                       # leerlo dará error, como el de verdad
            else:
                with open(p, "w") as f:
                    f.write(f"{int(round(v * 1000))}\n")


class TestSensor(TempData):
    def test_sin_sensor_no_se_usa_ni_se_exige(self):
        self.assertFalse(cp.sensor_available())
        self.assertIsNone(cp.read_sensor(sleep=lambda s: None))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cp.sensor_test(), 1)
        self.assertIn("no hace falta", out.getvalue())

    def test_lee_el_dht22_del_sistema(self):
        cp.IIO_BASE = os.path.join(self.dir, "iio")
        FakeDHT(cp.IIO_BASE)
        self.assertTrue(cp.sensor_available())
        t, h = cp.read_sensor(sleep=lambda s: None)
        self.assertAlmostEqual(t, 21.5)
        self.assertAlmostEqual(h, 48.2)

    def test_se_puede_desactivar(self):
        cp.IIO_BASE = os.path.join(self.dir, "iio")
        FakeDHT(cp.IIO_BASE)
        cp.save_settings(sensor={"enabled": False})
        self.assertFalse(cp.sensor_available())
        self.assertIsNone(cp.read_sensor(sleep=lambda s: None))

    def test_lecturas_fallidas_y_absurdas(self):
        cp.IIO_BASE = os.path.join(self.dir, "iio")
        dht = FakeDHT(cp.IIO_BASE)
        dht.set(None, None)
        self.assertIsNone(cp.read_sensor(sleep=lambda s: None))
        dht.set(150.0, 50.0)                               # fuera de rango
        self.assertIsNone(cp.read_sensor(sleep=lambda s: None))
        seq = iter([None, (20.0, 50.0), (99.0, 50.0), (20.2, 51.0), (20.4, 52.0)])

        def fake(dev, pin):
            x = next(seq)
            if x is None:
                raise OSError(5, "Input/output error")
            return x
        with mock.patch.object(cp, "_read_once", fake):
            t, h = cp.read_sensor(sleep=lambda s: None)
        self.assertAlmostEqual(t, 20.1)                    # dos buenas que coinciden: su media
        self.assertAlmostEqual(h, 50.5)
        seq = iter([(20.0, 50.0), (21.0, 50.0), (20.2, 52.0)])
        with mock.patch.object(cp, "_read_once", lambda dev, pin: next(seq)):
            t, h = cp.read_sensor(sleep=lambda s: None)
        self.assertAlmostEqual(t, 20.2)                    # no coinciden: mediana de 3
        self.assertAlmostEqual(h, 50.0)

    def test_libreria_que_falta_no_rompe(self):
        cp.save_settings(sensor={"pin": 4})
        with mock.patch.dict(sys.modules, {"adafruit_dht": None}):
            self.assertIsNone(cp.read_sensor(sleep=lambda s: None))
        drain()

    def test_filtro_de_saltos(self):
        f = cp.SpikeFilter()
        self.assertEqual(len(f.push(0, 20.0, 50)), 1)
        self.assertEqual(f.push(300, 28.0, 50), [])          # +8 °C en 5 min: se aparta
        self.assertEqual(len(f.push(600, 20.3, 50)), 1)      # era un error
        self.assertEqual(f.push(900, 26.0, 50), [])
        self.assertEqual(len(f.push(1200, 26.4, 50)), 2)     # se confirma: era de verdad
        self.assertEqual(len(f.push(1200 + 3600, 10.0, 50)), 1)   # tras un rato sin datos, vale

    def test_medias_horarias(self):
        t0 = 1_700_000_000 // 3600 * 3600
        cp.store_sensor(LOC, [(t0 + 300 * i, 10.0 + (i // 12), 50.0) for i in range(36)])
        temp, hum = cp.sensor_hourly(LOC, t0, t0 + 3 * 3600)
        self.assertEqual(sorted(temp), [t0, t0 + 3600, t0 + 7200, t0 + 10800])
        temp, _ = cp.sensor_hourly(LOC, t0, t0 + 2 * 3600 + 900)       # las 3:00 aún no han llegado
        self.assertEqual(max(temp), t0 + 7200)
        self.assertAlmostEqual(temp[t0 + 3600], (10 * 6 + 11 * 6) / 12)
        self.assertEqual(cp.sensor_latest(LOC, now=t0 + 36 * 300)[1], 12.0)
        self.assertIsNone(cp.sensor_latest(LOC, now=t0 + 10 * 3600))   # demasiado vieja
        self.assertIsNone(cp.sensor_latest(LOC, now=t0 - 10))          # no ve el futuro

    def test_punto_de_rocio_y_sensacion(self):
        self.assertAlmostEqual(float(cp.dew_point(20, 50)), 9.3, delta=0.1)
        self.assertAlmostEqual(float(cp.dew_point(30, 100)), 30, delta=0.01)
        self.assertGreater(float(cp.apparent(32, 80)), 32)
        self.assertLess(float(cp.apparent(10, 30)), 10)


@unittest.skipIf(MADRID is None, "sin zonas horarias")
class TestSinConexion(TempData):
    """Modo sin conexión con un DHT22: datos sintéticos con un sensor mal situado a propósito."""

    @classmethod
    def setUpClass(cls):
        cls.W = Mundo(days=760, seed=5)
        cls.T0 = int(cls.W.ts[0])
        cls.upto = cls.T0 + 700 * D
        cls.tr = cp.train(cls.W.era5(cls.upto), cls.W.prev_all(cls.upto), cls.W.app(cls.upto), MADRID)
        cls.sens = dict(zip(cls.W.ts.tolist(), cls.W.sensor_temp.tolist()))
        drain()

    def install_sensor(self, days_before, until):
        cp.store_sensor(LOC, self.W.sensor_rows(until - days_before * D, until))

    def online(self, start, stop, every=6):
        """Funciona con red un tiempo (guarda pronósticos, corrige con el sensor)."""
        out = []
        for now in range(start, stop, every * 3600):
            corr = cp.sensor_correction(LOC, MADRID, now)
            obs = cp.sensor_hourly(LOC, now - 7 * D, now)[0]
            r = cp.make_forecast(self.W.live(now), self.W.extras(now), self.tr, MADRID, now, LOC, corr, obs)
            cp.log_predictions(LOC, now, r["pi_raw"])
            out.append((now, r))
            drain()
        return out

    def test_el_sensor_mejora_el_pronostico_con_red(self):
        end = self.upto + 40 * D
        self.install_sensor(60, end)
        e_raw, e_cor = [], []
        for now, r in self.online(self.upto + 5 * 3600, end):
            if now < self.upto + 20 * D:
                continue
            self.assertTrue(r["corrected"])
            for t in r["pi"]:
                if t - now <= 2 * D:
                    e_raw.append(abs(r["pi_raw"][t] - self.sens[t]))
                    e_cor.append(abs(r["pi"][t] - self.sens[t]))
        self.assertLess(np.mean(e_cor), np.mean(e_raw) * 0.8)

    def test_sin_red_y_sin_sensor_usa_lo_ultimo_descargado(self):
        self.online(self.upto + 5 * 3600, self.upto + 3 * D)
        now = self.upto + 3 * D + 1800
        r = cp.offline_forecast(LOC, MADRID, now, self.tr)
        self.assertTrue(r["offline"])
        self.assertEqual(r["sources"]["sensor"], 0)
        self.assertGreater(len(r["pi"]), 100)
        self.assertEqual(len(r["days"]), 7)
        truth = dict(zip(self.W.ts.tolist(), self.W.truth.tolist()))
        err = np.mean([abs(v - truth[t]) for t, v in r["pi"].items() if t - now < 2 * D])
        self.assertLess(err, 2.0)

    def test_sin_nada_da_error_claro(self):
        with self.assertRaisesRegex(RuntimeError, "sin conexión"):
            cp.offline_forecast(LOC, MADRID, self.upto, self.tr)

    def test_modelo_del_sensor_y_prueba_justa(self):
        until = self.upto + 30 * D
        self.install_sensor(300, until)
        temp, hum = cp.sensor_hourly(LOC, 0, until)
        sm = cp.train_sensor(temp, hum, MADRID, self.tr["clim"])
        self.assertIsNotNone(sm)
        for h in (1, 6, 24):
            i = sm["h"][h]
            self.assertLess(min(i["mae_model"], i["mae_damped"]), i["mae_persist"] * 1.05, f"{h} h")
        self.assertLess(sm["h"][1]["mae_model"], sm["h"][1]["mae_damped"])
        cp.save_sensor_model(LOC, sm)
        sm2 = cp.load_sensor_model(LOC)
        a = cp.sensor_forecast(sm, temp, hum, MADRID, until, self.tr["clim"])
        b = cp.sensor_forecast(sm2, temp, hum, MADRID, until, self.tr["clim"])
        self.assertEqual(len(a), 168)
        for t in a:
            self.assertAlmostEqual(a[t][0], b[t][0], places=6)

    def test_pocos_dias_de_sensor_no_entrena(self):
        until = self.upto + 30 * D
        self.install_sensor(10, until)
        temp, hum = cp.sensor_hourly(LOC, 0, until)
        self.assertIsNone(cp.train_sensor(temp, hum, MADRID, self.tr["clim"]))
        f = cp.sensor_forecast(None, temp, hum, MADRID, until, self.tr["clim"])
        self.assertGreater(len(f), 160)                    # persistencia amortiguada

    def test_pronostico_sin_conexion_completo_y_con_margen_honesto(self):
        start = self.upto + 25 * D
        self.install_sensor(200, start + 8 * D)
        self.online(start - 20 * D + 5 * 3600, start)
        temp, hum = cp.sensor_hourly(LOC, 0, start)
        sm = cp.train_sensor(temp, hum, MADRID, self.tr["clim"])
        inside, err6, errp = [], [], []
        for now in range(start + 1800, start + 6 * D, 12 * 3600):
            r = cp.offline_forecast(LOC, MADRID, now, self.tr, sm)
            self.assertEqual(len(r["days"]), 7)
            self.assertTrue(all(row["band"] for rows in r["hours"].values() for row in rows if not row["past"]))
            for t, v in r["pi"].items():
                lo, hi = r["band"][t]
                inside.append(lo <= self.sens[t] <= hi)
                if t - now <= 6 * 3600:
                    err6.append(abs(v - self.sens[t]))
                    # referencia: suponer que sigue como ahora (persistencia)
                    cur = cp.sensor_latest(LOC, now=now)[1]
                    errp.append(abs(cur - self.sens[t]))
            drain()
        self.assertLess(np.mean(err6), np.mean(errp))
        self.assertGreater(np.mean(inside), 0.55)           # «8 de cada 10» de media; esta semana es difícil
        self.assertLess(np.mean(inside), 0.97)              # ni dar márgenes inútiles

    def test_lo_de_sin_conexion_no_se_guarda_como_pronostico(self):
        """Si se guardara, la siguiente vez lo tomaría como «lo último descargado»."""
        self.online(self.upto + 5 * 3600, self.upto + 2 * D)
        issue1, _ = cp.stale_forecast(LOC, self.upto + 2 * D + 600)
        con = cp.db(LOC)
        n1 = con.execute("SELECT COUNT(*) FROM pred").fetchone()[0]
        con.close()
        app = cp.App.__new__(cp.App)
        app.q = __import__("queue").Queue()
        with mock.patch.object(cp.time, "time", return_value=self.upto + 2 * D + 600):
            app._offline_job(LOC, None, MADRID, self.tr, False, False, False)
        self.assertEqual(app.q.get_nowait()[0], "status")
        self.assertTrue(app.q.get_nowait()[1]["offline"])
        con = cp.db(LOC)
        n2 = con.execute("SELECT COUNT(*) FROM pred").fetchone()[0]
        con.close()
        self.assertEqual(n1, n2)
        self.assertEqual(issue1, cp.stale_forecast(LOC, self.upto + 2 * D + 600)[0])
        drain()


# =========================================================================== varias copias
class TestCopias(TempData):
    def test_canal_de_ordenes_responde_y_detecta_ventana_colgada(self):
        if not hasattr(cp.socket, "AF_UNIX"):
            self.skipTest("sin sockets Unix")
        q = __import__("queue").Queue()
        with mock.patch.object(cp, "sock_path", lambda: os.path.join(self.dir, "c.sock")):
            srv = cp.start_command_server(q)
            try:
                v = cp.send_cmd("version").split()
                self.assertEqual(v[:2], ["ok", cp.APP_VERSION])
                self.assertEqual(int(v[2]), os.getpid())

                def window():                                   # la ventana atiende la orden
                    kind, data, done = q.get(timeout=5)
                    done.set()
                threading.Thread(target=window, daemon=True).start()
                self.assertEqual(cp.send_cmd("show", timeout=15), "ok")
                t = time.time()                                 # nadie atiende: colgada
                self.assertEqual(cp.send_cmd("show", timeout=15), "busy")
                self.assertGreater(time.time() - t, 9)
            finally:
                srv.close()


if __name__ == "__main__":
    unittest.main()
