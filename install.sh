#!/bin/sh
# Instala Clima Pi en la Raspberry Pi (Raspberry Pi OS Bookworm o Trixie; sirve en cualquier
# Linux con escritorio). Sin terminal: doble clic en «Instalar Clima Pi».
#
#   sh install.sh               instala o actualiza (quita antes la versión anterior)
#   sh install.sh --uninstall   lo quita (tus datos de ~/.clima_pi se quedan)
#   sh install.sh --purge       lo quita y borra también tus datos
#
# Opciones:
#   --autostart      que arranque solo al encender el Pi
#   --no-autostart   que no arranque solo (por defecto se respeta lo que tenías)
#   --no-start       no abrir Clima Pi al terminar
#
# Ejecútalo como tu usuario normal (sin sudo): solo pide la contraseña para instalar
# paquetes con apt si falta alguno. Ejecutarlo otra vez nunca duplica nada.
# shellcheck disable=SC2086  # listas de paquetes y de procesos: se separan a propósito
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/clima_pi.py"
PY=${PYTHON:-python3}
APP_ID=clima-pi
DEST_DIR="$HOME/.local/share/clima-pi"
DEST="$DEST_DIR/clima_pi.py"
DATA="$HOME/.clima_pi"
MENU="$HOME/.local/share/applications/$APP_ID.desktop"
AUTOSTART="$HOME/.config/autostart/$APP_ID.desktop"
BACKUP="$DATA/version_anterior"
NEEDED="python3-tk python3-numpy"
TRAY="gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pil"

say() { printf '\n==> %s\n' "$*"; }

UNINSTALL="" PURGE="" AUTO="keep" START=1
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --purge) UNINSTALL=1 PURGE=1 ;;
        --autostart) AUTO=on ;;
        --no-autostart) AUTO=off ;;
        --no-start) START="" ;;
        *) echo "Opción desconocida: $arg"; exit 2 ;;
    esac
done

# Con sudo los archivos irían al usuario root, no al tuyo: volver a lanzarlo como tú
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    echo "No hace falta sudo: lo ejecuto como $SUDO_USER (pedirá la contraseña si falta algún paquete)."
    exec sudo -u "$SUDO_USER" -H sh "$0" "$@"
fi

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi
}

# ------------------------------------------------------------ quitar la versión anterior
running_pids() {
    # Copias de Clima Pi de este usuario (ventana e icono de la barra), de cualquier versión y
    # estén donde estén guardadas: Python ejecutando clima_pi.py. No toca un editor (Thonny,
    # Geany…) que solo tenga el archivo abierto.
    "$PY" - <<'EOF'
import os
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        if os.stat("/proc/" + pid).st_uid != os.getuid():
            continue
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            argv = [a for a in f.read().decode(errors="replace").split("\0") if a]
    except OSError:
        continue
    if not argv or "python" not in os.path.basename(argv[0]):
        continue
    args = [a for a in argv[1:] if not a.startswith("-")]
    if args and os.path.basename(args[0]) == "clima_pi.py":
        print(pid)
EOF
}

stop_running() {
    pids=$(running_pids)
    [ -z "$pids" ] && return 0
    say "Cerrando Clima Pi, que está abierto"
    # primero por las buenas (guarda lo que tenga a medias), como el botón Salir
    "$PY" - <<'EOF' 2>/dev/null || true
import os, socket
for base in (os.environ.get("XDG_RUNTIME_DIR"), os.path.expanduser("~/.clima_pi")):
    if not base:
        continue
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(12)
        s.connect(os.path.join(base, "clima-pi.sock"))
        s.sendall(b"quit\n")
        s.recv(64)
        s.close()
    except OSError:
        pass
EOF
    i=0
    while [ -n "$(running_pids)" ] && [ $i -lt 20 ]; do sleep 0.5; i=$((i + 1)); done
    pids=$(running_pids)
    if [ -n "$pids" ]; then                         # colgado: a la fuerza
        kill $pids 2>/dev/null || true
        sleep 2
        pids=$(running_pids)
        if [ -n "$pids" ]; then kill -9 $pids 2>/dev/null || true; fi
    fi
    echo "Cerrado."
}

old_scripts() {
    # El .py al que apuntan las entradas del menú y de arranque (la versión anterior lo
    # dejaba allí donde lo guardaste: Escritorio, carpeta personal, Thonny…)
    for f in "$MENU" "$AUTOSTART"; do
        [ -f "$f" ] || continue
        sed -n 's/^Exec=.*"\([^"]*clima_pi\.py\)".*/\1/p' "$f"
    done | sort -u
}

other_versions() {
    # Copias de otra versión a las que apunta el menú (ni la instalada aquí ni la que se instala)
    old_scripts | while IFS= read -r old; do
        if [ -f "$old" ] && [ "$old" != "$DEST" ] && [ "$old" != "$SRC" ]; then
            printf '%s\n' "$old"
        fi
    done
}

