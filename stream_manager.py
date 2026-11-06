import gc
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_DEFAULT_TITLE = "Sistema de Emisión 24/7-Astra"

# Circuit breaker: cuanto tarda cada backoff antes del siguiente reintento.
# Tras 4to fallo consecutivo, el stream queda en estado 'error' y NO se
# reintenta mas. Solo un start manual resetea el contador.
_RETRY_BACKOFF_SECONDS = [5, 15, 30, 60]
_MAX_CONSECUTIVE_FAILURES = 4
_STABLE_RUNNING_SECONDS = 60  # tiempo en 'running' que resetea el breaker

# Patrones de error realmente irrecuperables: el stream NO se reintenta solo.
# Todo lo demas (5xx, timeouts, connection refused, DNS caido, frames
# corruptos) es TRANSITORIO y debe reintentarse con backoff: mataba streams
# de forma permanente ante un hipo de 1 s del origen o del destino.
_FATAL_ERROR_PATTERNS = (
    "404 not found",
    "no such file or directory",
    "protocol not found",
)

# Patrones transitorios que antes se trataban como fatales. Se listan aqui
# solo para logging/observabilidad (no cambian el comportamiento).
_TRANSIENT_ERROR_PATTERNS = (
    "server returned 4",
    "server returned 5",
    "error opening input",
    "invalid data found",
    "name or service not known",
    "connection refused",
    "connection timed out",
    "conversion failed",
    "input/output error",
    "broken pipe",
)

# Pipeline de 2 etapas: la GPU NVIDIA (NVDEC + scale_cuda) decodifica y escala,
# y la iGPU (VAAPI) solo codifica. Los frames viajan por un pipe NUT.
_NV_PIPE_DIR = "/tmp/astra_nv"
_NV_RENDER_NODE = "/dev/dri/renderD128"


def _is_fatal_error(message):
    if not message:
        return False
    low = message.lower()
    return any(p in low for p in _FATAL_ERROR_PATTERNS)


def _is_transient_error(message):
    if not message:
        return False
    low = message.lower()
    return any(p in low for p in _TRANSIENT_ERROR_PATTERNS)


def _kill_process_tree(proc, label, term_timeout=10, kill_timeout=5, group=True):
    """Termina un proceso y su grupo de procesos. NUNCA lanza una excepcion.

    Devuelve True si el proceso murio, False si sobrevive (queda huerfano).

    Por que no puede lanzar excepcion: stop() se llama desde el boton
    "Reiniciar" y desde el monitor. Si stop() lanza, el reinicio se corta a
    medias: el proceso sigue vivo pero deja de estar controlado, el pipe no
    se borra y el stream NO vuelve a arrancar. Eso fue exactamente lo que
    paso con 'Conversion failed!': proc.wait() lanzaba TimeoutExpired (que
    hereda de SubprocessError, no de OSError) y el `except (OSError,
    ValueError)` no lo cubria. El monitor se comia la excepcion y el canal
    se quedaba caido.

    `group` debe ser False para procesos que NO se arrancaron con
    start_new_session=True: matarian a todo el grupo, incluida la app.
    """
    if proc is None:
        return True

    try:
        if proc.poll() is not None:
            return True  # ya estaba muerto
        pid = proc.pid
    except (OSError, ValueError):
        return True

    def _signal_group(sig):
        """Envia una senal a todo el grupo de procesos del ffmpeg."""
        if not group:
            return False
        try:
            target_pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        if target_pgid == os.getpgrp():
            # Peligro: ese proceso comparte grupo con la app. Matar al grupo
            # mataria tambien a la app y a TODOS sus streams.
            return False
        try:
            os.killpg(target_pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    # 1) SIGTERM al grupo (el ffmpeg y cualquier hijo que haya creado).
    if not _signal_group(signal.SIGTERM):
        try:
            proc.terminate()
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=term_timeout)
        return True
    except subprocess.TimeoutExpired:
        pass
    except (OSError, ValueError):
        return True

    # 2) SIGKILL al grupo.
    logger.warning("%s (pid %s) no termino con SIGTERM, usando SIGKILL", label, pid)
    if not _signal_group(signal.SIGKILL):
        try:
            proc.kill()
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=kill_timeout)
        return True
    except subprocess.TimeoutExpired:
        logger.error(
            "%s (pid %s) sobrevive SIGKILL: queda huerfano y lo eliminara el reaper",
            label,
            pid,
        )
        return False
    except (OSError, ValueError):
        return True


def _close_process_pipes(proc):
    """Cierra los descriptores de un Popen. Nunca lanza."""
    if proc is None:
        return
    for attr in ("stderr", "stdout", "stdin"):
        try:
            handle = getattr(proc, attr, None)
            if handle:
                handle.close()
        except (OSError, ValueError):
            pass


def _iter_process_cmdlines():
    """Recorre /proc y devuelve (pid, argv) de cada proceso con linea de comandos."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                raw = handle.read()
        except (OSError, ValueError):
            continue
        if not raw:
            continue
        parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
        if parts:
            yield int(entry), parts


def _pid_alive(pid):
    """True si el pid sigue existiendo."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_matches(pid, destinations):
    """Re-verifica que un pid sigue siendo uno de los nuestros.

    Se usa justo antes de matar: entre el escaneo y el kill el pid pudo
    liberarse y ser reutilizado por otro proceso.
    """
    for candidate, parts in _iter_process_cmdlines():
        if candidate != pid:
            continue
        return _is_our_ffmpeg(parts, destinations)
    return False


def _looks_like_ffmpeg(parts):
    """True si la linea de comandos corresponde a un ffmpeg.

    Se mira argv[0] y argv[1]: un ffmpeg invocado a traves de un interprete
    (script con shebang) tiene el interprete como argv[0].
    """
    if not parts:
        return False
    for candidate in parts[:2]:
        if os.path.basename(candidate) == "ffmpeg":
            return True
    return False


def _is_our_ffmpeg(parts, destinations):
    """True si la linea de comandos es un ffmpeg nuestro (pipeline NVIDIA o
    publicando a una Destination_URL nuestra)."""
    if not _looks_like_ffmpeg(parts):
        return False
    if any(_NV_PIPE_DIR in part for part in parts):
        return True
    return parts[-1] in destinations


def _pid_ppid(pid):
    """Devuelve el pid del padre de un proceso, o None si no se puede leer."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return None
    try:
        # El campo comm puede llevar espacios y parentesis: se parte por el
        # ultimo ')'. Tras el va el estado y luego el ppid.
        rest = data[data.rindex(b")") + 2:].split()
        return int(rest[1])
    except (ValueError, IndexError):
        return None


def _pid_age_seconds(pid):
    """Edad del proceso en segundos (0.0 si no se puede determinar)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
        rest = data[data.rindex(b")") + 2:].split()
        # starttime es el campo 22 del stat; tras partir por el ultimo ')',
        # rest[0] es el campo 3 (state): starttime = rest[19].
        starttime_ticks = int(rest[19])
    except (OSError, ValueError, IndexError):
        return 0.0
    try:
        with open("/proc/uptime", "rb") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    try:
        ticks = os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, OSError, AttributeError):
        ticks = 100
    age = uptime - (starttime_ticks / float(ticks))
    return age if age > 0 else 0.0


