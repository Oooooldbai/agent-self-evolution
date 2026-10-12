#!/usr/bin/env python3
"""
recurrence_scan.py — Agent 自进化 · 复发检测 / 归因 / 纵向追踪

设计依据（Rutgers & UNC Charlotte survey, 19 种技能进化方法 + 10 个基准）：
  1. 诊断与改写分离 —— 先做根因分析再改，跨任务效果显著优于"直接在原始轨迹上让模型自己改"
     （后者会把任务特异性的偶然行为当成可复用模式）
  2. 补纵向追踪盲区 —— 没有任何 benchmark 追踪技能在多轮反馈后是否真正变好；
     本脚本输出事故数的期际变化，用数据回答"装了闸门到底有没有用"
  3. 安全只做一次性检查是错的 —— 自进化可能悄悄剥离安全约束，
     故安全类策略硬编码"不自动改"

用法：
  python3 scripts/recurrence_scan.py --config config.json              # 扫描
  python3 scripts/recurrence_scan.py --config config.json --attr       # + LLM 归因
  python3 scripts/recurrence_scan.py --config config.json --trend      # + 纵向追踪
  python3 scripts/recurrence_scan.py --config config.json --all        # 全部
输出：<out_dir>/recurrence-report-YYYY-MM-DD.md   + 机器可读的同名 .json
"""
import argparse
import datetime
import glob
import json
import os
import re
import shlex
import subprocess
import sys

# ---------- 默认病灶词典（可在 config.json 用 patterns 覆盖/追加） ----------
DEFAULT_PATTERNS = {
    "流程跳读（未读 skill/SOP 就动手）": r"未调用|未通读|跳读|不走流程|没读.{0,10}(skill|SOP)|未执行启动|没读原文",
    "改动前置确认（收意见直接动手）": r"改动前置确认|直接改|直接动手|又直接|直接落盘|未先确认|直接交付",
    "凭据明文回显（安全级）": r"令牌泄露|泄露事故|明文|config\.local|--export",
    "照搬用户原话进产出": r"照搬|原样搬|把.{0,6}意见.{0,4}正文|不照搬|解释.{0,4}搬进文章",
    "交付不自查（数据/一致性）": r"数据遗漏|交叉核对|数据完整性|口径.{0,6}统一|图注.{0,6}不一致|做图前.{0,6}读原文|空白字段",
    "机制空转 / 假绿": r"空转|假绿|存在.{0,6}≠.{0,6}运行|漏读|积压|没落地|确认了.{0,6}没",
    "未实测就下结论": r"未实测|没有权限.{0,10}实际|直接测试|用结果说话|声称.{0,6}权限",
    "归属 / 来源混淆": r"归属.{0,6}(错误|混淆)|混淆.{0,8}(来源|归属)|未经核实.{0,6}声明|归因错误",
    "第三方依赖未审查": r"安全审查|0\s*star|可信度|未审",
}

# 事故词（真出过事）；比宽口径"教训"严格得多，避免把规则文本当事故
INCIDENT = re.compile(
    r"事故|又犯|再次发生|违规|空转|假绿|漏读|积压|泄露|失误|遗漏|怒批|又直接|"
    r"重复|二次|三次|四次|反了|不一致|丢失|失效|崩|错误|失败", re.I)
RULE = re.compile(r"铁律|红线|应当|禁止|必须|不得|规则|清单|原则|固化|规范", re.I)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 安全类病灶：命中则不自动改，必须人工批准（survey 盲区三）
SAFE_CRITICAL = ("凭据", "权限", "删除", "外发", "密钥")


def load_config(path):
    cfg = json.load(open(path, encoding="utf-8"))
    cfg.setdefault("patterns", {})
    cfg.setdefault("out_dir", "./reports")
    cfg.setdefault("policy_dir", "./policy")
    cfg.setdefault("llm_cmd", "")
    cfg.setdefault("notify_cmd", "")
    return cfg


def expand(paths):
    out = []
    for p in paths:
        out += sorted(glob.glob(os.path.expanduser(p))) or [os.path.expanduser(p)]
    return out


def scan(cfg):
    patterns = dict(DEFAULT_PATTERNS)
    patterns.update(cfg.get("patterns", {}))
    hits = {c: {"incident": [], "rule": []} for c in patterns}
    unclassified = []
    for path in expand(cfg.get("sources", [])):
        if not os.path.exists(path):
            continue
        rel = os.path.basename(path)
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("| ---") or set(s) <= set("-| "):
                continue
            cat = next((c for c, p in patterns.items() if re.search(p, s, re.I)), None)
            if cat:
                if INCIDENT.search(s):
                    hits[cat]["incident"].append((rel, i, s))
                elif RULE.search(s):
                    hits[cat]["rule"].append((rel, i, s))
            elif INCIDENT.search(s) and re.search(r"教训|根因|反面案例|agent|女娲", s):
                unclassified.append((rel, i, s))
    return hits, unclassified, patterns


