import json
import logging
import os
import threading
import time

from stream_manager import stream_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Grace period desde el start del stream: durante este tiempo no se evalua
# el freeze, porque los streams IPTV/HLS pueden tardar en producir el primer
# 'size=' o 'frame=' significativo.
WARMUP_SECONDS = 90

# Cuantos checks consecutivos fallidos antes de reiniciar (a check_interval=10s
# 6 checks ~= 60s adicionales encima del grace period).
FREEZE_CHECKS_BEFORE_RESTART = 6


def _read_auto_restart_enabled():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                if isinstance(cfg, dict):
                    return cfg.get("auto_restart", True)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("No se pudo leer auto_restart de config: %s", e)
    return True


class StreamMonitor:
    def __init__(self, check_interval=10, freeze_threshold=60):
        self.check_interval = check_interval
        self.freeze_threshold = freeze_threshold
        self._running = False
        self._thread = None
        self._frozen_streams = {}
        self._frozen_lock = threading.Lock()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(
            "Stream monitor started (check_interval=%ss, freeze=%ss, warmup=%ss)",
            self.check_interval,
            self.freeze_threshold,
            WARMUP_SECONDS,
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Stream monitor stopped")

    def _monitor_loop(self):
        while self._running:
            try:
                self._check_all_streams()
            except Exception:
                logger.exception("Error en el loop del monitor")
            time.sleep(self.check_interval)

    def _check_all_streams(self):
        if not _read_auto_restart_enabled():
            return
        all_status = stream_manager.get_all_status()
        for name, status in all_status.items():
            self._check_freeze(name, status)
            self._check_process_health(name, status)

    def _check_freeze(self, name, status):
        if status.get("status") != "running":
            return

        stream = stream_manager.streams.get(name)
        if not stream:
            return

        # Grace period: respetar el warm-up del stream
        if stream.start_time is None:
            return
        if time.time() - stream.start_time < WARMUP_SECONDS:
            with self._frozen_lock:
                self._frozen_streams.pop(name, None)
            return

        # Una vez que el stream haya producido contenido, congelado = sin
        # actividad reciente.
        with stream._logs_lock:
            last_frame_time = stream._last_frame_time
        if not last_frame_time:
            return

        time_since_last_frame = time.time() - last_frame_time
        if time_since_last_frame <= self.freeze_threshold:
            with self._frozen_lock:
                self._frozen_streams.pop(name, None)
            return

        with self._frozen_lock:
            count = self._frozen_streams.get(name, 0) + 1
            self._frozen_streams[name] = count

        logger.warning(
            "Stream '%s' parece congelado (sin frames por %ds, contador %d/%d)",
            name,
            int(time_since_last_frame),
            count,
            FREEZE_CHECKS_BEFORE_RESTART,
        )

        if count >= FREEZE_CHECKS_BEFORE_RESTART:
            with self._frozen_lock:
                self._frozen_streams[name] = 0
            logger.error("Stream '%s' congelado demasiado tiempo, reiniciando...", name)
            stream_manager.restart_stream(name, reason="frozen")

    def _check_process_health(self, name, status):
        stream = stream_manager.streams.get(name)
        if not stream:
            return

        if status.get("status") == "running" and not stream.is_running():
            error_msg = stream.get_last_error_from_logs()
            logger.error(
                "Stream '%s' proceso murio inesperadamente: %s", name, error_msg
            )
            with stream._status_lock:
                stream._status = "error"
                stream.error_message = error_msg
            stream_manager.restart_stream(name, reason="process_died")


# freeze_threshold=60s + 6 checks = 6 minutos totales maximos antes de actuar
# (mas generoso que antes para streams con arranque lento)
monitor = StreamMonitor(check_interval=10, freeze_threshold=60)
