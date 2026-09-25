"""User-facing table and column descriptions."""
TABLES = {
    "sessions": ("会话", "会话、当前入口路由及原始 Anchor 载荷"),
    "turns": ("执行", "轮次事实、用量及环境快照"),
    "messages": ("执行", "有序消息及所属轮次"),
    "tool_calls": ("执行", "工具请求、结果和来源引用"),
    "session_workspaces": ("调度态", "工作台状态、计划版本和当前节点"),
    "workspace_items": ("调度态", "任务依赖、分工、输入输出和显式执行状态"),
    "goal_contract_versions": ("叙事态", "目标契约的不可覆盖版本"),
    "narrative_events": ("叙事态", "只追加账本与待采纳观察"),
    "session_state_items": ("机械态", "精确参数的历史版本及 Provenance"),
    "memory_items": ("长期记忆", "当前记忆卡片，包含关键词和证据"),
    "memory_item_versions": ("长期记忆", "记忆及证据的完整历史"),
    "context_documents": ("文档", "Markdown 来源登记"),
    "jobs": ("后台", "通用作业、领取凭证、重试和提交结果"),
    "traces": ("观测", "模块操作摘要"),
    "trace_items": ("观测", "原子观测；分离模型声明和运行时事实"),
    "audit_events": ("审计", "管理操作记录"),
    "schema_migrations": ("系统", "已应用结构版本及校验和"),
}

FIELDS = {
    "payload_json": "完整领域载荷；保留领域时间和 Anchor 原始格式",
    "content_json": "完整记忆卡片",
    "keywords_json": "结构化关键词数组",
    "evidence_json": "证据引用数组，随卡片版本保存",
    "environment_json": "本轮环境快照",
    "revision": "乐观并发版本；修改时校验预期版本",
    "plan_revision": "计划版本",
    "contract_revision": "当前目标契约版本",
    "is_current": "此入口是否选中该会话；同一入口最多一个",
    "session_key": "入口路由键",
    "scope_key": "明确的归属范围",
    "value_json": "机械态精确值；decimal 使用字符串",
    "validity": "当前版本是否失效；不自动回退旧值",
    "source_type": "信息来源类型",
    "source_id": "产生信息的消息、工具或程序标识",
    "executor_ref": "指定执行者；不自动调度",
    "dependencies_json": "同一工作台的依赖节点 ID",
    "lease_token": "本次领取凭证；隔离过期 Worker",
    "lease_expires_at": "领取过期时间，UTC 毫秒",
    "input_json": "具有任务类型的输入",
    "result_json": "已提交结果与幂等记录",
    "ai_json": "模型自报信息",
    "runtime_json": "运行时观测",
    "snapshot_json": "完整历史版本",
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
