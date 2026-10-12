#!/usr/bin/env python3
"""
gate.py — 通用出口断言框架

为什么有这个：
  归因结果显示，4 类不同病灶给出的建议闸门**结构完全一致**——
  读源 → 比对 → 不一致即 exit 1 → 阻断。差别只在"比对什么"。
  写 4 个独立脚本是重复劳动，故抽成框架：注册断言 → 按 hook 执行 → 任一失败 exit 1。
  以后新增断言的成本接近零。

用法：
  python3 gate.py --list
  python3 gate.py --hook before_deliver --json '{"source":"a.csv","target":"b.csv","pk":"id"}'
  python3 gate.py --hook before_write  --json '{"text":"...","forbidden":["..."]}'
  python3 gate.py --hook before_output --json '{"claims":[{"type":"writable","path":"/tmp"}]}'
  python3 gate.py --hook pre_green     --json '{"raw":25,"parsed":25,"ignored":0}'

退出码：0=全通过；1=有断言失败（阻断）；2=用法/加载错误
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATES_DIR = os.path.join(HERE, "gates")


def load_gates():
    """扫描 gates/ 下所有 .py，收集 GATES 注册表。"""
    gates = {}
    if not os.path.isdir(GATES_DIR):
        return gates
    for fn in sorted(os.listdir(GATES_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(GATES_DIR, fn)
        spec = importlib.util.spec_from_file_location(fn[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"⚠️ 加载 {fn} 失败：{e}")
            continue
        for name, meta in getattr(mod, "GATES", {}).items():
            meta["_src"] = fn
            gates[name] = meta
    return gates


def run_hook(hook, ctx, gates=None, only=None):
    """执行某 hook 下的全部断言。返回 (passed, results)。"""
    gates = gates or load_gates()
    results, ok = [], True
    for name, meta in gates.items():
        if meta.get("hook") != hook:
            continue
        if only and name not in only:
            continue
        try:
            passed, msg = meta["fn"](ctx)
        except Exception as e:
            passed, msg = False, f"断言执行异常：{e}"
        results.append((name, passed, msg, meta.get("desc", "")))
        if not passed:
            ok = False
    return ok, results


def main():
    ap = argparse.ArgumentParser(description="通用出口断言框架")
    ap.add_argument("--hook", help="钩子名，如 before_deliver / before_write")
    ap.add_argument("--json", help="上下文 JSON（或 @文件路径）")
    ap.add_argument("--list", action="store_true", help="列出所有已注册断言")
    ap.add_argument("--only", help="只跑指定断言（逗号分隔）")
    args = ap.parse_args()

    gates = load_gates()
    if args.list or not args.hook:
        print(f"已注册 {len(gates)} 条断言：\n")
        for name, meta in sorted(gates.items(), key=lambda kv: kv[1].get("hook", "")):
            print(f"  [{meta.get('hook')}] {name} — {meta.get('desc','')}")
        print("\n用法：gate.py --hook <hook> --json '<上下文>'")
        return 0

    raw = args.json or "{}"
    if raw.startswith("@"):
        raw = open(raw[1:], encoding="utf-8").read()
    try:
        ctx = json.loads(raw)
    except Exception as e:
        print(f"❌ 上下文 JSON 解析失败：{e}")
        return 2

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    ok, results = run_hook(args.hook, ctx, gates, only)

    if not results:
        print(f"⚠️ hook `{args.hook}` 下没有注册任何断言 —— 这不等于通过（防假绿）")
        return 2

    print(f"## 出口断言 · {args.hook}\n")
    print("| 断言 | 结果 | 说明 |")
    print("|---|---|---|")
    for name, passed, msg, desc in results:
        print(f"| {name} | {'✅' if passed else '❌'} | {msg} |")
    print()
    if ok:
        print(f"✅ 全部通过（{len(results)} 条）→ 放行")
        return 0
    failed = [n for n, p, _, _ in results if not p]
    print(f"❌ 阻断：{len(failed)} 条未通过 → {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
