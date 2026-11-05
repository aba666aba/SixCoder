r"""派工与交付自检：三个机制都得"能被否掉"才算成立。

1. **setup 依赖**（治"连坐失败"）
   store.py 里 `create_table` 没验收时，upsert/fetch_all 必然因为"表不存在"一起失败，
   把每张单的 2 次重试机会烧光。规则：有未验收的 setup 型函数时，只派它。
   反例：setup 已验收 → 必须把后面的派出来（不许永远只派 setup）。

2. **隔离前救一次**（治"桩被放弃"）
   连续失败 2 次的单，在隔离（≥3 次不再派）之前先让远端直接做一次；
   远端不可用/也失败 → 记进 abandoned，并写进交付报告。
   反例：远端不可用时不许假装救过。

3. **缺依赖只报告、绝不安装**（甲方明确要求 pip 不放开）
   交付报告列出缺什么 + 建议装哪个；同时沙箱必须**继续拦 pip install**。

跑法：python check_dispatch.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import company, contract as cm, iterate, sandbox, workorder  # noqa: E402

FAILS: list[str] = []
FIXTURE = Path(__file__).resolve().parent.parent / "tools" / "fixtures" / "hq_requirement.txt"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def fake_state(cs: dict, store_verified: list[str], store_have: list[str]) -> dict:
    st = {}
    for path in cs:
        if path == "store.py":
            st[path] = {"symbols": set(store_have), "stubs": set(),
                        "verified_names": list(store_verified), "fails_all": []}
        else:
            st[path] = {"symbols": set(), "stubs": set(), "verified_names": [], "fails_all": []}
    return st


def main() -> int:
    cs = cm.parse_requirement(FIXTURE.read_text(encoding="utf-8"))

    print("[1] setup 依赖：建表没好之前不许派后面的单")
    have = ["create_table", "upsert", "fetch_all"]
    o1 = [o for o in workorder.triage(Path("."), cs, fake_state(cs, [], have))
          if o["target"] == "store.py"]
    check("未验收 setup → 只派 create_table", [x["symbol"] for x in o1] == ["create_table"],
          str([x["symbol"] for x in o1]))
    check("原因写清了（连坐失败）", any("连坐" in x["detail"] or "前提" in x["detail"] for x in o1),
          str(o1[:1]))
    o2 = [o for o in workorder.triage(Path("."), cs, fake_state(cs, ["create_table"], have))
          if o["target"] == "store.py"]
    check("setup 已验收 → 派出 upsert/fetch_all",
          {x["symbol"] for x in o2} == {"upsert", "fetch_all"}, str([x["symbol"] for x in o2]))

    print("[2] 隔离前救一次 + 被放弃的单要进交付报告")
    ws = Path(tempfile.mkdtemp(prefix="disp_"))
    (ws / "store.py").write_text("def create_table(conn):\n    raise NotImplementedError\n",
                                 encoding="utf-8")
    abandoned = {"store.py::create_table::implement": {
        "order": "[implement] store.py::create_table", "target": "store.py",
        "symbol": "create_table", "fails": 3, "remote_tried": True,
        "reason": "【G5 测试】NameError: conn 未定义", "detail": "建表"}}
    state = iterate.inspect(ws, cs)
    p = company.write_delivery(ws, FIXTURE.read_text(encoding="utf-8"), state, [],
                              "（自检）", 0.0, abandoned=abandoned)
    txt = p.read_text(encoding="utf-8")
    check("交付报告有「被放弃的工作单」段", "被放弃的工作单" in txt, txt[:80])
    check("列出了具体被放弃的单", "create_table" in txt)
    check("标了远端是否试过", "远端试过" in txt)
    check("没有 abandoned 时不出现该段",
          "被放弃的工作单" not in company.write_delivery(
              ws, "需求：写个函数", state, [], "（自检）", 0.0).read_text(encoding="utf-8"))
    check("_remote_available 失败时返回 False（不抛）",
          company._remote_available() in (True, False))

    print("[3] 缺依赖：只报告，绝不安装；沙箱继续拦 pip")
    ws2 = Path(tempfile.mkdtemp(prefix="depz_"))
    (ws2 / "a.py").write_text("import json\nimport definitely_missing_pkg_xyz\n", encoding="utf-8")
    miss = company._missing_deps(ws2)
    check("检出缺失第三方模块", miss == ["definitely_missing_pkg_xyz"], str(miss))
    check("标准库不算缺依赖", "json" not in miss)
    p2 = company.write_delivery(ws2, "需求", {}, [], "（自检）", 0.0)
    t2 = p2.read_text(encoding="utf-8")
    check("报告里有「需要你安装的依赖」段", "需要你安装的依赖" in t2)
    check("清单文件已写", (ws2 / "requirements.missing.txt").exists())
    src = (Path(__file__).resolve().parent.parent / "core" / "company.py").read_text(
        encoding="utf-8")
    deps_src = src.split("def _missing_deps")[1].split("\ndef ")[0]
    check("_missing_deps 里没有任何执行调用的手段",
          all(k not in deps_src for k in ("subprocess", "os.system", "popen", "os.exec")),
          deps_src[:120])
    check("源码里没有以 pip 作为程序去执行的代码", "'pip'" not in src and '"pip"' not in src)
    sb = sandbox.Sandbox(ws2, mode="write")
    blocked = False
    try:
        sb.check_cmd("pip install requests")
    except sandbox.SandboxError:
        blocked = True
    check("沙箱仍然拦 pip install（裸 pip）", blocked)
    blocked2 = False
    try:
        sb.check_cmd("python -m pip install requests")
    except sandbox.SandboxError as exc:
        blocked2 = "pip" in str(exc) or "拒绝" in str(exc)
    check("沙箱仍然拦 python -m pip install", blocked2)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（setup 依赖 / 隔离前救一次 / 缺依赖只报告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
