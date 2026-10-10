"""技能 patch 的读版本与真实 Repository 乐观锁配合。"""


def check_skill(arguments):
    if arguments.get("action") == "patch" and "expected_revision" not in arguments:
        return "stale_precondition"
    if arguments.get("action") == "create" and (not arguments.get("description") or not arguments.get("body")):
        return "missing_skill_content"
    return None

