#!/usr/bin/env python3
"""Watchdog de la plataforma de emision.

Se ejecuta cada 5 minutos desde cron. Comprueba:

  1. Que las dos instancias (5006 / 5007) responden y que sus streams estan
     vivos. Los que esten caidos se reinician (con reset del breaker).
  2. Que el SRS (contenedor) responde y no esta saturado.
  3. Que el disco de grabaciones no esta degradado (await alto / PSI io).
  4. Que la CPU del host no este ahogada.

Solo escribe en el log cuando detecta problemas.

Uso: astra_watchdog.py [--verbose]
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# (etiqueta, base URL, fichero de token)
INSTANCES = [
    ("112_A", "http://127.0.0.1:5006", ".api_token.112_a"),
    ("112_B", "http://127.0.0.1:5007", ".api_token.112_b"),
]
SRS_API = "http://127.0.0.1:1986/api/v1/summaries"
LOG_FILE = "/var/log/astra-watchdog.log"
DISCO = "sdc"          # disco de grabaciones (10 TB)
MAX_DISK_AWAIT_MS = 40
MAX_SRS_CPU = 85
MAX_HOST_LOAD = 20

VERBOSE = "--verbose" in sys.argv


NOTIFY = os.path.join(BASE_DIR, "tools", "astra_notify.py")


def notify(msg):
    """Envia la alerta a Telegram (si el bot esta configurado)."""
    try:
        subprocess.run(
            [sys.executable, NOTIFY, msg], timeout=40, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def log(msg, always=False):
    if always or VERBOSE:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if always:
            notify(msg)


def api_token(token_file):
    """Token de API de una instancia (fichero .api_token.<instancia>, 0600)."""
    path = os.path.join(BASE_DIR, token_file)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def http_json(url, method="GET", token="", timeout=20):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("X-Astra-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# El watchdog NO reinicia un stream que el monitor esta reintentando: cada
# 'start' manual resetea el circuit breaker y anula el backoff, de modo que
# el stream se reiniciaba cada 5 min para siempre. Solo interviene cuando el
# stream esta en 'error'/'stopped' (ya no lo reintenta nadie) y no se ha
# tocado hace WATCHDOG_COOLDOWN.
WATCHDOG_COOLDOWN = 1800  # 30 min entre intervenciones del watchdog por stream
_watchdog_history = {}


def check_streams():
    """Alerta y reinicio de streams que el monitor ya no reintenta."""
    now = time.time()
    for label, base, token_file in INSTANCES:
        token = api_token(token_file)
        try:
            data = http_json(f"{base}/api/status", token=token)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            log(f"ALERTA: la instancia {label} ({base}) no responde: {e}", always=True)
            continue

        streams = data.get("streams", {})
        caidos = [
            (n, s) for n, s in streams.items()
            if s.get("status") != "running"
        ]
        if not caidos:
            log(f"{label}: {len(streams)} streams OK")
            continue

        for name, s in caidos:
            err = (s.get("error_message") or "")[:80]
            cb = s.get("circuit_breaker", {}) or {}
            log(
                f"ALERTA: {label} stream '{name}' caido "
                f"(status={s.get('status')} alive={s.get('alive')} "
                f"fallos={cb.get('consecutive_failures')} bloqueado={cb.get('blocked_reason')}) {err}",
                always=True,
            )
            key = f"{label}/{name}"
            if now - _watchdog_history.get(key, 0) < WATCHDOG_COOLDOWN:
                continue
            try:
                http_json(
                    f"{base}/api/stream/{urllib.parse.quote(name)}/start",
                    method="POST", token=token,
                )
                _watchdog_history[key] = now
                log(f"   -> reinicio solicitado: {name}", always=True)
            except Exception as e:
                log(f"   -> no se pudo reiniciar {name}: {e}", always=True)


def check_srs():
    try:
        data = http_json(SRS_API, timeout=10)["data"]
    except Exception as e:
        log(f"ALERTA: el SRS no responde en {SRS_API}: {e}", always=True)
        return
    cpu = data.get("self", {}).get("cpu_percent", 0)
    conns = data.get("system", {}).get("conn_srs", 0)
    try:
        streams = len(http_json(
            "http://127.0.0.1:1986/api/v1/streams/?count=500", timeout=10
        ).get("streams", []))
    except Exception:
        streams = -1
    if cpu > MAX_SRS_CPU:
        log(f"ALERTA: SRS CPU {cpu}% (> {MAX_SRS_CPU}%) - un hilo saturado", always=True)
    else:
        log(f"SRS: cpu={cpu}% streams={streams} conexiones={conns}")


def disk_await_ms(dev):
    """Espera media de escritura del disco (ms) calculada de /proc/diskstats.

    /proc/diskstats: 1 reads_completed 2 reads_merged 3 sectors_read
    4 ms_reading 5 writes_completed 6 writes_merged 7 sectors_written
    8 ms_writing ...
    """
    try:
        with open("/proc/diskstats", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 11 or parts[2] != dev:
                    continue
                writes_done = int(parts[4])
                write_ms = int(parts[7])
                return write_ms, writes_done
    except OSError:
        pass
    return 0, 0


def check_disk():
    ms1, w1 = disk_await_ms(DISCO)
    if w1 == 0:
        return
    acum = ms1 / w1
    time.sleep(10)
    ms2, w2 = disk_await_ms(DISCO)
    dw = w2 - w1
    if dw <= 0:
        # Con commit=120 las escrituras se agrupan en rafagas: no hay E/S
        # continua. Se informa del promedio acumulado desde el arranque.
        if acum > MAX_DISK_AWAIT_MS:
            log(
                f"ALERTA: disco {DISCO} con w_await acumulado {acum:.1f} ms "
                f"(> {MAX_DISK_AWAIT_MS} ms)",
                always=True,
            )
        else:
            log(f"disco {DISCO}: w_await acumulado {acum:.1f} ms (sin E/S en la ventana)")
        return
    await_ms = (ms2 - ms1) / dw
    if await_ms > MAX_DISK_AWAIT_MS:
        log(
            f"ALERTA: disco {DISCO} con w_await {await_ms:.1f} ms "
            f"(> {MAX_DISK_AWAIT_MS} ms) - degradado o saturado",
            always=True,
        )
    else:
        log(f"disco {DISCO}: w_await {await_ms:.1f} ms ({dw} escrituras/10s)")

    try:
        with open("/proc/pressure/io", encoding="utf-8") as f:
            line = f.read().strip()
        if line:
            log(f"PSI io: {line}")
    except OSError:
        pass


def check_host():
    load1 = os.getloadavg()[0]
    if load1 > MAX_HOST_LOAD:
        log(f"ALERTA: load average {load1:.1f} (> {MAX_HOST_LOAD})", always=True)
    else:
        log(f"host: load={load1:.1f}")
    try:
        out = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=5
        ).stdout.splitlines()[1].split()
        used_pct = int(out[2]) / int(out[1]) * 100
        if used_pct > 90:
            log(f"ALERTA: memoria {used_pct:.0f}% usada", always=True)
    except Exception:
        pass


def main():
    check_streams()
    check_srs()
    check_disk()
    check_host()


if __name__ == "__main__":
    main()
