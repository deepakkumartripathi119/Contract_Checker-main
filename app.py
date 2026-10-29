import streamlit as st
from dotenv import load_dotenv
import os

from pipeline.nodes.document_parser import parse_vendor_contract
from pipeline.nodes.extract_clause import extract_clauses
from pipeline.nodes.retriever import process_uploaded_files

st.set_page_config(layout="wide")
st.session_state.setdefault("submitted", False)
st.session_state.setdefault("uploaded_files", [])
st.session_state.setdefault("retrievers", [])

from pipeline.graph import app
from pipeline.subgraph import sub_graph

# ---------------------------------------------------------------------------
# Load environment variables
# ---------------------------------------------------------------------------
if st.secrets:
    os.environ["LANGSMITH_TRACING"] = st.secrets.get("LANGSMITH_TRACING", "false")
    os.environ["LANGSMITH_ENDPOINT"] = st.secrets.get(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )
    os.environ["LANGSMITH_API_KEY"] = st.secrets.get("LANGSMITH_API_KEY", "")
    os.environ["LANGSMITH_PROJECT"] = st.secrets.get("LANGSMITH_PROJECT", "contract-checker")
else:
    load_dotenv()

# ---------------------------------------------------------------------------
# Sidebar — uploaded policy files & retrievers
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Uploaded Policy Files")
    if st.session_state.get("uploaded_files"):
        for file in st.session_state.uploaded_files:
            st.write(f"- 📄 {file.name}")
    else:
        st.write("No files uploaded yet.")

    st.markdown("---")
    st.header("Retrievers")
    if st.session_state.get("retrievers"):
        for retriever in st.session_state.retrievers:
            retriever_name = getattr(retriever, "name", str(retriever))
            st.write(f"- 🤖 {retriever_name}")
    else:
        st.write("No retrievers created yet.")

# ---------------------------------------------------------------------------
# Step 1 — Upload policy documents (once per session)
# ---------------------------------------------------------------------------
if not st.session_state.submitted:
    st.title("📂 Document Processing")
    st.header("Upload Policy Documents")
    st.write("Upload one or more PDF policy documents to build the retrieval index.")

    uploaded_files = st.file_uploader(
        "Choose PDF files", accept_multiple_files=True, type=["pdf"]
    )

    if uploaded_files:
        st.session_state.uploaded_files = uploaded_files
        st.write("Uploaded files:")
        for file in uploaded_files:
            st.write(f"- {file.name}")

    if st.button("Submit Documents"):
        if uploaded_files:
            retrievers = process_uploaded_files(uploaded_files)
            st.session_state.retrievers = retrievers
            st.session_state.submitted = True
            st.success("Documents processed successfully!")
        else:
            st.error("Please upload at least one file before submitting.")

