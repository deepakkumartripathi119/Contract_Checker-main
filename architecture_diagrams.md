# Contract Checker: Actual Current Design vs. Proposed New Design

> **Why these differ from the previous conversation's diagrams:** The previous agent couldn't read your code (compressed .zip). Now that I've read every file, here are the corrections.

---

## Current Design (What Your Code ACTUALLY Does Today)

> [!WARNING]
> Key corrections from the previous conversation:
> - LLM is **Groq (`qwen/qwen3.8-27b`)**, not Gemini
> - Only **5 randomly-sampled clauses** are checked, not 30
> - Embeddings are **Cohere (`embed-english-v3.0`)**, not Google
> - `RAG.py` is dead code — active retrieval is in `retriever.py`
> - `COHERE_API_KEY` has a **trailing space bug** in `retriever.py` line 21

```mermaid
flowchart TD
    subgraph OLD ["CURRENT DESIGN (Verified from Code)"]
        direction TB
        O_PDF["Vendor Contract PDF"] --> O_Split["app.py lines 91-104:\nSplit into 4-Page Chunks\n(Hard page boundary — clauses can be sliced in half!)"]
        
        O_Split -->|"Fan-Out #1\nextract_clause_sender.py\nSend('extract_clauses', chunk)"| O_Ext["Extract_clause.py\n(Groq: qwen/qwen3.8-27b, temp=0)\nwith_structured_output(ExtractedClauses)\n5-retry loop\nHardcoded to 8 clause types only"]
        
        O_Ext -->|"Produces N clauses\n(e.g., 15-30)"| O_PassThru["clause_sender_node.py\n(Empty passthrough: return {})"]
        
        O_PassThru -->|"clause_sender.py:\nrandom.sample(clauses, min(5, len))\n⚠️ RANDOMLY DROPS all but 5!"| O_Sub["Max 5 Parallel Subgraphs\n(subgraph.py × 5)"]
        
        subgraph O_Worker ["Inside EACH of the 5 Randomly-Chosen Clauses"]
            direction TB
            O_QG["1. query_generator.py\n(Groq LLM call per clause)\nwith_structured_output(query)\n3-retry loop"]
            O_Ret["2. retriever.py: document_retriever()\nIterates ALL ensemble retrievers in global list\nEach retriever = EnsembleRetriever(\n  FAISS k=3 + BM25 k=3,\n  weights=[0.5, 0.5]\n)\nChunks: RecursiveCharacterTextSplitter(\n  chunk_size=1000, chunk_overlap=0)"]
            O_Rerank["3. Reranker.py\nCohereRerank('rerank-english-v3.0')\nReranks by CLAUSE TEXT (not generated query)\nHardcoded top_indices[:2]\n⚠️ NO score threshold — always sends top 2"]
            O_Check["4. Compiliance_checker.py\n(Groq LLM call per clause)\nwith_structured_output(ClauseComplianceResult)\n⚠️ No deterministic citation verification"]
            O_QG --> O_Ret --> O_Rerank --> O_Check
        end
        
        O_Sub --> O_Worker
        O_Worker --> O_Out["Final Report in Streamlit\n(Only 5 of N clauses checked)\n(No persistence — lost on page refresh)"]
    end
```

### Actual API calls per contract (5 clauses checked):

| Stage | API Calls | Provider |
|---|---|---|
| Clause Extraction | 1-3 Groq calls (per 4-page chunk) | Groq |
| Query Generation | 5 Groq calls (1 per clause) | Groq |
| Embedding (inside FAISS retriever) | 5 Cohere calls (1 per query) | Cohere |
| Reranking | 5 Cohere calls (1 per clause) | Cohere |
| Compliance Check | 5 Groq calls (1 per clause) | Groq |
| **Total** | **~21-23 API calls** | Mixed |

---

## Proposed New Design (The 6 Agreed Upgrades)

