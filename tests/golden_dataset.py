"""
tests/golden_dataset.py — Golden evaluation dataset for contract compliance.

Contains 30 labeled test cases spanning:
1. Compliant clauses (exact matches and clauses that benefit Nexify)
2. Non-compliant clauses (clauses that harm Nexify or contradict policy)
3. Irrelevant / Out-of-scope clauses (Zone 3 skip candidates)
4. Boundary / Ambiguous clauses (Zone 2 query reformulation candidates)
5. Omitted mandatory clauses (Bidirectional missing-clause alerts)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class GoldenTestCase:
    id: str
    category: str                   # "compliant" | "non_compliant" | "irrelevant" | "boundary" | "missing_clause"
    clause_type: str
    clause_text: str
    target_section: str            # expected policy section to match
    expected_compliant: bool
    expected_zone: str             # "zone_1_proceed" | "zone_2_retry" | "zone_3_skip"
    mandatory_keywords: list[str]
    notes: str = ""


GOLDEN_TEST_CASES: list[GoldenTestCase] = [
    # -----------------------------------------------------------------------
    # 1. Compliant Clauses (Exact Match or Benefits Nexify)
    # -----------------------------------------------------------------------
    GoldenTestCase(
        id="TC-01",
        category="compliant",
        clause_type="Payment Terms",
        clause_text="Nexify Solutions shall pay all valid and undisputed invoices within 45 days of receipt (Net 45).",
        target_section="1. Payment Terms",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["payment", "45", "invoices"],
        notes="Exact match with policy Net 45.",
    ),
    GoldenTestCase(
        id="TC-02",
        category="compliant",
        clause_type="Payment Terms",
        clause_text="Nexify Solutions agrees to pay all undisputed invoices within 90 days of receipt.",
        target_section="1. Payment Terms",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["payment", "90", "invoices"],
        notes="Net 90 extends credit period, benefiting Nexify cash flow.",
    ),
    GoldenTestCase(
        id="TC-03",
        category="compliant",
        clause_type="Payment Terms",
        clause_text="Nexify Solutions shall remit payment within 45 days. A 2% discount shall apply if paid within 10 days.",
        target_section="1. Payment Terms",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["payment", "discount", "45"],
        notes="Early payment discount is explicitly encouraged by policy.",
    ),
    GoldenTestCase(
        id="TC-04",
        category="compliant",
        clause_type="Indemnification",
        clause_text="Vendor agrees to indemnify, defend, and hold harmless Nexify Solutions against all third-party claims arising from vendor's negligence, willful misconduct, or breach of contract.",
        target_section="2. Indemnification",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["indemnify", "negligence", "breach"],
        notes="Exact match with policy indemnification requirements.",
    ),
    GoldenTestCase(
        id="TC-05",
        category="compliant",
        clause_type="Indemnification",
        clause_text="The Vendor shall fully defend and hold Nexify Solutions harmless from all losses, damages, liabilities, and expenses arising from any third-party intellectual property infringement or contractor negligence.",
        target_section="2. Indemnification",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["defend", "harmless", "negligence"],
        notes="Paraphrased indemnification protecting Nexify.",
    ),
    GoldenTestCase(
        id="TC-06",
        category="compliant",
        clause_type="Dispute Resolution",
        clause_text="Any dispute, controversy, or claim arising out of this Agreement shall be resolved by binding arbitration in New York, NY, administered by the American Arbitration Association.",
        target_section="3. Dispute Resolution",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["arbitration", "new york", "american arbitration association"],
        notes="Exact match with New York AAA arbitration policy.",
    ),
    GoldenTestCase(
        id="TC-07",
        category="compliant",
        clause_type="Insurance Requirements",
        clause_text="Vendor shall maintain commercial general liability insurance with limits of not less than $2,000,000 per occurrence throughout the term of this Agreement.",
        target_section="4. Insurance Requirements",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["insurance", "2,000,000", "liability"],
        notes="Exact match with $2,000,000 liability insurance requirement.",
    ),
    GoldenTestCase(
        id="TC-08",
        category="compliant",
        clause_type="Insurance Requirements",
        clause_text="Vendor covenants to carry comprehensive general liability insurance with a coverage limit of at least $5,000,000 per occurrence.",
        target_section="4. Insurance Requirements",
        expected_compliant=True,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["insurance", "5,000,000", "liability"],
        notes="Exceeds $2M threshold, offering greater protection to Nexify.",
    ),

    # -----------------------------------------------------------------------
    # 2. Non-Compliant Clauses (Harms Nexify or Direct Conflict)
    # -----------------------------------------------------------------------
    GoldenTestCase(
        id="TC-09",
        category="non_compliant",
        clause_type="Payment Terms",
        clause_text="Nexify Solutions shall remit all invoice payments strictly within fifteen (15) days of invoice date.",
        target_section="1. Payment Terms",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["payment", "15", "invoices"],
        notes="Net 15 significantly shortens required Net 45 period, harming Nexify.",
    ),
    GoldenTestCase(
        id="TC-10",
        category="non_compliant",
        clause_type="Payment Terms",
        clause_text="Payment must be received within 10 business days. Failure to pay will incur a 5% weekly compounded penalty fee.",
        target_section="1. Payment Terms",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["payment", "penalty", "fee"],
        notes="Short window plus punitive compounding late fees harm Nexify.",
    ),
    GoldenTestCase(
        id="TC-11",
        category="non_compliant",
        clause_type="Payment Terms",
        clause_text="Vendor requires 100% full prepayment in advance before commencement of any services.",
        target_section="1. Payment Terms",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["prepayment", "advance", "payment"],
        notes="Prepayment contradicts policy Net 45 upon receipt of valid invoice.",
    ),
    GoldenTestCase(
        id="TC-12",
        category="non_compliant",
        clause_type="Indemnification",
        clause_text="Neither party shall be obligated to indemnify or hold harmless the other party under any circumstances whatsoever.",
        target_section="2. Indemnification",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["indemnify", "harmless", "circumstances"],
        notes="Complete disclaimer of indemnification violates mandatory vendor indemnity.",
    ),
    GoldenTestCase(
        id="TC-13",
        category="non_compliant",
        clause_type="Indemnification",
        clause_text="Nexify Solutions agrees to indemnify, defend, and hold harmless Vendor from all claims, while Vendor provides zero indemnity.",
        target_section="2. Indemnification",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["indemnify", "vendor", "claims"],
        notes="Reverse one-sided indemnification puts all liability on Nexify.",
    ),
    GoldenTestCase(
        id="TC-14",
        category="non_compliant",
        clause_type="Indemnification",
        clause_text="Vendor's total indemnification liability under this Agreement shall not exceed the sum of $1,000.",
        target_section="2. Indemnification",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["indemnification", "liability", "1,000"],
        notes="De minimis liability cap eviscerates policy indemnification.",
    ),
    GoldenTestCase(
        id="TC-15",
        category="non_compliant",
        clause_type="Dispute Resolution",
        clause_text="All disputes shall be litigated exclusively in the district courts located in Tokyo, Japan, under the laws of Japan.",
        target_section="3. Dispute Resolution",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["disputes", "tokyo", "japan"],
        notes="Direct conflict with mandatory New York AAA binding arbitration.",
    ),
    GoldenTestCase(
        id="TC-16",
        category="non_compliant",
        clause_type="Dispute Resolution",
        clause_text="All controversies shall be resolved by litigation in the High Court of London, England.",
        target_section="3. Dispute Resolution",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["controversies", "london", "england"],
        notes="Direct conflict with New York arbitration.",
    ),
    GoldenTestCase(
        id="TC-17",
        category="irrelevant",
        clause_type="Insurance Requirements",
        clause_text="Vendor shall carry general liability insurance with a maximum policy coverage limit of $500,000 per occurrence.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=["insurance", "500,000", "liability"],
        notes="The supplied policy has no minimum general liability insurance limit.",
    ),
    GoldenTestCase(
        id="TC-18",
        category="irrelevant",
        clause_type="Insurance Requirements",
        clause_text="Vendor is not required to carry or maintain any commercial general liability or professional insurance policy.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=["insurance", "not required", "policy"],
        notes="The supplied policy has no insurance requirement to violate.",
    ),

    # -----------------------------------------------------------------------
    # 3. Irrelevant / Out-of-Scope Clauses (Zone 3 Skip Candidates)
    # -----------------------------------------------------------------------
    GoldenTestCase(
        id="TC-19",
        category="irrelevant",
        clause_type="Cafeteria & Catering",
        clause_text="Vendor catering staff shall adhere to cafeteria hygiene protocols and provide gluten-free lunch options upon request.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=[],
        notes="No procurement policy on catering; Zone 3 skip candidate.",
    ),
    GoldenTestCase(
        id="TC-20",
        category="irrelevant",
        clause_type="Parking Facilities",
        clause_text="Visitor parking passes shall be issued for up to three vendor vehicles in the basement parking lot.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=[],
        notes="No procurement policy on parking; Zone 3 skip candidate.",
    ),
    GoldenTestCase(
        id="TC-21",
        category="irrelevant",
        clause_type="Publicity & Marketing",
        clause_text="Vendor may include Nexify Solutions' name and logo in its public client directory upon receiving prior written consent.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=[],
        notes="No internal policy on customer directory logos; compliant by default.",
    ),
    GoldenTestCase(
        id="TC-22",
        category="irrelevant",
        clause_type="Waste Disposal",
        clause_text="All obsolete hardware and e-waste shall be discarded in accordance with ISO 14001 environmental standards.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=[],
        notes="Out-of-scope; Zone 3 skip candidate.",
    ),

    # -----------------------------------------------------------------------
    # 4. Boundary / Ambiguous Clauses (Zone 2 Retry & Subtle Semantics)
    # -----------------------------------------------------------------------
    GoldenTestCase(
        id="TC-23",
        category="boundary",
        clause_type="Invoicing Procedure",
        clause_text="Electronic invoice submission portal and detailed billing breakdown requirements for monthly services.",
        target_section="1. Payment Terms",
        expected_compliant=True,
        expected_zone="zone_2_retry",
        mandatory_keywords=["invoice", "billing"],
        notes="Ambiguous phrasing; triggers Zone 2 query reformulation.",
    ),
    GoldenTestCase(
        id="TC-24",
        category="boundary",
        clause_type="Force Majeure",
        clause_text="Neither party shall be liable for delays resulting from acts of God, war, pandemic, or government embargo.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=["force majeure", "pandemic"],
        notes="Standard contract clause not restricted by Nexify policy.",
    ),
    GoldenTestCase(
        id="TC-25",
        category="boundary",
        clause_type="Escrow Protection",
        clause_text="Source code for bespoke deliverables shall be deposited in an independent technology escrow account.",
        target_section="NO INTERNAL POLICY FOUND",
        expected_compliant=True,
        expected_zone="zone_3_skip",
        mandatory_keywords=["escrow", "source code"],
        notes="Protective clause with no contrary policy constraint.",
    ),
    GoldenTestCase(
        id="TC-26",
        category="boundary",
        clause_type="Subcontracting Notice",
        clause_text="Vendor may subcontract portions of services provided vendor remains fully liable for all acts of subcontractors.",
        target_section="2. Indemnification",
        expected_compliant=True,
        expected_zone="zone_2_retry",
        mandatory_keywords=["subcontractor", "liable"],
        notes="Related to vendor negligence liability; ambiguous initial retrieval.",
    ),

    # -----------------------------------------------------------------------
    # 5. Omitted Mandatory Clauses (Bidirectional Reverse Retrieval)
    # -----------------------------------------------------------------------
    GoldenTestCase(
        id="TC-27",
        category="missing_clause",
        clause_type="MISSING: Indemnification",
        clause_text="[OMITTED FROM CONTRACT]",
        target_section="2. Indemnification",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["indemnify", "harmless", "negligence"],
        notes="Contract that omits mandatory indemnification must trigger a missing-clause alert.",
    ),
    GoldenTestCase(
        id="TC-29",
        category="missing_clause",
        clause_type="MISSING: Dispute Resolution",
        clause_text="[OMITTED FROM CONTRACT]",
        target_section="3. Dispute Resolution",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["dispute", "arbitration", "new york"],
        notes="Contract that omits dispute resolution arbitration must trigger an alert.",
    ),
    GoldenTestCase(
        id="TC-30",
        category="missing_clause",
        clause_type="MISSING: Payment Terms",
        clause_text="[OMITTED FROM CONTRACT]",
        target_section="1. Payment Terms",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["payment", "45", "invoices"],
        notes="Contract that omits payment terms must trigger an alert.",
    ),
    GoldenTestCase(
        id="TC-31",
        category="non_compliant",
        clause_type="Indemnification",
        clause_text=(
            "Vendor shall indemnify Nexify Solutions only for third-party claims "
            "arising from gross negligence or willful misconduct."
        ),
        target_section="2. Indemnification",
        expected_compliant=False,
        expected_zone="zone_1_proceed",
        mandatory_keywords=["gross negligence", "willful misconduct"],
        notes=(
            "The clause omits ordinary negligence, the duty to defend, and breach "
            "of contract required by the supplied policy."
        ),
    ),
]


def export_golden_dataset_json(output_path: Optional[str] = None) -> Path:
    """Export the golden test cases as a clean JSON file."""
    path = Path(output_path) if output_path else Path(__file__).parent / "golden_dataset.json"
    data = [asdict(tc) for tc in GOLDEN_TEST_CASES]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


if __name__ == "__main__":
    p = export_golden_dataset_json()
    print(f"Exported {len(GOLDEN_TEST_CASES)} golden test cases to {p}")
