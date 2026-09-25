"""
SkillRepository — 技能写入的唯一入口

所有 SKILL.md 变更（新建 / patch / 晋升 / 停用 / 回滚）都必须经过本模块，
调用方不再自己拼 frontmatter、自己 write_text、自己维护备份编号。

这里集中维护的不变量：
    - 原子写：先写临时文件再 os.replace，中途失败不留下半个 SKILL.md
    - 版本：revision 是「正文版本」，每次内容变更 +1 并归档旧版为 SKILL.md.bak.<revision>
      状态迁移（draft/active/disabled）不改 revision，因为正文没变
    - 乐观锁：expected_revision 不匹配直接拒绝，避免并发覆盖
    - 内置只读：source=builtin 的技能不接受任何自动变更
    - 越权拒绝：路径必须落在 skills_root 内
    - patch 只作用于正文：模型无法借 patch 把 status 改成 active 绕过质量门
    - 去重：正文指纹相同的候选不再重复落盘
    - 串行化：进程内 RLock；写入后统一 reload Registry
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .base import (
    Skill,
    SkillParseError,
    SkillSource,
    content_hash_of,
    normalize_skill_name,
)

logger = logging.getLogger(__name__)

SkillChangeAction = Literal["create", "patch", "promote", "disable", "rollback"]


class SkillDraft(BaseModel):
    """一份待落盘的技能内容（领域载体，不是某个函数的参数包）。"""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    body: str
    source: SkillSource = "generated"
    scope_key: str = ""
    origin_turn_id: str = ""
    required_tools: tuple[str, ...] = ()


class SkillChangeRequest(BaseModel):
    """一次技能变更请求。"""

    model_config = ConfigDict(frozen=True)

    action: SkillChangeAction
    name: str = ""
    draft: SkillDraft | None = None
    old_string: str = ""
    new_string: str = ""
    expected_revision: int | None = None
    target_revision: int | None = None
    actor: str = "system"
    reason: str = ""
    turn_id: str | None = None


class SkillChangeResult(BaseModel):
    """变更结果；code 是稳定的机器可读原因，便于审计与测试断言。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    code: str
    name: str = ""
    revision: int = 0
    status: str = ""
    path: str = ""
    detail: str = ""


