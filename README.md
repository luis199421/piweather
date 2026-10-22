# Clima Pi

`clima_pi.py` es un solo programa de Python para la **Raspberry Pi** (funciona en
cualquier PC con Python 3.9+) que hace tu propio pronóstico del tiempo:

* Descarga gratis (Open-Meteo, sin clave) los pronósticos de **13 modelos** meteorológicos:
  ECMWF IFS, GFS, ICON, Météo-France, UK Met Office, GEM, JMA, KMA, CMA y BOM, y **tres de
  inteligencia artificial**: ECMWF AIFS, NOAA AIGFS y NOAA HGEFS (híbrido física + IA).
* Entrena el **Modelo Pi**: aprende cuánto se equivoca cada modelo en tu zona (comparando
  ~2 años de sus pronósticos con lo que pasó, según ERA5) y los combina dándole más peso a
  los que mejor aciertan (técnica tipo MOS).
* **Opcional: un sensor DHT22.** Si lo conectas, corrige el pronóstico para tu sitio exacto y,
  **sin internet**, sigue pronosticando. Si no lo conectas, todo funciona igual que siempre.
* Pestañas: Ahora · Pronóstico · Por horas · Sol y Luna · Precisión · Clima histórico ·
  **Almanaque** · En vivo.

## Instalar (o actualizar desde la 2.5)

```bash
git clone -b claude/optimize-test-code-3ahdkb https://github.com/luis199421/piweather.git
cd piweather
sh install.sh
```

Sin terminal: descarga el ZIP, descomprímelo y haz doble clic en **Instalar Clima Pi**.
Ejecútalo como tu usuario (sin `sudo`): solo pide la contraseña si falta algún paquete.

El instalador:

1. Instala lo que falte (`python3-tk`, `python3-numpy` y, si se puede, lo del icono de la barra).
2. **Quita la versión anterior**: cierra la ventana y el icono de la barra (por las buenas; si
   está colgada, a la fuerza), y aparta el `clima_pi.py` antiguo, esté donde esté guardado,
   a `~/.clima_pi/version_anterior/clima_pi_2.5.py` (no lo borra, por si acaso). No toca un
   editor como Thonny que tenga el archivo abierto.
3. Instala la nueva en `~/.local/share/clima-pi/clima_pi.py`, la pone en el menú y respeta si
   tenías «Iniciar con el Pi». **Tus datos se conservan** (historial, modelo, sensor).
4. La abre.

**Es idempotente:** ejecutarlo otra vez con lo mismo no cambia nada (ni cierra la ventana ni
toca un archivo; dice «no había nada que cambiar»). Con una versión nueva, solo cambia lo
necesario.

```bash
sh install.sh --autostart       # que arranque al encender (--no-autostart para quitarlo)
sh install.sh --no-start        # no abrirla al terminar
sh install.sh --uninstall       # quitarlo (tus datos de ~/.clima_pi se quedan)
sh install.sh --purge           # quitarlo y borrar también tus datos
```

## Usar

```bash
python3 ~/.local/share/clima-pi/clima_pi.py                  # la ventana (o desde el menú)
python3 ~/.local/share/clima-pi/clima_pi.py --offline        # sin usar internet aunque lo haya
python3 ~/.local/share/clima-pi/clima_pi.py --backtest       # prueba de precisión, sin ventana
python3 ~/.local/share/clima-pi/clima_pi.py --sensor-test    # comprobar el DHT22
```

Datos en `~/.clima_pi/`. Datos meteorológicos: Open-Meteo.com (CC BY 4.0).

---

## Qué cambió en la versión 2.8: pestaña Almanaque

Un archivo de tu clima: eliges **cualquier fecha desde 1950** (◀ ▶, día/mes/año u «Hoy») y ves:

* **Ese día:** máxima, mínima y lluvia (reanálisis ERA5), cuánto se separó de lo normal y en
  qué puesto quedó entre todos los años («la n.º 1 más alta de 76»); y lo que guardó Clima Pi:
  lo que midió tu sensor, lo observado y lo que apuntaste en «Pi contra la tele». Si es una
  fecha futura de esta semana, el pronóstico del Pi.
