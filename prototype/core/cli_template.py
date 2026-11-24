"""CLI 骨架模板（P1，2026-11-24）。

## 为什么需要独立的骨架

FastAPI 的骨架是 WEB_ENTRY：ASGI 应用 + include_router。
CLI 的骨架完全不同 —— 需要参数解析、子命令分发、退出码约定。
照搬 Web 那套只会生成一个跑不起来的文件。

## 设计原则（与 WEB_ENTRY 一致）

1. **入口只做组装，不做业务**：只负责解析参数、找到对应模块、调它、按约定返回退出码。
2. **单个子命令坏了不拖垮整个程序**：某个模块 import 失败，只让那条命令不可用，
   其他命令照常工作（对应 Web 的"坏模块跳过即可"）。
3. **退出码是契约**：0 成功 / 2 用法错误 / 1 业务失败。判据靠它判过不过。
4. **--help 必须可用**：这是最基本的可用性证据，也是最容易漏的一项。

## 与 judges_cli 的配合

judges_cli 的判据（退出码 / 输出 / 产物）就是照这个骨架的约定写的。
骨架改了约定，判据也要同步改 —— 两者是同一个契约的两面。
"""
from __future__ import annotations

#: 入口文件名（CLI 计划默认写这里）
CLI_ENTRY_FILE = "main.py"

CLI_ENTRY_MARK = "由引擎生成（CLI 入口）"


CLI_ENTRY = '''"""命令行入口（**由引擎生成，不要改**）。

为什么不让你写这个文件：子命令是分模块长的（cmd/add.py、cmd/list.py …），
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


# 引擎按蓝图填的子命令清单：模块名 -> 命令名
_COMMANDS = {commands!r}


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
        prog="prog",
        description="SixCoder 生成的命令行工具",
    )
    parser.add_argument("--version", action="version", version="0.1.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for module_name, cmd_name in sorted(_COMMANDS.items()):
        fn, why = _load_command(module_name)
        if fn is None:
            # 不可用的命令**仍然出现在帮助里**（标成 unavailable），
            # 而不是静默消失 —— 否则用户以为"没这个功能"，其实是它坏了。
            p = sub.add_parser(cmd_name, help="(unavailable: %s)" % why)
            p.set_defaults(_unavailable=why)
            continue
        p = sub.add_parser(cmd_name, help=(fn.__doc__ or "").strip().splitlines()[0]
                          if fn.__doc__ else "")
        # 通用位置参数：子命令的剩余参数。
        #
        # 为什么是"通用"而不是让每个子命令声明自己的参数：
        # 骨架是代码写的，但**每个子命令要什么参数是业务决定的**（远端写的），
        # 骨架不可能预先知道。所以给一个约定：位置参数进 args.items。
        # 实测过的坑（2026-11-24）：不给这个参数，`add 写测试` 会被 argparse
        # 判成 "unrecognized arguments" 退出码 2 —— 命令一有参数就用不了。
        #
        # 模块想用更正式的参数（--flag）时，可以在自己的 run() 里再解析 args.items，
        # 或者改这个骨架 —— 但默认这一层必须能用。
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
'''


def render_entry(commands) -> str:
    """按子命令清单渲染入口。

    commands: {模块名: 命令名}。空字典也能生成一个只有 --help 的可用入口。
    """
    return CLI_ENTRY.replace("{commands!r}", repr(dict(sorted((commands or {}).items()))))
