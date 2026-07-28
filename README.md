# Sistema de Emisión 24/7-Astra

Dashboard web para gestión y monitoreo de streams FFmpeg en tiempo real. Control total de múltiples transmisiones simultáneas con monitoreo automático, detección de freeze por inactividad de frames, reinicio a medianoche, métricas de sistema (CPU/RAM/red) y limpieza de memoria.

---

## Arquitectura del Sistema

```
┌────────────────────────────────────────────────────────────────────┐
│                         Flask App (app.py)                          │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────┐      │
│  │ API REST │  │ MidNight     │  │ StreamMonitor            │      │
│  │ /api/*   │  │ Scheduler    │  │ (cada 10s)               │      │
│  └──────────┘  └──────┬───────┘  └──────────┬───────────────┘      │
│                       │                      │                      │
│   get_system_stats()  │                      │                      │
│   (cpu/ram/net)       │                      │                      │
└───────────────────────┼──────────────────────┼──────────────────────┘
                        │                      │
          ┌─────────────┴──────────┐   ┌────────┴────────┐
          │   StreamManager        │   │  config.json    │
          │   (Singleton)          │   │  (persistencia) │
          │   + Lock global        │   └─────────────────┘
          └─────────────┬──────────┘
                        │
          ┌─────────────┴─────────────────────────────────┐
          │                   streams {}                   │
          │  ┌─────────────┐ ┌─────────────┐ ┌─────────┐  │
          │  │ Stream      │ │ Stream      │ │ Stream  │  │
          │  │ Instance    │ │ Instance    │ │ Instance│  │
          │  │ [name1]     │ │ [name2]     │ │ [nameN] │  │
          │  └──────┬──────┘ └──────┬──────┘ └────┬────┘  │
          └─────────┼───────────────┼─────────────┼───────┘
                    │               │             │
              ┌─────┴──────┐  ┌─────┴─────┐  ┌────┴─────┐
              │ ffmpeg -re │  │ ffmpeg -re│  │ ffmpeg -re│
              │ stderr.raw │  │ stderr.raw│  │ stderr.raw│
              │ (split \r  │  │ (split \r │  │ (split \r │
              │  y \n)     │  │  y \n)    │  │  y \n)    │
              │ last_lines │  │ last_lines│  │ last_lines│
              │ (deque)    │  │ (deque)   │  │ (deque)   │
              └───────────┘  └───────────┘  └───────────┘
```

> **Lectura robusta de stderr:** el progreso de ffmpeg llega separado por `\r`, no `\n`. `_read_output` lee el stream con `read1(4096)` y parte tanto por `\r` como por `\n`, parseando `frame=`, `size=`, `time=`, `bitrate=`, `Audio: ... kbps` y `speed=`.

---

## Estado de un Stream

El campo `status` puede valer:

| Valor | Significado |
|-------|-------------|
| `stopped` | Proceso detenido o nunca iniciado |
| `starting` | Popen ejecutado, a la espera del primer progreso |
| `running` | FFmpeg emite progreso (`frame=`, `size=` o `time=` avanzando) |
| `error` | Proceso murió o no se pudo iniciar |

---

## Actualización Parcial del INI

Cuando se guarda el archivo `cadena_rcn.ini` via la UI (`POST /api/ini/write`):

1. Se hace una copia del contenido anterior en `cadena_rcn.ini.bak`.
2. Se valida el contenido nuevo en un archivo temporal (escritura atómica `tmp + fsync + rename`).
3. Se compara la configuración anterior con la nueva.
4. Solo se afectan los streams que cambiaron:
   - **Streams añadidos** → se registran e inician (si `autostart=true` o `start_all_on_boot=true`)
   - **Streams eliminados** → se detienen y se borran del manager
   - **Streams modificados** → se reinician con la nueva config (solo si estaban running)
   - **Streams sin cambios** → no se tocan
5. La respuesta JSON incluye `added`, `changed` y `removed` (listas de nombres).

---

## Flujo de Funcionamiento

### 1. Inicialización (app.py)

```
python app.py
    │
    ├── load_app_config()          → lee config.json
    │                                  (start_all_on_boot, midnight_restart,
    │                                   auto_restart, platform_title)
    │
    ├── initialize_streams()
    │       │
    │       └── Para cada sección en cadena_rcn.ini:
    │               ├── config_parser.get_all_streams()
    │               ├── stream_manager.register_stream(name, config)
    │               └── Si autostart=true OR START_ALL_ON_BOOT → start_stream(name)
    │                                                   └─ sleep(2) entre streams
    │
    ├── monitor.start()            → hilo daemon, check cada 10s
    ├── scheduler.start()          → hilo daemon que calcula medianoche siguiente
    └── app.run(host=0.0.0.0, port=5006, threaded=True)
```

