"""Minimal agent-to-agent layer (A2A-style): agent cards, skills, task states, discovery by skill.
Not the full A2A spec: same ideas (card, task, states incl. input-required), simple JSON over HTTP."""
import json, time, uuid, urllib.request
from dataclasses import dataclass, field

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
        t0 = time.perf_counter()
        self.agents[name].run(task)
        self.trace.append({"from": sender, "to": name, "skill": skill, "state": task.state,
                           "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return task
