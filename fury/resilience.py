"""Integration resilience: retries for SAFE reads only, a circuit breaker so a dead system fails fast, and health.

State-changing calls (create ticket, update, add note, run an action) are NEVER retried: a retry after an ambiguous
failure could duplicate the operation. They fail once, visibly, and the workflow escalates. Reads are idempotent, so a
couple of retries with backoff are safe. After `threshold` consecutive failed calls the breaker opens for `cooldown`
seconds (calls fail instantly instead of hanging the workflow), then one trial call is allowed."""
import time
from . import telemetry


class IntegrationUnavailable(Exception):
    pass


class Resilient:
    def __init__(self, inner, name, read_methods, retries=2, backoff=0.3, threshold=5, cooldown=30.0, sleep=time.sleep, clock=time.monotonic):
        self._inner, self._name, self._reads = inner, name, set(read_methods)
        self._retries, self._backoff, self._threshold, self._cooldown, self._sleep, self._clock = retries, backoff, threshold, cooldown, sleep, clock
        self._fails, self._open_until, self._last_error, self._last_ok = 0, 0.0, "", None

    def __getattr__(self, attr):
        target = getattr(self._inner, attr)
        if attr.startswith("_") or not callable(target):
            return target

        def call(*a, **kw):
            now = self._clock()
            if self._fails >= self._threshold and now < self._open_until:
                telemetry.counter("integration.call", integration=self._name, outcome="circuit_open")
                raise IntegrationUnavailable(f"{self._name} is temporarily unavailable (circuit open after {self._fails} consecutive failures); last error: {self._last_error}")
            attempts = self._retries + 1 if attr in self._reads else 1
            err = None
            for i in range(attempts):
                try:
                    res = target(*a, **kw)
                except Exception as e:
                    err = e
                    if i < attempts - 1:
                        self._sleep(self._backoff * 2 ** i)
                    continue
                self._fails, self._last_ok = 0, time.strftime("%H:%M:%S")
                telemetry.counter("integration.call", integration=self._name, outcome="ok")
                return res
            self._fails += 1
            self._last_error = telemetry.redact(f"{type(err).__name__}: {err}", 160)
            if self._fails >= self._threshold:
                self._open_until = self._clock() + self._cooldown
            telemetry.counter("integration.call", integration=self._name, outcome="error")
            raise err

        return call

    def health(self):
        open_ = self._fails >= self._threshold and self._clock() < self._open_until
        return {"name": self._name, "state": "open" if open_ else ("degraded" if self._fails else "ok"),
                "consecutive_failures": self._fails, "last_error": self._last_error, "last_ok": self._last_ok}


SNOW_READS = {"find_open", "list_recent", "get_notes", "find_group", "close_code"}   # create/update/add_note/ensure_group are writes