### 2. Monitoreo en Tiempo Real (UI)

```
Browser (polling cada 2s)
    │
    └── GET /api/status
            │
            ├── stream_manager.get_all_status()
            │     └── {name: stream.get_info() for name, stream in streams.items()}
            │
            └── get_system_stats()
                  └── psutil: cpu_percent, virtual_memory, net_io_counters(enp2s0)
                              → upload_mbps, download_mbps

stream.get_info() retorna:
{
  name, status, bitrate, audio_bitrate, file_size, speed, uptime,
  restart_count, error_message, pid, last_lines[-50:]
}

Respuesta completa de /api/status:
{
  "streams": { ... },
  "summary": { "total", "running", "stopped", "all_running" },
  "system":  { "cpu_percent", "mem_percent",
               "net_interface", "upload_mbps", "download_mbps" }
}
```

### 3. Hilo _read_output (por cada stream activo)

```
subprocess.Popen(env={LIBVA_DRIVER_NAME=... si vaapi_driver})
    └── stderr.read1(4096)  ──►  split por \r y \n
                                   │
                                   ├── _parse_output(text)
                                   │     ├── frame=  → marca progreso (latido)
                                   │     ├── size=   → actualiza file_size y latido
                                   │     ├── time=   → valida rango, actualiza last y latido
                                   │     ├── bitrate=→ actualiza bitrate (kbits/s, Mbps, bps)
                                   │     ├── Audio:  → audio_bitrate
                                   │     ├── speed=  → speed
                                   │     └── error/failed → guarda error_message
                                   │
                                   └── last_lines.append("[HH:MM:SS] <línea>")  [deque maxlen=1000]
```

### 4. Detección de Freeze y Reinicio Automático

```
StreamMonitor (cada 10s)
    │
    ├── _check_freeze(name, status)
    │       │
    │       ├── Si status != "running"           → ignora
    │       ├── Si start_time es None            → ignora
    │       ├── Si now - start_time < 90s        → warmup, ignora
    │       │                                      (los streams IPTV/HLS tardan en
    │       │                                       emitir el primer frame válido)
    │       ├── Lee stream._last_frame_time
    │       │
    │       ├── Si now - last_frame_time ≤ 60s   → reset contador, OK
    │       └── Si now - last_frame_time > 60s   → contador++
    │                                              │
    │                                              └── contador ≥ 6 (≈60s adicionales)
    │                                                   → restart_stream(name, reason="frozen")
    │
    └── _check_process_health(name, status)
            └── Si status == "running" pero !is_running()
                    → captura get_last_error_from_logs()
                    → status = "error"
                    → restart_stream(name, reason="process_died")

restart_stream(name, reason):
    stop()  ──► error_history.append({type:"restart", reason, message, command})
    restart_count += 1
    sleep(2)
    start()
    └─ si falla → error_history.append({type:"error", reason:"start_failed"})
```

> Con `freeze_threshold=60s` + 6 checks consecutivos + 90s de warmup, el tiempo máximo antes de actuar sobre un stream realmente congelado es de aproximadamente **2 minutos y medio** desde el último progreso real.

### 5. Reinicio a Medianoche (Full Memory Clean)

```
Scheduler (hilo daemon)
    │
    └── Calcula next_midnight y duerme hasta entonces (con topes de 60s
        entre comprobaciones para liberar CPU).
            │
            └── Cuando now.hour == 0 and now.minute < 5 and !already_executed:
                    │
                    └── full_restart()
                            │
                            ├── stop_all_and_cleanup()
                            │       ├── stream.cleanup() × N streams
                            │       │       ├── stop() → terminate/wait(10s)/kill
                            │       │       ├── close pipes (stderr/stdout/stdin)
                            │       │       ├── last_lines.clear()
                            │       │       └── reset bitrate/size/speed/etc.
                            │       │
                            │       └── gc.collect()
                            │
                            ├── sleep(3)  ← pausa para liberación de recursos OS
                            │
                            └── stream.start() × N streams  ← reinicio limpio
```

Si el toggle `midnight_restart` está deshabilitado, el scheduler registra el evento pero no detiene los streams.

---

