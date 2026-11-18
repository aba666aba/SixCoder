"""C 档落点 1：把"语言级测试/类型检查"接成 props.py 能执行的判据 dict。

为什么单独一层（不让 props.py 直接 import lang）：
    props.py 的判据跑在**独立子进程的 RUNNER 字符串模板**里（见 core/props.py:138），
    模板内不能调用 core/lang.py 的模块级函数（实测调了就 NameError，全仓判据变红）。
    所以这里把"完整 cmd"塞进 check dict，让 RUNNER 拿到就能自包含地执行，不依赖 lang。

两种 check 的执行语义（供主代理在 RUNNER 里实现，模板内代码必须自包含）：
    · cmd 为空、或首元素不可执行 → ok=False, detail="判据没带命令"；
    · 否则 subprocess.run(cmd, cwd=ws_root, timeout=..., capture_output=True)，
      returncode==0 → 绿；非 0 → 红（detail 带 stdout/stderr 尾部）。
    · Windows 注意：npm/npx/mvn 是 .CMD 包装脚本，裸命令名 Popen 会
      FileNotFoundError；裸 "python" 又可能是没装 pytest 的解释器。模板里应把
      cmd[0] 用 shutil.which 解析成绝对路径、"python" 换成 sys.executable
      （与 core/lang.py 的 _effective_cmd 同口径），否则会误判红。

产出规则：
    · 只在该语言配了对应命令（test_cmd / typecheck_cmd）时才产出；
    · 工具链缺失（lang.available(cmd[0]) 为假）→ 不产出 —— 判不了不是产物错。
"""
from __future__ import annotations

from . import lang as _lang


def kinds() -> list[str]:
    return ["lang_test", "lang_typecheck"]


def checks_for(ws, lang: str) -> list[dict]:
    """产出该语言的 lang_test / lang_typecheck 判据 dict（形状对齐 props.py）。

    ws 暂不参与（保留参数以对齐 props 侧其它 check 生成接口）：命令在**产物仓库
    根目录**执行，路径由 RUNNER 的 ws_root 决定，不在生成期绑定。
    """
    out: list[dict] = []
    name = (_lang.LANGS.get(lang) or {}).get("name", lang)
    for kind, cmd in (("lang_test", _lang.test_command(lang)),
                      ("lang_typecheck", _lang.typecheck_command(lang))):
        if not cmd:
            continue
        if not _lang.available(cmd[0]):
            continue
        desc = (f"{name} 测试套件必须全绿" if kind == "lang_test"
                else f"{name} 类型/静态检查必须通过")
        out.append({"kind": kind, "lang": lang, "cmd": list(cmd),
                    "desc": desc, "hard": True})
    return out
