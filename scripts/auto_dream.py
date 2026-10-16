#!/usr/bin/env python3
"""
auto_dream.py — 记忆巩固（Auto Dream / KAIROS · Claude Code L4 全规格）

灵感来源：Claude Code 的 L4 记忆层 Auto Dream——独立后台子代理，在你不用它时运行，
做「大扫除 + 整理 + 提炼」：合并重复记忆、删除无效条目、提炼核心模式、优化索引、
生成跨会话高级洞察。模拟人脑的睡眠记忆巩固。
（规格真源：LSB 对照卡《ClaudeCode五条价值观与记忆系统-对照我们的实现》§L4 挂账规格，
 2026-10-15 老白点火开做）

⚠️ 设计红线：**默认只出报告，绝不自动改/删文件。**
   理由一：诊断与改写分离（survey 结论：先诊断再改，跨任务效果更好）
   理由二：自动删记忆是危险操作，且不可逆
   → 所有结论进报告，由人（或上层 agent）裁定后执行。

L4 规格五要素 → 本脚本对应：
  1. 触发闸（KAIROS 24h + ≥5 新会话）—— 距上次 Dream ≥ min_hours（默认 24）
     且窗口内新会话 ≥ min_sessions（默认 5）才跑；不达标 exit 3（显式未到点，
     绝不静默假装跑过）；--force 手动点火。状态在 <out_dir>/auto-dream.state.json。
  2. 三重晋升门槛（OpenClaw Dreaming 同款）—— 召回次数 / 查询多样性 / 来源可信度，
     三档判定：晋升候选 / 观察 / 沉默。信号双轨：recall-log 协议（权威，
     配套 scripts/memory_recall.py）+ 会话日志提及近似（兜底）；信号缺失显式降级。
  3. 五项动作 —— 合并重复(duplicates) / 删除无效(stale) / 优化索引(index_coverage) /
     提炼核心模式 + 跨会话洞察(insights，--attr 调 LLM)。合并与删除只出建议不执行。
  4. 独立后台运行 —— 由 cron/launchd 每小时调用，闸门自判到点与否（见 README）。
  5. 只出报告 —— 本仓红线，见上。

用法：
  python3 scripts/auto_dream.py --config config.json              # 过闸才跑
  python3 scripts/auto_dream.py --config config.json --attr       # + LLM 跨会话洞察
  python3 scripts/auto_dream.py --config config.json --gate-only  # 只判触发闸，不扫描
  python3 scripts/auto_dream.py --config config.json --force      # 手动点火（绕过触发闸）
输出：<out_dir>/auto-dream-YYYY-MM-DD.md + <out_dir>/auto-dream.state.json
退出码：0 成功出报告 / 3 触发闸未到点 / 2 信号源或配置错误 / 1 其它失败
"""
import argparse
import datetime
import glob
import gzip
import json
import os
import re
import shlex
import subprocess
import sys
import time

# ⚠️ 前置负向断言：路径必须**自成词头**。
# 踩过：`Identity/profile.md` 这种「相对写法嵌在词中间」会被抓成 `/profile.md` → 误报失效。
PATH_RE = re.compile(r"(?<![\w/\-])(/[\w./\-]+\.(?:md|json|py|sh|env))"
                     r"|(?<![\w/\-])(~/[\w./\-]+\.\w+)")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

EXIT_OK, EXIT_FAIL, EXIT_CONF, EXIT_NOTDUE = 0, 1, 2, 3
COMPRESSED_MAX = 10 * 1024 * 1024   # 压缩会话日志整读上限（超出=跳过并说明）
SESSIONS_MAX = 200                  # 单次晋升判定最多读多少个会话文件（防爆）


