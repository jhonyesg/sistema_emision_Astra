#!/bin/bash
# Espera las dependencias de las instancias de emision antes de arrancar.
#
# Sin esto, al arrancar el equipo:
#   - el SRS (Docker) todavia no escucha en 127.0.0.1:1935  -> los streams
#     con Destination_URL=rtmp://127.0.0.1:1935/... nacen muertos con
#     "Error opening output files: Connection refused"
#   - la red (WAN) aun no responde -> los streams que leen de una
#     fuente HTTP/HLS externa mueren al abrir la entrada
#
# Uso: wait-for-deps.sh [timeout_total_s] [puerto_srs] [puerto_panel]
set -u

TOTAL_TIMEOUT="${1:-120}"
# Host de referencia para comprobar que el DNS publico responde.
DNS_CHECK_HOST="${DNS_CHECK_HOST:-one.one.one.one}"
SRS_PORT="${2:-1935}"
PANEL_PORT="${3:-1985}"
DEADLINE=$(( $(date +%s) + TOTAL_TIMEOUT ))

log() { echo "[wait-for-deps] $*"; }

# 1. Red: ruta por defecto + DNS + salida a Internet
wait_net() {
    local i=0
    while [ "$i" -lt 30 ]; do
        if ip route get 8.8.8.8 >/dev/null 2>&1 \
           && getent hosts "$DNS_CHECK_HOST" >/dev/null 2>&1; then
            log "red y DNS OK"
            return 0
        fi
        sleep 2; i=$((i+1))
    done
    log "AVISO: la red/DNS no respondio tras 60s (se continua)"
    return 1
}

# 2. Puerto TCP local (SRS en Docker)
wait_port() {
    local port="$1" name="$2" i=0
    while [ "$i" -lt 60 ]; do
        if (exec 3<>/dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
            exec 3<&- 3>&-
            log "$name escuchando en 127.0.0.1:$port"
            return 0
        fi
        sleep 2; i=$((i+1))
    done
    log "AVISO: $name no escucha en 127.0.0.1:$port tras 120s (se continua)"
    return 1
}

wait_net
wait_port "$SRS_PORT" "SRS (RTMP)"

# El panel del contenedor es opcional: si no responde, se continua igual.
if ! wait_port "$PANEL_PORT" "panel SRS"; then
    log "El panel SRS no responde; se arranca la emision de todos modos"
fi

# 3. Margen final para que el SRS termine de registrar los vhosts
if [ "$(date +%s)" -lt "$DEADLINE" ]; then
    sleep 3
fi

log "dependencias listas"
exit 0
