"""Offline models read the public text format; scoring data stays out of it."""
import json
import re


def memory_output(cards=(), *, skipped=(), **kwargs):
    return json.dumps({"cards": [
        {"retention_reason": "测试中的明确长期事实或有边界的真实观察",
         "future_use": "后续相同主题沟通或相同条件排查时使用", **card}
        for card in cards
    ], "skipped": list(skipped)}, **kwargs)


def decode_memory_input(text):
    sources, cards = [], []
    turn = None
    block = None
    card = None
    for line in text.splitlines():
        match = re.fullmatch(r"## Turn (\S+) \| Session (\S+)", line)
        if match:
            turn = {"turn_id": match[1], "session_id": match[2], "user_message_id": "",
                    "user_text": "", "assistant": [], "tools": []}
            sources.append(turn)
        match = re.fullmatch(r"### Source (\S+) \| Role (\S+) \| Offset (\d+) \| Total (\d+)", line)
        if match:
            block = {"id": match[1], "role": match[2], "lines": []}
        elif line == "### End source":
            content = "\n".join(block["lines"])
            if block["role"] == "user":
                turn.update(user_message_id=block["id"], user_text=content)
            elif block["role"] == "tool":
                turn["tools"].append({"tool_call_id": block["id"], "result": content})
            else:
                turn["assistant"].append({"message_id": block["id"], "text": content})
            block = None
        elif line == "### Existing card":
            card = {"summary": ""}
        elif line == "### End card":
            cards.append(card)
            card = None
        elif card is not None:
            if line.startswith("Kind: "):
                card["kind"] = line[6:]
            elif line.startswith("Slot: "):
                card["slot_key"] = None if line[6:] == "-" else line[6:]
            elif line.startswith("> "):
                card["summary"] += ("\n" if card["summary"] else "") + line[2:]
        elif block is not None and line.startswith("> "):
            block["lines"].append(line[2:])
    return {"sources": sources, "existing_cards": cards}
