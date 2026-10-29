"""
tests/test_structure.py — Validate project packaging, imports, pipeline integrity, and Phases 2, 3, & 4 features.
"""
import unittest
import os
import gc
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from langchain.schema import Document
from core.vector_store import Chunk, SQLiteVectorStore
from core.state import _missing_clauses_reducer
from core.cache import SQLiteCache
from core.citation_verifier import (
    verify_citation,
    verify_compliance_result,
    clean_quote_formatting,
)
from pipeline.nodes.retriever import (
    rrf_merge,
    deduplicate_parents_max_pooling,
    batch_retrieve,
    policy_topic_matches,
)
from pipeline.nodes.reranker import Reranker
from pipeline.nodes.compliance_checker import (
    evaluate_micro_batch,
    compliance_checker,
    _normalize_batch_result,
)
from pipeline.nodes.missing_clause_checker import missing_clause_checker, is_policy_section_covered
from pipeline.nodes.document_parser import (
    remove_synthetic_expected_annotations,
    clean_extracted_text,
    parse_vendor_contract,
    parse_policy_pdf,
)
from prompts.compliance import COMPLIANCE_SYSTEM_PROMPT


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
            "policy_evidence_status": "searched",
        }
        res = Reranker(state)

        self.assertEqual(res["rerank_status"], "skip")
        self.assertLess(res["rerank_score"], 0.15)
        self.assertEqual(len(res["relevant_docs"]), 0)
        self.assertTrue(len(res["answer"]) > 0)
        verdict = res["answer"][0]
        self.assertTrue(verdict["compliant"])
        self.assertEqual(verdict["policy_source"], "NO INTERNAL POLICY FOUND")

    def test_empty_retrieval_requires_review_when_policy_search_is_unknown(self):
        state = {
            "clause": {"text": "Vendor publicity terms", "clause_type": "Publicity"},
            "retrived_docs": [],
            "retry_count": 1,
        }
        result = Reranker(state)["answer"][0]
        self.assertFalse(result["compliant"])
        self.assertEqual(result["policy_source"], "POLICY_RETRIEVAL_FAILED")

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

    def test_extracted_text_removes_markdown_and_page_artifacts(self):
        source = "## 2. Insurance\n\n[Page 3]\nVendor shall maintain coverage.\n\n"
        self.assertEqual(
            clean_extracted_text(source),
            "2. Insurance\n\nVendor shall maintain coverage.",
        )

    def test_quote_cleanup_preserves_verbatim_words_and_verifies(self):
        doc = Document(
            page_content=(
                "2. Payment Terms\n"
                "Invoices shall be paid within 45 days of receipt."
            ),
            metadata={"source": "Policy.pdf"},
        )
        formatted_quote = "## 2. Payment Terms\nInvoices shall be paid within 45 days of receipt."
        verified, cleaned = verify_citation(formatted_quote, [doc])
        self.assertTrue(verified)
        self.assertEqual(
            cleaned,
            "2. Payment Terms\nInvoices shall be paid within 45 days of receipt.",
        )
        self.assertEqual(clean_quote_formatting(formatted_quote), cleaned)

    def test_citation_verifier_detects_hallucination(self):
        """Citation verifier catches hallucinated quotes not in retrieved documents."""
        doc = Document(
            page_content="Invoices shall be payable Net 30 days from receipt.",
            metadata={"source": "Policy.pdf"},
        )
        fake_quote = "Vendor shall pay a penalty of ten thousand dollars for each day of delay."
        verified, clean_quote = verify_citation(fake_quote, [doc])
        self.assertFalse(verified)
        bad_result = {
            "clause_text": "Late penalties",
            "reason": "Vendor violated penalty terms.",
            "exact_policy_quote": fake_quote,
        }
        annotated = verify_compliance_result(bad_result, [doc])
        self.assertFalse(annotated["citation_verified"])
        self.assertIn("[CITATION UNVERIFIED]", annotated["reason"])

    def test_short_citation_still_requires_source_match(self):
        """Even a short displayed quote must be present in the policy source."""
        self.assertTrue(
            verify_citation("OK", [Document(page_content="OK")])[0]
        )
        self.assertFalse(
            verify_citation("OK", [Document(page_content="Not applicable")])[0]
        )

    def test_policy_topic_status_distinguishes_silence_from_missing_evidence(self):
        """A policy topic found in the corpus is not treated as verified silence."""
        self.assertTrue(
            policy_topic_matches(
                "Insurance",
                "Insurance Requirements",
                "Vendor shall maintain coverage.",
            )
        )
        self.assertFalse(
            policy_topic_matches(
                "Insurance",
                "Indemnification",
                "Vendor shall indemnify and defend.",
            )
        )

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
            self.assertEqual(len(alerts["missing_clauses"]), 1)
            missing = alerts["missing_clauses"][0]
            self.assertTrue(missing["is_missing_clause"])
            self.assertIn("Confidentiality", missing["clause_title"])
            self.assertFalse(missing["compliant"])
            self.assertEqual(missing["clause_text"], "[OMITTED FROM VENDOR CONTRACT]")
            self.assertIn("Mutual 5-year NDA", missing["exact_policy_quote"])

    def test_missing_clause_checker_deduplicates_parent_child_policy_sections(self):
        store = SQLiteVectorStore(db_path=":memory:")
        sections = [
            Chunk(
                id="parent",
                org_id="nexify",
                doc_id="policy-doc",
                chunk_type="parent",
                text="Data Security & Breach Notification: Vendors must notify Nexify within 24 hours.",
                breadcrumb="[Doc: Policy | Section 3: Data Security & Breach Notification]",
                metadata={
                    "heading": "Data Security & Breach Notification",
                    "heading_num": "3",
                    "doc_name": "Policy.pdf",
                },
            ),
            Chunk(
                id="child",
                org_id="nexify",
                doc_id="policy-doc",
                parent_id="parent",
                chunk_type="child",
                text="Data Security & Breach Notification: Vendors must notify Nexify within 24 hours.",
                breadcrumb="[Doc: Policy | Section 3: Data Security & Breach Notification]",
                metadata={
                    "heading": "Data Security & Breach Notification",
                    "heading_num": "3",
                    "doc_name": "Policy.pdf",
                },
            ),
            Chunk(
                id="audit",
                org_id="nexify",
                doc_id="policy-doc",
                chunk_type="parent",
                text="Audit Rights: Nexify may audit vendor records.",
                breadcrumb="[Doc: Policy | Section 5: Audit Rights]",
                metadata={
                    "heading": "Audit Rights",
                    "heading_num": "5",
                    "doc_name": "Policy.pdf",
                },
            ),
        ]
        store.add_chunks("nexify", sections)
        state = {
            "org_id": "nexify",
            "extracted_clauses": [],
        }
        with patch("pipeline.nodes.missing_clause_checker.get_vector_store", return_value=store):
            first = missing_clause_checker(state)["missing_clauses"]
            second = missing_clause_checker(state)["missing_clauses"]
        merged = _missing_clauses_reducer(first, second)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(merged), 2)
        self.assertEqual(
            {finding["policy_requirement_id"] for finding in merged},
            {"policy-doc:3", "policy-doc:5"},
        )
        self.assertTrue(all(finding["exact_policy_quote"] for finding in merged))

    def test_missing_clause_reducer_keeps_distinct_requirements_with_similar_text(self):
        findings = [
            {
                "policy_requirement_id": "policy:3",
                "clause_title": "Missing: Security",
                "exact_policy_quote": "Notify within 24 hours.",
            },
            {
                "policy_requirement_id": "policy:4",
                "clause_title": "Missing: Security",
                "exact_policy_quote": "Notify within 24 hours.",
            },
        ]
        self.assertEqual(len(_missing_clauses_reducer([], findings)), 2)

    def test_supplied_sample_has_five_clauses_and_two_unique_missing_findings(self):
        root = os.path.dirname(os.path.dirname(__file__))
        contract, used_fallback = parse_vendor_contract(
            Path(root, "data", "Vendor_Contract_Sample.pdf").read_bytes(),
            "Vendor_Contract_Sample.pdf",
        )
        policy = parse_policy_pdf(
            Path(root, "data", "Google_Company_Policy.pdf").read_bytes(),
            "Google_Company_Policy.pdf",
            "nexify",
        )
        store = SQLiteVectorStore(db_path=":memory:")
        store.add_chunks("nexify", policy)
        with patch("pipeline.nodes.missing_clause_checker.get_vector_store", return_value=store):
            findings = missing_clause_checker(
                {"org_id": "nexify", "extracted_clauses": contract}
            )["missing_clauses"]

        self.assertFalse(used_fallback)
        self.assertEqual(len(contract), 5)
        self.assertEqual(len(findings), 2)
        requirement_ids = {
            finding["policy_requirement_id"] for finding in findings
        }
        self.assertEqual(len(requirement_ids), 2)
        self.assertTrue(
            all(requirement_id.endswith((":3", ":5")) for requirement_id in requirement_ids)
        )
        self.assertTrue(
            all(
                finding["citation_verified"]
                and finding["exact_policy_quote"]
                and finding["policy_evidence_status"] == "topic_found"
                for finding in findings
            )
        )
    def test_batch_normalization_preserves_authoritative_clause_and_policy_topic(self):
        """Compact provider output cannot replace source clause text or cross-topic evidence."""
        item = {
            "clause": {
                "text": "Vendor shall maintain general liability insurance of $500,000.",
                "clause_type": "Insurance Requirements",
            }
        }
        liability_doc = Document(
            page_content="A vendor's maximum contractual liability cap must not be less than fees paid.",
            metadata={"source": "Policy.pdf", "heading": "Limitation of Liability"},
        )
        result = _normalize_batch_result(
            {
                "clause_id": 1,
                "status": "Compliant",
                "reason": "No insurance requirement was found.",
                "exact_policy_quote": None,
            },
            item,
            [],
        )
        self.assertTrue(result["compliant"])
        self.assertEqual(result["clause_text"], item["clause"]["text"])
        self.assertEqual(result["policy_source"], "Unknown")

        with self.assertRaises(ValueError):
            _normalize_batch_result(
                {
                    "status": "unknown",
                    "compliant": True,
                    "reason": "invalid",
                },
                item,
                [liability_doc],
            )

    def test_insurance_clause_does_not_use_liability_policy_as_evidence(self):
        """A liability-cap result must not be used to reject an insurance clause."""
        item = {
            "clause": {
                "text": "Vendor shall maintain general liability insurance of $500,000.",
                "clause_type": "Insurance",
            },
            "policy_evidence_status": "searched",
            "relevant_docs": [
                Document(
                    page_content="A vendor's maximum contractual liability cap must not be less than fees paid.",
                    metadata={"source": "Policy.pdf", "heading": "Limitation of Liability"},
                )
            ],
        }
        mock_model = MagicMock()
        with patch("pipeline.nodes.compliance_checker.get_cache") as cache_factory, \
             patch("pipeline.nodes.compliance_checker.micro_batch_compliance_model", mock_model):
            cache_factory.return_value.get_verdict.return_value = None
            result = evaluate_micro_batch([item])
        self.assertEqual(result[0]["policy_source"], "NO INTERNAL POLICY FOUND")
        self.assertTrue(result[0]["compliant"])
        self.assertIn("checked internal policy contains no applicable rule", result[0]["reason"])

    def test_favorable_payment_deviation_is_compliant_in_single_path(self):
        item = {
            "clause": {
                "text": (
                    "Nexify Solutions agrees to pay all undisputed invoices within "
                    "90 days of receipt. Nexify is entitled to a 5% discount if paid "
                    "within 15 days."
                ),
                "clause_type": "Payment Terms",
            },
            "relevant_docs": [
                Document(
                    page_content=(
                        "Nexify shall pay valid invoices within 45 days of receipt. "
                        "Early payment discounts are encouraged and may be negotiated."
                    ),
                    metadata={"source": "Policy.pdf", "heading": "Payment Terms"},
                )
            ],
        }
        response = MagicMock()
        response.model_dump.return_value = {
            "clause_text": item["clause"]["text"],
            "policy_source": "Policy.pdf",
            "reason": (
                "The 90-day payment window gives Nexify additional time to pay. "
                "The 5% discount is consistent with the policy's encouragement "
                "of early-payment discounts."
            ),
            "compliant": True,
            "clauses_internal": [],
            "suggested_revision": None,
            "exact_policy_quote": (
                "Nexify shall pay valid invoices within 45 days of receipt. "
                "Early payment discounts are encouraged and may be negotiated."
            ),
        }
        with patch("pipeline.nodes.compliance_checker.get_cache") as cache_factory, \
             patch("pipeline.nodes.compliance_checker.compliance_model", MagicMock(invoke=MagicMock(return_value=response))):
            cache_factory.return_value.get_verdict.return_value = None
            result = compliance_checker(item)["answer"][0]
        self.assertTrue(result["compliant"])
        self.assertTrue(result["citation_verified"])
        self.assertIsNone(result["suggested_revision"])

    def test_punitive_compounding_fee_remains_non_compliant(self):
        item = {
            "clause": {
                "text": (
                    "Nexify may pay invoices within 90 days, but late payment incurs "
                    "a 5% weekly compounded penalty fee."
                ),
                "clause_type": "Payment Terms",
            },
            "relevant_docs": [
                Document(
                    page_content=(
                        "Valid invoices are payable within 45 days. Punitive "
                        "compounding late fees are not accepted."
                    ),
                    metadata={"source": "Policy.pdf", "heading": "Payment Terms"},
                )
            ],
        }
        response = MagicMock()
        response.model_dump.return_value = {
            "clause_text": item["clause"]["text"],
            "policy_source": "Policy.pdf",
            "reason": "Compounding late fees violate policy.",
            "compliant": False,
            "clauses_internal": [],
            "suggested_revision": "Remove the penalty.",
            "exact_policy_quote": "Punitive compounding late fees are not accepted.",
        }
        with patch("pipeline.nodes.compliance_checker.get_cache") as cache_factory, \
             patch("pipeline.nodes.compliance_checker.compliance_model", MagicMock(invoke=MagicMock(return_value=response))):
            cache_factory.return_value.get_verdict.return_value = None
            result = compliance_checker(item)["answer"][0]
        self.assertFalse(result["compliant"])

    def test_single_and_batch_payment_paths_agree(self):
        item = {
            "clause": {
                "text": "Pay undisputed invoices within 90 days and receive a 5% discount within 15 days.",
                "clause_type": "Payment Terms",
            },
            "relevant_docs": [
                Document(
                    page_content=(
                        "Invoices are payable within 45 days of receipt. Early payment "
                        "discounts are encouraged and may be negotiated."
                    ),
                    metadata={"source": "Policy.pdf", "heading": "Payment Terms"},
                )
            ],
        }
        single_response = MagicMock()
        single_response.model_dump.return_value = {
            "clause_text": item["clause"]["text"],
            "policy_source": "Policy.pdf",
            "reason": "The longer payment period is favorable and the discount is encouraged.",
            "compliant": True,
            "clauses_internal": [],
            "suggested_revision": None,
            "exact_policy_quote": (
                "Invoices are payable within 45 days of receipt. Early payment "
                "discounts are encouraged and may be negotiated."
            ),
        }
        batch_item = MagicMock()
        batch_item.model_dump.return_value = {
            "clause_id": 1,
            "status": "Compliant",
            "reason": "The longer payment period is favorable and the discount is encouraged.",
            "suggested_revision": None,
            "exact_policy_quote": (
                "Invoices are payable within 45 days of receipt. Early payment "
                "discounts are encouraged and may be negotiated."
            ),
        }
        batch_response = MagicMock(results=[batch_item])
        with patch("pipeline.nodes.compliance_checker.get_cache") as cache_factory, \
             patch("pipeline.nodes.compliance_checker.compliance_model", MagicMock(invoke=MagicMock(return_value=single_response))), \
             patch("pipeline.nodes.compliance_checker.micro_batch_compliance_model", MagicMock(invoke=MagicMock(return_value=batch_response))):
            cache_factory.return_value.get_verdict.return_value = None
            single = compliance_checker(item)["answer"][0]
            batch = evaluate_micro_batch([item])[0]
        self.assertTrue(single["compliant"])
        self.assertTrue(batch["compliant"])

    def test_synthetic_expected_verdict_is_removed_but_contract_text_is_preserved(self):
        """Benchmark labels must not enter extraction or compliance inputs."""
        text = (
            "2. Insurance (Edge Case: Harms Nexify -> Should be Non-compliant)\n"
            "Vendor shall maintain general liability insurance of $500,000.\n"
            "The vendor must provide certificates upon request."
        )
        cleaned = remove_synthetic_expected_annotations(text)
        self.assertNotIn("Edge Case", cleaned)
        self.assertNotIn("Should be", cleaned)
        self.assertIn("$500,000", cleaned)
        self.assertIn("certificates upon request", cleaned)

    def test_insurance_relevance_excludes_indemnification_evidence(self):
        """Neither liability caps nor indemnification can justify an insurance verdict."""
        item = {
            "clause": {
                "text": (
                    "2. Insurance (Edge Case: Harms Nexify -> Should be Non-compliant)\n"
                    "Vendor shall maintain general liability insurance of $500,000."
                ),
                "clause_type": "Insurance",
            },
            "policy_evidence_status": "searched",
            "relevant_docs": [
                Document(
                    page_content=(
                        "Vendors must indemnify, defend, and hold harmless Nexify "
                        "against claims arising from negligence."
                    ),
                    metadata={"source": "Policy.pdf", "heading": "Indemnification"},
                ),
                Document(
                    page_content="A vendor's maximum contractual liability cap must not be less than fees paid.",
                    metadata={"source": "Policy.pdf", "heading": "Limitation of Liability"},
                ),
            ],
        }
        with patch("pipeline.nodes.compliance_checker.get_cache") as cache_factory:
            cache_factory.return_value.get_verdict.return_value = None
            result = evaluate_micro_batch([item])[0]
        self.assertTrue(result["compliant"])
        self.assertEqual(result["policy_source"], "NO INTERNAL POLICY FOUND")
        self.assertNotIn("Edge Case", result["clause_text"])

    def test_prompt_keeps_favorable_payment_distinct_from_silent_policy(self):
        """The payment rule must require policy-grounded favorable-deviation reasoning."""
        prompt = COMPLIANCE_SYSTEM_PROMPT.format(company_name="Nexify")
        self.assertIn("FAVORABLE DEVIATION", prompt)
        self.assertIn("covered topic", prompt)
        self.assertIn("do not describe the topic as silent", prompt)
        self.assertIn("Evaluate any late-payment penalty separately", prompt)
        self.assertIn("PAYMENT EVIDENCE", prompt)
        self.assertIn(
            "differs from the standard [policy payment term] but is treated as a favorable deviation",
            prompt,
        )

    def test_silent_policy_output_uses_verified_no_policy_marker(self):
        """Verified silence is explicit; retrieval failure remains manual review."""
        from pipeline.nodes.compliance_checker import _no_policy_result, _retrieval_review_result

        item = {
            "clause": {
                "text": "Vendor may use the customer's logo.",
                "clause_type": "Brand Usage",
            },
            "relevant_docs": [],
        }
        silent = _no_policy_result(item)
        failed = _retrieval_review_result(item)
        self.assertTrue(silent["compliant"])
        self.assertEqual(silent["policy_source"], "NO INTERNAL POLICY FOUND")
        self.assertFalse(failed["compliant"])
        self.assertEqual(failed["policy_source"], "POLICY_RETRIEVAL_FAILED")
        self.assertEqual(silent["policy_evidence_status"], "silence_verified")
        self.assertEqual(failed["policy_evidence_status"], "retrieval_failed")

    def test_manual_retrieval_preserves_dynamic_evidence_status(self):
        from pipeline.nodes.retriever import document_retriever

        section = Chunk(
            org_id="custom-org",
            chunk_type="parent",
            text="Retention rules require records to be kept for seven years.",
            metadata={"heading": "Records Retention"},
        )
        store = MagicMock()
        store.search_dense.return_value = []
        store.search_sparse.return_value = []
        store.get_policy_sections.return_value = [section]
        embedding_model = MagicMock()
        embedding_model.embed_query.return_value = [0.1]
        with patch("pipeline.nodes.retriever.get_vector_store", return_value=store), \
             patch("pipeline.nodes.retriever.get_embedding_model", return_value=embedding_model), \
             patch("pipeline.nodes.retriever.deduplicate_parents_max_pooling", return_value=[]):
            result = document_retriever(
                {
                    "org_id": "custom-org",
                    "clause": {
                        "clause_type": "Records Retention",
                        "text": "Vendor must retain records.",
                    },
                }
            )
        self.assertEqual(result["policy_evidence_status"], "topic_found")

    def test_payment_policy_fixture_states_negotiated_exception_rule(self):
        from tests.generate_test_pdfs import policy_content

        payment_section = next(
            section for section in policy_content if "shall pay all valid" in section
        )
        self.assertIn("negotiated exception", payment_section)

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
