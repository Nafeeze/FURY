"""Minimal agent-to-agent layer (A2A-style): agent cards, skills, task states, discovery by skill.
Not the full A2A spec: same ideas (card, task, states incl. input-required), simple JSON over HTTP."""
import contextvars, json, os, time, uuid, urllib.request
from collections import Counter
from dataclasses import dataclass, field
from . import telemetry

STATES = ("submitted", "working", "input-required", "completed", "failed")

@dataclass
class Task:
    skill: str
    input: dict
    sender: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "submitted"
    output: dict = field(default_factory=dict)
    error: str = ""

class Agent:
    name = "agent"
    description = ""
    skills: dict = {}
    def card(self):
        return {"name": self.name, "description": self.description, "version": "1.0", "skills": sorted(self.skills)}
    def run(self, task):
        fn = self.skills.get(task.skill)
        if not fn:
            task.state, task.error = "failed", f"{self.name} has no skill '{task.skill}'"; return task
        task.state = "working"
        try:
            task.output = fn(task) or {}
            if task.state in ("working", "input-required"): task.state = "completed"   # a paused task that returns has finished
        except Exception as e:
            task.state, task.error = "failed", f"{type(e).__name__}: {e}"
        return task

class RemoteAgent:
    """Talks to an agent served over HTTP (see serve())."""
    def __init__(self, url):
        self.url = url.rstrip("/")
        with urllib.request.urlopen(self.url + "/.well-known/agent.json", timeout=10) as r:
            c = json.loads(r.read())
        self.name = c["name"]; self._card = c; self.skills = {s: None for s in c["skills"]}
    def card(self): return self._card
    def run(self, task):
        body = json.dumps({"skill": task.skill, "input": task.input, "sender": task.sender}).encode()
        req = urllib.request.Request(self.url + "/tasks", data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        task.state, task.output, task.error = d["state"], d.get("output", {}), d.get("error", "")
        return task

def serve(agent, port):
    from flask import Flask, jsonify, request
    app = Flask(agent.name)
    @app.get("/.well-known/agent.json")
    def card(): return jsonify(agent.card())
    @app.post("/tasks")
    def tasks():
        b = request.get_json()
        t = agent.run(Task(skill=b["skill"], input=b.get("input", {}), sender=b.get("sender", "")))
        return jsonify(id=t.id, state=t.state, output=t.output, error=t.error)
    return app

# ---------------- Workflow budget: the orchestrator owns the limits, agents cannot bypass them ----------------
# One Budget exists per top-level workflow (a send to "triage"). Every nested hop is charged to it, so delegation
# cannot loop or fan out without bound. Limits are cooperative (checked before each hop, never by killing a thread:
# abandoning a half-finished action would be less safe than finishing it).
# Skills that tell a human something went wrong stay available after the budget runs out, up to a small reserve,
# so "budget exceeded" always ends in an escalation and never in silence.
DEFAULT_AGENT_CALLS = {"knowledge": 3, "diagnostic": 8, "remediation": 2, "comms": 4, "itsm": 20}
RESERVE_SKILLS = {"add_note", "assign", "notify_escalation"}

@dataclass
class Budget:
    max_hops: int = 40
    max_seconds: float = 1800.0
    max_reserve: int = 12
    per_agent: dict = field(default_factory=lambda: dict(DEFAULT_AGENT_CALLS))
    hops: int = 0
    reserve_used: int = 0
    calls: Counter = field(default_factory=Counter)
    started: float = field(default_factory=time.monotonic)
    @classmethod
    def from_env(cls):
        return cls(max_hops=int(os.getenv("FURY_MAX_WORKFLOW_HOPS", "40")), max_seconds=float(os.getenv("FURY_MAX_WORKFLOW_SECONDS", "1800")))
    def elapsed(self): return time.monotonic() - self.started
    def admit(self, agent, skill):
        """Return None and charge the budget if this hop may run; otherwise return the reason it may not."""
        if skill in RESERVE_SKILLS:
            if self.reserve_used >= self.max_reserve: return "escalation reserve used up"
            self.reserve_used += 1; return None
        if self.hops >= self.max_hops: return f"hop limit {self.max_hops} reached"
        if self.elapsed() > self.max_seconds: return f"time limit {int(self.max_seconds)}s reached"
        cap = self.per_agent.get(agent)
        if cap is not None and self.calls[agent] >= cap: return f"{agent} call limit {cap} reached"
        self.hops += 1; self.calls[agent] += 1; return None

_active = contextvars.ContextVar("fury_budget", default=None)

class Bus:
    """Registry plus transport. Agents find each other by skill (discovery), not by hard-coded names."""
    def __init__(self):
        self.agents, self.trace = {}, []
    def register(self, agent): self.agents[agent.name] = agent; return agent
    def find(self, skill):
        for n, a in self.agents.items():
            if skill in a.card()["skills"]: return n
        raise LookupError(f"no agent offers skill '{skill}'")
    def send(self, sender, skill, data, to=None):
        name = to or self.find(skill)
        task = Task(skill=skill, input=data, sender=sender)
        entry = name == "triage" and _active.get() is None      # a new top-level workflow gets its own budget
        token = _active.set(Budget.from_env()) if entry else None
        budget = _active.get()
        try:
            return self._send(budget, entry, sender, skill, name, task)
        finally:
            if entry:
                telemetry.histogram("workflow.hops", budget.hops + budget.reserve_used); telemetry.histogram("workflow.seconds", round(budget.elapsed(), 2))
                _active.reset(token)
    def _send(self, budget, entry, sender, skill, name, task):
        t0 = time.perf_counter()
        with telemetry.span("agent.hop", skill=skill, agent_from=sender, agent_to=name) as sp:
            why = None if (entry or budget is None) else budget.admit(name, skill)
            if why:
                task.state, task.error = "failed", f"budget exceeded: {why}"
                telemetry.set_attr(sp, "budget_exceeded", why); telemetry.counter("budget.exceeded", agent=name, skill=skill)
            else:
                self.agents[name].run(task)
            # Agent.run swallows exceptions into task.state, so record the real outcome here
            telemetry.set_attr(sp, "state", task.state)
            if task.state == "failed":
                sp["ok"], sp["error"] = False, telemetry.redact(task.error, 120)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.trace.append({"from": sender, "to": name, "skill": skill, "state": task.state, "ms": ms})
        telemetry.histogram("agent.hop.duration_ms", ms, skill=skill, to=name)
        telemetry.counter("agent.hop.count", skill=skill, to=name, state=task.state)
        return task
