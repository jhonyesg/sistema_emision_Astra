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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_DEFAULT_TITLE = "Sistema de Emisión 24/7-Astra"


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
        self._logs_lock = threading.RLock()
        self._status_lock = threading.RLock()
        self.last_lines = deque(maxlen=1000)
        self._output_thread = None

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
            "--rtsp-tcp",
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
            cmd = self.build_ffmpeg_command(vlc_url)
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
            self._output_thread = threading.Thread(
                target=self._read_output, daemon=True
            )
            self._output_thread.start()

            return True
        except (OSError, ValueError) as e:
            self._status = "error"
            self.error_message = str(e)
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
        return self.process.poll() is None

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
            "ffmpeg_pre_options": cfg.get("ffmpeg_pre_options", ""),
            "ffmpeg_post_options": cfg.get("ffmpeg_post_options", ""),
            "vlc_transcode": cfg.get("vlc_transcode", ""),
            "vlc_port": cfg.get("vlc_port"),
            "autostart": bool(cfg.get("autostart", False)),
            "last_lines": last_lines_snapshot,
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
            if name in self.streams:
                return self.streams[name].start()
        return False

    def stop_stream(self, name):
        with self._lock:
            if name in self.streams:
                self.streams[name].stop()
                return True
        return False

    def restart_stream(self, name, reason="manual"):
        with self._lock:
            if name not in self.streams:
                return False
            stream = self.streams[name]
            error_msg = stream.error_message or "Unknown error"
            last_cmd = " ".join(stream.build_ffmpeg_command())
            stream.stop()
            stream.restart_count += 1
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
            return sum(1 for s in self.streams.values() if s.status == "running")

    def stop_all_and_cleanup(self):
        with self._lock:
            for stream in self.streams.values():
                stream.cleanup()
            logger.info("Todos los streams detenidos y limpiados")
        gc.collect()
        logger.info("Recolector de basura ejecutado")

    def full_restart(self):
        self.stop_all_and_cleanup()
        time.sleep(3)
        with self._lock:
            for stream in self.streams.values():
                stream.start()
        logger.info("Reinicio completo - todos los streams iniciados de nuevo")

    def get_error_history(self):
        with self._lock:
            return list(self._error_history)


stream_manager = StreamManager()
