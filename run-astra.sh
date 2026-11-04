#!/bin/bash
# Wrapper único para el servicio Astra RTMP.
# (necesario porque systemd --user no hereda variables de la sesión interactiva;
#  copiamos lo mínimo imprescindible para ffmpeg + DBus).
#
# NOTA: las rutas eran /home/<usuario>/... (usuario de otro equipo) y con
# 'set -e' el script abortaba en este host. Ahora se derivan de la ubicacion
# del propio script.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

if [ -f "$XDG_RUNTIME_DIR/pulse/pulse.env" ]; then
    set -a
    . "$XDG_RUNTIME_DIR/pulse/pulse.env"
    set +a
fi

if [ -z "$PULSE_SERVER" ] && [ -S "$XDG_RUNTIME_DIR/pulse/native" ]; then
    export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
fi

if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi

# Puerto configurable (default 5006)
: "${ASTRA_PORT:=5006}"
export ASTRA_PORT

# INI activo (priority 1 sobre config.json)
: "${ASTRA_INI:=}"
[ -n "$ASTRA_INI" ] && export ASTRA_INI

mkdir -p "$HOME/.astra-stream"

cd "$SCRIPT_DIR"
exec /usr/bin/python3 app.py
