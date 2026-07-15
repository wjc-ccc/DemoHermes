"""
Channel 包：各平台适配器注册入口（后续动态加载）。

当前约定：
    - 每个渠道实现 BaseChannel：parse_inbound / deliver
    - Gateway.register_channel() 注册后，只通过 in_dto / out_dto 与核心交互
    - 字段映射（如微信 content→text）只在对应 channel 文件里做
"""