def load_config(path):
    cfg = json.load(open(path, encoding="utf-8"))
    d = cfg.setdefault("dream", {})
    d.setdefault("memory_index", "~/MEMORY.md")
    d.setdefault("topics_dir", "~/memory/topics")
    d.setdefault("sessions_dir", "~/memory/sessions/daily")
    d.setdefault("out_dir", "./reports")
    d.setdefault("stale_days", 90)

    # ---- 触发闸（KAIROS 24h + ≥5 新会话）----
    t = d.setdefault("trigger", {})
    t.setdefault("enabled", True)
    t.setdefault("min_hours", 24)
    t.setdefault("min_sessions", 5)
    t.setdefault("session_globs", [])   # 会话信号源：文件或目录均可；空 = 拒绝跑（不静默放行）

    # ---- 三重晋升门槛（召回次数 / 查询多样性 / 来源可信度）----
    p = d.setdefault("promotion", {})
    p.setdefault("recall_log", "")      # recall-log 协议（权威信号），空 = 退回会话近似
    if p.get("session_globs") is None:
        p["session_globs"] = []
    p.setdefault("entry_globs", [])     # 空 = 用 topics_dir/*.md 当记忆条目
    th = p.setdefault("thresholds", {})
    th.setdefault("recall_min", 3)
    th.setdefault("diversity_min", 2)
    th.setdefault("credibility_min", 0.6)
    sc = p.setdefault("source_credibility", {})
    sc.setdefault("default", 0.5)
    if not isinstance(sc.get("rules"), list):
        sc["rules"] = []
    p.setdefault("session_tail_bytes", 1_000_000)

    # ---- 跨会话洞察（LLM 通道，可选）----
    ins = d.setdefault("insights", {})
    ins.setdefault("timeout_sec", 300)   # 单次调用超时；headless/agent 类通道冷启动慢
    ins.setdefault("max_files", 7)       # 读最近几份会话归档
    return cfg


def resolve_bases(cfg):
    """路径解析根。

    ⚠️ 踩过的坑：topics 里常写 "/AGENTS.md" 这类**相对工作区根**的路径，
    直接当系统绝对路径查会全部误报为"不存在"（实测 6 处里 3 处是误报）。
    必须多根尝试：工作区根 / memory 根 / 配置里额外指定的根。
    """
    mi = cfg["dream"]["memory_index"]
    paths = mi if isinstance(mi, list) else [mi]
    bases = []
    for p in paths:
        w = os.path.dirname(os.path.expanduser(p))
        bases += [w, os.path.join(w, "memory")]
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


# ===================== 触发闸（KAIROS：24h + ≥5 新会话） =====================
def state_file(d):
    return os.path.join(os.path.expanduser(d["out_dir"]), "auto-dream.state.json")


