# Tool & Skill & Plugin - 能力扩展体系

> 对应代码：`agent/tools/`、`agent/skills/`、`plugin/`（预留）
> 地位：Agent Loop 的"手脚"和"经验包"。Tool 给 LLM 提供确定性执行能力，Skill 给 LLM 提供流程经验，Plugin 是外部扩展位。

---

## 一、定义是什么

这三者都是**给 Agent Loop 扩展能力**的机制，但定位完全不同：

### Tool（工具）
**确定性的原子能力**。一个函数级的执行单元：输入参数确定 -> 输出结果确定（除了抛异常）。LLM 通过 function calling 协议决定何时调用、传什么参数，Loop 负责执行并把结果回注。

```
LLM: "我需要算 23*17" -> tool_calls=[{name:"calculator", args:{expression:"23*17"}}]
Loop: ToolRegistry.dispatch("calculator", {expression:"23*17"})
      -> ToolResult(ok=True, result_text="23*17 = 391")
Loop: 把 "23*17 = 391" 作为 role=tool 消息回注给 LLM
```

### Skill（技能）
**经验包 / 流程指南**。本质是一个 `SKILL.md` 文件，告诉 LLM "遇到某类任务该按什么步骤做"。它**不直接执行代码**，而是把"怎么做"的说明注入到 LLM 的上下文里，让 LLM 按说明一步步思考。

```
system prompt 里注入: "可用技能：weekly_report - 把零散工作整理成周报"
LLM: "用户要写周报，我先调用 use_skill 加载说明"
      -> tool_calls=[{name:"use_skill", args:{skill_name:"weekly_report"}}]
Loop: UseSkillTool.execute -> 返回 SKILL.md 正文
LLM: 看到正文后，按其中步骤逐步执行（提取事项 -> 分类 -> 格式化输出）
```

### Plugin（插件）
**外部插拔式扩展位**（预留）。设计意图是把 tool / skill / memory 的"外部化"统一到 `plugin/` 目录下动态加载，当前阶段未实现，只有空目录骨架。

---

## 二、负责的任务是什么

### Tool 的职责
1. **执行确定性操作**：计算、查数据库、调外部 API、读写文件
2. **声明自己的能力**：通过 `name` / `description` / `parameters`（JSON Schema）让 LLM 理解"什么时候调、怎么调"
3. **返回结构化结果**：`ToolResult(ok, result_text, data, error)`，result_text 回注 LLM，data 供前端展示
4. **自我容错**：实现内部应捕获预期异常返回 `ok=False`，不让异常打断 Loop

### Skill 的职责
1. **引导 LLM 按流程做事**：复杂任务（周报、代码评审、故障排查）通过 SKILL.md 给出标准化步骤
2. **两级加载节省 token**：
   - Level 1：所有技能的一行摘要注入 system prompt（让 LLM 知道"有哪些技能"）
   - Level 3：实际要用时通过 `use_skill` 工具加载完整正文（避免一次性灌入所有技能全文）
3. **可被 Agent 自建**：`data/skills/` 目录存放运行期 evolution 模块生成的技能

### Plugin 的职责（预留）
1. 统一管理外部 tool / skill / memory 的动态加载
2. 支持运行期插拔，不重启进程

---

## 三、能力清单

### 3.1 Tool 体系

| 组件 | 文件 | 能力 |
|------|------|------|
| `Tool`（抽象基类） | `tools/base.py` | 定义 name/description/parameters/execute 接口 |
| `ToolResult` | `tools/base.py` | 统一结果模型（ok/result_text/data/error） |
| `ToolRegistry` | `tools/registry.py` | 注册、schema 收集、容错分发 |
| `CalculatorTool` | `tools/calculator_tools.py` | 安全的数学表达式求值（AST 白名单，不用 eval） |
| `UseSkillTool` | `skills/skill_tool.py` | Skill 与 Tool 的桥梁，加载技能正文 |
| `BashTool` / `FileTool` / `MemoryTool` | `tools/*.py` | 骨架，待完善 |

**ToolRegistry 核心能力：**
- `register(tool)`：注册工具实例
- `get_schemas()`：返回所有工具的 OpenAI function calling schema（随 chat 请求发给 LLM）
- `dispatch(name, args)`：**容错分发**，任何异常兜底成 `ToolResult(ok=False)`，绝不抛给 Loop

### 3.2 Skill 体系

