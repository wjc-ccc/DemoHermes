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
from typing import TYPE_CHECKING

from agent.core.precision.errors import AnchorCompactionError, ContextBudgetExceededError
from .building_messages import build_assistant_message, build_tool_message
from agent.core.precision.percision_admission import mark_readback_miss
from agent.types.conversation import Message, Session, TurnResult
from agent.types.runtime import LoopConfig, TurnContext

if TYPE_CHECKING:
    from agent.types.action.policy import AssemblyPolicy
    from agent.types.memory.scope import MemoryScope
    from agent.types.memory.snapshot import MemorySnapshot

logger = logging.getLogger(__name__)

_PREVIEW = 100

## agent loop 加载tool skills event bus 迭代次数 percision trace等等 + 负责emit中间状态对象 
# 接受网关传入的特定session id的 session和message 组装上下文 + 工程化 retry等等
class AgentLoop:
    """执行单个会话 turn 的 ReAct 循环，并把上下文治理委托给 Precision。"""
    ## 进程级别的loop 就应该具备进程级别的模块配置  skill tool anchor admission budget
    def __init__(self, model, *, precision, tool_handler, skills=None, trace, config=None):
        """注入完整模块；预算、Anchor 和执行控制由对应模块管理。"""
        self.model = model
        self.precision = precision
        self.tool_handler = tool_handler
        self.skills = skills
        self.trace = trace
        self.config = config or LoopConfig()
        self.agent_id = self.config.agent_id


    ## retry适合网络波动  对于有error的 一部分是阻止二次尝试 + 工具出错的熔断工具并返回工具错误信息 + 参数错误的传出工具提示的错误信息
    def _chat_with_retry(self, messages: list[dict], *, tool_schemas=None):
        """仅针对主模型瞬时异常执行有限次数重试。"""
        from ..model.media_messages import prepare_model_messages

        messages = prepare_model_messages(
            messages, supports_images=bool(getattr(self.model, "supports_images", False))
        )
        # ACTION-HOOK-03：think 前使用当前 turn 筛选 schema；[] 不回退成全部工具。
        tools = tool_schemas
        last_error: Exception | None = None
        for attempt in range(self.config.model_retries + 1):
            try:
                return self.model.chat(messages, tools=tools or None)
            except Exception as e:
                last_error = e
                logger.warning(
                    "模型调用失败（第 %d/%d 次）: %s", attempt + 1, self.config.model_retries + 1, e
                )
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
            # 唯一上下文主链：Precision 内部固定执行加载 → 分级 → 构建。
            try:
                precision_result = self.precision.build(
                    session,
                    memory_snapshot=memory_snapshot,
                    skill_selection=skill_selection,
                    ephemeral_prompt=ephemeral_prompt,
                    trace_id=turn_id,
                    iteration=iteration,
                )
            except AnchorCompactionError as exc:
                self.trace.precision.record_failure(turn_id, iteration, "anchor_compaction_failed")
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
                self.trace.precision.record_failure(turn_id, iteration, "context_budget_exceeded")
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
                self.trace.precision.record_failure(turn_id, iteration, "context_build_failed")
                raise

            if precision_result.anchor_updated and precision_result.anchor is not None:
                session.context_anchor = precision_result.anchor
            if precision_result.verdicts_updated:
                session.context_verdicts = dict(precision_result.verdicts)
            messages = precision_result.messages
            schemas = self.tool_handler.get_schemas(tool_context)
            self.trace.precision.record_request(turn_id, iteration, schemas or [])

            self.trace.emit(
                "llm_request", turn_id, session, session_key,
                iteration=iteration, **precision_result.metrics.event_data(),
            )
            try:
                ## TODO：针对于不同的错误信息应该使用不同的 retry方法 
                response = self._chat_with_retry(messages, tool_schemas=schemas)

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


            ## 判断是否工具调用
            if response.has_tool_calls:
                session.add_message(build_assistant_message(response.text, response.tool_calls))
                # ACTION-HOOK-04：Handler 审批/分配，Dispatch 执行，Loop 统一记录和回注。
                records = self.tool_handler.handle_batch(response.tool_calls, tool_context)
                for record in records:
                    session.tool_calls.append(record)
                    if record.ok and record.readback_reference is not None:
                        self.trace.precision.record_memory_read(turn_id, record.readback_reference)
                        self._pin_readback(session, record.readback_reference, turn_id, session_key)
                    session.add_message(build_tool_message(record))
                continue
            
            ## 记录当前结果messages
            ## TODO 是否应该记录类型为action or observation
            ## 不需要 这里使用jev来判断对应的工具回复了 不需要定死类别
            session.add_message(build_assistant_message(response.text))
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
        session.add_message(build_assistant_message(fallback))
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

    ### 如果模型工具调用读会这个字段信息 那么就不在context_verdicts里删除
    def _pin_readback(
        self, session: Session, reference: str, turn_id: str, session_key: str
    ) -> None:
        """模型读回了被准入移出/裁剪的工具结果：判错，钉住全文并发出事件。"""
        if not isinstance(reference, str) or not session.context_verdicts:
            return
        verdicts, pinned = mark_readback_miss(session.context_verdicts, reference)
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
