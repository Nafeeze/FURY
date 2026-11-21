"""Slack approval hygiene: buttons are retired once a request is decided, and stale requests cannot be acted on."""
import sys, threading, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fury import system
from fury.adapters import FakeTarget
from fury.slack_chat import SlackChat
from fury.system import ALERTS

class StubClient:
    def __init__(self): self.updates, self.posts = [], []
    def chat_postMessage(self, channel, text, blocks=None): self.posts.append(text); return {"ts": f"1.{len(self.posts)}"}
    def chat_update(self, **kw): self.updates.append(kw)
    def conversations_list(self, **kw): return {"channels": [{"name": "sre-approvals", "id": "C1"}, {"name": "sre-l2", "id": "C2"}], "response_metadata": {}}

def chat():
    c = SlackChat("x", "y"); c.client = StubClient(); return c

class SlackOutcome(unittest.TestCase):
    def test_approval_replaces_buttons_with_who_decided(self):
        c = chat(); t = FakeTarget(); t.inject("service_down")
        bus, _, _ = system.build(target=t, chat=c)
        def approve_when_asked():   # a human taps Approve on whichever request is waiting
            for _ in range(200):
                p = c.pending()
                if p: c._on_button("U0CAZH32V2T", p[0]["request"], "approve"); return
                time.sleep(0.02)
        threading.Thread(target=approve_when_asked, daemon=True).start()
        out = bus.send("cloudwatch", "handle_alert", ALERTS["service_down"], to="triage").output
        self.assertEqual(out["outcome"], "resolved")
        self.assertTrue(any("Approved by <@U0CAZH32V2T>" in u["text"] for u in c.client.updates), c.client.updates)
        self.assertTrue(all(not any(b.get("type") == "actions" for b in u["blocks"]) for u in c.client.updates), "buttons must be gone")

    def test_timeout_marks_the_request_expired(self):
        c = chat(); t = FakeTarget(); t.inject("service_down")
        bus, _, _ = system.build(target=t, chat=c, ttl=0.3)
        out = bus.send("cloudwatch", "handle_alert", ALERTS["service_down"], to="triage").output
        self.assertEqual(out["outcome"], "escalated")
        self.assertTrue(any("Expired" in u["text"] for u in c.client.updates))

    def test_live_only_while_waiting_and_internal_fields_stay_private(self):
        c = chat(); c.post_approval("#sre-approvals", "text", "r1")
        self.assertTrue(c.live("r1")); self.assertFalse(c.live("never-asked"))
        self.assertTrue(all(not k.startswith("_") for p in c.pending() for k in p))
        c.close("r1"); self.assertFalse(c.live("r1"))

if __name__ == "__main__": unittest.main()
