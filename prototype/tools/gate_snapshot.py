"""闸门行为快照：重构前的安全网基线（阶段 0-a）。

## 为什么需要这个

路线 B 要把 30+ 个闸门从引擎里抽成可插拔判据。抽之前必须能回答一个问题：

    "这次重构有没有改变任何现有行为？"

只有快照能回答。做法：给每个闸门喂**固定的输入**，记录**完整输出**
（GateResult 的每个字段），存成 JSON。重构后再跑一遍，逐字段比对。

## 为什么不用 pytest 直接测

闸门大量依赖真实文件系统（ws / paths）、子进程（pytest / mypy / bandit）、
甚至网络（pip-audit）。直接用 pytest 写，每条都要搭夹具，而且很容易变成
"测了夹具而不是测闸门"。

快照法反过来：**先记录现状，再要求重构后逐字不变**。它不判断"对不对"，
只判断"变没变" —— 这正是重构期的安全网该做的事。

## 用法

    python tools/gate_snapshot.py --record    # 记录基线（重构前跑一次）
    python tools/gate_snapshot.py --verify    # 比对（重构后跑）

快照存在 tests/gate_snapshot.json，必须入库。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT.parent / "tests" / "gate_snapshot.json"


def _stable(res) -> dict:
    """把 GateResult 转成可比对的稳定字典（去掉耗时这种天然会变的东西）。"""
    if not isinstance(res, dict) and hasattr(res, "gate"):
        d = {
            "gate": getattr(res, "gate", ""),
            "ok": getattr(res, "ok", None),
            "evidence_kind": getattr(res, "evidence_kind", ""),
            "evidence_ref": getattr(res, "evidence_ref", ""),
            "detail": getattr(res, "detail", ""),
        }
    elif isinstance(res, dict):
        d = {k: res.get(k) for k in ("gate", "ok", "evidence_kind", "evidence_ref", "detail")}
    else:
        return {"raw": repr(res)[:400]}
    # detail 里常嵌绝对路径/临时目录名，归一再比
    det = str(d.get("detail") or "")
    for frag in ("\\", "/"):
        pass
    d["detail"] = det[:600]
    return d


def _mkws(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="gatesnap-"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


# ── 固定输入：每个闸门至少一个"应该通过"和一个"应该失败"的样本 ──

CASES: list[tuple[str, str, callable]] = []


def case(name: str, desc: str):
    def deco(fn):
        CASES.append((name, desc, fn))
        return fn
    return deco


@case("gate_syntax", "语法闸门：好文件通过 / 坏文件拦下")
def _c_syntax(g):
    ws_ok = _mkws({"a.py": "def f():\n    return 1\n"})
    ws_bad = _mkws({"a.py": "def f(:\n    return 1\n"})
    return {
        "good": _stable(g.gate_syntax(ws_ok, ["a.py"])),
        "bad": _stable(g.gate_syntax(ws_bad, ["a.py"])),
        "missing": _stable(g.gate_syntax(ws_ok, ["nope.py"])),
    }


@case("gate_anti_cheat", "反逃避闸门：7 项检查各喂一个反例")
def _c_anti(g):
    base = "def f():\n    return 1\n"
    out = {}
    # 实现节点改测试文件
    out["edit_test"] = _stable(g.gate_anti_cheat(
        _mkws({}), "impl", {"tests/test_x.py": ("", "def test_a():\n    assert 1\n")}))
    # 新增跳过标记
    out["skip_mark"] = _stable(g.gate_anti_cheat(
        _mkws({}), "test", {"tests/test_x.py": (base, base + "@pytest.mark.skip\ndef test_b(): pass\n")}))
    # 恒真断言
    out["trivial_assert"] = _stable(g.gate_anti_cheat(
        _mkws({}), "test", {"tests/test_y.py": ("", "def test_c():\n    assert True\n")}))
    # 裸 except: pass
    out["bare_except"] = _stable(g.gate_anti_cheat(
        _mkws({}), "impl", {"m.py": ("", "def f():\n    try:\n        pass\n    except Exception:\n        pass\n")}))
    # 删除既有测试
    out["del_test"] = _stable(g.gate_anti_cheat(
        _mkws({}), "test", {"tests/test_z.py": ("def test_keep():\n    assert 1\n", "")}))
    # 干净改动 -> 通过
    out["clean"] = _stable(g.gate_anti_cheat(
        _mkws({}), "impl", {"m.py": ("", base)}))
    return out


@case("gate_stub", "桩闸门：raise NotImplementedError 要被抓")
def _c_stub(g):
    ws = _mkws({
        "stub.py": "def f():\n    raise NotImplementedError\n",
        "real.py": "def f():\n    return 42\n",
    })
    return {
        "stub": _stable(g.gate_stub(ws, ["stub.py"])),
        "real": _stable(g.gate_stub(ws, ["real.py"])),
    }


@case("gate_import_ok", "导入闸门：能 import / 不能 import / 平台条件模块")
def _c_import(g):
    ws = _mkws({
        "good.py": "import os\n",
        "bad.py": "import definitely_not_a_real_module_xyz\n",
        "windows_only.py": "import msvcrt\n",
    })
    return {
        "good": _stable(g.gate_import_ok(ws, ["good.py"], timeout=30)),
        "bad": _stable(g.gate_import_ok(ws, ["bad.py"], timeout=30)),
        "platform": _stable(g.gate_import_ok(ws, ["windows_only.py"], timeout=30)),
    }


@case("gate_duplicates", "同文件重名闸门")
def _c_dup(g):
    ws = _mkws({
        "dup.py": "def f():\n    return 1\n\n\ndef f():\n    return 2\n",
        "ok.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n",
    })
    return {
        "dup": _stable(g.gate_duplicates(ws, ["dup.py"])),
        "ok": _stable(g.gate_duplicates(ws, ["ok.py"])),
    }


@case("gate_completeness", "完整性闸门：契约点名的函数全部存在")
def _c_complete(g):
    ws = _mkws({"m.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"})
    return {
        "all_present": _stable(g.gate_completeness(ws, ["m.py"], "a() 与 b() 都要有")),
        "missing_one": _stable(g.gate_completeness(ws, ["m.py"], "a() 与 zzz() 都要有")),
    }


@case("red_is_meaningful", "有效失败判定：测试自己写错不算有效失败")
def _c_red(g):
    return {
        "infra_error": _stable_dict(g.red_is_meaningful("NameError: name 'pytest' is not defined")),
        "real_red": _stable_dict(g.red_is_meaningful("AssertionError: 1 != 2")),
        "import_missing_is_real": _stable_dict(
            g.red_is_meaningful("ImportError: cannot import name 'divide' from 'calc'")),
    }


def _stable_dict(v) -> dict:
    if isinstance(v, tuple):
        return {"ok": v[0], "detail": str(v[1])[:300]}
    return {"raw": repr(v)[:300]}


def collect() -> dict:
    from core import gates as g
    out: dict = {}
    for name, desc, fn in CASES:
        try:
            out[name] = {"desc": desc, "result": fn(g)}
        except Exception as exc:                          # noqa: BLE001
            out[name] = {"desc": desc, "error": f"{type(exc).__name__}: {exc}"}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="闸门行为快照（阶段 0 安全网）")
    ap.add_argument("--record", action="store_true", help="记录基线")
    ap.add_argument("--verify", action="store_true", help="与基线比对")
    a = ap.parse_args(argv)
    if not (a.record or a.verify):
        ap.error("必须给 --record 或 --verify")

    now = collect()
    if a.record:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(now, ensure_ascii=False, indent=1, sort_keys=True),
                            encoding="utf-8")
        print(f"已记录基线：{SNAPSHOT}（{len(now)} 个闸门）")
        return 0

    if not SNAPSHOT.exists():
        print(f"基线不存在：{SNAPSHOT}\n先跑 --record")
        return 2
    base = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    diffs = []
    for name in sorted(set(base) | set(now)):
        b, n = base.get(name), now.get(name)
        if b != n:
            diffs.append(name)
    if diffs:
        print("行为发生变化（重构期间这应当是 0）：")
        for d in diffs:
            print("  - " + d)
        # 打印第一条的具体差异，便于定位
        first = diffs[0]
        print(f"\n第一条差异 {first}:")
        print("  基线:", json.dumps(base.get(first), ensure_ascii=False)[:500])
        print("  现在:", json.dumps(now.get(first), ensure_ascii=False)[:500])
        return 1
    print(f"OK 全部 {len(now)} 个闸门行为与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
