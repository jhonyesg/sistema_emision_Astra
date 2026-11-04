#!/usr/bin/env python3
"""Aplica el paquete de robustez de ffmpeg a los relays que van al CDN.

Se ejecuta contra la API del panel (POST /api/stream/<n>/update), que
reescribe el INI preservando comentarios y reinicia el stream con la nueva
configuracion (y resetea el circuit breaker).

Uso: harden_relays.py <api_base> <ini_path>
"""
import json
import re
import sys
import urllib.request

RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30"
CONNECT_TIMEOUT = "-timeout 15000000"
RW_TIMEOUT = "-rw_timeout 15000000"

# Destinos externos: cualquier relay que sale de esta red.
EXTERNAL = re.compile(r"col\.mediaserver\.com\.co|51\.81\.203\.154")


def parse_ini(path):
    """Devuelve [(seccion, {clave: valor})] preservando el orden."""
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
    api = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5006"
    ini = sys.argv[2] if len(sys.argv) > 2 else "db/112_a.ini"

    changed = 0
    for name, kv in parse_ini(ini):
        dest = kv.get("Destination_URL", "")
        if not EXTERNAL.search(dest):
            continue

        pre = kv.get("FFMPEG_PRE_OPTIONS", "")
        post = kv.get("FFMPEG_POST_OPTIONS", "")
        both = f"{pre} {post}"

        add = []
        if "-reconnect" not in both:
            add.append(RECONNECT)
        if not re.search(r"(?<![-\w])-timeout\s+\d", both):
            add.append(CONNECT_TIMEOUT)
        if "-rw_timeout" not in both:
            add.append(RW_TIMEOUT)

        if not add:
            print(f"  [OK]    {name}: ya tiene el paquete completo")
            continue

        new_pre = (pre + " " + " ".join(add)).strip() if pre else " ".join(add)
        payload = json.dumps({"FFMPEG_PRE_OPTIONS": new_pre}).encode()
        req = urllib.request.Request(
            f"{api}/api/stream/{name}/update",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode())
        except Exception as e:
            print(f"  [FALLO] {name}: {e}")
            continue

        if res.get("success"):
            changed += 1
            print(f"  [LISTO] {name}: +{' '.join(add)}")
        else:
            print(f"  [FALLO] {name}: {res.get('error')}")

    print(f"\nTotal streams actualizados: {changed}")


if __name__ == "__main__":
    main()
