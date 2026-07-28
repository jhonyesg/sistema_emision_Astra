#!/bin/bash
# Reinicia la plataforma de emision (app.py puerto 5006)
# Espera a que el proceso actual termine, mata ffmpeg huerfanos, y vuelve a arrancar.

LOG_FILE="${1:-/tmp/kilo/emision_app.log}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Esperar a que el proceso actual (que llama os._exit) termine
sleep 3

# Matar cualquier ffmpeg huerfano
pkill -9 -f 'ffmpeg -re' 2>/dev/null
sleep 2

# Verificar que el puerto 5006 no este en uso
if ss -tlnp | grep -q ':5006'; then
    # Matar lo que tenga el puerto
    fuser -k 5006/tcp 2>/dev/null
    sleep 2
fi

# Arrancar la app de nuevo
cd "$SCRIPT_DIR"
nohup python3 app.py > "$LOG_FILE" 2>&1 &

echo "[$(date)] Plataforma reiniciada, PID=$!" >> "$LOG_FILE"