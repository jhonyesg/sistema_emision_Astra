# Instancias múltiples en un mismo host (multicliente / multi-puerto)

**Objetivo**: correr varias instancias del sistema de emisión Astra **simultáneamente** en el mismo equipo (cada una con su `.ini` y su panel web en un puerto distinto), o replicar la misma receta en **otro host**, **sin modificar los valores por defecto del programa**. Las opciones se pasan por variables de entorno y por el archivo `.ini` que cada instancia apunta.

---

## 1. Cómo funciona hoy (mecánica interna)

El panel es un solo binario: `app.py` (Flask). Lo único que lo distingue entre instancias son **dos variables de entorno** que lee en su arranque:

| Variable     | Default | Dónde se consume                      | Efecto                                                                 |
|--------------|---------|----------------------------------------|------------------------------------------------------------------------|
| `ASTRA_INI`  | *(vacío)* | `app.py:85` (`resolve_active_ini_path`) | Ruta del `.ini` que carga ese proceso (`db/<instancia>.ini`).          |
| `ASTRA_PORT` | `5006`   | `app.py:1176`                          | Puerto TCP del panel web / API REST de **esa** instancia.               |

### 1.1 Prioridad de selección del `.ini` (`app.py:82-106`)

```
1. Variable de entorno  ASTRA_INI=db/<inst>.ini     ← (deployment override)
2. config.json > active_config                       ← (UI lo modifica)
3. Primer *.ini alfabético dentro de db/             ← (fallback automático)
4. Legado cadena_rcn.ini en la raíz                  ← (compatibilidad)
```

Es decir: con sólo exportar `ASTRA_INI=db/lo_que_sea.ini` antes de lanzar `app.py`, esa instancia cargará ese `.ini` y ningún otro. El `config.json` no se toca.

### 1.2 Prioridad / hardcoded del puerto

`app.py:1176`:
```python
_port = int(os.environ.get("ASTRA_PORT", "5006"))
app.run(host="0.0.0.0", port=_port, debug=False, threaded=True)
```

Sólo se respeta `ASTRA_PORT` desde el entorno. Si nadie lo define, asume `5006`. **Esto es exactamente el patrón "darle opciones personalizadas sin tocar los defaults"**: el default del binario es 5006, y cada instancia lo sobreescribe.

### 1.3 Wrapper `run-astra.sh` (recomendado)

Centraliza la exportación de las dos variables y mantiene los defaults centralizados:

```bash
# run-astra.sh (extracto)
: "${ASTRA_PORT:=5006}"   # default si nadie lo fija
: "${ASTRA_INI:=}"        # si está vacío, app.py resuelve con su propia prioridad
exec /usr/bin/python3 app.py
```

Úsalo siempre; evita errores de tipeo y deja un único lugar para cambiar defaults globales.

> ⚠️ **Caveat actual**: existe un segundo binario `app_hot.py` con puerto **hardcoded** `5007` (`app_hot.py:1168`). Para mantener el principio de "no tocar los defaults en el código", conviene migrarlo al mismo patrón (`os.environ.get("ASTRA_PORT", "5007")`) o, mejor aún, **eliminarlo y lanzar otra instancia de `app.py`** con `ASTRA_PORT=5007`.

---

## 2. Lo que está corriendo en este host (estado actual)

```
LISTEN  0.0.0.0:5006  python3  app.py       (instancia 112_A)
LISTEN  0.0.0.0:5007  python3  app_hot.py   (instancia 112_B)
```

> **Convención adoptada (2026-10-14)**: el nombre de cada unidad systemd sigue la regla
> `<número>_<letra>.service` para evitar colisiones con herramientas de sincronización.
> Ej.: `112_A.service` para el `.ini` `db/112_a.ini` (puerto 5006),
> `112_B.service` para `db/112_b.ini` (puerto 5007).

### 2.1 Instancia **112_A** — puerto 5006 (app.py / `.ini` `db/112_a.ini`)