* **Sol y Luna** de esa fecha (calculado en el Pi, sin internet): salida y puesta, alba y
  anochecer civil, horas de luz, fase, salida y puesta de la Luna, próximas luna llena y nueva.
* **Lo normal** (1991–2020, ±3 días): máxima, mínima y cuántos años llueve ese día.
* **Récords de esa fecha:** máxima más alta y más baja, mínima más baja y más alta, día más
  lluvioso, con su año.
* Dos gráficas: **el mes** día a día sobre la banda de lo normal (con el pronóstico en los días
  que aún no han pasado) y **esa fecha cada año** desde 1950. Pasa el ratón para ver cada valor.
* **Exportar CSV**: todo el historial diario a `~/Clima_Pi_historial_<ciudad>.csv`.

Para esto el historial diario ahora trae máxima, mínima y lluvia (antes solo la media), y los
días son **de tu hora**, no de la UTC (el 1 de enero es tu 1 de enero). Cuesta lo mismo en la
API, pero **la primera vez vuelve a descargar el historial desde 1950** (unos minutos, en
segundo plano). Se hace en la **primera actualización** (o al pulsar «Actualizar ahora»), sin
esperar al reentrenamiento diario; mientras tanto el almanaque dice que está descargando, y sin
internet lo dice y lo reintenta en cuanto vuelve. Ya enseña el sol, la luna y lo que guardó
Clima Pi.

*2.8.1:* en la 2.8 el historial nuevo solo se descargaba al reentrenar (una vez al día), así
que tras actualizar con el modelo ya entrenado el almanaque salía vacío. Ahora se descarga en
cuanto falta, está incompleto o va atrasado.

## Qué cambió en la versión 2.7: modelos de inteligencia artificial

Los modelos de IA (GraphCast, AIFS, Pangu…) calculan el planeta entero y necesitan tarjetas
gráficas grandes: no caben en una Raspberry. Lo que sí se puede es **usar lo que ya calculan**
y que el Modelo Pi aprenda cuánto confiar en ellos en tu zona. Además del ECMWF AIFS que ya
estaba, se añaden los dos de NOAA que ofrece Open-Meteo:

| Modelo | Identificador | Qué es |
|---|---|---|
| NOAA AIGFS | `ncep_aigfs025` | la versión con IA del GFS |
| NOAA HGEFS | `ncep_hgefs025_ensemble_mean` | media de un conjunto híbrido física + IA |

Dan **un dato cada 6 horas**. Rellenar las horas de en medio con la media de los demás modelos
(lo que se hacía con cualquier hueco) le quita al Modelo Pi casi todo lo que aportan. Ahora se
interpola en el tiempo **cuánto se separa ese modelo de la media de los demás**: el ciclo
día/noche lo ponen los otros y la opinión propia del modelo de IA se conserva.

Lo que dicen las pruebas (4 mundos simulados, error medio en vivo):

| | error medio | a 5 días |
|---|---|---|
| sin los modelos de NOAA | 1,159 °C | 1,69 °C |
| con ellos, si aciertan **parte de lo que los demás fallan a la vez** | **1,043 °C (−10 %)** | **1,49 °C** |
| lo mismo pero rellenando como antes | 1,143 °C | 1,66 °C |
| con ellos, si solo son «uno más» (fallan lo mismo que los demás) | 1,158 °C | 1,68 °C |

O sea: el Modelo Pi los aprovecha (en ese caso les da el mayor peso, ~0,5 cada uno) **si de
verdad aportan algo**, y si no, no empeora. Cuánto aportan en tu zona solo lo dicen tus datos
reales: mira la pestaña Precisión o `--backtest` después del primer entrenamiento.

Avisos: si Open-Meteo no tiene el archivo de pronósticos pasados de alguno, el programa lo dice
(«sin datos de: …») y sigue sin él; y como son recientes, al principio tienen menos historial
que los demás (se usan desde que tienen 45 días con datos).

## Qué cambió en la versión 2.6

### Más precisión

