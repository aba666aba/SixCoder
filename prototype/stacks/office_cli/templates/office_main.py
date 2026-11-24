"""Office 文档处理工具入口（**由引擎生成，不要改**）。

为什么不让你写这个文件：子命令是分模块长的（commands/to_csv.py、commands/merge.py …），
每加一个命令就重写入口 -> 上一个命令被抹掉。

你只需要在 commands/*.py 里写 `def run(args) -> int` 并返回退出码。
args.items 是子命令后面的位置参数（如 `to_csv in.xlsx` -> args.items == ["in.xlsx"]）。

退出码约定（判据按这个判）：
    0  成功
    1  业务失败（程序跑通了，但结果不对）
    2  用法错误（参数不对、子命令不存在）
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys

COMMANDS_DIR = "commands"


def _discover_commands():
    """扫 commands/ 下的 .py，返回 {模块名: 命令名}。

    命令名 = 文件名去 .py，下划线转连字符（to_csv -> to-csv）。
    以 _ 开头的文件跳过（内部工具，不是命令）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(here, COMMANDS_DIR)
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        stem = fn[:-3]
        if stem in ("__init__", "office_main"):
            continue
        out[COMMANDS_DIR + "." + stem] = stem.replace("_", "-")
    return out


def _load_command(module_name):
    """加载子命令模块取 run()。单个命令坏了不拖垮整体。"""
    try:
        if importlib.util.find_spec(module_name) is None:
            return None, "模块不存在"
    except (ImportError, ValueError) as exc:
        return None, "模块名无效: %s" % exc
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:                          # noqa: BLE001
        return None, "import 失败: %s: %s" % (type(exc).__name__, exc)
    fn = getattr(mod, "run", None)
    if not callable(fn):
        return None, "没有定义 run(args)"
    return fn, ""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="office_main.py",
        description="Office 文档处理工具（xlsx / docx / pptx）",
    )
    parser.add_argument("--version", action="version", version="0.1.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for module_name, cmd_name in sorted(_discover_commands().items()):
        fn, why = _load_command(module_name)
        if fn is None:
            # 不可用的命令仍出现在帮助里（标 unavailable），不静默消失
            p = sub.add_parser(cmd_name, help="(unavailable: %s)" % why)
            p.set_defaults(_unavailable=why)
            continue
        doc = (fn.__doc__ or "").strip().splitlines()
        p = sub.add_parser(cmd_name, help=doc[0] if doc else "")
        p.add_argument("items", nargs="*", metavar="ARG",
                       help="子命令参数（具体含义由命令自己解释）")
        p.set_defaults(_run=fn)

    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2

    why = getattr(args, "_unavailable", None)
    if why:
        print("命令 %s 当前不可用：%s" % (args.command, why), file=sys.stderr)
        return 1

    fn = getattr(args, "_run", None)
    if fn is None:
        parser.print_help(sys.stderr)
        return 2

    try:
        rc = fn(args)
    except SystemExit:
        raise
    except Exception as exc:                          # noqa: BLE001
        print("执行失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0 if rc is None else int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