`/etc/systemd/system/112_A.service` (system service):
```ini
[Service]
User=<host>
Group=<host>
WorkingDirectory=/home/<host>/.../sistema_emision_Astra
Environment=ASTRA_INI=db/112_a.ini
ExecStart=/usr/bin/python3 /home/<host>/.../app.py
Restart=on-failure
```

- `Environment=` único: `ASTRA_INI`. Puerto por defecto del binario → **5006**.
- Carga `db/112_a.ini` (~25 streams).
- Logs: `/var/log/astra-emision.log` (mantenido por Compatibilidad; no se renombró).

### 2.2 Instancia **112_B** — puerto 5007 (app_hot.py / `.ini` `db/112_b.ini`)

`/etc/systemd/system/112_B.service` (system service):
```ini
[Service]
Environment=ASTRA_INI=db/112_b.ini
ExecStart=/usr/bin/python3 /home/<host>/.../app_hot.py
```

- Lanza `app_hot.py` (la copia legacy con 14 streams locales).
- Puerto **5007** viene hardcoded dentro de `app_hot.py:1168`. **Pendiente de refactor** al patrón `os.environ.get("ASTRA_PORT", "5007")` para alinear con el principio "opciones por entorno, no por código".
- Logs: `/var/log/astra-emision-hot.log`.

> **Migración del 2026-10-14**: las unidades se llamaban `astra-emision.service` y `astra-emision-hot.service`. Se renombraron para evitar que la cadena `astra` colisionara con el sistema de sincronización del operador. Esta receta de rename es la que se replica en otros hosts (ver §8).

---

## 3. Procedimiento para añadir una nueva instancia (mismo host u otro)

### 3.1 Crear el `.ini` de la nueva instancia

Cada instancia = un `.ini` propio. Copia la plantilla sanitizada:

```bash
cd /<path>/sistema_emision_Astra
cp cadena_rcn.ini.example db/20_cliente_nuevo.ini
# editar db/20_cliente_nuevo.ini:
#   - cambiar [01_Canal_Principal] y resto de secciones
#   - mantener [servidor] si quieres overrides por despliegue
#     (Title, Network, Night_Restart, Auto_Restart, Start_All_On_Boot,
#      ver config_parser.py:load_deployment_overrides)
```

`db/*.ini` está en `.gitignore`: **no se commitea**, cada despliegue mantiene sus datos.

### 3.2 Crear / clonar el wrapper del launcher

```bash
# /<path>/sistema_emision_Astra/run-astra-cliente_nuevo.sh
#!/bin/bash
set -e
export ASTRA_INI="db/20_cliente_nuevo.ini"
export ASTRA_PORT="5010"             # único puerto del panel de ESTA instancia
exec /<path>/run-astra.sh            # delega al wrapper común (PULSE/DBus/XDG)
```

`chmod +x run-astra-cliente_nuevo.sh`. **Clave: los defaults del binario (`app.py`) no se tocan**; lo único específico está en este `.sh`.

### 3.3 Registrar la unidad systemd (system o user)

**Opción A — system service** (ejemplo `40_canal_bogota`):

> En lugar del nombre genérico `astra-emision-<inst>`, usa el prefijo numérico del `.ini`
> seguido de una letra si hay más de una instancia por número: `40_a.service`, `40_b.service`.

```ini
[Unit]
Description=Astra Emision (canal_bogota, puerto 5010, db/40_canal_bogota.ini)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<host>
Group=<host>
WorkingDirectory=/<path>/sistema_emision_Astra
Environment=ASTRA_INI=db/40_canal_bogota.ini
Environment=ASTRA_PORT=5010
ExecStart=/usr/bin/python3 /<path>/app.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
StandardOutput=append:/var/log/astra-40_canal_bogota.log
StandardError=append:/var/log/astra-40_canal_bogota.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now 40_canal_bogota.service
sudo systemctl status 40_canal_bogota.service
```

