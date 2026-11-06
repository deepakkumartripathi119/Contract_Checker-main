"""A job is accepted before the checker runs. The worker fills in the report."""

import os
import sqlite3
import tempfile
import threading
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

        seen = {}

        def parse_and_read_step(*_args, **_kwargs):
            connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
            row = connection.execute(
                "SELECT step FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.close()
            seen["during_extract"] = row[0]
            return ([{"text": "Pay in 30 days.", "clause_type": "Payment"}], False)

        with patch(
            "services.contract_check.parse_vendor_contract",
            side_effect=parse_and_read_step,
        ), patch(
            "services.contract_check.contract_graph.invoke",
            return_value={
                "answer": [{"clause_title": "Payment", "compliant": True}],
                "missing_clauses": [],
            },
        ):
            self.assertTrue(run_once())
        self.assertEqual(seen["during_extract"], "extracting")
        self.assertFalse(run_once())

        report = self.client.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(report.status_code, 200)
        body = report.json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["step"], "completed")
        self.assertEqual(body["result"]["present_clauses"][0]["clause_title"], "Payment")
        self.assertEqual(body["result"]["summary"]["matches_the_policy"], 1)
        self.assertEqual(body["result"]["summary"]["sentence"], "1 matches the policy.")

    def test_analysing_and_matching_are_written_when_those_nodes_start(self):
        from pipeline.graph import _analyse, _match

        with patch("pipeline.graph.set_job_step") as record, patch(
            "pipeline.graph.clause_sender_node",
            return_value={},
        ), patch(
            "pipeline.graph.missing_clause_checker",
            return_value={},
        ):
            _analyse({"job_id": "job-1"})
            _match({"job_id": "job-1"})
            _analyse({})
        self.assertEqual(
            [call.args for call in record.call_args_list],
            [("job-1", "analysing"), ("job-1", "matching")],
        )

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

    def test_a_company_sees_its_contract_files_and_not_another_companys(self):
        owner = self._token()
        self.client.post(
            "/v1/checks",
            headers={"Authorization": f"Bearer {owner}"},
            files={"contract": ("vendor_a.pdf", b"%PDF-a", "application/pdf")},
        )
        self.client.post(
            "/v1/checks",
            headers={"Authorization": f"Bearer {owner}"},
            files={"contract": ("vendor_b.pdf", b"%PDF-b", "application/pdf")},
        )
        self.client.post(
            "/v1/policies",
            headers={"Authorization": f"Bearer {owner}"},
            files={"files": ("policy.pdf", b"%PDF-policy", "application/pdf")},
        )
        listed = self.client.get("/v1/jobs", headers={"Authorization": f"Bearer {owner}"})
        self.assertEqual(listed.status_code, 200)
        names = [item["filename"] for item in listed.json()["jobs"]]
        self.assertEqual(set(names), {"vendor_b.pdf", "vendor_a.pdf"})
        self.assertEqual(len(names), 2)
        self.assertTrue(all(item["job_id"] for item in listed.json()["jobs"]))
        self.assertNotIn("policy.pdf", names)

        other = self._token("other-co", "other-login")
        foreign = self.client.get("/v1/jobs", headers={"Authorization": f"Bearer {other}"})
        self.assertEqual(foreign.json()["jobs"], [])

    def test_two_vendors_keep_the_same_contract_as_two_checks(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        first = self.client.post("/v1/vendors", headers=headers, json={"name": "A1"})
        second = self.client.post("/v1/vendors", headers=headers, json={"name": "A2"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        a1 = first.json()["vendor_id"]
        a2 = second.json()["vendor_id"]
        same = b"%PDF-same-bytes"
        left = self.client.post(
            f"/v1/vendors/{a1}/contract",
            headers=headers,
            files={"contract": ("vendor_a.pdf", same, "application/pdf")},
        )
        right = self.client.post(
            f"/v1/vendors/{a2}/contract",
            headers=headers,
            files={"contract": ("vendor_a.pdf", same, "application/pdf")},
        )
        self.assertEqual(left.status_code, 200)
        self.assertEqual(right.status_code, 200)
        self.assertNotEqual(left.json()["job_id"], right.json()["job_id"])
        self.assertNotIn("existing", left.json())
        again = self.client.post(
            f"/v1/vendors/{a1}/contract",
            headers=headers,
            files={"contract": ("renamed.pdf", same, "application/pdf")},
        )
        self.assertTrue(again.json()["existing"])
        self.assertEqual(again.json()["job_id"], left.json()["job_id"])
        from core.jobs import mark_succeeded
        mark_succeeded(left.json()["job_id"], {"present_clauses": [], "missing_clauses": []})
        replaced = self.client.post(
            f"/v1/vendors/{a1}/contract",
            headers=headers,
            files={"contract": ("new.pdf", b"%PDF-different", "application/pdf")},
        )
        self.assertNotEqual(replaced.json()["job_id"], left.json()["job_id"])
        self.assertNotIn("existing", replaced.json())
        listed = self.client.get("/v1/vendors", headers=headers)
        rows = {item["name"]: item for item in listed.json()["vendors"]}
        self.assertEqual(rows["A1"]["filename"], "vendor_a.pdf")
        self.assertTrue(rows["A1"]["updated_at"])
        self.assertEqual(rows["A1"]["pending_job_id"], replaced.json()["job_id"])
        self.assertEqual(rows["A2"]["filename"], "vendor_a.pdf")
        mark_succeeded(replaced.json()["job_id"], {"present_clauses": [], "missing_clauses": []})
        finished = self.client.get("/v1/vendors", headers=headers)
        after = {item["name"]: item for item in finished.json()["vendors"]}
        self.assertEqual(after["A1"]["filename"], "new.pdf")
        self.assertEqual(after["A1"]["job_id"], replaced.json()["job_id"])
        self.assertEqual(self.client.get(f"/v1/jobs/{left.json()['job_id']}", headers=headers).status_code, 404)
        other = self._token("other-co", "other-login")
        hidden = self.client.get("/v1/vendors", headers={"Authorization": f"Bearer {other}"})
        self.assertEqual(hidden.json()["vendors"], [])

    def test_hiding_a_vendor_keeps_the_job_and_deleting_a_report_removes_it(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        created = self.client.post("/v1/vendors", headers=headers, json={"name": "North"})
        vendor_id = created.json()["vendor_id"]
        uploaded = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-report", "application/pdf")},
        )
        job_id = uploaded.json()["job_id"]
        from core.jobs import mark_succeeded

        mark_succeeded(job_id, {"present_clauses": [], "missing_clauses": []})
        removed = self.client.delete(f"/v1/vendors/{vendor_id}/report", headers=headers)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get(f"/v1/jobs/{job_id}", headers=headers).status_code, 404)
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertIsNone(listed[0]["job_id"])
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        status = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        self.assertEqual(status[0], "deleting")
        mark_succeeded(job_id, {"present_clauses": [{"clause_title": "late"}]})
        self.assertEqual(self.client.get(f"/v1/jobs/{job_id}", headers=headers).status_code, 404)
        again = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-report", "application/pdf")},
        )
        self.assertNotIn("existing", again.json())
        self.assertNotEqual(again.json()["job_id"], job_id)
        from scripts.worker import run_once

        self.assertTrue(run_once())
        gone = connection.execute(
            "SELECT job_id FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        self.assertIsNone(gone)
        self.assertEqual(self.client.delete(f"/v1/vendors/{vendor_id}", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/vendors", headers=headers).json()["vendors"], [])
        self.assertEqual(self.client.get(f"/v1/jobs/{job_id}", headers=headers).status_code, 404)

    def test_a_crash_during_delete_puts_the_vendor_and_report_back(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Crash"}).json()["vendor_id"]
        uploaded = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-crash", "application/pdf")},
        )
        job_id = uploaded.json()["job_id"]
        from core.jobs import mark_succeeded
        from core.vendors import clear_vendor_report, hide_vendor

        mark_succeeded(job_id, {"present_clauses": [], "missing_clauses": []})
        with patch("core.jobs._vendor_change", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                clear_vendor_report("nexify", vendor_id)
        self.assertEqual(self.client.get(f"/v1/jobs/{job_id}", headers=headers).status_code, 200)
        kept = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertEqual(kept[0]["job_id"], job_id)
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        self.assertIsNone(connection.execute(
            "SELECT job_id FROM jobs WHERE kind = 'report.deleted'",
        ).fetchone())
        with patch("core.jobs._vendor_change", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                hide_vendor("nexify", vendor_id)
        self.assertEqual(self.client.get("/v1/vendors", headers=headers).json()["vendors"][0]["vendor_id"], vendor_id)
        self.assertEqual(self.client.get(f"/v1/jobs/{job_id}", headers=headers).status_code, 200)
        self.assertIsNone(connection.execute(
            "SELECT job_id FROM jobs WHERE status = 'deleting' OR kind = 'vendor.deleted'",
        ).fetchone())
        connection.close()

    def test_a_crashed_worker_finishes_the_delete_on_the_next_claim(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Retry"}).json()["vendor_id"]
        uploaded = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-retry", "application/pdf")},
        )
        job_id = uploaded.json()["job_id"]
        from core.jobs import mark_succeeded
        from services.contract_check import run_job

        mark_succeeded(job_id, {"present_clauses": [], "missing_clauses": []})
        removed = self.client.delete(f"/v1/vendors/{vendor_id}/report", headers=headers)
        self.assertEqual(removed.status_code, 200)
        claimed = claim_next()
        self.assertEqual(claimed["kind"], "report.deleted")
        run_job(claimed)
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        connection.execute(
            "UPDATE jobs SET updated_at = '2000-01-01 00:00:00' WHERE job_id = ?",
            (claimed["job_id"],),
        )
        connection.commit()
        again = claim_next()
        self.assertEqual(again["job_id"], claimed["job_id"])
        mark_succeeded(again["job_id"], run_job(again))
        self.assertIsNone(connection.execute(
            "SELECT job_id FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone())
        self.assertEqual(connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (claimed["job_id"],),
        ).fetchone()[0], "succeeded")
        connection.close()
        self.assertIsNone(self.client.get("/v1/vendors", headers=headers).json()["vendors"][0]["job_id"])

    def test_the_same_contract_is_checked_again_after_the_policy_changes(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        created = self.client.post("/v1/vendors", headers=headers, json={"name": "A"})
        vendor_id = created.json()["vendor_id"]
        same = b"%PDF-same-after-policy"
        first = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", same, "application/pdf")},
        )
        from core.jobs import mark_succeeded
        from core.tenant import _IN_MEMORY_TENANTS, bump_tenant_policy_version, get_tenant_config

        version = get_tenant_config("nexify").active_policy_version
        mark_succeeded(first.json()["job_id"], {
            "present_clauses": [],
            "missing_clauses": [],
            "checked_policy_version": version,
        })
        again = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", same, "application/pdf")},
        )
        self.assertTrue(again.json()["existing"])
        self.assertEqual(again.json()["job_id"], first.json()["job_id"])
        bumped = bump_tenant_policy_version("nexify")
        self.assertNotEqual(bumped, version)
        _IN_MEMORY_TENANTS["nexify"].active_policy_version = version
        fresh = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", same, "application/pdf")},
        )
        self.assertEqual(fresh.status_code, 200)
        self.assertNotIn("existing", fresh.json())
        self.assertNotEqual(fresh.json()["job_id"], first.json()["job_id"])
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertEqual(listed[0]["job_id"], first.json()["job_id"])
        self.assertEqual(listed[0]["pending_job_id"], fresh.json()["job_id"])
        self.assertEqual(listed[0]["pending_status"], "queued")

    def test_a_finished_report_without_a_policy_version_is_not_reused(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Old"}).json()["vendor_id"]
        same = b"%PDF-unversioned"
        first = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", same, "application/pdf")},
        )
        from core.jobs import mark_succeeded

        mark_succeeded(first.json()["job_id"], {"present_clauses": [], "missing_clauses": []})
        again = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", same, "application/pdf")},
        )
        self.assertEqual(again.status_code, 200)
        self.assertNotIn("existing", again.json())
        self.assertNotEqual(again.json()["job_id"], first.json()["job_id"])

    def test_a_clause_is_rejected_while_a_contract_is_still_open(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Open"}).json()["vendor_id"]
        uploaded = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-open", "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 200)
        clause = self.client.post(
            f"/v1/vendors/{vendor_id}/clause",
            headers=headers,
            json={"clause_text": "Pay in 30 days.", "clause_type": "Payment"},
        )
        self.assertEqual(clause.status_code, 400)
        self.assertIn("already has a submission", clause.json()["detail"])
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertEqual(listed[0]["input_mode"], "contract")

    def test_a_contract_is_rejected_while_a_clause_is_still_open(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Clause"}).json()["vendor_id"]
        from core.vendors import claim_vendor_mode

        self.assertTrue(claim_vendor_mode("nexify", vendor_id, "clause"))
        uploaded = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-clause-open", "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 400)
        self.assertIn("already has a submission", uploaded.json()["detail"])
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertEqual(listed[0]["input_mode"], "clause")

    def test_two_overlapping_first_claims_store_one_mode(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Race"}).json()["vendor_id"]
        from core.vendors import claim_vendor_mode

        barrier = threading.Barrier(2)
        wins = []

        def claim(mode):
            barrier.wait()
            wins.append(claim_vendor_mode("nexify", vendor_id, mode))

        threads = [
            threading.Thread(target=claim, args=("contract",)),
            threading.Thread(target=claim, args=("clause",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(wins.count(True), 1)
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertIn(listed[0]["input_mode"], {"contract", "clause"})

    def test_a_clause_that_fails_before_a_report_clears_the_mode(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Retry"}).json()["vendor_id"]
        with patch(
            "services.contract_check.check_clause",
            side_effect=ValueError("policy index could not be restored"),
        ):
            failed = self.client.post(
                f"/v1/vendors/{vendor_id}/clause",
                headers=headers,
                json={"clause_text": "Pay in 30 days.", "clause_type": "Payment"},
            )
        self.assertEqual(failed.status_code, 400)
        uploaded = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-after-failed-clause", "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 200)
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertEqual(listed[0]["input_mode"], "contract")

    def test_a_crash_while_adopting_a_replacement_keeps_both_reports(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Adopt"}).json()["vendor_id"]
        first = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("first.pdf", b"%PDF-first", "application/pdf")},
        )
        from core.jobs import _vendor_change, mark_succeeded

        mark_succeeded(first.json()["job_id"], {"present_clauses": [], "missing_clauses": []})
        second = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("second.pdf", b"%PDF-second", "application/pdf")},
        )
        real_change = _vendor_change

        def crash_on_adopt(*args, **kwargs):
            change = kwargs.get("change", args[1] if len(args) > 1 else "")
            if change == "adopt":
                raise RuntimeError("crash")
            return real_change(*args, **kwargs)

        with patch("core.jobs._vendor_change", side_effect=crash_on_adopt):
            with self.assertRaises(RuntimeError):
                mark_succeeded(second.json()["job_id"], {"present_clauses": [], "missing_clauses": []})
        listed = self.client.get("/v1/vendors", headers=headers).json()["vendors"]
        self.assertEqual(listed[0]["job_id"], first.json()["job_id"])
        self.assertEqual(listed[0]["pending_job_id"], second.json()["job_id"])
        self.assertEqual(self.client.get(f"/v1/jobs/{first.json()['job_id']}", headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/jobs/{second.json()['job_id']}", headers=headers).status_code, 200)
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        self.assertIsNone(connection.execute(
            "SELECT job_id FROM jobs WHERE status = 'deleting' OR kind = 'report.deleted'",
        ).fetchone())
        connection.close()

    def test_a_rejected_contract_does_not_leave_a_job(self):
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"}
        vendor_id = self.client.post("/v1/vendors", headers=headers, json={"name": "Held"}).json()["vendor_id"]
        from core.vendors import claim_vendor_mode

        self.assertTrue(claim_vendor_mode("nexify", vendor_id, "clause"))
        rejected = self.client.post(
            f"/v1/vendors/{vendor_id}/contract",
            headers=headers,
            files={"contract": ("vendor.pdf", b"%PDF-held", "application/pdf")},
        )
        self.assertEqual(rejected.status_code, 400)
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        self.assertIsNone(connection.execute(
            "SELECT job_id FROM jobs WHERE kind = 'contract.submitted'",
        ).fetchone())
        connection.close()

    def test_a_policy_version_rolls_back_with_the_index(self):
        self._token()
        from core.sqlite_vector_store import SQLiteVectorStore
        from core.tenant import advance_policy_version, get_tenant_config
        from core.vector_store import Chunk

        store = SQLiteVectorStore(os.environ["SQLITE_DB_PATH"])
        store.add_chunks("nexify", [Chunk(
            id="c-initial",
            org_id="nexify",
            doc_id="doc-old",
            chunk_type="child",
            text="Initial policy content",
            embedding=[0.1] * 8,
        )])
        before = get_tenant_config("nexify").active_policy_version

        def crash(connection):
            advance_policy_version(connection, "nexify")
            raise RuntimeError("crash")

        with self.assertRaises(RuntimeError):
            store.replace_org_chunks("nexify", [Chunk(
                id="c-new",
                org_id="nexify",
                doc_id="doc-new",
                chunk_type="child",
                text="Replacement policy content",
                embedding=[0.2] * 8,
            )], alongside=crash)
        self.assertEqual(get_tenant_config("nexify").active_policy_version, before)
        self.assertEqual([chunk.id for chunk in store.export_org_chunks("nexify")], ["c-initial"])

    def test_the_stored_policy_version_replaces_the_memory_copy(self):
        self._token()
        from core.tenant import _IN_MEMORY_TENANTS, get_tenant_config
        from core.tenant_repository import get_tenant_repository

        current = get_tenant_config("nexify")
        saved = current.model_copy(update={"active_policy_version": "v9.9"})
        get_tenant_repository().save(saved)
        _IN_MEMORY_TENANTS["nexify"].active_policy_version = current.active_policy_version
        self.assertEqual(get_tenant_config("nexify").active_policy_version, "v9.9")