| Cambio | Por qué |
|---|---|
| **Antelación correcta** | Open-Meteo define `previous_day1` como lo que el modelo predecía **24 h antes**. La 2.5 usaba ese modelo (24–48 h) para las horas de **hoy** (0–24 h), y el de 2 días para mañana por la tarde, etc.: siempre un día de más. El Modelo Pi trata un pronóstico de 3 días como uno de 4 y lo acerca de más a la normal. |
| **Modelo de «hoy» (0 días)** | Se descarga también la pasada más reciente (`temperature_2m` de la API de pasadas anteriores) y se entrena un modelo para las próximas 24 h. |
| **Mezcla entre antelaciones** | Un pronóstico a 30 h está entre el modelo de 1 día y el de 2 días: se mezclan (75 % / 25 %). Así no hay saltos al pasar de un día a otro. Lo mismo para la máxima y la mínima. |
| **Hora local exacta en el cambio de hora** | Antes se calculaba una vez al día a mediodía UTC; el día del cambio de hora las horas de la mañana quedaban corridas. |

**Resultado** (simulación con 4 «mundos» sintéticos, pronósticos emitidos cada 12 h durante 3 meses):
error medio por hora **1,24 → 1,16 °C (−6 %)**; máxima/mínima de los próximos días 0,98 → 0,96 °C.
En uno de esos mundos, por día de antelación: hoy 0,40 → 0,39 · 1 día 0,72 → 0,63 ·
3 días 1,46 → 1,11 · 6 días 1,90 → 1,46 °C (lo que más mejora: de 3 a 6 días).

Lo que **probé y descarté** porque no mejoraba (o empeoraba) en la prueba justa:
regularización elegida automáticamente, dar más peso a los datos recientes, más variables
(segundo armónico diario, estación × hora), rellenar huecos teniendo en cuenta el sesgo de cada
modelo, y usar la media simple en las antelaciones donde «parece» ganar. Con unos meses de
validación el ruido decide más que la señal, y elegía mal.

Una cosa honesta: **a 0 días** (hoy) los modelos ya aciertan tanto que el Modelo Pi empata con
la media simple (±0,02 °C). A partir de 1 día gana claramente.

### Más fiabilidad

* **La ventana no se queda congelada por un error.** Antes, si algo fallaba al pintar (un dato
  raro), los temporizadores de Tk se paraban y la ventana dejaba de actualizarse para siempre.
  Ahora se apunta el error y sigue (y vuelve a enseñar el último pronóstico bueno).
* **Abrirla otra vez trae la que está abierta, o sustituye a una colgada.** La ventana responde
  ella misma (antes respondía un hilo aunque la ventana estuviera colgada); si no responde en
  10 s, se cierra y se abre otra. *(Idea traída del proyecto Pi 5 Energy Plan.)*
* **Errores guardados** en `~/.clima_pi/ventana.log` cuando se abre desde el menú o al encender.
* **Red:** reintenta los errores 5xx del servidor y las respuestas cortadas o corruptas (antes
  una respuesta cortada podía tirar todo el reentrenamiento).
* **Disco:** escritura atómica con `fsync` (un corte de luz no deja un JSON a medias); una
  descarga que se cortó a mitad ya no se da por buena solo porque el archivo es reciente.
* `--backtest`: la prueba de precisión con tus datos reales, sin red ni ventana.

---

## Sensor DHT22 (opcional)

**No hace falta.** Sin sensor el programa no lo busca más que mirando si existe, no avisa de
nada y funciona como siempre.

### Conexión

| DHT22 | Raspberry Pi |
|---|---|
| VCC (+) | 3,3 V (pin 1) |
| DATA (out) | GPIO 4 (pin 7) — con resistencia de 10 kΩ a 3,3 V si es el sensor suelto (los módulos de 3 patas ya la llevan) |
| GND (−) | GND (pin 6) |

Añade esta línea a `/boot/firmware/config.txt` y reinicia (el controlador `dht11` de Linux
también sirve para el DHT22; es lo más fiable en la Pi 5):

```
dtoverlay=dht11,gpiopin=4
```

Compruébalo con `python3 clima_pi.py --sensor-test`. Alternativa sin el overlay: la librería
de Adafruit (`pip install adafruit-circuitpython-dht`) y en `~/.clima_pi/config.json`
`"sensor": {"pin": 4}`. Para desactivarlo: `"sensor": {"enabled": false}`.

