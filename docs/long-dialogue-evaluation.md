# 长对话端到端评测

所有历史均为合成数据，参考答案在运行前冻结。新增48个场景、96道问题，各场景40–80轮并设两个历史检查点：

| 内容版本 | 场景 | 问题 | 文件 |
|---|---:|---:|---|
| memory_behavior@3.0.0 | 24 | 48 | tests/eval/datasets/memory_long_v3.json |
| perception_behavior@1.0.0 | 16 | 32 | tests/eval/datasets/perception_long_v1.json |
| combined_behavior@1.0.0 | 8 | 16 | tests/eval/datasets/combined_long_v1.json |

原 memory_behavior@1.0.0、2.0.0 共43个场景、44道问题仍原样保存，指纹保持不变。五个版本合计91个场景、140道问题。新增题库字段契约为2；每题独立保存 `question_schema_version`、`history_end_turn_id` 和 `expected_active_memory_ids`。题目内容版本跟随题库，没有独立内容版本。

```powershell
.venv\Scripts\python.exe -m agent.eval.long_dialogue --output data/evals/long-new-run --workers 2 --repeat-failures 2
.venv\Scripts\python.exe scripts/audit_long_dialogue_storage.py data/evals/long-new-run
.venv\Scripts\python.exe scripts/summarize_long_repair.py data/evals/long-new-run
.venv\Scripts\python.exe scripts/check_long_repair_acceptance.py data/evals/long-new-run
```

可用 `--datasets memory_behavior` 或 `--cases profession_phone` 选择范围。实现与题库指纹写入 manifest；源码变化后必须使用新目录，不能混用旧缓存。运行期间不要修改agent源码。

历史助手和工具输出按 fixture 回放，实际经过 SessionStore 持久化；Memory 提取、Anchor、当前问题回答、工具选择与语义审查均使用真实模型。JEV 服务排除，独立的固定 Judge 回归验证准入逻辑。每题采用独立 scope 和数据库；完整历史基线在问题开始前以 SQLite backup 冻结，防止回答污染。检索时钟使用题目日期，SQLite 写入时间保持真实时间。

参考答案和评分标准仅发送给独立审查调用，不进入提取器、Anchor 或回答模型。卡片召回要求预期事实对应实际卡片 ID，类别及持久证据必须匹配；回答里出现事实不能冒充卡片召回。评分结构非法或自相矛盾时修复一次，仍非法记失败。实际回答保持不可改写。

每题保存 `agent.db`、`baseline.db`、`model-calls.jsonl` 和 `result.json`。模型录制保留实际请求、schema、参数、usage、耗时和结束原因；主模型输入节省同时报告首次真实usage对照及统一估算对照。`quality-comparison.json/md`将初次和复跑的提取、准入、格式修复、Anchor及评分成本分别统计，并核对问题轮次的实际memory_read调用与P3读回引用。未知用量独立计数，调用合计耗时不等于并行墙钟时间。失败初次结果保持原样，并在 `reruns/attempt-1`、`attempt-2` 保留两次重新提取和回答。

总报告 `report.json` 和 `evaluations.db` 保存逐题结果及版本绑定。必须同时报告硬性约束、纯负例/禁止卡/更正遗忘违规、记忆召回、答案语义及主模型节省。代码测试绿色不等于真实模型达到用户更新后的90%验收阈值；当前状态见 `docs/audits/storage_memory_perception_repair.md`。

`acceptance.json`逐项核对96题覆盖、冻结版本、当前实现、完整存储、全部尝试的硬约束、零误建与违规、两项90%质量阈值、全部48道对照题至少30%真实输入节省及两次失败复跑。首次请求和整个回答轮次都计入，防止反复读回抵消首次压缩收益。任一未达标时验收命令返回非零；Python和前端回归的独立证据另见验收台账。
