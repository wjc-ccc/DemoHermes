# 120 场景长对话核心评测

本评测只新增评测类型、题库、运行器和报告。生产 memory、perception 与提示词使用运行清单中的逐文件 SHA-256 冻结；已有题库内容指纹继续由回归测试保护。

正式题库是三个独立的 schema 3 / version 1.0.0 数据集：perception 60 场景、memory 30 场景、combined 30 场景。每个场景有三个 schema 2 问题，40、80、120 轮各 40 场景，在 25%、60%、100% 历史位置截断。所有证据轮次与有效卡片均绑定题目哈希；评分只读取允许前缀。

四个开关条件为 P0M0、P0M1、P1M0、P1M1，各重复三次。正式回答计划 4,320 条。另选 12 个 perception 和 12 个 combined 场景，在同一数据库、同一会话和真实 Anchor 状态中连续六问；P0M1 / P1M1 各重复三次，共 864 条。

试运行使用独立 20 场景、60 问题，四组各执行一次，共 240 条；不能并入正式结果。试运行期间只允许修正评测设施或尚未发布题目的真实歧义，不调整生产模块，也不能根据模型答案改写标准。

## 执行与恢复

在项目虚拟环境下运行：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe -u -m agent.eval.core_benchmark --pilot --output data/evals/core-benchmark-pilot-20261012-v1 --workers 4
.venv/Scripts/python.exe -u -m agent.eval.core_benchmark --output data/evals/core-benchmark-formal-20261012-v3 --workers 8
```

同一个目录使用同一条命令恢复。模型、配置、题库或评测实现指纹改变后必须使用新目录。实际答案先保存为 `answer.json`，评分另存；已有答案不会因评分重试而重新调用主模型。来源重放通过持久化来源 ID 恢复，检查点数据库按允许前缀冻结。连续复用的六次回答共享真实对话和生产 Anchor，记录正常发生的后台记忆工作。

`--cases`、`--repeats` 和 `--no-reuse` 用于设施排查；使用这些参数的运行不能声称完成完整正式实验。`--workers` 只控制并发，所有条件使用相同模型配置、预算和重试策略。

完整对照预算 250,000、perception 预算 20,000、输出预留 1,500、最多十次工具迭代、主模型重试一次。预检查通过实际协议组装器估算允许前缀能否装入完整输入预算；实际成本始终来自 API usage，估算不得替代 usage。JEV 不参与本实验。

## 记录与数据库

每次运行目录包含 `manifest.json`、`production-freeze.json`、`preflight.json`、`progress.json`、每个检查点的来源快照、每题每条件独立的 `agent.db` 和实际回答。

`model-calls.jsonl` 保存实际请求、工具 schema、输出预算、响应、原始 usage、调用阶段、耗时和失败尝试。提炼、准入、格式修复、Anchor、主模型及裁判分别标记。失败调用不删除；缺失 usage 不按零计算。日志含合成对话内容，不包含配置密钥。

`evaluations.db` 使用已有 `eval_datasets / eval_cases / eval_questions / eval_runs / eval_results` 表。运行按条件及重复轮次分开，绑定题库版本、问题哈希、代码和提示词指纹。仅全部计划结果完成评分后才结束对应运行。

## 指标解释

`report.json / report.md` 汇总四组正确率、配对差异、按场景聚类的 95% bootstrap 区间、逐场景和长度分层节省、memory 提取与检索、硬检查及特殊违规。`answers.csv` 保留逐次质量与成本；`reuse-cost-curve.csv` 报告连续第 1、3、6 问的累计质量和成本；`failures.json` 保留失败原因和真实回答位置。

主输入成本包含一次回答的所有主模型请求和重试。冷启动辅助成本归属到当前检查点前的记忆提炼、本次 Anchor 和记忆空闲提炼；唯一日志总表另列实际产生的调用总量，避免把共享来源快照当成重复 API 调用。复用两组从同一初始 memory 快照分叉，各自后续正常提炼计费。裁判成本始终单列。输入、输出及两者之和分别报告，不配置价格就不能推断货币节省。

纯 perception 单会话比较用于判断压缩的非劣性；跨会话 combined 单独报告生产会话策略的影响。非劣界限为正确率下降不超过两个百分点；只有区间下界达到界限才标记支持非劣，否则报告证据不足或不满足。任何条件未达到 90% 正确率、预标记高干扰组主输入未节省 30%、存在禁止卡或更正/遗忘违规，都必须如实记录。

memory 提取 Recall 的分母是冻结有效事实，卡片 Precision 判断实际卡片是否有来源支持及持久用途；合理额外卡片不因未列入必需事实而判错，同时保留严格黄金卡片匹配率。遗漏事实、无证据卡、负例误建、类别识别及已正确提取卡片的检索 Recall@5 / @10 分列。检索候选在回答时记录，后续评分不能使用未来数据库状态补齐召回。过期、遗忘、更正和成败边界单列。

所有回答完成后按固定随机种子打乱评分顺序，裁判看不到条件名、token 成本或前一次评分。违规以及按目标、长度、条件分层的至少 10% 结果进行第二次独立盲评，原评分和分歧都保留。两次评分使用同一模型，不能当成人工复核；该限制必须随结论披露。

## 当前状态

本地完整回归通过 474 项，新增修订设施的定向测试通过。独立试运行已保存 240 回答、238 条有效评分及 35 次第二评分；两条卡片评分错误保留，未修改原标准。正式题库已发布，包含 120 个独立案例文件、360 个黄金问题以及事实时间线。

试运行的两组 perception 开启条件保守正确率为 71.67% / 60%，不支持非劣；具体数字仅属于独立试运行。`core-benchmark-formal-20261012-v2` 因复用初始 memory 未控制一致而中止，保留 91 条回答及未完成调用 usage 未知标记，不用于正式消融。`core-benchmark-formal-20261012-v3` 正在运行，尚无完整正式结论。

在运行目录创建 `stop-requested` 文件可在来源或问题边界停止，已开始的 API 调用会先保存；删除该文件后用原命令恢复。代码或配置变化仍需新版本。每次调用开始前保存 `inflight` 请求，结束后写入正式调用日志；未完成调用的 usage 保持未知。业务对话使用题目逻辑时钟，API 日志保留真实墙钟与耗时。

正式 4,320 回答全部完成之后，如果复用阶段中断，使用独立阶段恢复入口。它先逐题核对正式矩阵、题库哈希、生产代码、评测代码和模型配置，再调用冻结版本的复用、评分、报告函数；数据库导入同时包含正式及复用题库。入口自身的 SHA-256 另存于 `stage-resume-manifest.json`，不重跑已保存答案。此入口解决 v3 主入口在复用中断后仅传入正式题库导入、出现 `StopIteration` 的评测设施问题。

```powershell
.venv/Scripts/python.exe -u scripts/resume_core_benchmark.py --output data/evals/core-benchmark-formal-20261012-v3
.venv/Scripts/python.exe scripts/audit_core_benchmark.py data/evals/core-benchmark-formal-20261012-v3
```

最终需结合 `integrity-audit.json` 阅读成本结果。审计以唯一调用 ID 合并已返回日志和遗留 `inflight` 请求，核对题目哈希、历史截止、perception 两组原始来源一致性、数据库外键及回答覆盖。若存在未返回 usage 的中断调用，实际产品总成本保持未知；已返回调用之和不能直接声称为完整产品成本，复用净节省结论也必须注明成本完整度。

本次冻结运行器的具体装配为 `AdmissionPolicy(mode="off")`，不调用 JEV；perception 开启组实际验证生产预算、优先级与 Anchor。生产默认 shadow 同样不应用裁剪，但本实验没有验证 active trim / defer 的收益，不能把本次数字推广到主动工具结果裁剪。跨会话 combined 则保留生产 perception 的会话策略，完整历史控制组可读取允许的前序会话，结果另列。

原始结果正文约 2.4 GB，`scripts/finalize_core_benchmark.py` 提供低内存汇总。它使用原统计器，只在本进程解析结果时移除该统计器不用的请求正文、卡片正文和 trace；usage、评分、失败、工具与模型元数据保持相同。原始答案及日志不改写，等价性测试覆盖四组配对统计和调用计数，汇总入口指纹另存 `report-finalize-manifest.json`。

## 独立评分修复版本

原 v3 已保存完整 5,184 回答，但 387 条主评分失败。实际 `memory_read` 返回的 `context` 包含相邻摘要列表，原评测器按对象调用 `.get` 导致至少 299 条失败；另有匹配 ID / JSON 结构等评分失败。这是评测设施问题，不能当成受测模型的答案错误或真实记忆遗漏。

`core-benchmark-formal-20261012-v3-score-v2` 是独立评分版本。它保留生产代码、全部原回答、卡片快照、标准答案和原有效评分，仅解析真实 context 结构，令分类结构错误进入原两次重试，再复评 387 条失败主评分及 347 条失败/缺失复核。原版本与原评分历史保留。评分代码 SHA-256 和原回答清单哈希绑定新版本；新增模型调用全部标记为裁判成本。

```powershell
.venv/Scripts/python.exe -u scripts/rescore_core_benchmark.py --source data/evals/core-benchmark-formal-20261012-v3 --output data/evals/core-benchmark-formal-20261012-v3-score-v2
.venv/Scripts/python.exe scripts/finalize_core_benchmark.py data/evals/core-benchmark-formal-20261012-v3-score-v2
.venv/Scripts/python.exe scripts/plot_core_benchmark.py data/evals/core-benchmark-formal-20261012-v3-score-v2
```

已经结束的产品调用日志用硬链接保持相同字节；需要追加的裁判日志使用独立副本，原日志不追加。逐次数据库结果指向原回答工件。最终数值使用修复评分版本，并同时披露残余评分错误、同模型裁判分歧及八个中断产品调用的未知 usage。绘图库仅安装在 `data/evals/plot-libs`，不改变项目或受测进程的依赖环境。

## 最终状态（2026-10-12）

正式 4,320 回答与连续复用 864 回答全部完成。修复评分版本有 5,142 个有效评分、42 个未知评分，48 个独立数据库运行；21 项设施测试通过。5,184 条生产输出、调用、来源与卡片的前后逐项比较无差异。报告、实际调用日志、数据库、失败列表和四组质量/成本曲线均已交付。

最终结论见 [120 场景结果](audits/core-benchmark-120-results.md)：纯 perception / M0 的质量 95.56% → 75.00%，主输入减少 81.43%，冷启动产品总 token 增加 53.45%；memory 的保守 Recall / Precision 为 91.56% / 84.81%，负例误建为 2 / 27。生产模块未达到本计划的质量与零违规要求，未知成本未作为零计费。