# --- Sonda de audio --------------------------------------------------------
# El monitorHistorically solo miraba el contador de frames de VIDEO. Un canal
# con video vivo y audio mudo pasaba por 'sano' para siempre y se quedaba
# mudo indefinidamente. Esta sonda lee la URL realmente publicada y cuenta
# los paquetes de audio que llegan de verdad.
#
# El binario importa: el ffprobe del sistema (/usr/bin/ffprobe) carga
# libva-x11 y peta con "undefined symbol: va_fool_postp" si hereda el
# LD_LIBRARY_PATH=/opt/libva-new/lib del servicio. Por eso se usa el mismo
# build estatico que el ffmpeg de emision (/usr/local/bin/ffprobe) y se le
# limpia el entorno de VAAPI.
def _pick_ffprobe():
    for candidate in ("/usr/local/bin/ffprobe", "/usr/bin/ffprobe"):
        if os.path.exists(candidate):
            return candidate
    return "ffprobe"


_FFPROBE_BIN = _pick_ffprobe()

# Un ffmpeg recien arrancado puede no estar todavia registrado en ningun
# stream (la ventana entre Popen y note_spawn). Con esta gracia por edad el
# reaper nunca se lleva por delante una emision en curso.
ORPHAN_MIN_AGE_SECONDS = 120


