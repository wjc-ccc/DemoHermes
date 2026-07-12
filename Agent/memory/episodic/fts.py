"""
EpisodicFTS — L2 全文检索

基于 SQLite FTS5 对历史对话进行关键词搜索：
    - 按关键词召回相关对话片段
    - 支持按 session、时间范围过滤
    - 返回带相关性评分的片段列表

供 tools/memory_tools 的 recall_memory 和 context_builder 使用。
"""
