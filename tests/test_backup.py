import os, shutil, tempfile, unittest
from fury import backup as B
from fury.audit import Audit
from fury.store import Store


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db, self.log = os.path.join(self.d, "fury.db"), os.path.join(self.d, "audit.log")
        s = Store(self.db)
        for i in range(3): s.save_run({"id": f"r{i}", "at": "10:00:00", "state": "done", "label": f"run {i}", "source": "chat", "out": {"outcome": "resolved", "ticket": f"INC{i}"}})
        s._db.close()
        a = Audit(self.log)
        for i in range(4): a.record("tester", "unit.test", "ok", n=i)
        self.dest = os.path.join(self.d, "bk")

    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)

    def test_round_trip_restores_runs_and_audit_chain_into_a_fresh_location(self):
        B.backup(self.db, self.dest, self.log)
        self.assertEqual(B.verify(self.dest), (True, []))
        out_db, out_log = os.path.join(self.d, "new", "fury.db"), os.path.join(self.d, "new", "audit.log")
        B.restore(self.dest, out_db, out_log)
        runs = Store(out_db).load_runs()
        self.assertEqual(sorted(runs), ["r0", "r1", "r2"])
        self.assertEqual(Audit.verify_file(out_log)[:2], (True, 4))
        self.assertEqual(Audit(out_log).head, Audit(self.log).head)

    def test_tampered_database_is_detected_and_not_restored(self):
        B.backup(self.db, self.dest, self.log)
        with open(os.path.join(self.dest, "fury.db"), "ab") as f: f.write(b"x")
        ok, problems = B.verify(self.dest)
        self.assertFalse(ok); self.assertIn("hash mismatch", problems[0])
        with self.assertRaises(ValueError): B.restore(self.dest, os.path.join(self.d, "out.db"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "out.db")))

    def test_tampered_audit_log_is_detected_at_backup_time(self):
        lines = open(self.log, encoding="utf-8").read().splitlines()
        open(self.log, "w", encoding="utf-8").write("\n".join([lines[0].replace("tester", "mallory")] + lines[1:]) + "\n")
        with self.assertRaises(ValueError): B.backup(self.db, self.dest, self.log)

    def test_restore_refuses_to_overwrite_without_force(self):
        B.backup(self.db, self.dest, self.log)
        with self.assertRaises(FileExistsError): B.restore(self.dest, self.db)
        B.restore(self.dest, self.db, force=True)

    def test_missing_manifest_or_file_fails_verification(self):
        self.assertFalse(B.verify(os.path.join(self.d, "nope"))[0])
        B.backup(self.db, self.dest, self.log); os.remove(os.path.join(self.dest, "audit.log"))
        self.assertIn("audit.log is missing", B.verify(self.dest)[1])


if __name__ == "__main__": unittest.main()