def latest_date(rows):
    ds = [d for _, _, t in rows for d in DATE_RE.findall(t)]
    return max(ds) if ds else "—"


# ---------- 归因：诊断与改写分离，只输出诊断，绝不改文件 ----------
def clean_llm_output(out):
    """清理 LLM 输出污染。

    经验来源（已踩过的坑）：某些 infer CLI 的 stdout 混着 config warnings 面板与元信息，
    直接取全文会得到空或垃圾。定位最后一次 outputs: N，取其后的内容。
    """
    ms = list(re.finditer(r"outputs:\s*(\d+)", out))
    if not ms:
        # 没有 outputs 标记 = 模型根本没返回内容（可能只吐了 config warnings 面板）
        # 必须判为空，否则会把告警面板当成归因正文写进报告
        return ""
    if int(ms[-1].group(1)) == 0:
        return ""
    return out[ms[-1].end():].strip()


def call_llm(cfg, prompt, timeout=90):
    """调用 LLM。llm_cmd 支持 {PROMPT} 占位符（推荐）；否则退化为 stdin 传入。

    固化三条踩坑经验：
      1. stdin 必须 DEVNULL —— 非 TTY 下继承 stdin 会让 CLI 挂起等待输入（实测必现：
         含换行的长 prompt 每次都挂到 timeout；短 prompt 侥幸不死，极易被误判为"模型慢"）
      2. 不要把 OPENCLAW_HOME 带进主进程环境（会导致 Unknown model）
      3. 输出要清洗（见 clean_llm_output）
      3. provider 间歇性失败 → 重试 3 次，全空则明确返回空（由上层标注，不静默）
    """
    tpl = (cfg.get("llm_cmd") or "").strip()
    if not tpl:
        return ""
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_HOME"}
    for _ in range(2):
        try:
            # ⚠️ stdin 必须显式 DEVNULL：非 TTY 下继承的 stdin 会让 CLI 挂起等待输入
            # （实测：同一条命令，短 prompt 恰好不死，含换行的长 prompt 必挂起直到 timeout）
            if "{PROMPT}" in tpl:
                cmd = tpl.replace("{PROMPT}", shlex.quote(prompt))
                r = subprocess.run(cmd, shell=True, capture_output=True,
                                   text=True, timeout=timeout, env=env,
                                   stdin=subprocess.DEVNULL)
            else:
                r = subprocess.run(tpl, shell=True, input=prompt, capture_output=True,
                                   text=True, timeout=timeout, env=env,
                                   stdin=subprocess.DEVNULL)
            txt = clean_llm_output(r.stdout or "")
            if txt:
                return txt
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            continue
    return ""


def attribute(cfg, relapsed, hits):
    """调用配置里的 llm_cmd 做根因分析。缺失或失败则明确降级（不静默失败）。"""
    if not cfg.get("llm_cmd"):
        return {}, "⚠️ 未配置 llm_cmd，跳过归因（显式降级，非静默失败）"
    results, failed = {}, []
    for cat in relapsed:
        ev = "\n".join(f"- {t[:130]}" for _, _, t in hits[cat]["incident"][:5])
        prompt = (
            "你是 Agent 系统的根因分析师。下面是一类反复出现的问题及其证据行。\n"
            "请只做诊断，不要给泛泛建议。输出严格按三行：\n"
            "根因：<一句话，指出机制层面为什么会发生，不要复述现象>\n"
            "建议闸门：<一个具体可执行的拦截手段，必须是脚本/hook/断言级别，"
            "不能是'写进文档'或'记得注意'>\n"
            "置信度：<0-1 之间的数字>\n\n"
            f"问题类别：{cat}\n事故次数：{len(hits[cat]['incident'])}\n"
            f"已有条款数：{len(hits[cat]['rule'])}\n证据：\n{ev}\n"
        )
        out = call_llm(cfg, prompt)
        results[cat] = out or "（LLM 调用失败或返回空，已重试 2 次）"
        if not out:
            failed.append(cat)
    note = "⚠️ 以下类别归因失败（已重试，非静默跳过）：" + "、".join(failed) if failed else None
    return results, note