# ---------------------------------------------------------------------------
# Step 2 — Contract analysis tabs
# ---------------------------------------------------------------------------
else:
    st.title("📄 Contract Clause Intelligence Suite")

    tab1, tab2 = st.tabs(["📌 Clause Compliance Checker", "📑 Clause Extractor"])

    # -----------------------------------------------------------------------
    # Tab 1: Full compliance check pipeline
    # -----------------------------------------------------------------------
    with tab1:
        input_method = st.radio(
            "Choose input method:", ("Upload PDF", "Manual Clause Input")
        )

        if input_method == "Upload PDF":
            uploaded_file = st.file_uploader("Upload a contract PDF", type="pdf")

            if uploaded_file:
                pdf_bytes = uploaded_file.read()
                doc_name = uploaded_file.name

                # Phase 1: layout-aware Markdown parsing + heading-boundary split
                # parse_vendor_contract returns (clauses, used_llm_fallback)
                clauses_raw, used_llm_fallback = parse_vendor_contract(pdf_bytes, doc_name)

                if used_llm_fallback:
                    # No headings detected — pass full text to LLM extractor
                    st.info(
                        "No section headings detected in this PDF. "
                        "Using LLM extraction as fallback."
                    )

                if not clauses_raw:
                    st.error("Could not extract any content from the uploaded PDF.")
                else:
                    # Bypass LLM extraction if we successfully parsed structure
                    if not used_llm_fallback:
                        state = {"extracted_clauses": clauses_raw}
                    else:
                        state = {"contract": [clauses_raw[0]["text"]]}

                    # max_concurrency: caps parallel SubGraph runs to avoid rate limits
                    response = app.invoke(state, config={"max_concurrency": 5})
                    data = response["answer"]

                    clause_groups = []
                    if not used_llm_fallback:
                        current_parent = None
                        for clause in clauses_raw:
                            metadata = clause.get("metadata", {})
                            if metadata.get("depth", 1) == 1:
                                current_parent = {
                                    "clause_type": clause["clause_type"],
                                    "page": str(metadata.get("page", "?")),
                                    "children": [],
                                }
                                clause_groups.append(current_parent)
                            elif current_parent is not None:
                                current_parent["children"].append(clause)

                    child_clause_count = sum(
                        len(group["children"]) for group in clause_groups
                    )

                    st.subheader("Clauses Extracted from Contract")
                    num_clauses = (
                        child_clause_count
                        if not used_llm_fallback
                        else len(response["extracted_clauses"])
                    )
                    st.write("Number of extracted clauses:", num_clauses)
                    st.info(f"✅ Checking ALL {num_clauses} clauses (no random sampling).")

                    if not used_llm_fallback:
                        for idx, group in enumerate(clause_groups, 1):
                            st.markdown(
                                f"### {idx}. {group['clause_type']} (Pages: {group['page']})"
                            )
                            for child in group["children"]:
                                st.markdown(f"> ***{child['text']}***")
                            st.markdown("---")
                    else:
                        for idx, clause in enumerate(
                            response["extracted_clauses"], 1
                        ):
                            st.markdown(f"### {idx}. {clause['clause_type']}")
                            st.markdown(f"> ***{clause['text']}***")
                            st.markdown("---")

                    st.subheader("Clause Compliance Results")

                    def render_compliance_result(clause, heading=None):
                        if heading:
                            st.markdown(heading)

                        if clause.get("is_missing_clause"):
                            st.error("**Status:** 🚨 Omitted by Vendor (Non-compliant)")
                            st.markdown(f"**Risk Analysis:** {clause['reason']}")
                        else:
                            st.markdown(
                                f"**Extracted Clause (from Input):** {clause['clause_text']}"
                            )
                            st.markdown(f"**Reason:** {clause['reason']}")

                        if clause.get("cached"):
                            st.caption(
                                f"⚡ Served from cache ({clause.get('cache_tier', 'exact')})"
                            )

                        if clause.get("exact_policy_quote"):
                            if clause.get("citation_verified") is True:
                                st.markdown(
                                    f"**Verified Policy Quote:** ✅ *\"{clause['exact_policy_quote']}\"*"
                                )
                            elif clause.get("citation_verified") is False:
                                st.markdown(
                                    f"**Policy Quote:** ⚠️ *\"{clause['exact_policy_quote']}\" (unverified)*"
                                )
                            else:
                                st.markdown(
                                    f"**Policy Quote:** *\"{clause['exact_policy_quote']}\"*"
                                )

                        if clause.get("suggested_revision"):
                            st.markdown(
                                f"**Suggested Revision:** _{clause['suggested_revision']}_"
                            )

                        if clause.get("clauses_internal"):
                            st.markdown("**Policy References:**")
                            for entry in clause.get("clauses_internal", []):
                                st.markdown(f"> *{entry}*")

                        st.markdown("---")

                    if not used_llm_fallback:
                        result_queues = {}
                        for result in data:
                            if not result.get("is_missing_clause"):
                                key = " ".join(
                                    result.get("clause_text", "").lower().split()
                                )
                                result_queues.setdefault(key, []).append(result)

                        result_index = 0
                        for idx, group in enumerate(clause_groups, 1):
                            st.markdown(
                                f"### {idx}. {group['clause_type']} (Pages: {group['page']})"
                            )
                            for child in group["children"]:
                                key = " ".join(child["text"].lower().split())
                                matching_results = result_queues.get(key, [])
                                if matching_results:
                                    result = matching_results.pop(0)
                                    compliant_icon = (
                                        "✅" if result["compliant"] else "❌"
                                    )
                                    render_compliance_result(
                                        result,
                                        f"**{compliant_icon} {result['clause_title']}**",
                                    )
                                else:
                                    st.warning(
                                        "No compliance result was returned for "
                                        f"{child['clause_type']}."
                                    )
                    else:
                        for idx, clause in enumerate(data, 1):
                            compliant_icon = "✅" if clause["compliant"] else "❌"
                            heading = (
                                f"### {idx}. {clause['clause_title']}"
                                if clause.get("is_missing_clause")
                                else f"### {idx}. {compliant_icon} {clause['clause_title']}"
                            )
                            render_compliance_result(clause, heading)

                    missing_results = [
                        clause for clause in data if clause.get("is_missing_clause")
                    ]
                    if not used_llm_fallback:
                        for result_index, clause in enumerate(
                            missing_results, child_clause_count + 1
                        ):
                            render_compliance_result(
                                clause,
                                f"### {result_index}. {clause['clause_title']}",
                            )
        else:
            clause_text = st.text_area(
                "Enter Clause Text", placeholder="Paste the clause text here..."
            )
            clause_type = st.text_input(
                "Enter Clause Type",
                placeholder="e.g., Payment Terms, Termination, Confidentiality",
            )

            if st.button("Check Compliance"):
                if not clause_text or not clause_type:
                    st.error("Please provide both clause text and clause type.")
                else:
                    state = {
                        "clause": {
                            "text": clause_text,
                            "clause_type": clause_type,
                            "metadata": {},
                        }
                    }
                    response = sub_graph.invoke(state)
                    data = response["answer"]

                    st.subheader("Clause Compliance Results")
                    st.table(
                        {
                            "Clause Title": [d["clause_title"] for d in data],
                            "Extracted Clause (from Input)": [
                                d["clause_text"] for d in data
                            ],
                            "Retrieved From": [d["policy_source"] for d in data],
                            "Compliance Logic": [d["reason"] for d in data],
                            "Status": [
                                "✅ Compliant" if d["compliant"] else "❌ Non-compliant"
                                for d in data
                            ],
                            "Suggested Revision": [
                                (
                                    d["suggested_revision"]
                                    if not d["compliant"] and d.get("suggested_revision")
                                    else "N/A"
                                )
                                for d in data
                            ],
                        }
                    )

    # -----------------------------------------------------------------------
    # Tab 2: Clause extraction only (no compliance check)
    # -----------------------------------------------------------------------
    with tab2:
        st.subheader("Upload Contract PDF to Extract Clauses")

        uploaded_file = st.file_uploader(
            "Upload PDF (Clause Extraction Only)", type="pdf", key="extractor"
        )
        if uploaded_file:
            try:
                pdf_bytes = uploaded_file.read()
                doc_name = uploaded_file.name

                # Phase 1: layout-aware parsing for Tab 2 extractor
                clauses_raw, used_llm_fallback = parse_vendor_contract(pdf_bytes, doc_name)

                if used_llm_fallback:
                    st.info(
                        "No section headings detected — using LLM extraction fallback."
                    )
                    # LLM extraction path (same as before)
                    all_extracted_clauses = []
                    full_text = clauses_raw[0]["text"] if clauses_raw else ""
                    if full_text.strip():
                        result = extract_clauses({"contract": full_text})
                        all_extracted_clauses = [
                            {**clause, "page_range": "?"}
                            for clause in result.get("extracted_clauses", [])
                        ]
                else:
                    # Group nested sections under their top-level parent.
                    all_extracted_clauses = []
                    current_parent = None
                    for clause in clauses_raw:
                        metadata = clause.get("metadata", {})
                        depth = metadata.get("depth", 1)

                        if depth == 1:
                            current_parent = {
                                "clause_type": clause["clause_type"],
                                "page_range": str(metadata.get("page", "?")),
                                "children": [],
                            }
                            all_extracted_clauses.append(current_parent)
                        elif current_parent is not None:
                            current_parent["children"].append(
                                {
                                    "text": clause["text"],
                                    "page_range": str(metadata.get("page", "?")),
                                }
                            )

                if not all_extracted_clauses:
                    st.warning("⚠️ No clauses were extracted from the document.")
                else:
                    st.success(
                        f"✅ {len(all_extracted_clauses)} parent clauses extracted."
                    )

                    for idx, clause in enumerate(all_extracted_clauses, 1):
                        st.markdown(
                            f"### {idx}. {clause.get('clause_type', 'Unknown')} (Pages: {clause['page_range']})"
                        )
                        children = clause.get("children", [])
                        if children:
                            for child in children:
                                st.markdown(f"> ***{child['text']}***")
                        else:
                            st.markdown(
                                f"> ***{clause.get('text', 'No text found.')}***"
                            )
                        st.markdown("---")

            except Exception as e:
                st.error(f"🚨 Error while processing the file: {e}")