"""给 CLI 加 iterate 子命令：按工作单迭代，每轮只求"比上一轮更好"。"""
import ast
from pathlib import Path


def add_cmd(args) -> None:
    from core import iterate

    def on_event(kind, payload):
        if kind == "round":
            print(f"\n=== 第 {payload['round']} 轮｜已验收 {payload['verified']}/{payload['total']} 函数 ===")
        elif kind == "architect":
            print(f"[架构师] 根因：{payload.get('root_cause') or '（未给）'}")
            for o in payload.get("orders") or []:
                print(f"  单 {o}")
        elif kind == "order_done":
            print(f"  ✓ {payload['order']}  应用：{payload.get('applied')}")
        elif kind == "order_failed":
            print(f"  ✗ {payload['order']}  原因：{(payload.get('reason') or '')[:90]}")

    res = iterate.iterate(Path(args.repo), Path(args.requirement_file).read_text(encoding="utf-8-sig"),
                          args.model, rounds=args.rounds, use_remote=args.remote, emit=on_event)
    print("\n=== 结果 ===")
    print(f"  验收 {res.get('verified')}/{res.get('total')} 个函数")
    for path, st in (res.get("modules") or {}).items():
        print(f"  {path}: {st['verified']}/{st['funcs']} 函数已验收；桩 {st['stubs']}")
    print("  进度曲线：")
    for row in res.get("curve") or []:
        print(f"    第{row['round']}轮 verified={row['verified']}/{row['total']}  {row['note']}")
    return res


def main_cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="iterate")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--requirement-file", required=True)
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--remote", action="store_true", help="让远端做架构师分诊")
    args = ap.parse_args(argv)
    add_cmd(args)


if __name__ == "__main__":
    main_cli()
