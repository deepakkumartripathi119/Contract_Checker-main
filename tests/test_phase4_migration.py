"""Dry-run checks for the SQLite backup. No Postgres connection and no source writes."""

import hashlib
import unittest
from pathlib import Path

from scripts.migrate_sqlite_to_postgres import decode_embedding, inspect_sqlite


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "data" / "contract_checker.phase3.bak"


class Phase4MigrationTests(unittest.TestCase):
    def test_backup_matches_the_written_mapping(self):
        if not BACKUP.exists():
            self.skipTest("Phase 3 backup is not in this workspace.")
        before = hashlib.sha256(BACKUP.read_bytes()).hexdigest()
        report = inspect_sqlite(BACKUP)
        after = hashlib.sha256(BACKUP.read_bytes()).hexdigest()

        self.assertEqual(before, after)
        self.assertEqual(report["source_sha256"], before)
        self.assertEqual(report["chunk_count"], 17)
        self.assertEqual(report["parent_count"], 5)
        self.assertEqual(report["child_count"], 12)
        self.assertEqual(report["child_embedding_count"], 12)
        self.assertEqual(report["embedding_dim"], 1024)
        self.assertEqual(report["orphan_count"], 0)
        self.assertEqual(report["cache_rows_not_migrated"], 5)
        self.assertEqual(len(report["tenants"]), 1)
        tenant = report["tenants"][0]
        self.assertEqual(tenant["org_id"], "nexify")
        self.assertEqual(tenant["active_policy_version"], "v1.3")
        self.assertEqual(tenant["silent_policy_behavior"], "compliant")
        self.assertEqual(tenant["favorable_deviation_behavior"], "allow_favorable")
        self.assertEqual(tenant["aliases"], [])
        self.assertEqual(tenant["supported_languages"], ["en"])

    def test_child_embedding_must_be_1024_floats(self):
        with self.assertRaises(ValueError):
            decode_embedding(b"\x00" * 8, "child")

    def test_parent_may_omit_embedding(self):
        self.assertIsNone(decode_embedding(None, "parent"))


if __name__ == "__main__":
    unittest.main()