def load_state(d):
    try:
        st = json.load(open(state_file(d), encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d, st):
    with open(state_file(d), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def fmt_ts(ms):
    if not ms:
        return "从未（首次运行）"
    return datetime.datetime.fromtimestamp(ms / 1000.0 if ms > 1e12 else ms).strftime("%Y-%m-%d %H:%M")


def newest_mtime(p):
    """文件或目录（含内部文件）的最新 mtime。会话是"活"的：被续写=仍在用。"""
    if os.path.isfile(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    newest = 0.0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                continue
    return newest


def gate(d, force=False):
    """KAIROS 触发闸判定。返回 (ok, exit_code, report_lines, meta)。

    宁可拒绝跑，也不静默放行——信号源没配/没匹配到 = 配置错误（exit 2），
    正常但没到点 = exit 3；两种都会把数字打出来，绝不"假装跑过"。
    """
    t = d["trigger"]
    st = load_state(d)
    last_raw = float(st.get("last_dream_ms") or 0)
    # ⚠️ 单位必须统一到秒：state 存毫秒，time.time()/mtime 都是秒。
    # 踩过：直接拿毫秒当秒比 → 间隔算成负数、新会话恒 0（夹具 T3 抓出）。
    last = last_raw / 1000.0 if last_raw > 1e12 else last_raw
    meta = {"last_s": last, "new_sessions": None, "total_sessions": None, "forced": False}

    if not t.get("enabled", True):
        lines = ["⚠️ 触发闸已显式关闭（trigger.enabled=false）——**这不是 L4 规格**，本次无条件运行"]
        return True, EXIT_OK, lines, meta
    if force:
        meta["forced"] = True
        lines = [f"⚠️ 本次为 --force 手动点火（绕过 KAIROS 触发闸）。上次 Dream：{fmt_ts(last)}"]
        return True, EXIT_OK, lines, meta

    globs = t.get("session_globs") or []
    if not globs:
        return False, EXIT_CONF, [
            "🔴 触发闸信号源未配置：trigger.session_globs 为空，无法统计「≥5 个新会话」。",
            "   （信号源配错=结论全错的教训 → 宁可拒绝跑，也不静默放行）",
            "   修 config 后重跑；或确需立即执行用 --force 手动点火。"], meta

    matches = []
    for g in globs:
        matches += glob.glob(os.path.expanduser(str(g)))
    if not matches:
        return False, EXIT_CONF, [
            f"🔴 触发闸信号源一个都没匹配到：{globs}",
            "   路径可能配错（换机/改名/会话目录搬家）。修正后重跑，或 --force 手动点火。"], meta

    total = len(matches)
    new = sum(1 for m in matches if newest_mtime(m) > last)
    meta["total_sessions"], meta["new_sessions"] = total, new

    min_h, min_s = float(t["min_hours"]), int(t["min_sessions"])
    hours = (time.time() - last) / 3600.0 if last else None
    hours_ok = (last == 0) or (hours is not None and hours >= min_h)
    sess_ok = new >= min_s

    head = (f"触发闸（KAIROS ≥{min_h:g}h + ≥{min_s} 新会话）：上次 Dream {fmt_ts(last)}"
            f"（{'首次，间隔视为满足' if hours is None else f'{hours:.1f}h 前'}）"
            f" · 窗口内新会话 {new}/{min_s} · 会话信号总量 {total}")
    if hours_ok and sess_ok:
        return True, EXIT_OK, [head + " · ✅ 放行"], meta

    fails = []
    if not hours_ok:
        fails.append(f"间隔不足（{hours:.1f}h < {min_h:g}h）")
    if not sess_ok:
        fails.append(f"新会话不足（{new} < {min_s}）")
    return False, EXIT_NOTDUE, [head + " · ⛔ 未到点", "未到点原因：" + "；".join(fails)], meta


# ===================== 1. 索引覆盖：有文件但没映射 =====================
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


# ===================== 2. 重复条目：内容高重叠 =====================
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


# ===================== 3. 失效条目 =====================
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


# ===================== 4. 三重晋升门槛（Dreaming：召回/多样性/可信度） =====================
def read_session_text(path, tail_bytes):
    """读一个会话日志成文本。读不了返回 (None, 原因)——显式降级，不静默。"""
    try:
        size = os.path.getsize(path)
        ext = path.lower()
        compressed = ext.endswith((".zstd", ".zst", ".gz"))
        if compressed:
            if size > COMPRESSED_MAX:
                return None, "压缩日志超 10MB 读取上限"
            with open(path, "rb") as f:
                raw = f.read()
            if ext.endswith(".gz"):
                return gzip.decompress(raw).decode("utf-8", "replace"), None
            r = subprocess.run(["zstd", "-dc"], input=raw, capture_output=True, timeout=30)
            if r.returncode != 0:
                return None, "zstd 解压失败"
            return r.stdout.decode("utf-8", "replace"), None
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)   # 平文本只读尾部（近期召回更重要），省 IO
            raw = f.read()
        return raw.decode("utf-8", "replace"), None
    except FileNotFoundError:
        return None, "zstd 不在 PATH（无法解压压缩会话）"
    except subprocess.TimeoutExpired:
        return None, "zstd 解压超时"
    except OSError as e:
        return None, f"读取失败（{e.__class__.__name__}）"
    except Exception as e:                    # gzip.BadGzipFile 等
        return None, f"解码失败（{e.__class__.__name__}）"


def _expand_session_files(globs, cap=SESSIONS_MAX):
    """会话信号 → 待读文件清单（目录展开到内部文件）。"""
    out, seen = [], set()
    for g in globs:
        for m in sorted(glob.glob(os.path.expanduser(str(g)))):
            paths = []
            if os.path.isdir(m):
                for root, _d, files in os.walk(m):
                    paths += [os.path.join(root, x) for x in files]
            elif os.path.isfile(m):
                paths = [m]
            for p in sorted(paths):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
    if len(out) > cap:
        out = out[-cap:]          # 只取最近的 cap 个（按路径序近似，够用且可复现）
    return out


def load_recall_log(path):
    """recall-log 协议（NDJSON：{"id","query","source","t"}）→ 每 id 的精确统计。"""
    stats, bad = {}, 0
    for line in read(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            _id = str(rec.get("id", "")).strip()
            if not _id:
                bad += 1
                continue
        except ValueError:
            bad += 1
            continue
        s = stats.setdefault(_id, {"count": 0, "queries": set(), "source": ""})
        s["count"] += 1
        q = str(rec.get("query", "")).strip()
        if q:
            s["queries"].add(q)
        if rec.get("source"):
            s["source"] = str(rec["source"])
    return stats, bad


def promotion(cfg, topics):
    """三重门槛判定。返回 (rows, notes, thresholds)。

    rows: (条目, 召回, 多样性, 可信度, 信号源, 判定)
    信号优先级：recall-log（权威）> 会话日志提及近似（兜底）；两者皆无 → ⚪ 无信号，不判定。
    """
    d = cfg["dream"]
    p = d["promotion"]
    th = p["thresholds"]
    notes, stats_log = [], {}

    # 条目集合
    entries = []
    eg = p.get("entry_globs") or []
    if eg:
        for g in eg:
            entries += [f for f in sorted(glob.glob(os.path.expanduser(str(g)))) if os.path.isfile(f)]
    else:
        entries = list_topics(d["topics_dir"])
    entries = sorted(set(entries))

    # 来源可信度（条目路径匹配规则表，先匹配先生效；recall-log 的 source 可覆盖）
    rules = p["source_credibility"].get("rules") or []
    default_cred = float(p["source_credibility"].get("default", 0.5))

    def cred_of(path, override=""):
        s = override or os.path.expanduser(path)
        for r in rules:
            m = str(r.get("match", ""))
            if m and m in s:
                try:
                    return round(float(r.get("score", default_cred)), 2)
                except (TypeError, ValueError):
                    continue
        return round(default_cred, 2)

    # 信号源 A：recall-log
    rl = str(p.get("recall_log") or "").strip()
    if rl:
        rlp = os.path.expanduser(rl)
        if os.path.exists(rlp):
            stats_log, bad = load_recall_log(rlp)
            if bad:
                notes.append(f"recall-log 有 {bad} 行无法解析（已跳过，不静默）")
            notes.append(f"recall-log：{sum(s['count'] for s in stats_log.values())} 条召回记录，"
                         f"覆盖 {len(stats_log)} 个条目（权威信号）")
        else:
            notes.append(f"⚠️ recall_log 配置了但文件不存在（{rlp}）→ 退回会话日志近似")

    # 信号源 B：会话日志提及近似（提及次数=召回，不同会话数=查询多样性）
    globs = p.get("session_globs") or d["trigger"].get("session_globs") or []
    sess_hit, sess_div, unreadable, reasons = {}, {}, 0, {}
    sess_files = _expand_session_files(globs) if globs else []
    stems = [os.path.basename(f)[:-3] for f in entries]
    for fp in sess_files:
        txt, why = read_session_text(fp, int(p.get("session_tail_bytes", 1_000_000)))
        if txt is None:
            unreadable += 1
            reasons[why] = reasons.get(why, 0) + 1
            continue
        hit_stems = [s for s in stems if s in txt]
        for s in hit_stems:
            sess_hit[s] = sess_hit.get(s, 0) + txt.count(s)
            sess_div[s] = sess_div.get(s, 0) + 1
    if globs and not sess_files:
        notes.append(f"⚠️ 会话信号 glob 未匹配到任何文件：{globs}（可能配错，显式标注）")
    if unreadable:
        detail = "；".join(f"{k}×{v}" for k, v in reasons.items())
        notes.append(f"⚠️ {unreadable}/{len(sess_files)} 个会话文件无法读取（{detail}）"
                     f"→ 召回统计只基于可读部分（显式降级）")
    if sess_files and not unreadable:
        notes.append(f"会话日志近似：{len(sess_files)} 个会话文件全部可读"
                     f"（提及次数=召回 · 不同会话数=查询多样性）")
    if sess_files:
        notes.append("⚠️ 会话近似含**本次会话自身**（自指偏差：本任务反复提到的条目会虚高）"
                     "——权威信号请接 recall-log，单会话命中不要当晋升依据")

    # 逐条目合成指标
    rows = []
    for f in entries:
        stem = os.path.basename(f)[:-3]
        lg = stats_log.get(stem) or stats_log.get(os.path.basename(f))
        if lg and lg["count"]:
            recalled = lg["count"]
            diversity = len(lg["queries"]) or (1 if lg["count"] else 0)
            src, cred = "recall-log", cred_of(f, lg.get("source", ""))
            has_signal = True
        else:
            recalled = sess_hit.get(stem, 0)
            diversity = sess_div.get(stem, 0)
            src = "会话近似" if recalled else "无信号"
            cred = cred_of(f)
            has_signal = bool(recalled)

        checks = [recalled >= int(th["recall_min"]),
                  diversity >= int(th["diversity_min"]),
                  cred >= float(th["credibility_min"])]
        if not has_signal:
            verdict = "⚪ 无信号（不判定）"
        elif all(checks):
            verdict = "🟢 晋升候选"
        elif sum(checks) >= 2:
            verdict = "🟡 观察"
        else:
            verdict = "⚪ 沉默（降级/转 COLD 候选）"
        rows.append((os.path.basename(f), recalled, diversity, cred, src, verdict))

    order = {"🟢 晋升候选": 0, "🟡 观察": 1,
             "⚪ 沉默（降级/转 COLD 候选）": 2, "⚪ 无信号（不判定）": 3}
    rows.sort(key=lambda r: (order.get(r[5], 9), -r[1], r[0]))
    if entries and not any(r[4] != "无信号" for r in rows):
        notes.append("⚠️ 三重门槛整体无召回信号（无 recall_log 且会话日志不可读/为空）"
                     "→ 本节不判定，只列来源可信度。装 recall-log（scripts/memory_recall.py）后生效"
                     "（显式降级，非静默）")
    return rows, notes, th


# ===================== 5. 跨会话洞察（LLM，可选） =====================
def insights(cfg, sessions_dir, llm_cmd):
    """跨会话洞察（LLM 通道，可选）。

    **通道中立**：优先认 OpenClaw 的 `outputs: N` 标记（取其后的正文）；没有标记就直接用 stdout
    ——凡"单次问答、答案走 stdout、诊断走 stderr"的 CLI 都能接（如 `dsh --profile headless {PROMPT}`）。
    非零退出码 / 超时 / 空输出一律**显式报错**，不把失败静默当成"没洞察"。
    """
    d = cfg["dream"]
    ins = d.get("insights", {}) or {}
    timeout = int(ins.get("timeout_sec", 300))
    if not llm_cmd:
        return "⚠️ 未配置 llm_cmd，跳过洞察（显式降级）"
    files = sorted(glob.glob(os.path.join(os.path.expanduser(sessions_dir), "*.md")))[-int(ins.get("max_files", 7)):]
    if not files:
        return "无会话归档，跳过"
    corpus = "\n".join(read(f)[-1500:] for f in files)
    prompt = (
        "下面是这个 agent 最近若干天的会话归档摘要。请做「跨会话洞察与核心模式提炼」——\n"
        "不要复述单日流水，只提炼**跨越多天反复出现**的模式。输出三段：\n"
        "重复模式：<跨会话反复出现的做事方式/偏好/坑，正负向都要，1-3条>\n"
        "新增动向：<最近出现的、以前没有的关注点，1-2条>\n"
        "建议写入记忆：<一句话，说明该把哪条洞察固化到长期记忆、放哪个主题>\n"
        "⚠️ 这是一次纯文本提炼：**不要调用任何工具、不要读写任何文件**，直接输出上述三段。\n\n"
        f"会话归档：\n{corpus[:6000]}\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_HOME"}
    cmd = (llm_cmd.replace("{PROMPT}", shlex.quote(prompt)) if "{PROMPT}" in llm_cmd else llm_cmd)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return (f"⚠️ 洞察调用超时（>{timeout}s，可在 config `dream.insights.timeout_sec` 调大）"
                f"——非静默失败")
    except Exception as e:
        return f"⚠️ 调用失败：{e}"

    out = (r.stdout or "").strip()
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        tail = err[-1][:300] if err else "无 stderr"
        return (f"⚠️ 洞察调用失败（exit {r.returncode}）：{tail}"
                + (f"\n\nstdout 片段：{out[:200]}" if out else ""))
    ms = list(re.finditer(r"outputs:\s*(\d+)", out))
    if ms:                                     # OpenClaw 格式：取最后一个标记之后的正文
        if int(ms[-1].group(1)) == 0:
            return "⚠️ 通道返回 outputs: 0（无输出）——非静默失败"
        body = out[ms[-1].end():].strip()
        return body or "⚠️ 通道返回空正文（outputs 标记后无内容）"
    if not out:                                # 通用通道：stdout 即正文
        return "⚠️ 通道返回空输出——跳过本次（非静默跳过）"
    return out


def main():
    ap = argparse.ArgumentParser(description="Auto Dream · 记忆巩固（L4 规格）")
    ap.add_argument("--config", required=True)
    ap.add_argument("--attr", action="store_true", help="含 LLM 跨会话洞察")
    ap.add_argument("--gate-only", action="store_true",
                    help="只判触发闸：0=到点 3=未到点 2=信号源错误")
    ap.add_argument("--force", action="store_true", help="手动点火：绕过 KAIROS 触发闸")
    args = ap.parse_args()

    cfg = load_config(args.config)
    d = cfg["dream"]
    os.makedirs(os.path.expanduser(d["out_dir"]), exist_ok=True)

    # ---- 触发闸先行 ----
    ok, code, glines, meta = gate(d, force=args.force)
    if args.gate_only:
        print("\n".join(glines))
        print(f"→ {'到点，可以跑' if ok else '未到点/信号源错误'}（exit {code}）")
        return code if not ok else EXIT_OK
    if not ok:
        print("\n".join(glines))
        print(f"→ Auto Dream 未执行（exit {code}；--force 可手动点火）")
        return code

    today = datetime.date.today().isoformat()
    topics = list_topics(d["topics_dir"])
    mi = d["memory_index"]
    paths = mi if isinstance(mi, list) else [mi]
    # ⚠️ 索引往往不止一份（MEMORY.md 速览 + memory/index.md 检索链）。
    # 只查一份 → 会把「另一份里有映射」的文件误报成未映射（实测踩过，假阳性）。
    index_text = "\n".join(read(p) for p in paths)

    miss = index_coverage(topics, index_text)
    dup = duplicates(topics)
    bases = resolve_bases(cfg)
    st = stale(topics, index_text, d.get("stale_days", 90), bases)
    prows, pnotes, pth = promotion(cfg, topics)

    L = [f"# Auto Dream · 记忆巩固报告 · {today}", "",
         f"扫描主题文件 {len(topics)} 个 · 索引源 {len(paths)} 份（{'；'.join(paths)}）", ""]
    L += [f"> {'⚠️ ' if meta.get('forced') else ''}**触发闸**：{'；'.join(glines)}", "",
          "> ⚠️ 本报告**只出建议，不自动改文件**。所有条目需人工裁定后执行。", ""]

    L += ["## 1. 索引覆盖（漏了 = 文件存在但搜不到）", ""]
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

    L += ["", "## 4. 三重晋升门槛（Dreaming：召回次数 / 查询多样性 / 来源可信度）", "",
          f"门槛：召回 ≥{pth['recall_min']} · 多样性 ≥{pth['diversity_min']} · "
          f"来源可信度 ≥{pth['credibility_min']}（config `promotion.thresholds` 可调）", ""]
    for n in pnotes:
        L.append(f"- {n}")
    L.append("")
    if prows:
        L += ["| 条目 | 召回 | 多样性 | 来源可信 | 信号源 | 判定 |", "|---|---|---|---|---|---|"]
        for name, rc, dv, cr, src, vd in prows[:20]:
            L.append(f"| {name} | {rc} | {dv} | {cr} | {src} | {vd} |")
        tier = {}
        for r in prows:
            tier[r[5]] = tier.get(r[5], 0) + 1
        L += ["", "汇总：" + " · ".join(f"{k} {v}" for k, v in tier.items())]
    else:
        L.append("（无记忆条目，跳过）")

    if args.attr:
        L += ["", "## 5. 跨会话洞察与核心模式（LLM）", "",
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
          "6. 重复条目合并时保留更**新**的那份，旧份标记转 COLD 而非硬删。", "",
          "> L4 规格对照（LSB 对照卡 §L4）：KAIROS 触发闸 24h + ≥5 新会话（本脚本强制执行）·",
          "> 三重晋升门槛（召回/多样性/可信度）· 五项动作（合并/删无效/提炼模式/优化索引/跨会话洞察）·",
          "> 后台由 cron/launchd 每小时探测、闸门自判到点 · 只出报告不自动改。"]

    rep = os.path.join(os.path.expanduser(d["out_dir"]), f"auto-dream-{today}.md")
    open(rep, "w", encoding="utf-8").write("\n".join(L))

    # ---- 成功出报告后才推进触发闸水位（没跑成不推进，防假绿）----
    st_new = dict(load_state(d))
    st_new.update({
        "last_dream_ms": int(time.time() * 1000),
        "runs": int(st_new.get("runs", 0)) + 1,
        "last_forced": bool(meta.get("forced")),
        "last_new_sessions": meta.get("new_sessions"),
        "last_report": rep,
    })
    save_state(d, st_new)

    print("\n".join(L[:30]))
    print(f"\n📄 {rep}")
    print(f"未映射 {len(miss)} · 重复 {len(dup)} · 失效 {len(st)} · 晋升候选 "
          f"{sum(1 for r in prows if r[5].startswith('🟢'))}")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except json.JSONDecodeError as e:
        print(f"🔴 配置解析失败：{e}", file=sys.stderr)
        sys.exit(EXIT_CONF)
    except FileNotFoundError as e:
        print(f"🔴 文件不存在：{e}", file=sys.stderr)
        sys.exit(EXIT_CONF)
