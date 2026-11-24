"""通用探针判据测试（2026-11-24 打通）。

## 背景

核对三处注册表时发现：probe.py 支持 7 种探针（cli/py_call/pytest/http/
browser/sql/electron），但 judges 里一条都没有。于是 go_service /
node_express / spring_boot（声明 probe:cli）、microservice（probe:sql）、
py_lib（probe:py_call）这些包，验收能力完全没接进判据体系。

## 证明什么

1. 7 条探针判据都注册了，且对所有栈可见
2. 真跑探针：过的判绿、不过的判红并指出原因
3. **没有 probes.json = 判红，不是判通过**（沿用 props.py:414 的口径）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import judges, judges_probe   # noqa: E402


def _proj(probes=None, app_src=None) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="probejudge-"))
    (ws / ".sixcoder").mkdir(parents=True, exist_ok=True)
    if app_src is not None:
        (ws / "app.py").write_text(app_src, encoding="utf-8")
    if probes is not None:
        (ws / ".sixcoder" / "probes.json").write_text(
            json.dumps(probes, ensure_ascii=False), encoding="utf-8")
    return ws


SAY_HELLO = (
    "import sys\n"
    "args = sys.argv[1:]\n"
    "if not args:\n"
    "    print(\"usage\")\n"
    "    sys.exit(2)\n"
    "print(\"hello\", args[0])\n"
)


def test_all_seven_probe_judges_registered():
    reg = judges.full_registry()
    keys = {j.key for j in reg.all()}
    for kind in judges_probe.PROBE_KINDS:
        assert "probe." + kind in keys, "缺 probe." + kind


def test_probe_judges_visible_for_every_stack():
    """探针判据用 ANY_STACK：用哪种探针由项目声明，不由栈名。"""
    reg = judges.full_registry()
    for stack in ("fastapi", "cli_tool", "go_service", "microservice",
                  "node_express", "py_lib", "spring_boot", "electron"):
        keys = {j.key for j in reg.for_stack(stack)}
        assert "probe.cli" in keys, stack + " 缺 probe.cli"


def test_passing_probe_is_green():
    ws = _proj(
        probes={"greet": {"kind": "cli", "entry": "app.py", "args": ["world"],
                         "stdout_has": "hello world", "expect_exit": 0}},
        app_src=SAY_HELLO)
    j = judges.full_registry().get("probe.cli")
    r = j.check(judges.JudgeContext(ws=ws, extras={"timeout": 60}))
    assert r.ok is True, r.detail
    assert r.evidence_kind == "probe"


def test_failing_probe_is_red_with_reason():
    ws = _proj(
        probes={"greet": {"kind": "cli", "entry": "app.py", "args": ["world"],
                         "stdout_has": "NEVER_APPEARS", "expect_exit": 0}},
        app_src=SAY_HELLO)
    j = judges.full_registry().get("probe.cli")
    r = j.check(judges.JudgeContext(ws=ws, extras={"timeout": 60}))
    assert r.ok is False
    assert "NEVER_APPEARS" in r.detail or "greet" in r.detail, r.detail


def test_no_probes_file_is_red_not_green():
    """核心口径：没有可验收的东西 = 判红，不是判通过。"""
    ws = _proj()
    j = judges.full_registry().get("probe.cli")
    r = j.check(judges.JudgeContext(ws=ws))
    assert r.ok is False, "没有 probes.json 竟然判通过"
    assert r.evidence_ref == "no-probes"


def test_wrong_kind_reports_available_kinds():
    ws = _proj(
        probes={"x": {"kind": "sql", "sql": "select 1", "expect": []}},
        app_src=SAY_HELLO)
    j = judges.full_registry().get("probe.cli")
    r = j.check(judges.JudgeContext(ws=ws))
    assert r.ok is False
    assert "没有 kind=cli" in r.detail or "sql" in r.detail, r.detail


def test_exit_code_failure_detected():
    """业务失败（非 0 退出）要被抓住。"""
    ws = _proj(
        probes={"run": {"kind": "cli", "entry": "app.py", "args": [],
                        "stdout_has": "usage", "expect_exit": 0}},
        app_src=SAY_HELLO)
    j = judges.full_registry().get("probe.cli")
    r = j.check(judges.JudgeContext(ws=ws, extras={"timeout": 60}))
    # 无参数时 app.py 退出码 2，探针期望 0 -> 红
    assert r.ok is False, r.detail