| 组件 | 文件 | 能力 |
|------|------|------|
| `Skill`（数据模型） | `skills/base.py` | name/description/body/path，支持从 SKILL.md 解析 |
| `SkillRegistry` | `skills/registry.py` | 扫描目录自动注册、两级加载接口 |
| `UseSkillTool` | `skills/skill_tool.py` | 让"加载技能"复用工具调用链路 |
| `weekly_report` | `skills/builtin/weekly_report/SKILL.md` | 内置示例技能 |

**SkillRegistry 核心能力：**
- 启动时扫描 `agent/skills/builtin/*/SKILL.md` + `data/skills/*/SKILL.md`
- `summaries_prompt()`：Level 1 摘要块（注入 system prompt）
- `load_content(name)`：Level 3 完整正文（use_skill 调用时返回）

### 3.3 SKILL.md 格式

```markdown
---
name: weekly_report
description: 把零散工作记录整理成结构清晰的中文周报
---

# 周报生成技能

## 步骤
1. 从用户描述中提取工作事项...
2. 按"完成/进行中/阻塞"分类...
3. 按格式输出...

## 输出格式
...

## 注意
- 不要编造用户没提过的工作内容
```

frontmatter 只支持最简单的 `key: value`，不引入 yaml 依赖。

---

## 四、功能边界是什么

### Tool 的边界
**做的事：**
- 执行单一确定操作
- 声明 schema 供 LLM 调用
- 返回结构化结果

**不做的事：**
- 不做多步编排（那是 Loop 的工具内循环干的）
- 不直接调模型
- 不写 Session（结果交给 Loop 回注）
- 不做平台 I/O（除非工具本身就是"发飞书消息"这种）

### Skill 的边界
**做的事：**
- 提供流程指南文本
- 通过 use_skill 工具被加载

**不做的事：**
- 不直接执行代码
- 不自己调工具（是 LLM 看了说明后去调）
- 不持久化状态

### 关键边界：Skill 不等于 Tool
- Tool 是"手"：替 LLM 执行它做不了的事（精确计算、查实时数据）
- Skill 是"脑"：教 LLM 怎么组织思考和输出（写作模板、排查流程）
- 同一个任务可能既用 Skill 又用 Tool：比如"写周报"先 use_skill 加载模板，再用 file_tool 读取工作记录

### Plugin 的边界（预留）
- 只做加载和管理，不实现具体能力
- 不侵入 Loop 主流程

---

## 五、如何相连

### 5.1 与 Agent Loop 的连接

```
Loop.__init__(tool_registry=..., skill_registry=...)

think 阶段:
   schemas = tool_registry.get_schemas()   # 发给 LLM
   skill_summary = skill_registry.summaries_prompt()  # 注入 system prompt

act 阶段（模型要求调工具）:
   result = tool_registry.dispatch(name, args)
   # 如果 name == "use_skill", 内部调 skill_registry.load_content
```

**关键设计：Skill 调用复用 Tool 调用链路。** Loop 无需为 Skill 单开分支，`use_skill` 就是一个普通 Tool，只是它的 execute 返回的是技能说明文本。

### 5.2 Tool 与 Skill 的桥梁

```
UseSkillTool.__init__(registry: SkillRegistry)
UseSkillTool.execute({skill_name:"weekly_report"})
   -> registry.load_content("weekly_report")
   -> ToolResult(result_text="已加载技能「weekly_report」，请按以下说明执行：\n\n...")
```

LLM 收到这段文本后，把它当作"操作手册"指导后续行动。

### 5.3 与 ContextBuilder 的连接

ContextBuilder 构造时传入 skill_registry：

```python
builder = ContextBuilder(session, skill_registry=self.skills, ephemeral_prompt=...)
# building_context() 时:
# 1. system prompt
# 2. skill_registry.summaries_prompt()  <- Level 1 摘要
# 3. ephemeral 平台说明
# 4. 历史消息
```

### 5.4 与 EventBus 的连接

工具执行时 Loop 发 `tool_call_start` / `tool_call_result` 事件，前端可实时显示"正在调用 calculator -> 结果: 391 (耗时 12ms)"。

---

## 六、例子

### 6.1 CalculatorTool 完整调用链

