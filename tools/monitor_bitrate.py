#!/usr/bin/env python3
"""Monitor en vivo de Bps/bitrate de un stream (y de la red global).

Uso:
    python3 tools/monitor_<stream>.py [--interval 2] [--stream NOMBRE]

Muestra cada N segundos: bitrate por stream, Kbps de red de subida/bajada,
y Bytes en cola TX/RX de la interfaz configurada en el INI activo.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from config_parser import load_config  # noqa: E402

DEFAULT_FILTER = ("<stream>",)


def http_get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def get_network_interface():
    cfg_path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        active = cfg.get("active_config", "")
        if active:
            ini_path = os.path.join(BASE_DIR, active)
            sections = load_config(ini_path).config.sections()
            if "servidor" in sections:
                net = load_config(ini_path).config.get("servidor", "Network",
                                                       fallback="enp2s0")
                return net
    except Exception:
        pass
    return "enp2s0"


def fmt_bps(bps):
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.2f} Mbps"
    if bps >= 1_000:
        return f"{bps/1_000:.1f} kbps"
    return f"{bps:.0f} bps"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--api", default="http://localhost:5007")
    ap.add_argument("--srs-api", default="http://localhost:1985")
    ap.add_argument("--stream", action="append", default=None,
                    help="Filtrar por nombre (substring, repetible). Default: <stream>")
    args = ap.parse_args()

    needles = args.stream if args.stream else list(DEFAULT_FILTER)
    iface = get_network_interface()
    print(f"# Interface: {iface}  |  refresh: {args.interval}s  |  filter: {needles}")
    print()

    last_tx = None
    last_rx = None
    last_t = None

    while True:
        try:
            d = http_get(f"{args.api}/api/status")
            sys_info = d.get("system", {}) or {}
            net_iface = sys_info.get("net_interface") or iface
            up_mbps = sys_info.get("upload_mbps", 0) or 0
            down_mbps = sys_info.get("download_mbps", 0) or 0
            cpu = sys_info.get("cpu_percent", 0)
            mem = sys_info.get("mem_percent", 0)
            now = time.strftime("%H:%M:%S")

            print(f"\033[2J\033[H=== {now}  CPU {cpu:.0f}%  RAM {mem:.0f}%  "
                  f"↑ {fmt_bps(up_mbps*1_000_000)}  ↓ {fmt_bps(down_mbps*1_000_000)}"
                  f"  iface={net_iface} ===\n")

            streams = d.get("streams", {})
            matched = []
            for name, st in streams.items():
                if any(n.lower() in name.lower() for n in needles):
                    matched.append((name, st))

            if not matched:
                print(f"  (sin streams que coincidan con {needles})")
            else:
                print(f"  {'STREAM':<28} {'STATUS':<8} {'BITRATE':>12} {'UP/s':>10} "
                      f"{'SIZE':>10} {'UPTIME':>8} {'RST':>4}")
                print(f"  {'-'*28} {'-'*8} {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*4}")
                for name, st in matched:
                    br = st.get("bitrate", "")
                    pid = st.get("pid") or "-"
                    size = st.get("file_size", "")
                    up = st.get("uptime", 0)
                    rst = st.get("restart_count", 0)
                    stat = st.get("status", "?")
                    # convertir bitrate (ej "2304.7 KBITS/S" o "1.2 MBITS/S") a bps
                    bps = parse_bitrate(br)
                    print(f"  {name:<28} {stat:<8} {br:>12} "
                          f"{fmt_bps(bps):>10} {size:>10} "
                          f"{fmt_dur(up):>8} {rst:>4}")
                    err = st.get("error_message", "")
                    if err:
                        print(f"      ⚠ {err[:100]}")

            # Detalle SRS si hay streams <stream>
            try:
                srs = http_get(f"{args.srs_api}/api/v1/streams/")
                srs_streams = [
                    s for s in srs.get("streams", [])
                    if any(n.lower() in s.get("name","").lower() for n in needles)
                ]
                if srs_streams:
                    print()
                    print(f"  SRS ({len(srs_streams)} stream(s) en servidor local):")
                    for s in srs_streams:
                        print(f"    • {s['name']:<22} "
                              f"recv={s.get('kbps',{}).get('recv_30s',0)} kbps  "
                              f"send={s.get('kbps',{}).get('send_30s',0)} kbps  "
                              f"clients={s.get('clients',0)}  "
                              f"pub={'Y' if s.get('publish',{}).get('active') else 'N'}")
            except Exception as e:
                pass

        except KeyboardInterrupt:
            print("\nbye.")
            return
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)

        time.sleep(args.interval)


def parse_bitrate(s):
    if not s:
        return 0
    s = s.upper().replace("/S", "").strip()
    try:
        if "MBIT" in s:
            return float(s.split()[0]) * 1_000_000
        if "KBIT" in s:
            return float(s.split()[0]) * 1_000
        if "BIT" in s:
            return float(s.split()[0])
    except Exception:
        return 0
    return 0


def fmt_dur(secs):
    secs = int(secs or 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


if __name__ == "__main__":
    main()
