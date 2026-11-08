from pathlib import Path
from .a2a import Bus
from . import runbooks as RB
from .adapters import FakeTarget, FakeSnow, FakeChat
from .agents import Knowledge, Diagnostic, Remediation, ITSM, Comms, Triage

class ReadOnlyView:
    def __init__(self, t): self._t = t
    def __getattr__(self, n):
        if n.startswith("check_") or n == "read_events": return getattr(self._t, n)
        raise AttributeError(f"read-only view has no '{n}'")
class ActView:
    def __init__(self, t): self._t = t
    def act(self, action, target): return self._t.act(action, target)

def build(target=None, snow=None, chat=None, approvers=("U0CAZH32V2T",), rb_dir=None, ttl=600, read_target=None, act_target=None):
    if read_target is None and act_target is None: target = target or FakeTarget(); read_target = act_target = target
    snow = snow or FakeSnow()
    chat = chat if chat is not None else FakeChat()
    bus = Bus(); rbs = RB.load(rb_dir or Path(__file__).resolve().parent.parent / "runbooks")
    for a in (Knowledge(rbs), Diagnostic(ReadOnlyView(read_target)), Remediation(bus, ActView(act_target)), ITSM(snow), Comms(chat, approvers, ttl=ttl)):
        bus.register(a)
    triage = bus.register(Triage(bus))
    return bus, triage, dict(target=read_target if read_target is act_target else None, snow=snow, chat=chat, read_target=read_target, act_target=act_target)

def alert(name, description, metric, instance="i-00654e3403673c840", severity="major"):
    return {"name": name, "description": description, "metric": metric, "instance": instance, "severity": severity}
ALERTS = {
    "disk_full": alert("Fury-Disk-F-High", "Disk F: usage above 90%", "disk"),
    "service_down": alert("Fury-W3SVC-Stopped", "W3SVC service stopped", "service"),
    "cpu_runaway": alert("Fury-CPU-High", "CPU utilization above 90%", "cpu"),
    "memory_pressure": alert("Fury-Mem-High", "Memory usage above 90%", "memory"),
}
def run_alert(bus, a):
    from .a2a import Task
    return bus.send("cloudwatch", "handle_alert", a, to="triage")