**Opción B — user service** (sin root, ideal para el wrapper `.sh`):
```ini
# ~/.config/systemd/user/40_canal_bogota.service
[Unit]
Description=Astra Emision (canal_bogota, puerto 5010)
After=network-online.target

[Service]
WorkingDirectory=/<path>/sistema_emision_Astra
Environment=ASTRA_INI=db/40_canal_bogota.ini
Environment=ASTRA_PORT=5010
ExecStart=/<path>/run-astra-40_canal_bogota.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now 40_canal_bogota.service
loginctl enable-linger <host>     # para que sobreviva al logout
```

### 3.4 Verificación rápida

```bash
# 1) Puerto escuchando
ss -tlnp | grep 5010

# 2) Panel responde
curl -s http://127.0.0.1:5010/api/status | jq .

# 3) Variables de entorno dentro del proceso
tr '\0' '\n' < /proc/$(pgrep -f 'app.py.*5010' | head -1)/environ | grep ASTRA_

# 4) Logs
journalctl -u 40_canal_bogota.service -f
#   o, en system:
sudo tail -f /var/log/astra-40_canal_bogota.log
```

---

## 4. Replicar el patrón completo en **otro host**

### 4.1 Requisitos
- Mismo path de instalación recomendado: `/home/<host>/Aplicaciones/99_Multimedia/RTMP/Linea_Stream/sistema_emision_Astra` (otros funcionan si ajustas las unidades).
- Dependencias: `pip install -r requirements.txt` (Flask, psutil, …).
- ffmpeg + driver VAAPI en `/dev/dri/renderD128` si usas aceleración.
- PulseAudio / DBus si los streams capturan audio del escritorio (lo que ya prepara `run-astra.sh`).

### 4.2 Pasos

```bash
# 1) Clonar el repo en el host destino
git clone <repo-url> /<path>/sistema_emision_Astra
cd /<path>/sistema_emision_Astra

# 2) Instalar deps
pip install -r requirements.txt

# 3) Sembrar el db/ desde la plantilla
python3 app.py --init                # crea db/1_local.ini si db/ está vacío

# 4) Crear / copiar el .ini real de esa máquina
cp cadena_rcn.ini.example db/30_cliente_remoto.ini
$EDITOR db/30_cliente_remoto.ini

# 5) Elegir puerto libre (no usado por OTRO Astra en el mismo host)
#    5006, 5007 ya están tomados en este host; en otro elige p.ej. 5010.

# 6) Crear la unidad systemd (sección 3.3) ajustando:
#       Environment=ASTRA_INI=db/30_cliente_remoto.ini
#       Environment=ASTRA_PORT=5010
#       ExecStart=app.py (mismo binario)

# 7) Activar y verificar (sección 3.4)
```

**No hay que tocar `app.py` ni `config.json` en el host destino**: el archivo se elige por `ASTRA_INI` y el puerto por `ASTRA_PORT`. Los defaults del binario permanecen intactos.

### 4.3 Diferencias legítimas entre hosts (sí se permiten)

Sólo lo del bloque `[servidor]` del `.ini` (ver `config_parser.py:load_deployment_overrides`):

| Clave              | Override del default          |
|--------------------|-------------------------------|
| `Title`            | `config.json:platform_title`  |
| `Network`          | interfaz para stats de red    |
| `Night_Restart`    | `config.json:midnight_restart`|
| `Auto_Restart`     | `config.json:auto_restart`    |
| `Start_All_On_Boot`| `config.json:start_all_on_boot`|
| `Service`          | nombre opcional del servicio  |

---

## 5. Cosas a NO hacer (anti-patrones observados)

