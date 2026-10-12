# JEV / Anchor 真实调用验收（2026-10-10）

使用项目 .env、当前默认 ModelRegistry 模型和合成多轮对话。运行库独立放在 data/eval/perception-live-20261010-b/agent.db；未修改生产数据库、Memory / Action 开关、准入档位或 Anchor 85% 触发比例。

## JEV

- 完整请求端点：https://jevmodel.org/v1/systemone，模型 jev-latest，Bearer 鉴权。
- 用户提供的最小 noul 请求与项目 Judge 均返回 401。服务端说明为 `Invalid or disabled API key.`。请求已到达该端点；本次未验证到成功的真实 JEV 裁决。
- 默认生产模式为 shadow，成功判定也只记录；active 才改变模型输入投影。
- 探针在进程内将准入触发降至 1%、min_chars 降至 512、首尾保留降至 120 字，构造 3 条候选。一次判官失败后按 prior 定案，shadow 保留全部输入；相同定案的 active 投影估算成本由 11836 降至 3197。
- 3 条定案 source 均为 prior，2 条 trim、1 条 defer。SQLite 原文未变；读取完整 3273 字工具原文后，定案被钉住为 keep，后续窗口恢复全文。

依据[端点文档](https://jevmodel.org/docs/)，每次最多 8 道题。原默认 batch_size=8 条上下文，每条 choice+noul 两题，会形成 16 题。新增回归先复现 422，再修正为默认及物理批量上限 4 条；9 条输入拆为 8、8、2 题，顺序保持。密钥仍需由目标服务签发并保持启用；base URL 无需因本次 401 改为其他地址。

## Anchor

- 实际模型：claude 协议适配器上的 deepseek-v4-flash，与当前 Runtime 默认模型一致。
- 合成历史包含 SQLite 强制持久化、7 天改 30 天、迁移检查失败、无关长输出、Memory 关闭与 Action 迁入 core 的约束。输入预算先压至 10000，以长历史触发真实压缩。
- 真实模型生成合法五槽 Anchor，覆盖最旧 3 轮；原始估算成本 28143，压缩后 6422。来源 ID、覆盖 digest、失败 decisions、最近两轮原文保护均通过。
- Anchor 随 Session 提交 SQLite；新 Storage 实例恢复后，低压力窗口直接 reused，无模型调用。
- 后续加入最新 45 天设置及长工具输出。最初两次扩展返回 anchor_model_error，未留下足以细分异常的原始调用记录。单独重试得到真实 JSON：74.547 秒，响应 3170 字符。
- 同一真实响应在 10000 输入预算下触发 ContextBudgetExceededError；最近两轮原文与 Anchor 相加超过测试预算。将测试预算增加至 11000 后离线回放，连续覆盖从 3 轮扩至 7 轮，窗口估算 10323，Anchor 估算 1434。这里复用了真实返回，没有以模拟生成器替代模型结果。
- 主模型读取该窗口，2.743 秒回答：SQLite、45 天、迁移检查未通过，仍缺少 idx_session_scope。真实主模型 usage 为 input_tokens=1891、output_tokens=135；这些是服务端计量，以上窗口成本是项目保守估算，不能混为计费 token。

## 修正与复现

提示词已被工作区迁到 agent/prompt/ 下，旧 prompt_loader / prompt.prompts 导入无法运行。消费者直接采用新模块路径，SYSTEM_PROMPT_PATH 同步调整，未恢复包装模块。

完整后端回归 393 passed；CLI 导入与 git diff --check 通过。飞书 SDK 既有 DeprecationWarning 保留。

可重复运行：

```powershell
.venv\Scripts\python.exe -B tests/live/perception_probe.py --output data/eval/perception-live-新的目录
```

探针会使用真实服务、合成对话和全新 SQLite，输出调用记录与报告。准入阈值仅在探针中降低，Anchor 输入预算为 11000。两条真实能力未全部通过时退出码为 1。

最终机器记录：[verification.json](../../../data/eval/perception-live-20261010-b/verification.json)。第一次完整探针报告、扩展调用记录及回放确认分别保留为 report.json、anchor-extension-calls.json、anchor-extension-confirmed.json。
