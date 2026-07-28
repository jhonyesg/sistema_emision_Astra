import configparser
import os


class ConfigParser:
    def __init__(self, ini_path=None, content=None):
        if content is not None:
            self.ini_path = None
            self.config = configparser.ConfigParser()
            self.config.read_string(content)
        else:
            self.ini_path = ini_path
            self.config = configparser.ConfigParser()
            self.config.read(ini_path, encoding="utf-8")

    def get_all_streams(self):
        streams = []
        for section in self.config.sections():
            stream = {
                "name": section,
                "original_url": self._get_value(section, "Original_URL")
                or self._get_value(section, "original_url"),
                "destination_url": self._get_value(section, "Destination_URL")
                or self._get_value(section, "destination_url"),
                "ffmpeg_pre_options": self._get_value(section, "FFMPEG_PRE_OPTIONS")
                or self._get_value(section, "ffmpeg_pre_options")
                or "",
                "ffmpeg_post_options": self._get_value(section, "FFMPEG_POST_OPTIONS")
                or self._get_value(section, "ffmpeg_post_options")
                or "",
                "ffmpeg_path": self._get_value(section, "FFMPEG_PATH")
                or self._get_value(section, "ffmpeg_path")
                or "ffmpeg",
                "vaapi_driver": self._get_value(section, "VAAPI_DRIVER")
                or self._get_value(section, "vaapi_driver")
                or "",
                "autostart": self._get_value(section, "autostart", "false").lower()
                == "true",
            }
            if stream["original_url"] and stream["destination_url"]:
                streams.append(stream)
        return streams

    def _get_value(self, section, key, default=""):
        try:
            value = self.config.get(section, key)
            return value.strip() if value else default
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def get_stream_by_name(self, name):
        for stream in self.get_all_streams():
            if stream["name"] == name:
                return stream
        return None


def load_config(ini_path):
    return ConfigParser(ini_path=ini_path)


def load_config_from_content(content):
    return ConfigParser(content=content)


def load_deployment_overrides(ini_path=None, content=None):
    """Lee claves opcionales del bloque [servidor] para sobreescribir defaults.

    Claves reconocidas:
      Title              → nombre de plataforma (sobreescribe config.json)
      Network            → interfaz de red para stats de red (enp2s0, eth0, ...)
      Night_Restart      → bool, reinicio a medianoche (00:00–00:05)
      Auto_Restart       → bool, monitor re-lanza streams congelados/caídos
      Start_All_On_Boot  → bool, arrancar streams en el boot

    Las claves no presentes devuelven None, y la app decide si caer al
    config.json / constante hardcodeada.
    """
    out = {
        "title": None,
        "network": None,
        "midnight_restart": None,
        "auto_restart": None,
        "start_all_on_boot": None,
    }
    try:
        if content is not None:
            cp = configparser.ConfigParser()
            cp.read_string(content)
        elif ini_path and os.path.isfile(ini_path):
            cp = configparser.ConfigParser()
            cp.read(ini_path, encoding="utf-8")
        else:
            return out
        if not cp.has_section("servidor"):
            return out
        s = cp["servidor"]
        if "Title" in s:
            out["title"] = s.get("Title", "").strip() or None
        if "Network" in s:
            out["network"] = s.get("Network", "").strip() or None
        for ini_key, out_key in [
            ("Night_Restart", "midnight_restart"),
            ("Auto_Restart", "auto_restart"),
            ("Start_All_On_Boot", "start_all_on_boot"),
        ]:
            if ini_key in s:
                v = s.get(ini_key, "").strip().lower()
                out[out_key] = v in ("true", "1", "yes", "on")
    except Exception:
        return out
    return out
