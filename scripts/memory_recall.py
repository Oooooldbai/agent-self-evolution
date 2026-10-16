#!/usr/bin/env python3
"""
memory_recall.py — recall-log 协议：记录「哪条记忆被召回过」

Auto Dream 三重晋升门槛（召回次数 / 查询多样性 / 来源可信度）的**权威信号源**。
会话日志提及近似只是兜底；本 helper 记录的才是精确召回：
  agent 每次从长期记忆里召回/引用一条主题，就 log 一行。

协议（NDJSON，一行一条，追加写）：
  {"id": "<记忆条目文件名，如 topic-a.md 或主干 topic-a>",
   "query": "<当时触发召回的检索词/问题>",
   "source": "<条目来源路径（可省，省了按 config 路径规则评可信度）>",
   "t": 1792000000000}          # 毫秒时间戳，省略时自动补

用法：
  python3 scripts/memory_recall.py --config config.json log --id topic-a.md --query "怎么做闸门"
  python3 scripts/memory_recall.py --config config.json log --id topic-a.md          # 极简
  python3 scripts/memory_recall.py --config config.json list                         # 汇总看板
  python3 scripts/memory_recall.py --config config.json list --id topic-a.md          # 单条
写入位置：config.json → dream.promotion.recall_log（未配置 = 退出码 2，不静默丢弃）
退出码：0 成功 / 2 配置错误 / 1 写入失败
"""
import argparse
import json
import os
import sys
import time

EXIT_OK, EXIT_FAIL, EXIT_CONF = 0, 1, 2


def recall_log_path(args):
    try:
        cfg = json.load(open(args.config, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"🔴 配置读不了：{e}", file=sys.stderr)
        sys.exit(EXIT_CONF)
    rl = str((cfg.get("dream", {}).get("promotion", {}) or {}).get("recall_log") or "").strip()
    if not rl:
        print("🔴 未配置 dream.promotion.recall_log —— 召回没地方记（不静默丢弃）。"
              "在 config.json 里补一行路径，如 \"~/your-agent/memory/recall-log.ndjson\"。",
              file=sys.stderr)
        sys.exit(EXIT_CONF)
    return os.path.expanduser(rl)


def cmd_log(args):
    path = recall_log_path(args)
    rec = {"id": args.id.strip(), "query": (args.query or "").strip(),
           "source": (args.source or "").strip(), "t": int(time.time() * 1000)}
    if not rec["id"]:
        print("🔴 --id 不能为空（id = 记忆条目文件名或主干）", file=sys.stderr)
        return EXIT_CONF
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"🔴 写入失败：{e}", file=sys.stderr)
        return EXIT_FAIL
    print(f"✅ 已记录召回：{rec['id']}" + (f"（query：{rec['query']}）" if rec["query"] else ""))
    return EXIT_OK


def cmd_list(args):
    path = recall_log_path(args)
    if not os.path.exists(path):
        print(f"（recall-log 还不存在：{path} —— 第一条召回写入后出现）")
        return EXIT_OK
    stats, bad = {}, 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
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
                s = stats.setdefault(_id, {"count": 0, "queries": set(), "last": 0, "source": ""})
                s["count"] += 1
                if rec.get("query"):
                    s["queries"].add(str(rec["query"]))
                s["last"] = max(s["last"], int(rec.get("t") or 0))
                if rec.get("source"):
                    s["source"] = str(rec["source"])
    except OSError as e:
        print(f"🔴 读取失败：{e}", file=sys.stderr)
        return EXIT_FAIL

    if args.id:
        stats = {k: v for k, v in stats.items() if k == args.id or k == args.id + ".md"}
    print(f"recall-log：{path}")
    if bad:
        print(f"⚠️ {bad} 行无法解析（已跳过，不静默）")
    if not stats:
        print("（无召回记录）")
        return EXIT_OK
    print(f"{'条目':<40}{'召回':>5}{'多样性':>7}  最近召回")
    for _id, s in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last"] / 1000.0)) if s["last"] else "—"
        print(f"{_id:<40}{s['count']:>5}{len(s['queries']):>7}  {last}")
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(description="recall-log 协议：记录记忆召回（Auto Dream 三重门槛信号源）")
    ap.add_argument("--config", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log", help="记一次召回")
    lg.add_argument("--id", required=True, help="记忆条目 id（文件名，如 topic-a.md）")
    lg.add_argument("--query", default="", help="当时触发召回的检索词")
    lg.add_argument("--source", default="", help="条目来源路径（可省）")
    ls = sub.add_parser("list", help="汇总看板")
    ls.add_argument("--id", default="", help="只看某条目")
    args = ap.parse_args()
    return cmd_log(args) if args.cmd == "log" else cmd_list(args)


if __name__ == "__main__":
    sys.exit(main())