| Anti-patrón | Por qué evitarlo | Cómo hacerlo bien |
|-------------|------------------|-------------------|
| Hardcodear `app.run(port=5007)` en `app_hot.py` | Bifurca los defaults del binario; rompe el principio "opciones por entorno". | Una sola línea `port=int(os.environ.get("ASTRA_PORT","5006"))` o siempre `app.py`. |
| Editar `config.json > active_config` para cambiar de instancia | Afecta a **todas** las instancias que corran sin `ASTRA_INI`. | Exportar `ASTRA_INI` por instancia. |
| Compartir un mismo `.ini` entre dos instancias simultáneas | El `IniChangeWatcher` re-arranca streams en ambos procesos; pisadas. | Cada instancia con su `.ini` propio (puede ser copia, pero ruta distinta). |
| Lanzar con `python app.py &` sin systemd | Muere al cerrar sesión, sin `Restart=`. | Usar `systemctl enable --now ...`. |
| Poner secretos reales en `db/*.ini` y commitear | `.gitignore` ya los excluye; respetarlo. | Commitear sólo `cadena_rcn.ini.example`. |

---

## 6. Cheatsheet

```bash
# Ver qué instancia usa qué puerto/proceso
ss -tlnp | grep -E ':50(0[6-9]|1[0-9])'

# Variables dentro de un proceso Astra concreto
PID=$(pgrep -f 'app.py' | head -1)
tr '\0' '\n' < /proc/$PID/environ | grep -E '^ASTRA_'

# Forzar manualmente un .ini sin systemd
ASTRA_INI=db/20_cliente_nuevo.ini ASTRA_PORT=5010 ./run-astra.sh
```

---

## 7. Referencias cruzadas en este repo

- Mecánica de env vars: `app.py:82-106` (resolución INI), `app.py:1176` (puerto).
- Parser de `[servidor]`: `config_parser.py:168-217` (`load_deployment_overrides`).
- Defaults del wrapper: `run-astra.sh:23-29`.
- Documentación de despliegues múltiples (versión corta): `README.md` sección "Despliegues múltiples (carpeta `db/`)" (~línea 7).
- Documentación del `.ini`: `db/README.md` y `README.md` sección "Overrides por despliegue".
- Estado en vivo documentado: `docs/DIAGNOSTICO_STREAMS.md`, `docs/PLAN_ESTABILIDAD.md`.
- Cambio aplicado de alineación restart: `CHANGELOG.md` (entrada 2026-10-14).

---

## 8. Checklist de alineación para otros hosts

> **Propósito**: que cualquier nueva instancia (en este equipo o en otro host) tenga el botón **Reiniciar plataforma** correctamente alineado con su servicio systemd, sin tocar el código ni los defaults del binario.

### 8.1 Por qué es necesario

`/api/restart_platform` resuelve por dónde reiniciar así:

1. Lee `Service=` del bloque `[servidor]` del INI activo (`config_parser.py:206-207`, `app.py:186-187`).
2. Si existe → `sudo -n systemctl restart <Service>` + `os._exit(0)` (camino limpio, per-instancia).
3. Si **no** existe → fallback a `restart_platform.sh` (cableado al 5006: mata todo y re-lanza `app.py`).

**Implicación práctica**: si una instancia NO declara `Service=…` en su INI, su botón Reiniciar Plataforma reinicia la instancia equivocada y mata ffmpegs del resto. Por eso cada `.ini` que represente un servicio systemd independiente **debe** declarar su `Service=`.

### 8.2 Regla de oro (una línea por instancia)

```
El nombre de la unidad systemd debe coincidir con `Service=` en el INI.
```

Concretamente:

| Unidad systemd (`/etc/systemd/system/*.service`) | Clave requerida en `db/<inst>.ini`                |
|-------------------------------------------------|--------------------------------------------------|
| `112_A.service`                                 | `Service=112_A.service`                          |
| `112_B.service`                                 | `Service=112_B.service`                          |
| `40_canal_bogota.service`                       | `Service=40_canal_bogota.service`                |

