import json
import logging
import os
import threading
import time

from stream_manager import stream_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _instance_config_file():
    """config.json propio de la instancia (config.112_a.json)."""
    ini = os.environ.get("ASTRA_INI", "")
    stem = os.path.splitext(os.path.basename(ini))[0] if ini else ""
    if not stem:
        return None
    return os.path.join(os.path.dirname(CONFIG_FILE), f"config.{stem}.json")


def _read_auto_restart_enabled():
    """Lee auto_restart del config de la instancia (y del base)."""
    try:
        cfg = {}
        for path in (CONFIG_FILE, _instance_config_file()):
            if not path or not os.path.exists(path):
                continue
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        return cfg.get("auto_restart", True)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("No se pudo leer auto_restart de config: %s", e)
    return True

# Grace period desde el start del stream: durante este tiempo no se evalua
# el freeze. Reducido para detectar bloqueos rapidamente.
WARMUP_SECONDS = 30

# Cuantos checks consecutivos fallidos antes de reiniciar. Con check_interval=10s
# y 3 checks se reacciona en ~30s adicionales despues del warmup (total ~60s).
FREEZE_CHECKS_BEFORE_RESTART = 3

# Anti-tormenta de reinicios (P0): sin esto, 14 streams caidos generaban
# ~1,5 reinicios/segundo y el host se saturaba de procesos ffmpeg.
MIN_RESTART_INTERVAL_GLOBAL = 3    # segundos minimos entre reinicios automaticos
MAX_RESTARTS_PER_HOUR = 12         # tope de reinicios por stream y hora
COOLDOWN_AFTER_MAX_RESTARTS = 600  # tras superar el tope, 10 min de pausa
RECOVERY_INTERVAL = 600            # reintento de streams bloqueados por el breaker

# Barrido de procesos huerfanos: ffmpeg que siguen publicando a una de
# nuestras URLs sin que nadie los controle. El SRS les da StreamBusy (1028)
# al publisher nuevo y el canal no levanta.
REAP_INTERVAL = 120                # segundos entre barridos


class StreamMonitor:
    def __init__(self, check_interval=10, freeze_threshold=60):
        self.check_interval = check_interval
        self.freeze_threshold = freeze_threshold
        self._running = False
        self._thread = None
        self._frozen_streams = {}
        self._frozen_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._last_restart_at = 0.0
        self._last_reap_at = 0.0

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

    def _reap_orphans(self):
        """Elimina ffmpeg que publican a nuestras URLs sin control.

        Sin esto, un reinicio que se quedo a medias deja el ffmpeg viejo
        publicando en paralelo: el SRS rechaza al nuevo con StreamBusy
        (1028) y el canal se queda caido sin ninguna explicacion.
        """
        try:
            stream_manager.reap_orphans()
            stream_manager.kill_leaked()
            stream_manager.cleanup_stale_pipes()
        except Exception:
            logger.exception("Error en el barrido de procesos huerfanos")

    def _check_all_streams(self):
        if not _read_auto_restart_enabled():
            return
        # Barrido periodico de huerfanos (no depende del estado de los
        # streams: un huerfano puede existir aunque todos los streams
        # 'parezcan' bien).
        now = time.time()
        if now - self._last_reap_at >= REAP_INTERVAL:
            self._last_reap_at = now
            self._reap_orphans()
        all_status = stream_manager.get_all_status()
        for name, status in all_status.items():
            self._check_freeze(name, status)
        # Segunda pasada con estado FRESCO: si no, el snapshot viejo hacia que
        # un stream recien reiniciado volviera a reiniciarse en el mismo ciclo.
        all_status = stream_manager.get_all_status()
        for name, status in all_status.items():
            self._check_process_health(name, status)
            self._maybe_reset_breaker(name, status)

    def _may_restart(self, name, stream):
        """Controla el ritmo de reinicios (anti-tormenta)."""
        now = time.time()
        with self._restart_lock:
            if now - self._last_restart_at < MIN_RESTART_INTERVAL_GLOBAL:
                return False
            self._last_restart_at = now
        if stream.restarts_last_hour() >= MAX_RESTARTS_PER_HOUR:
            retry_state = stream.get_retry_state()
            last = retry_state.get("last_failure_at") or 0
            if now - last < COOLDOWN_AFTER_MAX_RESTARTS:
                logger.warning(
                    "Stream '%s' supero %d reinicios/hora: en pausa hasta %ds.",
                    name, MAX_RESTARTS_PER_HOUR, int(COOLDOWN_AFTER_MAX_RESTARTS),
                )
                return False
        return True

    def _maybe_reset_breaker(self, name, status):
        """Si el stream lleva un rato estable, resetea el circuit breaker."""
        if status.get("status") != "running":
            return
        stream = stream_manager.streams.get(name)
        if not stream:
            return
        if stream.maybe_reset_on_stable():
            logger.info(
                "Stream '%s' estable tras varios reinicios, circuit breaker reseteado.",
                name,
            )

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
            if not self._may_restart(name, stream):
                return
            logger.error("Stream '%s' congelado demasiado tiempo, reiniciando...", name)
            stream_manager.restart_stream(name, reason="frozen")

    def _check_process_health(self, name, status):
        stream = stream_manager.streams.get(name)
        if not stream:
            return

        # Recuperacion de streams bloqueados por el circuit breaker: antes
        # quedaban en 'error' para siempre y solo un humano los resucitaba.
        if status.get("status") == "error":
            self._try_recover_blocked(name, stream)
            return

        if status.get("status") == "running" and not stream.is_running():
            # Respetar circuit breaker: si ya esta bloqueado (error fatal o
            # max_failures), no reintentar sin esperar la ventana de
            # recuperacion.
            blocked, why = stream.is_blocked()
            if blocked:
                logger.debug(
                    "Stream '%s' en estado bloqueado (%s), monitor no reintenta.",
                    name, why,
                )
                self._try_recover_blocked(name, stream)
                return

            error_msg = stream.get_last_error_from_logs()
            logger.error(
                "Stream '%s' proceso murio inesperadamente: %s", name, error_msg
            )
            if not self._may_restart(name, stream):
                return
            # NO cambiamos status a 'error' aqui. restart_stream decide:
            #   - si permite retry: start() deja status='running' o 'error'
            #   - si backoff/breaker: status queda como esta para que el
            #     monitor siga intentando en el siguiente ciclo (solo el
            #     breaker abierto cambia status a 'error' definitivo)
            stream_manager.restart_stream(name, reason="process_died")

    def _try_recover_blocked(self, name, stream):
        """Reintenta un stream bloqueado cada RECOVERY_INTERVAL segundos."""
        retry_state = stream.get_retry_state()
        last = retry_state.get("last_failure_at") or 0
        elapsed = time.time() - last
        if elapsed < RECOVERY_INTERVAL:
            return False
        if not self._may_restart(name, stream):
            return False
        logger.warning(
            "Stream '%s' bloqueado (%s) hace %ds: reintentando automaticamente.",
            name, retry_state.get("blocked_reason") or "max_failures", int(elapsed),
        )
        stream.reset_failures()
        return stream_manager.restart_stream(name, reason="recovery")


# freeze_threshold=20s + 3 checks (a 10s) ~= 30s extra tras warmup,
# total ~60s antes de actuar. Suficiente para arrancar y reaccionar rapido
# ante bloqueos.
# check_interval=10: suficientemente holgado para no entrar en feedback-loop
# de reconexiones sincronizadas contra el destino RTMP.
monitor = StreamMonitor(check_interval=10, freeze_threshold=20)