## Estructura de Archivos

```
sistema_emision/
├── app.py                  # Flask app + scheduler + initialization + /help
├── stream_manager.py       # StreamInstance + StreamManager (singleton)
├── monitor.py              # StreamMonitor (freeze/crash detection con warmup)
├── config_parser.py        # Parser del archivo INI
├── cadena_rcn.ini          # Configuración de streams (se edita desde UI)
├── cadena_rcn.ini.bak      # Backup automático antes de cada guardado
├── config.json             # Persistencia de toggles y platform_title
├── emisor_v1.sh            # Launcher con título personalizado para terminal
├── restart_platform.sh     # Script de reinicio completo del servicio
├── requirements.txt        # flask, psutil, markdown
├── templates/
│   └── index.html          # Dashboard con tabla, modales, toasts
├── static/
│   └── style.css           # Estilos completos
└── README.md               # Este archivo
```

---

## API REST

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/` | Dashboard HTML (título personalizado vía `platform_title`) |
| GET | `/help` | Documentación: renderiza este README.md como HTML |
| GET | `/api/status` | Estado de streams + `summary` + `system` (cpu/ram/red) |
| POST | `/api/stream/<name>/start` | Iniciar stream |
| POST | `/api/stream/<name>/stop` | Detener stream |
| POST | `/api/stream/<name>/restart` | Reiniciar stream individual |
| GET | `/api/logs/<name>?lines=N` | Obtener logs de un stream (N máx 1000) |
| GET | `/api/stream/<name>/command` | Ver comando FFmpeg generado + config |
| POST | `/api/start_all` | Iniciar todos los streams |
| POST | `/api/stop_all` | Detener todos los streams |
| POST | `/api/restart_platform` | Detener streams, lanzar `restart_platform.sh` y `os._exit(0)` |
| GET | `/api/config` | Obtener toggles + `platform_title` |
| POST | `/api/config` | Guardar toggles y/o `platform_title` |
| GET | `/api/ini/read` | Leer archivo INI |
| POST | `/api/ini/write` | Guardar INI (validación + backup + actualización parcial) |
| GET | `/api/errors` | Historial de últimos 5 reinicios/errores |

---

## Formato cadena_rcn.ini

```ini
[Nombre_Del_Stream]
Original_URL=https://fuente.com/stream
Destination_URL=rtmp://servidor.com/live/stream
FFMPEG_PRE_OPTIONS=-re -user_agent "Mozilla/5.0"
FFMPEG_POST_OPTIONS=-c copy -f flv
autostart=true
```

| Campo | Obligatorio | Descripción |
|-------|-------------|-------------|
| `Original_URL` | sí | Fuente a leer (HLS, RTMP, HTTP, etc.) |
| `Destination_URL` | sí | Destino donde reemitir (normalmente RTMP) |
| `FFMPEG_PRE_OPTIONS` | no | Flags antes de `-i`, ej. `-re -user_agent "..."` |
| `FFMPEG_POST_OPTIONS` | no | Flags después de `-i`, ej. `-c copy -f flv` |
| `autostart` | no | `true`/`false`. Iniciar al arranque (o en guardado del INI) |
| `ffmpeg_path` | no | Ruta al binario ffmpeg (default `ffmpeg`) |
| `vaapi_driver` | no | Driver VAAPI (ej. `i965`, `iHD`). Se exporta como `LIBVA_DRIVER_NAME` |

> El comando final siempre lleva `-re` antepuesto. Ejemplo:
> `ffmpeg -re -user_agent "Mozilla/5.0" -i <Original_URL> -c copy -f flv <Destination_URL>`

---

## Toggles de Configuración (config.json)

| Campo | Default | Efecto |
|-------|---------|--------|
| `start_all_on_boot` | `true` | Si true, al iniciar la app también arranca los streams con `autostart=false` |
| `auto_restart` | `true` | Si true, el monitor reinicia streams congelados o caídos |
| `midnight_restart` | `true` | Si true, a las 00:00–00:05 ejecuta `full_restart()` para liberar memoria |
| `platform_title` | `Sistema de Emisión 24/7-Astra` | Título del dashboard y de la pestaña del navegador (se sanitiza contra `<>"'&`, máx 100 chars) |

`platform_title` se puede editar desde la UI con el botón **✏️ Título**; al guardar se persiste en `config.json` y se aplica al `<h1>` y al `document.title`.

---

## Métricas de Sistema (`get_system_stats`)

