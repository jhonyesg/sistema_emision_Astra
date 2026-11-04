#!/bin/bash
# Reinicia la plataforma de emision (app.py puerto 5006)
# Espera a que el proceso actual termine, mata SOLO los ffmpeg de esta
# instancia, y vuelve a arrancar con el INI correcto.

LOG_FILE="${1:-/tmp/kilo/emision_app.log}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# INI de esta instancia (112_A -> db/112_a.ini). Si no se define, se usa
# db/112_a.ini. Antes arrancaba sin ASTRA_INI y cargaba el 'active_config'
# de config.json (el INI equivocado).
export ASTRA_INI="${ASTRA_INI:-db/112_a.ini}"

# PID de la app que lanzo este script: sus hijos son los ffmpeg de ESTA
# instancia. Antes se hacia 'pkill -9 -f "ffmpeg -re"' global, que mataba
# los ffmpeg de la otra instancia (112_B) y los del grabador.
APP_PID="$PPID"

# Esperar a que el proceso actual (que llama os._exit) termine
sleep 3

# Matar solo los ffmpeg huerfanos de esta instancia
if [ -n "$APP_PID" ] && [ "$APP_PID" != "1" ]; then
    pkill -9 -P "$APP_PID" 2>/dev/null
fi
sleep 2

# Verificar que el puerto 5006 no este en uso
if ss -tlnp | grep -q ':5006'; then
    fuser -k 5006/tcp 2>/dev/null
    sleep 2
fi

# Arrancar la app de nuevo
cd "$SCRIPT_DIR"
nohup python3 app.py > "$LOG_FILE" 2>&1 &

echo "[$(date)] Plataforma reiniciada (ASTRA_INI=$ASTRA_INI), PID=$!" >> "$LOG_FILE"
