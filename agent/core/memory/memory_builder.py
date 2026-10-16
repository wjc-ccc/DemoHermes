"""装配记忆能力；快照、工具、审查和事务实现继续由各模块负责。"""
from __future__ import annotations

from pathlib import Path

from agent.types.memory.protocols import MemoryBuildResult
from agent.types.runtime import RuntimeConfig
from agent.core.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.core.memory.config import MemoryContextConfig, MemoryReviewConfig, MemoryExperienceConfig, experience_model_key
from .memory_batch_reviewer import MemoryBatchReviewer
from .memory_context_loader import MemoryContextLoader
from .memory_extractor import MemoryExtractor
from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
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
        from agent.core.tools.memory_edit_tool import MemoryEditTool
        from agent.core.tools.memory_tools import MemoryReadTool, MemorySearchTool

        storage = self._storage
        experience_config = self._config.memory_experience_config or MemoryExperienceConfig()
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            memory_model = self._models.get()
        except Exception:
            memory_model = None
        if memory_model is not None and experience_config.enabled:
            storage.card_decisions.rebuild_stats(experience_model_key(memory_model))
        extractor = MemoryExtractor(
            memory_model, storage.memory,
            config=self._config.memory_extraction_config,
            token_estimate=self._config.token_estimate_config,
        )
        project_books = tuple(
            path for path in (
                self._project_root / "project.md",
                self._project_root / "AGENTS.md",
                self._project_root / "CLAUDE.md",
            ) if path.exists()
        )
        policy_paths = (self._root / "POLICY.md",)
        context_cfg = self._config.memory_context_config or MemoryContextConfig()
        manual_tokens = self._config.manual_tokens if self._config.manual_tokens is not None else context_cfg.manual_tokens
        experience_tokens = (
            self._config.experience_tokens
            if self._config.experience_tokens is not None else context_cfg.experience_tokens
        )
        documents = MarkdownDocuments(
            self._root, storage.db, policy_paths=policy_paths,
            project_paths=project_books, max_tokens=manual_tokens,
        )
        retriever = MemoryRetriever(
            storage.db, document_root=self._root, documents=documents,
            allowed_document_paths=(*policy_paths, *project_books),
            experience_config=experience_config,
        )
        context = MemoryContextLoader(
            root=self._root, policy_paths=policy_paths, project_paths=project_books,
            document_registry=storage.documents, documents=documents,
            experience_retriever=retriever,
            experience_token_budget=experience_tokens,
            experience_limit=context_cfg.experience_limit,
        )
        review = self._config.memory_review_config or MemoryReviewConfig()
        maintenance = []
        if experience_config.enabled:
            from agent.core.provider import build_experience_judge
            from .memory_feedback import MemoryFeedback
            from .memory_embedding import OpenAIEmbedding
            from .memory_consolidation import MemoryConsolidator

            judge = build_experience_judge(experience_config)
            embedding = OpenAIEmbedding.from_env(config=experience_config)
            if judge is not None and memory_model is not None:
                maintenance.append(MemoryFeedback(
                    storage, judge, experience_model_key(memory_model), experience_config,
                    review_config=review,
                ))
            if embedding is not None:
                maintenance.append(MemoryConsolidator(
                    storage, embedding=embedding, judge=judge, model=memory_model,
                    config=experience_config,
                ))
        reviewer = MemoryBatchReviewer(extractor, validator=MemoryValidator())
        worker = MemoryReviewWorker(
            storage.sessions, reviewer, storage.review_committer,
            extractor_version=extractor.version,
            lease_seconds=review.lease_seconds,
            max_attempts=review.max_attempts,
            backoff_seconds=review.backoff_seconds,
            poll_interval=review.poll_interval,
            shutdown_grace=review.shutdown_grace,
            maintenance=maintenance,
        )
        return MemoryBuildResult(
            context=context,
            tools=(MemorySearchTool(retriever), MemoryReadTool(retriever),
                   MemoryEditTool(storage.memory, documents)),
            reviewer=worker,
        )
