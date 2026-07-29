import configparser
import gc
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import psutil
from flask import Flask, jsonify, render_template, render_template_string, request

logger = logging.getLogger(__name__)

app = Flask(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_parser import load_config, load_config_from_content, load_deployment_overrides
from monitor import monitor
from stream_manager import stream_manager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "db")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def list_db_configs():
    """Devuelve [(name, full_path, size, mtime)] ordenados alfabéticamente."""
    if not os.path.isdir(DB_DIR):
        return []
    out = []
    for name in sorted(os.listdir(DB_DIR)):
        if not name.lower().endswith(".ini"):
            continue
        full = os.path.join(DB_DIR, name)
        if not os.path.isfile(full):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        out.append((name, full, st.st_size, int(st.st_mtime)))
    return out


def count_streams_in_ini(path):
    if not path or not os.path.isfile(path):
        return 0
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s.startswith("[") or not s.endswith("]"):
                    continue
                if s[1:-1].lower() == "servidor":
                    continue
                n += 1
    except OSError:
        return 0
    return n


def find_db_config(name):
    """Resuelve un nombre de config dentro de db/ con guardas de seguridad. Acepta nombres con o sin `.ini`."""
    if not name:
        return None
    name = name.strip()
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    safe = os.path.basename(name)
    if not safe.lower().endswith(".ini"):
        safe = safe + ".ini"
    return os.path.join(DB_DIR, safe)


def resolve_active_ini_path():
    """Resuelve el INI activo siguiendo la prioridad ASTRA_INI > config.json > primer archivo en db/ > legacy raíz."""
    # 1) variable de entorno (override de despliegue)
    env = (os.environ.get("ASTRA_INI") or "").strip()
    if env:
        return env if os.path.isabs(env) else os.path.join(BASE_DIR, env)
    # 2) config.json > active_config
    cfg = load_app_config()
    stored = (cfg.get("active_config") or "").strip()
    if stored:
        candidate = stored if os.path.isabs(stored) else os.path.join(BASE_DIR, stored)
        if os.path.isfile(candidate):
            return candidate
    # 3) primer *.ini alfabético dentro de db/
    if os.path.isdir(DB_DIR):
        for n in sorted(os.listdir(DB_DIR)):
            if n.lower().endswith(".ini"):
                full = os.path.join(DB_DIR, n)
                if os.path.isfile(full):
                    return full
    # 4) legacy: cadena_rcn.ini en la raíz
    legacy = os.path.join(BASE_DIR, "cadena_rcn.ini")
    if os.path.isfile(legacy):
        return legacy
    return None


def get_active_ini_name():
    p = resolve_active_ini_path()
    return os.path.basename(p) if p else None


def migrate_root_to_db():
    """Si existe cadena_rcn.ini en la raíz y db/ está vacío, lo mueve y persiste el activo."""
    legacy = os.path.join(BASE_DIR, "cadena_rcn.ini")
    if not os.path.isfile(legacy):
        return None
    os.makedirs(DB_DIR, exist_ok=True)
    existing = [n for n in os.listdir(DB_DIR) if n.lower().endswith(".ini")]
    if existing:
        return None
    for i in range(1, 1000):
        target = os.path.join(DB_DIR, f"{i}_default.ini")
        if not os.path.exists(target):
            target_name = os.path.basename(target)
            break
    else:
        return None
    try:
        os.replace(legacy, os.path.join(DB_DIR, target_name))
    except OSError as e:
        logger.warning(f"No se pudo migrar {legacy}: {e}")
        return None
    backup = legacy + ".bak"
    if os.path.exists(backup):
        try:
            os.replace(backup, os.path.join(DB_DIR, target_name + ".bak"))
        except OSError:
            pass
    cfg = load_app_config()
    cfg["active_config"] = f"db/{target_name}"
    try:
        save_app_config(cfg)
    except OSError as e:
        logger.warning(f"No se pudo guardar active_config: {e}")
    return target_name


