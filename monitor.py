import json
import logging
import os
import subprocess
import sys
import threading
import time

from stream_manager import probe_audio_packets, stream_manager

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

# --- Salud de audio -------------------------------------------------------
# El monitor solo miraba el contador de frames de VIDEO. Un canal con video
# vivo y audio mudo pasaba por 'sano' para siempre y se quedaba mudo de
# forma indefinida: el unico remedio era que un humano reiniciara.
#
# Aqui se sondea la URL REALMENTE publicada y se cuentan los paquetes de
# audio que llegan. Si la pista existe pero no llega ni un paquete, el canal
# esta mudo y se corrige reiniciando el ffmpeg (que reconecta al origen y
# vuelve a leer el PMT: es lo que arregla el audio cuando el origen esta
# bien).
AUDIO_CHECK_INTERVAL = 60          # segundos entre sondeos
AUDIO_PROBE_SECONDS = 8            # segundos de lectura en cada sondeo
AUDIO_WARMUP_SECONDS = 240         # no se evalua hasta que el stream lleva estable
AUDIO_STRIKES_BEFORE_RESTART = 3   # sondeos seguidos sin audio antes de actuar
MAX_AUDIO_RESTARTS_PER_HOUR = 3    # tope de reinicios por audio y stream/hora
AUDIO_ALERT_COOLDOWN = 1800        # no repetir la misma alerta antes de esto

_NOTIFY_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tools", "astra_notify.py"
)


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
        self._last_audio_check_at = 0.0
        self._audio_alerted_at = {}
        self._audio_thread = None
        self._audio_running = False

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
        # El audio se comprueba aparte y en su propio hilo: el sondeo tarda
        # ~15 s y no debe retrasar la deteccion de caidas.
        self._schedule_audio_check(all_status)

    def _schedule_audio_check(self, all_status):
        """Lanza el sondeo de audio en segundo plano (uno a la vez)."""
        if self._audio_running:
            return
        now = time.time()
        if now - self._last_audio_check_at < AUDIO_CHECK_INTERVAL:
            return
        self._last_audio_check_at = now
        self._audio_running = True

        def _worker():
            try:
                self._check_audio_health(all_status)
            except Exception:
                logger.exception("Error en el hilo de chequeo de audio")
            finally:
                self._audio_running = False

        self._audio_thread = threading.Thread(target=_worker, daemon=True)
        self._audio_thread.start()

    # --- Deteccion y correccion automatica del audio ----------------------

    def _notify(self, name, message):
        """Avisa por Telegram (si esta configurado). Nunca rompe el monitor."""
        now = time.time()
        last = self._audio_alerted_at.get(name, 0)
        if now - last < AUDIO_ALERT_COOLDOWN:
            return
        self._audio_alerted_at[name] = now
        try:
            subprocess.Popen(
                [sys.executable, _NOTIFY_SCRIPT, f"[{name}] {message}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _check_audio_health(self, all_status):
        """Sondea el audio de cada stream en marcha."""
        now = time.time()
        for name, status in all_status.items():
            if status.get("status") != "running":
                continue
            stream = stream_manager.streams.get(name)
            if not stream or stream.start_time is None:
                continue
            if now - stream.start_time < AUDIO_WARMUP_SECONDS:
                continue
            try:
                self._check_one_audio(name, stream)
            except Exception:
                logger.exception("Error en el chequeo de audio de '%s'", name)

    def _check_one_audio(self, name, stream):
        """Un stream: si tiene audio publicandolo y, si no, que se hace."""
        dest = (stream.config or {}).get("destination_url")
        if not dest:
            return
        result = probe_audio_packets(dest, seconds=AUDIO_PROBE_SECONDS)
        state = stream.note_audio_probe(result)

        if state == "ok":
            if stream.audio_strikes:
                logger.info(
                    "Stream '%s': audio recuperado (%s paquetes en %ss)",
                    name, result.get("packets"), AUDIO_PROBE_SECONDS,
                )
                self._notify(name, "Audio recuperado (se reinicio el canal).")
            stream.clear_audio_strikes()
            return

        if state == "error":
            # No se pudo sondear: no se actua (puede ser un fallo de red).
            logger.debug(
                "Stream '%s': sondeo de audio fallido (%s)",
                name, result.get("detail"),
            )
            return

        # state == 'missing' (pista de audio declarada pero 0 paquetes) o
        # 'no_track' (la publicacion no declara pista de audio).
        source_state = stream.source_audio_state()
        if source_state in ("no_track", "missing"):
            # El audio no llega desde el origen: reiniciar el ffmpeg NO lo
            # arregla. Solo genera una tormenta de reinicios. Se avisa y se
            # deja el canal con video.
            logger.error(
                "Stream '%s' sin audio en el ORIGEN (%s): reiniciar el ffmpeg "
                "no lo arregla. Se deja el canal con video.",
                name, source_state,
            )
            self._notify(
                name,
                f"Sin audio en el origen ({source_state}). El canal sigue con "
                "video; revisar CAM/origen.",
            )
            stream.clear_audio_strikes()
            return
        if source_state != "ok":
            # No se pudo confirmar el origen: no se reinicia nada todavia.
            logger.debug(
                "Stream '%s': origen no verificado (%s), no se actua aun",
                name, source_state,
            )
            return

        strikes = stream.note_audio_strike()
        logger.warning(
            "Stream '%s' mudo: la pista de audio no llega a la publicacion "
            "(video %s paquetes, origen con audio OK). Sondeo %d/%d",
            name, result.get("video_packets"), strikes,
            AUDIO_STRIKES_BEFORE_RESTART,
        )
        if strikes < AUDIO_STRIKES_BEFORE_RESTART:
            return

        if stream.audio_restarts_last_hour() >= MAX_AUDIO_RESTARTS_PER_HOUR:
            logger.error(
                "Stream '%s' lleva %d reinicios por audio en la ultima hora: "
                "se para de reiniciar para no hacer una tormenta.",
                name, stream.audio_restarts_last_hour(),
            )
            self._notify(
                name,
                "Persiste la falta de audio tras varios reinicios. Requiere "
                "revision manual.",
            )
            stream.clear_audio_strikes()
            return

        if not self._may_restart(name, stream):
            return

        logger.error(
            "Stream '%s' mudo (video OK, 0 paquetes de audio): reiniciando "
            "el ffmpeg para reconectar con el origen...",
            name,
        )
        self._notify(
            name,
            "Canal sin audio (video OK). Reiniciando el ffmpeg para recuperar "
            "el sonido.",
        )
        stream.note_audio_restart()
        stream.clear_audio_strikes()
        try:
            stream_manager.restart_stream(name, reason="no_audio")
        except Exception:
            logger.exception("Error reiniciando '%s' por falta de audio", name)

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
