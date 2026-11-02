"""A job is accepted before the checker runs. The worker fills in the report."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import tenant, tenant_repository
from core.jobs import claim_next
from scripts.worker import run_once
from services.api import app


class TestJobQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(
            os.environ,
            {
                "SQLITE_DB_PATH": os.path.join(self.tmp.name, "jobs.db"),
                "OBJECT_STORE_PATH": os.path.join(self.tmp.name, "objects"),
                "OBJECT_STORE_BACKEND": "local",
                "JWT_SECRET": "test-secret",
                "VECTOR_STORE_BACKEND": "sqlite",
            },
        )
        self.env.start()
        tenant._IN_MEMORY_TENANTS.clear()
        tenant_repository._MEM_CONN = None
        self.client = TestClient(app)

    def tearDown(self):
        tenant._IN_MEMORY_TENANTS.clear()
        tenant_repository._MEM_CONN = None
        self.env.stop()
        self.tmp.cleanup()

    def _token(self, org_id="nexify", login_name="nexify") -> str:
        response = self.client.post(
            "/v1/register",
            json={
                "org_id": org_id,
                "login_name": login_name,
                "password": "correct-horse",
                "name": org_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def test_worker_writes_the_contract_report(self):
        token = self._token()
        accepted = self.client.post(
            "/v1/checks",
            headers={"Authorization": f"Bearer {token}"},
            files={"contract": ("contract.pdf", b"%PDF-1.4 contract-bytes", "application/pdf")},
        )
        self.assertEqual(accepted.status_code, 200)
        job_id = accepted.json()["job_id"]

        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        stored = connection.execute("SELECT files FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        connection.close()
        self.assertNotIn(b"contract-bytes", stored[0].encode())

        with patch(
            "services.contract_check.parse_vendor_contract",
            return_value=([{"text": "Pay in 30 days.", "clause_type": "Payment"}], False),
        ), patch(
            "services.contract_check.contract_graph.invoke",
            return_value={
                "answer": [{"clause_title": "Payment", "compliant": True}],
                "missing_clauses": [],
            },
        ):
            self.assertTrue(run_once())
        self.assertFalse(run_once())

        report = self.client.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(report.status_code, 200)
        body = report.json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["result"]["present_clauses"][0]["clause_title"], "Payment")

    def test_a_running_job_is_not_claimed_twice(self):
        token = self._token()
        self.client.post(
            "/v1/checks",
            headers={"Authorization": f"Bearer {token}"},
            files={"contract": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
        )
        first = claim_next()
        second = claim_next()
        self.assertEqual(first["org_id"], "nexify")
        self.assertEqual(first["kind"], "contract.submitted")
        self.assertIsNone(second)

    def test_another_company_cannot_read_the_job(self):
        owner = self._token()
        accepted = self.client.post(
            "/v1/policies",
            headers={"Authorization": f"Bearer {owner}"},
            files={"files": ("policy.pdf", b"%PDF-1.4", "application/pdf")},
        )
        other = self._token("other-co", "other-co")
        hidden = self.client.get(
            f"/v1/jobs/{accepted.json()['job_id']}",
            headers={"Authorization": f"Bearer {other}"},
        )
        self.assertEqual(hidden.status_code, 404)

        with patch("services.contract_check.process_uploaded_files", return_value=["pipe-1"]):
            self.assertTrue(run_once())
        done = self.client.get(
            f"/v1/jobs/{accepted.json()['job_id']}",
            headers={"Authorization": f"Bearer {owner}"},
        )
        self.assertEqual(done.json()["result"]["document_ids"], ["pipe-1"])

    def test_a_failed_check_is_stored_on_the_job(self):
        token = self._token()
        accepted = self.client.post(
            "/v1/checks",
            headers={"Authorization": f"Bearer {token}"},
            files={"contract": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
        )
        with patch(
            "services.contract_check.parse_vendor_contract",
            side_effect=ValueError("Could not extract any content from the contract PDF"),
        ):
            self.assertTrue(run_once())
        report = self.client.get(
            f"/v1/jobs/{accepted.json()['job_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(report.json()["status"], "failed")
        self.assertIn("Could not extract", report.json()["error"])
