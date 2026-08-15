from concurrent.futures import ThreadPoolExecutor
from time import sleep

from lighting_agent.document_loader import load_document
from lighting_agent.rag import ChromaEvidenceStore, LocalEvidenceStore, StoredChunk, chroma_keyword_terms, stable_chunk_id


def test_local_rag_returns_source_and_locator(tmp_path) -> None:
    source = tmp_path / "standard.md"
    source.write_text("Meeting room maintained illuminance is 500 lx. CRI must be at least 80.", encoding="utf-8")
    document = load_document(source, allowed_root=tmp_path)
    store = LocalEvidenceStore(tmp_path / "index.json")

    assert store.add_document(document, source_type="standard") == 1
    results = store.search("meeting room illuminance cri")

    assert len(results) == 1
    assert results[0].source_name == "standard.md"
    assert results[0].locator == "chunk 1"
    assert "500 lx" in results[0].excerpt


class FakeDocument:
    def __init__(self, page_content: str, metadata: dict) -> None:
        self.page_content = page_content
        self.metadata = metadata


class FakeChromaVectorStore:
    def similarity_search_with_relevance_scores(self, _query: str, *, k: int):
        assert k == 1
        return [(FakeDocument("A general room requires 300 lx.", {"chunk_id": "semantic", "locator": "chunk 1"}), 0.9)]

    def get(self, *, where_document, include):
        assert include == ["documents", "metadatas"]
        if where_document:
            return {
                "documents": ["A video meeting room requires 750 lx."],
                "metadatas": [{"chunk_id": "video", "source_name": "GB 50034-2024.md", "source_type": "standard", "locator": "Table 5.3.1"}],
            }
        return {"documents": [], "metadatas": []}


def test_chroma_keyword_lookup_recovers_long_table_chunks() -> None:
    store = object.__new__(ChromaEvidenceStore)
    store.vector_store = FakeChromaVectorStore()
    query = "\u89c6\u9891\u4f1a\u8bae\u5ba4\u7684\u7167\u5ea6\u6807\u51c6\u662f\u591a\u5c11"

    results = store.search(query, top_k=1)

    assert "\u89c6\u9891\u4f1a\u8bae\u5ba4" in chroma_keyword_terms(query)
    assert results[0].source_name == "GB 50034-2024.md"
    assert results[0].locator == "Table 5.3.1"
    assert "750 lx" in results[0].excerpt


def test_chroma_search_serializes_non_reentrant_embedding_access() -> None:
    class NonReentrantVectorStore(FakeChromaVectorStore):
        def __init__(self) -> None:
            self.in_use = False

        def similarity_search_with_relevance_scores(self, query: str, *, k: int):
            if self.in_use:
                raise RuntimeError("Already borrowed")
            self.in_use = True
            try:
                sleep(0.02)
                return super().similarity_search_with_relevance_scores(query, k=k)
            finally:
                self.in_use = False

    store = object.__new__(ChromaEvidenceStore)
    store.vector_store = NonReentrantVectorStore()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: store.search("视频会议室照度", top_k=1), range(2)))

    assert all(result[0].source_name == "GB 50034-2024.md" for result in results)


def test_audit_store_upserts_chroma_compatible_chunk_ids(tmp_path) -> None:
    store = LocalEvidenceStore(tmp_path / "index.json")
    content = "Meeting room maintained illuminance is 300 lx."
    chunk_id = stable_chunk_id("source-hash", 1, content)

    count = store.upsert_chunks(
        [
            StoredChunk(
                chunk_id=chunk_id,
                source_name="standard.md",
                source_type="standard",
                source_hash="source-hash",
                locator="chunk 1",
                content=content,
                indexed_at="2026-07-30T00:00:00+00:00",
            )
        ]
    )

    assert count == 1
    assert store.get_evidence([chunk_id])[0].excerpt == content
