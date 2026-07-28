import configparser


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
