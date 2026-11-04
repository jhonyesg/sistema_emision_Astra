import gc
import logging
import os
import re
import shlex
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
        # Control de "episodio de emision": un episodio empieza cuando el
        # proceso arranca OK y termina cuando muere o se congela. Permite
        # contar UN fallo por episodio real (antes nunca se contaban y el
        # circuit breaker no se cerraba nunca).
        self._episode_active = False
        # Tope de reinicios por hora (anti-tormenta) y cooldown por stream.
        self._restart_times = deque(maxlen=60)

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
        if self._vlc_process:
            try:
                self._vlc_process.terminate()
                try:
                    self._vlc_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._vlc_process.kill()
                    self._vlc_process.wait(timeout=3)
            except (OSError, ValueError):
                pass
            finally:
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
                )
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
            )
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
        # ffmpeg imprime su progreso (frame=/size=/bitrate=) con '\r', no '\n'.
        # readline() solo corta por '\n', asi que en streams sin mensajes de
        # log intercalados (que si traen '\n') el hilo se queda bloqueado
        # esperando y el progreso nunca se parsea. Por eso leemos crudo y
        # partimos tanto por '\r' como por '\n'.
        buf = b""
        try:
            while True:
                chunk = self.process.stderr.read1(4096)
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
                with self._logs_lock:
                    self.last_lines.append(f"[{ts}] [nv] {text}")
                if "error" in text.lower() or "failed" in text.lower():
                    self.error_message = text[:200]
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
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"Stream '{self.name}' no terminó con SIGTERM, usando SIGKILL"
                    )
                    self.process.kill()
                    self.process.wait(timeout=5)
            finally:
                proc = self.process
                self.process = None
                for stream_attr in ("stderr", "stdout", "stdin"):
                    try:
                        s = getattr(proc, stream_attr, None)
                        if s:
                            s.close()
                    except (OSError, ValueError):
                        pass
        # Etapa NVIDIA: se detiene despues del encoder para que no quede
        # escribiendo en un pipe sin lector.
        if self._stage_process:
            proc = self._stage_process
            self._stage_process = None
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    if proc.stderr:
                        proc.stderr.close()
                except (OSError, ValueError):
                    pass
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

    def start_stream(self, name):
        with self._lock:
            stream = self.streams.get(name)
        if stream is None:
            return False
        # Start manual/explicito: resetear circuit breaker para que
        # un operador pueda recuperar un stream bloqueado.
        stream.reset_failures()
        with stream._op_lock:
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

        # El episodio en curso (proceso que vivía) termina aqui: si estaba
        # activo, este reinicio se debe a un fallo real y debe contabilizarse.
        episode_ended = stream.register_episode_end()

        with stream._op_lock:
            stream.stop()
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
