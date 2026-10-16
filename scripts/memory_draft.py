#!/usr/bin/env python3
"""
memory_draft.py — L2 记忆写入通道 · 素材切片（A 纯版模式：无模型、无网络）

解决什么：L2 记忆最大的坑是「靠记得写」——那是记日记，已被实证无效
（条款 8 条、事故仍 6 次）。本脚本把「新素材自动出现在记忆库」变机械动作：
日度窗口切片 → `drafts/` → 刷新「待提炼」清单。

**只搬素材，不写记忆**：
  机械层（本脚本）= 切片 + 挂清单；
  语义层（会话内 agent + 老白裁定）= 按四类抽条目 → 写 `topics/` → 在 `MEMORY.md` 登记指针 → 删掉草稿。
  这样「自动提取」有了固定入口，而「写进记忆」仍有人把关（诊断与改写分离的同构）。

输入：每日画像 digest（`~/老白项目/每日画像提炼/digests/*.md`，已脱敏/已过滤本人原话），
     不直接读会话日志——复用已验证管线，避免重复解析与二次脱敏风险。

用法：
  python3 scripts/memory_draft.py --config memory.config.json            # 过窗口（增量）
  python3 scripts/memory_draft.py --config memory.config.json --dry-run  # 只看要切什么
  python3 scripts/memory_draft.py --config memory.config.json --all      # 全量重切（不写状态）
  python3 scripts/memory_draft.py --config memory.config.json --since=2026-10-15
退出码：0 成功 / 2 配置或目录错误 / 1 写入失败
"""
import argparse
import datetime
import glob
import json
import os
import shutil
import sys
import time

EXIT_OK, EXIT_FAIL, EXIT_CONF = 0, 1, 2


def expand(p):
    return os.path.expanduser(str(p))


def load_config(path):
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"🔴 配置读不了：{e}", file=sys.stderr)
        sys.exit(EXIT_CONF)
    cfg.setdefault("digests_dir", "~/digests")
    cfg.setdefault("drafts_dir", "~/drafts")
    cfg.setdefault("state_file", "~/draft.state.json")
    cfg.setdefault("todo_file", "~/待提炼.md")
    cfg.setdefault("stale_days", 7)
    return cfg


def load_state(path):
    try:
        st = json.load(open(path, encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def fmt(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def pick(cfg, args):
    """按窗口挑素材：mtime > 上次切片（增量） / --since / --all。"""
    d = expand(cfg["digests_dir"])
    if not os.path.isdir(d):
        print(f"🔴 digest 目录不存在：{d}（每日画像提炼是否在跑？）", file=sys.stderr)
        sys.exit(EXIT_CONF)
    files = sorted(glob.glob(os.path.join(d, "*.md")))
    if args.all:
        return files, None
    if args.since:
        try:
            since = datetime.datetime.strptime(args.since, "%Y-%m-%d").timestamp()
        except ValueError:
            print("🔴 --since 格式应为 YYYY-MM-DD", file=sys.stderr)
            sys.exit(EXIT_CONF)
        return [f for f in files if os.path.getmtime(f) > since], since
    last = float(load_state(expand(cfg["state_file"])).get("last_run_ms") or 0) / 1000.0
    return [f for f in files if os.path.getmtime(f) > last], last


def write_todo(cfg, drafts_dir, dry):
    """刷新「待提炼」清单：列现存草稿 + 陈旧提醒（放太久=没人提炼，响亮提示）。"""
    stale_days = int(cfg.get("stale_days", 7))
    now = time.time()
    rows, stale = [], []
    for f in sorted(glob.glob(os.path.join(drafts_dir, "*.md"))):
        age = (now - os.path.getmtime(f)) / 86400.0
        rows.append(f"- [ ] {os.path.basename(f)}（切片 {fmt(os.path.getmtime(f))}，已放 {age:.1f} 天）")
        if age >= stale_days:
            stale.append(os.path.basename(f))
    L = ["# 待提炼素材（由 scripts/memory_draft.py 自动维护）", "",
         "> 提炼法：读 `drafts/<文件>` → 按四类（user/feedback/project/reference）抽条目 →",
         "> 写入 `topics/<type>-<名>.md` → 在 `MEMORY.md` 索引登记指针 → 删掉该草稿文件",
         "> （下一轮自动从清单摘掉）。**别把草稿当记忆用**：草稿是素材，不是结论。", ""]
    L += rows or ["（无待提炼素材）"]
    if stale:
        L += ["", f"⚠️ 陈旧（≥{stale_days} 天未提炼，别让它烂在 drafts/）：" + "、".join(stale)]
    if not dry:
        with open(expand(cfg["todo_file"]), "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
    return len(rows), stale


def main():
    ap = argparse.ArgumentParser(description="L2 写入通道：digest → drafts 切片")
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--since", default="")
    args = ap.parse_args()

    cfg = load_config(args.config)
    drafts = expand(cfg["drafts_dir"])
    try:
        os.makedirs(drafts, exist_ok=True)
    except OSError as e:
        print(f"🔴 drafts 目录建不了：{e}", file=sys.stderr)
        return EXIT_CONF

    files, since = pick(cfg, args)
    print(f"窗口：{'全量' if args.all else ('since ' + args.since if args.since else f'mtime > {fmt(since)}' if since else '首次（全部）')}"
          f" · 命中 {len(files)} 份 digest")
    if not files:
        n, stale = write_todo(cfg, drafts, args.dry_run)
        print(f"无新素材（清单仍有 {n} 份待提炼{'，陈旧 ' + str(len(stale)) + ' 份' if stale else ''}）")
        return EXIT_OK

    wrote = []
    for src in files:
        dst = os.path.join(drafts, os.path.basename(src))
        if os.path.exists(dst) and not (args.all or args.since):
            continue
        header = (f"> **素材草稿**（不是记忆）：来源 `{src}` · 切片 {fmt(time.time())}\n"
                  f"> 提炼后请删除本文件；详规见 `MEMORY.md` 头部与 `待提炼.md`。\n\n")
        if args.dry_run:
            wrote.append(os.path.basename(dst))
            continue
        try:
            with open(src, encoding="utf-8") as fh:
                body = fh.read()
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(header + body)
            wrote.append(os.path.basename(dst))
        except OSError as e:
            print(f"🔴 切片失败 {src}：{e}", file=sys.stderr)
            return EXIT_FAIL

    n, stale = write_todo(cfg, drafts, args.dry_run)
    if not args.dry_run and not args.all:
        st = load_state(expand(cfg["state_file"]))
        st.update({"last_run_ms": int(time.time() * 1000), "last_drafted": wrote,
                   "runs": int(st.get("runs", 0)) + 1})
        with open(expand(cfg["state_file"]), "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=2)

    tag = "（dry-run，未落盘）" if args.dry_run else ""
    print(f"{'将切片' if args.dry_run else '已切片'} {len(wrote)} 份{tag}：" + "、".join(wrote))
    print(f"待提炼清单 {n} 份" + (f"，其中陈旧 {len(stale)} 份：{'、'.join(stale)}" if stale else ""))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
