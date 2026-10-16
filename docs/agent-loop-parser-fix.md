# AgentLoop正式响应解析修复（2026-10-15）

用户明确要求检查并一并修复正式AgentLoop解析。本次生产代码调整属于该请求授权。

## 核对结论

正式AgentLoop调用provider适配器得到ModelResponse，工具调用转为ToolCallRequest后交给Handler/Dispatch。JEV由Perception调用provider.build_context_judge构造的SystemOneContextJudge，原先常规平铺协议能正确解析；此前Flash评测适配层另有格式归一化实现。

已修复以下真实边界问题：

1. 判官归一化移到正式SystemOne解析入口，生产与评测共用。兼容已观察到的iN嵌套、iN直接类别和数值nN，冲突或缺失仍未知，不猜数；NaN、正负无穷、布尔概率不再被当成置信度。正常有限越界概率仍沿用原有裁剪语义。
2. Claude/Doubao响应不再把[]/false/0/null直接覆盖成{}；原参数留档。非Action工具执行路径补充原始参数语法、对象类型与归一化参数一致性检查，非法输入回注invalid_argument_syntax，不执行工具。正常空对象{}仍可执行。回注Anthropic历史时维持input对象协议，原始错误参数不从Session删除。
3. length/max_tokens截断输出不再标记completed=True：保存failed助手记录，返回model_output_truncated，不执行未完整生成的工具批次。空截断不再按空响应重复请求。下一轮组装会排除未闭合的失败工具协议，原文失败事实仍留档。

对应正式模块回归测试保留在`tests/core/test_agent_response_parsing.py`；历史评测审计产物已清理。