def _ffprobe_env():
    """Entorno limpio para ffprobe: sin LD_LIBRARY_PATH de VAAPI."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("LIBVA") or key == "LD_LIBRARY_PATH":
            env.pop(key, None)
    return env


def probe_audio_packets(url, seconds=8, timeout=25):
    """Cuenta los paquetes de audio que llegan realmente por `url`.

    Devuelve un dict con:
      state         'ok'        hay pista de audio y llegan paquetes
                    'missing'   hay pista de audio pero NO llega ningun paquete
                                (el canal esta mudo: es el fallo que buscamos)
                    'no_track'  la URL no publica ninguna pista de audio
                    'error'     no se pudo sondear
      packets       paquetes de audio leidos
      video_packets paquetes de video leidos
      detail        detalle del error, si lo hubo
    """
    empty = {"state": "error", "packets": 0, "video_packets": 0, "detail": "sin url"}
    if not url:
        return empty
    cmd = [
        _FFPROBE_BIN, "-v", "error", "-count_packets",
        "-read_intervals", f"%+{int(seconds)}",
        "-show_entries", "stream=index,codec_type,codec_name,nb_read_packets",
        "-of", "json", url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=_ffprobe_env(),
        )
    except subprocess.TimeoutExpired:
        return {"state": "error", "packets": 0, "video_packets": 0, "detail": "timeout"}
    except (OSError, ValueError) as exc:
        return {
            "state": "error", "packets": 0, "video_packets": 0,
            "detail": str(exc)[:200],
        }
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return {
            "state": "error", "packets": 0, "video_packets": 0,
            "detail": (detail[-1] if detail else "ffprobe failed")[:200],
        }
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return {
            "state": "error", "packets": 0, "video_packets": 0,
            "detail": "json invalido de ffprobe",
        }

    audio_packets = 0
    video_packets = 0
    has_audio = False
    for entry in (data.get("streams") or []):
        try:
            count = int(entry.get("nb_read_packets") or 0)
        except (TypeError, ValueError):
            count = 0
        if entry.get("codec_type") == "audio":
            has_audio = True
            audio_packets = max(audio_packets, count)
        elif entry.get("codec_type") == "video":
            video_packets = max(video_packets, count)

    if not has_audio:
        return {
            "state": "no_track", "packets": 0, "video_packets": video_packets,
            "detail": "la URL no publica pista de audio",
        }
    if audio_packets <= 0:
        return {
            "state": "missing", "packets": 0, "video_packets": video_packets,
            "detail": "pista de audio declarada pero 0 paquetes recibidos",
        }
    return {
        "state": "ok", "packets": audio_packets, "video_packets": video_packets,
        "detail": "",
    }


class StreamInstance:
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.process = None
        self._vlc_process = None
        self._vlc_port = None
        self._vlc_url = None
        self._status = "stopped"
        self.bitrate = "0 kbps"
        self.video_bitrate = ""
        self.audio_bitrate = ""
        self.file_size = ""
        self.speed = ""
        self._last_frame_time = None
        self._last_ffmpeg_time_value = 0
        self._last_size_value = 0
        self.start_time = None
        self.restart_count = 0
        self.error_message = ""
        # Circuit breaker / backoff
        self._consecutive_failures = 0
        self._last_failure_at = 0.0
        self._next_retry_at = 0.0
        self._blocked_reason = ""
        self._lock = threading.RLock()
        self._logs_lock = threading.RLock()
        self._status_lock = threading.RLock()
        # Serializa start/stop de un mismo stream (evita que un reinicio del
        # monitor y uno manual se pisen).
        self._op_lock = threading.Lock()
        self.last_lines = deque(maxlen=1000)
        self._output_thread = None
        # Pipeline NVIDIA (2 etapas): la etapa 1 (NVDEC) escribe en un pipe y
        # la etapa 2 (encode VAAPI) lo consume desde self.process.
        self._stage_process = None
        self._pipe_path = None
        self._stage_output_thread = None
        # Pids que sobrevivieron a SIGTERM+SIGKILL en un stop(): quedan
        # publicando en paralelo y el SRS rechaza al nuevo publisher con
        # StreamBusy (1028). Los lleva el reaper.
        self._leaked_pids = []
        # Salida de procesos que ya no controlamos (huerfanos). Se guarda
        # aparte: si se mezclara en last_lines, el monitor atribuiria su
        # "Conversion failed!" al stream vivo y lo mataria en bucle.
        self.orphan_lines = deque(maxlen=200)
        # Control de "episodio de emision": un episodio empieza cuando el
        # proceso arranca OK y termina cuando muere o se congela. Permite
        # contar UN fallo por episodio real (antes nunca se contaban y el
        # circuit breaker no se cerraba nunca).
        self._episode_active = False
        # Tope de reinicios por hora (anti-tormenta) y cooldown por stream.
        self._restart_times = deque(maxlen=60)
        # Salud de audio. El monitor solo miraba video: un canal con video
        # vivo y audio mudo pasaba por 'sano' y se quedaba mudo para siempre.
        self.audio_state = "unknown"   # unknown | ok | missing | no_track | error
        self.audio_packets = 0
        self.audio_video_packets = 0
        self.audio_checked_at = 0.0
        self.audio_detail = ""
        self.audio_strikes = 0
        self._audio_restart_times = deque(maxlen=20)
        self._audio_source_state = ""
        self._audio_source_checked_at = 0.0

    def register_failure(self, message=""):
        """Registra una falla y devuelve (should_retry, fatal, backoff_seconds)."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_at = time.time()
            fatal = _is_fatal_error(message or self.error_message)
            if fatal:
                self._blocked_reason = "fatal_error"
                self._next_retry_at = 0.0
                return False, True, 0
            idx = min(self._consecutive_failures - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
            backoff = _RETRY_BACKOFF_SECONDS[idx]
            should_retry = self._consecutive_failures < _MAX_CONSECUTIVE_FAILURES
            if not should_retry:
                self._blocked_reason = "max_failures"
                self._next_retry_at = 0.0
                return False, False, backoff
            self._blocked_reason = ""
            self._next_retry_at = time.time() + backoff
            return True, False, backoff

    def mark_episode_started(self):
        """Marca que el proceso arranco OK: hay un episodinio en curso."""
        with self._lock:
            self._episode_active = True

    def register_episode_end(self):
        """Cierra el episodio en curso. Devuelve True si habia uno abierto.

        Es idempotente: solo el primer cierre cuenta como fallo, de modo que
        un mismo episodio (proceso que muere) no se contabiliza dos veces.
        """
        with self._lock:
            if not self._episode_active:
                return False
            self._episode_active = False
            return True

    def note_restart(self):
        """Registra el instante de un reinicio (para el tope por hora)."""
        with self._lock:
            self._restart_times.append(time.time())

    def restarts_last_hour(self):
        cutoff = time.time() - 3600
        with self._lock:
            return sum(1 for t in self._restart_times if t >= cutoff)

    # --- Salud de audio ----------------------------------------------------

    def note_audio_probe(self, result):
        """Guarda el resultado del sondeo de audio de la URL publicada."""
        with self._lock:
            self.audio_state = (result or {}).get("state", "unknown")
            self.audio_packets = (result or {}).get("packets", 0)
            self.audio_video_packets = (result or {}).get("video_packets", 0)
            self.audio_detail = ((result or {}).get("detail") or "")[:200]
            self.audio_checked_at = time.time()
            return self.audio_state

    def note_audio_strike(self):
        """Suma un fallo de audio consecutivo. Devuelve el nuevo total."""
        with self._lock:
            self.audio_strikes += 1
            return self.audio_strikes

    def clear_audio_strikes(self):
        with self._lock:
            self.audio_strikes = 0

    def note_audio_restart(self):
        """Registra un reinicio provocado por falta de audio."""
        with self._lock:
            self._audio_restart_times.append(time.time())

    def audio_restarts_last_hour(self):
        cutoff = time.time() - 3600
        with self._lock:
            return sum(1 for t in self._audio_restart_times if t >= cutoff)

    def source_audio_state(self, ttl=300):
        """Estado del audio en el ORIGEN, con cache para no sondear siempre.

        Sirve para distinguir dos fallos muy distintos:
          - origen sin audio  -> reiniciar el ffmpeg NO arregla nada
          - origen con audio, salida muda -> reiniciar el ffmpeg SI lo arregla
        """
        now = time.time()
        with self._lock:
            if self._audio_source_state and (now - self._audio_source_checked_at) < ttl:
                return self._audio_source_state
        url = (self.config or {}).get("original_url")
        if not url:
            return "unknown"
        result = probe_audio_packets(url)
        state = result.get("state", "error")
        with self._lock:
            self._audio_source_state = state
            self._audio_source_checked_at = time.time()
        return state

    def reset_failures(self):
        with self._lock:
            self._consecutive_failures = 0
            self._next_retry_at = 0.0
            self._blocked_reason = ""
            self._episode_active = False

    def is_blocked(self):
        with self._lock:
            if self._blocked_reason:
                return True, self._blocked_reason
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                return True, "max_failures"
            return False, ""

    def get_retry_state(self):
        with self._lock:
            return {
                "consecutive_failures": self._consecutive_failures,
                "last_failure_at": self._last_failure_at,
                "next_retry_at": self._next_retry_at,
                "blocked": bool(self._blocked_reason),
                "blocked_reason": self._blocked_reason,
            }

    def maybe_reset_on_stable(self):
        """Si el stream lleva > _STABLE_RUNNING_SECONDS en running, resetea el breaker."""
        if self._consecutive_failures == 0:
            return False
        if self.start_time is None or self.status != "running":
            return False
        if time.time() - self.start_time >= _STABLE_RUNNING_SECONDS:
            self.reset_failures()
            return True
        return False

    @property
    def status(self):
        with self._status_lock:
            return self._status

    @status.setter
    def status(self, value):
        with self._status_lock:
            self._status = value

    @property
    def last_frame_time(self):
        with self._logs_lock:
            return self._last_frame_time

    @last_frame_time.setter
    def last_frame_time(self, value):
        with self._logs_lock:
            self._last_frame_time = value

    def build_ffmpeg_command(self, override_url=None):
        ffmpeg_path = self.config.get("ffmpeg_path") or "ffmpeg"
        pre_opts = self.config.get("ffmpeg_pre_options", "")
        original_url = override_url or self.config["original_url"]
        post_opts = self.config.get("ffmpeg_post_options", "")
        destination_url = self.config["destination_url"]

        cmd = [ffmpeg_path, "-re"]
        if pre_opts:
            cmd.extend(shlex.split(pre_opts))
        cmd.extend(["-i", original_url])
        if post_opts:
            cmd.extend(shlex.split(post_opts))
        cmd.append(destination_url)
        return cmd

    # ---------- pipeline NVIDIA (decodificar/escalar en la NVIDIA) ----------

    def _uses_nvidia_pipeline(self):
        return (self.config.get("decoder") or "").strip().lower() == "nvidia"

    @staticmethod
    def _split_vf(tokens):
        """Separa la cadena de filtros -vf del resto de los tokens."""
        filters, rest, in_vf = [], [], False
        for t in tokens:
            if t == "-vf":
                in_vf = True
                continue
            if in_vf:
                if t.startswith("-"):
                    in_vf = False
                    rest.append(t)
                    continue
                filters.extend(t.split(","))
                continue
            rest.append(t)
        return [f for f in filters if f], rest

    @staticmethod
    def _target_size(filters, default="720:408"):
        """Deduce el tamano destino de la escala configurada."""
        for f in filters:
            for key in ("scale_vaapi=", "scale="):
                if f.startswith(key):
                    parts = f[len(key):].split(":")
                    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                        return f"{parts[0]}:{parts[1]}"
        return default

    def _make_pipe_path(self):
        os.makedirs(_NV_PIPE_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.name)[:60]
        return os.path.join(
            _NV_PIPE_DIR, f"{safe}.{os.getpid()}.{int(time.time() * 1000)}.nut"
        )

    def build_ffmpeg_commands(self, override_url=None):
        """Devuelve la lista de comandos: 1 (clasico) o 2 (pipeline NVIDIA)."""
        if not self._uses_nvidia_pipeline():
            return [self.build_ffmpeg_command(override_url)]

        ffmpeg_path = self.config.get("ffmpeg_path") or "ffmpeg"
        pre_opts = self.config.get("ffmpeg_pre_options", "")
        original_url = override_url or self.config["original_url"]
        post_opts = self.config.get("ffmpeg_post_options", "")
        destination_url = self.config["destination_url"]

        # Etapa 1: -vaapi_device no aplica (esa GPU no participa) y solo
        # mantendria el contexto VAAPI de la iGPU abierto.
        clean_pre, i = [], 0
        pre_tokens = shlex.split(pre_opts)
        while i < len(pre_tokens):
            if pre_tokens[i] == "-vaapi_device":
                i += 2
                continue
            clean_pre.append(pre_tokens[i])
            i += 1

        filters, rest_post = self._split_vf(shlex.split(post_opts))
        size = self._target_size(filters)

        # Los filtros de software (bwdif) van a la NVIDIA con su equivalente
        # CUDA; el resto de la escala la hace scale_cuda en la NVIDIA.
        nv_filters = []
        for f in filters:
            if f.startswith("bwdif="):
                opts = {}
                for part in f.split(":")[1:]:
                    if "=" in part:
                        k, v = part.split("=", 1)
                        opts[k] = v
                nv_filters.append(
                    "bwdif_cuda=mode=%s:parity=%s"
                    % (opts.get("mode", "send_frame"), opts.get("parity", "tff"))
                )
        nv_filters.append(f"scale_cuda={size}:interp_algo=bilinear")
        nv_filters.append("hwdownload,format=nv12")

        pipe_path = self._make_pipe_path()
        cmd_stage1 = [ffmpeg_path, "-y", "-re"] + clean_pre + [
            "-hwaccel", "cuvid",
            "-hwaccel_device", "0",
            "-hwaccel_output_format", "cuda",
            "-i", original_url,
            "-vf", ",".join(nv_filters),
            "-c:v", "rawvideo",
            "-c:a", "copy",
            "-f", "nut",
            pipe_path,
        ]

        # Etapa 2: frames crudos -> superficie VAAPI -> encode en la iGPU.
        enc_filters = ["format=nv12", "hwupload"]
        for f in filters:
            name = f.split("=")[0]
            if name in ("scale", "scale_vaapi", "format", "hwupload", "bwdif"):
                continue
            enc_filters.append(f)

        cmd_stage2 = [ffmpeg_path, "-y", "-vaapi_device", _NV_RENDER_NODE,
                      "-f", "nut", "-i", pipe_path,
                      "-vf", ",".join(enc_filters)]
        cmd_stage2 += rest_post
        cmd_stage2.append(destination_url)
        return [cmd_stage1, cmd_stage2]

    def _build_env(self):
        env = os.environ.copy()
        vaapi_driver = self.config.get("vaapi_driver", "")
        if vaapi_driver:
            env["LIBVA_DRIVER_NAME"] = vaapi_driver
        return env

    def _start_vlc(self):
        vlc_transcode = self.config.get("vlc_transcode", "")
        if not vlc_transcode:
            return None

        original_url = self.config["original_url"]
        port = self.config.get("vlc_port")
        if not port:
            port = stream_manager.assign_vlc_port(exclude_name=self.name)
        if not port:
            logger.error(f"No hay puertos VLC libres para '{self.name}'")
            return None
        self._vlc_port = port
        self._vlc_url = f"http://127.0.0.1:{port}"
        stream_manager.reserve_vlc_port(port, self.name)

        # Detener VLC anterior si existe
        self._stop_vlc()

        # Cadena de transcodificación de VLC:
        # Solo transcodifica el audio (AC-3 corrupto → AC-3 limpio),
        # el video pasa sin tocar (passthrough).
        sout = (
            f"#transcode{{{vlc_transcode}}}"
            f":standard{{access=http,mux=ts,dst=127.0.0.1:{port}}}"
        )
        cmd = [
            "cvlc",
            "--no-video-title-show",
            "--no-daemon",
            "--no-interact",
            "--no-stats",
            original_url,
            "--sout", sout,
            "--no-sout-all",
            "--sout-keep",
            "--sout-mux-caching=3000",
            "--network-caching=3000",
        ]

        logger.info(f"Iniciando VLC transcoder para '{self.name}' en puerto {port}")
        try:
            self._vlc_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Esperar a que VLC esté listo (el puerto acepte conexiones)
            import socket as _socket
            for _ in range(30):
                try:
                    with _socket.create_connection(("127.0.0.1", port), timeout=1):
                        logger.info(f"VLC transcoder listo en puerto {port}")
                        return self._vlc_url
                except (OSError, ConnectionRefusedError):
                    if self._vlc_process.poll() is not None:
                        logger.error(f"VLC murió durante el arranque para '{self.name}'")
                        self._vlc_process = None
                        return None
                    time.sleep(1)
            logger.warning(f"VLC no respondió en puerto {port} tras 30s")
            return self._vlc_url
        except (OSError, ValueError) as e:
            logger.error(f"Error iniciando VLC para '{self.name}': {e}")
            self._vlc_process = None
            return None

    def _stop_vlc(self):
        proc = self._vlc_process
        if proc:
            # Mismo criterio que los ffmpeg: nunca propagar una excepcion.
            # Si wait() lanzaba TimeoutExpired, el VLC quedaba vivo y el
            # stop() del stream se cortaba a medias.
            _kill_process_tree(
                proc,
                f"VLC transcoder de '{self.name}'",
                term_timeout=5,
                kill_timeout=3,
            )
            _close_process_pipes(proc)
            logger.info(f"VLC transcoder detenido para '{self.name}'")
            self._vlc_process = None
            if self._vlc_port:
                stream_manager.release_vlc_port(self._vlc_port)
                self._vlc_port = None
                self._vlc_url = None

    def start(self):
        if self.process and self.is_running():
            return False

        # NOTA: NO reseteamos el circuit breaker aqui. Eso se hace:
        #   - explicitamente en StreamManager.start_stream() (boton manual)
        #   - automaticamente cuando el stream lleva >60s en 'running'
        #     (via monitor._maybe_reset_on_stable -> maybe_reset_on_stable)
        # Asi el contador de fallos sobrevive a un restart automatico.

        self._status = "starting"
        self.error_message = ""
        self.last_frame_time = time.time()
        self._last_ffmpeg_time_value = 0
        self._last_size_value = 0
        with self._logs_lock:
            self.last_lines.clear()
        self.bitrate = "0 kbps"
        self.audio_bitrate = ""
        self.file_size = ""
        self.speed = ""

        # Si el stream tiene VLC_TRANSCODE configurado, arrancar VLC primero
        # y usar la URL del VLC como entrada de FFmpeg.
        vlc_url = None
        if self.config.get("vlc_transcode"):
            vlc_url = self._start_vlc()
            if vlc_url is None:
                self._status = "error"
                self.error_message = "VLC transcoder no pudo iniciar"
                logger.error(f"VLC transcoder falló para '{self.name}', no se inicia FFmpeg")
                return False

        try:
            cmds = self.build_ffmpeg_commands(vlc_url)
            if len(cmds) == 2:
                # Pipeline NVIDIA: la etapa 1 (NVDEC) escribe en el pipe y la
                # etapa 2 (encode VAAPI) lo consume. El fifo debe existir
                # antes de arrancar la etapa 1.
                self._pipe_path = cmds[0][-1]
                os.makedirs(_NV_PIPE_DIR, exist_ok=True)
                if not os.path.exists(self._pipe_path):
                    os.mkfifo(self._pipe_path)
                logger.info(
                    f"Iniciando etapa NVIDIA (NVDEC) de '{self.name}': {' '.join(cmds[0])}"
                )
                self._stage_process = subprocess.Popen(
                    cmds[0],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    env=self._build_env(),
                    start_new_session=True,
                )
                stream_manager.note_spawn(self._stage_process.pid)
                self._stage_output_thread = threading.Thread(
                    target=self._read_stage_output, daemon=True
                )
                self._stage_output_thread.start()

            cmd = cmds[-1]
            logger.info(f"Iniciando stream '{self.name}': {' '.join(cmd)}")

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=self._build_env(),
                start_new_session=True,
            )
            stream_manager.note_spawn(self.process.pid)
            self.start_time = time.time()
            self._status = "running"
            self.mark_episode_started()
            self._output_thread = threading.Thread(
                target=self._read_output, daemon=True
            )
            self._output_thread.start()

            return True
        except (OSError, ValueError) as e:
            self._status = "error"
            self.error_message = str(e)
            self.register_failure(str(e))
            logger.exception("Error iniciando stream '%s'", self.name)
            return False

    def _read_output(self):
        # El proceso se captura aqui: si se leyera self.process en cada
        # iteracion, el hilo de la generacion anterior se quedaria leyendo el
        # stderr del proceso NUEVO (robandole sus lineas de progreso) y el
        # monitor veria "stream congelado" con el stream sano.
        proc = self.process
        if proc is None or proc.stderr is None:
            return
        # ffmpeg imprime su progreso (frame=/size=/bitrate=) con '\r', no '\n'.
        # readline() solo corta por '\n', asi que en streams sin mensajes de
        # log intercalados (que si traen '\n') el hilo se queda bloqueado
        # esperando y el progreso nunca se parsea. Por eso leemos crudo y
        # partimos tanto por '\r' como por '\n'.
        buf = b""
        try:
            while True:
                chunk = proc.stderr.read1(4096)
                if not chunk:
                    break
                buf += chunk
                start = 0
                while True:
                    idx_n = buf.find(b"\n", start)
                    idx_r = buf.find(b"\r", start)
                    candidates = [i for i in (idx_n, idx_r) if i != -1]
                    if not candidates:
                        break
                    idx = min(candidates)
                    raw_line, start = buf[start:idx], idx + 1
                    if not raw_line:
                        continue
                    try:
                        text = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    self._parse_output(text)
                    ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
                    with self._logs_lock:
                        self.last_lines.append(f"[{ts}] {text.rstrip()}")
                buf = buf[start:]
        except (OSError, ValueError):
            logger.exception("Error leyendo salida de '%s'", self.name)

    def _read_stage_output(self):
        """Lee el stderr de la etapa NVIDIA (NVDEC) para diagnostics."""
        proc = self._stage_process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = proc.stderr.read1(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace").strip()
                if not text:
                    continue
                ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
                # Si el proceso ya no es el que controlamos (quedo huerfano),
                # su salida va a un buffer aparte: si se metiera en
                # last_lines, el monitor leeria su "Conversion failed!" como
                # error del stream vivo y lo reiniciaria en bucle.
                if self._stage_process is proc:
                    with self._logs_lock:
                        self.last_lines.append(f"[{ts}] [nv] {text}")
                    if "error" in text.lower() or "failed" in text.lower():
                        self.error_message = text[:200]
                else:
                    with self._logs_lock:
                        self.orphan_lines.append(f"[{ts}] [nv huerfano] {text}")
        except (OSError, ValueError):
            pass

    def _parse_output(self, line):
        # Cualquier progreso (frame=, size=, time= valido) marca latido.
        progress_seen = False

        bitrate_match = re.search(
            r"bitrate[=:\s]+(-?\d+\.?\d*)\s*(kbits?/s|Mbps|bps)", line, re.IGNORECASE
        )
        if bitrate_match:
            self.bitrate = f"{bitrate_match.group(1)} {bitrate_match.group(2).upper()}"

        audio_bitrate_match = re.search(r"Audio:.*?(\d+)\s*(kbps)", line, re.IGNORECASE)
        if audio_bitrate_match:
            self.audio_bitrate = f"{audio_bitrate_match.group(1)} kbps"

        size_match = re.search(r"size\s*=\s*(\d+)\s*ki?B", line, re.IGNORECASE)
        if size_match:
            kb = int(size_match.group(1))
            self.file_size = f"{kb / 1000:.1f}MB" if kb > 1000 else f"{kb}KB"
            with self._logs_lock:
                self._last_size_value = max(self._last_size_value, kb)
            progress_seen = True

        speed_match = re.search(r"speed\s*=\s*(\d+\.?\d*)x?", line, re.IGNORECASE)
        if speed_match:
            self.speed = f"{speed_match.group(1)}x"

        # time= puede tener horas negativas (al inicio el PTS es indefinido).
        # Validamos que sea un timestamp razonable.
        time_match = re.search(r"time[=:](-?\d{1,5}):(\d{2}):(\d{2})", line)
        if time_match:
            h, m, s = int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))
            if 0 <= h < 100000 and 0 <= m < 60 and 0 <= s < 60:
                time_value = h * 3600 + m * 60 + s
                with self._logs_lock:
                    self._last_ffmpeg_time_value = max(self._last_ffmpeg_time_value, time_value)
                progress_seen = True

        # frame= es la mejor senal de que ffmpeg esta procesando.
        # Matchear cualquier numero, con o sin espacios.
        frame_match = re.search(r"frame=\s*(\d+)", line)
        if frame_match:
            progress_seen = True

        if progress_seen:
            self.last_frame_time = time.time()

        if "error" in line.lower() or "failed" in line.lower():
            self.error_message = line.strip()[:200]

    def get_last_error_from_logs(self):
        with self._logs_lock:
            lines = list(self.last_lines)
        for line in reversed(lines):
            low = line.lower()
            if "error" in low or "failed" in low:
                return line.strip()[:200]
        return self.error_message or "Process died without error message"

    def stop(self):
        """Detiene el stream. NUNCA lanza una excepcion.

        Orden importante: primero se mata (encoder y etapa NVIDIA), y solo
        despues se sueltan las referencias. Antes se soltaban ANTES de
        comprobar la muerte: si el wait() lanzaba TimeoutExpired, el proceso
        quedaba vivo y sin control (huerfano) publicando en paralelo, el
        pipe no se borraba y el stream no volvia a arrancar.
        """
        leaked = []

        proc = self.process
        if proc is not None:
            if not _kill_process_tree(proc, f"Stream '{self.name}' (encoder)"):
                leaked.append(proc.pid)
            _close_process_pipes(proc)
        self.process = None

        # Etapa NVIDIA: se detiene despues del encoder para que no quede
        # escribiendo en un pipe sin lector.
        proc = self._stage_process
        if proc is not None:
            if not _kill_process_tree(proc, f"Stream '{self.name}' (etapa NVIDIA)"):
                leaked.append(proc.pid)
            _close_process_pipes(proc)
        self._stage_process = None

        if leaked:
            self._leaked_pids.extend(leaked)
            logger.error(
                "Stream '%s' dejo procesos sin terminar (huerfanos): %s",
                self.name,
                leaked,
            )

        if self._pipe_path:
            try:
                os.unlink(self._pipe_path)
            except OSError:
                pass
            self._pipe_path = None
        self._stop_vlc()
        self._status = "stopped"
        self.start_time = None
        logger.info(f"Stream '{self.name}' detenido")

    def cleanup(self):
        self.stop()
        with self._logs_lock:
            self.last_lines.clear()
        self.bitrate = "0 kbps"
        self.video_bitrate = ""
        self.audio_bitrate = ""
        self.file_size = ""
        self.speed = ""
        self.error_message = ""
        self._last_frame_time = None
        self._last_ffmpeg_time_value = 0
        self._output_thread = None
        logger.info(f"Stream '{self.name}' limpieza completada")

    def is_running(self):
        if self.process is None:
            return False
        if self.process.poll() is not None:
            return False
        # Si la etapa NVIDIA murio, el encoder se queda sin frames: el
        # stream esta caido aunque el proceso de encode siga vivo.
        if self._stage_process is not None and self._stage_process.poll() is not None:
            return False
        return True

    def get_uptime(self):
        if self.start_time and self.status == "running":
            return int(time.time() - self.start_time)
        return 0

    def get_info(self):
        with self._logs_lock:
            last_lines_snapshot = list(self.last_lines)[-50:]
        with self._status_lock:
            status = self._status
        cfg = self.config or {}
        retry_state = self.get_retry_state()
        blocked, blocked_reason = self.is_blocked()
        return {
            "name": self.name,
            "status": status,
            "bitrate": self.bitrate,
            "audio_bitrate": self.audio_bitrate,
            "file_size": self.file_size,
            "speed": self.speed,
            "uptime": (
                int(time.time() - self.start_time)
                if self.start_time and status == "running"
                else 0
            ),
            "restart_count": self.restart_count,
            "error_message": self.error_message,
            "audio_state": self.audio_state,
            "audio_packets": self.audio_packets,
            "audio_detail": self.audio_detail,
            "audio_strikes": self.audio_strikes,
            "audio_checked_at": self.audio_checked_at,
            "audio_restarts_last_hour": self.audio_restarts_last_hour(),
            "alive": self.is_running(),
            "pid": self.process.pid if self.process and self.is_running() else None,
            "vlc": {
                "enabled": bool(cfg.get("vlc_transcode")),
                "active": self._vlc_process is not None
                and self._vlc_process.poll() is None,
                "pid": self._vlc_process.pid
                if self._vlc_process and self._vlc_process.poll() is None
                else None,
                "port": self._vlc_port,
                "url": self._vlc_url,
            },
            "original_url": cfg.get("original_url", ""),
            "destination_url": cfg.get("destination_url", ""),
            "ffmpeg_path": cfg.get("ffmpeg_path", ""),
            "vaapi_driver": cfg.get("vaapi_driver", ""),
            "decoder": cfg.get("decoder", ""),
            "pipeline": "nvidia-nvdec" if self._uses_nvidia_pipeline() else "cpu-decode",
            "ffmpeg_pre_options": cfg.get("ffmpeg_pre_options", ""),
            "ffmpeg_post_options": cfg.get("ffmpeg_post_options", ""),
            "vlc_transcode": cfg.get("vlc_transcode", ""),
            "vlc_port": cfg.get("vlc_port"),
            "autostart": bool(cfg.get("autostart", False)),
            "last_lines": last_lines_snapshot,
            "circuit_breaker": {
                "consecutive_failures": retry_state["consecutive_failures"],
                "next_retry_at": retry_state["next_retry_at"],
                "blocked": blocked,
                "blocked_reason": blocked_reason,
            },
        }


