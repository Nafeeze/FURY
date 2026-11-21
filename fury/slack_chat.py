"""Slack adapter (Socket Mode: connects outward, no public URL). slack_bolt is imported only in start()."""
import queue, threading, time

YES, NO = {"yes", "y", "approve", "approved", "ok", "go ahead", "do it"}, {"no", "n", "reject", "rejected", "deny", "stop"}

class SlackChat:
    def __init__(self, bot_token, app_token, approvals_channel="#sre-approvals", l2_channel="#sre-l2"):
        self.bot_token, self.app_token = bot_token, app_token
        self.names = {"approvals": approvals_channel, "l2": l2_channel}
        self.q, self.ts2req, self._pending, self.lock, self.cids, self.client = {}, {}, {}, threading.Lock(), {}, None
    # ---------- pure logic (unit-testable without Slack) ----------
    def _queue(self, rid):
        with self.lock: return self.q.setdefault(rid, queue.Queue())
    def _on_button(self, user, rid, decision): self._queue(rid).put((user, decision))
    def _on_message(self, event):
        if event.get("bot_id") or event.get("subtype"): return
        rid = self.ts2req.get(event.get("thread_ts"))
        if not rid: return
        t = (event.get("text") or "").strip().lower().rstrip(".!")
        if t in YES: self._on_button(event["user"], rid, "approve")
        elif t in NO: self._on_button(event["user"], rid, "reject")
    def wait_decision(self, rid, timeout):
        try: return self._queue(rid).get(timeout=max(timeout, 0.1))
        except queue.Empty: return None
    def close(self, rid):
        with self.lock: self._pending.pop(rid, None); self.q.pop(rid, None)
    def pending(self):
        with self.lock: return [{"request": r, **{k: x for k, x in v.items() if not k.startswith("_")}} for r, v in self._pending.items()]
    # ---------- Slack I/O ----------
    def _cid(self, name):
        if name.startswith("C") and name.isupper(): return name
        n = name.lstrip("#")
        if n not in self.cids:
            cur = None
            while True:
                r = self.client.conversations_list(types="public_channel,private_channel", limit=200, cursor=cur)
                for c in r["channels"]: self.cids[c["name"]] = c["id"]
                cur = (r.get("response_metadata") or {}).get("next_cursor")
                if not cur or n in self.cids: break
        if n not in self.cids: raise RuntimeError(f"Slack channel #{n} not found or the bot is not in it")
        return self.cids[n]
    def post(self, channel, text, blocks=None):
        r = self.client.chat_postMessage(channel=self._cid(channel), text=text, blocks=blocks); return r["ts"]
    def post_approval(self, channel, text, rid):
        btn = lambda label, aid, style: {"type": "button", "text": {"type": "plain_text", "text": label}, "style": style, "action_id": aid, "value": rid}
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                  {"type": "actions", "elements": [btn("Approve", "fury_approve", "primary"), btn("Reject", "fury_reject", "danger")]},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text": "You can also reply *yes* or *no* in this thread."}]}]
        ts = self.post(channel, text, blocks)
        self.ts2req[ts] = rid
        with self.lock: self._pending[rid] = {"text": text, "since": time.strftime("%H:%M:%S"), "_msg": (self._cid(channel), ts)}
        return ts
    def live(self, rid):
        """True while a request is still waiting. A click on an old message (after a restart or a timeout) must not queue a phantom decision."""
        with self.lock: return rid in self._pending
    def outcome(self, rid, text):
        """Replace the approval message's buttons with the result, so nobody can click a finished request and the thread shows who decided."""
        with self.lock: p = self._pending.get(rid)
        if not p or "_msg" not in p or self.client is None: return
        cid, ts = p["_msg"]
        try:
            self.client.chat_update(channel=cid, ts=ts, text=text, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": p["text"]}},
                                    {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}])
        except Exception: pass   # cosmetic: the decision itself is already recorded
    def start(self):
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        app = App(token=self.bot_token); self.client = app.client
        def click(body, decision):
            rid = body["actions"][0]["value"]
            if self.live(rid): self._on_button(body["user"]["id"], rid, decision)
            else:   # stale message: retire its buttons instead of acting on it
                c = body.get("container") or {}
                try: self.client.chat_update(channel=c["channel_id"], ts=c["message_ts"], text="This approval request is no longer active.",
                                             blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": ":no_entry_sign: This approval request is no longer active (expired, answered, or FURY restarted). Nothing was run."}}])
                except Exception: pass
        @app.action("fury_approve")
        def _a(ack, body): ack(); click(body, "approve")
        @app.action("fury_reject")
        def _r(ack, body): ack(); click(body, "reject")
        @app.event("message")
        def _m(event): self._on_message(event)
        handler = SocketModeHandler(app, self.app_token)
        def _run():
            try: handler.start()
            except ValueError: pass   # bolt tries to install a SIGINT handler, which Python only allows on the main thread; the connection is already up
        threading.Thread(target=_run, daemon=True).start()
        time.sleep(2)
