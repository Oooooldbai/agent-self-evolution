#!/usr/bin/env python3
"""
progress_light.py — 红绿灯进度条断言（hook: pre_reply）

来源：卡尔的AI沃茨《开源「红绿灯」提示语》（2026-10-13 老白批准女娲自用）。
解决：Agent 刹不住车 —— 长任务里原始需求做完后自动列一堆优化建议，
拉着人无限往下跑，且永远没有「完成时刻」。

断言内容（长任务回复发出前）：
  1. 回复末行必须是进度条（🟢 / 🟡 / 🔴 之一）
  2. 🟢 必须写明「还差：…」——否则灯亮了却不说还差什么 = 假绿
  3. 🟡 / 🔴 必须已达成原始需求（语义层自查，此处只校验格式完备）

约定：fn(ctx) -> (passed: bool, msg: str)
ctx: {"reply": "<回复全文>", "long_task": true}

⚠️ 铁律：空结果 ≠ 通过。long_task=true 但 reply 缺失/为空 → 一律阻断。
"""

LIGHTS = ("🟢", "🟡", "🔴")


def _last_nonempty_line(text):
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def progress_light(ctx):
    # 短任务/闲聊豁免：明确说明跳过原因，不做静默放行
    if ctx.get("long_task") is not True:
        return True, "long_task 非 True（短问答/闲聊），本断言豁免"

    reply = ctx.get("reply")
    if reply is None or not str(reply).strip():
        return False, "long_task=True 但 reply 缺失或为空 → 阻断（空结果不等于通过）"

    last = _last_nonempty_line(str(reply))
    if not last:
        return False, "回复无有效末行 → 阻断"

    hit = [l for l in LIGHTS if l in last]
    if not hit:
        return False, (
            f"末行无进度条（须含 {'/'.join(LIGHTS)} 之一）→ 阻断。\n"
            f"    实际末行：{last[:120]}"
        )

    light = hit[0]

    # 绿灯必须写明还差什么，否则等于没制动
    if light == "🟢" and "还差" not in last:
        return False, f"🟢 未写明「还差：…」→ 阻断（灯亮了不说还差什么是假绿）。末行：{last[:120]}"

    # 黄/红灯必须给出结论性收口，不能只是一句「已完成」
    if light in ("🟡", "🔴") and len(last) < 8:
        return False, f"{light} 末行过短，未给出可判断的收口信息 → 阻断。末行：{last[:120]}"

    return True, f"进度条合规：{light}（末行长度 {len(last)}）"


GATES = {
    "progress_light": {
        "hook": "pre_reply",
        "fn": progress_light,
        "desc": "长任务回复末行须为红绿灯进度条；🟢 须写明「还差：…」",
        "ctx": '{"reply":"<回复全文>","long_task":true}',
    },
}
