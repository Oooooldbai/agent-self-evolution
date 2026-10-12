#!/usr/bin/env python3
"""
auto_dream.py — 记忆巩固（Auto Dream / KAIROS）

灵感来源：Claude Code 的 L4 记忆层 Auto Dream——独立后台子代理，在你不用它时运行，
做「大扫除 + 整理 + 提炼」：合并重复记忆、删除无效条目、提炼核心模式、优化索引、
生成跨会话高级洞察。模拟人脑的睡眠记忆巩固。

⚠️ 设计红线：**默认只出报告，绝不自动改/删文件。**
   理由一：诊断与改写分离（survey 结论：先诊断再改，跨任务效果更好）
   理由二：自动删记忆是危险操作，且不可逆
   → 所有结论进报告，由人（或上层 agent）裁定后执行。

四项检查：
  1. index_coverage —— topics 里有文件但索引里没映射（最容易漏，漏了=搜不到）
  2. duplicates     —— 内容高度重叠的条目，建议合并
  3. stale          —— 引用路径已不存在 / 长期未更新，建议标删
  4. insights       —— 从近期会话提炼跨会话模式（需 --attr 调 LLM）

用法：
  python3 scripts/auto_dream.py --config config.json
  python3 scripts/auto_dream.py --config config.json --attr     # 含 LLM 洞察
输出：<out_dir>/auto-dream-YYYY-MM-DD.md
"""
import argparse
import datetime
import difflib
import glob
import json
import os
import re
import subprocess
import shlex
import sys

PATH_RE = re.compile(r"(/[\w./\-]+\.(?:md|json|py|sh|env))|(~/[\w./\-]+\.\w+)")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_config(path):
    cfg = json.load(open(path, encoding="utf-8"))
    d = cfg.setdefault("dream", {})
    d.setdefault("memory_index", "~/MEMORY.md")
    d.setdefault("topics_dir", "~/memory/topics")
    d.setdefault("sessions_dir", "~/memory/sessions/daily")
    d.setdefault("out_dir", "./reports")
    d.setdefault("stale_days", 90)
    return cfg


def resolve_bases(cfg):
    """路径解析根。

    ⚠️ 踩过的坑：topics 里常写 "/AGENTS.md" 这类**相对工作区根**的路径，
    直接当系统绝对路径查会全部误报为"不存在"（实测 6 处里 3 处是误报）。
    必须多根尝试：工作区根 / memory 根 / 配置里额外指定的根。
    """
    idx = os.path.expanduser(cfg["dream"]["memory_index"])
    ws = os.path.dirname(idx)
    bases = [ws, os.path.join(ws, "memory")]
    bases += [os.path.expanduser(r) for r in cfg["dream"].get("extra_roots", [])]
    return [b for b in bases if b]


def path_exists(p, bases):
    if os.path.exists(os.path.expanduser(p)):
        return True
    for b in bases:
        if os.path.exists(os.path.join(b, p.lstrip("/"))):
            return True
    return False


def read(p):
    p = os.path.expanduser(p)
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return ""


def list_topics(d):
    d = os.path.expanduser(d)
    return sorted(glob.glob(os.path.join(d, "*.md"))) if os.path.isdir(d) else []


# ---------- 1. 索引覆盖：有文件但没映射 ----------
def index_coverage(topics, index_text):
    missing = []
    for f in topics:
        name = os.path.basename(f)
        stem = name[:-3]
        # 索引里提到文件名或文件名主干即算覆盖
        if name in index_text or stem in index_text:
            continue
        missing.append((name, f))
    return missing


# ---------- 2. 重复条目：内容高重叠 ----------
def keywords(text, top=40):
    """极简关键词提取：去停用词后的高频中文片段/英文词。"""
    stop = set("的 了 是 在 和 与 及 对 于 把 被 有 这 那 之 以 为 中 上 下 不 也 都 就 而 或 其 # | - • >".split())
    toks = re.findall(r"[\u4e00-\u9fa5]{2,6}|[A-Za-z][\w\-]{2,}", text)
    freq = {}
    for t in toks:
        if t in stop:
            continue
        freq[t] = freq.get(t, 0) + 1
    return {t for t, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:top]}


def duplicates(topics, threshold=0.45):
    docs = {}
    for f in topics:
        docs[f] = keywords(read(f))
    out = []
    fs = list(docs)
    for i in range(len(fs)):
        for j in range(i + 1, len(fs)):
            a, b = docs[fs[i]], docs[fs[j]]
            if not a or not b:
                continue
            inter = len(a & b)
            union = len(a | b)
            if union and inter / union >= threshold:
                out.append((os.path.basename(fs[i]), os.path.basename(fs[j]),
                            round(inter / union, 2)))
    return sorted(out, key=lambda x: -x[2])


# ---------- 3. 失效条目 ----------
def stale(topics, index_text, stale_days, bases):
    now = datetime.date.today()
    out = []
    for f in topics:
        txt = read(f)
        reasons = []
        # 3a 引用路径不存在（多根解析，避免把"相对工作区根"误判为失效）
        paths = [p for p in PATH_RE.findall(txt) for p in (p if isinstance(p, tuple) else (p,)) if p]
        dead = []
        for p in set(paths):
            if p.startswith("/") and not path_exists(p, bases):
                dead.append(p)
        if dead:
            reasons.append(f"引用路径已不存在 {len(dead)} 处（如 {dead[0]}）")
        # 3b 长期未更新
        ds = [datetime.date.fromisoformat(d) for d in DATE_RE.findall(txt)]
        if ds:
            age = (now - max(ds)).days
            if age >= stale_days:
                reasons.append(f"最新标注日期距今 {age} 天（≥{stale_days}）")
        # 3c 索引里没提（孤文件）
        if os.path.basename(f)[:-3] not in index_text:
            reasons.append("索引无映射（等于搜不到）")
        if reasons:
            out.append((os.path.basename(f), reasons))
    return out


