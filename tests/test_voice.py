import unittest
from fury.voice import VoiceSessionBroker

class TestVoiceSessionBroker(unittest.TestCase):
    def test_capabilities(self):
        broker = VoiceSessionBroker(api_key="")
        caps = broker.get_capabilities()
        self.assertTrue(caps["webrtc_available"])
        self.assertTrue(caps["full_duplex"])
        self.assertTrue(caps["interruptions_supported"])
        self.assertTrue(caps["offline_mode"])

    def test_create_session_offline(self):
        broker = VoiceSessionBroker(api_key="")
        sess = broker.create_session(user_id="sre-dev")
        self.assertIn("session_id", sess)
        self.assertEqual(sess["mode"], "simulated_local")
        self.assertTrue(sess["webrtc_available"])

    def test_record_turn_and_end(self):
        broker = VoiceSessionBroker(api_key="")
        sess = broker.create_session(user_id="sre-dev")
        sid = sess["session_id"]
        broker.record_turn(sid, "Website is down", "Investigating W3SVC", latency_ms=180.5, interrupted=False)
        broker.record_turn(sid, "Stop that", "Paused", latency_ms=95.0, interrupted=True)
        broker.end_session(sid, interruptions=1, latency_first_audio_ms=210.0)

if __name__ == "__main__":
    unittest.main()
