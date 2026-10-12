"""Hand-labelled long dialogues. Historical filler is unrelated, scoped conversation.

The standards are authored before any model run; answers never enter the replay.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
from agent.types.eval.datasets import EvalDataset

FIXTURES = Path(__file__).resolve().parents[3] / "tests/eval/datasets"
FILLER = (
    ("这次练习，把‘小船靠岸，天色渐暗’改成一句更自然的话。只是练习文本。", "小船在渐暗的天色中靠了岸。"),
    ("帮我把这段虚构故事中的苹果、梨、桃排成三项清单。", "- 苹果\n- 梨\n- 桃"),
    ("本轮只解释‘缓缓’和‘慢慢’在这句练习里的区别。", "两者都表示速度不快，这段里‘缓缓’更书面。"),
    ("这里有一个临时标题‘雨后的窗’，给它换一个近义标题。", "雨停之后的窗边。"),
    ("把练习人物小周说的‘我爱热闹’转成第三人称转述；这不是我的偏好。", "小周说他喜欢热闹。"),
    ("本次演示只把 A、B、C 三个占位符按字母顺序列出来。", "A、B、C。"),
    ("这次只给虚构的展览起一个临时名字，不用沿用到其他事情。", "可以暂用‘微光与纸页’。"),
    ("这段是小说素材：角色在松桥镇散步。检查有没有地点信息。", "有，松桥镇是这段小说的地点。"),
    ("只为这次练习，把‘先开窗，再整理桌面’拆成两步。", "第一步开窗，第二步整理桌面。"),
    ("把示例里的空行解释一下即可，不需要操作任何实际文件。", "空行用于分隔段落。"),
    ("这次只统计‘春夏秋冬’有几个汉字。", "四个汉字。"),
    ("练习中有人建议采用双栏；目前没有执行，也没有结果。换一种说法。", "双栏只是一个尚未尝试的建议。"),
    ("把临时草稿的‘结束’换成更温和的词，这次用一次就好。", "可以用‘暂告一段落’。"),
    ("这轮只说明这段虚构对话里有问句，不要把人物的看法当成我的。", "有一个问句，人物观点不代表你。"),
    ("谢谢，这个小练习到这里结束。接下来的事项另算。", "收到，这次练习结束。"),
    ("刚才的占位名字没有现实身份；现在只检查标点是否清楚。", "标点清楚，这些名字仍是练习占位符。"),
)


def memory(identity, kind, claim, evidence, *anchors, conditions=(), outcome=None, until=None):
    value = dict(memory_id=identity, kind=kind, claim=claim, evidence_turn_ids=list(evidence),
                 anchors=[[word] for word in anchors], query=" ".join(anchors), conditions=list(conditions))
    if outcome:
        value["outcome_status"] = outcome
    if until:
        value["valid_until"] = until
    return value


def question(text, answer, evidence, active=(), *, required=(), forbidden=(), action="answer"):
    return dict(text=text, reference_answer=answer, evidence_turn_ids=list(evidence),
                expected_active_memory_ids=list(active), expected_memory_ids=list(active),
                required_meaning=list(required) or [answer], forbidden_meaning=list(forbidden),
                expected_action=action, requires_semantic_review=True)


def scene(identity, title, events, memories, before, after, *, abilities=(), target="memory", count=40):
    def turn_id(index):
        return f"{identity}.t{index:02d}"
    turns = []
    for index in range(1, count + 1):
        user, assistant = FILLER[(index - 1) % len(FILLER)]
        turn = dict(turn_id=turn_id(index), user_text=user, assistant_text=assistant)
        if index in events:
            event = events[index]
            turn.update(dict(user_text=event) if isinstance(event, str) else event)
            turn["assistant_text"] = event.get("assistant_text", "收到这条记录。") if isinstance(event, dict) else "收到这条记录。"
        turns.append(turn)
    split = count // 2
    history = [dict(session_id=f"{identity}.s1", occurred_at="2026-10-01T09:00:00+08:00", turns=turns[:split]),
               dict(session_id=f"{identity}.s2", occurred_at="2026-10-03T09:00:00+08:00", turns=turns[split:])]
    if target == "perception":
        history = [dict(session_id=f"{identity}.s1", occurred_at="2026-10-01T09:00:00+08:00", turns=turns)]
    expectations = copy.deepcopy(memories)
    for expected in expectations:
        expected["evidence_turn_ids"] = [turn_id(index) for index in expected["evidence_turn_ids"]]
    questions = []
    for ordinal, (original, end, asked) in enumerate(((before, split, "2026-10-02T09:00:00+08:00"),
                                                    (after, count, "2026-10-04T09:00:00+08:00")), 1):
        q = copy.deepcopy(original)
        q.update(question_id=f"q{ordinal}", question_schema_version="2", history_end_turn_id=turn_id(end), asked_at=asked)
        q["evidence_turn_ids"] = [turn_id(index) for index in q["evidence_turn_ids"]]
        questions.append(q)
    return dict(case_id=identity, title=title, description="跨主题长对话，两个独立的历史前缀检查点。",
                scenario=identity, target=target, difficulty="long_context", abilities=list(abilities) or ["long_history", "provenance", "temporal"],
                history=history, expected_memories=expectations, questions=questions,
                ignored_information=["所有虚构练习、临时改写、助手建议和未执行计划"],
                strict_card_count=not expectations)


def memory_scenes():
    cases = []
    cases.append(scene("profession_phone", "职业更正与手机阅读", {
        1:"我是 Python 后端工程师。", 12:"以后在手机上回复我时，先给短版结论。",
        36:"更正我的职业记录：我现在是 Go 后端工程师，Python 是以前的岗位。"}, [
        memory("old_job","profile","用户此前是 Python 后端工程师",[1],"Python","后端"),
        memory("phone","stable_preference","手机先给短版结论",[12],"手机","短版"),
        memory("new_job","profile","当前是 Go 后端工程师",[36],"Go","后端")],
        question("我做什么职业？按我在手机上惯常的阅读方式介绍。","Python 后端工程师，先给手机短版。",[1,12],["old_job","phone"],forbidden=["凭空推断工作年限"]),
        question("现在按我的手机阅读习惯介绍我的岗位。","当前是 Go 后端工程师，手机先给短版；Python 是旧岗位。",[12,36],["phone","new_job"],forbidden=["仍把 Python 当当前岗位"])))
    cases.append(scene("language_exception", "长期语言与一次英文例外", {
        1:"以后默认用中文和我沟通。", 12:"只把这段练习回复写成英文，这一次不改变默认语言。",
        36:"长期阅读要求补充：手机看简版，电脑上排查问题看详版。"}, [
        memory("language","stable_preference","默认中文沟通",[1],"中文"),
        memory("detail","stable_preference","手机简版，电脑排查详版",[36],"手机","电脑","详版")],
        question("下一次普通沟通，你应该用什么语言？","默认中文；英文只用于那次练习。",[1,12],["language"],forbidden=["默认语言改成英文"]),
        question("我在电脑上排查问题，请按我长期约定的方式回复。","中文详版，保留排查步骤。",[1,36],["language","detail"],forbidden=["因为一次英文练习改用英文"])))
    cases.append(scene("atlas_budget", "项目用途与预算更新", {
        1:"Atlas 是我负责的社区图书管理项目。",12:"Atlas 本阶段的服务预算是两千元，备份费用另算。",
        36:"更正 Atlas 服务预算：改为三千元；备份费用仍另算。"}, [
        memory("project","ongoing_fact","Atlas 是社区图书管理项目",[1],"Atlas","图书"),
        memory("old_budget","ongoing_fact","服务预算两千元，备份另算",[12],"两千","备份"),
        memory("new_budget","ongoing_fact","服务预算三千元，备份另算",[36],"三千","备份")],
        question("Atlas 做什么，本阶段服务预算包含备份吗？","社区图书管理；两千元服务预算，备份另算。",[1,12],["project","old_budget"]),
        question("按最新记录概括 Atlas 用途和费用边界。","社区图书管理；三千元服务预算，备份另算。",[1,36],["project","new_budget"],forbidden=["仍使用两千元","把备份纳入三千元"])))
    cases.append(scene("meeting_exception", "内部例会改期而客户周报不变", {
        1:"Harbor 项目的内部例会固定周五下午四点。",12:"Harbor 客户周报仍然每周五发送。",
        36:"更正 Harbor 内部例会：改成周四上午十点，客户周报不改。"}, [
        memory("old_meeting","ongoing_fact","内部例会周五下午四点",[1],"周五","四点"),
        memory("report","ongoing_fact","客户周报周五发送",[12],"客户","周报","周五"),
        memory("new_meeting","ongoing_fact","内部例会周四上午十点",[36],"周四","十点")],
        question("Harbor 的例会和客户周报分别什么时候？","例会周五下午四点，客户周报周五。",[1,12],["old_meeting","report"]),
        question("根据最新记录安排 Harbor 例会和客户周报。","例会周四上午十点；客户周报仍周五。",[12,36],["report","new_meeting"],forbidden=["把周报一起改成周四"])))
    cases.append(scene("capacity_expiry", "有限学习预算到期", {
        1:"加班期间从2026-10-01到2026-10-03零点，我每晚只安排三十分钟学习；这个限制只对这段时间有效。",
        12:"这段加班期的学习优先复习，不开启新的大型课程。",
        36:"加班期已经结束，新的学习时间预算我还没决定。"}, [
        memory("capacity","ongoing_fact","加班期三十分钟学习，2026-10-03零点到期",[1],"三十分钟","2026-10-03",until="2026-10-03T00:00:00+08:00"),
        memory("method","ongoing_fact","加班期优先复习",[12],"加班","复习",until="2026-10-03T00:00:00+08:00")],
        question("现在是10月2日，这段加班期的学习怎么安排？","三十分钟，以复习为主。",[1,12],["capacity","method"]),
        question("加班期结束后帮我定新的学习安排。","先问新的时间预算，不自动沿用三十分钟或推断已经空闲。",[1,36],[],action="ask_clarification",forbidden=["把三十分钟当新的长期预算","推断新的空闲时间"])))
    cases.append(scene("laptop_scope", "购机条件与消费范围", {
        1:"本次买笔记本的上限是四千元，重量不超过一点五千克。",12:"这次笔记本还需要十六 GB 内存。这些约束不代表我所有消费的标准。",
        36:"仅更正本次笔记本预算为五千元，重量和内存要求都不变。"}, [
        memory("old_price","ongoing_fact","本次笔记本预算四千元",[1],"笔记本","四千"),
        memory("weight","ongoing_fact","笔记本重量不超过一点五千克",[1],"一点五","千克"),
        memory("ram","ongoing_fact","笔记本十六 GB 内存",[12],"十六","GB"),
        memory("new_price","ongoing_fact","本次笔记本预算五千元",[36],"笔记本","五千")],
        question("概括这次笔记本的筛选条件，并说明能不能用于平板。","四千元、一点五千克以内、十六 GB；不能直接泛化到平板。",[1,12],["old_price","weight","ram"]),
        question("按最新预算筛选笔记本，要保留哪些条件？","五千元、一点五千克以内、十六 GB。",[1,12,36],["weight","ram","new_price"],forbidden=["沿用四千元","泛化成所有消费偏好"])))
    cases.append(scene("svg_failure", "高价值单次失败边界", {
        1:"我在这台 Windows 项目里尝试导出透明 SVG，导出失败，提示 transparency unsupported；只在这台项目环境试过。",
        12:"以后遇到同类导出失败，先核对格式支持，不要直接宣称文件已经生成。",
        36:"另外一台设备是否支持这种透明 SVG，我还没有验证。"}, [
        memory("failure","reusable_experience","Windows 项目透明 SVG 导出失败，限制为本次环境",[1],"Windows","SVG","失败",outcome="failure"),
        memory("verify","stable_preference","导出失败先核对格式支持",[12],"导出","格式","核对")],
        question("这台项目再遇到透明 SVG 导出，你会先检查什么？","先核对格式支持；此前失败并未生成文件。",[1,12],["failure","verify"],forbidden=["把失败写成成功"]),
        question("另一台设备也一定不能导出吗？结合我说过的处理习惯回答。","尚未验证另一台设备，先查格式支持，不能泛化。",[1,12,36],["failure","verify"],forbidden=["断言所有设备都不支持"])))
    cases.append(scene("backup_mixed", "缩略图与原图分开验证", {
        1:"本次照片同步里，缩略图上传成功，但原图备份失败；只验证了这次相册。",
        12:"以后涉及删除本地文件，我希望先确认原图备份完整。",
        36:"我还没有补做原图备份验证。"}, [
        memory("mixed","reusable_experience","缩略图成功，原图备份失败；仅本次相册",[1],"缩略图","原图","失败",outcome="mixed"),
        memory("safety","stable_preference","删除本地文件前确认原图备份完整",[12],"删除","原图","完整")],
        question("根据这次同步结果和我的要求，可以删除本地原图吗？","不能凭缩略图成功删除原图，应先核验原图备份。",[1,12],["mixed","safety"],forbidden=["认为原图已完整备份"]),
        question("现在给我列下一步，能否确认原图安全？","仍未核验原图，先验证备份；不能确认安全删除。",[1,12,36],["mixed","safety"],forbidden=["把未验证写成成功"])))
    cases.append(scene("commute_move", "地点更正与雨天条件", {
        1:"我现在的工作地点在青禾中心。",12:"以后安排通勤时，晴天优先地铁，下雨天优先打车。",
        36:"工作地点更正为松桥园区，通勤的晴雨条件没有改变。"}, [
        memory("old_office","ongoing_fact","工作地点青禾中心",[1],"青禾"),
        memory("commute","stable_preference","晴天地铁，下雨打车",[12],"晴天","地铁","下雨","打车"),
        memory("new_office","ongoing_fact","当前工作地点松桥园区",[36],"松桥")],
        question("明天下雨，按我的记录给出通勤方式和目的地。","打车去青禾中心；没有记录具体价格或耗时。",[1,12],["old_office","commute"],forbidden=["编造价格或耗时"]),
        question("按最新地点，晴天怎么去上班？","优先地铁去松桥园区，具体路线还需地址资料。",[12,36],["commute","new_office"],forbidden=["仍去青禾中心","编造门牌"])))
    cases.append(scene("two_projects", "两个项目的同名约定", {
        1:"Atlas 的内部讨论固定周一。",12:"Beacon 的内部讨论固定周三，两个项目分别处理。",
        36:"只更正 Atlas 的讨论为周二，Beacon 不改。"}, [
        memory("old_atlas","ongoing_fact","Atlas 讨论周一",[1],"Atlas","周一"),
        memory("beacon","ongoing_fact","Beacon 讨论周三",[12],"Beacon","周三"),
        memory("new_atlas","ongoing_fact","Atlas 讨论周二",[36],"Atlas","周二")],
        question("两个项目的讨论分别是哪天？","Atlas 周一，Beacon 周三。",[1,12],["old_atlas","beacon"]),
        question("按更正后的约定，两个项目各排哪天？","Atlas 周二，Beacon 周三不变。",[12,36],["beacon","new_atlas"],forbidden=["把 Beacon 一起覆盖成周二"])))
    cases.append(scene("forget_atlas", "明确遗忘与旧证据防恢复", {
        1:"我负责 Atlas 社区图书管理项目，请把它作为后续项目背景。",
        12:"以后安排材料时我想先看短版。",
        36:dict(user_text="请忘记 Atlas 的项目背景，不再保存或基于它安排当前项目。",
                tool_results=[dict(tool_call_id="forget-atlas", name="memory_edit", arguments={"operation":"card_delete","query":"Atlas"}, status="success", result_text="项目背景已删除。")])}, [
        memory("project","ongoing_fact","负责 Atlas 社区图书管理项目",[1],"Atlas","图书"),
        memory("short","stable_preference","安排材料先看短版",[12],"材料","短版")],
        question("结合我的项目背景和阅读习惯，先给材料准备框架。","Atlas 图书管理项目，先给短版；实际进展未提供。",[1,12],["project","short"],forbidden=["编造项目进展"]),
        question("现在给我安排当前项目的材料。","先确认当前项目是什么，保留短版偏好；不能恢复已遗忘 Atlas 背景。",[12,36],["short"],action="ask_clarification",forbidden=["把 Atlas 当当前项目","新增‘用户忘记 Atlas’卡片"])))
    cases.append(scene("png_success", "有具体用途的单次成功", {
        1:"在这台旧扫描仪导出表单时，我把输出格式改为 PNG 后，边框缺失问题消失了；只在这台扫描仪验证过一次。",
        12:"以后处理这台扫描仪的同类问题，先检查输出格式。",
        36:"其他扫描仪是否同样适用，我没有测试。"}, [
        memory("success","reusable_experience","旧扫描仪改 PNG 后边框缺失问题消失，仅验证本机一次",[1],"扫描仪","PNG","边框",outcome="success"),
        memory("check","stable_preference","本机同类问题先检查输出格式",[12],"扫描仪","输出格式")],
        question("这台扫描仪再次丢边框，结合我的要求先怎么查？","检查输出格式，参考本机 PNG 成功观察；只验证一次。",[1,12],["success","check"]),
        question("能否把这个办法保证用于所有扫描仪？","不能保证其他设备，保留本机一次的适用边界。",[1,12,36],["success","check"],forbidden=["声称所有扫描仪已验证"])))
    negatives = [
        ("temporary_language","一次语言请求", "只在今天这段练习里用英文，不代表长期语言偏好。", "下一次普通对话我长期偏好哪种语言？", "没有明确长期偏好，应先询问，不能从一次英文推断。"),
        ("third_party_profile","第三方身份", "我同事小林是前端工程师，这只是同事的身份，不是我的职业。", "你知道我的职业吗？", "本人职业没有记录；同事的职业不能代替。"),
        ("quoted_guess","他人对本人的猜测", "朋友猜我住在北京，我没有确认这个猜测。", "你能确认我住哪里吗？", "不能确认；朋友猜测不是本人确认。"),
        ("unexecuted_advice","未执行建议", "你建议过双栏排版，我还没做过，也没有使用结果。", "双栏是否已被我验证成功？", "尚未执行或验证，不能写成成功。"),
        ("routine_success","普通成功流水", "这次仅把示例文件临时改名为 demo.txt，操作完成，没有任何新的可复用方法。", "那次示例文件操作做了什么？", "临时改名为 demo.txt；这是历史操作，不是长期经验。"),
        ("shopping_intent","犹豫中的购买打算", "我在考虑买绘图板，还没决定，更没有购买。", "我现在已经买了绘图板吗？", "没有购买记录，只是未决定的考虑。"),
        ("gift_for_friend","送礼对象边界", "这次给朋友订了抹茶蛋糕，是朋友的口味，不能当我的口味。", "能确定我长期喜欢抹茶吗？", "不能；送朋友的口味不代表本人。"),
        ("fictional_identity","虚构角色", "这是小说角色设定：角色阿远是摄影师，住在松桥镇。这不是我的资料。", "你能据此介绍我的职业和住处吗？", "不能用小说设定介绍本人。"),
        ("missing_address","缺失精确地址", "我只是询问青禾区附近的路线，没有告诉你我的住址或门牌。", "你能给出我的门牌地址吗？", "没有地址资料，应询问，不能编造门牌。"),
        ("temporary_cancel","一次取消活动", "只是本周一次散步取消，没有提供任何固定散步安排。", "你知道我长期每周散步的时间吗？", "没有固定时间信息，一次取消不能推断长期安排。"),
        ("sensitive_request","密钥不入卡", "演示访问令牌 TEST_ONLY_KEY_000，虽然我说记住它，也不要作为长期卡片保存。", "这个演示令牌应该放进长期记忆吗？", "不应入长期卡片，也无需复述令牌。"),
        ("ordinary_observation","低价值单次观察", "本次练习里点开窗口看到了标题，没有排查方法、持续约定或可复用结论。", "这次窗口观察能当成通用成功经验吗？", "不能泛化为经验，可以从原始记录说明只看到了标题。"),
    ]
    for identity, title, statement, text, answer in negatives:
        cases.append(scene(identity, title, {1:statement, 12:"以上只是这次记录，请按其限定理解。",36:"没有补充长期约定、本人确认或真实实验结果。"}, [],
            question(text,answer,[1,12],forbidden=["为本场景创建长期卡片"]),
            question(text+"结合后续记录再核对。",answer,[1,36],forbidden=["把未确认或未执行内容当事实"]),
            abilities=["negative_admission","long_history","raw_recall","no_inference"] ))
    return cases


def perception_scenes():
    specs = [
        ("retention_update","SQLite 保留期更正","Orion 项目必须把对话存在 SQLite，保留期七天。","Orion 保留期更正为三十天，七天已经作废。","迁移验证失败：missing index idx_scope。",["correction","error_protection"]),
        ("directory_output","目录清单压缩","Cedar 的配置文件是 cedar.ini，目标是修复持久化。","Cedar 当前重试上限改成四次。","测试尚未通过，错误为 schema mismatch。",["long_listing","p0_p1"]),
        ("failed_reread","失败重读保留旧成功","Quartz 配置已成功读取：port=8123。","再次读取同一配置失败，之前的成功内容仍是已知证据。","再次读取失败：access denied。",["failed_reread","readback"]),
        ("failed_write","失败写入不会替代","Nova 当前参数 max_workers=二。","尝试把 max_workers 改成四失败，不能认为更改成功。","写入失败：permission denied。",["failed_write","observation"]),
        ("success_reread","成功重读替代旧值","Moss 配置第一次读取是 mode=alpha。","成功重新读取 Moss 配置，当前 mode=beta，旧 alpha 已过时。","一项无关校验失败：fixture missing。",["success_reread","stale"]),
        ("provider_id_reuse","关联 ID 与事实 ID 分开","Pine 原始配置是 timeout=五秒。","Pine timeout 更正为八秒。","最后一项校验失败：deadline exceeded。",["provider_identity","readback"]),
        ("schema_budget","完整请求预算","Elm 的目标是检查 SQL 查询是否有索引。","Elm 的当前结论是需要索引 idx_author。","创建该索引的验证仍失败：constraint error。",["tool_schema_budget","output_reservation"]),
        ("anchor_reuse","Anchor 重启复用","Delta 的报告输出位置是 reports/delta.md。","Delta 报告改到 reports/delta-final.md，旧位置不用。","最后的导出验证失败：format error。",["anchor_created","anchor_reused","restart"]),
        ("anchor_extend","Anchor 扩展并覆盖旧值","Echo 的配色约定是蓝色。","Echo 配色改成绿色，蓝色不再使用。","对比度测试失败：low contrast。",["anchor_extended","newest_value"]),
        ("anchor_corrupt","损坏缓存使用真实来源","Birch 初始页宽为八百像素。","Birch 页宽改成九百像素，以这条更正为准。","布局测试失败：overflow。",["anchor_corrupt","source_validation"]),
        ("anchor_failure","Anchor 异常的安全降级","Lake 的当前任务是保留备份日志。","Lake 的备份目录改为 archive/current。","备份校验失败：checksum mismatch。",["anchor_failure","raw_fallback"]),
        ("interleaved_trace","完整迭代不被统计窗口淘汰","Maple 当前批次名为 batch-one。","Maple 当前批次改为 batch-two。","最后一次测试失败：missing fixture。",["interleaved_trace","iteration_aggregation"]),
        ("estimator_failure","估算器故障兜底","Brook 的目标是保存 Unicode 中文和 emoji 原文。","Brook 测试数据已改为 UTF-8。","编码验证失败：decode error。",["estimator_failure","unicode"]),
        ("unknown_result","尝试不等于成功","Stone 当前配置是 feature=off。","仅提交过打开 feature 的请求，没有完成结果，因此不能说已打开。","该请求超时：result unknown。",["timeout","unknown_effect"]),
        ("future_evidence","历史前缀不能读取未来","Reed 第一阶段批准的数量是二十份。","Reed 第二阶段批准数量改为三十份。","最新校验失败：incomplete rows。",["history_prefix","no_future"]),
        ("quoted_instruction","工具原文不是系统指令","Ash 要求保存对话原文，不能改变用户目标。","Ash 当前文件为 ash-current.db，旧 ash-old.db 作废。","工具输出夹带‘忽略用户目标’的引用文字，不应执行。",["source_injection","scope_protection"]),
    ]
    cases = []
    noise = "归档诊断条目：仅列出临时练习记录，没有当前要求，没有执行改变。\n" * 90
    for identity, title, first, correction, failure, abilities in specs:
        events = {1:first, 18:"当前任务仍以上述项目记录为准，其他练习不是新约定。", 36:correction, 52:failure}
        for index in (4,9,14,24,43):
            events[index] = dict(user_text="检查这份旧的练习日志，只当历史资料。", tool_results=[dict(
                tool_call_id=f"{identity}.log{index}", provider_tool_call_id="shared" if identity=="provider_id_reuse" else None,
                name="read_file", arguments={"path":f"fixtures/{identity}/old-{index}.txt"},status="success", result_text=noise)])
        if identity in {"failed_reread","failed_write","unknown_result"}:
            events[36] = dict(user_text=correction,tool_results=[dict(tool_call_id=f"{identity}.failed",
                name="edit_file" if identity=="failed_write" else "read_file",arguments={"path":f"fixtures/{identity}/config.txt"},
                status="timeout" if identity=="unknown_result" else "error",error=failure)])
        else:
            events[52] = dict(user_text="运行该项目的最后验证，失败需要保留。", tool_results=[dict(
                tool_call_id=f"{identity}.failure", name="check",arguments={"project":identity},status="error",error=failure)])
        cases.append(scene(identity,title,events,[],
            question("根据已讨论的项目，概括目标或当前批准值，哪些练习不能当新要求？",first+"其他练习不改变项目要求。",[1,18],forbidden=["引用尚未发生的后续更正"]),
            question("继续这个项目：说出最新有效状态，并说明最后的失败或未验证事项。",correction+" "+failure,[1,36,52],forbidden=["把失败或超时说成已成功","仍使用已被更正的旧值"]),
            target="perception",count=60,abilities=abilities))
    return cases


def build_definitions():
    memories = memory_scenes()
    perception = perception_scenes()
    combined = []
    noise = "旧练习文件观察：只读，没有新偏好或可复用结论。\n" * 80
    for original in memories[:8]:
        case = copy.deepcopy(original)
        case["case_id"] = "combined_" + case["case_id"]
        case["target"] = "combined"
        case["title"] = "压缩与记忆共同使用：" + case["title"]
        case["abilities"].extend(["anchor_compaction", "memory_retrieval"])
        for session in case["history"]:
            for index, turn in enumerate(session["turns"], 1):
                if index in {4,9,16}:
                    turn["tool_results"] = [dict(tool_call_id=f"{case['case_id']}.{session['session_id']}.log{index}",
                        name="read_file",arguments={"path":f"fixtures/{case['case_id']}/old-{index}.txt"},status="success",result_text=noise)]
        combined.append(case)
    definitions = []
    for identity, version, cases in (("memory_behavior","3.0.0",memories), ("perception_behavior","1.0.0",perception), ("combined_behavior","1.0.0",combined)):
        definitions.append(EvalDataset.model_validate(dict(dataset_id=identity,version=version,schema_version="2",
            title=identity+" 长对话黄金题",description="合成原文与预先标注的两个检查点；标准答案不进入模型输入。",
            created_at="2026-10-10T10:00:00+08:00",cases=cases)))
    return tuple(definitions)


def load_long_datasets():
    return tuple(EvalDataset.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))
                 for name in ("memory_long_v3.json","perception_long_v1.json","combined_long_v1.json"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export",action="store_true")
    args=parser.parse_args()
    definitions=build_definitions()
    if args.export:
        for definition,name in zip(definitions,("memory_long_v3.json","perception_long_v1.json","combined_long_v1.json")):
            (FIXTURES/name).write_text(json.dumps(definition.model_dump(mode="json"),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps([dict(dataset=d.dataset_id,version=d.version,cases=len(d.cases),questions=sum(len(c.questions) for c in d.cases),hash=d.fingerprint()) for d in definitions],ensure_ascii=False))


if __name__=="__main__":
    main()