set_aside_other_versions() {
    list=$(mktemp)
    other_versions > "$list" || true
    while IFS= read -r old; do                  # (rutas con espacios incluidas)
        mkdir -p "$BACKUP"
        ver=$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' "$old" | head -n 1)
        mv "$old" "$BACKUP/clima_pi_${ver:-anterior}.py"
        echo "Versión anterior apartada (no se borra, por si acaso):"
        echo "  $old  →  $BACKUP/clima_pi_${ver:-anterior}.py"
    done < "$list"
    rm -f "$list"
}

desktop_state() {
    # huella de todo lo que el instalador toca, para saber si algo cambió
    cat "$DEST" "$MENU" "$AUTOSTART" 2>/dev/null | cksum
}

if [ -n "$UNINSTALL" ]; then
    say "Desinstalando Clima Pi"
    if [ -z "$(running_pids)" ] && [ ! -e "$DEST_DIR" ] && [ ! -e "$MENU" ] && [ ! -e "$AUTOSTART" ]; then
        echo "No está instalado: nada que quitar."
    else
        stop_running
        set_aside_other_versions
        rm -rf "$DEST_DIR"
        rm -f "$MENU" "$AUTOSTART" "$DATA/$APP_ID.png" "$DATA/estado.json" "$DATA/$APP_ID.sock" \
              "${XDG_RUNTIME_DIR:-/nonexistent}/$APP_ID.sock"
        echo "Quitado del menú y del arranque."
    fi
    if [ -n "$PURGE" ]; then
        if [ -e "$DATA" ]; then
            rm -rf "$DATA"
            echo "Borrados también tus datos ($DATA)."
        fi
    elif [ -e "$DATA" ]; then
        echo "Tus datos siguen en $DATA (historial, modelo entrenado, lecturas del sensor)."
        echo "Para borrarlos también: sh $0 --purge"
    fi
    say "Listo: Clima Pi desinstalado"
    exit 0
fi

# ------------------------------------------------------------ instalar
if [ ! -f "$SRC" ]; then
    echo "No encuentro clima_pi.py junto a este instalador ($HERE)."
    exit 1
fi
NEW_VER=$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' "$SRC" | head -n 1)

say "Comprobando lo que necesita (Python, tkinter, numpy)"
missing=""
"$PY" -c "import tkinter" 2>/dev/null || missing="$missing python3-tk"
"$PY" -c "import numpy" 2>/dev/null || missing="$missing python3-numpy"
if [ -n "$missing" ]; then
    echo "Instalo:$missing"
    as_root apt-get update -qq || true
    as_root apt-get install -y $missing
fi
"$PY" -c "import tkinter, numpy" || { echo "Sigue faltando algo de: $NEEDED"; exit 1; }
# el icono con la temperatura en la barra es opcional: si no se puede, se sigue sin él
if ! "$PY" -c "import gi, PIL" 2>/dev/null; then
    echo "Instalo lo del icono de la barra (opcional): $TRAY"
    as_root apt-get install -y $TRAY 2>/dev/null || echo "(sin icono en la barra: no pasa nada)"
fi
echo "Todo en su sitio."

before=$(desktop_state)
case "$AUTO" in                                 # por defecto, lo que tuvieras
    on) AUTO_FLAG=--autostart ;;
    off) AUTO_FLAG=--no-autostart ;;
    *) if [ -f "$AUTOSTART" ]; then AUTO_FLAG=--autostart; else AUTO_FLAG=""; fi ;;
esac

if cmp -s "$SRC" "$DEST" && [ -z "$(other_versions)" ]; then
    say "Clima Pi $NEW_VER ya está instalado en $DEST"
else
    say "Quitando la versión anterior"
    stop_running
    set_aside_other_versions
    say "Instalando Clima Pi $NEW_VER en $DEST"
    mkdir -p "$DEST_DIR"
    cp "$SRC" "$DEST.tmp"
    chmod 755 "$DEST.tmp"
    mv "$DEST.tmp" "$DEST"                      # sustitución atómica
fi
# menú y arranque: se reescriben solo si cambian
"$PY" "$DEST" --menu-entry $AUTO_FLAG
if [ -f "$AUTOSTART" ]; then
    echo "En el menú de aplicaciones, y arrancará solo al encender el Pi."
else
    echo "En el menú de aplicaciones (Accesorios / Ciencia)."
fi
# tus datos (historial, modelo, sensor) se conservan; el modelo de una versión anterior se
# reentrena solo la primera vez (unos minutos)

if [ -n "$START" ] && [ -z "$(running_pids)" ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    say "Abriendo Clima Pi"
    nohup "$PY" "$DEST" >/dev/null 2>&1 &
fi
if [ "$before" = "$(desktop_state)" ]; then
    say "Listo: no había nada que cambiar"
else
    say "Listo: Clima Pi $NEW_VER instalado"
fi
echo "Para quitarlo: sh $0 --uninstall"
