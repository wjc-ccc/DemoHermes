"""
Session — 会话生命周期（说明）

运行时实现见 gateway/session_store.py（JsonlSessionStore / MemorySessionStore）。
core/types.Session 是数据结构；Store 负责 get_or_create / save / load / reset。
"""
