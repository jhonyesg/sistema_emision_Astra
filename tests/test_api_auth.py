"""Pruebas del control de acceso a la API.

Contexto: tras un reinicio de equipo, el navegador del operador cambio de IP
(.117 -> .137). La lista de IPs de confianza vivia en config.json y la app
la leia SOLO al arrancar, asi que el panel se quedaba en 401 (0 streams,
"cargando...", "sin servicio systemd") con la emision perfectamente viva.

Con el cargador de config, la lista se relee del disco: cambiar
api_trusted_ips ya no exige reiniciar el servicio.

Uso: python3 -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_auth import DEFAULT_TRUSTED_IPS, ApiAuth  # noqa: E402


class ApiAuthTests(unittest.TestCase):
    def test_ip_de_confianza_sin_cargador(self):
        auth = ApiAuth({"api_trusted_ips": ["127.0.0.1", "192.0.2.117"]})
        self.assertTrue(auth.is_trusted("192.0.2.117"))
        self.assertTrue(auth.is_trusted("127.0.0.1"))
        self.assertFalse(auth.is_trusted("192.0.2.137"))

    def test_rango_cidr(self):
        auth = ApiAuth({"api_trusted_ips": ["192.0.2.0/24"]})
        self.assertTrue(auth.is_trusted("192.0.2.137"))
        self.assertTrue(auth.is_trusted("192.0.2.8"))
        self.assertFalse(auth.is_trusted("10.0.0.5"))

    def test_default_sin_lista(self):
        auth = ApiAuth({})
        self.assertEqual(auth.trusted_ips(), DEFAULT_TRUSTED_IPS)

    def test_la_lista_se_relee_del_disco(self):
        """Cambiar api_trusted_ips surte efecto sin reiniciar la app."""
        state = {"api_trusted_ips": ["127.0.0.1"]}
        auth = ApiAuth(state, config_loader=lambda: state)
        self.assertFalse(auth.is_trusted("192.0.2.137"))

        state["api_trusted_ips"] = ["127.0.0.1", "192.0.2.137"]
        auth._cached_at = 0.0          # simula el paso del TTL
        self.assertTrue(auth.is_trusted("192.0.2.137"))

    def test_cargador_roto_no_rompe(self):
        def boom():
            raise RuntimeError("config ilegible")
        auth = ApiAuth({"api_trusted_ips": ["192.0.2.117"]}, config_loader=boom)
        self.assertTrue(auth.is_trusted("192.0.2.117"))

    def test_escrituras_siempre_exigen_token(self):
        """Aunque la IP sea de confianza, POST sin token se rechaza."""
        auth = ApiAuth({"api_trusted_ips": ["192.0.2.137"]})
        denied = auth.check("/api/stream/x/restart", "POST", "192.0.2.137", "")
        self.assertIsNotNone(denied)
        self.assertEqual(denied[1], 401)

    def test_lectura_desde_ip_no_confianza_sin_token(self):
        auth = ApiAuth({"api_trusted_ips": ["127.0.0.1"]})
        denied = auth.check("/api/status", "GET", "192.0.2.137", "")
        self.assertIsNotNone(denied)
        self.assertEqual(denied[1], 401)

    def test_token_valido_abre_puerta(self):
        auth = ApiAuth({"api_trusted_ips": []})
        denied = auth.check("/api/status", "GET", "192.0.2.137", auth.token())
        self.assertIsNone(denied)


if __name__ == "__main__":
    unittest.main()