> La unidad **debe** usar `ExecStart=/usr/bin/python3 /…/app.py` (con `app_hot.py` solo si la mantienes como caso legacy). El patrón limpio es una sola instancia de `app.py` por servicio.
>
> El nombre del `.service` y el del `.ini` deben "casar" por contenido (`Service=`), no por prefijo genérico. Evitar prefijos que coincidan con otros sistemas (aquí se sustituyó `astra-emision-*` por `<número>_<letra>` por una colisión con el sistema de sincronización externo).

### 8.3 Procedimiento rápido (mismo host u otro)

```bash
# 0) Identifica la unidad y el INI asociado a esa instancia
systemctl cat <servicio>.service | grep -E 'Environment=|ExecStart='
# → te confirma qué ASTRA_INI y ASTRA_PORT están definidos

# 1) Edita el INI y añade la línea al bloque [servidor]
INI="$(systemctl show -p Environment <servicio>.service | sed 's/^Environment=//' \
        | tr ' ' '\n' | grep '^ASTRA_INI=' | cut -d= -f2-)"
$EDITOR "$INI"
#   → bajo [servidor], añadir (si no está):
#       Service=<servicio>.service

# Forma rápida (sed atómico para INIs sin Service= ya presente):
if ! grep -q '^Service=' "$INI"; then
    awk 'BEGIN{p=0} /^\[servidor\]/{p=1; print; next} /^\[/{p=0} p && NF && !/^Service=/{print; next} {print}' \
        "$INI" > "$INI.new" && mv "$INI.new" "$INI"
    # luego añadir el Service= manualmente en la sección [servidor] preferiblemente
fi

# 2) Aplica sin reiniciar nada
curl -s -X POST http://127.0.0.1:${PUERTO}/api/restart_platform | jq .
#   → debe devolver {"method":"systemd","service":"<servicio>.service", ...}

# 3) Confirma en logs del servicio correcto (no del 5006!)
sudo journalctl -u <servicio>.service -n 30 --no-pager
```

### 8.4 Validación global (todas las instancias a la vez)

```bash
#!/bin/bash
# check_alineacion_instancias.sh — ejecutalo en cada host
for U in $(systemctl list-unit-files 'astra-*.service' --no-legend | awk '{print $1}'); do
  INI_ENV=$(systemctl show -p Environment "$U" | tr ' ' '\n' | grep '^ASTRA_INI=' | cut -d= -f2-)
  [ -z "$INI_ENV" ] && { echo "  ✗ $U sin ASTRA_INI"; continue; }
  [ ! -f "$INI_ENV" ] && { echo "  ✗ $U INI=$INI_ENV (no existe)"; continue; }
  if grep -q "^Service=$U" "$INI_ENV"; then
      echo "  ✓ $U ← $INI_ENV"
  else
      echo "  ✗ $U ← $INI_ENV (FALTA Service=$U en [servidor])"
  fi
done
```

### 8.5 Anti-patrones a evitar cuando se replica

| No hacer                                                                 | Por qué                                                   |
|-------------------------------------------------------------------------|-----------------------------------------------------------|
| Crear el `.ini` sin `Service=` "porque parece opcional"                 | Caerá al fallback (`restart_platform.sh` cableado al 5006). |
| Comentar/omitir `Service=` en un INI que ya levanta su propia unidad    | El botón Reiniciar de la UI no hará lo que esperas.       |
| Reutilizar `db/112_a.ini` en dos instancias distintas                  | El IniChangeWatcher re-arranca streams en ambos procesos. |
| Editar `app.py`/`app_hot.py` para añadir `port=5007`                   | Rompe el patrón "default + override por entorno".          |
| Cambiar `config.json > active_config` como sustituto de `ASTRA_INI`     | Afecta a **todas** las instancias sin env override.       |

### 8.6 Estado en este host tras la corrección y migración

```
✓ db/112_a.ini                       Service=112_A.service         (puerto 5006)
✓ db/112_b.ini                       Service=112_B.service         (puerto 5007)
✓ /etc/systemd/system/112_A.service  cargado y enabled
✓ /etc/systemd/system/112_B.service  cargado y enabled
```

