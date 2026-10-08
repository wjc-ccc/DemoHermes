# Memory 运行链路

完整报告见 [Memory 当前运行链路与清理结果](../../agent/core/docs/memory_done.md)。

当前实现分为四条链路：SQLite 原始对话与工具事实、每轮冻结的 Markdown 手册与约束、后台选择性提炼的用户卡片、当前 Session 与 Anchor。原始事实保留完整证据；`MEMORY.md` 常驻但严格限长；只有可复用经验在关键词命中时自动召回，最多 3 张；其他卡片和原文通过 `memory_search`、`memory_read` 按需获取。

新卡只由 `MemoryExtractor.extract_batch` 从正常完成的轮次生成，调度点是同一用户 scope 累计第 1、3、6、10、15、20……轮。模型返回 `[]` 即本批不入卡；模型异常使作业重试。Reviewer 与 Validator 校验本批用户消息/工具原文的逐字引文、类别结构、敏感内容和经验结果；Committer 同事务写入卡片、证据、历史版本、Trace 和 Job 结果。

卡片仅有 `kind` 一个分类：`profile`、`stable_preference`、`ongoing_fact`、`reusable_experience`。前三种用对象和属性定位可更正的事实槽位；经验记录具体条件、做法、观察结果、限制及关键词。主模型的 `memory_edit` 只能凭 ID 和预期修订号更正或删除已有卡片，且仅在轮次正常完成后提交。手册可由人或模型独立编辑；两者之间没有自动同步。

当前检索只实现关键词。语义/RAG、混合检索与外部内容导入尚未启用。Task 三态运行模型及调试页面已下线；普通 Session、原始消息、工具记录和 Anchor 保留。数据库结构见 [SQLite 与 Memory 存储](../sqlite-memory-storage.md)。
