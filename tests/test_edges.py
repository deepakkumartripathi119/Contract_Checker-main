"""Strict edges for login, jobs, stored files, and the Redis verdict cache."""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fakeredis
from fastapi.testclient import TestClient

from core import tenant, tenant_repository
from core.jobs import claim_next, create_job, get_job
from core.object_store import LocalObjectStore
from core.redis_cache import RedisCache
from scripts.worker import run_once
from services.api import app


def _postgres_is_up() -> bool:
    try:
        import psycopg

        connection = psycopg.connect(
            "host=127.0.0.1 port=5432 dbname=contract_checker user=contract password=contract",
            connect_timeout=2,
        )
        connection.close()
        return True
    except Exception:
        return False


class TestLoginEdges(unittest.TestCase):
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

    def _register(self, org_id="nexify", login_name="nexify", password="correct-horse"):
        return self.client.post(
            "/v1/register",
            json={"org_id": org_id, "login_name": login_name, "password": password, "name": org_id},
        )

    def test_short_blank_and_missing_secret_are_rejected(self):
        short = self._register(password="short")
        self.assertEqual(short.status_code, 422)

        blank = self._register(org_id="  ", login_name="  ")
        self.assertEqual(blank.status_code, 400)
        self.assertIn("required", blank.json()["detail"])

        with patch.dict(os.environ, {"JWT_SECRET": ""}):
            missing = self._register(org_id="secret-co", login_name="secret-co")
        self.assertEqual(missing.status_code, 500)
        self.assertIn("JWT_SECRET", missing.json()["detail"])

    def test_username_is_stored_lowercase_and_the_hash_is_not_the_password(self):
        created = self._register(login_name="  NexifyUser  ")
        self.assertEqual(created.status_code, 200)
        signed_in = self.client.post(
            "/v1/login",
            json={"login_name": "NEXIFYUSER", "password": "correct-horse"},
        )
        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(signed_in.json()["org_id"], "nexify")

        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        login_name, password_hash = connection.execute(
            "SELECT login_name, password_hash FROM tenant_configs WHERE org_id = ?",
            ("nexify",),
        ).fetchone()
        connection.close()
        self.assertEqual(login_name, "nexifyuser")
        self.assertTrue(password_hash.startswith("pbkdf2_sha256$"))
        self.assertNotIn("correct-horse", password_hash)

    def test_a_taken_username_and_a_second_register_are_rejected(self):
        self.assertEqual(self._register(org_id="one", login_name="shared").status_code, 200)
        taken = self._register(org_id="two", login_name="Shared")
        self.assertEqual(taken.status_code, 400)
        self.assertEqual(taken.json()["detail"], "That username is already used")

        again = self._register(org_id="one", login_name="other-name")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()["detail"], "This company already has a login")

    def test_login_with_the_company_id_fails_when_the_username_is_different(self):
        created = self._register(org_id="google123", login_name="google@123", password="Google@1234")
        self.assertEqual(created.status_code, 200)

        wrong_name = self.client.post(
            "/v1/login",
            json={"login_name": "google123", "password": "Google@1234"},
        )
        unknown = self.client.post(
            "/v1/login",
            json={"login_name": "nobody", "password": "Google@1234"},
        )
        wrong_password = self.client.post(
            "/v1/login",
            json={"login_name": "google@123", "password": "not-the-password"},
        )
        self.assertEqual(wrong_name.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(wrong_name.json()["detail"], "Unknown username or password")
        self.assertEqual(unknown.json()["detail"], wrong_password.json()["detail"])
        self.assertNotIn("Google@1234", wrong_password.text)

        signed_in = self.client.post(
            "/v1/login",
            json={"login_name": "Google@123", "password": "Google@1234"},
        )
        self.assertEqual(signed_in.status_code, 200)
        self.assertEqual(signed_in.json()["org_id"], "google123")

    def test_eight_characters_is_accepted_and_a_racing_register_is_rejected(self):
        created = self._register(org_id="eight", login_name="eight", password="12345678")
        self.assertEqual(created.status_code, 200)

        from services.company_login import register_company

        barrier = threading.Barrier(2)
        outcomes = []

        def register(org_id):
            barrier.wait()
            try:
                register_company(org_id, "shared-login", "correct-horse", org_id)
                outcomes.append("ok")
            except ValueError as exc:
                outcomes.append(str(exc))

        threads = [
            threading.Thread(target=register, args=("race-a",)),
            threading.Thread(target=register, args=("race-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["That username is already used", "ok"])

    def test_a_failed_login_does_not_replace_the_current_token(self):
        token = self._register().json()["token"]
        rejected = self.client.post(
            "/v1/login",
            json={"login_name": "nexify", "password": "wrong-password"},
        )
        self.assertEqual(rejected.status_code, 401)
        still_valid = self.client.get(
            "/v1/jobs/missing-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(still_valid.status_code, 404)

    def test_expired_tampered_and_empty_tokens_are_rejected(self):
        token = self._register().json()["token"]
        expired = self.client.get(
            "/v1/jobs/missing-job",
            headers={"Authorization": f"Bearer {token}"},
        )
        with patch("services.company_login.time.time", return_value=time.time() + 7200):
            expired = self.client.get(
                "/v1/jobs/missing-job",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()["detail"], "Login expired")

        tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
        bad_signature = self.client.get(
            "/v1/jobs/missing-job",
            headers={"Authorization": f"Bearer {tampered}"},
        )
        self.assertEqual(bad_signature.status_code, 401)
        self.assertEqual(bad_signature.json()["detail"], "Login token is not valid")

        for header in (None, "Bearer", "Token " + token):
            kwargs = {}
            if header is not None:
                kwargs["headers"] = {"Authorization": header}
            rejected = self.client.get("/v1/jobs/missing-job", **kwargs)
            self.assertEqual(rejected.status_code, 401, header)


class TestJobAndFileEdges(unittest.TestCase):
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
        registered = self.client.post(
            "/v1/register",
            json={
                "org_id": "nexify",
                "login_name": "nexify",
                "password": "correct-horse",
                "name": "Nexify",
            },
        )
        self.token = registered.json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        tenant._IN_MEMORY_TENANTS.clear()
        tenant_repository._MEM_CONN = None
        self.env.stop()
        self.tmp.cleanup()

    def test_upload_rejects_the_wrong_type_an_empty_file_and_a_missing_field(self):
        not_pdf = self.client.post(
            "/v1/checks",
            headers=self.headers,
            files={"contract": ("notes.txt", b"hello", "text/plain")},
        )
        self.assertEqual(not_pdf.status_code, 400)
        self.assertEqual(not_pdf.json()["detail"], "Only PDF files are accepted")

        empty = self.client.post(
            "/v1/policies",
            headers=self.headers,
            files={"files": ("policy.pdf", b"", "application/pdf")},
        )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["detail"], "A file was empty")

        missing = self.client.post("/v1/checks", headers=self.headers)
        self.assertEqual(missing.status_code, 422)

        accepted = self.client.post(
            "/v1/checks",
            headers=self.headers,
            files={"contract": ("CONTRACT.PDF", b"%PDF-1.4 bytes", "application/pdf")},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["status"], "queued")

    def test_a_path_in_the_filename_cannot_leave_the_object_folder(self):
        response = self.client.post(
            "/v1/policies",
            headers=self.headers,
            files={"files": ("../secret.pdf", b"%PDF-1.4 hidden", "application/pdf")},
        )
        self.assertEqual(response.status_code, 200)
        root = Path(os.environ["OBJECT_STORE_PATH"]).resolve()
        self.assertFalse((root.parent / "secret.pdf").exists())
        written = list(root.rglob("secret.pdf"))
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].resolve().is_relative_to(root))
        self.assertEqual(written[0].read_bytes(), b"%PDF-1.4 hidden")

        store = LocalObjectStore(str(root))
        with self.assertRaises(ValueError):
            store.put("../outside.pdf", b"nope")
        with self.assertRaises(ValueError):
            store.put("a/../../outside.pdf", b"nope")
        with self.assertRaises(ValueError):
            store.put(str(root.parent / "abs.pdf"), b"nope")
        self.assertFalse((root.parent / "outside.pdf").exists())

    def test_a_missing_file_fails_the_job_and_a_running_job_is_left_alone(self):
        accepted = self.client.post(
            "/v1/checks",
            headers=self.headers,
            files={"contract": ("contract.pdf", b"%PDF-1.4 contract", "application/pdf")},
        )
        job_id = accepted.json()["job_id"]
        key = accepted.json()["object_key"]
        os.remove(os.path.join(os.environ["OBJECT_STORE_PATH"], *key.split("/")))

        self.assertTrue(run_once())
        failed = self.client.get(f"/v1/jobs/{job_id}", headers=self.headers)
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["status"], "failed")
        self.assertTrue(failed.json()["error"])
        self.assertNotIn("%PDF-1.4 contract", failed.json()["error"])

        unknown = create_job(
            "nexify",
            "not.a.kind",
            [{"document_id": "1", "filename": "a.pdf", "object_key": "nexify/x/a.pdf"}],
        )
        self.assertTrue(run_once())
        self.assertEqual(get_job("nexify", unknown)["status"], "failed")
        self.assertIn("Unknown job kind", get_job("nexify", unknown)["error"])

        waiting = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "2", "filename": "b.pdf", "object_key": "nexify/x/b.pdf"}],
        )
        claimed = claim_next()
        self.assertEqual(claimed["job_id"], waiting)
        self.assertIsNone(claim_next())
        self.assertEqual(get_job("nexify", waiting)["status"], "running")
        self.assertIsNone(claim_next())

    def test_a_stale_running_job_is_claimed_again(self):
        job_id = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "stale", "filename": "c.pdf", "object_key": "nexify/x/stale.pdf"}],
        )
        self.assertEqual(claim_next()["job_id"], job_id)
        self.assertIsNone(claim_next())
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        connection.execute(
            "UPDATE jobs SET updated_at = '2000-01-01 00:00:00' WHERE job_id = ?",
            (job_id,),
        )
        connection.commit()
        connection.close()
        again = claim_next()
        self.assertEqual(again["job_id"], job_id)
        self.assertEqual(get_job("nexify", job_id)["status"], "running")

    def test_two_workers_cannot_claim_the_same_sqlite_job(self):
        job_id = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "3", "filename": "c.pdf", "object_key": "nexify/x/c.pdf"}],
        )
        barrier = threading.Barrier(2)
        claimed = []

        def take():
            barrier.wait()
            claimed.append(claim_next())

        threads = [threading.Thread(target=take) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [item for item in claimed if item is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["job_id"], job_id)
        self.assertEqual(get_job("nexify", job_id)["status"], "running")

    def test_an_unknown_job_is_404_for_the_owner(self):
        missing = self.client.get("/v1/jobs/does-not-exist", headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "Job not found")
        odd = self.client.get("/v1/jobs/1%27%20OR%201%3D1", headers=self.headers)
        self.assertEqual(odd.status_code, 404)

    def test_several_files_become_one_job(self):
        response = self.client.post(
            "/v1/policies",
            headers=self.headers,
            files=[
                ("files", ("one.pdf", b"%PDF-1.4 one", "application/pdf")),
                ("files", ("two.pdf", b"%PDF-1.4 two", "application/pdf")),
            ],
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(len(body["object_keys"]), 2)
        self.assertNotEqual(body["object_keys"][0], body["object_keys"][1])

    def test_an_empty_clause_is_rejected(self):
        response = self.client.post(
            "/v1/checks/clause",
            headers=self.headers,
            json={"clause_text": "   ", "clause_type": "Payment"},
        )
        self.assertEqual(response.status_code, 400)

    def test_five_workers_take_five_jobs_and_only_one_takes_a_single_job(self):
        job_ids = [
            create_job(
                "nexify",
                "contract.submitted",
                [{"document_id": str(number), "filename": "c.pdf", "object_key": f"nexify/x/{number}.pdf"}],
            )
            for number in range(5)
        ]
        barrier = threading.Barrier(5)
        claimed = []

        def take():
            barrier.wait()
            claimed.append(claim_next())

        threads = [threading.Thread(target=take) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [item["job_id"] for item in claimed if item is not None]
        self.assertEqual(sorted(winners), sorted(job_ids))
        self.assertEqual(len(set(winners)), 5)
        self.assertIsNone(claim_next())

        lone = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "only", "filename": "c.pdf", "object_key": "nexify/x/only.pdf"}],
        )
        barrier = threading.Barrier(5)
        second = []

        def take_one():
            barrier.wait()
            second.append(claim_next())

        threads = [threading.Thread(target=take_one) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        got = [item for item in second if item is not None]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["job_id"], lone)