def trend(cfg, hits, today):
    """纵向追踪：与上一期报告对比事故数变化（补 survey 盲区一）。"""
    files = sorted(glob.glob(os.path.join(cfg["out_dir"], "recurrence-report-*.json")))
    prev = None
    for f in files:
        if os.path.basename(f) == f"recurrence-report-{today}.json":
            continue
        prev = f
    if not prev:
        return None, "无历史报告，本期为基线"
    try:
        old = json.load(open(prev, encoding="utf-8"))["incident_counts"]
    except Exception:
        return None, "历史报告不可解析"
    cur = {c: len(v["incident"]) for c, v in hits.items()}
    rows = []
    for c in sorted(set(list(old) + list(cur))):
        a, b = old.get(c, 0), cur.get(c, 0)
        delta = b - a
        mark = "✅ 下降" if delta < 0 else ("⚠️ 上升" if delta > 0 else "—持平")
        rows.append((c, a, b, delta, mark))
    return (os.path.basename(prev), rows), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--attr", action="store_true", help="LLM 归因（诊断与改写分离）")
    ap.add_argument("--trend", action="store_true", help="纵向追踪（与上期对比）")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--top", type=int, default=3,
                   help="只归因事故最多的 N 类（LLM 串行调用较慢，默认 3）")
    ap.add_argument("--notify", action="store_true", help="有复发时通知")
    args = ap.parse_args()
    if args.all:
        args.attr = args.trend = True

    cfg = load_config(args.config)
    os.makedirs(cfg["out_dir"], exist_ok=True)
    today = datetime.date.today().isoformat()

    hits, unclassified, patterns = scan(cfg)
    ranked = sorted(hits.keys(), key=lambda c: (-len(hits[c]["incident"]), -len(hits[c]["rule"])))
    relapsed = [c for c in ranked if len(hits[c]["incident"]) >= 2]

    L = [f"# 复发检测报告 · {today}", "",
         f"扫描源 {len(expand(cfg.get('sources', [])))} 个",
         "口径：**事故**=真出过事的行；**条款**=只写了规矩的行。", "",
         "## 总览", "",
         "| 状态 | 病灶 | 事故 | 条款 | 最近事故 | 判读 |", "|---|---|---|---|---|---|"]
    for c in ranked:
        inc, rule = hits[c]["incident"], hits[c]["rule"]
        n = len(inc)
        if n >= 2:
            flag, verdict = "🔴", (f"**文档型失效**（条款 {len(rule)} 条 vs 事故 {n} 次）→ 必须闸门化"
                                  if len(rule) >= n else "复发 → 必须闸门化")
        elif n == 1:
            flag, verdict = "⚠️", "单次，观察"
        else:
            flag, verdict = "✅", f"无新事故（条款 {len(rule)} 条）→ 闸门可能已生效"
        L.append(f"| {flag} | {c} | {n} | {len(rule)} | {latest_date(inc)} | {verdict} |")

    attrs, attr_note = ({}, None)
    if args.attr:
        targets = relapsed[:args.top]
        attrs, attr_note = attribute(cfg, targets, hits)
        L += ["", f"## 根因分析（诊断与改写分离 — 只诊断，不改文件）· 归因 top{len(targets)}/{len(relapsed)}", ""]
        if attr_note:
            L.append(attr_note)
        for c, out in attrs.items():
            L += [f"**{c}**", "```", out, "```", ""]
            if any(k in c for k in SAFE_CRITICAL):
                L.append("> 🔒 安全类：**不自动改**，必须人工批准。")

    if args.trend:
        t, note = trend(cfg, hits, today)
        L += ["", "## 纵向追踪（补 survey 盲区：多轮反馈后是否真变好）", ""]
        if note:
            L.append(note)
        else:
            prev, rows = t
            L += [f"对比基线：{prev}", "", "| 病灶 | 上期事故 | 本期事故 | 变化 | 判定 |", "|---|---|---|---|---|"]
            for c, a, b, d, m in rows:
                L.append(f"| {c} | {a} | {b} | {d:+d} | {m} |")

    if unclassified:
        L += ["", "## 🆕 未归类事故信号（可能是新病灶）", ""]
        for rel, i, t in unclassified[:12]:
            L.append(f"- `{rel}:{i}` {t[:130]}")

    L += ["", "## 处置规则", "",
          "1. 事故 ≥2 → **必须闸门化**（脚本/hook/断言），不许只写进文档。",
          "2. 条款数 ≥ 事故数 = 文档型 update 已被证伪。",
          "3. 归因置信度 <0.5 → 不改，只记录（防把噪声当模式）。",
          "4. 安全类策略（凭据/权限/删除/外发）**永不自动改**。",
          "5. 连续 2 期事故为 0 → 标记已闭环，转 COLD。", "",
          "> 判据：固化后靠「我记得去看」保证，还是靠「系统拦一道」保证？答前者 = 记日记。"]

    rep = os.path.join(cfg["out_dir"], f"recurrence-report-{today}.md")
    open(rep, "w", encoding="utf-8").write("\n".join(L))
    js = os.path.join(cfg["out_dir"], f"recurrence-report-{today}.json")
    json.dump({"date": today,
               "incident_counts": {c: len(v["incident"]) for c, v in hits.items()},
               "rule_counts": {c: len(v["rule"]) for c, v in hits.items()},
               "relapsed": relapsed},
              open(js, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\n".join(L[:22]))
    print(f"\n📄 {rep}")
    print(f"🔴 复发 {len(relapsed)} 类")
    if args.notify and relapsed and cfg.get("notify_cmd"):
        msg = f"🔁 复发检测 {today}：{len(relapsed)} 类复发 — " + "、".join(relapsed[:3])
        subprocess.run(cfg["notify_cmd"].replace("{MSG}", msg), shell=True, timeout=40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
