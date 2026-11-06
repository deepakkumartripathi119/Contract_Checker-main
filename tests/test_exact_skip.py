"""An exact saved verdict does not embed the clause again."""

import unittest
from unittest.mock import patch

from pipeline.nodes.clause_sender import ReRanker_Sender


class _Tenant:
    active_policy_version = "1"
    silent_policy_behavior = "review_required"
    favorable_deviation_behavior = "compliant"


class _Cache:
    def get_verdict(self, *_args, **_kwargs):
        return {
            "compliant": True,
            "compliance_status": "compliant",
            "cached": True,
            "cache_tier": "tier1_exact",
        }


class TestExactSkip(unittest.TestCase):
    def test_every_exact_hit_skips_the_embedding_call(self):
        with patch("pipeline.nodes.clause_sender.batch_retrieve", side_effect=AssertionError("embed")), patch(
            "core.tenant.get_tenant_config", return_value=_Tenant()
        ), patch("core.cache.get_cache", return_value=_Cache()):
            sends = ReRanker_Sender({
                "org_id": "nexify",
                "extracted_clauses": [
                    {"text": "Pay in 30 days.", "clause_type": "Payment"},
                    {"text": "Disputes go to arbitration.", "clause_type": "Disputes"},
                ],
            })
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(send.node == "SkipNode" for send in sends))
        self.assertEqual(sends[0].arg["answer"][0]["cache_tier"], "tier1_exact")
