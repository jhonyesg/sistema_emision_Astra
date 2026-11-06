"""Pruebas del detector y la correccion automatica del audio.

Contexto: un canal puede quedarse con video pero SIN audio. El monitor solo
miraba el contador de frames de video, asi que no lo detectaba y el canal
se quedaba mudo indefinidamente. Aqui se comprueba la logica de decision:

  - salida sin audio + origen con audio -> reiniciar el ffmpeg (arregla)
  - salida sin audio + origen sin audio -> NO reiniciar (no arregla nada)
  - salida con audio -> no hacer nada

Uso: python3 -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402


class FakeStream:
    """Stream de mentira con la misma interfaz que usa el monitor."""

    def __init__(self, destination="rtmp://127.0.0.1:1935/live/test",
                 original="http://127.0.0.1:8000/play/test"):
        self.config = {"destination_url": destination, "original_url": original}
        self.start_time = time.time() - 3600
        self.audio_state = "unknown"
        self.audio_strikes = 0
        self._audio_restarts = 0

    def note_audio_probe(self, result):
        self.audio_state = result.get("state")
        return self.audio_state

    def note_audio_strike(self):
        self.audio_strikes += 1
        return self.audio_strikes

    def clear_audio_strikes(self):
        self.audio_strikes = 0

    def note_audio_restart(self):
        self._audio_restarts += 1

    def audio_restarts_last_hour(self):
        return self._audio_restarts

    def source_audio_state(self, ttl=300):
        return self._source_state

    def restarts_last_hour(self):
        return 0

    _source_state = "ok"


class AudioHealthTests(unittest.TestCase):
    def setUp(self):
        self.mon = monitor.StreamMonitor(check_interval=10, freeze_threshold=20)
        self.mon._audio_alerted_at = {}
        self.restarts = []
        self.notifications = []

        def fake_restart(name, reason="manual"):
            self.restarts.append((name, reason))
            return True

        self.mon._notify = lambda name, msg: self.notifications.append((name, msg))
        self._orig_restart = monitor.stream_manager.restart_stream
        monitor.stream_manager.restart_stream = fake_restart
        self._orig_probe = monitor.probe_audio_packets
        self._orig_may = self.mon._may_restart

    def tearDown(self):
        monitor.stream_manager.restart_stream = self._orig_restart
        monitor.probe_audio_packets = self._orig_probe
        self.mon._may_restart = self._orig_may

    def _set_probe(self, dest_state, source_state="ok"):
        """Sondea distinto segun la URL (destino vs origen)."""
        def probe(url, seconds=8, timeout=25):
            if "1935" in url:      # URL publicada
                return {"state": dest_state, "packets": 0, "video_packets": 180,
                        "detail": ""}
            return {"state": source_state, "packets": 200, "video_packets": 180,
                    "detail": ""}
        monitor.probe_audio_packets = probe

    def test_audio_ok_no_hace_nada(self):
        stream = FakeStream()
        self._set_probe("ok")
        self.mon._check_one_audio("<canal>", stream)
        self.assertEqual(self.restarts, [])
        self.assertEqual(stream.audio_state, "ok")

    def test_salida_muda_y_origen_bueno_reinicia(self):
        """El caso real: origen bien, publicacion muda -> reiniciar."""
        stream = FakeStream()
        self._set_probe("missing", source_state="ok")
        for _ in range(monitor.AUDIO_STRIKES_BEFORE_RESTART):
            self.mon._check_one_audio("<canal>", stream)
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(self.restarts[0][1], "no_audio")

    def test_origen_mudo_no_reinicia(self):
        """Si el origen no tiene audio, reiniciar el ffmpeg no arregla nada."""
        stream = FakeStream()
        stream._source_state = "missing"
        self._set_probe("missing", source_state="missing")
        for _ in range(monitor.AUDIO_STRIKES_BEFORE_RESTART + 2):
            self.mon._check_one_audio("<canal>", stream)
        self.assertEqual(self.restarts, [])
        self.assertTrue(self.notifications)

    def test_tope_por_hora(self):
        """No mas de MAX_AUDIO_RESTARTS_PER_HOUR reinicios por audio."""
        stream = FakeStream()
        self._set_probe("missing", source_state="ok")
        for _ in range((monitor.AUDIO_STRIKES_BEFORE_RESTART + 1) * 6):
            self.mon._check_one_audio("<canal>", stream)
        self.assertLessEqual(
            len(self.restarts), monitor.MAX_AUDIO_RESTARTS_PER_HOUR
        )

    def test_error_de_sondeo_no_dispara_nada(self):
        """Un fallo de red al sondear no puedeprovocar un reinicio."""
        stream = FakeStream()
        self._set_probe("error")
        for _ in range(monitor.AUDIO_STRIKES_BEFORE_RESTART + 2):
            self.mon._check_one_audio("<canal>", stream)
        self.assertEqual(self.restarts, [])


if __name__ == "__main__":
    unittest.main()