class StreamManager:
    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.streams = {}
        self._lock = threading.RLock()
        self._error_history = deque(maxlen=5)
        self._vlc_ports_used = set()
        self._vlc_port_min = 18090
        self._vlc_port_max = 18099
        # Pids arrancados por este manager. Se registran en el mismo instante
        # del Popen para que el reaper no mate un proceso que acaba de
        # nacer y todavia no esta asignado a ningun stream.
        self._known_pids = set()

    def note_spawn(self, pid):
        """Registra un pid recien arrancado por este manager."""
        if pid:
            with self._lock:
                self._known_pids.add(pid)

    def reap_orphans(self, dry_run=False, processes=None):
        """Elimina los ffmpeg de ESTA plataforma que nadie esta controlando.

        Un huerfano es un ffmpeg que sigue publicando a una de nuestras
        Destination_URL (o escribiendo en un pipe de /tmp/astra_nv) pero cuyo
        pid no esta registrado en ningun stream. Publica en paralelo al
        stream nuevo, el SRS lo rechaza con StreamBusy (1028) y el canal no
        levanta.

        Solo se matan procesos que:
          - son ffmpeg, y
          - (escriben en un pipe del pipeline NVIDIA) o (publican a una
            Destination_URL nuestra como ultimo argumento), y
          - no son de ninguno de nuestros streams, y
          - NO son hijos de este proceso.

        Esa ultima condicion es la red de seguridad: un huerfano de una
        instancia anterior queda adoptado por init (ppid=1), mientras que
        los ffmpeg que este manager acaba de arrancar son hijos suyos. Asi
        el reaper nunca puede matar la emision en curso por un error de
        orden o de estado.

        Nunca toca los procesos de otros servicios del host (el grabador
        escribe a fichero, no a una Destination_URL nuestra).
        """
        tracked = set()
        destinations = set()
        with self._lock:
            for stream in self.streams.values():
                for proc in (stream.process, stream._stage_process):
                    if proc is not None:
                        tracked.add(proc.pid)
                url = (stream.config or {}).get("destination_url")
                if url:
                    destinations.add(url)
            tracked |= set(self._known_pids)
            self._known_pids = {p for p in self._known_pids if _pid_alive(p)}

        if not self.streams:
            # Sin streams registrados no sabemos que procesos son nuestros:
            # matar a ciegas llevaria por delante la emision entera.
            logger.warning("[reaper] no hay streams registrados: no se reapea nada")
            return []

        snapshot = (
            list(processes) if processes is not None else list(_iter_process_cmdlines())
        )
        by_pid = {pid: parts for pid, parts in snapshot}

        orphans = []
        for pid, parts in snapshot:
            if pid in tracked or pid == os.getpid():
                continue
            if not _looks_like_ffmpeg(parts):
                continue
            if _pid_ppid(pid) == os.getpid():
                # Es hijo NUESTRO. Solo es huerfano si no lo tiene NINGUN
                # stream: un stop() que se corta a medias pierde la referencia
                # y el ffmpeg se queda vivo publicando en paralelo (con el
                # socket del origen lleno y el de RTMP en CLOSE-WAIT).
                #
                # Antes este caso se saltaba SIEMPRE, y por eso un huerfano
                # sobrevivio 7 dias. La red de seguridad ahora es la EDAD:
                # un ffmpeg de menos de ORPHAN_MIN_AGE_SECONDS puede ser uno
                # que esta arrancando ahora mismo y todavia no esta
                # registrado en ningun stream.
                age = _pid_age_seconds(pid)
                if age < ORPHAN_MIN_AGE_SECONDS:
                    continue
                is_pipeline = any(_NV_PIPE_DIR in part for part in parts)
                is_publisher = parts[-1] in destinations
                if not (is_pipeline or is_publisher):
                    continue
                orphans.append(
                    (pid, parts[-1] if is_publisher else _NV_PIPE_DIR)
                )
                continue
            is_pipeline = any(_NV_PIPE_DIR in part for part in parts)
            is_publisher = parts[-1] in destinations
            if not (is_pipeline or is_publisher):
                continue
            orphans.append((pid, parts[-1] if is_publisher else _NV_PIPE_DIR))

        for pid, target in orphans:
            if dry_run:
                logger.warning("[reaper] huerfano detectado: pid %s -> %s", pid, target)
                continue
            # Re-verificar justo antes de matar: entre el escaneo y el kill
            # el pid pudo liberarse y reutilizarse.
            parts = by_pid.get(pid)
            if not parts or not _is_our_ffmpeg(parts, destinations):
                continue
            logger.warning(
                "[reaper] eliminando ffmpeg huerfano: pid %s (%s)", pid, target
            )
            # SIGKILL al proceso, NO al grupo: el ffmpeg puede compartir
            # grupo con la app y matar al grupo mataria la emision entera.
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if orphans and not dry_run:
            logger.warning("[reaper] %d proceso(s) huerfano(s) eliminado(s)", len(orphans))
        return orphans

    def kill_leaked(self):
        """Mata los ffmpeg que un stop() dejo vivos (pids conocidos)."""
        killed = []
        with self._lock:
            streams = list(self.streams.values())
        for stream in streams:
            if not stream._leaked_pids:
                continue
            for pid in list(stream._leaked_pids):
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            stream._leaked_pids.clear()
        if killed:
            logger.warning(
                "[reaper] %d proceso(s) filtrado(s) de stop() eliminados: %s",
                len(killed),
                killed,
            )
        return killed

    def cleanup_stale_pipes(self):
        """Borra los pipes (fifos) de /tmp/astra_nv que ya no usa nadie.

        Un stop() que se corta a medias deja el fifo en disco. No rompen
        nada, pero se acumulan (uno por reinicio fallido).
        """
        in_use = set()
        for _pid, parts in _iter_process_cmdlines():
            for part in parts:
                if part.startswith(_NV_PIPE_DIR + "/"):
                    in_use.add(os.path.basename(part))
        removed = 0
        try:
            entries = os.listdir(_NV_PIPE_DIR)
        except OSError:
            return 0
        for entry in entries:
            if entry in in_use:
                continue
            path = os.path.join(_NV_PIPE_DIR, entry)
            try:
                if os.path.islink(path):
                    continue
                os.unlink(path)
                removed += 1
            except OSError:
                pass
        if removed:
            logger.info("[reaper] %d pipe(s) obsoleto(s) eliminado(s)", removed)
        return removed

    @staticmethod
    def is_port_free(port):
        if not isinstance(port, int) or port < 0 or port > 65535:
            return False
        import socket as _socket
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def assign_vlc_port(self, exclude_name=None):
        """Devuelve el primer puerto libre del rango [18090..18099].

        exclude_name: nombre del stream al que NO se le debe asignar puerto
        (típicamente porque está reasignando y debe conservar el suyo).
        """
        used_by_others = {
            port for port, owner in self._ports_by_owner().items()
            if owner != exclude_name
        }
        for p in range(self._vlc_port_min, self._vlc_port_max + 1):
            if p in used_by_others:
                continue
            if self.is_port_free(p):
                return p
        return None

    def _ports_by_owner(self):
        """Devuelve dict {puerto: nombre_stream} de todos los VLC activos en streams."""
        out = {}
        with self._lock:
            for name, stream in self.streams.items():
                p = stream.config.get("vlc_port") if stream.config else None
                if p:
                    out[int(p)] = name
        return out

    def release_vlc_port(self, port):
        if not port:
            return
        with self._lock:
            self._vlc_ports_used.discard(int(port))

    def reserve_vlc_port(self, port, owner):
        with self._lock:
            self._vlc_ports_used.add(int(port))

    def get_vlc_status_all(self):
        """Snapshot del estado de VLC de todos los streams."""
        with self._lock:
            out = {}
            for name, stream in self.streams.items():
                cfg = stream.config or {}
                enabled = bool(cfg.get("vlc_transcode"))
                p = stream.process.poll() if stream.process else None
                running = p is None
                out[name] = {
                    "enabled": enabled,
                    "vlc_active": stream._vlc_process is not None
                    and stream._vlc_process.poll() is None,
                    "vlc_pid": stream._vlc_process.pid
                    if stream._vlc_process and stream._vlc_process.poll() is None
                    else None,
                    "vlc_port": stream._vlc_port,
                    "vlc_url": stream._vlc_url,
                    "ffmpeg_running": running,
                    "ffmpeg_pid": stream.process.pid if running else None,
                }
            return out

    def get_vlc_status(self, name):
        status = self.get_vlc_status_all()
        return status.get(name)

    def restart_vlc_only(self, name):
        """Reinicia VLC de un stream sin tocar FFmpeg ni otros streams."""
        with self._lock:
            stream = self.streams.get(name)
            if not stream:
                return False, "Stream no encontrado"
            if not stream.config.get("vlc_transcode"):
                return False, "VLC no habilitado en este stream"
        stream._stop_vlc()
        new_url = stream._start_vlc()
        if not new_url:
            return False, "VLC no pudo arrancar"
        return True, "VLC reiniciado"

    def register_stream(self, name, config):
        with self._lock:
            if name not in self.streams:
                self.streams[name] = StreamInstance(name, config)
                logger.info(f"Stream registrado: {name}")
            else:
                old_cfg = self.streams[name].config or {}
                old_port = old_cfg.get("vlc_port")
                new_port = config.get("vlc_port")
                if old_port and old_port != new_port:
                    self.release_vlc_port(old_port)
                self.streams[name].config = config
                logger.info(f"Config actualizada para stream: {name}")

    def kill_foreign_publishers(self, destination_url, keep_pids=()):
        """Mata los ffmpeg que publican a una Destination_URL nuestra y que NO
        son los de este stream.

        Un huerfano de un reinicio anterior sigue publicando a la misma URL:
        el SRS acepta al nuevo pero el viejo sigue conectado, y uno de los
        dos se queda con StreamBusy (1028) o se pisan entre si. Por eso,
        antes de arrancar un stream, se limpia la URL de destino.
        """
        if not destination_url:
            return []
        keep = {p for p in keep_pids if p}
        killed = []
        for pid, parts in _iter_process_cmdlines():
            if pid in keep or pid == os.getpid():
                continue
            if not _looks_like_ffmpeg(parts):
                continue
            if parts[-1] != destination_url:
                continue
            logger.warning(
                "Matando ffmpeg ajeno que publica a '%s' (pid %s): "
                "dejaria al stream nuevo con StreamBusy",
                destination_url,
                pid,
            )
            # SIGKILL al proceso, no al grupo (ver nota en reap_orphans).
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            killed.append(pid)
        return killed

    def start_stream(self, name):
        with self._lock:
            stream = self.streams.get(name)
        if stream is None:
            return False
        # Start manual/explicito: resetear circuit breaker para que
        # un operador pueda recuperar un stream bloqueado.
        stream.reset_failures()
        with stream._op_lock:
            stream_manager.kill_foreign_publishers(
                (stream.config or {}).get("destination_url"),
                keep_pids=(stream.process.pid if stream.process else None,),
            )
            return stream.start()

    def stop_stream(self, name):
        with self._lock:
            stream = self.streams.get(name)
        if stream is None:
            return False
        with stream._op_lock:
            stream.stop()
        return True

    def restart_stream(self, name, reason="manual"):
        # El lock del manager NO se mantiene durante stop()/start(): stop()
        # puede tardar hasta 15 s (SIGTERM + SIGKILL) y bloqueando aqui
        # /api/status se quedaba colgado hasta 15 s (el panel del cliente
        # parecia congelado).
        with self._lock:
            if name not in self.streams:
                return False
            stream = self.streams[name]

            # Si es un reintento automatico y el circuit breaker esta abierto,
            # NO reiniciar. Solo un arranque manual (reason="manual" o
            # reason="start") resetera el breaker.
            is_auto = reason not in ("manual", "start")
            if is_auto:
                blocked, why = stream.is_blocked()
                if blocked:
                    logger.warning(
                        "Stream '%s' bloqueado por circuit breaker (%s), "
                        "no se reintenta. Use start manual para resetear.",
                        name, why,
                    )
                    return False

                # Respetar ventana de backoff (_next_retry_at)
                retry_state = stream.get_retry_state()
                if retry_state["next_retry_at"] > time.time():
                    wait = retry_state["next_retry_at"] - time.time()
                    logger.debug(
                        "Stream '%s' en backoff, faltan %.1fs para reintento",
                        name, wait,
                    )
                    return False

            error_msg = stream.error_message or "Unknown error"
            try:
                last_cmd = " ".join(stream.build_ffmpeg_command())
            except Exception:
                last_cmd = ""

        # Un reinicio manual es intervencion del operador: no cuenta como fallo.
        if not is_auto:
            stream.reset_failures()

        # Reiniciar por falta de audio NO es un fallo del stream: el canal
        # estaba bien (video) pero mudo. Si se contabilizara como fallo, el
        # circuit breaker abriria y el stream pasaria a 'error', dejando el
        # canal caido del todo por estar sin sonido.
        is_audio_fix = reason == "no_audio"

        # El episodio en curso (proceso que vivía) termina aqui: si estaba
        # activo, este reinicio se debe a un fallo real y debe contabilizarse.
        episode_ended = stream.register_episode_end()
        if is_audio_fix:
            episode_ended = False

        with stream._op_lock:
            # stop() ya no deberia lanzar, pero si lo hiciera el reinicio
            # MUSTO llegar a start(): si no, el canal se queda caido para
            # siempre porque nadie vuelve a pedirle que arranque.
            try:
                stream.stop()
            except Exception:
                logger.exception(
                    "Error deteniendo '%s' antes del reinicio; se sigue al start()",
                    name,
                )
            stream.restart_count += 1
            stream.note_restart()
            with self._lock:
                self._error_history.append({
                    "timestamp": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    "stream": name,
                    "type": "restart",
                    "reason": reason,
                    "message": error_msg[:200],
                    "command": last_cmd
                })

            time.sleep(2)
            # Antes de arrancar, se limpia la URL de destino: si quedo un
            # huerfano publicando a ella, el SRS le daria StreamBusy (1028)
            # al publisher nuevo y el canal seguiria sin levantar.
            stream_manager.kill_foreign_publishers(
                (stream.config or {}).get("destination_url"),
            )
            result = stream.start()

        # Contabilidad de fallos: UN fallo por episodio fallido.
        #   - episode_ended  -> el proceso murio/congelo (fallo real)
        #   - not result     -> no se pudo arrancar (fallo real)
        # Antes esto solo se ejecutaba si start() devolvia False, y start()
        # devuelve True en cuanto Popen tiene exito: el breaker NUNCA se
        # cerraba y los streams morian en bucle infinito de reinicios.
        if is_auto and (episode_ended or not result):
            if episode_ended:
                fail_msg = error_msg
            else:
                fail_msg = stream.error_message or "Failed to start after restart"
            should_retry, fatal, backoff = stream.register_failure(fail_msg)
            if fatal:
                logger.error(
                    "Stream '%s' error fatal detectado (%s). Marcado 'error', "
                    "requiere start manual.",
                    name, fail_msg[:120],
                )
                with stream._status_lock:
                    stream._status = "error"
            elif not should_retry:
                logger.error(
                    "Stream '%s' alcanzo %d fallos consecutivos. Marcado 'error', "
                    "requiere start manual.",
                    name, _MAX_CONSECUTIVE_FAILURES,
                )
                with stream._status_lock:
                    stream._status = "error"
            else:
                logger.warning(
                    "Stream '%s' fallo #%d (%s), reintentando en %ds.",
                    name, stream._consecutive_failures,
                    ("transitorio" if _is_transient_error(fail_msg) else "sin clasificar"),
                    backoff,
                )

        if not result:
            with self._lock:
                self._error_history.append({
                    "timestamp": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    "stream": name,
                    "type": "error",
                    "reason": "start_failed",
                    "message": stream.error_message or "Failed to start after restart",
                    "command": last_cmd
                })
        return result

    def get_stream_status(self, name):
        if name in self.streams:
            return self.streams[name].get_info()
        return None

    def get_all_status(self):
        with self._lock:
            return {name: stream.get_info() for name, stream in self.streams.items()}

    def get_logs(self, name, lines=500):
        if name in self.streams:
            stream = self.streams[name]
            with stream._logs_lock:
                return list(stream.last_lines)[-lines:]
        return []

    def is_all_running(self):
        with self._lock:
            return all(
                s.is_running() for s in self.streams.values() if s.status != "stopped"
            )

    def get_running_count(self):
        with self._lock:
            return sum(
                1 for s in self.streams.values()
                if s.status == "running" and s.is_running()
            )

    def stop_all_and_cleanup(self):
        for stream in list(self.streams.values()):
            stream.cleanup()
        logger.info("Todos los streams detenidos y limpiados")
        gc.collect()
        logger.info("Recolector de basura ejecutado")

    def full_restart(self):
        self.stop_all_and_cleanup()
        time.sleep(3)
        for stream in list(self.streams.values()):
            # Reinicio programado: se resetea el breaker para que ningun
            # stream quede en 'error' hasta el siguiente ciclo.
            stream.reset_failures()
            stream.start()
        logger.info("Reinicio completo - todos los streams iniciados de nuevo")

    def get_error_history(self):
        with self._lock:
            return list(self._error_history)


stream_manager = StreamManager()
