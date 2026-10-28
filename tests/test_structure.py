"""
tests/test_structure.py — Validate project packaging, imports, pipeline integrity, and Phases 2, 3, & 4 features.
"""
import unittest
import os
import gc
import shutil
import tempfile
from unittest.mock import patch, MagicMock

from langchain.schema import Document
from core.vector_store import Chunk, SQLiteVectorStore
from core.cache import SQLiteCache
from core.citation_verifier import verify_citation, verify_compliance_result
from pipeline.nodes.retriever import (
    rrf_merge,
    deduplicate_parents_max_pooling,
    batch_retrieve,
)
from pipeline.nodes.reranker import Reranker
from pipeline.nodes.compliance_checker import evaluate_micro_batch, compliance_checker
from pipeline.nodes.missing_clause_checker import missing_clause_checker, is_policy_section_covered


class TestProjectStructure(unittest.TestCase):
    def test_core_imports(self):
        from core.state import (
            AgentState,
            SubState,
            Clause,
            ExtractedClauses,
            ClauseComplianceResult,
            MicroBatchComplianceResult,
            query,
        )
        self.assertIsNotNone(AgentState)
        self.assertIsNotNone(SubState)
        self.assertIsNotNone(Clause)
        self.assertIsNotNone(ExtractedClauses)
        self.assertIsNotNone(ClauseComplianceResult)
        self.assertIsNotNone(MicroBatchComplianceResult)
        self.assertIsNotNone(query)

    def test_model_factory(self):
        from core.model import get_llm, get_embedding_model, micro_batch_compliance_model
        llm = get_llm(temperature=0.0)
        self.assertIsNotNone(llm)
        embedder = get_embedding_model()
        self.assertIsNotNone(embedder)
        self.assertIsNotNone(micro_batch_compliance_model)

    def test_pipeline_nodes(self):
        from pipeline.nodes.extract_clause import extract_clauses
        from pipeline.nodes.extract_clause_sender import extract_clause_sender
        from pipeline.nodes.clause_sender import ReRanker_Sender
        from pipeline.nodes.clause_sender_node import clause_sender_node
        from pipeline.nodes.query_generator import generate_relevant_docs_node
        from pipeline.nodes.retriever import (
            process_uploaded_files,
            document_retriever,
            batch_retrieve,
            rrf_merge,
        )
        from pipeline.nodes.reranker import Reranker
        from pipeline.nodes.compliance_checker import compliance_checker, evaluate_micro_batch
        from pipeline.nodes.missing_clause_checker import missing_clause_checker

        self.assertTrue(callable(extract_clauses))
        self.assertTrue(callable(extract_clause_sender))
        self.assertTrue(callable(ReRanker_Sender))
        self.assertTrue(callable(clause_sender_node))
        self.assertTrue(callable(generate_relevant_docs_node))
        self.assertTrue(callable(process_uploaded_files))
        self.assertTrue(callable(document_retriever))
        self.assertTrue(callable(batch_retrieve))
        self.assertTrue(callable(rrf_merge))
        self.assertTrue(callable(Reranker))
        self.assertTrue(callable(compliance_checker))
        self.assertTrue(callable(evaluate_micro_batch))
        self.assertTrue(callable(missing_clause_checker))

    def test_graphs_compiled(self):
        from pipeline.graph import app
        from pipeline.subgraph import sub_graph

        self.assertIsNotNone(app)
        self.assertIsNotNone(sub_graph)

    def test_rrf_merge_logic(self):
        """Test that Reciprocal Rank Fusion correctly weights dense and sparse hits."""
        c1 = Chunk(id="c1", text="Payment terms 30 days")
        c2 = Chunk(id="c2", text="Termination on 14 days notice")
        c3 = Chunk(id="c3", text="Liability limitation")

        dense_hits = [(0.9, c1), (0.8, c2), (0.7, c3)]
        sparse_hits = [(12.0, c2), (9.0, c1)]

        merged = rrf_merge(
            dense_hits, sparse_hits, w_dense=0.7, w_sparse=0.3, k=60
        )
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0][0].id, "c1")
        self.assertEqual(merged[1][0].id, "c2")
        self.assertEqual(merged[2][0].id, "c3")

    def test_parent_deduplication_max_pooling(self):
        """Test that multiple children of the same parent are grouped with max score."""
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, "test_parent.db")

        try:
            store = SQLiteVectorStore(db_path=tmp_path)
            parent = Chunk(
                id="p1",
                org_id="test_org",
                chunk_type="parent",
                text="Full Section 1: Payment and invoicing details.",
                breadcrumb="[Doc: Policy | Section 1]",
            )
            child1 = Chunk(
                id="c1",
                org_id="test_org",
                parent_id="p1",
                chunk_type="child",
                text="Invoices must be paid within 30 days.",
            )
            child2 = Chunk(
                id="c2",
                org_id="test_org",
                parent_id="p1",
                chunk_type="child",
                text="Late fee is 1.5% per month.",
            )
            store.add_chunks("test_org", [parent, child1, child2])

            scored_chunks = [(child1, 0.012), (child2, 0.025)]
            docs = deduplicate_parents_max_pooling(scored_chunks, store, top_k=5)

            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0].metadata["chunk_id"], "p1")
            self.assertEqual(docs[0].page_content, parent.text)
            self.assertAlmostEqual(docs[0].metadata["rrf_score"], 0.025)
            self.assertEqual(len(docs[0].metadata["matched_children"]), 2)
        finally:
            del store
            gc.collect()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_sqlite_bm25_search(self):
        """Test in-memory BM25 index on SQLiteVectorStore."""
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, "test_bm25.db")

        try:
            store = SQLiteVectorStore(db_path=tmp_path)
            c1 = Chunk(
                id="c1",
                org_id="acme",
                chunk_type="child",
                text="All vendor payments are Net 30 days from approval.",
            )
            c2 = Chunk(
                id="c2",
                org_id="acme",
                chunk_type="child",
                text="Confidential information shall be kept secret for 5 years.",
            )
            store.add_chunks("acme", [c1, c2])

            results = store.search_sparse("acme", "vendor payments Net 30", top_k=5)
            self.assertTrue(len(results) > 0)
            self.assertEqual(results[0][1].id, "c1")

            results_conf = store.search_sparse("acme", "confidential secret", top_k=5)
            self.assertTrue(len(results_conf) > 0)
            self.assertEqual(results_conf[0][1].id, "c2")
        finally:
            del store
            gc.collect()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_batch_retrieve_single_embedding_call(self):
        """Test that batch_retrieve embeds all clauses in a single batch call."""
        store = SQLiteVectorStore(db_path=":memory:")
        c1 = Chunk(id="c1", text="All invoices Net 30", chunk_type="child")
        store.add_chunks("nexify", [c1])

        mock_embedder = MagicMock()
        mock_embedder.embed_documents.return_value = [[0.1] * 1024, [0.2] * 1024]

        clauses = [
            {"text": "Vendor payments Net 30"},
            {"text": "Late payments interest"},
        ]

        with patch("pipeline.nodes.retriever.get_embedding_model", return_value=mock_embedder), \
             patch("pipeline.nodes.retriever.get_vector_store", return_value=store):
            results = batch_retrieve(clauses, org_id="nexify")

            self.assertEqual(len(results), 2)
            self.assertEqual(mock_embedder.embed_documents.call_count, 1)

    # -----------------------------------------------------------------------
    # Phase 3 Tests: 3-Zone Gate & Micro-Batching
    # -----------------------------------------------------------------------

    def test_reranker_3_zone_gate_skip(self):
        """Zone 3: Score < 0.15 deterministically produces skip verdict without LLM call."""
        doc = Document(
            page_content="Policy on office supplies",
            metadata={"rrf_score": 0.08},
        )
        state = {
            "clause": {"text": "Vendor cafeteria rules", "clause_type": "Cafeteria"},
            "retrived_docs": [doc],
            "retry_count": 0,
        }
        res = Reranker(state)

        self.assertEqual(res["rerank_status"], "skip")
        self.assertLess(res["rerank_score"], 0.15)
        self.assertEqual(len(res["relevant_docs"]), 0)
        self.assertTrue(len(res["answer"]) > 0)
        verdict = res["answer"][0]
        self.assertTrue(verdict["compliant"])
        self.assertEqual(verdict["policy_source"], "NO INTERNAL POLICY FOUND")

    def test_reranker_3_zone_gate_retry(self):
        """Zone 2: 0.15 <= score < 0.35 triggers retry status and increments retry_count."""
        doc = Document(
            page_content="Policy on vendor billing",
            metadata={"rrf_score": 0.25},
        )
        state = {
            "clause": {"text": "Vendor payment schedule", "clause_type": "Payment"},
            "retrived_docs": [doc],
            "retry_count": 0,
        }
        res = Reranker(state)

        self.assertEqual(res["rerank_status"], "retry")
        self.assertEqual(res["retry_count"], 1)

    def test_reranker_3_zone_gate_proceed(self):
        """Zone 1: Score >= 0.35 proceeds to compliance_checker with relevant docs."""
        doc = Document(
            page_content="Net 30 payment policy mandatory",
            metadata={"rrf_score": 0.85},
        )
        state = {
            "clause": {"text": "Net 30 days invoice payment", "clause_type": "Payment Terms"},
            "retrived_docs": [doc],
            "retry_count": 0,
        }
        res = Reranker(state)

        self.assertEqual(res["rerank_status"], "proceed")
        self.assertGreaterEqual(res["rerank_score"], 0.35)
        self.assertEqual(len(res["relevant_docs"]), 1)
        self.assertEqual(res["relevant_docs"][0].metadata["relevance_score"], 0.85)

    def test_compliance_checker_preserves_skip(self):
        """compliance_checker must preserve deterministic verdict when rerank_status is skip."""
        skip_verdict = {
            "clause_title": "Cafeteria",
            "clause_text": "Vendor cafeteria rules",
            "policy_source": "NO INTERNAL POLICY FOUND",
            "reason": "Irrelevant",
            "compliant": True,
            "clauses_internal": [],
            "suggested_revision": None,
        }
        state = {
            "clause": {"text": "Vendor cafeteria rules", "clause_type": "Cafeteria"},
            "relevant_docs": [],
            "rerank_status": "skip",
            "answer": [skip_verdict],
        }
        res = compliance_checker(state)
        self.assertEqual(res["answer"], [skip_verdict])

    def test_micro_batch_compliance_fallback(self):
        """evaluate_micro_batch falls back gracefully when LLM is unavailable/mocked."""
        item = {
            "clause": {"text": "Payment Net 30", "clause_type": "Payment"},
            "relevant_docs": [Document(page_content="All payments Net 30", metadata={"source": "Policy"})],
            "rerank_status": "proceed",
        }
        mock_model = MagicMock()
        mock_model.invoke.side_effect = RuntimeError("Offline mock error")

        with patch("pipeline.nodes.compliance_checker.micro_batch_compliance_model", mock_model), \
             patch("pipeline.nodes.compliance_checker.compliance_model", mock_model):
            results = evaluate_micro_batch([item])
            self.assertTrue(len(results) > 0)
            self.assertEqual(results[0]["clause_title"], "Payment")

    # -----------------------------------------------------------------------
    # Phase 4 Tests: Two-Tier Cache, Citation Verification, Missing Clauses
    # -----------------------------------------------------------------------

    def test_cache_tier1_exact_hit(self):
        """Tier 1: Identical clause text retrieves cached verdict in < 1ms."""
        cache = SQLiteCache(db_path=":memory:")
        clause = "Vendor shall submit monthly invoices."
        verdict = {
            "clause_text": clause,
            "compliant": True,
            "reason": "Matches policy.",
            "policy_source": "Policy Section 1",
        }
        cache.set_verdict("org1", clause, verdict)

        # Lookup with identical text (different whitespace/case)
        cached = cache.get_verdict("  vendor shall  submit monthly invoices. ", "org1")
        self.assertIsNotNone(cached)
        self.assertTrue(cached.get("cached"))
        self.assertEqual(cached.get("cache_tier"), "tier1_exact")
        self.assertEqual(cached["reason"], "Matches policy.")

    def test_cache_tier2_semantic_hit(self):
        """Tier 2: Paraphrased clause with similar embedding hits semantic cache."""
        cache = SQLiteCache(db_path=":memory:")
        orig_clause = "Vendor must send invoices at the end of each month."
        verdict = {
            "clause_text": orig_clause,
            "compliant": True,
            "reason": "Monthly billing accepted.",
            "policy_source": "Billing Policy",
        }
        emb1 = [1.0] + [0.0] * 15
        cache.set_verdict("org1", orig_clause, verdict, embedding=emb1)

        # Paraphrased clause with high cosine similarity (no numbers or negations)
        para_clause = "Invoices shall be dispatched by vendor on a monthly basis."
        emb2 = [0.99] + [0.05] * 15  # high cosine sim > 0.95
        cached = cache.get_verdict(para_clause, "org1", embedding=emb2, threshold=0.90)

        self.assertIsNotNone(cached)
        self.assertTrue(cached.get("cached"))
        self.assertEqual(cached.get("cache_tier"), "tier2_semantic")
        self.assertEqual(cached["clause_text"], para_clause)

    def test_cache_numeric_guard_rejects_mismatch(self):
        """Numeric Guard: Prevents 'Net 30' from matching 'Net 90' even if vector sim is 0.99."""
        cache = SQLiteCache(db_path=":memory:")
        clause_30 = "Payment shall be made within 30 days."
        verdict_30 = {"compliant": True, "reason": "30 days compliant."}
        emb_30 = [1.0] + [0.0] * 15
        cache.set_verdict("org1", clause_30, verdict_30, embedding=emb_30)

        # Query with 90 days with virtually identical vector
        clause_90 = "Payment shall be made within 90 days."
        emb_90 = [0.999] + [0.001] * 15
        cached = cache.get_verdict(clause_90, "org1", embedding=emb_90, threshold=0.90)

        # Must be REJECTED because 30 != 90!
        self.assertIsNone(cached)

    def test_cache_negation_guard_rejects_polarity(self):
        """Negation Guard: Prevents 'shall indemnify' from matching 'shall not indemnify'."""
        cache = SQLiteCache(db_path=":memory:")
        clause_pos = "Vendor shall indemnify and hold Nexify harmless."
        verdict = {"compliant": True, "reason": "Indemnity provided."}
        emb_pos = [1.0] + [0.0] * 15
        cache.set_verdict("org1", clause_pos, verdict, embedding=emb_pos)

        # Query with negation inserted
        clause_neg = "Vendor shall not indemnify or hold Nexify harmless."
        emb_neg = [0.99] + [0.01] * 15
        cached = cache.get_verdict(clause_neg, "org1", embedding=emb_neg, threshold=0.90)

        # Must be REJECTED because polarity is opposite!
        self.assertIsNone(cached)

    def test_citation_verifier_exact_match(self):
        """Citation verifier confirms authentic quote from retrieved doc."""
        doc = Document(
            page_content="Invoices shall be payable Net 30 days from receipt of a valid tax invoice.",
            metadata={"source": "Policy.pdf"},
        )
        quote = "Net 30 days from receipt of a valid tax invoice"
        verified, clean_quote = verify_citation(quote, [doc])
        self.assertTrue(verified)
        self.assertEqual(clean_quote, quote)

    def test_citation_verifier_detects_hallucination(self):
        """Citation verifier catches hallucinated quotes not in retrieved documents."""
        doc = Document(
            page_content="Invoices shall be payable Net 30 days from receipt.",
            metadata={"source": "Policy.pdf"},
        )
        fake_quote = "Vendor shall pay a penalty of ten thousand dollars for each day of delay."
        verified, clean_quote = verify_citation(fake_quote, [doc])
        self.assertFalse(verified)

        # Result annotation
        bad_result = {
            "clause_text": "Late penalties",
            "reason": "Vendor violated penalty terms.",
            "exact_policy_quote": fake_quote,
        }
        annotated = verify_compliance_result(bad_result, [doc])
        self.assertFalse(annotated["citation_verified"])
        self.assertIn("[CITATION UNVERIFIED]", annotated["reason"])

    def test_missing_clause_checker_detects_omitted_sections(self):
        """Missing clause checker identifies mandatory policy sections omitted by vendor."""
        store = SQLiteVectorStore(db_path=":memory:")
        sec_pay = Chunk(
            id="s1",
            org_id="nexify",
            chunk_type="parent",
            text="Payment Terms: All vendor invoices must be Net 30.",
            breadcrumb="[Doc: Policy | Section 1: Payment Terms]",
            metadata={"heading": "Payment Terms", "doc_name": "Policy.pdf"},
        )
        sec_conf = Chunk(
            id="s2",
            org_id="nexify",
            chunk_type="parent",
            text="Confidentiality: Mutual 5-year NDA is mandatory for all vendors.",
            breadcrumb="[Doc: Policy | Section 2: Confidentiality]",
            metadata={"heading": "Confidentiality", "doc_name": "Policy.pdf"},
        )
        store.add_chunks("nexify", [sec_pay, sec_conf])

        # Vendor contract that ONLY contains payment terms (omits confidentiality!)
        contract_clauses = [
            {"clause_type": "Payment Terms", "text": "Vendor shall be paid within 30 days."},
        ]
        state = {
            "contract": ["contract text"],
            "extracted_clauses": contract_clauses,
            "answer": [],
        }

        with patch("pipeline.nodes.missing_clause_checker.get_vector_store", return_value=store):
            alerts = missing_clause_checker(state)
            self.assertEqual(len(alerts["answer"]), 1)
            missing = alerts["answer"][0]
            self.assertTrue(missing["is_missing_clause"])
            self.assertIn("Confidentiality", missing["clause_title"])
            self.assertFalse(missing["compliant"])
            self.assertEqual(missing["clause_text"], "[OMITTED FROM VENDOR CONTRACT]")

    def test_golden_dataset_structure(self):
        """Golden dataset must contain at least 25 labeled cases covering all categories."""
        from tests.golden_dataset import GOLDEN_TEST_CASES
        self.assertGreaterEqual(len(GOLDEN_TEST_CASES), 25)
        categories = {tc.category for tc in GOLDEN_TEST_CASES}
        self.assertIn("compliant", categories)
        self.assertIn("non_compliant", categories)
        self.assertIn("irrelevant", categories)
        self.assertIn("missing_clause", categories)

    def test_ablation_benchmark_runs(self):
        """Ablation benchmark must execute all 4 configurations and produce metrics."""
        from tests.evaluate import run_ablation_benchmark
        results = run_ablation_benchmark()
        self.assertIn("Config A: Dense Only", results)
        self.assertIn("Config B: BM25 Sparse Only", results)
        self.assertIn("Config C: Naive Fixed Hybrid", results)
        self.assertIn("Config D: Full Modern Pipeline", results)
        self.assertGreater(results["Config D: Full Modern Pipeline"]["recall@3"], 50.0)


if __name__ == "__main__":
    unittest.main()
