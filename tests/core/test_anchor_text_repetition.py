from agent.core.prompt.renderers import render_anchor_input


def test_repeated_tool_lines_are_compacted_without_losing_unique_middle_or_tail():
    body="关键起始：SQLite\n"+"重复日志，没有改变。\n"*90+"中间更正：三十天\n"+"另一重复行\n"*60+"末尾错误：missing index"
    text=render_anchor_input(dict(new_turns=[dict(turn_index=0,start_seq=0,end_seq=0,messages=[dict(
        message_id="m1",role="tool",content=body,tool_calls=[],is_error=True)])]))
    assert all(value in text for value in ("关键起始：SQLite","中间更正：三十天","末尾错误：missing index"))
    assert "90" in text and "60" in text
    assert len(text)<len(body)/2
