# Changelog — sistema_emision_Astra

Todas las modificaciones relevantes del proyecto. Sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y versionado semántico ligero (pre-1.0: cualquier bump es válido).

---

## [Sin versionar] — 2026-11-04 — Reinicio de canal que no levantaba: `stop()` abortaba a medias y dejaba huérfanos

### Resumen en una línea
El botón "Reiniciar" del panel dejaba el canal caído y publicaba en paralelo un ffmpeg huérfano: `stop()` lanzaba `subprocess.TimeoutExpired` (que hereda de `SubprocessError`, **no** de `OSError`), abortando el reinicio a mitad de camino.

### Síntoma reportado
Canal `<canal>` (`db/<instancia>.ini`) registrado pero "no levantaba" tras pulsar *Reiniciar*. El SRS rechazaba al publisher nuevo con `StreamBusy (1028)` porque el ffmpeg viejo seguía publicando a `rtmp://127.0.0.1:1935/live/<stream>`.

### Causa raíz (confirmada con traceback en `/var/log/astra-emision.log`)
```
File "stream_manager.py", line 612, in stop
    proc.wait(timeout=5)
subprocess.TimeoutExpired: Command '[... ffmpeg ... -f nut /tmp/astra_nv/<otro-canal>...nut]' timed out after 5 seconds
During handling of the above exception, another exception occurred:
...
File "stream_manager.py", line 615, in stop
    proc.wait(timeout=5)
```
- El `except (OSError, ValueError)` del bloque de la etapa NVIDIA **no cubría** `TimeoutExpired`.
- El proceso se "olvidaba" (`self._stage_process = None`) **antes** de comprobar su muerte → el ffmpeg sobrevivía sin control.
- La excepción abortaba `stop()` → `restart_stream()` **nunca llegaba a `start()`** → el canal se quedaba caído.
- El `except Exception` del monitor se comía la excepción y seguía: bucle silencioso.

### Cambios aplicados
| Archivo | Cambio |
|---|---|
| `stream_manager.py` | `_kill_process_tree()`: termina por grupo de procesos (`start_new_session=True` + `killpg`), **nunca lanza**, y devuelve `False` si el proceso sobrevive a SIGKILL. |
| `stream_manager.py` | `stop()` reescrito: mata antes de soltar la referencia, no propaga excepciones, registra los pids supervivientes en `_leaked_pids` y borra el fifo siempre. |
| `stream_manager.py` | `_read_output()` captura el proceso localmente: el hilo de la generación anterior ya no roba el `stderr` del proceso nuevo (provocaba "congelado" falsos). |
| `stream_manager.py` | `_read_stage_output()`: la salida de un proceso ya no controlado va a `orphan_lines` en vez de `last_lines`; el monitor ya no atribuye el `Conversion failed!` de un huérfano al stream vivo (causa del bucle de reinicio de ~11 s). |
| `stream_manager.py` | `restart_stream()`: aunque `stop()` falle, **siempre** se llega a `start()`. |
| `stream_manager.py` | `reap_orphans()`: elimina ffmpeg que publican a nuestras URLs sin control (solo los que **no** son hijos de este proceso: red de seguridad). `kill_leaked()` y `cleanup_stale_pipes()` para los pids y fifos sobrantes. |
| `stream_manager.py` | `kill_foreign_publishers()`: antes de arrancar, mata al ffmpeg ajeno que publica a la misma `Destination_URL` (evita el `StreamBusy`). |
| `app.py`, `app_hot.py` | Arranque en 3 fases: registrar → reaper → arrancar. Ruta `/api/stream/<n>/restart` devuelve el error real en JSON en vez de un 500 vacío. |
| `monitor.py` | Barrido periódico de huérfanos y pipes obsoletos (`REAP_INTERVAL=120 s`). |
| `/etc/systemd/system/<instancia>_A.service` | `After/Wants=docker.service`, `ExecStartPre=tools/wait-for-deps.sh`, `Restart=always` (antes `on-failure`). |