**Dónde ponerlo** (lo que más cambia la precisión): a la sombra siempre, a 1,5 m del suelo,
lejos de paredes que dan al sol, con ventilación (mejor dentro de una garita o un protector de
platos). Al sol marca varios grados de más.

### Qué hace con conexión

* Lee el sensor cada 5 min (repite las lecturas fallidas, que en el DHT22 son frecuentes,
  descarta valores imposibles y saltos que no se confirman).
* Aprende la diferencia entre tu sitio y la casilla del modelo, **para cada hora del día**
  (sombra por la mañana, sol por la tarde, ciudad, ladera…), y corrige el pronóstico.
* Corrige también el error de ahora mismo, que se va olvidando en unas horas.
* La temperatura grande de «Ahora» y la línea «observado» pasan a ser **lo que mide tu sensor**.
* El desglose de «En vivo» muestra la «Corrección con tu sensor» y sigue sumando exacto.

Simulación (sensor con el sol de la tarde y la helada de la noche, 4 mundos):
error en las próximas 48 h frente a tu sensor **0,82 → 0,52 °C**.

### Qué hace sin conexión

Cuando no hay internet (o con `--offline`), junta dos fuentes, cada una pesada según el error
que se le ha **medido**:

1. **El modelo del propio sensor.** Lo esperable a cada hora (la misma hora en los últimos
   7 días, que va dando paso a la normal de la fecha ajustada a tu sitio) + cuánto se separará
   de eso. Lo entrena con tu historial (desde 21 días) con una prueba justa; en cada antelación
   se queda con el modelo o con la «persistencia amortiguada», lo que falle menos.
2. **El último pronóstico descargado**, corregido con el sensor. Su error real según los días
   que lleva emitido se mide con tu sensor mientras hay conexión.

Muestra un **margen probable** (8 de cada 10 veces la temperatura cae dentro) y en la barra
de estado pone «SIN CONEXIÓN». Cada 15 min mira si volvió la red. Sin sensor y sin
conexión, usa el último pronóstico descargado (hasta 7 días).

Lo que se puede esperar (simulación: 4 mundos × 9 cortes de una semana, error medio frente a
tu sensor, con ~10 meses de historial del sensor):

| Sin conexión | 0–6 h | 6–24 h | 1–2 días | 2–4 días | 4–7 días | margen 80 % |
|---|---|---|---|---|---|---|
| **Clima Pi sin conexión** | **0,53 °C** | **0,85** | **1,29** | **1,90** | **2,64** | acierta el 83 % |
| solo «como estos días» (persistencia) | 0,80 | 1,40 | 2,06 | 2,65 | 3,27 | |

Con solo ~2–3 meses de sensor: 0,52 · 0,88 · 1,43 · 2,31 · 3,21 °C. Para comparar, con
conexión (Modelo Pi + sensor) el error en las próximas 48 h es 0,52 °C.

**Lo honesto:** sin internet ningún sensor ve venir un frente. Las primeras 24 h salen bien;
de 2 días en adelante el pronóstico se acerca a «lo normal para la fecha». Si quieres más
alcance sin conexión, lo que más ayudaría es un **barómetro** (BME280): la tendencia de la
presión es la mejor pista de un cambio de tiempo (no está implementado todavía).

Todas las cifras de este README son de **datos simulados**. Las de tu sitio salen en la
pestaña Precisión y con `--backtest` (y la del sensor, en el registro «En vivo»).

---

## Pruebas

```bash
python3 -m unittest discover -s tests -v
```

82 pruebas, sin red, sin ventana y sin sensor de verdad (un DHT22 simulado con los archivos
que crea Linux), en ~30 s. Usan un mundo meteorológico sintético con el formato exacto de
las respuestas de Open-Meteo (`tests/sintetico.py`): 11 modelos con su sesgo, su sesgo
día/noche que cambia con la estación, error que crece con la antelación (en parte común a
todos), huecos, un modelo que empieza tarde y otro que cambia de versión a mitad; y, si se
pide, los 2 de IA de NOAA (un dato cada 6 h, historial corto, ciclo día/noche aplanado).

