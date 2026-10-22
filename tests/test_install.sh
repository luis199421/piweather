#!/bin/sh
# Prueba del instalador con la versión 2.5 funcionando de verdad (necesita tkinter y una
# pantalla; sin ella, con Xvfb):
#
#   xvfb-run -a sh tests/test_install.sh
#
# Todo pasa en una carpeta personal temporal: no toca la tuya.
# shellcheck disable=SC2016  # las comprobaciones van entre comillas simples a propósito (eval)
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
"$PY" -c "import tkinter, numpy" 2>/dev/null || { echo "Falta tkinter o numpy: me la salto"; exit 0; }
[ -n "${DISPLAY:-}" ] || { echo "Sin pantalla: ejecútala con xvfb-run -a sh $0"; exit 0; }
export PYTHON="$PY"
NEW=$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' "$REPO/clima_pi.py" | head -n 1)
export NEW
HOME=$(mktemp -d)
export HOME XDG_RUNTIME_DIR="$HOME/run"
mkdir -p "$XDG_RUNTIME_DIR"
SOCK="$XDG_RUNTIME_DIR/clima-pi.sock"
D="$HOME/.local/share/clima-pi/clima_pi.py"
export MENU="$HOME/.local/share/applications/clima-pi.desktop"
export AUTO="$HOME/.config/autostart/clima-pi.desktop"
FAIL=0
ok() { echo "  OK  $*"; }
bad() { echo "  MAL $*"; FAIL=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
running() {   # PID de las copias de Clima Pi vivas: Python ejecutando clima_pi.py (no zombis)
    "$PY" - <<'PYEOF'
import os
for pid in os.listdir("/proc"):
    try:
        with open("/proc/%s/status" % pid) as f:
            if "\nState:\tZ" in f.read():
                continue
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            argv = [a for a in f.read().decode(errors="replace").split("\0") if a]
    except (OSError, ValueError):
        continue
    args = [a for a in argv[1:] if not a.startswith("-")]
    if argv and "python" in os.path.basename(argv[0]) and args and os.path.basename(args[0]) == "clima_pi.py":
        print(pid)
PYEOF
}
wait_sock() { i=0; while [ ! -S "$SOCK" ] && [ $i -lt 60 ]; do sleep 0.5; i=$((i + 1)); done; }
version() {
    "$PY" -c "
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1])
s.sendall(b'version\n'); print(s.recv(64).decode().strip())" "$SOCK" 2>/dev/null
}
inst() { sh "$REPO/install.sh" --no-start "$@" 2>&1; }
footprint() {   # lo que gestiona el instalador, con fechas de modificación (no las cachés de GTK)
    for f in "$D" "$MENU" "$AUTO" "$HOME/.clima_pi/clima-pi.png" "$HOME/.clima_pi/version_anterior"/*; do
        [ -f "$f" ] && printf "%s %s %s\n" "$f" "$(stat -c %Y "$f")" "$(cksum < "$f")"
    done
}

echo "== La versión 2.5, guardada en una carpeta con espacios, abierta y con arranque al encender"
OLD="$HOME/Mis programas/clima_pi.py"
mkdir -p "$HOME/Mis programas" "$HOME/.clima_pi/40.417_-3.704"
cp "$REPO/tests/previous/clima_pi_2.5.py" "$OLD"
printf '{"location": {"name": "Madrid", "latitude": 40.4168, "longitude": -3.7038, "timezone": "Europe/Madrid"}}' \
    > "$HOME/.clima_pi/config.json"
echo "lecturas" > "$HOME/.clima_pi/40.417_-3.704/predicciones.db"
"$PY" -c "
import importlib.util, sys
s = importlib.util.spec_from_file_location('v', sys.argv[1]); m = importlib.util.module_from_spec(s)
s.loader.exec_module(m); m.set_autostart(True)" "$OLD"
(cd "$HOME" && "$PY" "$OLD" >/dev/null 2>&1 &)
"$PY" -c "import time; time.sleep(900)" "$OLD" &          # un editor con el archivo abierto
EDITOR=$!
wait_sock
check "la 2.5 está abierta" '[ -n "$(running)" ]'

echo "== 1. Instalar la $NEW"
out=$(inst)
echo "$out" | sed 's/^/     /'
check "la 2.5 se cerró" '[ -z "$(running)" ]'
check "el editor con el archivo abierto sigue abierto" 'kill -0 $EDITOR'
check "la 2.5 ya no está en su sitio" '[ ! -e "$OLD" ]'
check "copia de la 2.5 apartada" '[ -f "$HOME/.clima_pi/version_anterior/clima_pi_2.5.py" ]'
check "$NEW instalada" 'grep -q "^APP_VERSION = \"$NEW\"" "$D"'
check "el menú abre la nueva" 'grep -q "\"$D\"\$" "$MENU"'
check "se conserva el arranque al encender" 'grep -q "\"$D\" --background" "$AUTO"'
check "se conservan tus datos" '[ -f "$HOME/.clima_pi/config.json" ] && [ -f "$HOME/.clima_pi/40.417_-3.704/predicciones.db" ]'

echo "== 2. Idempotente: otra vez, con la $NEW abierta"
(cd "$HOME" && "$PY" "$D" >/dev/null 2>&1 &)
wait_sock
pid=$(version | awk '{print $3}'); export pid
check "la $NEW responde" '[ -n "$pid" ]'
before=$(footprint); export before
sleep 1
for n in 2 3; do
    out=$(inst)
    check "vez $n: «nada que cambiar»" 'echo "$out" | grep -q "no había nada que cambiar"'
    check "vez $n: no cierra la ventana abierta" '[ "$(version | awk "{print \$3}")" = "$pid" ]'
    check "vez $n: no toca ningún archivo" '[ "$before" = "$(footprint)" ]'
    [ "$before" = "$(footprint)" ] || { echo "$before" > "$HOME/a"; footprint > "$HOME/b"; diff "$HOME/a" "$HOME/b"; }
done

echo "== 3. Cambiar el arranque, también idempotente"
out=$(inst --no-autostart)
check "--no-autostart lo quita" '[ ! -e "$AUTO" ] && echo "$out" | grep -q "instalado"'
out=$(inst --no-autostart)
check "--no-autostart otra vez: nada que cambiar" 'echo "$out" | grep -q "no había nada que cambiar"'
out=$(inst)
check "sin opción: respeta que no arranque solo" '[ ! -e "$AUTO" ] && echo "$out" | grep -q "no había nada"'
out=$(inst --autostart)
check "--autostart lo pone" '[ -f "$AUTO" ]'

echo "== 4. Una ventana colgada se cierra a la fuerza"
cp "$REPO/clima_pi.py" "$HOME/otra_clima_pi.tmp" && mv "$HOME/otra_clima_pi.tmp" "$HOME/.local/share/clima-pi/clima_pi.py.nueva"
printf '\n# cambio\n' >> "$HOME/.local/share/clima-pi/clima_pi.py.nueva"
mkdir -p "$HOME/dl" && cp "$HOME/.local/share/clima-pi/clima_pi.py.nueva" "$HOME/dl/clima_pi.py"
cp "$REPO/install.sh" "$HOME/dl/install.sh"
rm "$HOME/.local/share/clima-pi/clima_pi.py.nueva"
for p in $(running); do kill -STOP "$p"; done
t=$(date +%s)
sh "$HOME/dl/install.sh" --no-start >/dev/null 2>&1
check "colgada cerrada ($(( $(date +%s) - t )) s)" '[ -z "$(running)" ]'
check "instalada la versión nueva" 'tail -n 1 "$D" | grep -q "# cambio"'

echo "== 5. Desinstalar y borrar, dos veces cada uno"
out=$(inst --uninstall)
check "desinstalar quita todo" '[ ! -e "$D" ] && [ ! -e "$MENU" ] && [ ! -e "$AUTO" ]'
check "y deja tus datos" '[ -f "$HOME/.clima_pi/config.json" ]'
out=$(inst --uninstall); st=$?; export st
check "desinstalar otra vez: «nada que quitar»" '[ $st = 0 ] && echo "$out" | grep -q "nada que quitar"'
inst --purge >/dev/null; st=$?; export st
check "--purge borra tus datos" '[ $st = 0 ] && [ ! -e "$HOME/.clima_pi" ]'
inst --purge >/dev/null; st=$?; export st
check "--purge otra vez no falla" '[ $st = 0 ]'

kill $EDITOR 2>/dev/null
rm -rf "$HOME"
echo
if [ $FAIL = 0 ]; then echo "INSTALADOR: TODO BIEN"; else echo "INSTALADOR: HAY FALLOS"; exit 1; fi