def _validate_config_name(name):
    """Devuelve un nombre .ini saneado o None si es inválido."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    if not name.lower().endswith(".ini"):
        name = name + ".ini"
    safe = os.path.basename(name)
    if safe != name:
        return None
    return safe

NET_INTERFACE = "enp2s0"
_DEFAULT_NET_INTERFACE = "enp2s0"


def _apply_deployment_overrides():
    """Lee [servidor] del INI activo y sobreescribe Title/Network/toggles.
    Se llama al arranque y cada vez que se activa una config nueva.
    """
    global PLATFORM_TITLE, NET_INTERFACE
    global START_ALL_ON_BOOT, MIDNIGHT_RESTART_ENABLED, AUTO_RESTART_ENABLED
    try:
        overrides = load_deployment_overrides(resolve_active_ini_path())
    except Exception as e:
        logger.warning(f"No se pudieron aplicar overrides de despliegue: {e}")
        return
    if overrides.get("title"):
        PLATFORM_TITLE = overrides["title"]
        app_config["platform_title"] = PLATFORM_TITLE
    if overrides.get("network"):
        NET_INTERFACE = overrides["network"]
    for key in ("start_all_on_boot", "midnight_restart", "auto_restart"):
        if overrides.get(key) is not None:
            val = bool(overrides[key])
            app_config[key] = val
            if key == "start_all_on_boot":
                START_ALL_ON_BOOT = val
            elif key == "midnight_restart":
                MIDNIGHT_RESTART_ENABLED = val
            elif key == "auto_restart":
                AUTO_RESTART_ENABLED = val
    try:
        _NET_PREV["time"] = time.time()
        cnt = psutil.net_io_counters(pernic=True).get(NET_INTERFACE)
        if cnt:
            _NET_PREV["bytes_sent"] = cnt.bytes_sent
            _NET_PREV["bytes_recv"] = cnt.bytes_recv
    except Exception:
        pass
_NET_LOCK = threading.Lock()
_NET_PREV = {"time": time.time(), "bytes_sent": 0, "bytes_recv": 0}
_net_counters = psutil.net_io_counters(pernic=True).get(NET_INTERFACE)
if _net_counters:
    _NET_PREV["bytes_sent"] = _net_counters.bytes_sent
    _NET_PREV["bytes_recv"] = _net_counters.bytes_recv

# Inicializar cpu_percent para que el primer get no devuelva 0.0
psutil.cpu_percent(interval=None)

_DEFAULT_TITLE = "Sistema de Emisión 24/7-Astra"
_MAX_TITLE_LEN = 100

_TITLE_SANITIZER = re.compile(r"[<>\"\'&]")


def get_system_stats():
    cpu_percent = psutil.cpu_percent(interval=None)
    mem_percent = psutil.virtual_memory().percent

    now = time.time()
    upload_mbps = 0.0
    download_mbps = 0.0
    counters = psutil.net_io_counters(pernic=True).get(NET_INTERFACE)
    if counters:
        with _NET_LOCK:
            dt = now - _NET_PREV["time"]
            if dt > 0:
                upload_mbps = max(
                    0, (counters.bytes_sent - _NET_PREV["bytes_sent"]) * 8 / dt / 1_000_000
                )
                download_mbps = max(
                    0, (counters.bytes_recv - _NET_PREV["bytes_recv"]) * 8 / dt / 1_000_000
                )
            _NET_PREV["time"] = now
            _NET_PREV["bytes_sent"] = counters.bytes_sent
            _NET_PREV["bytes_recv"] = counters.bytes_recv

    return {
        "cpu_percent": round(cpu_percent, 1),
        "mem_percent": round(mem_percent, 1),
        "net_interface": NET_INTERFACE,
        "upload_mbps": round(upload_mbps, 2),
        "download_mbps": round(download_mbps, 2),
    }


def load_app_config():
    defaults = {
        "start_all_on_boot": True,
        "midnight_restart": True,
        "auto_restart": True,
        "platform_title": _DEFAULT_TITLE,
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                defaults.update(cfg)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"No se pudo cargar {CONFIG_FILE}: {e}")
    return defaults


def save_app_config(config):
    """Escribe config.json con escritura atómica tmp + rename."""
    config_dir = os.path.dirname(CONFIG_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", dir=config_dir, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


app_config = load_app_config()
START_ALL_ON_BOOT = app_config.get("start_all_on_boot", True)
MIDNIGHT_RESTART_ENABLED = app_config.get("midnight_restart", True)
AUTO_RESTART_ENABLED = app_config.get("auto_restart", True)
PLATFORM_TITLE = app_config.get("platform_title", _DEFAULT_TITLE)
_CONFIG_LOCK = threading.Lock()

_apply_deployment_overrides()


class MidnightRestartScheduler:
    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        already_executed = False
        while self._running:
            now = datetime.now(timezone.utc).astimezone()
            next_midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seconds_until = (next_midnight - now).total_seconds()
            logger.info(f"Próximo reinicio a medianoche en {int(seconds_until)} segundos")
            time.sleep(min(seconds_until, 60))
            if not self._running:
                break
            now = datetime.now(timezone.utc).astimezone()
            if not MIDNIGHT_RESTART_ENABLED:
                if now.hour == 0 and now.minute < 5 and not already_executed:
                    logger.info("Reinicio a medianoche deshabilitado, saltando...")
                    already_executed = True
                if now.hour >= 5:
                    already_executed = False
                continue
            if now.hour == 0 and now.minute < 5 and not already_executed:
                logger.info("Ejecutando reinicio completo a medianoche...")
                stream_manager.full_restart()
                logger.info("Reinicio completo - memoria liberada")
                already_executed = True
            elif now.hour >= 5:
                already_executed = False


scheduler = MidnightRestartScheduler()


def initialize_streams():
    ini_path = resolve_active_ini_path()
    if not ini_path or not os.path.isfile(ini_path):
        return 0
    config = load_config(ini_path)
    streams = config.get_all_streams()
    for stream_config in streams:
        name = stream_config["name"]
        stream_manager.register_stream(name, stream_config)
        if stream_config.get("autostart") or START_ALL_ON_BOOT:
            stream_manager.start_stream(name)
            time.sleep(2)
    return len(streams)


# Import lazy de markdown para que /help no penalice el arranque
_markdown_module = None


def _get_markdown():
    global _markdown_module
    if _markdown_module is None:
        import markdown as _md
        _markdown_module = _md
    return _markdown_module


@app.route("/")
def index():
    return render_template("index.html", platform_title=PLATFORM_TITLE)


@app.route("/help")
def help():
    readme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()
        md = _get_markdown()
        html_content = md.markdown(content, extensions=["tables", "fenced_code"])
        return render_template_string('''
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Documentación - Sistema de Emisión</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #1e1e2f; border-bottom: 2px solid #667eea; padding-bottom: 10px; }
        h2 { color: #2d2d44; margin-top: 30px; border-bottom: 1px solid #ddd; padding-bottom: 5px; }
        h3 { color: #495057; }
        code { background: #1e1e2f; color: #00ff88; padding: 2px 6px; border-radius: 4px; font-family: 'Courier New', monospace; font-size: 0.9em; }
        pre { background: #1e1e2f; color: #00ff88; padding: 15px; border-radius: 8px; overflow-x: auto; font-size: 0.85em; }
        table { border-collapse: collapse; width: 100%; margin: 15px 0; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background: #667eea; color: white; }
        tr:nth-child(even) { background: #f9f9f9; }
        a { color: #667eea; }
        .container { background: white; padding: 30px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
    <div class="container">
        ''' + html_content + '''
    </div>
</body>
</html>
        ''')
    except FileNotFoundError:
        return "README.md no encontrado", 404
    except Exception as e:
        logger.exception("Error leyendo README")
        return f"Error reading README: {e}", 500


@app.route("/api/status")
def get_status():
    all_status = stream_manager.get_all_status()
    running_count = stream_manager.get_running_count()
    total_streams = len(all_status)

    return jsonify(
        {
            "streams": all_status,
            "summary": {
                "total": total_streams,
                "running": running_count,
                "stopped": total_streams - running_count,
                "all_running": running_count == total_streams and total_streams > 0,
            },
            "system": get_system_stats(),
        }
    )


@app.route("/api/stream/<name>/start", methods=["POST"])
def start_stream(name):
    success = stream_manager.start_stream(name)
    return jsonify({"success": success, "name": name})


@app.route("/api/stream/<name>/stop", methods=["POST"])
def stop_stream(name):
    success = stream_manager.stop_stream(name)
    return jsonify({"success": success, "name": name})


@app.route("/api/stream/<name>/restart", methods=["POST"])
def restart_stream(name):
    success = stream_manager.restart_stream(name)
    return jsonify({"success": success, "name": name})


@app.route("/api/logs/<name>")
def get_logs(name):
    try:
        lines = min(int(request.args.get("lines", 100)), 1000)
    except (TypeError, ValueError):
        lines = 100
    logs = stream_manager.get_logs(name, lines)
    return jsonify({"name": name, "logs": logs})


@app.route("/api/stream/<name>/command")
def get_command(name):
    if name in stream_manager.streams:
        stream = stream_manager.streams[name]
        cmd = stream.build_ffmpeg_command(stream._vlc_url if stream._vlc_process else None)
        quoted_cmd = []
        for arg in cmd:
            if "://" in arg or arg.startswith("rtmp") or "(" in arg or ")" in arg:
                quoted_cmd.append(f'"{arg}"')
            else:
                quoted_cmd.append(arg)
        return jsonify(
            {"name": name, "command": " ".join(quoted_cmd), "config": stream.config}
        )
    return jsonify({"error": "Stream not found"}), 404


@app.route("/api/start_all", methods=["POST"])
def start_all():
    results = {}
    for name in list(stream_manager.streams):
        results[name] = stream_manager.start_stream(name)
    return jsonify({"success": True, "results": results})


@app.route("/api/stop_all", methods=["POST"])
def stop_all():
    results = {}
    for name in list(stream_manager.streams):
        results[name] = stream_manager.stop_stream(name)
    return jsonify({"success": True, "results": results})


@app.route("/api/restart_platform", methods=["POST"])
def restart_platform():
    restart_script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "restart_platform.sh"
    )
    if not os.path.exists(restart_script):
        return jsonify({"success": False, "error": "restart_platform.sh not found"}), 500

    try:
        stream_manager.stop_all_and_cleanup()
    except Exception:
        logger.exception("Error en stop_all_and_cleanup durante restart_platform")

    log_file = "/tmp/kilo/emision_app.log"
    subprocess.Popen(
        ["nohup", "bash", restart_script, log_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    os._exit(0)


@app.route("/api/ini/read", methods=["GET"])
def read_ini():
    ini_path = resolve_active_ini_path()
    if not ini_path or not os.path.isfile(ini_path):
        return jsonify({"success": False, "error": "INI no encontrado"}), 404
    try:
        with open(ini_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"success": True, "content": content, "path": f"db/{os.path.basename(ini_path)}"})
    except (OSError, UnicodeDecodeError) as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/ini/write", methods=["POST"])
def write_ini():
    ini_path = resolve_active_ini_path()
    if not ini_path:
        return jsonify({"success": False, "error": "No hay INI activo"}), 404

    content = request.json.get("content", "") if request.json else ""
    if not isinstance(content, str):
        return jsonify({"success": False, "error": "content debe ser string"}), 400

    if len(content) > 200_000:
        return jsonify({"success": False, "error": "INI demasiado grande"}), 400

    backup_path = ini_path + ".bak"
    try:
        with open(ini_path, "r", encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        original = None

    try:
        old_config = load_config(ini_path)
        old_streams = {s["name"]: s for s in old_config.get_all_streams()}
    except (configparser.Error, OSError, ValueError) as e:
        return jsonify({"success": False, "error": f"INI actual inválido: {e}"}), 400

    ini_dir = os.path.dirname(ini_path) or "."
    os.makedirs(ini_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".astra-", dir=ini_dir, suffix=".ini")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except (OSError, UnicodeEncodeError) as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return jsonify({"success": False, "error": str(e)}), 500

    try:
        new_config = load_config(tmp_path)
        new_streams = {s["name"]: s for s in new_config.get_all_streams()}
    except (configparser.Error, OSError, ValueError) as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return jsonify({
            "success": False,
            "error": f"INI nuevo inválido, no se aplicó: {e}",
        }), 400

    try:
        os.replace(tmp_path, ini_path)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return jsonify({"success": False, "error": str(e)}), 500

    old_names = set(old_streams.keys())
    new_names = set(new_streams.keys())
    removed = old_names - new_names
    added = new_names - old_names
    changed = set()

    for name in old_names & new_names:
        old_s = old_streams[name]
        new_s = new_streams[name]
        if (
            old_s["original_url"] != new_s["original_url"]
            or old_s["destination_url"] != new_s["destination_url"]
            or old_s["ffmpeg_pre_options"] != new_s["ffmpeg_pre_options"]
            or old_s["ffmpeg_post_options"] != new_s["ffmpeg_post_options"]
            or old_s.get("ffmpeg_path", "") != new_s.get("ffmpeg_path", "")
            or old_s.get("vaapi_driver", "") != new_s.get("vaapi_driver", "")
            or old_s.get("vlc_transcode", "") != new_s.get("vlc_transcode", "")
            or old_s.get("vlc_port") != new_s.get("vlc_port")
        ):
            changed.add(name)

    for name in removed:
        with stream_manager._lock:
            if name in stream_manager.streams:
                stream_manager.stop_stream(name)
                del stream_manager.streams[name]

    for name in added:
        stream_manager.register_stream(name, new_streams[name])
        if new_streams[name].get("autostart") or START_ALL_ON_BOOT:
            stream_manager.start_stream(name)

    for name in changed:
        if name in stream_manager.streams:
            was_running = stream_manager.streams[name].status == "running"
            stream_manager.register_stream(name, new_streams[name])
            if was_running:
                stream_manager.restart_stream(name)

    if original is not None:
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(original)
        except OSError:
            logger.warning(f"No se pudo escribir backup {backup_path}")

    return jsonify({
        "success": True,
        "removed": list(removed),
        "added": list(added),
        "changed": list(changed),
    })


def _sanitize_title(value):
    """Limpia y valida el título de plataforma antes de guardarlo."""
    if not isinstance(value, str):
        return None
    cleaned = _TITLE_SANITIZER.sub("", value.strip()[:_MAX_TITLE_LEN])
    return cleaned or None


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "start_all_on_boot": START_ALL_ON_BOOT,
        "midnight_restart": MIDNIGHT_RESTART_ENABLED,
        "auto_restart": AUTO_RESTART_ENABLED,
        "platform_title": PLATFORM_TITLE,
        "network": NET_INTERFACE,
    })


@app.route("/api/config", methods=["POST"])
def set_config():
    global START_ALL_ON_BOOT, MIDNIGHT_RESTART_ENABLED, AUTO_RESTART_ENABLED, PLATFORM_TITLE
    if not request.json:
        return jsonify({"success": False, "error": "JSON requerido"}), 400

    with _CONFIG_LOCK:
        if "start_all_on_boot" in request.json:
            val = bool(request.json["start_all_on_boot"])
            START_ALL_ON_BOOT = val
            app_config["start_all_on_boot"] = val
        if "midnight_restart" in request.json:
            val = bool(request.json["midnight_restart"])
            MIDNIGHT_RESTART_ENABLED = val
            app_config["midnight_restart"] = val
        if "auto_restart" in request.json:
            val = bool(request.json["auto_restart"])
            AUTO_RESTART_ENABLED = val
            app_config["auto_restart"] = val
        if "platform_title" in request.json:
            cleaned = _sanitize_title(request.json["platform_title"])
            if cleaned is None:
                return jsonify({"success": False, "error": "Título vacío"}), 400
            PLATFORM_TITLE = cleaned
            app_config["platform_title"] = cleaned

        try:
            save_app_config(app_config)
        except OSError as e:
            logger.exception("Error guardando config")
            return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({
        "success": True,
        "start_all_on_boot": START_ALL_ON_BOOT,
        "midnight_restart": MIDNIGHT_RESTART_ENABLED,
        "auto_restart": AUTO_RESTART_ENABLED,
        "platform_title": PLATFORM_TITLE,
    })


@app.route("/api/errors", methods=["GET"])
def get_errors():
    errors = stream_manager.get_error_history()
    return jsonify({"errors": errors})


# -------- Multi-config (carpeta db/) ----------------------------------------

_INI_TEMPLATE = (
    "[servidor]\n"
    "FFMPEG_PATH=/usr/bin/ffmpeg\n"
    "\n"
    "[01_Ejemplo]\n"
    "Original_URL=https://origen.example.com/stream\n"
    "Destination_URL=rtmp://127.0.0.1:1935/live/ejemplo\n"
    "FFMPEG_PRE_OPTIONS=-re\n"
    "FFMPEG_POST_OPTIONS=-c copy -f flv\n"
    "autostart=true\n"
)


def _config_payload(name, full, size, mtime):
    """Convierte una entrada (name, full, size, mtime) al JSON público."""
    active = get_active_ini_name() or ""
    return {
        "name": name,
        "path": f"db/{name}",
        "size": size,
        "modified": mtime,
        "active": name == active,
        "streams": count_streams_in_ini(full),
    }


@app.route("/api/configs", methods=["GET"])
def configs_list():
    items = [_config_payload(n, full, sz, mt) for (n, full, sz, mt) in list_db_configs()]
    active = get_active_ini_name()
    ini_path = resolve_active_ini_path()
    return jsonify({
        "success": True,
        "configs": items,
        "active": active,
        "active_path": ini_path,
        "db_dir": DB_DIR,
        "env_override": bool((os.environ.get("ASTRA_INI") or "").strip()),
    })


@app.route("/api/configs", methods=["POST"])
def configs_create():
    data = request.json or {}
    name = _validate_config_name(data.get("name", ""))
    if not name:
        return jsonify({"success": False, "error": "Nombre inválido. Usa solo letras, números, guiones y la extensión .ini."}), 400
    target = os.path.join(DB_DIR, name)
    if os.path.isfile(target):
        return jsonify({"success": False, "error": f"{name} ya existe"}), 409
    content = data.get("content")
    if content is None:
        content = _INI_TEMPLATE
    if not isinstance(content, str):
        return jsonify({"success": False, "error": "content debe ser string"}), 400
    try:
        load_config_from_content(content).get_all_streams()  # validación de parseo
    except (configparser.Error, ValueError) as e:
        return jsonify({"success": False, "error": f"INI inválido: {e}"}), 400
    os.makedirs(DB_DIR, exist_ok=True)
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    if data.get("activate"):
        cfg = load_app_config()
        cfg["active_config"] = f"db/{name}"
        try:
            save_app_config(cfg)
        except OSError as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "name": name, "path": f"db/{name}"})


@app.route("/api/configs/activate", methods=["POST"])
def configs_activate():
    data = request.json or {}
    target = find_db_config(data.get("name", ""))
    if not target or not os.path.isfile(target):
        return jsonify({"success": False, "error": "Configuración no encontrada en db/"}), 404
    base = os.path.basename(target)
    cfg = load_app_config()
    cfg["active_config"] = f"db/{base}"
    try:
        save_app_config(cfg)
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # stop_all + cargar + arrancar
    with stream_manager._lock:
        try:
            stream_manager.stop_all_and_cleanup()
        except Exception as e:
            logger.warning(f"stop_all_and_cleanup: {e}")
        # limpiar referencias al manager
        for name in list(stream_manager.streams.keys()):
            stream_manager.streams.pop(name, None)
        gc.collect()

    loaded = 0
    started = 0
    try:
        new_config = load_config(target)
        for s in new_config.get_all_streams():
            stream_manager.register_stream(s["name"], s)
            loaded += 1
            if s.get("autostart") or START_ALL_ON_BOOT:
                stream_manager.start_stream(s["name"])
                time.sleep(2)
                started += 1
    except (configparser.Error, OSError, ValueError) as e:
        return jsonify({"success": False, "error": f"Error cargando {base}: {e}"}), 500

    _apply_deployment_overrides()

    return jsonify({
        "success": True,
        "active": base,
        "loaded": loaded,
        "started": started,
        "platform_title": PLATFORM_TITLE,
        "network": NET_INTERFACE,
    })


@app.route("/api/configs/<path:name>", methods=["DELETE"])
def configs_delete(name):
    target = find_db_config(name)
    if not target or not os.path.isfile(target):
        return jsonify({"success": False, "error": "No encontrada"}), 404
    active_path = resolve_active_ini_path()
    if active_path and os.path.abspath(active_path) == os.path.abspath(target):
        return jsonify({"success": False, "error": "No se puede borrar la configuración activa. Activa otra primero."}), 400
    try:
        os.unlink(target)
        bak = target + ".bak"
        if os.path.isfile(bak):
            os.unlink(bak)
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


if __name__ == "__main__":
    if "--init" in sys.argv:
        migrated = migrate_root_to_db()
        os.makedirs(DB_DIR, exist_ok=True)
        existing = list_db_configs()
        active = get_active_ini_name()
        if migrated:
            print(f"[init] Migrado cadena_rcn.ini → db/{migrated}")
        elif not existing and not active:
            template = os.path.join(BASE_DIR, "cadena_rcn.ini.example")
            if os.path.isfile(template):
                name = "1_local.ini"
                target = os.path.join(DB_DIR, name)
                with open(template, "r", encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
                cfg = load_app_config()
                cfg["active_config"] = f"db/{name}"
                save_app_config(cfg)
                print(f"[init] Creado db/{name} desde cadena_rcn.ini.example")
            else:
                print("[init] db/ vacío y no hay cadena_rcn.ini.example. Ejecuta la UI para crear uno.")
        else:
            print(f"[init] db/ ya contiene {len(existing)} configuraciones, activa: {active or '(ninguna)'}")
        sys.exit(0)

    migrate_root_to_db()
    count = initialize_streams()
    active = get_active_ini_name() or "(ninguna)"
    print(f"Loaded {count} streams from db/{active}")

    monitor.start()
    scheduler.start()
    print("Monitor y scheduler iniciados")

    try:
        app.run(host="0.0.0.0", port=5006, debug=False, threaded=True)
    finally:
        scheduler._running = False
        monitor.stop()
        stream_manager.stop_all_and_cleanup()
