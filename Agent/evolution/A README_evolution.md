# evolution — 自我进化

Agent 在后台自动学习和优化的机制，无需用户手动干预。

## 文件说明

| 文件 | 职责 |
|------|------|
| `reviewer.py` | 回合后复盘（独立 LLM 调用），评估成败并决定是否触发进化 |
| `memory_curator.py` | 定期压缩/更新 Hot Memory（USER.md / MEMORY.md） |
| `skill_creator.py` | 从复杂任务中自动提取并创建新 Skill |
| `skill_optimizer.py` | Skill 使用过程中的自我修正与优化 |
| `scheduler.py` | 异步任务 / cron 调度，触发上述进化流程 |
| `__init__.py` | 包初始化 |

## 进化触发时机

```
每轮对话结束 → reviewer 评估
  ├─ 记忆冗余 → memory_curator 整理 Hot Memory
  ├─ 发现可复用流程 → skill_creator 创建 Skill
  └─ Skill 执行出错 → skill_optimizer 修正
```

## 与其他模块的关系

- 读写 `memory/` 各层
- 参考 `prompt/templates/evolution_guide.md` 中的进化准则
