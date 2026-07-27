"""
EpisodicFTS — L2 全文检索（后置占位，ADR-8）。

首版历史检索使用 scope 约束的参数化 LIKE（见 episodic/store.py 的 archive
reader），本文件只是后续 FTS5 migration 的显式落点，当前不含实现。

规划方向（实施前以方案文档 §6.4 为准）：
    - 基于 SQLite FTS5 的关键词召回，按 session、时间范围过滤
    - 数据量达阈值后以独立 migration 启用；普通 B-tree 索引不冒充全文索引
    - 向量检索未来作为 MemorySearchBackend adapter，不改 Manager.search
"""
