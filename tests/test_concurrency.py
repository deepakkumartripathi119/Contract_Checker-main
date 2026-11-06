"""Races across login, jobs, the verdict cache, and the company budget."""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import fakeredis

from core import tenant, tenant_repository
from core.company_budget import BudgetExceeded, charge_company
from core.jobs import claim_next, create_job, get_job
from core.redis_cache import RedisCache
from scripts.worker import run_once
from services.company_login import company_from_token, login_company, register_company


class TestLoginRaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(
            os.environ,
            {
                "SQLITE_DB_PATH": os.path.join(self.tmp.name, "tenants.db"),
                "JWT_SECRET": "test-secret",
                "VECTOR_STORE_BACKEND": "sqlite",
            },
        )
        self.env.start()
        tenant._IN_MEMORY_TENANTS.clear()
        tenant_repository._MEM_CONN = None
        register_company("nexify", "nexify", "correct-horse", "Nexify")

    def tearDown(self):
        tenant._IN_MEMORY_TENANTS.clear()
        tenant_repository._MEM_CONN = None
        self.env.stop()
        self.tmp.cleanup()

    def test_parallel_logins_keep_only_the_latest_token(self):
        barrier = threading.Barrier(6)
        tokens = []
        errors = []

        def sign_in():
            barrier.wait()
            try:
                token, _org = login_company("nexify", "correct-horse")
                tokens.append(token)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=sign_in) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(tokens), 6)
        versions = []
        for token in tokens:
            payload = token.split(".")[1]
            import base64
            import json
            padded = payload + "=" * (-len(payload) % 4)
            versions.append(json.loads(base64.urlsafe_b64decode(padded))["sv"])
        self.assertEqual(len(set(versions)), 6)
        latest = max(versions)
        for token, version in zip(tokens, versions):
            if version == latest:
                self.assertEqual(company_from_token(token), "nexify")
            else:
                with self.assertRaises(ValueError):
                    company_from_token(token)

    def test_a_policy_save_does_not_roll_back_the_login(self):
        token, _org = login_company("nexify", "correct-horse")
        cached = tenant._IN_MEMORY_TENANTS["nexify"]
        cached.session_version = 0
        from core.tenant import bump_tenant_policy_version

        bump_tenant_policy_version("nexify")
        self.assertEqual(company_from_token(token), "nexify")

    def test_two_registers_for_one_company_do_not_both_win(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def register(org_id):
            barrier.wait()
            try:
                register_company(org_id, "shared-name", "correct-horse", org_id)
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
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(len(outcomes), 2)


class TestJobRaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(
            os.environ,
            {
                "SQLITE_DB_PATH": os.path.join(self.tmp.name, "jobs.db"),
                "VECTOR_STORE_BACKEND": "sqlite",
                "OBJECT_STORE_BACKEND": "local",
                "OBJECT_STORE_PATH": os.path.join(self.tmp.name, "objects"),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_two_workers_run_one_job_once(self):
        job_id = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "1", "filename": "c.pdf", "object_key": "nexify/x/c.pdf"}],
        )
        calls = []

        def slow(job):
            time.sleep(0.05)
            calls.append(job["job_id"])
            return {"ok": True}

        barrier = threading.Barrier(2)
        results = []

        def work():
            barrier.wait()
            results.append(run_once())

        with patch("scripts.worker.run_job", side_effect=slow):
            threads = [threading.Thread(target=work) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(calls, [job_id])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(get_job("nexify", job_id)["status"], "succeeded")

    def test_a_queued_job_is_taken_before_a_stale_one(self):
        stale = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "old", "filename": "a.pdf", "object_key": "nexify/x/a.pdf"}],
        )
        self.assertEqual(claim_next()["job_id"], stale)
        connection = sqlite3.connect(os.environ["SQLITE_DB_PATH"])
        connection.execute(
            "UPDATE jobs SET updated_at = '2000-01-01 00:00:00' WHERE job_id = ?",
            (stale,),
        )
        connection.commit()
        connection.close()
        fresh = create_job(
            "nexify",
            "contract.submitted",
            [{"document_id": "new", "filename": "b.pdf", "object_key": "nexify/x/b.pdf"}],
        )
        self.assertEqual(claim_next()["job_id"], fresh)
        self.assertEqual(claim_next()["job_id"], stale)


class TestCacheAndBudgetRaces(unittest.TestCase):
    def test_parallel_writes_of_one_clause_do_not_crash(self):
        cache = RedisCache(client=fakeredis.FakeRedis(decode_responses=True))
        barrier = threading.Barrier(8)

        def write(mark):
            barrier.wait()
            cache.set_verdict("org1", "Pay within 30 days.", {"compliant": True, "reason": mark})

        threads = [threading.Thread(target=write, args=(str(number),)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        found = cache.get_verdict("Pay within 30 days.", "org1")
        self.assertIn(found["reason"], {str(number) for number in range(8)})
        cache.invalidate("org1")
        self.assertIsNone(cache.get_verdict("Pay within 30 days.", "org1"))

    def test_parallel_charges_stop_at_the_limit_for_each_company(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        barrier = threading.Barrier(40)
        allowed = []
        rejected = []

        def charge(org_id):
            barrier.wait()
            try:
                charge_company(org_id, client=fake, limit=10)
                allowed.append(org_id)
            except BudgetExceeded:
                rejected.append(org_id)

        threads = []
        for _ in range(20):
            threads.append(threading.Thread(target=charge, args=("nexify",)))
            threads.append(threading.Thread(target=charge, args=("other",)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(allowed.count("nexify"), 10)
        self.assertEqual(rejected.count("nexify"), 10)
        self.assertEqual(allowed.count("other"), 10)
        self.assertEqual(rejected.count("other"), 10)


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


@unittest.skipUnless(_postgres_is_up(), "Postgres is not running on 127.0.0.1:5432")
class TestPostgresLoginRace(unittest.TestCase):
    database = "contract_checker_race"

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
                    "postgresql+psycopg://contract:contract@127.0.0.1:5432/" + self.database
                ),
                "JWT_SECRET": "test-secret",
            },
        )
        self.env.start()
        tenant._IN_MEMORY_TENANTS.clear()
        register_company("nexify", "nexify", "correct-horse", "Nexify")

    def tearDown(self):
        tenant._IN_MEMORY_TENANTS.clear()
        self.env.stop()
        self.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (self.database,),
        )
        self.admin.execute(f"DROP DATABASE IF EXISTS {self.database}")
        self.admin.close()

    def test_parallel_logins_keep_only_the_latest_token(self):
        barrier = threading.Barrier(4)
        tokens = []

        def sign_in():
            barrier.wait()
            token, _org = login_company("nexify", "correct-horse")
            tokens.append(token)

        threads = [threading.Thread(target=sign_in) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        import base64
        import json

        versions = []
        for token in tokens:
            payload = token.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            versions.append(json.loads(base64.urlsafe_b64decode(padded))["sv"])
        self.assertEqual(sorted(versions), [2, 3, 4, 5])
        latest = [token for token, version in zip(tokens, versions) if version == 5][0]
        self.assertEqual(company_from_token(latest), "nexify")