Se exponen en `/api/status > system` y se muestran en la barra de estado superior:

| Métrica | Fuente |
|---------|--------|
| `cpu_percent` | `psutil.cpu_percent(interval=None)` |
| `mem_percent` | `psutil.virtual_memory().percent` |
| `net_interface` | `enp2s0` (constante `NET_INTERFACE` en `app.py`) |
| `upload_mbps`   | Δ `bytes_sent` × 8 / Δt / 1e6 |
| `download_mbps` | Δ `bytes_recv` × 8 / Δt / 1e6 |

El indicador textual del estado global cambia según `summary.all_running`:
- todos corriendo → **● 100% operacional**
- alguno detenido → **● N detenido(s)**
- ninguno corriendo → **● Sin actividad**

---

## Gestión de Memoria

### Por Stream

Cada `StreamInstance` mantiene:
- `last_lines` → `deque(maxlen=1000)` — máximo 1000 líneas de log en memoria
- `_last_frame_time` → timestamp del último progreso (frame/size/time)
- `_last_ffmpeg_time_value`, `_last_size_value` → métricas acumulativas
- `_output_thread` → hilo daemon que lee stderr con `read1(4096)` y split `\r`/`\n`

### Full Restart (cleanup completo)

1. `stop()` → `terminate()` → `wait(10s)` → `kill()` si no responde → cierra pipes
2. `last_lines.clear()` → libera referencias de strings
3. `gc.collect()` → fuerza Python a reclamar memoria al OS
4. `sleep(3)` → pausa para que el OS libere recursos
5. `stream.start()` × N → reinicio limpio

---

## Estructura de la UI

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 📺 Sistema de Emisión 24/7   [📖 Docs] [📊 Errores] [📝 INI]            │
│                               [✏️ Título] [▶] [■] [↻]                    │
├──────────────────────────────────────────────────────────────────────────┤
│  Activas: 8 │ Detenidas: 2 │ Total: 10 │ CPU 12.4% │ RAM 31.0% │        │
│                                                  Red ↑1.2 ↓45.6 Mbps    │
│                                       ● 2 detenido(s)                    │
├──────────────────────────────────────────────────────────────────────────┤
│ [🔍 Buscar...]    [Auto-inicio] [Auto-reinicio] [Reinicio medianoche]    │
├────┬──┬────────┬─────┬────────┬───────┬────────┬────────┬────────┬──────┤
│ #  │St│ Stream │ PID │Bitrate │ Tam.  │ Vel.   │ Uptime │  RST   │ Acc. │
├────┼──┼────────┼─────┼────────┼───────┼────────┼────────┼────────┼──────┤
│ 1  │● │ nombre │ 123 │500 kbps│ 1.2MB │ 1.0x   │ 2h 15m │   3    │▶■↻📋│
└────┴──┴────────┴─────┴────────┴───────┴────────┴────────┴────────┴──────┘
│                                           Actualizando cada 2s | -re ... │
└──────────────────────────────────────────────────────────────────────────┘
```

### Botones del header

| Botón | Acción |
|-------|--------|
| 📖 Docs | Abre `/help` (este README renderizado) |
| 📊 Errores | Modal con últimos 5 reinicios/errores (`/api/errors`) |
| 📝 INI | Modal editor de `cadena_rcn.ini` (`/api/ini/read|write`) |
| ✏️ Título | Prompt para renombrar `platform_title` (persiste en `config.json`) |
| ▶ | Inicia todos los streams (`/api/start_all`) |
| ■ | Detiene todos los streams (`/api/stop_all`) |
| ↻ | Reinicia toda la plataforma: detiene streams, lanza `restart_platform.sh` y la UI se reconecta automáticamente |

### Columnas ordenables (click en header)

`#` · `Estado` · `Stream` · `PID` · `Bitrate` · `Tamaño` · `Velocidad` · `Uptime` · `RST`

Click alterna asc/desc; las flechas se actualizan en cada header.

### Estado → etiqueta y color

| `status` | Etiqueta | Punto |
|----------|----------|-------|
| `running` | Emitiendo | verde |
| `starting` | Iniciando | amarillo |
| `stopped` | Detenido | gris |
| `error` | Error | rojo |
| `restarting` | Reiniciando | amarillo |

---

## Toasts (Notificaciones)

Operaciones de iniciar/detener/reiniciar/show command/show logs/editar título/guardar INI muestran toast temporal:

- ✓ Éxito → verde
- ✗ Error → rojo
- ⚠ Warning → naranja
- ℹ Info → azul

