"""装配记忆能力；快照、工具、审查和事务实现继续由各模块负责。"""
from __future__ import annotations

from pathlib import Path

from agent.types.memory.protocols import MemoryBuildResult
from agent.types.runtime import RuntimeConfig
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from .memory_batch_reviewer import MemoryBatchReviewer
from .memory_context_loader import MemoryContextLoader
from .memory_extractor import MemoryExtractor
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from .memory_review_worker import MemoryReviewWorker
from .memory_validator import MemoryValidator


class MemoryBuilder:
    def __init__(self, storage, model_registry, *, root: Path, project_root: Path, config: RuntimeConfig):
        self._storage = storage
        self._models = model_registry
        self._root = root
        self._project_root = project_root
        self._config = config

    def build(self) -> MemoryBuildResult:
        from agent.tools.memory_edit_tool import MemoryEditTool
        from agent.tools.memory_tools import MemoryReadTool, MemorySearchTool

        storage = self._storage
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            memory_model = self._models.get()
        except Exception:
            memory_model = None
        extractor = MemoryExtractor(memory_model, storage.memory)
        project_books = tuple(
            path for path in (
                self._project_root / "project.md",
                self._project_root / "AGENTS.md",
                self._project_root / "CLAUDE.md",
            ) if path.exists()
        )
        policy_paths = (self._root / "POLICY.md",)
        documents = MarkdownDocuments(
            self._root, storage.db, policy_paths=policy_paths,
            project_paths=project_books, max_tokens=self._config.manual_tokens,
        )
        retriever = MemoryRetriever(
            storage.db, document_root=self._root, documents=documents,
            allowed_document_paths=(*policy_paths, *project_books),
        )
        context = MemoryContextLoader(
            root=self._root, policy_paths=policy_paths, project_paths=project_books,
            document_registry=storage.documents, documents=documents,
            experience_retriever=retriever,
            experience_token_budget=self._config.experience_tokens,
        )
        reviewer = MemoryBatchReviewer(extractor, validator=MemoryValidator())
        worker = MemoryReviewWorker(
            storage.sessions, reviewer, storage.review_committer,
            extractor_version=extractor.version,
        )
        return MemoryBuildResult(
            context=context,
            tools=(MemorySearchTool(retriever), MemoryReadTool(retriever),
                   MemoryEditTool(storage.memory, documents)),
            reviewer=worker,
        )
