#!/usr/bin/env python3
"""Corrige las opciones de ffmpeg segun el protocolo de entrada/salida.

Regla (ffmpeg 6.1.1):
  - entrada http/https  -> admits -reconnect*, -timeout, -rw_timeout
  - entrada rtmp://      -> NO admite -reconnect* ('Option reconnect not found')
                           y -timeout provoca el modo 'listen' del protocolo
                           RTMP ('Address already in use')
  - entrada srt://       -> NO admite -reconnect* ni -timeout
  - salida rtmp://       -> NO admite -reconnect*

Deja siempre -rw_timeout (opcion generica del AVFormatContext) y las de
decodificacion (-fflags, -err_detect, -analyzeduration, -probesize).

Uso: fix_protocol_options.py <api_base> <ini_path>
"""
import json
import re
import sys
import urllib.request

RECONNECT_TOKENS = (
    "-reconnect",
    "-reconnect_streamed",
    "-reconnect_delay_max",
    "-reconnect_on_network_error",
    "-reconnect_on_http_error",
)

HTTP_LIKE = ("http://", "https://")


def split_options(text):
    return text.split() if text else []


def clean(options, protocol, is_output=False):
    """Elimina las opciones no soportadas por el protocolo (y su valor)."""
    out = []
    skip_next = False
    for opt in options:
        if skip_next:
            skip_next = False
            continue
        if opt == "-timeout" and not protocol.startswith(HTTP_LIKE):
            skip_next = True
            continue
        if opt in RECONNECT_TOKENS and not protocol.startswith(HTTP_LIKE):
            # estas opciones llevan un valor que tambien hay que quitar
            skip_next = True
            continue
        out.append(opt)
    return out


def main():
    api = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5006"
    ini = sys.argv[2] if len(sys.argv) > 2 else "db/112_a.ini"

    # Reutiliza el parser del proyecto para no duplicar logica
    sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
    from config_parser import load_config

    cfg = load_config(ini)
    streams = {s["name"]: s for s in cfg.get_all_streams()}

    changed = 0
    for name, s in streams.items():
        src = s.get("original_url", "") or ""
        dst = s.get("destination_url", "") or ""
        pre = s.get("ffmpeg_pre_options", "") or ""
        post = s.get("ffmpeg_post_options", "") or ""

        new_pre = " ".join(clean(split_options(pre), src))
        new_post = " ".join(clean(split_options(post), dst, is_output=True))

        if new_pre == pre.strip() and new_post == post.strip():
            continue

        payload = {}
        if new_pre != pre.strip():
            payload["FFMPEG_PRE_OPTIONS"] = new_pre
        if new_post != post.strip():
            payload["FFMPEG_POST_OPTIONS"] = new_post
        if not payload:
            continue

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{api}/api/stream/{name}/update",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.loads(r.read().decode())
            if res.get("success"):
                changed += 1
                print(f"  [CORREGIDO] {name}")
                if "FFMPEG_PRE_OPTIONS" in payload:
                    print(f"      PRE  -> {new_pre}")
                if "FFMPEG_POST_OPTIONS" in payload:
                    print(f"      POST -> {new_post}")
            else:
                print(f"  [FALLO] {name}: {res.get('error')}")
        except Exception as e:
            print(f"  [FALLO] {name}: {e}")

    print(f"\nStreams corregidos: {changed}")


if __name__ == "__main__":
    main()