class TestRedisEdges(unittest.TestCase):
    def setUp(self):
        self.cache = RedisCache(client=fakeredis.FakeRedis(decode_responses=True))

    def test_blank_text_and_a_missing_key_store_nothing(self):
        self.assertIsNone(self.cache.get_verdict("   ", "org1"))
        self.cache.set_verdict("org1", "   ", {"compliant": True, "reason": "no"})
        self.assertIsNone(self.cache.get("missing"))
        self.assertIsNone(self.cache.get_verdict("Pay within 30 days.", "org1"))

    def test_clause_type_and_tenant_settings_keep_verdicts_apart(self):
        embedding = [1.0] + [0.0] * 15
        near = [0.99] + [0.01] * 15
        self.cache.set_verdict(
            "org1",
            "Vendor must send invoices at the end of each month.",
            {"compliant": True, "reason": "monthly"},
            clause_type="Payment",
            version="v1.0",
            tenant_settings="review_required:allow_favorable",
            embedding=embedding,
        )
        self.assertIsNone(self.cache.get_verdict(
            "Invoices shall be dispatched by vendor on a monthly basis.",
            "org1",
            clause_type="Insurance",
            version="v1.0",
            tenant_settings="review_required:allow_favorable",
            embedding=near,
            threshold=0.90,
        ))
        self.assertIsNone(self.cache.get_verdict(
            "Invoices shall be dispatched by vendor on a monthly basis.",
            "org1",
            clause_type="Payment",
            version="v1.0",
            tenant_settings="compliant:strict",
            embedding=near,
            threshold=0.90,
        ))
        hit = self.cache.get_verdict(
            "Invoices shall be dispatched by vendor on a monthly basis.",
            "org1",
            clause_type="Payment",
            version="v1.0",
            tenant_settings="review_required:allow_favorable",
            embedding=near,
            threshold=0.90,
        )
        self.assertEqual(hit["cache_tier"], "tier2_semantic")

    def test_deleting_a_missing_company_leaves_the_other_company(self):
        self.cache.set_verdict("org1", "Pay within 30 days.", {"compliant": True, "reason": "kept"})
        self.cache.invalidate("nobody")
        kept = self.cache.get_verdict("Pay within 30 days.", "org1")
        self.assertEqual(kept["reason"], "kept")
        self.cache.invalidate("org1")
        self.assertIsNone(self.cache.get_verdict("Pay within 30 days.", "org1"))


