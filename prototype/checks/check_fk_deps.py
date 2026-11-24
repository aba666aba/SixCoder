"""外键依赖推导自检（2026-11-08）。

背景：蓝图 76 条能力里只有 20 条有 deps，且全是"响应字段被下一步用到"的数据流依赖。
56 条是孤儿 → 拓扑序退化成 cap 顺序 → 切片先做边角细节，主干（发题→填报→匹配→结果）
排在后面，跑了一天没有一条能走通的主干。

本自检钉住"外键即结构依赖"这条规则，以及两个已知的误连：
  · 负向用例（期望 403）不能当资源提供者 —— 否则"填报志愿"会连到"越权发题"上
  · 操作端点（/batches/{id}/run）不能当资源提供者 —— 否则资源名会变成 run
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as bp  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def cap(cid, steps, desc="x"):
    return bp.Cap(id=cid, desc=desc, funcs_est=1,
                  probe={"kind": "http", "steps": steps})


def main() -> int:
    print("[1] 资源名单复数")
    check("topics → topic", bp._singular("topics") == "topic", bp._singular("topics"))
    check("majors → major", bp._singular("majors") == "major", bp._singular("majors"))
    check("preferences → preference",
          bp._singular("preferences") == "preference", bp._singular("preferences"))
    check("status 不动（ss 结尾）",
          bp._singular("status") == "status", bp._singular("status"))

    print("\n[2] 外键推出结构依赖")
    b = bp.Blueprint(goal="g")
    b.add(cap("cap.topic.create", [{"method": "POST", "path": "/api/v1/topics",
                                    "json": {"title": "t", "quota": 2},
                                    "expect_status": 201}]))
    b.add(cap("cap.pref.fill", [{"method": "POST", "path": "/api/v1/preferences",
                                 "json": {"topic_id": "1"}, "expect_status": 201}]))
    b.add(cap("cap.batch.create", [{"method": "POST", "path": "/api/v1/batches",
                                    "json": {"name": "n"}, "expect_status": 201}]))
    b.add(cap("cap.batch.run", [{"method": "POST",
                                 "path": "/api/v1/batches/{batch_id}/run",
                                 "expect_status": 200}]))
    added = bp.derive_fk_deps(b)
    # 填报→发题、跑匹配→建批次；建批次本身没有外键，是根
    check("推导出 2 条边", added == 2, f"added={added}")
    check("填报依赖发题",
          "cap.topic.create" in b.by_id("cap.pref.fill").deps,
          str(b.by_id("cap.pref.fill").deps))
    check("跑匹配依赖建批次",
          "cap.batch.create" in b.by_id("cap.batch.run").deps,
          str(b.by_id("cap.batch.run").deps))

    print("\n[3] 两个已知误连")
    b2 = bp.Blueprint(goal="g")
    b2.add(cap("cap.auth.forbidden_role", [
        {"method": "POST", "path": "/api/v1/topics", "json": {"title": "x"},
         "expect_status": 403}]))
    b2.add(cap("cap.topic.create", [{"method": "POST", "path": "/api/v1/topics",
                                     "json": {"title": "t", "quota": 2},
                                     "expect_status": 201}]))
    b2.add(cap("cap.pref.fill", [{"method": "POST", "path": "/api/v1/preferences",
                                  "json": {"topic_id": "1"}, "expect_status": 201}]))
    bp.derive_fk_deps(b2)
    check("负向用例不当资源提供者",
          "cap.auth.forbidden_role" not in b2.by_id("cap.pref.fill").deps,
          str(b2.by_id("cap.pref.fill").deps))
    check("真提供者被选中",
          "cap.topic.create" in b2.by_id("cap.pref.fill").deps,
          str(b2.by_id("cap.pref.fill").deps))

    b3 = bp.Blueprint(goal="g")
    b3.add(cap("cap.batch.run", [{"method": "POST",
                                  "path": "/api/v1/batches/{batch_id}/run",
                                  "expect_status": 200}]))
    b3.add(cap("cap.run2", [{"method": "POST", "path": "/api/v1/run",
                             "json": {"run_id": "1"}, "expect_status": 201}]))
    bp.derive_fk_deps(b3)
    check("操作端点不当资源提供者",
          "cap.batch.run" not in b3.by_id("cap.run2").deps,
          str(b3.by_id("cap.run2").deps))

    print("\n[4] 幂等")
    n1 = bp.derive_fk_deps(b)
    check("重复调用不再加边", n1 == 0, f"second={n1}")

    print("\n[5] 前置步骤必须进依赖（否则带一条注定 404 的链被派单）")
    b4 = bp.Blueprint(goal="g")
    b4.add(cap("cap.auth.reg", [{"method": "POST", "path": "/api/v1/auth/register",
                                 "json": {"username": "u", "password": "p"},
                                 "expect_status": 201}]))
    b4.add(cap("cap.auth.login", [{"method": "POST", "path": "/api/v1/auth/login",
                                   "json": {"username": "u", "password": "p"},
                                   "expect_status": 200, "save": {"token": "token"}}]))
    b4.add(cap("cap.topic.create", [{"method": "POST", "path": "/api/v1/topics",
                                     "json": {"title": "t"},
                                     "headers": {"Authorization": "Bearer ${token}"},
                                     "expect_status": 201}]))
    bp.normalize_http(b4)
    tc = b4.by_id("cap.topic.create")
    check("用到 token 就依赖 login", "cap.auth.login" in tc.deps, str(tc.deps))
    check("也依赖 register（login 前要先注册）", "cap.auth.reg" in tc.deps, str(tc.deps))
    # 幂等：再跑一次不重复加
    n_before = len(tc.deps)
    bp.normalize_http(b4)
    check("重复调用不重复加依赖", len(tc.deps) == n_before,
          f"{n_before} -> {len(tc.deps)}")

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（外键依赖可机械推导，负向用例与操作端点不会冒充资源提供者）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
