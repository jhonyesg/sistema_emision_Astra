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
                "vlc_transcode": self._get_value(section, "VLC_TRANSCODE")
                or self._get_value(section, "vlc_transcode")
                or "",
                "vlc_port": int(self._get_value(section, "VLC_PORT", "0") or 0)
                or None,
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


def update_stream_section(ini_path, name, updates):
    """Actualiza claves de la sección [name] en el INI preservando el resto.

    `updates` es un dict con las claves a escribir. Las claves que no estén
    en `updates` se dejan tal cual. Si el valor es None o string vacío, la
    clave se elimina del bloque.

    Preserva: comentarios, líneas en blanco, orden de claves existentes,
    secciones vecinas.

    Returns: (success: bool, error: str|None, old_section: dict|None)
    """
    import re
    if not os.path.isfile(ini_path):
        return False, "INI no existe", None

    with open(ini_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    section_re = re.compile(r"^\[([^\]]+)\]\s*$")
    header_idx = None
    next_section_idx = len(lines)
    for i, line in enumerate(lines):
        m = section_re.match(line)
        if m:
            if m.group(1).strip() == name:
                if header_idx is None:
                    header_idx = i
            elif header_idx is not None:
                next_section_idx = i
                break

    if header_idx is None:
        return False, f"Sección [{name}] no encontrada", None

    body_start = header_idx + 1
    body_end = next_section_idx

    old_section = {}
    preserved_lines = []
    for line in lines[body_start:body_end]:
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            preserved_lines.append(line)
            continue
        if "=" not in stripped:
            preserved_lines.append(line)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in updates:
            old_section[key] = value.strip()
            new_val = updates[key]
            if new_val is None or (isinstance(new_val, str) and new_val.strip() == ""):
                continue
            preserved_lines.append(f"{key}={new_val}\n")
        else:
            old_section[key] = value.strip()
            preserved_lines.append(line)

    for key, value in updates.items():
        if key in old_section or value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        preserved_lines.append(f"{key}={value}\n")

    new_lines = preserved_lines

    new_block = lines[:body_start] + new_lines + lines[body_end:]
    try:
        load_config_from_content("".join(new_block)).get_all_streams()
    except Exception as e:
        return False, f"INI resultante inválido: {e}", old_section

    tmp_path = ini_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(new_block)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, ini_path)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False, str(e), old_section

    return True, None, old_section


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
        "service": None,
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
        if "Service" in s:
            out["service"] = s.get("Service", "").strip() or None
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
