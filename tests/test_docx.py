"""A .docx uses the same heading split as a PDF. The old .doc format does not."""

import io
import unittest
import zipfile

from fastapi import HTTPException

from pipeline.nodes.document_parser import parse_vendor_contract
from services.api import _pdf_name


def _docx(paragraphs: list[tuple[str | None, str]]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = []
    for style, text in paragraphs:
        style_xml = ""
        if style:
            style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        body.append(
            f'<w:p>{style_xml}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


class TestDocx(unittest.TestCase):
    def test_a_numbered_docx_paragraph_is_a_clause(self):
        data = _docx([
            (None, "1. Payment"),
            (None, "Pay within 45 days."),
        ])
        clauses, used_fallback = parse_vendor_contract(data, "policy.docx")
        self.assertFalse(used_fallback)
        self.assertEqual(clauses[0]["clause_type"], "Payment")
        self.assertIn("Pay within 45 days.", clauses[0]["text"])

    def test_a_word_heading_without_a_number_is_still_a_section(self):
        data = _docx([
            ("Heading1", "Payment"),
            (None, "Pay within 45 days."),
        ])
        clauses, used_fallback = parse_vendor_contract(data, "policy.docx")
        self.assertFalse(used_fallback)
        self.assertEqual(clauses[0]["clause_type"], "Payment")

    def test_the_old_doc_format_is_rejected(self):
        class Upload:
            filename = "contract.doc"

        with self.assertRaises(HTTPException) as caught:
            _pdf_name(Upload())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn(".docx", caught.exception.detail)

        class Docx:
            filename = "contract.docx"

        self.assertEqual(_pdf_name(Docx()), "contract.docx")
