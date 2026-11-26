"""Checks for ocusage. Builds a fake OpenCode database in temp.

    python test_ocusage.py
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

import ocusage

SCHEMA = """
create table project (id text primary key, name text, worktree text);
create table session_v2 (
    id text primary key,
    project_id text,
    title text,
    directory text,
    model text,
    agent text,
    cost real,
    tokens_input integer,
    tokens_output integer,
    tokens_reasoning integer,
    tokens_cache_read integer,
    tokens_cache_write integer,
    time_created integer,
    time_updated integer
);
create table session_message (id text, session_id text, type text, data text);
"""


def build(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("insert into project values ('p1', 'alpha', 'C:/work/alpha')")
    con.execute("insert into project values ('p2', null, 'C:/work/beta')")

    now = datetime.now()
    old = now - timedelta(days=30)
    model = json.dumps({"id": "deepseek-v4", "providerID": "opencode"})

    rows = [
        # id, project, title, in, out, reason, cache_read, cache_write, cost, when
        ("s1", "p1", "first", 1000, 100, 0, 9000, 0, 0.0, now),
        ("s2", "p1", "second", 500, 50, 10, 4500, 0, 0.25, now - timedelta(days=1)),
        ("s3", "p2", "old one", 200, 20, 0, 1800, 0, 0.0, old),
    ]
    for sid, pid, title, tin, tout, treason, cread, cwrite, cost, when in rows:
        con.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, pid, title, "C:/work", model, "build", cost, tin, tout, treason,
             cread, cwrite, int(when.timestamp() * 1000), int(when.timestamp() * 1000)),
        )
    for i in range(3):
        con.execute("insert into session_message values (?,?,?,?)", ("m%d" % i, "s1", "assistant", "{}"))
    con.commit()
    con.close()


class Usage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ocusage-test-")
        self.db = os.path.join(self.tmp, "opencode.db")
        build(self.db)
        self.rows = ocusage.load(ocusage.connect(self.db))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loads_every_session(self):
        self.assertEqual(len(self.rows), 3)

    def test_token_total_includes_cache(self):
        s1 = [r for r in self.rows if r["id"] == "s1"][0]
        self.assertEqual(s1["total"], 1000 + 100 + 0 + 9000)

    def test_project_falls_back_to_folder_name(self):
        s3 = [r for r in self.rows if r["id"] == "s3"][0]
        self.assertEqual(s3["project"], "beta")

    def test_model_name(self):
        self.assertEqual(self.rows[0]["model"], "opencode/deepseek-v4")
        self.assertEqual(ocusage.model_name(None), "unknown")
        self.assertEqual(ocusage.model_name("garbage"), "garbage")

    def test_since_filters_old_sessions(self):
        cut = ocusage.cutoff("7d")
        recent = [r for r in self.rows if r["created"] >= cut]
        self.assertEqual(len(recent), 2)
        self.assertNotIn("s3", [r["id"] for r in recent])

    def test_group_by_project(self):
        groups = ocusage.group(self.rows, "project")
        self.assertEqual(groups[0]["name"], "alpha")
        self.assertEqual(groups[0]["sessions"], 2)
        self.assertEqual(sum(g["total"] for g in groups), sum(r["total"] for r in self.rows))

    def test_totals(self):
        t = ocusage.totals(self.rows)
        self.assertEqual(t["sessions"], 3)
        self.assertEqual(t["messages"], 3)
        self.assertAlmostEqual(t["cost"], 0.25)

    def test_messages_counted(self):
        s1 = [r for r in self.rows if r["id"] == "s1"][0]
        self.assertEqual(s1["messages"], 3)

    def test_formatting(self):
        self.assertEqual(ocusage.human(999), "999")
        self.assertEqual(ocusage.human(1500), "1.5 K")
        self.assertEqual(ocusage.human(2_400_000), "2.4 M")
        self.assertEqual(ocusage.pct(95, 100), "95%")
        self.assertEqual(ocusage.pct(0, 0), "0%")

    def test_formatting_rounds_up_a_unit(self):
        # 999999 must not print as "1000.0 K".
        self.assertEqual(ocusage.human(999_999), "1.0 M")
        self.assertEqual(ocusage.human(999_499), "999.5 K")
        self.assertEqual(ocusage.human(999_500_000), "1.0 B")


class BadInput(unittest.TestCase):
    def test_missing_file_says_so(self):
        with self.assertRaises(SystemExit) as ctx:
            ocusage.connect(os.path.join(tempfile.gettempdir(), "definitely-not-here.db"))
        self.assertIn("No OpenCode database", str(ctx.exception.code))

    def test_file_that_is_not_a_database(self):
        tmp = tempfile.mkdtemp(prefix="ocusage-bad-")
        try:
            path = os.path.join(tmp, "junk.db")
            with open(path, "wb") as fh:
                fh.write(b"this is not sqlite" * 100)
            with self.assertRaises(SystemExit) as ctx:
                ocusage.connect(path)
            self.assertIn("not a readable OpenCode database", str(ctx.exception.code))
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_database_without_the_usage_table(self):
        tmp = tempfile.mkdtemp(prefix="ocusage-old-")
        try:
            path = os.path.join(tmp, "old.db")
            con = sqlite3.connect(path)
            con.execute("create table session (id text)")
            con.commit()
            con.close()
            with self.assertRaises(SystemExit) as ctx:
                ocusage.connect(path)
            self.assertIn("not a readable OpenCode database", str(ctx.exception.code))
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