Verificado vía:

```bash
grep '^Service=' /<path>/sistema_emision_Astra/db/112_a.ini /<path>/sistema_emision_Astra/db/112_b.ini
# → debe devolver ambos con Service=112_X.service

curl -s -X POST http://127.0.0.1:5006/api/restart_platform | python3 -m json.tool
# → {"method": "systemd", "service": "112_A.service", ...}

curl -s -X POST http://127.0.0.1:5007/api/restart_platform
# → 200 OK (respuesta vacía por os._exit(0); el servicio se reinicia vía systemd)
# comprobar en journalctl:
sudo journalctl -u 112_B.service -n 15 --no-pager
```

Tras pulsar Reiniciar Plataforma, las instancias regresan al cabo de
`RestartSec=5 + tiempo_de_inicio_ffaup` (~5–25 s según carga de streams).

### 8.7 Si NO quieres editar el INI (workaround legacy)

Si por motivos operativos no puedes tocar el `.ini` de un host remoto, el workaround temporal es:

```bash
# Crear un sudoers NOPASSWD restringido a esa unidad
echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/systemctl restart 112_B.service" \
    | sudo tee /etc/sudoers.d/astra-112_B-restart

# Y dejar un alias/atajo en el shell remoto
alias restart_112_B='sudo systemctl restart 112_B.service'
```

Esto **no** alinea el botón de la UI; sólo te da una vía manual fiable. La solución correcta es siempre añadir `Service=` al INI.

---

## 9. Renombrar unidades `.service` en un host ya desplegado

> Caso real: 2026-10-14 — `astra-emision.service` → `112_A.service`,
> `astra-emision-hot.service` → `112_B.service`, por colisión con un sistema de
> sincronización externo del operador. Aquí está la receta exacta para replicarlo
> en otros hosts sin caer en empirismo.

### 9.1 Por qué este patrón

- El nombre de la unidad debe **distinguirse claramente** de otros sistemas del host (sincronización, monitoring, etc.).
- Patrón recomendado: `<número>_<letra>.service` donde el número es el prefijo numérico del `.ini` (`db/<n>_<resto>.ini`) y la letra distingue instancias en el mismo host.
- La clave `Service=` dentro de `[servidor]` del `.ini` correspondiente **debe coincidir exactamente** con el nuevo nombre.

### 9.2 Procedimiento (orden importa: parar antes de renombrar)

```bash
# 0) Snapshot del estado actual
systemctl cat astra-emision.service | grep -E 'Description=|Environment=|ExecStart='
systemctl cat astra-emision-hot.service | grep -E 'Description=|Environment=|ExecStart='
ls -la /etc/systemd/system/multi-user.target.wants/ | grep astra

# 1) Apagar (orden: hot primero, luego frío, para minimizar ffmpegs pkill cruzados)
sudo systemctl stop astra-emision-hot.service
sudo systemctl stop astra-emision.service

# 2) Quitar enable / romper symlinks
sudo systemctl disable astra-emision-hot.service
sudo systemctl disable astra-emision.service

# 3) Renombrar archivos .service
sudo mv /etc/systemd/system/astra-emision-hot.service     /etc/systemd/system/112_B.service
sudo mv /etc/systemd/system/astra-emision.service         /etc/systemd/system/112_A.service

# 4) Editar contenido (Description + Environment ASTRA_INI)
sudo sed -i \
  -e 's|Description=Astra Emision Hot.*|Description=Astra 112_B (app_hot.py puerto 5007, db/112_b.ini)|' \
  -e 's|Environment=ASTRA_INI=db/<antiguo_hot>.ini|Environment=ASTRA_INI=db/112_b.ini|' \
  /etc/systemd/system/112_B.service

sudo sed -i \
  -e 's|Description=Astra Emision RTMP.*|Description=Astra 112_A (app.py puerto 5006, db/112_a.ini)|' \
  -e 's|Environment=ASTRA_INI=db/<antiguo_frio>.ini|Environment=ASTRA_INI=db/112_a.ini|' \
  /etc/systemd/system/112_A.service

# 5) Renombrar .ini en db/
mv db/<antiguo_hot>.ini   db/112_b.ini
mv db/<antiguo_frio>.ini  db/112_a.ini

# 6) Actualizar Service= en [servidor] de cada .ini
sed -i 's|^Service=astra-emision\.service$|Service=112_A.service|' db/112_a.ini
sed -i 's|^Service=astra-emision-hot\.service$|Service=112_B.service|' db/112_b.ini

# 7) Activar nuevas unidades
sudo systemctl daemon-reload
sudo systemctl enable --now 112_A.service 112_B.service
```

