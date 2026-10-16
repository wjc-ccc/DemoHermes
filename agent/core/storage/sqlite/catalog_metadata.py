"""User-facing table and column descriptions."""
TABLES = {
    "sessions": ("会话", "会话、当前入口路由及原始 Anchor 载荷"),
    "turns": ("执行", "轮次事实、用量及环境快照"),
    "messages": ("执行", "有序消息及所属轮次"),
    "tool_calls": ("执行", "工具请求、结果和来源引用"),
    "memory_cards": ("长期记忆", "单一类别的原子事实或经验观察"),
    "memory_experiences": ("长期记忆", "经验适用条件、做法、结果及限制"),
    "memory_evidence": ("长期记忆", "可追到原文的证据引用与逐字引文"),
    "memory_terms": ("长期记忆", "关键词检索索引"),
    "memory_card_versions": ("长期记忆", "记忆卡片完整历史版本"),
    "memory_card_decisions": ("长期记忆", "经验曝光、JEV使用判定和合并判定；统计应用标记防止重复累计"),
    "memory_conflicts": ("长期记忆", "无法自动解决的事实冲突"),
    "memory_review_turns": ("后台", "正常完成轮次的用户级累计序号和固定提炼批次"),
    "memory_document_versions": ("文档", "个人 MEMORY.md 的人工与模型修订历史"),
    "context_documents": ("文档", "Markdown 来源登记"),
    "jobs": ("后台", "通用作业、领取凭证、重试和提交结果"),
    "traces": ("观测", "模块操作摘要"),
    "trace_items": ("观测", "原子观测；分离模型声明和运行时事实"),
    "audit_events": ("审计", "管理操作记录"),
    "schema_migrations": ("系统", "已应用结构版本及校验和"),
    "eval_datasets": ("评测", "已发布的案例版本及内容指纹"),
    "eval_cases": ("评测", "场景、对话历史、预期记忆与忽略规则"),
    "eval_questions": ("评测", "自然提问、参考回答、证据与字段契约版本"),
    "eval_runs": ("评测", "绑定题库版本的模型运行与实现版本"),
    "eval_results": ("评测", "实际回答、问题指纹与语义审核结果"),
}

FIELDS = {
    "payload_json": "完整领域载荷；保留领域时间和 Anchor 原始格式",
    "session_json": "会话状态和 Anchor；消息数量从事实表计算",
    "turn_json": "本轮完整事实与 Perception 迭代记录",
    "message_json": "完整原始消息及附件、来源信息",
    "tool_call_json": "完整工具参数、结果与持久事实身份",
    "dataset_json": "题库元信息，不重复保存案例列表",
    "case_json": "对话场景，不重复保存问题列表",
    "question_json": "黄金问题、证据、参考答案和语义标准",
    "question_schema_version": "题目字段契约版本；内容随题库版本发布",
    "history_end_turn_id": "本题可读取历史的最后一轮，禁止未来信息",
    "metadata_json": "卡片的非索引来源元数据",
    "slot_key": "事实对象和稳定属性形成的唯一槽位；经验为空",
    "normalized_term": "规范化检索词",
    "environment_json": "本轮环境快照",
    "revision": "乐观并发版本；修改时校验预期版本",
    "card_revision": "本次经验评判实际曝光的卡片修订号",
    "used_count": "实际注入主模型的轮次数，同轮只计一次",
    "judge_count": "已计入当前主模型统计的JEV使用判定次数",
    "effective_count": "建议有执行证据落实的次数",
    "helpful_count": "执行后有正向结果支持的次数",
    "harmful_count": "执行后有负向结果支持的次数",
    "recall_enabled": "是否允许自动召回；关闭后仍可按需检索",
    "counted_at": "本判定已应用到卡片统计的UTC毫秒，为空表示未应用",
    "is_current": "此入口是否选中该会话；同一入口最多一个",
    "session_key": "入口路由键",
    "scope_key": "明确的归属范围",
    "source_type": "信息来源类型",
    "source_id": "产生信息的消息、工具或程序标识",
    "lease_token": "本次领取凭证；隔离过期 Worker",
    "lease_expires_at": "领取过期时间，UTC 毫秒",
    "input_json": "具有任务类型的输入",
    "result_json": "已提交结果与幂等记录",
    "ai_json": "模型自报信息",
    "runtime_json": "运行时观测",
    "snapshot_json": "完整历史版本",
    "dataset_version": "案例集内容版本；独立于数据库结构与提取器版本",
    "schema_version": "评测示例字段契约版本",
    "requires_semantic_review": "自动关键词检查后仍需按语义标准复核",
}

def field_description(name):
    if name in FIELDS:
        return FIELDS[name]
    if name.endswith("_at"):
        return "UTC Unix 毫秒"
    if name.endswith("_id") or name == "id":
        return "对象或关联对象标识"
    if name.endswith("_json"):
        return "具有明确领域类型的结构化 JSON"
    return {"status":"显式状态","sequence_no":"会话内顺序","seq_id":"会话内消息顺序",
            "handler_version":"作业处理器版本","dedupe_key":"唯一去重键",
            "created_by":"创建者","changed_by":"修改者","change_reason":"修改原因",
            "author":"事件作者","key":"作用域内状态键","value_type":"精确值类型"}.get(name,name)
