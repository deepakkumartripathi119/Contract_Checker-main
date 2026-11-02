"""The original PDF is stored by key. The database row does not hold the bytes."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.contract_check import ingest_policies


class TestOriginalFileStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "SQLITE_DB_PATH": os.path.join(self.tmp.name, "docs.db"),
                "OBJECT_STORE_BACKEND": "local",
                "OBJECT_STORE_PATH": os.path.join(self.tmp.name, "objects"),
                "VECTOR_STORE_BACKEND": "sqlite",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_policy_bytes_are_stored_outside_the_row(self):
        pdf = b"%PDF-1.4 original-bytes"
        with patch("services.contract_check.process_uploaded_files", return_value=["pipe-1"]):
            result = ingest_policies([("policy.pdf", pdf)], "acme")

        self.assertEqual(result["document_ids"], ["pipe-1"])
        key = result["object_keys"][0]
        self.assertTrue(key.startswith("acme/policies/"))
        with open(os.path.join(os.environ["OBJECT_STORE_PATH"], *key.split("/")), "rb") as handle:
            stored = handle.read()
        self.assertEqual(stored, pdf)

        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        row = connection.execute(
            "SELECT object_key, filename FROM stored_documents WHERE org_id = ?",
            ("acme",),
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], key)
        self.assertEqual(row[1], "policy.pdf")
        self.assertNotIn(pdf, str(row).encode())

    def test_prepare_creates_the_folder_and_the_key_table(self):
        from scripts.prepare_runtime import prepare

        prepare()
        self.assertTrue(os.path.isdir(os.environ["OBJECT_STORE_PATH"]))
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        connection.close()
        self.assertIn("stored_documents", names)
        self.assertIn("jobs", names)