### 9.3 Verificación funcional

```bash
# A) Puertos bind a las instancias correctas
ss -tlnp | grep -E ':(5006|5007)\s'

# B) Reinicio desde UI ahora debe ir por systemd (no por restart_platform.sh)
curl -sS -X POST http://127.0.0.1:5006/api/restart_platform | python3 -m json.tool
# → {"method": "systemd", "service": "112_A.service", "success": true}

# C) Symlinks nuevos en multi-user.target.wants/
sudo ls -la /etc/systemd/system/multi-user.target.wants/ | grep -E 'astra|112_'

# D) Logs (¡ojo: nombres antiguos se mantienen en los logs por Compatibilidad!)
tail -3 /var/log/astra-emision.log
tail -3 /var/log/astra-emision-hot.log

# E) Confirmar Service= en cada INI
grep '^Service=' db/112_a.ini db/112_b.ini
```

### 9.4 Detalles a respetar

- **Orden de stop**: `hot` (el que comparte `pkill -f 'ffmpeg -re'` global) **primero**, luego el frío. Si se hace al revés, el `pkill` del script de stop del frío puede afectar al hot antes de su shutdown limpio.
- **`RestartSec=5`** hace que tras `systemctl restart` el servicio tarde ≥5s + tiempo_de_inicio_ffmpeg en volver. Para una instancia con 25 streams en VAAPI puede ser 20–30s. Paciencia, los `curl` consecutivos durante ese intervalo fallarán — es normal.
- **Logs**: los `StandardOutput=append:/var/log/astra-emision*.log` se preservan para no perder histórico. Si decides renombrarlos también, hazlo antes de la primera ejecución para no perder contexto.
- **`db/*.bak`**: NO renombrar. Los backups mantienen el nombre antiguo porque son punto de rollback; tu editor / script de rename debe saltarlos.
- **`config.json > active_config`**: si apunta a uno de los INI renombrados (`db/<antiguo>.ini`), actualízalo a `db/112_a.ini` (o lo que aplique) **antes** del paso 7. Si no, el watcher puede intentar leer `active_config` en su próxima ejecución y caer al default del primer `*.ini` alfabético.

### 9.5 Rollback

Si algo va mal:

```bash
# 1) Apagar nuevas unidades
sudo systemctl disable --now 112_A.service 112_B.service

# 2) Restaurar nombres antiguos en .service
sudo mv /etc/systemd/system/112_A.service /etc/systemd/system/astra-emision.service
sudo mv /etc/systemd/system/112_B.service /etc/systemd/system/astra-emision-hot.service

# 3) Restaurar INI antiguo desde .bak (si lo tenías)
cp db/112_a.ini.bak db/112.ini
cp db/112_b.ini.bak db/112_srs_local_conflic.ini

# 4) Restaurar symlinks
sudo systemctl daemon-reload
sudo systemctl enable --now astra-emision.service astra-emision-hot.service
```

El `.bak` es la red de seguridad; si no existía, sólo queda volver a `git checkout db/<antiguo>.ini` (si estaba versionado).
