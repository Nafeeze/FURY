"""Alert a human when FURY's own SLOs breach (and when they recover).

Edge-triggered: one message when an SLO flips to `breach`, one when it flips back, never a message per poll. `no_data`
is not a breach (an idle system must not page anyone). Notification goes through a callable so it works with Slack,
the fake chat in tests, or anything else; delivery failures are swallowed so the watcher can never take FURY down."""
import threading, time
from . import slo as SLO, telemetry


class SloWatcher:
    def __init__(self, notify, evaluate=SLO.evaluate, interval=60.0, audit=None, sleep=time.sleep):
        self.notify, self.evaluate, self.interval, self.audit, self._sleep = notify, evaluate, interval, audit, sleep
        self.state = {}          # slo id -> last seen status
        self.stop = threading.Event()

    def check_once(self):
        """Evaluate once; return the messages sent (useful in tests)."""
        sent = []
        for s in self.evaluate():
            prev, cur = self.state.get(s["id"]), s["status"]
            self.state[s["id"]] = cur
            if cur == "breach" and prev != "breach":
                msg = f":rotating_light: *FURY SLO breach*: {s['description']}\nMeasured {s['measured']}{s.get('unit') or ''}, objective {s['objective']}."
            elif prev == "breach" and cur != "breach":
                msg = f":white_check_mark: FURY SLO recovered: {s['description']} (now {s['measured']}{s.get('unit') or ''})."
            else:
                continue
            sent.append(msg)
            try: self.notify(msg)
            except Exception as e: telemetry.counter("slo.alert.error", error=type(e).__name__)
            if self.audit: self.audit.record("system", "slo.alert", "breach" if cur == "breach" else "recovered", slo=s["id"])
        return sent

    def run_forever(self):
        while not self.stop.is_set():
            try: self.check_once()
            except Exception as e: telemetry.counter("slo.alert.error", error=type(e).__name__)
            self._sleep(self.interval)
