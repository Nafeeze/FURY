"""Logic tests for servicenow_setup/fury_snow_setup.js using an in-memory mock of GlideRecord/gs, run with Windows
cscript (JScript/ES3, so it also proves the script avoids modern-only syntax). This does NOT prove behaviour on a real
instance (field names and password handling must still be confirmed there with the DRY_RUN output and the curl check)."""
import os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "servicenow_setup" / "fury_snow_setup.js").read_text(encoding="utf-8")
CSCRIPT = shutil.which("cscript")

MOCK = r"""
var __out = [];
var __id = 100;
var DB = {sys_user_group: [], sys_user: [], sys_user_role: [], sys_user_has_role: [], sys_user_grmember: [], sys_choice: []};
var ADMIN = true, PWSET = 0, PWVALUE = null;
var gs = {print: function (m) { WScript.Echo(m); }, hasRole: function (r) { return ADMIN; }};
function norm(v) { return v === true ? '1' : (v === false ? '0' : String(v)); }
function GlideRecord(table) {
    var self = this;
    this.table = table; this.q = []; this.rows = []; this.i = -1; this.cur = null; this.lim = 0; this.isNew = false;
    this.user_password = {setDisplayValue: function (v) { PWSET++; PWVALUE = v; if (self.cur) self.cur._pw = '<hashed>'; }};
}
GlideRecord.prototype.addQuery = function (f, v) { this.q.push([f, norm(v)]); };
GlideRecord.prototype.setLimit = function (n) { this.lim = n; };
GlideRecord.prototype.orderBy = function (f) {};
GlideRecord.prototype.query = function () {
    this.rows = []; this.i = -1;
    var all = DB[this.table];
    for (var k = 0; k < all.length; k++) {
        var ok = true;
        for (var j = 0; j < this.q.length; j++) { if (String(all[k][this.q[j][0]]) !== this.q[j][1]) ok = false; }
        if (ok) this.rows.push(all[k]);
    }
    if (this.lim && this.rows.length > this.lim) this.rows.length = this.lim;
};
GlideRecord.prototype.next = function () { this.i++; if (this.i < this.rows.length) { this.cur = this.rows[this.i]; return true; } return false; };
GlideRecord.prototype.getValue = function (f) { return this.cur && this.cur[f] !== undefined ? String(this.cur[f]) : ''; };
GlideRecord.prototype.getUniqueValue = function () { return this.cur.sys_id; };
GlideRecord.prototype.initialize = function () { this.cur = {}; this.isNew = true; };
GlideRecord.prototype.setValue = function (f, v) { this.cur[f] = norm(v); };
GlideRecord.prototype.insert = function () { this.cur.sys_id = 'id' + (__id++); DB[this.table].push(this.cur); return this.cur.sys_id; };
GlideRecord.prototype.update = function () { return this.cur.sys_id; };
function addRole(name) { DB.sys_user_role.push({sys_id: 'role_' + name, name: name}); }
function counts() {
    return 'DB groups=' + DB.sys_user_group.length + ' users=' + DB.sys_user.length + ' grants=' + DB.sys_user_has_role.length + ' members=' + DB.sys_user_grmember.length + ' pwset=' + PWSET;
}
"""


def configure(script, dry=True, password="Str0ng-Passw0rd-Example", members="[]", reset="false"):
    s = script
    s = s.replace("var DRY_RUN = true;", f"var DRY_RUN = {'true' if dry else 'false'};", 1)
    s = s.replace("var SERVICE_PASSWORD = 'CHANGE-ME-before-running';", f"var SERVICE_PASSWORD = '{password}';", 1)
    s = s.replace("var L2_MEMBERS = [];", f"var L2_MEMBERS = {members};", 1)
    s = s.replace("var RESET_PASSWORD = false;", f"var RESET_PASSWORD = {reset};", 1)
    return s