```python
# 用户: "帮我算 (3+5)*2 + sqrt(16)"

# 1. Loop think: ContextBuilder 组 messages，schemas 里含 calculator
# 2. model.chat 返回:
#    tool_calls=[{name:"calculator", arguments:{expression:"(3+5)*2 + sqrt(16)"}}]
# 3. Loop act: ToolRegistry.dispatch("calculator", {expression:"(3+5)*2 + sqrt(16)"})
#    -> CalculatorTool.execute
#    -> _safe_eval(ast.parse("(3+5)*2 + sqrt(16)", mode="eval"))
#    -> 20.0
#    -> ToolResult(ok=True, result_text="(3+5)*2 + sqrt(16) = 20", data={"value":20})
# 4. Loop 回注: session.add_message(Message(role="tool", content_text="...=20"))
# 5. 下一轮 think: LLM 看到 20 -> 返回纯文本 "(3+5)*2+√16 = 20"
```

### 6.2 Skill 调用链

```python
# 用户: "帮我写个周报，这周修了3个bug，做了1个需求"

# 1. system prompt 已注入: "- weekly_report: 把零散工作整理成周报"
# 2. model.chat 返回:
#    tool_calls=[{name:"use_skill", arguments:{skill_name:"weekly_report"}}]
# 3. ToolRegistry.dispatch("use_skill", {skill_name:"weekly_report"})
#    -> UseSkillTool.execute
#    -> registry.load_content("weekly_report")
#    -> ToolResult(result_text="已加载技能「weekly_report」，请按以下说明执行：\n\n# 周报生成技能\n## 步骤...")
# 4. Loop 回注这段文本给 LLM
# 5. 下一轮 think: LLM 按技能说明，把"修3个bug+做1个需求"组织成周报格式输出
```

### 6.3 工具失败的自我修正

```python
# 模型调 calculator 传了 expression="23*17a"（非法字符）
# dispatch -> CalculatorTool.execute -> _safe_eval 抛 ValueError
# CalculatorTool 内部 catch -> ToolResult(ok=False, error="表达式无法计算: ...")
# 回注给 LLM: "工具执行失败: 表达式无法计算: ..."
# 下一轮: LLM 看到错误 -> 改成 expression="23*17" 重新调用 -> 成功
```

---

## 七、往往需要优化哪些问题

### Tool 侧

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **无超时控制** | 工具执行可能永久卡住 | dispatch 加 timeout，超时返回 `ok=False` |
| **无并行执行** | 多个 tool_calls 串行跑 | 独立调用用 ThreadPool 并行 |
| **无权限/沙箱** | bash_tool 能执行任意命令 | 加命令白名单或容器沙箱 |
| **schema 手写** | parameters 是手写 JSON Schema | 用 pydantic 模型自动生成 schema |
| **无调用限流** | LLM 可能疯狂重试同一个失败工具 | 加同名工具连续失败计数，超限拒绝 |
| **结果过大** | 大查询结果全量回注撑爆上下文 | result_text 截断 + data 完整存 Session |

### Skill 侧

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **frontmatter 功能弱** | 只支持简单 key:value | 真用 yaml，支持 tags/version/author |
| **无版本管理** | 技能更新无回滚 | 加 version 字段，保留历史版本 |
| **无效果追踪** | 不知道哪个技能好用 | 记录 skill 被调用后的任务成败，供 evolution 优化 |
| **Level 2 缺失** | 只有 Level 1（摘要）和 Level 3（全文） | 加 Level 2：按当前任务召回相关技能（类似 RAG） |
| **无冲突检测** | 两个技能描述重叠时 LLM 可能乱选 | 注册时做相似度检查 |

### Plugin 侧

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **未实现** | 只有空目录 | 定义 plugin manifest 格式，支持动态加载 tool/skill |
| **无隔离** | - | 插件代码在独立进程或容器，避免主进程崩溃 |

---

## 八、设计原则小结

1. **Tool 是手，Skill 是脑**：Tool 替 LLM 执行，Skill 教 LLM 思考，不要混用。
2. **Skill 复用 Tool 链路**：`use_skill` 就是个普通 Tool，Loop 不为它开特例，架构最简。
3. **两级加载省 token**：摘要常驻 system，全文按需加载，避免上下文爆炸。
4. **容错在 Registry**：Tool 内部尽量自愈，Registry 做最后一道兜底，保证 Loop 不被工具异常打断。
5. **schema 即契约**：Tool 的 parameters 就是它与 LLM 的契约，描述要清晰、约束要明确。
6. **Plugin 预留位**：现在不实现，但 tool/skill 的注册接口已为动态加载留好空间。
