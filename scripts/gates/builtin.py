#!/usr/bin/env python3
"""
builtin.py — 内置出口断言（由归因结论直接落地）

每条断言对应一次真实归因（见 recurrence-report），不是拍脑袋想的：
  deliver_reconcile    ← 交付不自查（事故 4 次 / 条款 7 条，置信度 0.90）
  green_state_assert   ← 机制空转·假绿（置信度 0.93）
  no_verbatim          ← 照搬用户原话进产出（置信度 0.86）
  verify_memory_fact   ← 未实测就下结论（置信度 0.90）

约定：每条 fn(ctx) -> (passed: bool, msg: str)
"""
import csv
import difflib
import json
import os
import re
import stat as statmod

INSTRUCT_WORDS = r"不要|别|应|建议|解释|说明|意思是|改成|改为|最好|麻烦|帮我|请|一下|这里|这段|那句"


def _load_rows(path):
    """读 CSV / JSON（list of dict）成记录列表。"""
    if path.lower().endswith(".json"):
        return json.load(open(path, encoding="utf-8"))
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------- 1. 交付前对账 ----------------
def deliver_reconcile(ctx):
    src, tgt = ctx.get("source"), ctx.get("target")
    pk = ctx.get("pk")
    if not src or not tgt:
        return False, "缺少 source / target"
    if not os.path.exists(src) or not os.path.exists(tgt):
        return False, f"文件不存在：{src} / {tgt}"
    try:
        srows, trows = _load_rows(src), _load_rows(tgt)
    except Exception as e:
        return False, f"读取失败：{e}"

    msgs = []
    ok = True
    if len(srows) != len(trows):
        ok = False
        msgs.append(f"行数不一致 源{len(srows)} vs 目标{len(trows)}")

    if pk:
        sv = {str(r.get(pk)) for r in srows}
        tv = {str(r.get(pk)) for r in trows}
        miss, extra = sv - tv, tv - sv
        if miss:
            ok = False
            msgs.append(f"目标缺失主键 {len(miss)} 个（如 {sorted(miss)[:3]}）")
        if extra:
            ok = False
            msgs.append(f"目标多出主键 {len(extra)} 个（如 {sorted(extra)[:3]}）")

    fields = ctx.get("fields")
    if fields and len(srows) == len(trows):
        ssum = [tuple(str(r.get(f, "")) for f in fields) for r in srows]
        tsum = [tuple(str(r.get(f, "")) for f in fields) for r in trows]
        if sorted(map(str, ssum)) != sorted(map(str, tsum)):
            ok = False
            msgs.append(f"关键字段 {fields} 内容不一致（checksum 不匹配）")

    return ok, "；".join(msgs) if msgs else f"对账通过：{len(srows)} 行" + (f"，主键 {pk} 一致" if pk else "")


# ---------------- 2. 绿态前防假绿 ----------------
def green_state_assert(ctx):
    raw = ctx.get("raw")
    parsed = ctx.get("parsed")
    ignored = ctx.get("ignored", 0)
    if raw is None or parsed is None:
        return False, "缺少 raw / parsed"
    if raw != parsed + ignored:
        return False, (f"守恒失败：原始 {raw} ≠ 解析 {parsed} + 忽略 {ignored}"
                       f"（差 {raw - parsed - ignored} 条被静默吞掉 → 典型假绿）")
    if raw > 0 and parsed == 0:
        return False, f"原始 {raw} 条但解析 0 条 → 解析器很可能漏读，禁止标绿"
    return True, f"守恒通过：{raw} = {parsed} + {ignored}"


