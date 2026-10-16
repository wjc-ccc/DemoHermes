"""
AgentLoop — 核心对话循环（observation → reasoning ⇄ action → return）

职责边界：
    - 只根据 Session + Message → TurnResult，不认识 Channel / 队列 / 平台字段
    - react内循环：模型要求调工具就执行并回注，直到模型给出纯文本或触达上限
    - 不写磁盘（持久化是 Gateway/Store 的事），只往 EventBus 发观测事件
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from agent.core.perception.errors import AnchorCompactionError, ContextBudgetExceededError
from .building_messages import build_assistant_message, build_tool_message
from agent.core.perception.perception_admission import mark_readback_miss
from agent.types.conversation import Message, Session, TurnResult
from agent.core.config import LoopConfig
from agent.types.runtime import TurnContext
from agent.types.perception.trace import ModelCallSnapshot, normalized_usage, response_metadata

if TYPE_CHECKING:
    from agent.types.action.policy import AssemblyPolicy
    from agent.types.memory.scope import MemoryScope
    from agent.types.memory.snapshot import MemorySnapshot

logger = logging.getLogger(__name__)

_PREVIEW = 100

## agent loop 加载tool skills event bus 迭代次数 perception trace等等 + 负责emit中间状态对象
# 接受网关传入的特定session id的 session和message 组装上下文 + 工程化 retry等等
class AgentLoop:
    """执行单个会话 turn 的 ReAct 循环，并把上下文治理委托给 Perception。"""
    ## 进程级别的loop 就应该具备进程级别的模块配置  skill tool anchor admission budget
    def __init__(self, model, *, perception, tool_handler, skills=None, trace, config=None, memory_usage=None):
        """注入完整模块；预算、Anchor 和执行控制由对应模块管理。"""
        self.model = model
        self.perception = perception
        self.tool_handler = tool_handler
        self.skills = skills
        self.trace = trace
        self.config = config or LoopConfig()
        self.agent_id = self.config.agent_id
        self._memory_usage = memory_usage


    ## retry适合网络波动  对于有error的 一部分是阻止二次尝试 + 工具出错的熔断工具并返回工具错误信息 + 参数错误的传出工具提示的错误信息
    def _chat_with_retry(self, messages: list[dict], *, tool_schemas=None, turn_id=None, iteration=None, output_token_limit=None):
        """仅针对主模型瞬时异常执行有限次数重试。"""
        from .model.media_messages import prepare_model_messages

        messages = prepare_model_messages(
            messages, supports_images=bool(getattr(self.model, "supports_images", False))
        )
        # ACTION-HOOK-03：think 前使用当前 turn 筛选 schema；[] 不回退成全部工具。
        tools = tool_schemas
        if turn_id is not None:
            self.trace.perception.record_request(turn_id, iteration, tools or [], messages=messages)
        last_error: Exception | None = None
        for attempt in range(self.config.model_retries + 1):
            started = time.time()
            response = None
            error = None
            try:
                response = self.model.chat(messages, tools=tools or None,
                    **({"max_tokens": output_token_limit} if output_token_limit is not None else {}))
                if not (response.text or "").strip() and not response.tool_calls and response.finish_reason != "length":
                    raise RuntimeError("empty_model_response")
                return response
            except Exception as e:
                last_error = e
                error = str(e)
                logger.warning(
                    "模型调用失败（第 %d/%d 次）: %s", attempt + 1, self.config.model_retries + 1, e
                )
            finally:
                finished = time.time()
                usage = dict(response.usage or {}) if response is not None else {}
                input_tokens, output_tokens = normalized_usage(usage)
                if turn_id is not None:
                    self.trace.perception.record_model_call(turn_id, iteration, ModelCallSnapshot(
                        attempt=attempt + 1, provider=getattr(self.model, "name", None),
                        model=getattr(self.model, "model_name", None), started_at=started,
                        finished_at=finished, duration_ms=max(0, int((finished - started) * 1000)),
                        usage=usage, input_token_count=input_tokens, output_token_count=output_tokens, error=error,
                        **response_metadata(response),
                    ))
            if attempt < self.config.model_retries:
                time.sleep(0.5 * (attempt + 1))
        raise last_error  # type: ignore[misc]

    # ** 初始化turnid 和 指标 然后执行run turn** 核心函数 loop调用接口
    def run_turn(
        self,
        session: Session,
        user_message: Message,
        *,
        memory_snapshot: MemorySnapshot | None = None,
        memory_scope: MemoryScope | None = None,
        ephemeral_prompt: str | None = None,
        assembly_policy: AssemblyPolicy | None = None,
    ) -> TurnResult:
        """初始化 turn 身份，并执行上下文构建、主模型调用、工具回注和完成判定。"""
        turn_id = uuid.uuid4().hex[:12]
        user_message.trajectory_id = turn_id
        session_key = session.session_key

        ## 构建当前session
        session.add_message(user_message)
        self.trace.emit(
            "turn_start", turn_id, session, session_key,
            user_text=user_message.content_text[:_PREVIEW],
            message_count=session.message_count,
        )
        logger.info("turn 开始 turn_id=%s session=%s", turn_id, session.session_id)

        # ACTION-HOOK-01：创建 turn 身份后、冻结技能前建立局部执行上下文。
        # 未启用时不调用模块；不能把上下文放到共享 Loop.current_context。
        tool_context = self.tool_handler.begin_turn(TurnContext(
            session_id=session.session_id, session_key=session_key, turn_id=turn_id,
            source_message_id=user_message.message_id, author_id=session.author_id,
            channel=session.channel, memory_scope=memory_scope,
            assembly_policy=assembly_policy, agent_id=self.agent_id,
        ))

        ## 冻结skill快照
        skill_selection = self._freeze_skills(
            skill_query=user_message.content_text,
            memory_scope=memory_scope,
            available_tools=tool_context.available_tools,
        )
        tool_context = tool_context.with_skills(skill_selection)

        ## 当前对话开始 循环迭代
        iteration_limit = tool_context.max_iterations or self.config.max_iterations
        for iteration in range(1, iteration_limit + 1):
            schemas = self.tool_handler.get_schemas(tool_context)
            # 唯一上下文主链：Perception 内部固定执行加载 → 分级 → 构建。
            try:
                perception_result = self.perception.build(
                    session,
                    memory_snapshot=memory_snapshot,
                    skill_selection=skill_selection,
                    ephemeral_prompt=ephemeral_prompt,
                    trace_id=turn_id,
                    iteration=iteration,
                    tool_schemas=schemas,
                )
            except AnchorCompactionError as exc:
                self.trace.perception.record_failure(turn_id, iteration, "anchor_compaction_failed")
                logger.warning(
                    "turn Anchor 压缩失败 turn_id=%s code=%s detail=%s",
                    turn_id,
                    exc.code,
                    exc.detail,
                )
                self.trace.emit(
                    "turn_error", turn_id, session, session_key,
                    error="anchor_compaction_failed",
                    anchor_error_code=exc.code,
                )
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False,
                    error="anchor_compaction_failed",
                )
            except ContextBudgetExceededError:
                self.trace.perception.record_failure(turn_id, iteration, "context_budget_exceeded")
                logger.warning("turn 上下文预算超限 turn_id=%s", turn_id)
                self.trace.emit(
                    "turn_error", turn_id, session, session_key,
                    error="context_budget_exceeded",
                )
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False, error="context_budget_exceeded",
                )

            except Exception:
                self.trace.perception.record_failure(turn_id, iteration, "context_build_failed")
                raise

            if perception_result.anchor_updated and perception_result.anchor is not None:
                session.context_anchor = perception_result.anchor
            if perception_result.verdicts_updated:
                session.context_verdicts = dict(perception_result.verdicts)
            messages = perception_result.messages
            self.trace.perception.record_request(turn_id, iteration, schemas or [])

            self.trace.emit(
                "llm_request", turn_id, session, session_key,
                iteration=iteration, **perception_result.metrics.event_data(),
            )
            try:
                ## TODO：针对于不同的错误信息应该使用不同的 retry方法 
                request_started_at = time.time()
                response = self._chat_with_retry(messages, tool_schemas=schemas, turn_id=turn_id, iteration=iteration,
                                                 output_token_limit=perception_result.output_token_limit)
                self._record_memory_exposure(
                    memory_snapshot, memory_scope, session, user_message.message_id,
                    turn_id, iteration, request_started_at, messages,
                    max((message.seq_id for message in session.messages), default=-1),
                )
                self._record_manual_memory_exposure(
                    memory_scope, session, user_message.message_id, turn_id, iteration,
                    request_started_at, messages,
                    max((message.seq_id for message in session.messages), default=-1),
                )

            except Exception as e:
                logger.exception("模型调用最终失败 turn_id=%s", turn_id)
                self.trace.emit("turn_error", turn_id, session, session_key, error=str(e))

                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False, error=f"模型调用失败: {e}",
                )

            self.trace.emit(
                "llm_response", turn_id, session, session_key,
                iteration=iteration,
                text_preview=response.text[:_PREVIEW],
                tool_calls=[{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                finish_reason=response.finish_reason,
                usage=response.usage,
            )


            if response.finish_reason == "length":
                # 截断载荷保留为失败事实，不执行未生成完的工具批次，也不伪装成终答。
                partial = build_assistant_message(response.text, response.tool_calls)
                partial.trajectory_id = turn_id
                partial.metadata["iteration"] = iteration
                partial.status = "failed"
                partial.metadata["finish_reason"] = response.finish_reason
                session.add_message(partial)
                self.trace.emit("turn_error", turn_id, session, session_key, error="model_output_truncated")
                return TurnResult(
                    final_text=response.text or None, session_id=session.session_id,
                    turn_id=turn_id, completed=False, error="model_output_truncated",
                )

            ## 判断是否工具调用
            if response.has_tool_calls:
                for request in response.tool_calls:
                    if not request.id:
                        request.id = uuid.uuid4().hex
                assistant_message = build_assistant_message(response.text, response.tool_calls)
                assistant_message.trajectory_id = turn_id
                assistant_message.metadata["iteration"] = iteration
                session.add_message(assistant_message)
                # ACTION-HOOK-04：Handler 审批/分配，Dispatch 执行，Loop 统一记录和回注。
                budget = self.perception.config.budget
                response_context = tool_context
                if budget is not None:
                    limit = self.config.tool_result_limit(
                        input_tokens=budget.max_input_tokens,
                        tokens_used=perception_result.metrics.tokens_used,
                        call_count=len(response.tool_calls),
                    )
                    response_context = replace(tool_context, invocation=replace(tool_context.invocation,
                        result_token_budget=limit), action=(tool_context.action.model_copy(
                            update={"result_token_budget": limit}) if tool_context.action is not None else None))
                records = self.tool_handler.handle_batch(response.tool_calls, response_context)
                for tool_index, record in enumerate(records):
                    record.metadata.update(assistant_message_id=assistant_message.message_id,
                                           iteration=iteration, response_tool_index=tool_index)
                    session.tool_calls.append(record)
                    if record.ok and record.readback_reference is not None:
                        self.trace.perception.record_memory_read(turn_id, record.readback_reference)
                        self._pin_readback(session, record.readback_reference, turn_id, session_key)
                    session.add_message(build_tool_message(record))
                continue
            
            ## 记录当前结果messages
            ## TODO 是否应该记录类型为action or observation
            ## 不需要 这里使用jev来判断对应的工具回复了 不需要定死类别
            assistant_message = build_assistant_message(response.text)
            assistant_message.trajectory_id = turn_id
            assistant_message.metadata["iteration"] = iteration
            session.add_message(assistant_message)
            self.trace.emit(
                "turn_end", turn_id, session, session_key,
                final_text=response.text[:_PREVIEW],
                iterations=iteration,
            )
            logger.info("turn 完成 turn_id=%s 迭代=%d", turn_id, iteration)
            return TurnResult(
                final_text=response.text,
                session_id=session.session_id,
                turn_id=turn_id,
                completed=True,
            )

        fallback = "（抱歉，这个问题需要的推理步骤超出了我的上限，请换个问法或拆小一点再试。）"
        logger.warning("turn 超过最大迭代 turn_id=%s max=%d", turn_id, iteration_limit)
        fallback_message = build_assistant_message(fallback)
        fallback_message.trajectory_id = turn_id
        fallback_message.metadata["iteration"] = iteration_limit
        session.add_message(fallback_message)
        self.trace.emit("turn_error", turn_id, session, session_key, error="max_iterations")
        return TurnResult(
            final_text=fallback,
            session_id=session.session_id,
            turn_id=turn_id,
            completed=False,
            error="max_iterations",
        )

    def _freeze_skills(self, *, skill_query, memory_scope, available_tools=None):
        """本 turn 只选一次技能，后续迭代与 use_skill 都看这份快照。"""
        if self.skills is None:
            return None
        # ACTION-HOOK-02：冻结技能时 required_tools 只能来自当前允许工具集。
        # 未启用模块才使用原注册表；显式空集合仍然为空。
        available = available_tools
        scope_key = getattr(memory_scope, "storage_key", None)
        return self.skills.select(
            query=skill_query,
            scope_key=scope_key,
            available_tools=available,
        )

    def _record_memory_exposure(self, snapshot, scope, session, source_message_id,
                                turn_id, iteration, exposed_at, messages, after_seq):
        """实际请求成功后暂存曝光，由轮次事务统一提交计数和反馈作业。"""
        if (self._memory_usage is None or snapshot is None or scope is None
                or snapshot.scope.storage_key != scope.storage_key
                or snapshot.session_id != session.session_id or not snapshot.experience_cards):
            return
        if not any(message.get("role") == "system"
                   and message.get("content") == snapshot.experience_prompt for message in messages):
            return
        for card in snapshot.experience_cards:
            self._stage_memory_exposure(
                session, source_message_id, card.card_id, card.revision, card.rendered_content,
                {"first_iteration": iteration, "exposed_at": exposed_at,
                 "source_user_message_id": source_message_id, "after_seq": after_seq},
            )

    def _record_manual_memory_exposure(self, scope, session, source_message_id,
                                       turn_id, iteration, exposed_at, messages, after_seq):
        """手动读卡结果完整进入实际请求后才记曝光，搜索摘要不计入。"""
        if self._memory_usage is None or scope is None:
            return
        visible = {(message.get("tool_call_id"), message.get("content"))
                   for message in messages if message.get("role") == "tool"
                   and isinstance(message.get("content"), str)}
        candidates = []
        for record in session.tool_calls:
            if (record.trajectory_id != turn_id or not record.ok or record.tool_name != "memory_read"
                    or not record.readback_reference or not record.readback_reference.startswith("card:")):
                continue
            data = record.result_data or {}
            revision = data.get("card_revision")
            if (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
                    or data.get("reference") != record.readback_reference):
                continue
            protocol_id = record.provider_tool_call_id or record.tool_call_id
            if not record.result_text or (protocol_id, record.result_text) not in visible:
                continue
            candidates.append((record, revision))
        for record, revision in candidates:
            self._stage_memory_exposure(
                session, source_message_id, record.readback_reference.partition(":")[2], revision,
                record.result_text,
                {"first_iteration": iteration, "exposed_at": exposed_at,
                 "source_user_message_id": source_message_id, "after_seq": after_seq,
                 "exposure_source": "memory_read", "tool_call_id": record.tool_call_id},
            )

    def _stage_memory_exposure(self, session, source_message_id, card_id, card_revision,
                               card_content, context):
        """记录仅附着于当前轮次的用户消息，不在共享 Loop 中保存轮次状态。"""
        from agent.core.memory.config import experience_model_key
        source = next((message for message in session.messages
                       if message.message_id == source_message_id and message.role == "user"), None)
        if source is None:
            return
        model_key = experience_model_key(self.model)
        exposures = source.metadata.setdefault("memory_exposures", [])
        identity = (card_id, card_revision, model_key)
        if any((item["card_id"], item["card_revision"], item["model_key"]) == identity for item in exposures):
            return
        exposures.append({"card_id": card_id, "card_revision": card_revision, "model_key": model_key,
                          "card_content": card_content, "context": context})

    ### 如果模型工具调用读会这个字段信息 那么就不在context_verdicts里删除
    def _pin_readback(
        self, session: Session, reference: str, turn_id: str, session_key: str
    ) -> None:
        """模型读回了被准入移出/裁剪的工具结果：判错，钉住全文并发出事件。"""
        if not isinstance(reference, str) or not session.context_verdicts:
            return
        turn = self.trace.perception.get(turn_id)
        applied = tuple(message_id for build in (turn.builds if turn else ())
                        for message_id in build.applied_admission_message_ids)
        verdicts, pinned = mark_readback_miss(
            session.context_verdicts, reference, applied_message_ids=applied,
        )
        if pinned is None:
            return
        session.context_verdicts = verdicts
        self.trace.emit(
            "context_admission_miss", turn_id, session, session_key,
            message_id=pinned.message_id,
            tool_call_id=pinned.tool_call_id,
            source=pinned.source,
            score=pinned.score,
            reason=pinned.reason,
        )

    ## TODO：这里应该切换为一个工具执行handler