* **Precisión:** el Modelo Pi mejora a la media y al mejor modelo a 1–7 días, también en
  máxima/mínima; la prueba no mira el futuro; mezcla de antelaciones; el desglose suma
  exactamente la predicción (también con huecos); guardar y cargar da lo mismo; un modelo
  de versión anterior o un archivo roto se ignoran.
* **Modelos de IA de 6 en 6 h:** el relleno conserva el ciclo de los demás y no inventa
  fuera de sus datos; se usan aunque tengan poco historial; si aportan, mejoran el pronóstico
  (y el relleno nuevo es necesario para ello); el desglose sigue sumando exacto.
* **Almanaque:** fechas de tu hora (también el 1 de enero en Madrid, que en UTC es aún
  el 31 de diciembre); lo normal 1991–2020; récords con su año (puestos a mano en los datos);
  el 29 de febrero; meses de 28, 29 y 31 días; sol de medianoche y noche polar; lo guardado
  por el sensor y el marcador; exportar CSV; y que con el historial de la 2.7 (solo la media)
  se vuelve a descargar con tu hora y todas las variables.
* **Hora local:** todas las horas de un año con sus cambios de hora, comparadas con `zoneinfo`.
* **Sol y Luna:** salida y puesta del sol en Madrid, Londres y Sídney (±3 min de lo publicado),
  sol de medianoche y noche polar en Tromsø, fases de la Luna de 2024 (±20 min).
* **Red:** reintentos con 5xx, 429 y respuestas cortadas; errores claros sin red; el
  limitador de llamadas por minuto.
* **Disco:** descarga por trozos, solo lo nuevo, continuar una descarga cortada, archivos de
  una versión anterior, escritura atómica.
* **Base de datos:** predicciones con ≥ 20 h de antelación, marcador «Pi contra la tele»
  (también con filas mal guardadas), días observados.
* **Sensor:** sin sensor no se usa ni se exige; lecturas fallidas, absurdas y saltos; la
  librería que falta; medias horarias (sin horas a medias ni del futuro).
* **Sin conexión:** el sensor mejora el pronóstico con conexión; sin red y sin sensor usa lo
  último descargado; modelo del sensor con prueba justa; con pocos días no entrena; el margen
  del 80 % ni presume ni es inútil; lo hecho sin conexión no se guarda como «pronóstico del Pi»
  (si no, la siguiente vez lo tomaría como lo último descargado).
* **Varias copias:** la ventana responde ella misma; si está colgada, quien pregunta lo sabe.

**El instalador** tiene su propia prueba, con la versión 2.5 abierta de verdad (en una
carpeta personal temporal, no toca la tuya): `xvfb-run -a sh tests/test_install.sh`.
Comprueba que cierra la 2.5 (también si está colgada), la aparta aunque esté en una carpeta
con espacios, no toca un editor con el archivo abierto, conserva el arranque y tus datos;
que una 2.ª y 3.ª vez no cambian nada (ni la ventana abierta ni un solo archivo); y que
desinstalar o borrar dos veces seguidas no falla.

GitHub Actions las pasa con Python 3.9, 3.11 y 3.12, y la del instalador en Ubuntu con una
pantalla virtual (`.github/workflows/tests.yml`).
Además probé la ventana de verdad (Tk en una pantalla virtual) con datos simulados, un DHT22
simulado y un corte de internet a mitad.

## Archivos en `~/.clima_pi/`

| Archivo | Qué es |
|---|---|
| `config.json` | ubicación y ajustes (`offline`, `sensor`) |
| `<lat>_<lon>/era5_horario.json`, `era5_diario.json` | historial de la realidad (ERA5) |
| `<lat>_<lon>/pasados_<modelo>_<id>.json` | pronósticos pasados de cada modelo (0–7 días) |
| `<lat>_<lon>/modelo_pi.json` | el Modelo Pi entrenado |
| `<lat>_<lon>/modelo_sensor.json` | el modelo del sensor (si hay) |
| `<lat>_<lon>/predicciones.db` | predicciones, marcador, días observados, lecturas del sensor |
| `ventana.log` | errores cuando se abre sin terminal |

Al pasar de la 2.5 a la 2.6 se vuelven a descargar una vez los pronósticos pasados (ahora
incluyen el día 0) y se reentrena: tarda unos minutos.
