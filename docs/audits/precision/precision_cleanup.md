# Precision 清理与全文展示实施记录

日期：2026-10-10。基于当前工作区，P3 保留，Anchor 覆盖协议保留。

## 删除依据

| 删除对象 | 依据 | 替代方式 |
| --- | --- | --- |
| PrecisionResult.summary | 无实际调用 | 直接使用结构化 anchor |
| context_anchor_payload | 仅测试使用 | 测试与生产一样读取 anchor |
| _origin_ref | 恒定返回 None | ContextItem.origin_ref 默认值；P3 字段继续保留 |
| ManualRead.external_change | 仅测试读取 | 用修订号及真实版本表验证变更 |
| engine.re / anchor.Anchor / result.json | 无符号使用 | 删除 import |
| MarkdownBook、MemoryManualEditor、ManualEditor 协议 | 两套读写与重复读取分支 | 一个 MarkdownDocuments 模块 |

## 文档职责

MarkdownDocuments 统一定位、读取、换行规范化、快照、哈希、内容校验和原子写入。
Policy/Project 路径由启动配置授权；AI 只能通过 replace_user/restore_user 编辑运行期 scope 对应的 MEMORY.md。
冻结、检索、memory_edit 和 Action 的资源定位共享生产实例与校验配置。
登记表仍保存文档索引，版本表仍保存有效历史正文；数据库提交失败恢复原文件字节。

DocumentRead 的四个字段都有实际消费者：snapshot 用于注入/检索，diagnostics 解释回退，disk_hash 防止覆盖人工改动，revision 标识可恢复历史。

## 全文与 Trace

每次构建按 turn_id + iteration 保存完整最终 messages、当次工具 schemas、定案与指标。
快照在调用模型前深拷贝，重试复用，读取窗口返回隔离副本。失败迭代 messages=None 并附错误码。
现有根级指标/records 用于列表摘要；builds 用于逐迭代详情。P3 的暴露、回取入口和指标保留。

全文与 previews 分开：previews 仍可截断以供列表检查，messages 不截断。
Gateway 将 builds 存入现有 Turn payload；详情接口按 iteration 返回一份快照，列表不返回全文。
旧记录明确返回 full_context_not_saved，失败构建不回退成上一迭代全文。

查看位置：Loop 的“调用大模型”步骤及 Precision Trace 的 context 列。
弹窗支持迭代选择、完整文本、原始 JSON、复制和定案原因；沿用调试开关及 HTTP 认证。

## 验证

相关 Python 回归 330 项通过；前端测试 11 项通过；类型检查与生产构建通过。
新增验证覆盖深拷贝、P3 回取、长正文、多次迭代、失败输入、持久化后读取、接口认证、人工变更一致性和数据库失败回滚。
隔离浏览器 QA 已验证 Loop/Trace 两个入口、迭代选择、JSON 中正文末尾及复制成功。

已有静态审计脚本已更新为迁移后的类型路径；其结果仍是候选证据，不能按名字搜索自动删除字段。