### Verificación post-despliegue
```bash
# Reinicio del canal (antes: se quedaba caido)
curl -sS -X POST -H "X-Astra-Token: $(cat .api_token.<instancia>)" \
     http://127.0.0.1:5006/api/stream/<canal>/restart
#   → {"name":"<canal>","success":true}   en 2,3 s

curl -sS http://127.0.0.1:5006/api/status | jq '.streams["<canal>"]'
#   → status=running, alive=true, bitrate=1838.8 KBITS/S, rc=1, error=""
```
- 38/38 streams vivos, **0 huérfanos**, **0 pipes obsoletos** en `/tmp/astra_nv`.
- El reaper detectó y eliminó un huérfano real (`live/<otro-stream>`) que habría dado `StreamBusy`.
- Test de regresión: `/tmp/kilo/test_fix_huerfanos.py` (6 grupos, todos en verde).

### Nota
El mismo código se comparte con el otra instancia (`112_A`/`112_B`): el fix llega por sincronización, pero **cada servicio necesita su propio `systemctl restart`** para cargar el código nuevo.

---

## [Sin versionar] — 2026-10-14 — Rename de unidades por colisión de prefijo

### Resumen en una línea
`astra-emision.service` → `<instancia>_A.service` (puerto 5006, `db/<instancia>_A.ini`) y `astra-emision-hot.service` → `<instancia>_B.service` (puerto 5007, `db/<instancia>_B.ini`). El prefijo genérico `astra-emision-*` colisionaba con el sistema de sincronización del operador y podía provocar falsos positivos.

### Cambio aplicado
- `db/<instancia>.ini` → **`db/<instancia>_A.ini`**; `Service=` actualizado a `<instancia>_A.service`.
- `db/<instancia>_hot.ini` → **`db/<instancia>_B.ini`**; `Service=` actualizado a `<instancia>_B.service`.
- `/etc/systemd/system/astra-emision.service` → **`/etc/systemd/system/<instancia>_A.service`**, con `Description=` y `Environment=ASTRA_INI=db/<instancia>_A.ini` actualizados.
- `/etc/systemd/system/astra-emision-hot.service` → **`/etc/systemd/system/<instancia>_B.service`**, ídem.
- Symlinks `/etc/systemd/system/multi-user.target.wants/astra-emision*.service` regenerados con los nombres nuevos.
- `docs/INSTANCIAS_MULTIPLES.md`: reescritas §2 (estado actual), §3.3 (ejemplos), §8 (tabla de unidades), §8.6/8.7 (verificación + workaround), y añadida nueva **§9** con el procedimiento de rename paso-a-paso y rollback para replicarlo en otros hosts.

### Por qué NO se tocó el código
- `app.py` y `app_hot.py` no se modificaron. La asignación puerto→instancia sigue siendo por `ASTRA_PORT`/default del binario, igual que antes.
- El cambio es puramente de naming (archivos + `Environment=` + `Service=` en INI). Defaults intactos.

### Verificación post-migración
```bash
# (1) puertos correctos
ss -tlnp | grep -E ':(5006|5007)\s'
#   → 5006 = <instancia>_A.service (app.py)
#   → 5007 = <instancia>_B.service (app_hot.py)

# (2) /api/restart_platform devuelve method=systemd con el nombre nuevo
curl -sS -X POST http://127.0.0.1:5006/api/restart_platform | jq .
#   → {"method":"systemd","service":"<instancia>_A.service","success":true,...}

# (3) symlinks regenerados
ls -la /etc/systemd/system/multi-user.target.wants/ | grep -E 'astra|<instancia>_'
#   → solo <instancia>_A.service y <instancia>_B.service; los astra-* ya no existen

# (4) Service= en cada INI
grep '^Service=' db/<instancia>_A.ini db/<instancia>_B.ini
#   → <instancia>_a.ini:Service=<instancia>_A.service
#   → <instancia>_b.ini:Service=<instancia>_B.service
```

