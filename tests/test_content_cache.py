"""Same file bytes keep the policy version and the saved contract report."""

import unittest
from unittest.mock import MagicMock, patch

import fakeredis

from core.cache import file_hash, policy_set_hash
from core.redis_cache import RedisCache
from core.sqlite_cache import SQLiteCache
from core.vector_store import Chunk
from pipeline.nodes.document_parser import _doc_id
from pipeline.nodes.retriever import process_uploaded_files
from services.contract_check import _check_stored_contract


class _Upload:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self._data = data

    def read(self) -> bytes:
        return self._data


class TestAlreadyJudged(unittest.TestCase):
    def test_a_requirement_already_quoted_is_not_also_missing(self):
        from core.requirements import PolicyRequirement, RequirementCoverageResult
        from pipeline.nodes.missing_clause_checker import _requirement_already_decided

        requirement = PolicyRequirement(
            requirement_id="policy:3",
            section_title="Dispute Resolution",
            source_text=(
                "3. Dispute Resolution All disputes arising out of vendor contracts "
                "shall be resolved by binding arbitration in New York, NY."
            ),
        )
        coverage = RequirementCoverageResult(
            requirement_id="policy:3",
            coverage_status="not_covered",
            explanation="No clause was matched.",
        )
        quote = (
            "All disputes arising out of vendor contracts shall be resolved "
            "by binding arbitration in New York, NY."
        )
        decided = [{
            "text": "litigation in california",
            "title": "jurisdiction",
            "quote": quote.lower(),
        }]
        self.assertTrue(_requirement_already_decided(coverage, requirement, decided))

        other = PolicyRequirement(
            requirement_id="policy:9",
            section_title="Audit",
            source_text="The vendor shall allow an annual audit of its books and records.",
        )
        self.assertFalse(_requirement_already_decided(coverage, other, decided))


class TestLeafClauses(unittest.TestCase):
    def test_a_heading_with_a_deeper_clause_is_not_judged_again(self):
        from pipeline.nodes.document_parser import leaf_clauses

        clauses = [
            {"clause_type": "Services", "text": "1. Services\n\n1.1 Invoicing: pay in 15 days.", "metadata": {"depth": 1}},
            {"clause_type": "Invoicing", "text": "1.1 Invoicing: pay in 15 days.", "metadata": {"depth": 2}},
            {"clause_type": "Payment Terms", "text": "Pay within 45 days.", "metadata": {"depth": 1}},
        ]
        kept = leaf_clauses(clauses)
        self.assertEqual([item["clause_type"] for item in kept], ["Invoicing", "Payment Terms"])


class TestContentCache(unittest.TestCase):
    def test_file_order_does_not_change_the_policy_hash(self):
        self.assertEqual(policy_set_hash([b"a", b"b"]), policy_set_hash([b"b", b"a"]))
        self.assertNotEqual(policy_set_hash([b"a"]), policy_set_hash([b"b"]))

    def test_a_changed_policy_drops_the_saved_report_and_keeps_the_new_hash(self):
        cache = SQLiteCache(":memory:")
        cache.set_policy_hash("acme", policy_set_hash([b"old"]))
        cache.set_report("acme", "v1.0", file_hash(b"contract"), {"document_name": "a.pdf"})
        cache.invalidate("acme")
        cache.set_policy_hash("acme", policy_set_hash([b"new"]))
        self.assertIsNone(cache.get_report("acme", "v1.0", file_hash(b"contract")))
        self.assertEqual(cache.get_policy_hash("acme"), policy_set_hash([b"new"]))

    def test_redis_drops_one_companys_report(self):
        cache = RedisCache(client=fakeredis.FakeRedis(decode_responses=True))
        cache.set_report("acme", "v1.0", "file", {"document_name": "a.pdf"})
        cache.set_report("other", "v1.0", "file", {"document_name": "b.pdf"})
        cache.set_policy_hash("acme", "hash")
        cache.invalidate("acme")
        self.assertIsNone(cache.get_report("acme", "v1.0", "file"))
        self.assertEqual(cache.get_report("other", "v1.0", "file")["document_name"], "b.pdf")
        self.assertEqual(cache.get_policy_hash("acme"), "hash")

    def test_the_same_policy_bytes_do_not_embed_again(self):
        data = b"%PDF-same"
        cache = SQLiteCache(":memory:")
        cache.set_policy_hash("acme", policy_set_hash([data]))
        chunk = Chunk(
            org_id="acme",
            doc_id=_doc_id(data),
            chunk_type="parent",
            text="Pay in 45 days.",
            breadcrumb="[Doc: old.pdf | Section 1: Payment | Page 1]",
            metadata={"doc_name": "old.pdf", "heading": "Payment"},
            embedding=[0.1, 0.2],
        )
        store = MagicMock()
        store.export_org_chunks.return_value = [chunk]
        with patch("pipeline.nodes.retriever.get_cache", return_value=cache), \
             patch("pipeline.nodes.retriever.get_vector_store", return_value=store), \
             patch("pipeline.nodes.retriever.get_embedding_model") as embed, \
             patch("core.tenant.bump_tenant_policy_version") as bump:
            ids = process_uploaded_files([_Upload("renamed.pdf", data)], org_id="acme")
        embed.assert_not_called()
        bump.assert_not_called()
        self.assertEqual(ids, [chunk.doc_id])
        renamed = store.replace_org_chunks.call_args[0][1][0]
        self.assertEqual(renamed.metadata["doc_name"], "renamed.pdf")
        self.assertIn("Doc: renamed.pdf", renamed.breadcrumb)

    def test_the_same_contract_bytes_return_the_saved_report(self):
        pdf = b"%PDF-contract"
        cache = SQLiteCache(":memory:")
        from services.contract_check import _report_version
        cache.set_report("acme", _report_version("v1.0"), file_hash(pdf), {
            "document_name": "first.pdf",
            "present_clauses": [{"clause_title": "Payment"}],
            "missing_clauses": [],
        })
        with patch("services.contract_check.get_cache", return_value=cache), \
             patch("services.contract_check.contract_graph.invoke") as graph, \
             patch("services.contract_check._policy_version", return_value="v1.0"):
            report = _check_stored_contract(pdf, "second.pdf", "acme", "key-2")
        graph.assert_not_called()
        self.assertEqual(report["document_name"], "second.pdf")
        self.assertEqual(report["object_key"], "key-2")
        self.assertEqual(report["report_cache"], "exact")
        self.assertEqual(report["present_clauses"][0]["clause_title"], "Payment")
        stored = cache.get_report("acme", _report_version("v1.0"), file_hash(pdf))
        self.assertEqual(stored["document_name"], "first.pdf")
