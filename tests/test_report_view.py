"""A missing verdict is not shown as a policy failure."""

import unittest
from pathlib import Path

from core.report_view import for_the_reader


class TestReportView(unittest.TestCase):
    def test_a_missing_status_is_not_called_a_mismatch(self):
        shown = for_the_reader({"compliant": False, "reason": "No rule was found."})
        self.assertEqual(shown["compliance_status"], "review_required")
        self.assertTrue(shown["needs_review"])
        self.assertFalse(shown["compliant"])

    def test_a_silent_policy_explanation_is_not_called_a_match(self):
        shown = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "policy_evidence_status": "topic_found",
            "citation_verified": True,
            "exact_policy_quote": "Vendors must indemnify Nexify Solutions.",
            "reason": (
                "The retrieved policy does not explicitly address the limitation of liability. "
                "Applying the silent policy rule, the clause is compliant."
            ),
        })
        self.assertEqual(shown["compliance_status"], "review_required")
        self.assertEqual(shown["reader_label"], "Needs a person to review")
        self.assertIsNone(shown["exact_policy_quote"])
        self.assertEqual(
            shown["review_reason"],
            "The company policy has no rule for this topic.",
        )

        match = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "citation_verified": True,
            "exact_policy_quote": "Pay within 45 days.",
            "reason": "The proposed payment terms match the internal policy requirement of Net 45 days.",
        })
        self.assertEqual(match["reader_label"], "Matches the policy")
        self.assertEqual(match["exact_policy_quote"], "Pay within 45 days.")

    def test_a_real_mismatch_stays_a_mismatch(self):
        shown = for_the_reader({
            "compliant": False,
            "compliance_status": "non_compliant",
            "exact_policy_quote": "Payment is due in 30 days.",
        })
        self.assertEqual(shown["compliance_status"], "non_compliant")
        self.assertFalse(shown["needs_review"])
        self.assertEqual(shown["exact_policy_quote"], "Payment is due in 30 days.")

    def test_a_placeholder_is_not_shown_as_a_quote(self):
        shown = for_the_reader({
            "compliance_status": "review_required",
            "exact_policy_quote": "SYSTEM_UNAVAILABLE",
        })
        self.assertIsNone(shown["exact_policy_quote"])

    def test_an_unverified_quote_is_not_used_as_proof(self):
        shown = for_the_reader({
            "compliant": False,
            "compliance_status": "non_compliant",
            "citation_verified": False,
            "exact_policy_quote": "Payment is due whenever we feel like it.",
            "reason": "[CITATION UNVERIFIED] The clause is too loose.",
        })
        self.assertEqual(shown["compliance_status"], "review_required")
        self.assertTrue(shown["needs_review"])
        self.assertIsNone(shown["exact_policy_quote"])
        self.assertFalse(shown["compliant"])

    def test_a_verified_quote_can_support_a_mismatch(self):
        shown = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": True,
            "exact_policy_quote": "Payment is due in 30 days.",
        })
        self.assertEqual(shown["compliance_status"], "non_compliant")
        self.assertEqual(shown["exact_policy_quote"], "Payment is due in 30 days.")
        self.assertFalse(shown["needs_review"])

    def test_a_match_stays_a_match(self):
        shown = for_the_reader({"compliance_status": "compliant", "compliant": True})
        self.assertEqual(shown["compliance_status"], "compliant")
        self.assertTrue(shown["compliant"])
        self.assertFalse(shown["needs_review"])

    def test_a_match_with_an_unverified_quote_needs_a_person(self):
        shown = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "citation_verified": False,
            "exact_policy_quote": "The vendor may pay whenever.",
            "reason": "[CITATION UNVERIFIED] Looks fine.",
            "clause_title": "Payment",
        })
        self.assertEqual(shown["compliance_status"], "review_required")
        self.assertTrue(shown["needs_review"])
        self.assertFalse(shown["compliant"])
        self.assertIsNone(shown["exact_policy_quote"])
        self.assertEqual(shown["clause_title"], "Payment")
        self.assertEqual(shown["reason"], "Looks fine.")

    def test_a_missing_citation_flag_does_not_erase_a_real_mismatch(self):
        shown = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": None,
            "exact_policy_quote": "Payment is due in 30 days.",
        })
        self.assertEqual(shown["compliance_status"], "non_compliant")
        self.assertEqual(shown["exact_policy_quote"], "Payment is due in 30 days.")
        self.assertFalse(shown["needs_review"])

    def test_blank_and_placeholder_quotes_are_removed(self):
        for quote in ("", "   ", "REVIEW_REQUIRED", "  SYSTEM_UNAVAILABLE  "):
            shown = for_the_reader({
                "compliance_status": "non_compliant",
                "exact_policy_quote": quote,
            })
            self.assertIsNone(shown["exact_policy_quote"], quote)
            self.assertEqual(shown["compliance_status"], "non_compliant")

    def test_an_unknown_status_is_not_called_a_mismatch(self):
        unknown = for_the_reader({"compliance_status": "maybe", "compliant": False})
        self.assertEqual(unknown["compliance_status"], "review_required")
        self.assertTrue(unknown["needs_review"])

        still_a_match = for_the_reader({"compliance_status": "yes", "compliant": True})
        self.assertEqual(still_a_match["compliance_status"], "compliant")
        self.assertFalse(still_a_match["needs_review"])

    def test_a_non_dict_is_left_unchanged_and_a_list_is_cleaned_item_by_item(self):
        from core.report_view import for_the_reader_list

        self.assertEqual(for_the_reader("not-a-finding"), "not-a-finding")
        shown = for_the_reader_list([
            {
                "compliance_status": "non_compliant",
                "citation_verified": False,
                "exact_policy_quote": "Invented text.",
            },
            {
                "compliance_status": "non_compliant",
                "citation_verified": True,
                "exact_policy_quote": "Payment is due in 30 days.",
            },
            {"compliant": False},
        ])
        self.assertEqual(shown[0]["compliance_status"], "review_required")
        self.assertIsNone(shown[0]["exact_policy_quote"])
        self.assertEqual(shown[1]["compliance_status"], "non_compliant")
        self.assertEqual(shown[1]["exact_policy_quote"], "Payment is due in 30 days.")
        self.assertEqual(shown[2]["compliance_status"], "review_required")

    def test_one_finding_is_accepted_without_a_list(self):
        from core.report_view import for_the_reader_list

        shown = for_the_reader_list({
            "compliance_status": "compliant",
            "citation_verified": False,
            "exact_policy_quote": "Not in the policy.",
        })
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["compliance_status"], "review_required")
        self.assertIsNone(shown[0]["exact_policy_quote"])

    def test_a_review_says_why(self):
        unverified = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": False,
            "policy_evidence_status": "topic_found",
            "exact_policy_quote": "Invented.",
        })
        self.assertEqual(
            unverified["review_reason"],
            "The quoted policy text was not found in the policy.",
        )

        silent = for_the_reader({
            "compliance_status": "review_required",
            "policy_evidence_status": "silence_verified",
        })
        self.assertEqual(
            silent["review_reason"],
            "The company policy has no rule for this topic.",
        )

        failed = for_the_reader({
            "compliance_status": "review_required",
            "policy_evidence_status": "retrieval_failed",
        })
        self.assertEqual(
            failed["review_reason"],
            "The policy search did not finish, so this cannot be decided.",
        )

        other = for_the_reader({"compliant": False})
        self.assertEqual(
            other["review_reason"],
            "A person needs to look at this before it is called a match or a mismatch.",
        )

        mismatch = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": True,
            "exact_policy_quote": "Payment is due in 30 days.",
        })
        self.assertIsNone(mismatch["review_reason"])

    def test_machine_tags_are_removed_and_a_system_error_is_not_a_bad_quote(self):
        tagged = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": False,
            "exact_policy_quote": "",
            "reason": "[CITATION UNVERIFIED - Missing Quote] No sentence was copied.",
            "policy_evidence_status": "topic_found",
        })
        self.assertEqual(tagged["reason"], "No sentence was copied.")
        self.assertNotEqual(
            tagged["review_reason"],
            "The quoted policy text was not found in the policy.",
        )

        broken = for_the_reader({
            "compliance_status": "review_required",
            "citation_verified": False,
            "exact_policy_quote": "SYSTEM_UNAVAILABLE",
            "reason": "SYSTEM ERROR: the model did not answer. Manual review required.",
        })
        self.assertEqual(
            broken["review_reason"],
            "The check could not finish, so a person needs to look.",
        )
        self.assertEqual(broken["reason"], "the model did not answer. Manual review required.")
        self.assertIsNone(broken["exact_policy_quote"])

    def test_streamlit_uses_the_same_reader(self):
        text = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn("for_the_reader", text)
        self.assertIn("reader_label", text)
        self.assertIn("summarize_findings", text)

    def test_a_missing_requirement_is_not_labeled_as_an_ordinary_mismatch(self):
        missing = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "citation_verified": True,
            "exact_policy_quote": "The vendor shall allow an annual audit.",
            "policy_evidence_status": "topic_found",
        })
        self.assertEqual(missing["reader_label"], "Not in the contract")
        self.assertEqual(missing["compliance_status"], "non_compliant")
        self.assertFalse(missing["needs_review"])
        self.assertIsNone(missing["review_reason"])

        present = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": True,
            "exact_policy_quote": "Payment is due in 30 days.",
        })
        self.assertEqual(present["reader_label"], "Does not match the policy")

        unsure = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "review_required",
            "policy_evidence_status": "topic_found",
        })
        self.assertEqual(unsure["reader_label"], "Needs a person to review")
        self.assertTrue(unsure["needs_review"])

        contradicted = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "coverage_status": "contradicted",
            "citation_verified": True,
            "exact_policy_quote": "The vendor shall not limit liability.",
        })
        self.assertEqual(contradicted["reader_label"], "Conflicts with the policy")

        partial = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "coverage_status": "partially_covered",
            "citation_verified": True,
            "exact_policy_quote": "The vendor shall allow an annual audit.",
        })
        self.assertEqual(partial["reader_label"], "Only partly in the contract")

    def test_a_machine_title_is_shown_as_the_requirement_name(self):
        shown = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "clause_title": "🚨 MISSING MANDATORY CLAUSE: Late Penalties",
            "citation_verified": True,
            "exact_policy_quote": "Pay late fees.",
        })
        self.assertEqual(shown["clause_title"], "Late Penalties")

        short = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "clause_title": "Missing: Security",
            "citation_verified": True,
            "exact_policy_quote": "Keep data encrypted.",
        })
        self.assertEqual(short["clause_title"], "Security")

        ordinary = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "clause_title": "Missing Persons",
        })
        self.assertEqual(ordinary["clause_title"], "Missing Persons")

    def test_a_placeholder_revision_is_not_shown(self):
        shown = for_the_reader({
            "compliance_status": "review_required",
            "suggested_revision": "REVIEW_REQUIRED",
        })
        self.assertIsNone(shown["suggested_revision"])

        unavailable = for_the_reader({
            "compliance_status": "non_compliant",
            "suggested_revision": "SYSTEM_UNAVAILABLE",
            "citation_verified": True,
            "exact_policy_quote": "Pay in 30 days.",
        })
        self.assertIsNone(unavailable["suggested_revision"])

    def test_a_real_mismatch_keeps_the_revision(self):
        shown = for_the_reader({
            "compliance_status": "non_compliant",
            "suggested_revision": "Change payment to 30 days.",
            "citation_verified": True,
            "exact_policy_quote": "Payment is due in 30 days.",
        })
        self.assertEqual(shown["suggested_revision"], "Change payment to 30 days.")

    def test_an_unverified_finding_does_not_keep_a_revision(self):
        shown = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": False,
            "exact_policy_quote": "Pay whenever.",
            "suggested_revision": "Pay in 30 days.",
        })
        self.assertEqual(shown["compliance_status"], "review_required")
        self.assertIsNone(shown["suggested_revision"])
        self.assertIsNone(shown["exact_policy_quote"])

    def test_the_report_summary_counts_each_label(self):
        from core.report_view import for_the_reader_report

        report = for_the_reader_report({
            "present_clauses": [
                {"compliance_status": "compliant", "compliant": True},
                {"compliance_status": "compliant", "compliant": True},
                {
                    "compliance_status": "non_compliant",
                    "citation_verified": True,
                    "exact_policy_quote": "Pay in 30 days.",
                },
                {"compliant": False},
            ],
            "missing_clauses": [
                {
                    "is_missing_clause": True,
                    "compliance_status": "non_compliant",
                    "clause_title": "🚨 MISSING MANDATORY CLAUSE: Audit",
                    "citation_verified": True,
                    "exact_policy_quote": "Allow an audit.",
                },
                {
                    "is_missing_clause": True,
                    "compliance_status": "non_compliant",
                    "coverage_status": "contradicted",
                    "citation_verified": True,
                    "exact_policy_quote": "Do not cap liability.",
                },
                {
                    "is_missing_clause": True,
                    "compliance_status": "non_compliant",
                    "coverage_status": "partially_covered",
                    "citation_verified": True,
                    "exact_policy_quote": "Allow an annual audit.",
                },
            ],
            "document_name": "vendor.pdf",
        })
        self.assertEqual(report["document_name"], "vendor.pdf")
        self.assertEqual(report["missing_clauses"][0]["clause_title"], "Audit")
        summary = report["summary"]
        self.assertEqual(summary["matches_the_policy"], 2)
        self.assertEqual(summary["does_not_match_the_policy"], 1)
        self.assertEqual(summary["needs_a_person_to_review"], 1)
        self.assertEqual(summary["not_in_the_contract"], 1)
        self.assertEqual(summary["conflicts_with_the_policy"], 1)
        self.assertEqual(summary["only_partly_in_the_contract"], 1)
        self.assertEqual(summary["total"], 7)
        self.assertEqual(
            summary["sentence"],
            "2 match the policy. 1 does not match the policy. "
            "1 is not in the contract. 1 is only partly in the contract. "
            "1 conflicts with the policy. 1 needs a person to review.",
        )

    def test_an_empty_report_says_no_findings(self):
        from core.report_view import for_the_reader_report

        self.assertEqual(for_the_reader_report("not-a-report"), "not-a-report")
        report = for_the_reader_report({"present_clauses": [], "missing_clauses": []})
        self.assertEqual(report["summary"]["sentence"], "No findings.")
        self.assertEqual(report["summary"]["total"], 0)

    def test_the_screens_show_the_summary_and_hide_machine_captions(self):
        streamlit = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        page = Path(__file__).resolve().parents[1].joinpath("apps/dashboard/index.html").read_text(encoding="utf-8")
        self.assertIn("summarize_findings", streamlit)
        self.assertNotIn("Policy evidence status", streamlit)
        self.assertNotIn("summary.sentence", page)
        self.assertIn("rememberJob", page)
        self.assertIn("Suggested revision", page)
        self.assertIn("Contract text", page)
        self.assertIn("Policy:", page)
        self.assertIn("if (missingItems.length)", page)
        self.assertIn("repeatsReview", page)
        self.assertIn("function contractText", page)
        self.assertIn("File: ", page)
        self.assertIn("document_name", page)
        self.assertIn("Policy already indexed", page)
        self.assertNotIn("Policy References", streamlit)

    def test_the_heading_is_not_repeated_as_the_contract_sentence(self):
        shown = for_the_reader({
            "compliance_status": "non_compliant",
            "clause_title": "Payment",
            "clause_text": "1. Payment\n\nNexify Solutions agrees to pay within 90 days.",
            "citation_verified": True,
            "exact_policy_quote": "Pay within 45 days.",
        })
        self.assertEqual(shown["clause_title"], "Payment")
        self.assertEqual(
            shown["clause_text"],
            "Nexify Solutions agrees to pay within 90 days.",
        )

        child = for_the_reader({
            "compliance_status": "non_compliant",
            "clause_title": "Invoicing: Pay within 15 days.",
            "clause_text": "1.1 Invoicing: Pay within 15 days.",
        })
        self.assertIsNone(child["clause_text"])
        self.assertEqual(child["clause_title"], "Invoicing: Pay within 15 days.")

        kept = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "clause_title": "Payment",
            "clause_text": "Payment is due in 30 days.",
        })
        self.assertEqual(kept["clause_text"], "Payment is due in 30 days.")

    def test_a_repeated_review_sentence_is_not_shown_twice(self):
        silent = for_the_reader({
            "compliance_status": "review_required",
            "policy_evidence_status": "silence_verified",
            "reason": (
                "The checked internal policy contains no applicable rule for this clause. "
                "Under configured tenant rules, manual review is required for silent topics."
            ),
        })
        self.assertIsNone(silent["reason"])
        self.assertEqual(
            silent["review_reason"],
            "The company policy has no rule for this topic.",
        )

        failed = for_the_reader({
            "compliance_status": "review_required",
            "policy_evidence_status": "retrieval_failed",
            "reason": (
                "Internal policy evidence could not be retrieved or inspected. "
                "Compliance cannot be established; manual review is required."
            ),
        })
        self.assertIsNone(failed["reason"])

        useful = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": False,
            "exact_policy_quote": "Pay whenever.",
            "reason": "[CITATION UNVERIFIED] The clause is too loose.",
        })
        self.assertEqual(useful["reason"], "The clause is too loose.")
        self.assertEqual(
            useful["review_reason"],
            "The quoted policy text was not found in the policy.",
        )

    def test_a_policy_breadcrumb_is_shown_as_a_place(self):
        shown = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "citation_verified": True,
            "exact_policy_quote": "Allow an annual audit.",
            "clauses_internal": [
                "Allow an annual audit. — `[Doc: handbook.pdf | Section 2: Audit Rights | Page 4]`",
                "Allow an annual audit. — `[Doc: handbook.pdf | Section 2: Audit Rights | Page 4]`",
            ],
        })
        self.assertEqual(
            shown["clauses_internal"],
            ["handbook.pdf, Section 2: Audit Rights, page 4"],
        )

    def test_an_unverified_quote_does_not_keep_a_policy_place(self):
        shown = for_the_reader({
            "compliance_status": "non_compliant",
            "citation_verified": False,
            "exact_policy_quote": "Pay whenever.",
            "clauses_internal": [
                "Pay whenever. — `[Doc: handbook.pdf | Section 1: Payment | Page 2]`",
            ],
        })
        self.assertEqual(shown["clauses_internal"], [])
        self.assertIsNone(shown["exact_policy_quote"])

    def test_a_reference_without_a_place_is_dropped(self):
        shown = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "citation_verified": True,
            "exact_policy_quote": "Pay in 30 days.",
            "clauses_internal": [
                "Pay in 30 days.",
                "Pay in 30 days. — `SYSTEM_UNAVAILABLE`",
                "Pay in 30 days. — `Billing Policy`",
            ],
        })
        self.assertEqual(shown["clauses_internal"], ["Billing Policy"])

    def test_the_omitted_placeholder_is_not_contract_text(self):
        shown = for_the_reader({
            "is_missing_clause": True,
            "compliance_status": "non_compliant",
            "clause_text": "[OMITTED FROM VENDOR CONTRACT]",
            "citation_verified": True,
            "exact_policy_quote": "Allow an annual audit.",
        })
        self.assertIsNone(shown["clause_text"])
        self.assertEqual(shown["exact_policy_quote"], "Allow an annual audit.")

    def test_a_real_contract_sentence_stays(self):
        shown = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "clause_text": "  Payment is due in 30 days.  ",
        })
        self.assertEqual(shown["clause_text"], "Payment is due in 30 days.")

    def test_a_status_string_is_not_shown_as_a_policy_file(self):
        silent = for_the_reader({
            "compliance_status": "review_required",
            "policy_source": "NO INTERNAL POLICY FOUND",
            "policy_evidence_status": "silence_verified",
        })
        self.assertIsNone(silent["policy_source"])

        failed = for_the_reader({
            "compliance_status": "review_required",
            "policy_source": "POLICY_RETRIEVAL_FAILED",
            "policy_evidence_status": "retrieval_failed",
        })
        self.assertIsNone(failed["policy_source"])

        named = for_the_reader({
            "compliance_status": "compliant",
            "compliant": True,
            "policy_source": "handbook.pdf",
        })
        self.assertEqual(named["policy_source"], "handbook.pdf")
