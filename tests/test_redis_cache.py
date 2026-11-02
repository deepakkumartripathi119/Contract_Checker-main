"""Redis stores the same verdicts as SQLite. The guards stay in the application."""

import os
import unittest
from unittest.mock import patch

import fakeredis

from core.cache import get_cache
from core.redis_cache import RedisCache


class TestRedisCache(unittest.TestCase):
    def setUp(self):
        self.cache = RedisCache(client=fakeredis.FakeRedis(decode_responses=True))

    def test_exact_text_hits_without_a_second_model_call(self):
        clause = "Vendor shall submit monthly invoices."
        self.cache.set_verdict("org1", clause, {"compliant": True, "reason": "Matches policy."})
        cached = self.cache.get_verdict("  vendor shall  submit monthly invoices. ", "org1")
        self.assertEqual(cached["cache_tier"], "tier1_exact")
        self.assertEqual(cached["reason"], "Matches policy.")

    def test_near_duplicate_keeps_the_number_and_negation_rules(self):
        original = "Vendor must send invoices at the end of each month."
        embedding = [1.0] + [0.0] * 15
        self.cache.set_verdict(
            "org1",
            original,
            {"compliant": True, "reason": "Monthly billing accepted."},
            embedding=embedding,
        )
        paraphrased = "Invoices shall be dispatched by vendor on a monthly basis."
        hit = self.cache.get_verdict(
            paraphrased,
            "org1",
            embedding=[0.99] + [0.05] * 15,
            threshold=0.90,
        )
        self.assertEqual(hit["cache_tier"], "tier2_semantic")
        self.assertEqual(hit["clause_text"], paraphrased)

        self.cache.set_verdict(
            "org1",
            "Payment shall be made within 30 days.",
            {"compliant": True, "reason": "30 days."},
            embedding=[1.0] + [0.0] * 15,
        )
        self.assertIsNone(self.cache.get_verdict(
            "Payment shall be made within 90 days.",
            "org1",
            embedding=[0.999] + [0.001] * 15,
            threshold=0.90,
        ))
        self.cache.set_verdict(
            "org1",
            "Vendor shall indemnify and hold Nexify harmless.",
            {"compliant": True, "reason": "Indemnity provided."},
            embedding=[1.0] + [0.0] * 15,
        )
        self.assertIsNone(self.cache.get_verdict(
            "Vendor shall not indemnify or hold Nexify harmless.",
            "org1",
            embedding=[0.99] + [0.01] * 15,
            threshold=0.90,
        ))

    def test_policy_upload_deletes_only_that_company(self):
        self.cache.set_verdict("org1", "Pay within 30 days.", {"compliant": True})
        self.cache.set_verdict("org2", "Pay within 30 days.", {"compliant": False})
        self.cache.invalidate("org1")
        self.assertIsNone(self.cache.get_verdict("Pay within 30 days.", "org1"))
        kept = self.cache.get_verdict("Pay within 30 days.", "org2")
        self.assertEqual(kept["compliant"], False)

    def test_another_company_and_another_version_do_not_share_a_verdict(self):
        clause = "Vendor shall submit quarterly compliance reports."
        self.cache.set_verdict(
            "tenant_alpha",
            clause,
            {"compliant": True, "reason": "Quarterly reporting acceptable."},
            clause_type="Reporting",
            version="v1.0",
            tenant_settings="review_required:allow_favorable",
        )
        self.assertIsNone(self.cache.get_verdict(
            clause,
            "tenant_beta",
            clause_type="Reporting",
            version="v1.0",
            tenant_settings="review_required:allow_favorable",
        ))
        self.assertIsNone(self.cache.get_verdict(
            clause,
            "tenant_alpha",
            clause_type="Reporting",
            version="v2.0",
            tenant_settings="review_required:allow_favorable",
        ))

    def test_get_cache_selects_redis(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        with patch.dict(os.environ, {"CACHE_BACKEND": "redis", "REDIS_URL": "redis://127.0.0.1:6379/0"}):
            with patch("redis.Redis.from_url", return_value=fake):
                cache = get_cache()
        self.assertIsInstance(cache, RedisCache)
        cache.set_verdict("org1", "Keep this verdict.", {"compliant": True})
        self.assertEqual(cache.get_verdict("Keep this verdict.", "org1")["cache_tier"], "tier1_exact")