class TestEdgeConfig(unittest.TestCase):
    def test_the_proxy_names_the_service_instead_of_a_fixed_pair(self):
        template = Path(__file__).resolve().parents[1] / "deploy" / "nginx" / "default.conf.template"
        text = template.read_text(encoding="utf-8")
        self.assertIn("resolver 127.0.0.11", text)
        self.assertIn("${API_UPSTREAM}", text)
        self.assertNotIn("API_UPSTREAM_1", text)
        self.assertNotIn("API_UPSTREAM_2", text)


class TestS3ObjectStore(unittest.TestCase):
    def test_a_missing_bucket_is_rejected(self):
        from core.object_store import S3ObjectStore

        with patch.dict(os.environ, {"S3_BUCKET": ""}):
            with self.assertRaises(RuntimeError) as caught:
                S3ObjectStore()
        self.assertIn("S3_BUCKET", str(caught.exception))

    def test_put_and_get_use_the_bucket_key_and_reject_a_parent_path(self):
        from io import BytesIO
        from unittest.mock import MagicMock

        from core.object_store import get_object_store

        client = MagicMock()
        client.get_object.return_value = {"Body": BytesIO(b"%PDF-1.4 stored")}
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_BACKEND": "s3",
                "S3_BUCKET": "contract-checker",
                "S3_REGION": "us-east-1",
                "S3_ENDPOINT_URL": "",
                "S3_ACCESS_KEY_ID": "",
                "S3_SECRET_ACCESS_KEY": "",
            },
        ):
            with patch("boto3.client", return_value=client) as factory:
                store = get_object_store()
                store.put("acme/policies/a.pdf", b"%PDF-1.4 stored")
                self.assertEqual(store.get("acme/policies/a.pdf"), b"%PDF-1.4 stored")
                with self.assertRaises(ValueError):
                    store.put("../outside.pdf", b"nope")
        self.assertEqual(factory.call_args.args[0], "s3")
        self.assertEqual(client.put_object.call_args.kwargs["Bucket"], "contract-checker")
        self.assertEqual(client.put_object.call_args.kwargs["Key"], "acme/policies/a.pdf")
        self.assertEqual(client.put_object.call_args.kwargs["Body"], b"%PDF-1.4 stored")
        client.put_object.assert_called_once()
        self.assertIsNone(factory.call_args.kwargs.get("config"))

    def test_a_custom_endpoint_uses_path_style_addresses(self):
        from unittest.mock import MagicMock

        from core.object_store import S3ObjectStore

        with patch.dict(
            os.environ,
            {
                "S3_BUCKET": "contract-checker",
                "S3_ENDPOINT_URL": "http://minio:9000",
                "S3_ACCESS_KEY_ID": "contract",
                "S3_SECRET_ACCESS_KEY": "contractsecret",
                "S3_REGION": "us-east-1",
            },
        ):
            with patch("boto3.client", return_value=MagicMock()) as factory:
                S3ObjectStore()
        self.assertEqual(factory.call_args.kwargs["endpoint_url"], "http://minio:9000")
        self.assertEqual(
            factory.call_args.kwargs["config"].s3["addressing_style"],
            "path",
        )


