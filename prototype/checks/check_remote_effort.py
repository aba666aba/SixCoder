"""远端思考档：**完全相信远端**的闸门自检。

2026-11-09 用户铁律：整个系统完全接管程序开发，只能相信远端能力是足够的。
所以写码一律最高思考档，且**不允许任何机制把它按"成功率"往下压**。

背景（为什么必须有这个自检）：
    2026-11-09 14:43 夜班收工自动校准把 `code_effort_ceiling` 写成 `none`，
    依据是"none 成功率最高（50%）"。但那些 none 样本是机械小任务（quota.py /
    auth.py），而 low/high 样本的失败大量来自**引擎自己的缺陷**（多行签名被截断、
    新函数被静默丢掉、路由把 batches 判成没有匹配函数）——那些 bug 当天都修了，
    失败却已记进台账。于是形成自我强化的错误闭环：
        引擎造坏数据 → 判深思考"没用" → 封顶压到 none → 远端失败后永远升不了档
        → 只能原地重写 → 失败更多 → 更证明"不该思考"。
    `escalate_capped` 恒真 = 远端失去唯一的救援通道。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # checks/ 的上一级 = prototype/（换盘符不失效）
sys.path.insert(0, str(ROOT))

from core import calibration, effort, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK  " if cond else "  FAIL ") + name + ("" if cond else "   " + extra))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] effort.route：写码一律最高思考档")
    for kind in ("code", "bodies", "linefix", "delegate", "format", "mechanical"):
        got = effort.route(kind, 0)
        check(f"route({kind!r}, 难度0) = max", got == "max", f"实际 {got!r}")
    for kind in ("code", "bodies", "mechanical"):
        got = effort.route(kind, 9)
        check(f"route({kind!r}, 难度9) = max", got == "max", f"实际 {got!r}")
    check("route('spec') = max", effort.route("spec", 0) == "max")
    check("route('diagnose') = max", effort.route("diagnose", 0) == "max")

    print("[2] 难度分不再决定思考深度（但仍被记录，用于可解释性）")
    check("难度 0 → max", effort.route("code", 0) == "max")
    check("难度 9 → max", effort.route("code", 9) == "max")
    check("difficulty() 仍能算出分数（事件流要用）",
          effort.difficulty(funcs=9, examples=9, fails=9, stub=True, lines=999) >= 7)

    print("[3] 校准器不得再按成功率下调 code_effort_ceiling")
    src = (ROOT / "core" / "calibration.py").read_text(encoding="utf-8")
    check("不再有 'want = max(good)[1]' 这种按成功率选档的写法",
          "want = max(good)[1]" not in src,
          "校准器又会把封顶按成功率压下去")
    check("封顶被显式写成 max", '"code_effort_ceiling"] = "max"' in src)

    print("[4] 当前生效的封顶就是 max")
    cur = calibration.current().get("code_effort_ceiling")
    check("calibration.current().code_effort_ceiling = max", cur == "max", f"实际 {cur!r}")
    raw = settings_store.get_remote().get("calibration") or {}
    check("settings 里的封顶 = max", raw.get("code_effort_ceiling") == "max",
          f"实际 {raw.get('code_effort_ceiling')!r}")

    print("[5] 升档通道：封顶 max 时 escalate_capped 不会恒真")
    steps = settings_store.ladder()
    cap_lvl = effort.pick(steps, "max")
    check("pick(steps,'max') 落在最高档", cap_lvl == len(steps) - 1,
          f"cap_lvl={cap_lvl} / len={len(steps)}")
    check("从第 0 档到封顶之间还有档可升（升档不是死的）",
          len(steps) - 1 > 0, f"只有 {len(steps)} 档")

    # ── 2026-11-10 双向自适应：route() 只管封顶，起点走「实测档案优先 + 难度先验」──
    print("[6] 起点：route_start 难度先验（max 永不当起点）")
    for s, want in ((0, "none"), (2, "none"), (3, "low"), (5, "low"),
                    (6, "high"), (9, "high")):
        got = effort.route_start(s)
        check(f"route_start({s}) = {want}", got == want, f"实际 {got!r}")
    for s in range(10):
        check(f"route_start({s}) 永不返回 max", effort.route_start(s) != "max",
              effort.route_start(s))

    print("[7] 起点：proven_floor 模块实测档案（只认成功证据）")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        d = ws / ".sixcoder"
        d.mkdir(parents=True)
        # 无台账 → 空
        check("无台账返回空串", effort.proven_floor(ws, "app/x.py") == "")
        # 同模块成功过 none → 返回 none
        (d / "author_attempts.jsonl").write_text(
            '\n'.join([
                '{"path":"app/x.py","effort":"low","ok":true,"funcs":2,"cost_cny":0.01}',
                '{"path":"app/x.py","effort":"none","ok":true,"funcs":2,"cost_cny":0.005}',
                '{"path":"app/x.py","effort":"max","ok":false,"funcs":2,"cost_cny":0.02}',
            ]) + "\n", encoding="utf-8")
        check("同模块成功过的最低档 = none（失败的 max 不影响）",
              effort.proven_floor(ws, "app/x.py") == "none",
              effort.proven_floor(ws, "app/x.py"))
        # 只有失败记录 → 空（失败不是降档证据，2026-11-09 教训）
        (d / "author_attempts.jsonl").write_text(
            '{"path":"app/y.py","effort":"none","ok":false}\n', encoding="utf-8")
        check("只有失败记录返回空串（失败不作降档证据）",
              effort.proven_floor(ws, "app/y.py") == "")
        # 不同模块 → 空
        (d / "author_attempts.jsonl").write_text(
            '{"path":"other.py","effort":"none","ok":true}\n', encoding="utf-8")
        check("不同模块的档案不相通", effort.proven_floor(ws, "app/z.py") == "")
        # 台账损坏 → 空且不抛错
        (d / "author_attempts.jsonl").write_text("不是json{{{\n", encoding="utf-8")
        check("台账损坏不抛错、返回空串", effort.proven_floor(ws, "app/x.py") == "")

    print("[8] company.py 起点接线（实测档案优先 + 先验兜底，静态钉住）")
    csrc = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("起点 = proven_floor 优先", "_start = _pf or _eff_m.route_start(_score)" in csrc)
    check("proven_floor 查询已接入起点", "_eff_m.proven_floor(ws, rel)" in csrc)
    check("档案命中时事件流解释来源", "模块实测档案" in csrc)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（封顶恒 max / 校准器不下调 / 升档通道在 / "
          "起点先验分段 / max 不当起点 / 模块档案只认成功 / 起点接线钉住）")
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
