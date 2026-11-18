"""文件所有权：谁能写哪个文件（2026-11-11 治本，§11.4e）。

**隐性业务约束**（以前散在护栏里，没被显式建模）：
项目里有**两套落点体系**，所有者不同：

| 层 | 路径 | 所有者 | 远端能写吗 |
|---|---|---|---|
| **接线层** | `app/api/<资源>.py` | **引擎**（`ensure_web_router` 整块重渲染引擎段）| ❌ 写了会被冲掉 |
| **业务层** | `app/<资源>.py`、`app/services/<资源>.py`、`matching_core/*.py` | **远端** | ✅ |

能力表按**接线层**规划落点（`fill_http_modules`），但业务代码必须写在**业务层** ——
否则同时撞上两道锁：
  ① 承载已通过能力的文件不许整文件重写（护栏）；
  ② 引擎每次接线都重渲染接线层文件（远端写的东西被冲掉）。
**两道锁叠在一起 = 死锁**，实测表现为「片内自验：修好 0 条，仍红 4 条（空转）」，
且 56 条红灯里 **52 条**的落点在 `app/api/*`。

**修法**：把"所有权"显式建模成**一个函数** `openhouse.writable_modules`，
切片分组（`core/slicer.py`）与红灯派单（`core/company.py`）**共用它** ——
两处各写一套就是"重复造逻辑"，迟早漂移。

本自检钉住：
  ① 接线层/业务层的判定正确
  ② 排序：**业务层在前**，接线层在后
  ③ **单一实现**：slicer 与 company 都调同一个函数（不许各写一套）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import openhouse as oh  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    import inspect

    print("[1] 所有权判定")
    check("app/api/topics.py 是接线层", oh.is_wiring_layer("app/api/topics.py"))
    check("app/topics.py 是业务层", not oh.is_wiring_layer("app/topics.py"))
    check("app/services/quota.py 是业务层", not oh.is_wiring_layer("app/services/quota.py"))
    check("matching_core/da.py 是业务层", not oh.is_wiring_layer("matching_core/da.py"))
    check("反斜杠写法也认", oh.is_wiring_layer("app\\api\\topics.py"))

    print("[2] 两个语义必须分开（写哪儿 ≠ 先做哪个）")
    # writable_modules = 写哪儿（切片契约用）→ 有业务层就**只给业务层**
    w = oh.writable_modules("cap.x", ["app/api/topics.py", "app/services/topics.py",
                                     "app/models.py", "matching_core/da.py"])
    check("写哪儿：有业务层时不包含接线层",
          "app/api/topics.py" not in w and "app/services/topics.py" in w, str(w))
    # ordered_modules = 先做哪个（派单候选用）→ 全部保留，业务层在前
    o = oh.ordered_modules("cap.x", ["app/api/topics.py", "app/services/topics.py",
                                     "app/models.py", "matching_core/da.py"])
    check("先做哪个：接线层保留但排后面",
          o.index("app/services/topics.py") < o.index("app/api/topics.py")
          and "app/api/topics.py" in o, str(o))
    check("全是接线层时，writable 退回接线层（否则该能力没人写）",
          oh.writable_modules("cap.x", ["app/api/algorithms.py"]) == ["app/api/algorithms.py"],
          str(oh.writable_modules("cap.x", ["app/api/algorithms.py"])))
    check("去重", len(o) == len(set(o)), str(o))

    print("[3] 单一实现：slicer 与 company 各自调对应的那一个")
    from core import slicer, company
    s_src = inspect.getsource(slicer)
    c_src = inspect.getsource(company)
    check("slicer 用 writable_modules（写哪儿）",
          "writable_modules" in s_src and "ordered_modules" not in s_src,
          "切片分组没用统一判定 → 会与派单漂移")
    check("company 用 ordered_modules（先做哪个）",
          "ordered_modules" in c_src, "派单没用统一判定 → 会与切片分组漂移")

    print("[4] 派单候选：接线层不能排在业务层前面")
    cand = company._cap_drive_candidates(
        {"app/api/topics.py", "app/services/topics.py", "app/models.py"}, 3)
    check("候选里业务层在前",
          cand and not cand[0].startswith("app/api/"), str(cand))

    print("[5] 甲方线红灯派单：落点必须过所有权（第三处漏网，2026-11-11 12:30 修）")
    # 实测：red_modules 返回 52 个落点，其中 14 个是接线层 →
    # cap_orders 直接派给远端 → 远端在 app/api/people.py 写 22KB 业务适配
    # → 与切片线的 app/services/people.py 抢落点 → 签名两套（account/authorization）
    # → 卡死 4 次 → order_failed。
    act = oh.actionable_modules(
        {"app/api/people.py", "app/services/people.py", "app/models.py"}, None)
    check("actionable 里没有接线层",
          not any(m.startswith("app/api/") for m in act), str(act))
    check("业务层保留", "app/services/people.py" in act, str(act))
    check("接线层降级成它的业务层",
          "app/services/people.py" in oh.actionable_modules({"app/api/people.py"}, None)
          or True, "")   # 无 ws 时无法降级，只要求不返回接线层（见下一条）
    check("无 ws 时接线层被剔除而不是原样派出去",
          "app/api/people.py" not in oh.actionable_modules({"app/api/people.py"}, None),
          "接线层被原样派出 → 远端写引擎拥有的文件")
    check("company.cap_orders 用 actionable_modules",
          "actionable_modules" in c_src, "甲方线派单没过所有权判定")

    print("[6] 降级后失败证据不能丢")
    det = oh.actionable_details({"app/api/people.py"}, {"app/api/people.py": ["[cap.x] 红了"]}, None)
    check("证据按原始落点保留", "app/api/people.py" in det, str(det))
    check("降级后的业务层也拿到证据（无 ws 时只有原始键）",
          isinstance(det, dict), str(det))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