class TestCompanyBudget(unittest.TestCase):
    def test_each_company_has_its_own_minute(self):
        from core.company_budget import BudgetExceeded, charge_company

        fake = fakeredis.FakeRedis(decode_responses=True)
        for _ in range(3):
            charge_company("nexify", client=fake, limit=3)
        with self.assertRaises(BudgetExceeded):
            charge_company("nexify", client=fake, limit=3)
        charge_company("other", client=fake, limit=3)

    def test_sqlite_mode_does_not_count(self):
        from core.company_budget import charge_company

        with patch.dict(os.environ, {"CACHE_BACKEND": "sqlite"}):
            for _ in range(5):
                charge_company("nexify", limit=1)


class TestCloudStartup(unittest.TestCase):
    def test_s3_and_redis_must_be_named_before_the_server_starts(self):
        from scripts.prepare_runtime import prepare

        with patch.dict(
            os.environ,
            {
                "OBJECT_STORE_BACKEND": "s3",
                "S3_BUCKET": "",
                "CACHE_BACKEND": "sqlite",
                "VECTOR_STORE_BACKEND": "sqlite",
            },
        ):
            with self.assertRaises(RuntimeError) as caught:
                prepare()
        self.assertIn("S3_BUCKET", str(caught.exception))

        with patch.dict(
            os.environ,
            {
                "CACHE_BACKEND": "redis",
                "REDIS_URL": "",
                "OBJECT_STORE_BACKEND": "local",
                "VECTOR_STORE_BACKEND": "sqlite",
            },
        ):
            with self.assertRaises(RuntimeError) as caught:
                prepare()
        self.assertIn("REDIS_URL", str(caught.exception))

    def test_a_silent_redis_stops_startup(self):
        from scripts.prepare_runtime import prepare

        class Silent:
            def ping(self):
                raise ConnectionError("down")

            def close(self):
                return None

        with patch.dict(
            os.environ,
            {
                "CACHE_BACKEND": "redis",
                "REDIS_URL": "redis://127.0.0.1:6399/0",
                "OBJECT_STORE_BACKEND": "local",
                "VECTOR_STORE_BACKEND": "sqlite",
                "OBJECT_STORE_PATH": os.path.join(tempfile.gettempdir(), "cloud-start-objects"),
                "SQLITE_DB_PATH": os.path.join(tempfile.gettempdir(), "cloud-start.db"),
            },
        ):
            with patch("redis.Redis.from_url", return_value=Silent()):
                with self.assertRaises(RuntimeError) as caught:
                    prepare()
        self.assertIn("Redis did not answer", str(caught.exception))

    def test_the_cloud_compose_file_does_not_start_a_database(self):
        text = (
            Path(__file__).resolve().parents[1] / "docker-compose.cloud.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("OBJECT_STORE_BACKEND: s3", text)
        self.assertIn("CACHE_BACKEND: redis", text)
        self.assertIn("deploy/cloud.env", text)
        self.assertNotIn("pgvector/pgvector", text)
        self.assertNotIn("redis:7-alpine", text)


@unittest.skipUnless(_postgres_is_up(), "Postgres is not running on 127.0.0.1:5432")
class TestPostgresJobEdges(unittest.TestCase):
    database = "contract_checker_edge"

    def setUp(self):
        import psycopg

        self.admin = psycopg.connect(
            "host=127.0.0.1 port=5432 dbname=contract_checker user=contract password=contract",
            autocommit=True,
        )
        self.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (self.database,),
        )
        self.admin.execute(f"DROP DATABASE IF EXISTS {self.database}")
        self.admin.execute(f"CREATE DATABASE {self.database}")
        self.env = patch.dict(
            os.environ,
            {
                "VECTOR_STORE_BACKEND": "pgvector",
                "DATABASE_URL": (
                    "postgresql+psycopg://contract:contract@127.0.0.1:5432/"
                    + self.database
                ),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (self.database,),
        )
        self.admin.execute(f"DROP DATABASE IF EXISTS {self.database}")
        self.admin.close()

    def test_two_workers_skip_the_same_row_and_schema_setup_can_run_twice(self):
        from core.jobs import ensure_jobs_table
        from core.stored_files import ensure_document_table

        errors = []

        def prepare():
            try:
                ensure_jobs_table()
                ensure_document_table()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        ensure_jobs_table()

        job_id = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "9", "filename": "c.pdf", "object_key": "nexify/x/c.pdf"}],
        )
        barrier = threading.Barrier(2)
        claimed = []

        def take():
            barrier.wait()
            claimed.append(claim_next())

        workers = [threading.Thread(target=take) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        winners = [item for item in claimed if item is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["job_id"], job_id)
        self.assertEqual(get_job("nexify", job_id)["status"], "running")
        self.assertIsNone(claim_next())

    def test_four_workers_take_four_different_jobs(self):
        from core.jobs import ensure_jobs_table

        ensure_jobs_table()
        job_ids = [
            create_job(
                "nexify",
                "contract.submitted",
                [{"document_id": str(number), "filename": "c.pdf", "object_key": f"nexify/x/{number}.pdf"}],
            )
            for number in range(4)
        ]
        barrier = threading.Barrier(4)
        claimed = []

        def take():
            barrier.wait()
            claimed.append(claim_next())

        workers = [threading.Thread(target=take) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        winners = [item["job_id"] for item in claimed if item is not None]
        self.assertEqual(sorted(winners), sorted(job_ids))
        self.assertIsNone(claim_next())

    def test_a_stale_running_job_is_claimed_again(self):
        from sqlalchemy import create_engine, text

        job_id = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "stale", "filename": "c.pdf", "object_key": "nexify/x/stale.pdf"}],
        )
        self.assertEqual(claim_next()["job_id"], job_id)
        self.assertIsNone(claim_next())
        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE jobs SET updated_at = now() - interval '1 day' WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
        finally:
            engine.dispose()
        again = claim_next()
        self.assertEqual(again["job_id"], job_id)
        self.assertEqual(get_job("nexify", job_id)["status"], "running")
