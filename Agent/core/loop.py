"""
AgentLoop — 核心对话循环

每轮对话的执行流程：
    observe  → 接收用户输入，记录事件
    think    → 调用 model 推理，决定是否使用工具
    act      → 通过 tools/registry 执行工具调用
    persist  → 将本轮轨迹写入 memory（episodic + hot）

由 gateway（cli / api）启动，是整个 Agent 的调度中枢。
"""
