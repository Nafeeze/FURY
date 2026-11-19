"""External-system adapters. Real: SnowClient. Fakes (same interface) for offline testing: FakeSnow, FakeTarget, FakeChat."""
import os, json, base64, urllib.request, urllib.parse, urllib.error

# ======================= ServiceNow =======================
class SnowError(Exception): pass

def pick_close_code(choices):
    """Choose the best 'resolved' code from whatever this ServiceNow release offers."""
    vals = [c for c in choices if c]
    if "Solved (Permanently)" in vals: return "Solved (Permanently)"
    for key in ("permanent", "solved", "resolved", "solution"):
        for v in vals:
            low = v.lower()
            if key in low and "not " not in low and "work around" not in low: return v
    return vals[0] if vals else "Solved (Permanently)"

OPEN_EXCLUDED_STATES = "6,7,8"   # Resolved, Closed, Canceled: never count as "already open" (FakeSnow uses the same rule)

class SnowClient:
    """Real ServiceNow Table API client (stdlib only). Credentials come from the environment, never logged."""
    def __init__(self, base_url, user, password, timeout=20):
        from .netsafe import require_http
        self.base, self.timeout, self.user = require_http(base_url), timeout, user
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._sid, self._cc, self.close_choices = {}, None, []
    def close_code(self):
        if self._cc: return self._cc
        if os.getenv("SNOW_CLOSE_CODE"): self._cc = os.environ["SNOW_CLOSE_CODE"]; return self._cc
        try:
            r = self._req("GET", "/api/now/table/sys_choice", params={"sysparm_query": "name=incident^element=close_code^inactive=false", "sysparm_fields": "value,sequence"})
            self.close_choices = [x["value"] for x in sorted(r or [], key=lambda x: int(x.get("sequence") or 0))]
        except SnowError: self.close_choices = []
        self._cc = pick_close_code(self.close_choices); return self._cc
    def _req(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": self._auth, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:   # nosec B310: base URL validated by netsafe.require_http (https or loopback)
                return json.loads(r.read() or b"{}").get("result")
        except urllib.error.HTTPError as e:
            raise SnowError(f"ServiceNow {method} {path} -> HTTP {e.code}: {e.read()[:200]!r}")
        except urllib.error.URLError as e:
            raise SnowError(f"ServiceNow unreachable: {e.reason}")
    def find_open(self, key):
        # A Resolved (6) incident is still active=true in ServiceNow until it auto-closes, but the problem is fixed: a
        # recurrence must get a NEW ticket, not a note on the resolved one. So exclude Resolved, Closed and Canceled.
        r = self._req("GET", "/api/now/table/incident", params={"sysparm_query": f"active=true^stateNOT IN{OPEN_EXCLUDED_STATES}^short_descriptionLIKE{key}",
                      "sysparm_limit": 1, "sysparm_fields": "number,sys_id"})
        if r: self._sid[r[0]["number"]] = r[0]["sys_id"]; return r[0]["number"]
        return None
    def create(self, fields):
        r = self._req("POST", "/api/now/table/incident", body=fields)
        self._sid[r["number"]] = r["sys_id"]; return r["number"]
    def _sys_id(self, number):
        if number not in self._sid:
            r = self._req("GET", "/api/now/table/incident", params={"sysparm_query": f"number={number}", "sysparm_limit": 1, "sysparm_fields": "sys_id"})
            if not r: raise SnowError(f"incident {number} not found")
            self._sid[number] = r[0]["sys_id"]
        return self._sid[number]
    def claim(self, number):
        """FURY takes ownership of a ticket it opened: In Progress, assigned to the integration user. Returns True if assigned."""
        fields = {"state": "2"}
        uid = self._user_sys_id()
        if uid: fields["assigned_to"] = uid
        self.update(number, fields); return bool(uid)
    def _user_sys_id(self):
        if not hasattr(self, "_uid"):
            try:
                r = self._req("GET", "/api/now/table/sys_user", params={"sysparm_query": f"user_name={self.user}", "sysparm_limit": 1, "sysparm_fields": "sys_id"})
                self._uid = r[0]["sys_id"] if r else None
            except SnowError: self._uid = None
        return self._uid
    def update(self, number, fields): self._req("PATCH", f"/api/now/table/incident/{self._sys_id(number)}", body=fields)
    def add_note(self, number, text): self.update(number, {"work_notes": text})
    def list_recent(self, key, limit=25):
        r = self._req("GET", "/api/now/table/incident", params={"sysparm_query": f"short_descriptionLIKE{key}^ORDERBYDESCsys_created_on", "sysparm_limit": limit,
                      "sysparm_display_value": "true", "sysparm_fields": "number,short_description,state,priority,assignment_group,assigned_to,sys_created_on,sys_id"})
        for i in r or []: self._sid[i["number"]] = i["sys_id"]
        return [{**{k: i[k] for k in ("number", "short_description", "state", "priority", "sys_created_on")},
                 "assignment_group": (i["assignment_group"] or {}).get("display_value", "") if isinstance(i["assignment_group"], dict) else i["assignment_group"],
                 "assigned_to": ((i.get("assigned_to") or {}).get("display_value", "") if isinstance(i.get("assigned_to"), dict) else i.get("assigned_to") or "").strip()} for i in r or []]
    def list_kb(self, limit=200):
        """Published knowledge articles (read-only). Needs read access to kb_knowledge, which the itil role has."""
        r = self._req("GET", "/api/now/table/kb_knowledge", params={"sysparm_query": "workflow_state=published^ORDERBYnumber", "sysparm_limit": limit, "sysparm_display_value": "true",
                      "sysparm_fields": "number,short_description,text,workflow_state,sys_updated_on,kb_knowledge_base,version"})
        return r or []
    def get_notes(self, number):
        r = self._req("GET", "/api/now/table/sys_journal_field", params={"sysparm_query": f"element_id={self._sys_id(number)}^element=work_notes^ORDERBYsys_created_on",
                      "sysparm_fields": "value,sys_created_on,sys_created_by"})
        return [{"at": x["sys_created_on"], "by": x["sys_created_by"], "text": x["value"]} for x in r or []]
    def find_group(self, name):
        r = self._req("GET", "/api/now/table/sys_user_group", params={"sysparm_query": f"name={name}", "sysparm_limit": 1, "sysparm_fields": "sys_id,name"})
        return r[0]["sys_id"] if r else None
    def ensure_group(self, name):
        gid = self.find_group(name)
        if gid: return gid
        try: return self._req("POST", "/api/now/table/sys_user_group", body={"name": name, "description": "Created by FURY"})["sys_id"]
        except SnowError: return None   # integration user lacks rights; caller notes it in the ticket

class FakeSnow:
    def __init__(self, groups=("SRE-Platform L2",), fail=False):
        self.incidents, self.groups, self.fail, self._n = {}, set(groups), fail, 10000
    def _chk(self):
        if self.fail: raise SnowError("ServiceNow unreachable (simulated)")
    def find_open(self, key):
        self._chk()
        for n, i in self.incidents.items():
            if i["state"] not in tuple(OPEN_EXCLUDED_STATES.split(",")) and key in i["short_description"]: return n
        return None
    def create(self, fields):
        self._chk(); self._n += 1; n = f"INC00{self._n}"
        self.incidents[n] = {**fields, "number": n, "state": "1", "work_notes": [], "assignment_group": ""}; return n
    def update(self, number, fields): self._chk(); self.incidents[number].update(fields)
    def claim(self, number): self.update(number, {"state": "2", "assigned_to": "fury_integration"}); return True
    def add_note(self, number, text): self._chk(); self.incidents[number]["work_notes"].append(text)
    def list_recent(self, key, limit=25):
        return [{"number": n, "short_description": i["short_description"], "state": i["state"], "priority": "2", "assignment_group": i["assignment_group"], "assigned_to": i.get("assigned_to", ""), "sys_created_on": ""}
                for n, i in sorted(self.incidents.items(), reverse=True)[:limit]]
    def get_notes(self, number): return [{"at": "", "by": "fury", "text": t} for t in self.incidents[number]["work_notes"]]
    def list_kb(self, limit=200): return list(getattr(self, "kb_articles", []))
    def close_code(self): return "Solved (Permanently)"
    def find_group(self, name): self._chk(); return name if name in self.groups else None
    def ensure_group(self, name): return self.find_group(name)

# ======================= Target server (Windows EC2 via SSM in production) =======================
class FakeTarget:
    """Simulated Windows EC2 with the same checks/actions the SSM documents will provide."""
    def __init__(self, fix_works=True): self.fix_works = fix_works; self.reset(); self.executed = []
    def reset(self):
        self.disk = {"used_mb": 820, "tmp_mb": 40, "total_mb": 2048}
        self.services = {"W3SVC": "running"}; self.procs = {}; self.mem = 48; self.events = []
    def inject(self, fault):
        if fault == "disk_full":
            self.disk.update(used_mb=1930, tmp_mb=1500); self.events.append("disk: The F: volume is out of space (Event 2013)")
        elif fault == "service_down":
            self.services["W3SVC"] = "stopped"; self.events.append("Service Control Manager: W3SVC terminated unexpectedly (Event 7034)")
        elif fault == "cpu_runaway": self.procs["fury-rogue"] = 97
        elif fault == "memory_pressure": self.mem = 96; self.events.append("Resource-Exhaustion-Detector: low virtual memory (Event 2004)")
        else: raise ValueError(fault)
    # ---- read-only ----
    def check_disk(self, drive="F:"):
        d = self.disk; pct = round(d["used_mb"] * 100 / d["total_mb"], 1)
        return {"drive": drive, "used_percent": pct, "used_mb": d["used_mb"], "tmp_mb": d["tmp_mb"], "healthy": pct < 85}
    def check_cpu(self):
        top = max(self.procs.values(), default=5)
        return {"top_process": max(self.procs, key=self.procs.get) if self.procs else "none", "top_cpu_percent": top, "healthy": top < 80}
    def check_service(self, name): s = self.services.get(name, "unknown"); return {"service": name, "state": s, "healthy": s == "running"}
    def check_memory(self): return {"used_percent": self.mem, "healthy": self.mem < 90}
    def read_events(self, log="System", last="20"): return {"log": log, "events": self.events[-int(last):], "healthy": True}
    # ---- actions ----
    def act(self, action, target):
        self.executed.append((action, target))
        if action == "clear_temp_files":
            if self.fix_works: freed = self.disk["tmp_mb"]; self.disk["used_mb"] -= freed; self.disk["tmp_mb"] = 0
            return {"freed_mb": freed if self.fix_works else 0}
        if action == "restart_service":
            if self.fix_works: self.services[target] = "running"
            return {"restarted": target}
        if action == "kill_process":
            if self.fix_works: self.procs.pop(target, None)
            return {"stopped": target}
        raise ValueError(action)

# ======================= Chat (Slack in production) =======================
class FakeChat:
    """Scripted humans. script = list of (user_id, 'approve'|'reject'). Empty script = nobody answers (timeout)."""
    def __init__(self, script=None): self.script, self.messages, self.requests = list(script or []), [], []
    def post(self, channel, text): self.messages.append((channel, text)); return len(self.messages)
    def post_approval(self, channel, text, request_id): self.requests.append(request_id); return self.post(channel, text)
    def wait_decision(self, request_id, timeout): return self.script.pop(0) if self.script else None
    def close(self, request_id): pass
    def pending(self): return []
