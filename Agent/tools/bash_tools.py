"""
BashTools — 终端命令执行工具

Agent 执行 shell 命令的能力：
    run_command : 在指定目录下执行 shell 命令，返回 stdout/stderr

用于运行测试、安装依赖、git 操作等。
需要安全控制：命令白名单、超时限制、工作目录限制。
"""
