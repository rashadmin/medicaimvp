"""
MedicAI MVP — RAG Tool
=======================
tools/rag_tool.py

Handles retrieval-augmented generation for first-aid content.

RAG Pipeline:
  1. Embed the query using the same model used to embed the knowledge base
  2. Search the vector store for top-k relevant chunks
  3. Rerank results by relevance score
  4. Return structured content for the agent to use

The vector store is pre-populated with first-aid content from:
  - WHO first aid guidelines
  - Red Cross manuals
  - Emergency medical protocols

Replace the vector store client with your actual implementation
(Pinecone, Weaviate, Chroma, pgvector, etc.)
"""

from __future__ import annotations

import os
import asyncio
from typing import Any
import faiss, numpy as np, pickle
import pandas as pd
import httpx
from langchain_core.tools import tool
# from langgraph.config import get_stream_writer

# ── at the top of rag_tool.py ─────────────────────────────────────────────
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from dotenv import load_dotenv
load_dotenv()
_embedder = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001",
    google_api_key=os.getenv("GOOGLE_API_KEY", ""),
)


# ════════════════════════════════════════════════════════════════════════════
#  VECTOR STORE CLIENT
#  Replace with your actual vector store implementation
# ════════════════════════════════════════════════════════════════════════════

# ── replace _embed_query ──────────────────────────────────────────────────
async def _embed_query(query: str) -> list[float]:
    """Embed a query using Gemini Embedding 2 via LangChain."""
    return await _embedder.aembed_query(query)


async def _search_vector_store(
    query_embedding: list[float],
    top_k: int = 5,
    filter_tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Search your vector store for relevant first-aid content chunks.
    Replace with your actual vector store client call.

    Returns list of:
    {
        "id":      str,
        "score":   float,       ← similarity score 0-1
        "content": str,         ← the actual text chunk
        "metadata": {
            "source":   str,    ← e.g. "WHO_first_aid_2023"
            "topic":    str,    ← e.g. "cardiac_arrest"
            "tags":     list,   ← e.g. ["cpr", "chest_compressions"]
            "page":     int,
        }
    }
    """

    import os

# Get the directory of the current script/notebook
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # for .py files
    # or for Jupyter:
    # BASE_DIR = os.getcwd()
    
    faiss_path = os.path.join(BASE_DIR, "..", "index.faiss")
    df_path = os.path.join(BASE_DIR, "..", "documents.pkl")
    
    index = faiss.read_index(faiss_path)
    df = pd.read_pickle(df_path)
    # 1. Load FAISS index + df
    # index = faiss.read_index("../index.faiss")
    # df = pd.read_pickle("../documents.pkl")



    query_embedding  = np.array(query_embedding).astype('float32').reshape(1, -1)
    # 3. Search FAISS
    top_k = 5
    distances, indices = index.search(query_embedding, top_k)
    
    # 4. Link back to original df
    results = df.iloc[indices[0]].copy()
    results['distance'] = distances[0]
    await asyncio.sleep(0.1)   # simulate network call
    return [
        {
            "id":      f"chunk_{i}",
            "score":   results['distance'].to_list()[i],
            "content":     results['text'].to_list()[i],
            "metadata": {"source": results['source'].to_list()[i]},
        }
        for i in range(top_k)
    ]


def _rerank_results(results: list[dict], query: str) -> list[dict]:
    """
    Optional reranking step — sort by score and filter low-relevance chunks.
    For production use a dedicated reranker (Cohere, BGE, etc.)
    """
    return sorted(
        [r for r in results if r["score"] > 0.3],
        key=lambda x: x["score"],
        reverse=True,
    )


# ════════════════════════════════════════════════════════════════════════════
#  RAG TOOL
# ════════════════════════════════════════════════════════════════════════════

# @tool
async def search_first_aid_rag(
    query: str,
    tags: list[str] | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """
    Search the first-aid knowledge base using RAG (Retrieval-Augmented Generation).
    Use this to retrieve specific first-aid protocols, procedures, and guidelines.

    Args:
        query : specific medical/first-aid question or topic
                e.g. "CPR steps for adult cardiac arrest"
                e.g. "how to control severe bleeding from stab wound"
                e.g. "recovery position unconscious breathing patient"
        tags  : optional topic tags to filter results
                e.g. ["cardiac_arrest", "cpr"]
                e.g. ["bleeding", "trauma", "wound"]
        top_k : number of chunks to retrieve (default 5)

    Returns structured first-aid content with source citations.
    """
    # writer = get_stream_writer()
    # writer({"event": "rag_search_started", "query": query, "tags": tags})

    try:
        # step 1: embed the query
        query_embedding = await _embed_query(query)
        print(type(query_embedding))
        # step 2: search vector store
        raw_results = await _search_vector_store(
            query_embedding, top_k=top_k, filter_tags=tags
        )

        # step 3: rerank
        ranked_results = _rerank_results(raw_results, query)

        # step 4: assemble context
        context = "\n\n---\n\n".join([
            f"[Source: {r['metadata'].get('source', 'unknown')} | "
            f"Score: {r['score']:.2f}]\n{r['content']}"
            for r in ranked_results
        ])

        # writer({"event": "rag_search_complete", "query": query,
        #         "chunks_found": len(ranked_results)})

        return {
            "query":         query,
            "context":       context,
            "chunks":        ranked_results,
            "chunks_found":  len(ranked_results),
            "status":        "success",
        }

    except Exception as e:
        # writer({"event": "rag_search_failed", "query": query, "error": str(e)})
        return {
            "query":        query,
            "context":      "",
            "chunks":       [],
            "chunks_found": 0,
            "status":       "failed",
            "error":        str(e),
        }
