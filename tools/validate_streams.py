#!/usr/bin/env python3
"""Valida el comando ffmpeg de cada stream y clasifica los errores.

Detecta:
  - opciones no soportadas por el protocolo ("Option not found")
  - el bug de ffmpeg 6.x: con '-timeout' una entrada rtmp:///srt:// cae en
    modo "listen" y falla con "Address already in use"
  - errores de conexion normales (no son bugs de configuracion)

Uso: validate_streams.py <ini_path> [api_base]
"""
import re
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_parser import load_config  # noqa: E402
from stream_manager import StreamInstance  # noqa: E402

BAD_OPTIONS = re.compile(r"Option (\w+) not found|Unrecognized option|Invalid argument")
LISTEN_BUG = re.compile(
    r"Address already in use|Cannot assign requested address|\?listen&"
)


def parse_ini(path):
    out = []
    cur = None
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = (s[1:-1].strip(), {})
            out.append(cur)
        elif cur and "=" in s and not s.startswith((";", "#")):
            k, v = s.split("=", 1)
            cur[1][k.strip()] = v.strip()
    return out


def main():
    ini = sys.argv[1] if len(sys.argv) > 1 else "db/112_a.ini"
    cfg = load_config(ini)
    streams = {s["name"]: s for s in cfg.get_all_streams()}

    problemas = 0
    from concurrent.futures import ThreadPoolExecutor

    def check(item):
        name, kv = item
        if name not in streams:
            return None
        si = StreamInstance(name, streams[name])
        try:
            cmd = si.build_ffmpeg_command()
        except Exception as e:
            return (f"[BUILD-ERR] {name}: {e}", True)
        if not cmd:
            return None
        test = list(cmd)[:-1]
        try:
            proc = subprocess.run(
                test + ["-t", "4", "-f", "null", "-"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=12,
            )
        except subprocess.TimeoutExpired:
            return (f"[TIMEOUT] {name}: no termino en 12s", True)
        err = proc.stderr.decode("utf-8", "replace")
        bad_opt = BAD_OPTIONS.search(err)
        listen = LISTEN_BUG.search(err)
        if bad_opt:
            return (f"[OPCION ] {name}: {bad_opt.group(0)}\n           cmd: {' '.join(test)[:150]}", True)
        if listen:
            return (f"[LISTEN ] {name}: bug ffmpeg '-timeout' en entrada rtmp/srt\n           cmd: {' '.join(test)[:150]}", True)
        return (f"[OK     ] {name}", False)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(check, parse_ini(ini)):
            if res is None:
                continue
            print(res[0])
            if res[1]:
                problemas += 1

    print(f"\nStreams con problema de opciones: {problemas}")


if __name__ == "__main__":
    main()
