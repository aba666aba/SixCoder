"""命令行入口（**由引擎生成，不要改**）。

为什么不让你写这个文件：子命令是分模块长的（commands/add.py、commands/list.py …），
每加一个命令就重写一次入口 → 上一个命令被抹掉。
所以入口只做一件事：解析参数、把子命令分发给对应模块的 run()。
你只需要在命令模块里写 `def run(args) -> int` 并返回退出码。
args.items 是子命令后面的位置参数列表（如 `add 写测试` -> args.items == ["写测试"]）。

退出码约定（判据按这个判）：
    0  成功
    1  业务失败（程序跑通了，但结果不对）
    2  用法错误（参数不对、子命令不存在）
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys


# 子命令清单：**运行时扫 commands/ 自己发现**，不依赖引擎填占位符。
#
# 为什么不用引擎替换占位符（2026-11-24 实测）：生态包的 apply_templates
# 是**原样复制文件**，不做任何变量替换 —— 模板里写 {commands!r} 会被
# 原样复制成一行语法错误（复制出来的 cli.py 直接跑不起来）。
#
# 自己扫目录反而更稳：
#   · 加一个命令只要丢一个 commands/xxx.py，不用重新生成入口
#   · 没有 commands/ 目录时就是一个只有 --help 的可用程序，不报错
COMMANDS_DIR = "commands"


def _discover_commands():
    """扫 commands/ 下的 .py，返回 {模块名: 命令名}。

    命令名 = 文件名（去 .py），下划线转连字符（add_item -> add-item）。
    以 _ 开头的文件跳过（内部工具，不是命令）。
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(here, COMMANDS_DIR)
    out = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out                       # 没有目录 = 没有命令，入口仍然可用
    for fn in names:
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        stem = fn[:-3]
        if stem in ("__init__", "cli"):
            continue
        out[COMMANDS_DIR + "." + stem] = stem.replace("_", "-")
    return out


def _load_command(module_name):
    """加载一个子命令模块，取它的 run()。

    **单个命令坏了不拖垮整个程序**（与 Web 入口的"坏模块跳过"同一原则）：
    模块 import 失败或没定义 run()，就返回 (None, 原因)，
    上层把这条命令标记成不可用，其它命令照常工作。
    """
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
    """构造参数解析器。子命令的帮助文本可来自模块的 __doc__。"""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="SixCoder 生成的命令行工具",
    )
    parser.add_argument("--version", action="version", version="0.1.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for module_name, cmd_name in sorted(_discover_commands().items()):
        fn, why = _load_command(module_name)
        if fn is None:
            # 不可用的命令**仍然出现在帮助里**（标成 unavailable），
            # 而不是静默消失 —— 否则用户以为"没这个功能"，其实是它坏了。
            p = sub.add_parser(cmd_name, help="(unavailable: %s)" % why)
            p.set_defaults(_unavailable=why)
            continue
        doc = (fn.__doc__ or "").strip().splitlines()
        p = sub.add_parser(cmd_name, help=doc[0] if doc else "")
        # 通用位置参数：子命令的剩余参数。
        # 为什么是"通用"而不是每个子命令声明自己的参数：
        # 骨架是代码写的，但每个子命令要什么参数是业务决定的（远端写的），
        # 骨架不可能预先知道。所以约定：位置参数进 args.items，模块自己解释。
        p.add_argument("items", nargs="*", metavar="ARG",
                       help="子命令参数（具体含义由命令自己解释）")
        p.set_defaults(_run=fn)

    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        # 没给子命令 = 用法错误。打印帮助到 stderr，退出码 2。
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
        # 业务异常统一变成退出码 1 + 可读信息，不吐 traceback 给用户。
        print("执行失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0 if rc is None else int(rc)


if __name__ == "__main__":
    raise SystemExit(main())