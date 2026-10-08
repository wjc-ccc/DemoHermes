# Memory 提取与检索行为评测

这套评测回答三个问题：该记的内容有没有保存到正确类别，不该记的内容有没有被忽略，之后能否查到卡片及真实来源。

本次实际运行的结果与发现的问题见 [评测结果报告](memory-behavior-eval-results.md)。当前样例集包含 23 个案例。

## 怎么运行

在项目根目录运行离线检查：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/eval/test_memory_behavior_eval.py tests/core/memory tests/memory tests/storage/sqlite/test_memory_review_scheduler.py tests/storage/sqlite/test_memory_review_committer.py
```

真实模型评测使用当前 `.env` 配置，需要可访问模型服务。每次使用新目录：

```powershell
.venv\Scripts\python.exe -m agent.eval.memory_behavior --live --output-dir data/evals/memory-new-run
```

可用 `--cases profile experience_success greeting` 选择样例。已有 `agent.db` 的目录会被拒绝，避免旧卡片让新评测误判通过。

脚本会启动绑定本机随机端口的 HTTP 后端，走生产 Gateway、AgentLoop、后台 Worker 和 SQLite；结束后关闭这次评测启动的后端。独立目录同时隔离用户手册和技能产物。模型输出不由脚本改写。

## 案例及预期

完整输入和断言位于 `tests/eval/datasets/memory_behavior.json`，在运行模型前就已确定。

| 案例 | 对话内容 | 预期 |
|---|---|---|
| profile | 我是 Python 后端工程师，这是长期职业 | 一张个人资料卡 |
| preference | 以后所有对话偏好中文回复 | 一张稳定偏好卡 |
| ongoing | 持续开发 AuroraNotes 离线笔记项目 | 一张持续事实卡 |
| atomic | Rust 后端职业，以及长期先给结论的偏好 | 拆成两张不同类别的卡 |
| experience_success | Windows 下用虚拟环境运行 pytest，通过；只验证一次 | 成功经验，保留限制 |
| experience_failure | Linux 下换 GBK 后导出仍失败 | 失败经验，不能写成成功 |
| experience_mixed | Redis 开缓存后读取成功、并发写入失败 | 同次实验的混合结果 |
| experience_observed | 改采样间隔后日志减少，未设置成败标准 | 观察经验，不夸大为性能提升 |
| greeting | 寒暄和感谢 | 零张卡，合法空数组 |
| temporary | 仅本次英文解释二叉树 | 零张卡，不能泛化成长久偏好 |
| todo | 今天买牛奶、寄快递、整理桌面 | 零张长期卡 |
| hypothesis | 猜测换线程池更快，尚未执行 | 零张卡，不能成为经验或项目事实 |
| calculator | 使用计算器计算 137 × 29 | 保留工具原文，但不提炼成经验 |
| correction | 跨会话从 Python 后端转岗到 Go 后端 | 只保留新职业，并可主动检索回忆 |
| preference_change | 长期回答偏好从简短改成详细 | 新偏好有效，旧偏好不继续参与检索 |
| forget | 保存项目后明确要求删除、忘掉 | 卡片失效，不再产生遗忘指令卡 |
| secret | 要求保存虚构的测试密码 | 零张卡 |
| third_party | 介绍同事的职业和生活习惯 | 不混成用户自己的记忆 |
| assistant_suggestion | 请助手推荐 Python 框架 | 建议不成为用户已经采用的事实 |
| multi_turn_preference | 临时写代码任务后说明长期类型注解偏好 | 只保存长期偏好 |
| name_recall | 告诉助手以后如何称呼自己 | 个人资料卡，新会话自然提问能查回 |
| dedup | 跨会话重复说明同一职业 | 只保留一张有效卡 |
| self_question | 询问是否适合转行，尚未决定 | 不保存为已有职业或长期事项 |

每组使用独立用户 scope。通过 `/api/reset` 结束学习会话；这也会补提炼不足调度间隔的尾批。渐进调度、重启、跨会话和失败轮次另由已有的调度及生命周期回归测试覆盖，不能把这组单轮真实样例当作完整调度验证。

`sessions` 可描述同一用户连续的多个会话，用于更正、偏好变化、遗忘和去重；普通案例使用 `turns`。每次 reset 后先等待该阶段提炼完成，再进入下一会话。

## 通过标准

- 后端对话正常完成，指定来源的后台作业真正完成。超时、网络错误、模型格式错误都不能冒充正常空结果。
- 预期事实按照类别、预先指定的关键含义及经验结果标识匹配。每个预期只能对应独立卡片，额外卡片也判失败。
- 空样例既不能产生卡片，也不能靠“生成噪声提案再全部拒绝”算作模型选择性提炼正确。
- 每张期望卡都通过真实 `memory_search` 参数找到，并能通过 `memory_read` 分页读取。原文逐字引文必须可回查，其他用户不能读取这些引用。
- 自动召回仅命中经验；资料、偏好和持续事实不混入自动经验上下文。不相关输入不召回。
- 职业案例在新会话中要求模型实际搜索、读取对应卡片后回答；经验案例在新会话中问原先验证过的做法。
- 数据库审计额外检查 SQLite 完整性、外键、原始消息的搜索与读取、工具原文读取，以及实际轮次 Trace 中的经验注入。

## 如何看报告

每次运行目录保留：

- `report.md`：逐例结果、真实卡片、漏提取和检索失败。
- `report.json`：原文、卡片、证据、作业状态、提案及拒绝原因、检索结果、跨会话回答。
- `model_calls.jsonl`：模型的实际输入输出，定位“模型没提取”“格式不对”“被校验拒绝”的差别。
- `database_audit.json`：原文与数据库审计结果。
- `agent.db`：可继续检查的独立评测数据库。
- `backend.log`：连接、超时和后台异常。

离线测试用外部模型 fixture 验证评测器和工程链路，不代表真实模型的分类能力。真实样例使用固定的关键含义断言，没有另请模型给自己打分；这种方法可复查，但仍不能覆盖所有语义错误。真实模型结果有波动，失败重跑必须保留原报告，不能只展示成功的一次。
