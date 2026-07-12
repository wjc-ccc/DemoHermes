<!--
  tools_guide.md — 工具使用指引模板

  用途：作为 System Prompt 的第二层（tools）注入，告诉 LLM 如何正确调用工具。
  加载方：prompt/builder.py（结合 tools/registry 的 schema 动态生成）

  应包含：
    - 工具调用的一般规范（何时该用、何时不该用）
    - 各工具的简要说明与使用示例
    - 工具调用失败时的处理策略
-->
