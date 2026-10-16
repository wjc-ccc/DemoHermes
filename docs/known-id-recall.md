# 已知ID召回与JEV读回修复 v1

目标：当前授权范围内、按现有生命周期规则可读的数据库记录，在给出完整引用或唯一原始ID时确定性定位。ID查找不依赖正文关键词，也不依赖LLM推断文件路径。

## 已实现

1. `MemoryRetriever.search`在关键词检索之前按主键处理message/tool/card/document/manual引用，原始ID可在对应类型中精确定位。消息读回覆盖user、assistant、tool。
2. `memory_read`接受完整引用或唯一原始ID，返回标准引用供后续追踪和钉住。跨类型ID重复时要求明确前缀，不擅自选取。
3. `memory_read(query=...)`定位正文中的目标片段；搜索返回`read_offset`，并在真实发送给模型的文本中显示“读取偏移”。返回预算收缩时保留query命中，目标无法容纳时明确报错。
4. JEV的trim占位符把准确的数据库引用和可直接调用的memory_read放在第一行，说明引用应原样复制。系统提示区分数据库引用与本机路径。按message:读回受裁剪工具消息时，同样触发keep/missed全文钉住。
5. 评测FixtureTool搜索覆盖用户、助手和工具消息，支持相同的精确ID、query定位和偏移语义。文件路径不存在时返回可见范围内的同名候选，不自动把错误路径重定向到另一文件。

JEV判官需要度、权重、阈值和首尾裁剪规则未调整。本次改变的是读回提示与确定性寻找/读取链路。

## 调用示例

```python
memory_search(query="message:<message_id>")
memory_search(query="<唯一原始ID>")
memory_read(reference="message:<message_id>")
memory_read(reference="tool:<tool_call_id>", query="目标批次ID")
memory_read(reference="tool:<tool_call_id>", offset=搜索返回的read_offset)
```

卡片使用card:，已登记文档使用document:，历史手册使用manual:版本号。来源权限与已删除、过期卡片的限制保持生效。定位query只影响返回页，不改数据库原文；分页offset/next_offset使用原始字符串字符偏移。

## 验收结果

全题库离线命令：

```powershell
python scripts/verify_known_id_recall.py
```

读取黄金库时使用SQLite只读连接，在临时数据库中调用正式MemoryRetriever；逐页拼接结果，与冻结原文逐字比较。没有远程模型调用。

| 范围 | 检查 | 结果 |
| --- | --- | ---: |
| 40份长短原文 | 正式SQLite完整分页读回 | 9,675 / 9,675 |
| 同上 | 正式SQLite完整引用精确搜索 | 9,675 / 9,675 |
| 同上 | 原始ID读取、原始ID搜索 | 各9,675 / 9,675 |
| 同上 | 跨用户读取与搜索拒绝 | 9,675 / 9,675 |
| 120个checkpoint | 评测可见引用搜索、读取 | 各23,054 / 23,054 |
| checkpoint边界 | 未来消息拒绝 | 80 / 80 |
| 问题中直接给出目标批次的长短输入 | 正式工具与评测工具定位目标片段 | 各116 / 116 |

9,675的统计单位是“原文版本＋message/tool引用”，23,054的单位是“checkpoint＋可见引用”，同一ID在长短版本和不同checkpoint中会重复检查；这些数字不是题目数量。题库仍为20场景、40份原文、60道独立题、120份长短问题输入。

其余card、document、manual类型由独立临时SQLite回归测试覆盖，包含卡片版本、删除/期限、历史手册、权限、Unicode分页、ID跨类型冲突和预算缩页。

47项相关测试通过，包含真实AgentLoop→MemoryReadTool→SQLite→下一轮模型请求的集成检查。该集成检查使用确定性本地模型替身，验证链路与证据注入，不把它当作Flash的真实答题表现。

测试范围：

- tests/core/memory/test_known_id_retrieval.py
- tests/core/memory/test_memory_retrieval.py
- tests/core/test_perception_admission.py
- tests/test_golden_eval.py
- tests/core/test_agent_loop_memory_scope.py
- tests/memory/test_memory_invalid_manual_retrieval.py
- tests/memory/test_memory_recall_lifecycle.py

## 保留与边界

原始七表指纹与原v3运行冻结指纹一致；240份原答案及原评分指纹一致。没有覆盖原结果，也没有新建付费作答运行。代码及提示词变化后，现有运行器要求使用新run-id。

验收JSON：data/evals/known-id-recall-v1.json。交付清单和回归测试范围：data/evals/known-id-recall-v1-validation.json。

100%是本次已知有效ID的定位、数据库读取和指定片段读回实测通过率，不是LLM工具选择率或最终回答正确率。不存在、跨范围、不可读状态或无法容纳的预算明确报错；不会靠猜路径或伪造成功补齐数字。Flash的新版本答题质量没有在本次重复付费验证。
