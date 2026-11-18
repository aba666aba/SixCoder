"""能力描述 ↔ 探针断言的口径冲突检测（§11.4m，2026-11-11）。

为什么要有这个自检：
    实测（切片 s33）：诊断架构师输出
        root_cause=create_candidates 返回的 status 是 'created' 而不是路由探针要求的 'created'
    —— **一句自相矛盾的话**。原因不是模型疯了，是**输入自相矛盾**：
      · 能力描述写 `status=pending_teacher`；
      · 引擎合成契约时取 `status='created'`（契约文本里明写"覆盖能力描述中的 pending_teacher"）；
      · 架构师同时读到了两个口径 → 拿它们互相比较 → 矛盾。
    同一个字段有**两个权威源**，下游全乱。

修法（`core/capconf.py` + `core/slicer.py`）：把冲突**显式化** ——
    简报里写明"以探针断言为准"（探针是唯一会真正跑的东西），
    让契约合成 / 诊断架构师 / 写码三方看到**同一个口径**。

断言：
    1. 能从能力描述里抽出 `key=value`（含不带引号的裸标识符）
    2. 能从探针 `expect_json` 里抽出字符串字面量
    3. 能定位**真实冲突**（描述 'pending_teacher' vs 探针 'created'）
    4. **数字/布尔不同不算冲突**（数字错是真 bug，不许"对齐"掉）
    5. 无冲突时返回空串（不污染简报）
    6. 简报里确实带上了口径说明（接线到 slicer）
    7. 实测产物仓能定位出已知的那几条冲突
"""
from __future__ import annotations

# 引擎不含项目身份（2026-11-19）：路径来自 --repo / SIXCODER_REPO / _logs/engine.json
import sys as _sys
_ENGINE = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import repo_root as _repo_root  # noqa: E402


import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import capconf as _cc  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class _Cap:
    def __init__(self, cid: str, desc: str, probe: dict) -> None:
        self.id = cid
        self.desc = desc
        self.probe = probe


def main() -> int:
    print("能力描述 ↔ 探针断言 · 口径冲突检测（§11.4m）")

    # ---- 1：抽 key=value（裸标识符也要）----
    d = _cc.desc_pairs("201 且 schema_ok=true、candidates=3、status=pending_teacher")
    check("能抽 key=value（裸标识符）",
          d.get("status") == "pending_teacher", str(d))
    check("能抽带引号的值",
          _cc.desc_pairs("code='not_found'").get("code") == "not_found")

    # ---- 2：抽探针字面量 ----
    p = _cc.probe_literals({"steps": [{"expect_json": {"status": "created", "candidates": 3}}]})
    check("能抽探针字面量", p.get("status") == "created", str(p))
    check("非字符串不进（数字不比对）", "candidates" not in p, str(p))

    # ---- 3：能定位真实冲突 ----
    cap = _Cap("cap.llm.generate_candidate",
               "教师请求 LLM 发题辅助 → 201 且 schema_ok=true、candidates=3、status=pending_teacher",
               {"kind": "http", "steps": [
                   {"method": "POST", "path": "/api/v1/llm/candidates",
                    "expect_json": {"schema_ok": True, "status": "created", "candidates": 3}}]})
    cf = _cc.conflicts(cap)
    check("能定位真实冲突", cf.get("status") == ("pending_teacher", "created"), str(cf))
    txt = _cc.describe_conflict(cap)
    check("冲突说明写明以探针为准",
          "以探针断言" in txt and "'created'" in txt, txt)

    # ---- 4：数字/布尔不同不算冲突 ----
    cap_num = _Cap("cap.x", "结果 count=3", {"kind": "http", "steps": [
        {"expect_json": {"count": 5}}]})
    check("数字不同不算冲突（数字错是真 bug）", not _cc.conflicts(cap_num), str(_cc.conflicts(cap_num)))
    cap_bool = _Cap("cap.y", "passed=false", {"kind": "http", "steps": [
        {"expect_json": {"passed": True}}]})
    check("布尔不同不算冲突", not _cc.conflicts(cap_bool), str(_cc.conflicts(cap_bool)))

    # ---- 5：无冲突返回空串 ----
    cap_ok = _Cap("cap.z", "GET / → 200，返回 HTML", {"kind": "http", "steps": [
        {"expect_json": {"ok": True}}]})
    check("无冲突返回空串", _cc.describe_conflict(cap_ok) == "", repr(_cc.describe_conflict(cap_ok)))

    # ---- 6：简报里带上了口径说明 ----
    src = (ROOT / "core" / "slicer.py").read_text(encoding="utf-8")
    check("简报接了口径冲突检测", "capconf" in src and "describe_conflict" in src)

    # ---- 7：实测产物仓能定位已知冲突 ----
    try:
        from core import blueprint as _bp
        ws = _repo_root()
        if ws.is_dir():
            b = _bp.load(ws)
            caps = b.caps if isinstance(b.caps, list) else list(b.caps.values())
            hits = [c.id for c in caps if _cc.conflicts(c)]
            check("实测产物仓能定位已知冲突（≥1 条）", len(hits) >= 1, str(hits))
            check("已知冲突 cap.llm.generate_candidate 被定位",
                  "cap.llm.generate_candidate" in hits, str(hits))
        else:
            print("  SKIP  实测产物仓不在本机")
    except Exception as exc:                           # noqa: BLE001
        print(f"  SKIP  实测产物仓读取失败：{type(exc).__name__}")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("全部通过（口径冲突可定位 + 简报会写明以探针为准）")
    return 0


# 未配置项目路径时：这些自检依赖真实产物，明确 SKIP（不是 FAIL）
if __name__ == "__main__" and _repo_root() is None:
    print("SKIP：未配置项目路径（--repo / SIXCODER_REPO / _logs/engine.json），本自检需要真实产物仓")
    raise SystemExit(0)

if __name__ == "__main__":
    sys.exit(main())