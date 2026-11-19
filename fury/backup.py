"""Backup and restore of FURY's durable state, with integrity checks. Disaster recovery you can actually test.

    python -m fury.backup backup  <dest_dir>            # needs FURY_DB (and optionally FURY_AUDIT_LOG) in the environment
    python -m fury.backup verify  <backup_dir>          # check hashes and the audit hash chain, change nothing
    python -m fury.backup restore <backup_dir> <db_out> [audit_out] [--force]

What is protected: the SQLite state (run history, approval audit trail, remediation attempt counters) and the hash-chained audit log.
Secrets are NOT in the backup (they live in .env or the secrets manager and must be recovered from there).
Safety: SQLite's online backup API (safe while FURY runs); every file is SHA-256 hashed in manifest.json; the audit chain is
re-verified on backup and again before a restore; a restore refuses to overwrite an existing file without --force and never
partially writes (files go to a temp name first)."""
import hashlib, json, os, shutil, sqlite3, sys, time
from .audit import Audit

MANIFEST = "manifest.json"


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()


def backup(db_path, dest_dir, audit_path=None):
    """Write a consistent snapshot into dest_dir (created). Returns the manifest dict."""
    if not db_path or not os.path.exists(db_path): raise FileNotFoundError("FURY_DB does not exist; nothing to back up")
    os.makedirs(dest_dir, exist_ok=True)
    files = {}
    src = sqlite3.connect(db_path); dst = sqlite3.connect(os.path.join(dest_dir, "fury.db"))
    try: src.backup(dst)                                   # consistent even while FURY is writing
    finally: dst.close(); src.close()
    files["fury.db"] = _sha(os.path.join(dest_dir, "fury.db"))
    head = None
    if audit_path and os.path.exists(audit_path):
        ok, n, bad = Audit.verify_file(audit_path)
        if not ok: raise ValueError(f"audit log fails verification at entry {bad}; refusing to back up a tampered chain")
        shutil.copyfile(audit_path, os.path.join(dest_dir, "audit.log")); files["audit.log"] = _sha(os.path.join(dest_dir, "audit.log"))
        head = Audit(audit_path).head
    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files, "audit_head": head}
    with open(os.path.join(dest_dir, MANIFEST), "w", encoding="utf-8") as f: json.dump(manifest, f, indent=2)
    return manifest


def verify(backup_dir):
    """Return (ok, problems[])."""
    problems = []
    try: manifest = json.load(open(os.path.join(backup_dir, MANIFEST), encoding="utf-8"))
    except (OSError, ValueError): return False, ["manifest.json missing or unreadable"]
    for name, want in manifest.get("files", {}).items():
        p = os.path.join(backup_dir, name)
        if not os.path.exists(p): problems.append(f"{name} is missing")
        elif _sha(p) != want: problems.append(f"{name} was modified after the backup (hash mismatch)")
    if "audit.log" in manifest.get("files", {}) and not problems:
        ok, n, bad = Audit.verify_file(os.path.join(backup_dir, "audit.log"))
        if not ok: problems.append(f"audit chain broken at entry {bad}")
        elif Audit(os.path.join(backup_dir, "audit.log")).head != manifest.get("audit_head"): problems.append("audit head does not match the manifest")
    if "fury.db" in manifest.get("files", {}) and not problems:
        try:
            c = sqlite3.connect(os.path.join(backup_dir, "fury.db")); res = c.execute("PRAGMA integrity_check").fetchone()[0]; c.close()
            if res != "ok": problems.append(f"sqlite integrity_check: {res}")
        except sqlite3.Error as e: problems.append(f"fury.db unreadable: {type(e).__name__}")
    return not problems, problems


def restore(backup_dir, db_out, audit_out=None, force=False):
    """Verify, then restore. Returns the list of files written. Raises ValueError on a bad backup, FileExistsError without force."""
    ok, problems = verify(backup_dir)
    if not ok: raise ValueError("backup failed verification: " + "; ".join(problems))
    targets = [(os.path.join(backup_dir, "fury.db"), db_out)]
    if audit_out and os.path.exists(os.path.join(backup_dir, "audit.log")): targets.append((os.path.join(backup_dir, "audit.log"), audit_out))
    for _, out in targets:
        if os.path.exists(out) and not force: raise FileExistsError(f"{out} exists; pass --force to overwrite")
    written = []
    for src, out in targets:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        tmp = out + ".restoring"; shutil.copyfile(src, tmp); os.replace(tmp, out); written.append(out)
    return written


def main(argv):
    force = "--force" in argv; args = [a for a in argv if a != "--force"]
    try:
        if len(args) >= 2 and args[0] == "backup":
            m = backup(os.environ.get("FURY_DB"), args[1], os.environ.get("FURY_AUDIT_LOG")); print("backed up:", ", ".join(m["files"]))
        elif len(args) == 2 and args[0] == "verify":
            ok, pr = verify(args[1]); print("OK: backup verified" if ok else "FAILED: " + "; ".join(pr)); return 0 if ok else 1
        elif len(args) >= 3 and args[0] == "restore":
            print("restored:", ", ".join(restore(args[1], args[2], args[3] if len(args) > 3 else None, force)))
        else:
            print(__doc__); return 2
    except (ValueError, FileNotFoundError, FileExistsError) as e:
        print("ERROR:", e); return 1
    return 0


if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