def run(scenario_js, *runs):
    """runs = configured script variants executed in order inside one mock instance. Returns combined output."""
    body = MOCK + "\n" + scenario_js + "\n" + "\n".join(f"WScript.Echo('=== RUN {i} ===');\n{r}\nWScript.Echo(counts());" for i, r in enumerate(runs, 1))
    f = Path(tempfile.mkdtemp()) / "t.js"; f.write_text(body, encoding="utf-8")
    p = subprocess.run([CSCRIPT, "//nologo", str(f)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


@unittest.skipUnless(sys.platform == "win32" and CSCRIPT, "needs Windows cscript")
class SnowSetupTests(unittest.TestCase):
    ITIL = "addRole('itil');"

    def test_dry_run_reports_but_changes_nothing(self):
        out = run(self.ITIL, configure(SCRIPT, dry=True))
        for want in ('Would create group "SRE-Platform L2"', 'Would create user "fury_integration"', 'Would grant role "itil"', "DRY RUN"):
            self.assertIn(want, out)
        self.assertIn("DB groups=0 users=0 grants=0 members=0 pwset=0", out)

    def test_real_run_creates_group_user_and_role_and_never_prints_the_password(self):
        pw = "Str0ng-Passw0rd-Example"
        out = run(self.ITIL, configure(SCRIPT, dry=False, password=pw))
        self.assertIn("DB groups=1 users=1 grants=1 members=0 pwset=1", out); self.assertIn("3 change(s) made", out); self.assertIn("No problems found", out)
        self.assertNotIn(pw, out, "the service account password must never be printed")
        self.assertIn("curl -s -u fury_integration:PASSWORD", out)

    def test_real_run_does_not_print_would_lines(self):
        self.assertNotIn("Would", run(self.ITIL, configure(SCRIPT, dry=False)))

    def test_user_is_web_service_only_active_and_unlocked(self):
        probe = "var u = DB.sys_user[0]; WScript.Echo('USER ws=' + u.web_service_access_only + ' active=' + u.active + ' locked=' + u.locked_out + ' reset=' + u.password_needs_reset + ' pw=' + u._pw);"
        out = run(self.ITIL, configure(SCRIPT, dry=False), probe)
        self.assertIn("USER ws=1 active=1 locked=0 reset=0 pw=<hashed>", out)

    def test_second_run_is_idempotent(self):
        out = run(self.ITIL, configure(SCRIPT, dry=False), configure(SCRIPT, dry=False))
        second = out.split("=== RUN 2 ===")[1]
        self.assertIn("0 change(s) made", second); self.assertIn("already exists", second); self.assertIn("already has role", second)
        self.assertTrue(out.rstrip().endswith("DB groups=1 users=1 grants=1 members=0 pwset=1"))

    def test_existing_misconfigured_user_is_repaired_not_duplicated(self):
        pre = self.ITIL + "DB.sys_user.push({sys_id:'u1', user_name:'fury_integration', active:'0', web_service_access_only:'0', locked_out:'1', password_needs_reset:'1'});"
        out = run(pre, configure(SCRIPT, dry=False), "var u = DB.sys_user[0]; WScript.Echo('USER ws=' + u.web_service_access_only + ' active=' + u.active + ' locked=' + u.locked_out + ' reset=' + u.password_needs_reset);")
        self.assertIn("fixed web_service_access_only, active, locked_out, password_needs_reset", out)
        self.assertIn("USER ws=1 active=1 locked=0 reset=0", out); self.assertIn("users=1", out)
        self.assertIn("pwset=0", out, "an existing user's password is left alone unless RESET_PASSWORD is true")

    def test_reset_password_flag_sets_it_on_existing_user(self):
        pre = self.ITIL + "DB.sys_user.push({sys_id:'u1', user_name:'fury_integration', active:'1', web_service_access_only:'1', locked_out:'0', password_needs_reset:'0'});"
        out = run(pre, configure(SCRIPT, dry=False, reset="true"))
        self.assertIn("pwset=1", out)

    def test_weak_or_placeholder_password_refuses_to_run(self):
        for pw in ("CHANGE-ME-before-running", "short"):
            out = run(self.ITIL, configure(SCRIPT, dry=False, password=pw))
            self.assertIn("STOP: set SERVICE_PASSWORD", out); self.assertIn("users=0", out)

    def test_password_not_required_when_it_will_not_be_used(self):
        pre = self.ITIL + "DB.sys_user.push({sys_id:'u1', user_name:'fury_integration', active:'1', web_service_access_only:'0', locked_out:'0', password_needs_reset:'0'});"
        out = run(pre, configure(SCRIPT, dry=False, password="CHANGE-ME-before-running", reset="false"))
        self.assertNotIn("STOP", out); self.assertIn("pwset=0", out); self.assertIn("fixed web_service_access_only", out)

    def test_reset_password_true_still_demands_a_strong_password(self):
        pre = self.ITIL + "DB.sys_user.push({sys_id:'u1', user_name:'fury_integration', active:'1', web_service_access_only:'1', locked_out:'0', password_needs_reset:'0'});"
        for pw in ("n=al:-IU37", "CHANGE-ME-before-running"):
            out = run(pre, configure(SCRIPT, dry=False, password=pw, reset="true"))
            self.assertIn("STOP: set SERVICE_PASSWORD", out); self.assertIn("pwset=0", out)

    def test_non_admin_is_refused(self):
        out = run("ADMIN = false;" + self.ITIL, configure(SCRIPT, dry=False))
        self.assertIn("STOP: run this as a user with the admin role", out); self.assertIn("users=0", out)

    def test_missing_itil_role_is_reported_not_silently_ignored(self):
        out = run("", configure(SCRIPT, dry=False))
        self.assertIn('PROBLEM: role "itil" not found', out); self.assertIn("1 problem(s)", out); self.assertIn("grants=0", out)

    def test_group_members_added_once_and_unknown_user_reported(self):
        pre = self.ITIL + "DB.sys_user.push({sys_id:'h1', user_name:'abel.tuter'});"
        cfg = configure(SCRIPT, dry=False, members="['abel.tuter','ghost.user']")
        out = run(pre, cfg, cfg)
        self.assertIn('PROBLEM: L2 member "ghost.user" not found', out); self.assertIn("members=1", out)
        self.assertIn('"abel.tuter" is already in "SRE-Platform L2"', out.split("=== RUN 2 ===")[1])
        self.assertTrue(out.rstrip().endswith("members=1 pwset=1"))

    def test_existing_group_is_reused(self):
        pre = self.ITIL + "DB.sys_user_group.push({sys_id:'g1', name:'SRE-Platform L2', active:'1'});"
        out = run(pre, configure(SCRIPT, dry=False))
        self.assertIn('Group "SRE-Platform L2" already exists (g1)', out); self.assertIn("groups=1", out)

    def test_resolution_codes_are_listed_in_sequence_order_filter_inactive(self):
        pre = self.ITIL + "DB.sys_choice.push({name:'incident', element:'close_code', inactive:'0', value:'Solved (Permanently)'});DB.sys_choice.push({name:'incident', element:'close_code', inactive:'1', value:'Retired code'});DB.sys_choice.push({name:'task', element:'close_code', inactive:'0', value:'Other table'});"
        out = run(pre, configure(SCRIPT, dry=True))
        self.assertIn("Solved (Permanently)", out); self.assertNotIn("Retired code", out); self.assertNotIn("Other table", out)

    def test_script_is_es5_only(self):
        for banned in ("=>", "let ", "const ", "`", "async ", "class "):
            self.assertNotIn(banned, re.sub(r"/\*.*?\*/|//[^\n]*", "", SCRIPT, flags=re.S), banned)


if __name__ == "__main__":
    unittest.main()