class SkillRepository:
    def __init__(
        self,
        skills_root: Path | str,
        *,
        registry=None,
        audit_log=None,
    ) -> None:
        self._root = Path(skills_root)
        self._registry = registry
        self._audit = audit_log
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    # ---- 唯一公共写入面 ----
    def apply(self, request: SkillChangeRequest) -> SkillChangeResult:
        with self._lock:
            result = self._apply_locked(request)
        self._record_audit(request, result)
        return result

    def _apply_locked(self, request: SkillChangeRequest) -> SkillChangeResult:
        if request.action == "create":
            return self._create(request)
        name = normalize_skill_name(request.name)
        if not name:
            return SkillChangeResult(ok=False, code="invalid", detail="缺少有效 name")
        current, error = self._load(name)
        if error is not None:
            return error
        assert current is not None
        if current.source == "builtin":
            return SkillChangeResult(
                ok=False, code="builtin_immutable", name=name,
                detail="内置技能不接受自动变更",
            )
        if (
            request.expected_revision is not None
            and request.expected_revision != current.revision
        ):
            return SkillChangeResult(
                ok=False, code="revision_conflict", name=name,
                revision=current.revision,
                detail=f"expected={request.expected_revision} actual={current.revision}",
            )
        if request.action == "patch":
            return self._patch(current, request)
        if request.action == "promote":
            return self._set_status(current, "active", "promoted")
        if request.action == "disable":
            return self._set_status(current, "disabled", "disabled")
        if request.action == "rollback":
            return self._rollback(current, request)
        return SkillChangeResult(
            ok=False, code="invalid", name=name,
            detail=f"不支持的 action: {request.action}",
        )

    # ---- 查询（只读，供 Gate / 观测使用）----
    def get(self, name: str) -> Skill | None:
        path = self._path_for(normalize_skill_name(name))
        if path is None or not path.is_file():
            return None
        try:
            return Skill.from_file(path)
        except SkillParseError:
            logger.exception("技能解析失败 path=%s", path)
            return None

    def list_backup_revisions(self, name: str) -> tuple[int, ...]:
        """可回滚的历史正文版本号（升序）。"""
        path = self._path_for(normalize_skill_name(name))
        if path is None:
            return ()
        revisions: list[int] = []
        for backup in path.parent.glob("SKILL.md.bak.*"):
            suffix = backup.name.rsplit(".", 1)[-1]
            if suffix.isdigit():
                revisions.append(int(suffix))
        return tuple(sorted(revisions))

    # ---- 各 action 实现 ----
    def _create(self, request: SkillChangeRequest) -> SkillChangeResult:
        draft = request.draft
        if draft is None:
            return SkillChangeResult(ok=False, code="invalid", detail="create 需要 draft")
        name = normalize_skill_name(draft.name)
        description = (draft.description or "").strip()
        body = (draft.body or "").strip()
        if not name:
            return SkillChangeResult(ok=False, code="invalid", detail="缺少有效 name")
        if not description or not body:
            return SkillChangeResult(
                ok=False, code="invalid", name=name,
                detail="create 需要 description 与 body",
            )
        if draft.source == "builtin":
            return SkillChangeResult(
                ok=False, code="invalid", name=name,
                detail="不允许创建 builtin 技能",
            )
        existing = self._registry.get(name) if self._registry is not None else None
        if existing is not None and existing.source == "builtin":
            return SkillChangeResult(
                ok=False, code="name_conflict", name=name,
                detail="与内置技能同名",
            )
        path = self._path_for(name)
        if path is None:
            return SkillChangeResult(ok=False, code="invalid", name=name, detail="非法路径")
        if path.exists():
            return SkillChangeResult(
                ok=False, code="exists", name=name, path=str(path),
                detail="同名技能已存在",
            )
        duplicate = self._find_duplicate(body)
        if duplicate is not None:
            return SkillChangeResult(
                ok=False, code="duplicate_content", name=name,
                detail=f"正文与已有技能重复: {duplicate}",
            )
        skill = Skill(
            name=name,
            description=description,
            body=body,
            path=str(path),
            status="draft",  # 新技能一律 draft；晋升只能由质量门完成
            revision=1,
            source=draft.source,
            scope_key=draft.scope_key,
            origin_turn_id=draft.origin_turn_id or (request.turn_id or ""),
            required_tools=draft.required_tools,
        )
        return self._write(skill, code="created")

    def _patch(
        self,
        current: Skill,
        request: SkillChangeRequest,
    ) -> SkillChangeResult:
        old, new = request.old_string, request.new_string
        if not old:
            return SkillChangeResult(
                ok=False, code="invalid", name=current.name,
                detail="patch 需要 old_string",
            )
        # 只在正文里替换：frontmatter 不在可 patch 范围内
        if old not in current.body:
            return SkillChangeResult(
                ok=False, code="not_matched", name=current.name,
                revision=current.revision,
                detail="old_string 未在正文中找到",
            )
        body = current.body.replace(old, new, 1)
        if body.strip() == current.body.strip():
            return SkillChangeResult(
                ok=False, code="no_change", name=current.name,
                revision=current.revision, detail="patch 未改变正文",
            )
        return self._write(
            current.model_copy(update={"body": body, "revision": current.revision + 1}),
            code="patched",
            archive_from=current,
        )

    def _set_status(
        self,
        current: Skill,
        status: str,
        code: str,
    ) -> SkillChangeResult:
        if current.status == status:
            return SkillChangeResult(
                ok=False, code=f"already_{status}", name=current.name,
                revision=current.revision, status=current.status,
            )
        # 状态迁移不改正文，因此 revision 保持不变
        return self._write(current.model_copy(update={"status": status}), code=code)

    def _rollback(
        self,
        current: Skill,
        request: SkillChangeRequest,
    ) -> SkillChangeResult:
        revisions = self.list_backup_revisions(current.name)
        if not revisions:
            return SkillChangeResult(
                ok=False, code="no_backup", name=current.name,
                revision=current.revision, detail="没有可回滚的历史版本",
            )
        target = request.target_revision if request.target_revision else revisions[-1]
        if target not in revisions:
            return SkillChangeResult(
                ok=False, code="no_backup", name=current.name,
                revision=current.revision,
                detail=f"目标版本不存在: {target}（可用 {list(revisions)}）",
            )
        backup = Path(current.path).with_name(f"SKILL.md.bak.{target}")
        try:
            restored = Skill.from_file(backup)
        except SkillParseError as exc:
            return SkillChangeResult(
                ok=False, code="no_backup", name=current.name,
                revision=current.revision, detail=f"历史版本无法解析: {exc}",
            )
        return self._write(
            current.model_copy(
                update={
                    "body": restored.body,
                    "description": restored.description,
                    "revision": current.revision + 1,
                }
            ),
            code="rolled_back",
            archive_from=current,
        )

    # ---- 落盘 ----
    def _write(
        self,
        skill: Skill,
        *,
        code: str,
        archive_from: Skill | None = None,
    ) -> SkillChangeResult:
        path = Path(skill.path)
        tmp = path.with_name(f"SKILL.md.tmp.{uuid.uuid4().hex[:8]}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if archive_from is not None:
                backup = path.with_name(f"SKILL.md.bak.{archive_from.revision}")
                backup.write_text(archive_from.render(), encoding="utf-8")
            tmp.write_text(skill.render(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            logger.exception("技能写入失败 name=%s path=%s", skill.name, path)
            return SkillChangeResult(
                ok=False, code="write_failed", name=skill.name, detail=str(exc)
            )
        if self._registry is not None and hasattr(self._registry, "reload"):
            self._registry.reload()
        logger.info(
            "技能变更 name=%s code=%s status=%s revision=%s",
            skill.name, code, skill.status, skill.revision,
        )
        return SkillChangeResult(
            ok=True,
            code=code,
            name=skill.name,
            revision=skill.revision,
            status=skill.status,
            path=str(path),
        )

    # ---- 辅助 ----
    def _load(self, name: str) -> tuple[Skill | None, SkillChangeResult | None]:
        path = self._path_for(name)
        if path is None:
            return None, SkillChangeResult(
                ok=False, code="invalid", name=name, detail="非法路径"
            )
        if not path.is_file():
            # 可能是内置技能：不在可写根目录内，但同样禁止变更
            existing = self._registry.get(name) if self._registry is not None else None
            if existing is not None and existing.source == "builtin":
                return None, SkillChangeResult(
                    ok=False, code="builtin_immutable", name=name,
                    detail="内置技能不接受自动变更",
                )
            return None, SkillChangeResult(
                ok=False, code="not_found", name=name, detail="技能不存在"
            )
        try:
            return Skill.from_file(path), None
        except SkillParseError as exc:
            return None, SkillChangeResult(
                ok=False, code="unparsable", name=name, detail=str(exc)
            )

    def _path_for(self, name: str) -> Path | None:
        if not name:
            return None
        path = (self._root / name / "SKILL.md").resolve()
        try:
            path.relative_to(self._root.resolve())
        except ValueError:
            return None
        return path

    def _find_duplicate(self, body: str) -> str | None:
        target = content_hash_of(body.strip())
        for skill_file in sorted(self._root.glob("*/SKILL.md")):
            try:
                existing = Skill.from_file(skill_file)
            except SkillParseError:
                continue
            if existing.content_hash == target:
                return existing.name
        return None

    def _record_audit(
        self,
        request: SkillChangeRequest,
        result: SkillChangeResult,
    ) -> None:
        if self._audit is None:
            return
        try:
            from ..evolution.audit import EvolutionAuditEntry

            self._audit.append(
                EvolutionAuditEntry(
                    actor=request.actor,
                    op=f"skill_{request.action}",
                    target=f"skill:{result.name or request.name}",
                    content_preview=f"{result.code} rev={result.revision}",
                    turn_id=request.turn_id,
                    reason=request.reason or result.detail,
                    admitted=result.ok,
                    scope_key=(
                        request.draft.scope_key if request.draft is not None else ""
                    ),
                )
            )
        except Exception:
            logger.exception("技能审计写入失败 name=%s", request.name)