# ---------------- 3. 落盘前禁抄检测 ----------------
def no_verbatim(ctx):
    text = ctx.get("text") or (open(ctx["text_file"], encoding="utf-8").read()
                               if ctx.get("text_file") else "")
    forb = ctx.get("forbidden") or []
    if ctx.get("forbidden_file"):
        forb = [l.strip() for l in open(ctx["forbidden_file"], encoding="utf-8")
                if l.strip() and not l.startswith("#")]
    if not text:
        return False, "缺少 text / text_file"
    if not forb:
        return False, "缺少 forbidden —— 禁抄语料为空，这条断言等于没挂（防假绿）"

    n_chars = ctx.get("n_chars", 8)
    threshold = ctx.get("threshold", 0.8)
    hits = []
    for f in forb:
        core = re.sub(INSTRUCT_WORDS, "", f).strip()
        if len(core) < n_chars:
            continue
        m = difflib.SequenceMatcher(None, core, text).find_longest_match(0, len(core), 0, len(text))
        if m.size >= n_chars:
            hits.append(f"连续 {m.size} 字照搬：「{core[m.a:m.a + m.size]}」")
            continue
        best = 0.0
        step = max(1, len(core) // 4)
        for i in range(0, max(1, len(text) - len(core) + 1), step):
            r = difflib.SequenceMatcher(None, core, text[i:i + len(core)]).ratio()
            best = max(best, r)
        if best >= threshold:
            hits.append(f"相似度 {best:.2f}：「{core[:24]}…」")
    if hits:
        return False, f"命中 {len(hits)} 处：" + "；".join(hits[:3])
    return True, f"无照搬（检查 {len(forb)} 条禁抄语料）"


# ---------------- 4. 输出前实测记忆事实 ----------------
def verify_memory_fact(ctx):
    claims = ctx.get("claims") or []
    if not claims:
        return False, "缺少 claims —— 没有待核实断言，这条等于没挂（防假绿）"
    bad = []
    for c in claims:
        p, typ = c.get("path"), c.get("type", "exists")
        expect = c.get("expect")
        try:
            if typ == "exists":
                got = os.path.exists(p)
            elif typ == "writable":
                got = os.access(p, os.W_OK)
            elif typ == "readable":
                got = os.access(p, os.R_OK)
            elif typ == "mode":
                got = oct(statmod.S_IMODE(os.stat(p).st_mode)).replace("0o", "")
            else:
                bad.append(f"{p}：未知断言类型 {typ}")
                continue
        except Exception as e:
            bad.append(f"{p}：实测异常 {e}")
            continue
        if expect is not None and str(got) != str(expect):
            bad.append(f"{p}：记忆值 {expect} vs 实测 {got}（不一致 → 标记 stale）")
    if bad:
        return False, f"{len(bad)} 条未通过实测：" + "；".join(bad[:3])
    return True, f"{len(claims)} 条断言全部实测通过"


# ---------------- 5. 原文 vs 解析 交叉校验（防漏读型假绿） ----------------
def pending_verbatim_crosscheck(ctx):
    """原始文本里明明有关键词，解析结果却是 0 —— 典型解析器漏读。

    事故原型：feedback 日志里有 25 条「待复盘」字样，解析器只认表格格式，
    自由格式行全被跳过 → 报告显示「待复盘 0」→ 绿得发亮，25 条积压躺了三个月。
    """
    text = ctx.get("log_text") or (open(ctx["log_file"], encoding="utf-8").read()
                                   if ctx.get("log_file") else "")
    kw = ctx.get("keyword", "待复盘")
    parsed = ctx.get("parsed_pending")
    if not text:
        return False, "缺少 log_text / log_file"
    if parsed is None:
        return False, "缺少 parsed_pending"
    n = text.count(kw)
    if n > 0 and parsed == 0:
        return False, f"原文出现「{kw}」{n} 次，但解析出 0 条 → 解析器漏读，报告数据不可信（典型假绿）"
    if n > 0 and parsed > n:
        return False, f"解析出 {parsed} 条 > 原文 {n} 次 → 重复解析，数据可疑"
    return True, f"交叉校验通过：原文「{kw}」{n} 次 vs 解析 {parsed} 条"


GATES = {
    "deliver_reconcile": {
        "hook": "before_deliver",
        "fn": deliver_reconcile,
        "desc": "交付前对账：行数/主键集合/关键字段 checksum 一致才放行",
    },
    "green_state_assert": {
        "hook": "pre_green",
        "fn": green_state_assert,
        "desc": "标绿前守恒断言：原始 == 解析 + 忽略，防止解析器漏读造成假绿",
    },
    "no_verbatim": {
        "hook": "before_write",
        "fn": no_verbatim,
        "desc": "落盘前禁抄检测：连续 8 字相同或相似度 ≥0.8 即阻断",
    },
    "verify_memory_fact": {
        "hook": "before_output",
        "fn": verify_memory_fact,
        "desc": "输出前实测：对记忆中的权限/存在/模式断言真跑 stat/test 比对",
    },
    "pending_verbatim_crosscheck": {
        "hook": "pre_green",
        "fn": pending_verbatim_crosscheck,
        "desc": "原文关键词 vs 解析结果交叉校验：原文有、解析为 0 → 漏读型假绿",
    },
}
