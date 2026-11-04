"""Control de acceso a la API del panel.

Problema que resuelve: la API (start/stop/restart de TODOS los streams)
escuchaba en 0.0.0.0 sin autenticacion: cualquier equipo de la LAN podia
parar la emision.

Esquema:
  - Las IPs de confianza (config: api_trusted_ips, por defecto la LAN y
    localhost) pueden consultar el estado sin token.
  - Las operaciones de escritura (POST/PUT/DELETE) SIEMPRE exigen token.
  - Cualquier otra IP necesita el token para incluso leer.
  - El token se genera solo la primera vez y se guarda en el config de la
    instancia (config.<ini>.json).

La UI (/ ) inyecta el token en la pagina para los clientes de confianza,
de modo que el operador no nota nada.
"""
import hmac
import ipaddress
import logging
import os
import secrets
import threading

logger = logging.getLogger(__name__)

MUTATING_METHODS = ("POST", "PUT", "DELETE", "PATCH")
DEFAULT_TRUSTED_IPS = ["127.0.0.1", "::1", "192.168.0.0/24"]


class ApiAuth:
    def __init__(self, config, base_dir=None):
        self._config = config
        self._base_dir = base_dir or os.getcwd()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _token_path(self):
        """Fichero propio del token.

        El token NO vive en config.json: las rutas que hacen
        load_app_config() + save_app_config() reconstruyen el dict desde el
        disco y dejaban el token fuera (se borraba en cada guardado).
        """
        ini = os.environ.get("ASTRA_INI", "")
        stem = os.path.splitext(os.path.basename(ini))[0] if ini else "default"
        return os.path.join(self._base_dir, f".api_token.{stem}")

    def token(self):
        with self._lock:
            path = self._token_path()
            try:
                with open(path, "r", encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    return token
            except OSError:
                pass
            token = secrets.token_urlsafe(24)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(token)
                logger.info("Token de API generado para esta instancia")
            except OSError as e:
                logger.warning("No se pudo persistir el token de API: %s", e)
            return token

    def trusted_ips(self):
        ips = self._config.get("api_trusted_ips")
        if not ips:
            return list(DEFAULT_TRUSTED_IPS)
        if isinstance(ips, str):
            ips = [ips]
        return ips

    def is_trusted(self, remote_addr):
        if not remote_addr:
            return False
        for net in self.trusted_ips():
            try:
                if ipaddress.ip_address(remote_addr) in ipaddress.ip_network(
                    net, strict=False
                ):
                    return True
            except ValueError:
                if remote_addr == net:
                    return True
        return False

    def token_ok(self, provided):
        if not provided:
            return False
        return hmac.compare_digest(str(provided), str(self.token()))

    # ------------------------------------------------------------------
    def check(self, path, method, remote_addr, provided_token):
        """Devuelve None si la peticion esta permitida, o (respuesta, codigo)."""
        if not path.startswith("/api/"):
            return None

        if self.token_ok(provided_token):
            return None

        if method in MUTATING_METHODS:
            return (
                {"success": False, "error": "Token de API invalido o ausente"},
                401,
            )

        if self.is_trusted(remote_addr):
            return None

        return (
            {"success": False, "error": "Acceso no autorizado desde esta IP"},
            401,
        )