### Cosas a tener en cuenta al replicarlo en otros hosts
- Logs: `/var/log/astra-emision*.log` se mantienen con nombre antiguo por Compatibilidad (no perder histórico). Si quieres renombrarlos hazlo en otro commit/ventana.
- Orden de stop: `hot` primero, luego frío (ver §9.4 del doc).
- `RestartSec=5` implica que tras pulsar Reiniciar Plataforma el panel tarda 5s + tiempo_ffmpeg_setup en volver (típicamente 5–25 s).
- Si `config.json > active_config` apunta a un INI renombrado, actualizar **antes** del `daemon-reload + enable --now`.
- Procedimiento completo (incluido rollback): `docs/INSTANCIAS_MULTIPLES.md` §9.

### Impacto / riesgo
- **Riesgo**: bajo. Tiempo de indisponibilidad combinado ≈ 3–4 min (stop + rename + start × 2 instancias × startup time).
- **Beneficio**: el nombre del servicio ya no compite con el sistema de sync externo; el botón Reiniciar Plataforma queda alineado a la unidad correcta y se elimina la posibilidad de que `restart_platform.sh` global ejecute pkill cruzados.

---

## [Sin versionar] — 2026-10-14 — Alineación restart_platform por instancia

### Resumen en una línea
Cada INI que se levanta como instancia systemd debe declarar `Service=<nombre>.service` en su bloque `[servidor]` para que el botón **Reiniciar plataforma** del panel reinicie **esa** instancia y no la default (5006).

### Problema diagnosticado
- `SERVICE_NAME = ""` por default en ambos binarios (`app.py:294`, `app_hot.py:294`).
- Sólo se rellena desde el INI activo si existe `[servidor] Service=...` (`app.py:186-187`, `config_parser.py:206-207`).
- Fallback: `restart_platform.sh` está **cableado al 5006** (`fuser -k 5006/tcp`, `nohup python3 app.py`). Si una instancia (p.ej. 5007) no declara `Service=...`, su botón Reiniciar Plataforma:
  1. Mata **todos** los ffmpeg del host (`pkill -9 -f 'ffmpeg -re'`).
  2. Mata y re-lanza **solo** `astra-emision.service` en 5006.
  3. Deja la instancia hot con sus streams muertos y sin orquestador.

### Cambio aplicado
- `db/<instancia>_hot.ini` (instancia hot, puerto 5007) → añadido al bloque `[servidor]`:
  ```ini
  Service=astra-emision-hot.service
  ```
- `db/<instancia>.ini` (instancia principal, puerto 5006) → **ya tenía** `Service=astra-emision.service`; sólo se documenta aquí.
- `docs/INSTANCIAS_MULTIPLES.md` → sección añadida **"Checklist de alineación para otros hosts"** con el procedimiento a replicar.

### Por qué NO se tocó el código
- El patrón "defaults del binario + overrides por entorno/.ini" se mantiene intacto. No se modificó `app.py`, `app_hot.py`, ni `config_parser.py`.
- `restart_platform.sh` queda como fallback legacy; cualquier nueva instancia debería ascender al camino systemd añadiendo `Service=` en su INI.

### Reproducir la verificación post-fix
```bash
# 1) Comprobar que el INI del 5007 ya lleva Service=
grep -A6 '^\[servidor\]' db/<instancia>_hot.ini | grep '^Service='

# 2) Comprobar el endpoint (debe devolver method=systemd tras pulsar el botón)
curl -s -X POST http://127.0.0.1:5007/api/restart_platform | jq .

# 3) journal confirmando el reinicio del servicio correcto
sudo journalctl -u astra-emision-hot.service -n 20 --no-pager
```

### Aplicar en otros hosts (resumen)
Ver `docs/INSTANCIAS_MULTIPLES.md` → **Checklist de alineación para otros hosts**. En una línea por instancia:

```bash
# Por cada <inst>.service que se levante:
echo "Service=<inst>.service" >> db/<inst>.ini      # bajo [servidor]
sudo systemctl restart astra-emision-<inst>.service
curl -s -X POST http://127.0.0.1:<puerto>/api/restart_platform | jq .
```

### Impacto / riesgo
- **Riesgo**: bajo. Si `sudo -n systemctl restart` falla, el código aún cae al fallback (peor comportamiento, no peor estabilidad).
- **Beneficio**: el botón Reiniciar pasa a ser per-instancia determinista; elimina el efecto secundario de matar ffmpegs cruzados.