# ---------- 4. 跨会话洞察（LLM，可选） ----------
def insights(cfg, sessions_dir, llm_cmd):
    if not llm_cmd:
        return "⚠️ 未配置 llm_cmd，跳过洞察（显式降级）"
    files = sorted(glob.glob(os.path.join(os.path.expanduser(sessions_dir), "*.md")))[-7:]
    if not files:
        return "无会话归档，跳过"
    corpus = "\n".join(read(f)[-1500:] for f in files)
    prompt = (
        "下面是这个 agent 最近若干天的会话归档摘要。请做「跨会话洞察」——\n"
        "不要复述单日流水，只提炼**跨越多天反复出现**的模式。输出三段：\n"
        "重复模式：<跨会话反复出现的做事方式/偏好/坑，1-3条>\n"
        "新增动向：<最近出现的、以前没有的关注点，1-2条>\n"
        "建议写入记忆：<一句话，说明该把哪条洞察固化到长期记忆、放哪个主题>\n\n"
        f"会话归档：\n{corpus[:6000]}\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_HOME"}
    cmd = (llm_cmd.replace("{PROMPT}", shlex.quote(prompt)) if "{PROMPT}" in llm_cmd else llm_cmd)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=150, env=env, stdin=subprocess.DEVNULL)
        out = (r.stdout or "").strip()
    except Exception as e:
        return f"调用失败：{e}"
    ms = list(re.finditer(r"outputs:\s*(\d+)", out))
    if not ms or int(ms[-1].group(1)) == 0:
        return "（LLM 返回空，已跳过；非静默失败）"
    return out[ms[-1].end():].strip()


def main():
    ap = argparse.ArgumentParser(description="Auto Dream · 记忆巩固")
    ap.add_argument("--config", required=True)
    ap.add_argument("--attr", action="store_true", help="含 LLM 跨会话洞察")
    args = ap.parse_args()

    cfg = load_config(args.config)
    d = cfg["dream"]
    os.makedirs(os.path.expanduser(d["out_dir"]), exist_ok=True)
    today = datetime.date.today().isoformat()

    topics = list_topics(d["topics_dir"])
    index_text = read(d["memory_index"])

    miss = index_coverage(topics, index_text)
    dup = duplicates(topics)
    bases = resolve_bases(cfg)
    st = stale(topics, index_text, d.get("stale_days", 90), bases)

    L = [f"# Auto Dream · 记忆巩固报告 · {today}", "",
         f"扫描主题文件 {len(topics)} 个 · 索引 `{os.path.basename(d['memory_index'])}`", "",
         "> ⚠️ 本报告**只出建议，不自动改文件**。所有条目需人工裁定后执行。", "",
         "## 1. 索引覆盖（漏了 = 文件存在但搜不到）", ""]
    if miss:
        L += ["| 未映射文件 | 路径 |", "|---|---|"]
        for name, p in miss:
            L.append(f"| {name} | `{p}` |")
    else:
        L.append("✅ 全部主题文件都有索引映射")

    L += ["", "## 2. 重复条目（建议合并）", ""]
    if dup:
        L += ["| A | B | 关键词重叠 |", "|---|---|---|"]
        for a, b, s in dup[:12]:
            L.append(f"| {a} | {b} | {s} |")
    else:
        L.append("✅ 未发现明显重复")

    L += ["", "## 3. 失效 / 可疑条目（建议标删或更新）", ""]
    if st:
        L += ["| 文件 | 原因 |", "|---|---|"]
        for name, reasons in st[:12]:
            L.append(f"| {name} | {'；'.join(reasons)} |")
    else:
        L.append("✅ 未发现失效条目")

    if args.attr:
        L += ["", "## 4. 跨会话洞察（LLM）", "",
              insights(cfg, d["sessions_dir"], cfg.get("llm_cmd", ""))]

    L += ["", "---", "",
          "## 处置纪律", "",
          "1. **默认不改**——报告是建议，执行前由人裁定。",
          "2. 合并/删除属**不可逆**操作，必须先备份（git 已提交即可回滚）。",
          "3. 「索引无映射」优先级最高：文件存在但搜不到 = 白存。",
          "4. ⚠️ **「引用路径不存在」未必是真失效**：记忆里常写相对路径（如 `/AGENTS.md`、",
          "   `/Identity/profile.md`），脚本已做多根解析但仍可能误报。",
          "   **核实前不要删**——先确认它到底指哪。",
          "5. 相对路径本身是问题：新写记忆尽量写全路径。",
          "4. 重复条目合并时保留更**新**的那份，旧份标记转 COLD 而非硬删。", "",
          "> 对照 Claude Code L4 Auto Dream：24h + ≥5 新会话触发，"
          "合并重复 / 删无效 / 提炼模式 / 优化索引 / 生成洞察。本脚本覆盖后四项，"
          "触发由 cron 承担。"]

    rep = os.path.join(os.path.expanduser(d["out_dir"]), f"auto-dream-{today}.md")
    open(rep, "w", encoding="utf-8").write("\n".join(L))
    print("\n".join(L[:30]))
    print(f"\n📄 {rep}")
    print(f"未映射 {len(miss)} · 重复 {len(dup)} · 失效 {len(st)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
