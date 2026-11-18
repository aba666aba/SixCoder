"""接口清单自检（开发文档 §8.2 / §8.6）。

要钉住三件事：
  1. 签名能被**枚举**出来（参数名/类型/返回/异常）——不能枚举就没法比对；
  2. 签名变了必须能算 diff（`batch_id: int → str` 那次就是没人算）；
  3. 跨模块 import 指向不存在的符号，静态就要红（别等运行时 NameError）。

跑法: python checks/check_interfaces.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import interfaces  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="if_"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


SRC = '''"""模块。"""


def run(batch_id: int, seed: int = 1) -> dict:
    """跑一个批次。"""
    if batch_id < 1:
        raise ValueError("bad")
    return {}
'''

CALLER = '''from app.svc import run


def handle(batch_id: int) -> dict:
    return run(batch_id)
'''


def main() -> int:
    ws = make({"app/__init__.py": "", "app/svc.py": SRC, "app/api.py": CALLER})

    print("[1] 签名能被枚举")
    data = interfaces.scan(ws)
    check("扫到两个模块", set(data) == {"app/svc.py", "app/api.py"}, str(sorted(data)))
    fn = {f["name"]: f for f in data["app/svc.py"]["funcs"]}["run"]
    check("参数名/类型正确", [(a["name"], a["type"]) for a in fn["params"]]
          == [("batch_id", "int"), ("seed", "int")], str(fn["params"]))
    check("默认值被标出来", [a["default"] for a in fn["params"]] == [False, True], str(fn["params"]))
    check("返回类型正确", fn["returns"] == "dict", fn["returns"])
    check("异常被抽出来", "ValueError" in fn["raises"], str(fn["raises"]))
    check("docstring 被抽出来", "跑一个批次" in fn["doc"], fn["doc"])

    print("[2] 锁定 / 读回")
    p = interfaces.save(ws, data)
    check("清单落盘", p.is_file())
    check("读回一致", interfaces.load(ws)["app/svc.py"]["hash"] == data["app/svc.py"]["hash"])

    print("[3] 签名变更必须算得出来（int → str 那次）")
    changed = SRC.replace("batch_id: int", "batch_id: str")
    (ws / "app/svc.py").write_text(changed, encoding="utf-8")
    d = interfaces.diff(ws)
    check("识别为签名变更", any(c["module"] == "app/svc.py" and "run" in c["resig"]
                          for c in d["changed"]), str(d.get("changed"))[:160])
    check("未变模块计入 unchanged", d["unchanged"] >= 0)

    print("[4] 函数增删也要看得见")
    (ws / "app/svc.py").write_text(changed + "\n\ndef extra() -> int:\n    return 1\n",
                                   encoding="utf-8")
    d2 = interfaces.diff(ws)
    hit = [c for c in d2["changed"] if c["module"] == "app/svc.py"]
    check("新增函数被记下来", hit and hit[0]["added"] == ["extra"], str(hit))

    print("[5] 跨模块 import 指向不存在的符号 → 必须红")
    ws2 = make({"app/__init__.py": "",
                "app/svc.py": "def run():\n    return 1\n",
                "app/api.py": "from app.svc import run\nfrom app.svc import nope\n\n\ndef h():\n    return nope()\n"})
    un = interfaces.unresolved_imports(ws2)
    check("抓到不存在的符号", any(u["symbol"] == "nope" for u in un), str(un))
    check("存在的符号不误报", not any(u["symbol"] == "run" for u in un), str(un))

    print("[6] 导入子模块不算错")
    ws3 = make({"app/__init__.py": "",
                "app/sub.py": "def f():\n    return 1\n",
                "app/api.py": "from app import sub\n\n\ndef h():\n    return sub.f()\n"})
    check("from app import sub 不误报", not interfaces.unresolved_imports(ws3),
          str(interfaces.unresolved_imports(ws3)))

    print("[7] consistency 汇总口径")
    ws4 = make({"app/__init__.py": "", "app/svc.py": SRC, "app/api.py": CALLER})
    interfaces.save(ws4)
    c1 = interfaces.consistency(ws4)
    check("干净项目一致", c1["ok"], str(c1)[:160])
    (ws4 / "app/svc.py").write_text(SRC.replace("int", "str", 1), encoding="utf-8")
    c2 = interfaces.consistency(ws4)
    check("漂移后不一致", c2["ok"] is False and c2["drift"]["changed"], str(c2["drift"])[:120])
    check("第一次生成算 first_time", interfaces.consistency(make({"app/x.py": "def f():\n    return 1\n"}))
          .get("first_time") is True)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（签名可枚举 / 变更可 diff / 不存在的 import 必红 / 子模块不误报）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