```mermaid
flowchart TD
    subgraph NEW ["NEW DESIGN: Vectorized Batch + Smart Parallelism"]
        direction TB
        
        N_PDF["Vendor Contract PDF"] --> N_Parse["1. Layout-Aware Markdown Parser\n(PyMuPDF4LLM / Docling)\nPreserves headings, tables, numbering\n0 LLM calls — fully deterministic"]
        
        N_Parse --> N_Context["2. Contextual Chunking\nSplit by clause headings (not char count)\nPrepend breadcrumb headers:\n'[Section 7: Liability | Clause 7.2]'\nBuild Parent-Child index\n(Search small children → return full parent)"]
        
        N_Context -->|"ALL N clauses\n(no random dropping!)"| N_Cache{"3. Two-Tier Cache Check\nTier 1: SHA256(policy_hash + clause_hash)\nTier 2: Cosine ≥ 0.97\n+ regex number/negation guard"}
        
        N_Cache -->|"Cache Hit\n(~10 boilerplate clauses)"| N_CacheResult["Return cached verdict\nin < 1ms, $0 cost"]
        
        N_Cache -->|"Cache Miss\n(~20 unseen clauses)"| N_BatchEmb["4. Batch Embedding\n1 API call:\nembed_documents([all 20 clauses])\n(Eliminates query_generator.py\nfor ~85% of clauses)"]
        
        N_BatchEmb --> N_RRF["5. Weighted Reciprocal Rank Fusion\nRRF(d) = w_bm25/(k + rank_bm25)\n       + w_faiss/(k + rank_faiss)\nTunable weights, not fixed 0.5/0.5\nMetadata-filtered by clause category"]
        
        N_RRF --> N_Dedup["6. Parent Deduplication\nIf children 7.1, 7.2, 7.3 all match:\n→ Collapse to Parent Section 7\n→ Score = max(child scores)\n  not average"]
        
        N_Dedup -->|"Bounded: max_concurrency=5"| N_Rerank["7. Reranker + 3-Zone Gate\nCohereRerank or local CrossEncoder"]
        
        N_Rerank -->|"Zone 3: score < 0.15"| N_Skip["Deterministic:\n'NO_APPLICABLE_POLICY'\nSkip LLM entirely"]
        
        N_Rerank -->|"Zone 2: 0.15 ≤ score < 0.35\n(~2-3 ambiguous clauses)"| N_Fallback["Conditional query_generator.py\n(Only 2-3 LLM calls as fallback)"]
        N_Fallback --> N_RRF
        
        N_Rerank -->|"Zone 1: score ≥ 0.35\n(~12 confident matches)"| N_Verify["8. Micro-Batched Compliance Check\n4-5 clauses per prompt\nPydantic structured output:\n  exact_policy_quote (verified)\n  policy_section_id\n  compliance_status\n  suggested_redline"]
        
        N_Verify --> N_Citation["9. Deterministic Citation Verifier\nnormalize(quote) in normalize(chunk)\nIf FALSE → catch hallucination\nbefore user sees it"]
        
        N_Citation --> N_Bidir["10. Bidirectional Missing-Clause Check\nReverse retrieval:\nFor each mandatory policy rule →\nsearch vendor contract\nCatch OMITTED clauses\n(what one-way RAG misses)"]
        
        N_Skip & N_CacheResult & N_Bidir --> N_Out["Final Verified Report\n100% of clauses checked\nDeterministic citations\nMissing-clause alerts\nPersisted to DB"]
    end
```

### Projected API calls per contract (ALL 30 clauses checked):

| Stage | Old (5 random clauses) | New (ALL 30 clauses) | Reduction |
|---|---|---|---|
| Clause Extraction | 1-3 LLM calls | **0** (deterministic parser) | **100%** |
| Query Generation | 5 LLM calls | **2-3** (fallback only) | **50-60%** |
| Embedding | 5 HTTP calls | **1** (batched) | **80%** |
| Reranking | 5 Cohere calls | **~4** (bounded, some skipped) | **20%** |
| Compliance Check | 5 LLM calls | **3-4** (micro-batched, cached skip) | **30%** |
| **Total** | **~21-23** (only 5 clauses!) | **~10-12** (ALL 30 clauses!) | **~50% fewer calls, 6× more clauses checked** |

---

## Side-by-Side: What Each Upgrade Fixes

| Problem in Current Code | File & Line | Upgrade That Fixes It |
|---|---|---|
| Hard 4-page split slices clauses in half | [app.py L91-93](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/app.py#L91-L93) | #1: Layout-aware Markdown parsing |
| `chunk_size=1000, chunk_overlap=0` loses headings | [retriever.py L47](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/retriever.py#L47) | #2: Contextual breadcrumb headers + parent-child |
| `random.sample(clauses, min(5, len))` drops clauses | [clause_sender.py L9-10](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/clause_sender.py#L9-L10) | Remove random sampling; check ALL clauses |
| 1 LLM call per clause in `query_generator.py` | [query_generator.py L29](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/query_generator.py#L29) | #4: Batch embedding; query gen as fallback only |
| `weights=[0.5, 0.5]` fixed ensemble (no tuning) | [retriever.py L65](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/retriever.py#L65) | #5: Explicit Weighted RRF with tunable weights |
| `reranked_docs[:2]` with no score threshold | [Reranker.py L53](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/Reranker.py#L53) | #7: 3-Zone confidence gate |
| No citation verification in compliance output | [Compiliance_checker.py L79-89](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/Compiliance_checker.py#L79-L89) | #9: Deterministic substring verifier |
| One-way check (misses omitted mandatory clauses) | Structural gap | #10: Bidirectional reverse retrieval |
| `COHERE_API_KEY ` trailing space bug | [retriever.py L21](file:///c:/Users/deepa/OneDrive/Desktop/projects/Contract_Checker-main/Contract_Checker-main/retriever.py#L21) | Fix env var key string |
| Dead code: `RAG.py`, `Retrivers.py` | Not imported anywhere | Delete both files |

> [!IMPORTANT]
> **Key difference from previous conversation:** The current design actually processes **~21 API calls for 5 random clauses**, not "125+ for 30 clauses" as the previous agent claimed. The new design processes **~10-12 API calls for ALL 30 clauses** — meaning we check **6× more clauses with fewer API calls**.