---

## Modales

1. **Detalles (Logs + Comando)** — Botón 📋 unificado con subpestañas:
   - **Logs**: `GET /api/logs/<name>?lines=200` con auto-refresh cada 2s, fondo oscuro monoespaciado, botón **Limpiar**
   - **Comando**: `GET /api/stream/<name>/command` con detalle de Original_URL, Destination_URL, FFMPEG_PRE_OPTIONS, FFMPEG_POST_OPTIONS, más botón **📋 Copiar**
2. **INI Editor** — `GET/POST /api/ini/read|write`. Muestra `✓ +N ~M -K` tras guardar; **no se aplica** si el nuevo INI no parsea.
3. **Historial de Errores** — Botón 📊 Errores en header, muestra tabla con los últimos 5 reinicios/errores con: índice, fecha, stream, tipo (Reinicio/Error) y detalle del mensaje de FFmpeg. Los datos los proporciona `GET /api/errors`.

---

## Dependencias

```
flask>=2.3.0
psutil>=5.9.0
markdown>=3.5      # usado por /help (import lazy)
```

Instalar con: `pip install -r requirements.txt`

---

## Ejecución

```bash
# Directo
cd sistema_emision
python app.py

# Con título personalizado en terminal
./emisor_v1.sh

# Con xfce4-terminal (autostart)
xfce4-terminal --hold -e "bash -c 'cd /home/difusor01/Nextcloud/99_Multimedia/RTMP/Linea_Stream/sistema_emision && sleep 2 && ./emisor_v1.sh; exec bash'" &
```

Acceso: `http://localhost:5006`

Reinicio completo desde la UI: botón ↻ del header (detiene streams, ejecuta `restart_platform.sh` y la app se relanza; la UI se reconecta automáticamente).

---

## Manejo de Errores

- **Stream congelado**: warmup 90s + 60s sin progreso de `frame=`/`size=`/`time=` × 6 checks → restart automático (`reason="frozen"`)
- **Process muerto**: `status == "running"` pero `poll() != None` → restart (`reason="process_died"`), error capturado del último log
- **Error al iniciar**: captura excepción, `status="error"`, mensaje visible
- **API error**: try/except en todos los endpoints, retorna JSON con `success: false` y `error`

---

## Reinicio a Medianoche

El scheduler ejecuta `full_restart()` cada noche entre las 00:00 y las 00:05:
1. Detiene todos los procesos FFmpeg y cierra sus pipes
2. Limpia buffers de log (`last_lines.clear()`)
3. Llama `gc.collect()` para liberar memoria
4. Espera 3 segundos para que el OS libere recursos
5. Reinicia todos los streams desde cero

Esto previene acumulación de memoria por logs o recursos no liberados durante operación continua.

---

## Mantenimiento

### Ver logs del servidor (consola donde corre la app)
```bash
# Los print() y logger.info() del scheduler y monitor van a stdout/stderr
```

### Ver procesos FFmpeg activos
```bash
ps aux | grep ffmpeg
```

### Reiniciar manualmente sin esperar medianoche
- **Plataforma completa**: botón ↻ del header, o
  ```bash
  bash restart_platform.sh /tmp/kilo/emision_app.log
  ```
- **Solo streams**:
  ```bash
  curl -X POST http://localhost:5006/api/stop_all
  curl -X POST http://localhost:5006/api/start_all
  # o desde la UI con ▶ y ■
  ```

### Editar INI y guardar
Botón 📝 INI en la UI → abre editor → guardar → actualización parcial:
- Streams nuevos → se inician
- Streams eliminados → se detienen y eliminan
- Streams modificados → se reinician (si estaban corriendo)
- Streams sin cambios → no se tocan
- Si el nuevo INI no parsea → no se aplica y se devuelve error
- Siempre se respalda en `cadena_rcn.ini.bak` antes de sobrescribir

### Cambiar el título de la plataforma
Botón ✏️ Título → introduce el nuevo nombre → Enter. Se guarda en `config.json > platform_title`, se aplica al `<h1>` del header y al `document.title` del navegador. Se sanitiza contra `< > " ' &` y se trunca a 100 caracteres.

### Ver historial de errores
Botón 📊 Errores en header → modal con tabla de últimos 5 reinicios:
- Timestamp, stream, tipo (Reinicio/Error) y mensaje de error de FFmpeg
- Útil para diagnosticar problemas recurrentes en links o comandos
