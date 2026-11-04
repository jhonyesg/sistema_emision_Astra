#!/usr/bin/env python3
"""Envia una alerta a Telegram usando el bot de astrabot.

Uso: astra_notify.py "mensaje"

Lee el token y los chats permitidos de /etc/astrabot.env. Si algo falla,
no rompe el script que lo invoca (el watchdog): solo deja rastro en el log.
"""
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime

ENV_FILE = "/etc/astrabot.env"
LOG = "/var/log/astra-watchdog.log"


def read_env():
    env = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def main():
    if len(sys.argv) < 2:
        return 0
    message = sys.argv[1]
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chats = [c.strip() for c in env.get("ASTRA_ALLOWED_CHATS", "").split(",") if c.strip()]
    if not token or not chats:
        return 0

    text = f"[ASTRA {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n{message}"
    for chat in chats:
        payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            req = urllib.request.Request(url, data=payload)
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            try:
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(f"[notify] no se pudo enviar a Telegram: {e}\n")
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
