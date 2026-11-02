"""The HTTP API calls the checker. Company routes require the login token."""

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import tenant, tenant_repository
from services.api import app
from services.contract_check import submit_policies


class TestContractApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(
            os.environ,
            {
                "SQLITE_DB_PATH": os.path.join(self.tmp.name, "tenants.db"),
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

    def _token(self) -> str:
        response = self.client.post(
            "/v1/register",
            json={
                "org_id": "nexify",
                "login_name": "nexify",
                "password": "correct-horse",
                "name": "Nexify",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["org_id"], "nexify")
        return response.json()["token"]

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertTrue(response.headers["x-served-by"])

    def test_dashboard_is_served_by_the_api(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Register", response.text)
        self.assertIn("Upload a company policy", response.text)
        self.assertIn("/v1/login", response.text)

    def test_blank_company_is_rejected_before_ingestion(self):
        with self.assertRaises(ValueError):
            submit_policies([("policy.pdf", b"%PDF")], "  ")

    def test_upload_without_a_token_is_rejected(self):
        response = self.client.post(
            "/v1/policies",
            files={"files": ("policy.pdf", b"%PDF-1.4", "application/pdf")},
        )
        self.assertEqual(response.status_code, 401)

    def test_policy_upload_uses_the_company_from_the_token(self):
        token = self._token()
        with patch("services.contract_check.process_uploaded_files") as index:
            response = self.client.post(
                "/v1/policies",
                headers={"Authorization": f"Bearer {token}"},
                files={"files": ("policy.pdf", b"%PDF-1.4", "application/pdf")},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["org_id"], "nexify")
        self.assertEqual(body["status"], "queued")
        self.assertTrue(body["job_id"])
        self.assertTrue(body["object_keys"][0].startswith("nexify/policies/"))
        index.assert_not_called()

    def test_a_new_login_replaces_the_previous_token(self):
        first = self._token()
        second = self.client.post(
            "/v1/login",
            json={"login_name": "nexify", "password": "correct-horse"},
        )
        self.assertEqual(second.status_code, 200)
        with patch("services.contract_check.process_uploaded_files"):
            rejected = self.client.post(
                "/v1/policies",
                headers={"Authorization": f"Bearer {first}"},
                files={"files": ("policy.pdf", b"%PDF-1.4", "application/pdf")},
            )
            accepted = self.client.post(
                "/v1/policies",
                headers={"Authorization": f"Bearer {second.json()['token']}"},
                files={"files": ("policy.pdf", b"%PDF-1.4", "application/pdf")},
            )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_policy_upload_rejects_a_non_pdf(self):
        token = self._token()
        response = self.client.post(
            "/v1/policies",
            headers={"Authorization": f"Bearer {token}"},
            files={"files": ("notes.txt", b"hello", "text/plain")},
        )
        self.assertEqual(response.status_code, 400)

    def test_contract_upload_returns_a_job_and_does_not_check_it(self):
        token = self._token()
        with patch("services.contract_check.contract_graph.invoke") as invoke:
            response = self.client.post(
                "/v1/checks",
                headers={"Authorization": f"Bearer {token}"},
                files={"contract": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["org_id"], "nexify")
        self.assertTrue(body["object_key"].startswith("nexify/contracts/"))
        invoke.assert_not_called()

    def test_empty_contract_is_rejected(self):
        token = self._token()
        response = self.client.post(
            "/v1/checks",
            headers={"Authorization": f"Bearer {token}"},
            files={"contract": ("contract.pdf", b"", "application/pdf")},
        )
        self.assertEqual(response.status_code, 400)

    def test_single_clause_does_not_invent_a_missing_check(self):
        token = self._token()
        report = {
            "org_id": "nexify",
            "present_clauses": [{"compliant": False}],
            "missing_clauses": [],
            "missing_clause_check": "not_run",
        }
        with patch("services.api.check_clause", return_value=report):
            response = self.client.post(
                "/v1/checks/clause",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "clause_text": "Payment is due in 90 days.",
                    "clause_type": "Payment",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["missing_clause_check"], "not_run")

    def test_clause_requires_text(self):
        token = self._token()
        with patch(
            "services.api.check_clause",
            side_effect=ValueError("clause_text and clause_type are required"),
        ):
            response = self.client.post(
                "/v1/checks/clause",
                headers={"Authorization": f"Bearer {token}"},
                json={"clause_text": "   ", "clause_type": "Payment"},
            )
        self.assertEqual(response.status_code, 400)
