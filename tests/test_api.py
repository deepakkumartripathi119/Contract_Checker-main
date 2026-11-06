"""The HTTP API calls the checker. Company routes require the login token."""

import json
import os
import tempfile
import unittest
from pathlib import Path
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

    def test_register_stores_the_company_details_from_the_picture(self):
        response = self.client.post(
            "/v1/register",
            json={
                "name": "Northwind Goods",
                "business_email": "owner@northwind.example",
                "industry": "Wholesale",
                "company_size": "11–50",
                "headquarters": "Pune",
                "website": "https://northwind.example",
                "password": "correct-horse",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Northwind Goods")
        tenant._IN_MEMORY_TENANTS.clear()
        import sqlite3
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT name, business_email, industry, company_size, headquarters, website, login_name
            FROM tenant_configs WHERE org_id = ?
            """,
            ("northwind-goods",),
        ).fetchone()
        connection.close()
        self.assertEqual(row["name"], "Northwind Goods")
        self.assertEqual(row["business_email"], "owner@northwind.example")
        self.assertEqual(row["industry"], "Wholesale")
        self.assertEqual(row["company_size"], "11–50")
        self.assertEqual(row["headquarters"], "Pune")
        self.assertEqual(row["website"], "https://northwind.example")
        self.assertEqual(row["login_name"], "owner@northwind.example")
        signed_in = self.client.post(
            "/v1/login",
            json={"login_name": "owner@northwind.example", "password": "correct-horse"},
        )
        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(signed_in.json()["org_id"], "northwind-goods")
        headers = {"Authorization": "Bearer " + signed_in.json()["token"]}
        profile = self.client.get("/v1/company", headers=headers)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["name"], "Northwind Goods")
        self.assertEqual(profile.json()["industry"], "Wholesale")
        self.assertEqual(profile.json()["company_size"], "11–50")
        self.assertNotIn("password_hash", profile.json())
        rejected = self.client.patch(
            "/v1/company",
            headers=headers,
            json={"current_password": "wrong-password", "new_password": "another-horse"},
        )
        self.assertEqual(rejected.status_code, 400)
        changed = self.client.patch(
            "/v1/company",
            headers=headers,
            json={
                "current_password": "correct-horse",
                "business_email": "new@northwind.example",
                "new_password": "another-horse",
            },
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()["token"])
        self.assertEqual(changed.json()["name"], "Northwind Goods")
        old_login = self.client.post(
            "/v1/login",
            json={"login_name": "owner@northwind.example", "password": "correct-horse"},
        )
        self.assertEqual(old_login.status_code, 401)
        new_login = self.client.post(
            "/v1/login",
            json={"login_name": "new@northwind.example", "password": "another-horse"},
        )
        self.assertEqual(new_login.status_code, 200)
        kept = self.client.get(
            "/v1/company",
            headers={"Authorization": "Bearer " + new_login.json()["token"]},
        )
        self.assertEqual(kept.json()["name"], "Northwind Goods")
        self.assertEqual(kept.json()["industry"], "Wholesale")
        self.assertEqual(kept.json()["headquarters"], "Pune")
        self.assertEqual(kept.json()["website"], "https://northwind.example")
        self.assertEqual(kept.json()["business_email"], "new@northwind.example")
        rejected = self.client.post(
            "/v1/register",
            json={
                "name": "Other Goods",
                "business_email": "owner@other.example",
                "company_size": "huge",
                "password": "correct-horse",
            },
        )
        self.assertEqual(rejected.status_code, 400)

    def test_checked_in_openapi_matches_the_running_api(self):
        spec = json.loads(
            Path(__file__).resolve().parents[1].joinpath("services", "openapi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(spec["paths"]), set(app.openapi()["paths"]))

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertTrue(response.headers["x-served-by"])

    def test_dashboard_is_served_by_the_api(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Register", response.text)
        self.assertIn("Company policy", response.text)
        self.assertIn("data-step=\"extracting\"", response.text)
        self.assertIn("showJobSteps", response.text)
        self.assertIn("Your session has ended. Sign in again.", response.text)
        self.assertIn("Delete report", response.text)
        self.assertIn("Needs a person to review", response.text)
        self.assertIn("reader_label", response.text)
        self.assertNotIn("Ask for a job", response.text)
        self.assertIn("Add Vendor", response.text)
        self.assertIn("loadVendors", response.text)
        self.assertIn("id=\"theme\"", response.text)
        self.assertIn("Company profile", response.text)
        self.assertIn("/v1/company", response.text)
        self.assertIn("rememberJob", response.text)
        self.assertNotIn("summary.sentence", response.text)
        self.assertIn("/v1/login", response.text)
        self.assertIn("We encountered an error.", response.text)
        self.assertNotIn("Policy indexed", response.text)
        self.assertIn("liveJob", response.text)
        self.assertIn("pending_status", response.text)
        self.assertIn("open_view", response.text)
        self.assertIn("id=\"policy-preview\"", response.text)
        self.assertIn("Find in the policy", response.text)
        self.assertIn("id=\"policy-page\"", response.text)
        self.assertIn("/v1/policies/preview", response.text)

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

    def test_work_requests_can_be_limited_and_a_status_poll_is_not(self):
        from core.company_budget import BudgetExceeded

        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        with patch("services.api.charge_company", side_effect=BudgetExceeded("full")):
            blocked = self.client.post(
                "/v1/checks",
                headers=headers,
                files={"contract": ("contract.pdf", b"%PDF-1.4", "application/pdf")},
            )
            polled = self.client.get("/v1/jobs/missing-job", headers=headers)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(polled.status_code, 404)

    def test_a_pdf_policy_preview_is_a_png_of_each_page(self):
        import pymupdf

        from core.jobs import POLICY_UPLOADED, SUCCEEDED, create_job
        from core.stored_files import keep_original_file

        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        document = pymupdf.open()
        first = document.new_page()
        first.insert_text((72, 100), "Alpha")
        second = document.new_page()
        second.insert_text((72, 200), "Beta")
        data = document.tobytes()
        document.close()
        stored = keep_original_file("nexify", "policies", "policy.pdf", data)
        create_job("nexify", POLICY_UPLOADED, [stored], status=SUCCEEDED, result={})
        info = self.client.get("/v1/policies/preview", headers=headers)
        self.assertEqual(info.status_code, 200)
        body = info.json()
        self.assertEqual(body["filename"], "policy.pdf")
        self.assertEqual(body["pages"], 2)
        beta = [word for word in body["words"] if word["t"] == "Beta"]
        self.assertEqual(len(beta), 1)
        self.assertEqual(beta[0]["page"], 2)
        self.assertGreater(beta[0]["y"], 0.15)
        self.assertLess(beta[0]["y"], 0.4)
        image = self.client.get("/v1/policies/preview/1", headers=headers)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        missing = self.client.get("/v1/policies/preview/3", headers=headers)
        self.assertEqual(missing.status_code, 404)
        other = self.client.post(
            "/v1/register",
            json={
                "org_id": "word-co",
                "login_name": "word-co",
                "password": "correct-horse",
                "name": "Word Co",
            },
        )
        self.assertEqual(other.status_code, 200)
        word = keep_original_file("word-co", "policies", "policy.docx", b"PK\x03\x04not-a-pdf")
        create_job("word-co", POLICY_UPLOADED, [word], status=SUCCEEDED, result={})
        refused = self.client.get(
            "/v1/policies/preview",
            headers={"Authorization": f"Bearer {other.json()['token']}"},
        )
        self.assertEqual(refused.status_code, 400)
