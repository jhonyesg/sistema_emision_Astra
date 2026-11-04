#!/bin/bash
# Mantenimiento del disco de grabaciones (/dev/sdc1 -> /mnt/grabador/radios2).
#
# CONTEXTO
#   El disco de 10 TB (ST10000NM0156) esta degradado:
#     - dmesg: "failed command: WRITE FPDMA QUEUED", "ATA bus error",
#              "hard resetting link", "qc timeout after 5000 msecs"
#     - ext4: "Corrupt filesystem", "bad block bitmap checksum",
#             "Filesystem failed CRC" (01:30, 05:42, 10:04)
#     - SMART: UDMA_CRC=0, Pending=0, Uncorrectable=0 -> no es el cable ni
#              sectores malos: es perdida/reordenacion de escrituras cuando el
#              enlace se cae -> corrupcion silenciosa del metadata.
#
# QUE HACE
#   1. Diagnostico SMART completo
#   2. fsck del filesystem (REQUIERE que el grabador este parado)
#   3. Remonta y deja el sistema listo
#
# USO
#   sudo ./tools/disk-maintenance.sh            # solo diagnostico (sin parar nada)
#   sudo ./tools/disk-maintenance.sh --fsck     # con fsck (para las grabaciones)
#
# El fsck sobre 5,7 TB puede tardar HORAS. No lo ejecutes en horario de
# emision sin avisar.
set -euo pipefail

DEV="${DEV:-/dev/sdc1}"
MNT="${MNT:-/mnt/grabador/radios2}"
LOG="/var/log/disk-maintenance.log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "=== Diagnostico SMART ==="
smartctl -A "$DEV" 2>&1 | grep -iE "Reallocated|Pending|Uncorrectable|Command_Timeout|G-Sense|CRC|Error_Rate|Start_Stop|Power_On" | tee -a "$LOG"

log "=== Estado del filesystem ==="
tune2fs -l "$DEV" 2>&1 | grep -iE "Filesystem state|Errors behavior|Mount count|Last mount time|Last checked" | tee -a "$LOG"

if [ "${1:-}" != "--fsck" ]; then
    log "Solo diagnostico. Usa --fsck para reparar (requiere parar el grabador)."
    exit 0
fi

log "AVISO: reparando $DEV. El grabador debe estar parado."
read -rp "¿Confirmas que el grabador esta parado y no hay nadie grabando? [s/N] " ok
[ "$ok" = "s" ] || { log "Cancelado"; exit 1; }

# Parar el grabador (no los emisores: la emision no depende de el)
# El grabador se localiza por nombre de unidad, no se fija en el codigo:
# asi el script funciona en cualquier nodo.
GRABADOR_UNIT="${GRABADOR_UNIT:-$(systemctl list-units --type=service --all --no-legend 2>/dev/null | awk '$1 ~ /grabador/ {print $1; exit}')}"
[ -n "$GRABADOR_UNIT" ] && log "Grabador detectado: $GRABADOR_UNIT"
systemctl stop "$GRABADOR_UNIT" 2>/dev/null || true
sleep 5

if mountpoint -q "$MNT"; then
    log "Desmontando $MNT"
    umount "$MNT" || { log "ERROR: no se pudo desmontar (hay procesos usando el disco)"; exit 1; }
fi

log "Ejecutando e2fsck -f -p (puede tardar horas en 5,7 TB)"
e2fsck -f -p "$DEV" 2>&1 | tee -a "$LOG"

log "Remontando"
mount "$MNT" || mount "$DEV" "$MNT"
systemctl start "$GRABADOR_UNIT" 2>/dev/null || true

log "Listo. Revisa el SMART y el estado del filesystem:"
log "  smartctl -A $DEV | grep -iE 'Reallocated|Pending|Uncorrectable'"
