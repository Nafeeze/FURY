import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fury import llm, system
from fury.adapters import FakeChat, FakeSnow, FakeTarget
from fury.webapp import Ctx, _ChatSessionPolicy, create_app


class ChatPolicyTests(unittest.TestCase):
    def setUp(self):
        self.target, self.snow, self.chat = FakeTarget(), FakeSnow(), FakeChat()
        bus, triage, _ = system.build(target=self.target, snow=self.snow, chat=self.chat)
        self.ctx = Ctx(bus, triage, self.snow, self.chat, "i-test", self.target)
        self.app = create_app(self.ctx, demo=True)
        self.app.testing = True
        self.client = self.app.test_client()

    def post_chat(self, message, session_id="session-test"):
        return self.client.post(
            "/api/chat",
            json={"message": message},
            headers={"X-Fury-Session": session_id},
        )

    def run_message(self, response):
        run_id = response.get_json()["run"]
        return self.client.get(f"/api/run/{run_id}").get_json()["message"]

    def test_default_readonly_refuses_report_without_submitting(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FURY_CHAT_MODE", None)
            with patch("fury.webapp.LLM.classify", return_value={"intent": "report"}):
                with patch.object(self.ctx, "submit") as submit:
                    response = self.post_chat("the disk is full")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.run_message(response),
            "Incident reporting is disabled in read-only mode. You can still check incident status, list incidents, or ask how-to questions.",
        )
        submit.assert_not_called()

    def test_full_mode_submits_report(self):
        callbacks = []

        def fake_submit(skill, data, source, label, on_complete=None):
            callbacks.append(on_complete)
            return self.ctx.answer(label, "queued")

        with patch.dict(os.environ, {"FURY_CHAT_MODE": "full"}):
            with patch("fury.webapp.LLM.classify", return_value={"intent": "report"}):
                with patch.object(self.ctx, "submit", side_effect=fake_submit) as submit:
                    response = self.post_chat("the disk is full")

        self.assertEqual(response.status_code, 200)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0], "handle_report")
        self.assertTrue(callable(callbacks[0]))
        callbacks[0]()

    def test_status_list_and_howto_work_in_readonly_mode(self):
        ticket = self.snow.create({"short_description": "disk test", "description": "test"})
        self.snow.add_note(ticket, "existing note")

        with patch.dict(os.environ, {"FURY_CHAT_MODE": "readonly"}):
            with patch("fury.webapp.LLM.classify", return_value=None):
                with patch.object(self.ctx, "submit") as submit:
                    status = self.post_chat(f"status of {ticket}", "status-session")
                    incidents = self.post_chat("show open incidents", "list-session")
                    howto = self.post_chat("how do I fix a full disk?", "howto-session")

        self.assertEqual(status.status_code, 200)
        self.assertIn(ticket, self.run_message(status))
        self.assertEqual(incidents.status_code, 200)
        self.assertIn(ticket, self.run_message(incidents))
        self.assertEqual(howto.status_code, 200)
        self.assertIn("Runbook", self.run_message(howto))
        submit.assert_not_called()

    def test_idle_session_over_30_minutes_resets_context(self):
        now = [1000.0]
        policy = _ChatSessionPolicy(clock=lambda: now[0])
        store = llm.SessionStore()
        session_id = str(uuid.uuid4())

        self.assertTrue(policy.begin(session_id, store.reset))
        policy.finish(session_id)
        store.record(session_id, "disk", "old context", True)

        now[0] += 30 * 60 + 1
        self.assertTrue(policy.begin(session_id, store.reset))
        self.assertEqual(store.context_block(session_id), "No prior context in this session.")
        policy.finish(session_id)

    def test_second_request_for_inflight_session_gets_busy_message(self):
        callbacks = []

        def fake_submit(skill, data, source, label, on_complete=None):
            callbacks.append(on_complete)
            return self.ctx.answer(label, "queued")

        with patch.dict(os.environ, {"FURY_CHAT_MODE": "full"}):
            with patch("fury.webapp.LLM.classify", return_value={"intent": "report"}):
                with patch.object(self.ctx, "submit", side_effect=fake_submit) as submit:
                    first = self.post_chat("the disk is full", "same-session")
                    second = self.post_chat("the CPU is high", "same-session")
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(second.status_code, 200)
                    self.assertEqual(
                        self.run_message(second),
                        "Your previous request is still running. Please wait.",
                    )
                    submit.assert_called_once()
                    callbacks[0]()

                    third = self.post_chat("the disk is full", "same-session")
                    self.assertEqual(third.status_code, 200)
                    self.assertEqual(submit.call_count, 2)
                    callbacks[1]()

    def test_inflight_flag_is_released_after_request_exception(self):
        with patch.dict(os.environ, {"FURY_CHAT_MODE": "readonly"}):
            with patch(
                "fury.webapp.LLM.classify",
                side_effect=[RuntimeError("test failure"), None],
            ):
                with self.assertRaises(RuntimeError):
                    self.post_chat("hello", "retry-session")
                response = self.post_chat("hello", "retry-session")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Hello", self.run_message(response))

    def test_inflight_flag_is_released_after_worker_exception(self):
        with patch.dict(os.environ, {"FURY_CHAT_MODE": "full"}):
            with patch(
                "fury.webapp.LLM.classify",
                side_effect=[{"intent": "report"}, None],
            ):
                with patch.object(self.ctx.bus, "send", side_effect=RuntimeError("worker failure")):
                    with patch("threading.excepthook"):
                        first = self.post_chat("the disk is full", "worker-session")
                        self.assertEqual(first.status_code, 200)

                        message = None
                        for _ in range(100):
                            response = self.post_chat("hello", "worker-session")
                            message = self.run_message(response)
                            if message != "Your previous request is still running. Please wait.":
                                break
                            time.sleep(0.01)

        self.assertIn("Hello", message)


if __name__ == "__main__":
    unittest.main()
