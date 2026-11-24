"""CLI 判据包测试（阶段 2）—— 证明判据真的可以脱离 Web。

核心要证明的：
  1. 一个能跑的 CLI 项目，用 CLI 判据能判绿；
  2. 一个坏掉的 CLI 项目，能判红且**原因具体**；
  3. 没有配置时，判据明确说"没配"，**不假装通过**；
  4. HTTP 判据不会跑到 CLI 项目上（固化问题的反面）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import judges as J          # noqa: E402
from core import judges_cli as JC     # noqa: E402


def _proj(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="cli-judge-"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def _ctx(ws: Path) -> J.JudgeContext:
    return J.JudgeContext(ws=ws)


GOOD_CLI = {
    "main.py": (
        "import argparse, sys\n"
        "p = argparse.ArgumentParser(description='演示工具')\n"
        "p.add_argument('--out')\n"
        "a = p.parse_args()\n"
        "if a.out:\n"
        "    open(a.out, 'w').write('done')\n"
        "print('用法: main.py [--out FILE]')\n"
    ),
    # 注意：不要用 --help 做冒烟参数 —— argparse 会直接 exit(0)，
    # 后面的 print 根本不执行。用空参数让程序走正常路径。
    ".sixcoder/cli.json": json.dumps({
        "run": "python main.py",
        "smoke_args": "",
        "expect_stdout": ["用法"],
        "artifacts": [],
        "timeout": 30,
    }, ensure_ascii=False),
}


def test_good_cli_passes():
    ws = _proj(GOOD_CLI)
    r = JC.judge_exit_code(_ctx(ws))
    assert r.ok is True, r.detail
    assert r.gate == "cli.exit_code"


def test_broken_cli_fails_with_reason():
    ws = _proj({
        "main.py": "raise SystemExit(3)\n",
        ".sixcoder/cli.json": json.dumps({"run": "python main.py", "timeout": 30}),
    })
    r = JC.judge_exit_code(_ctx(ws))
    assert r.ok is False
    assert "3" in r.evidence_ref or "3" in r.detail


def test_syntax_error_cli_fails():
    ws = _proj({
        "main.py": "def broken(:\n",
        ".sixcoder/cli.json": json.dumps({"run": "python main.py", "timeout": 30}),
    })
    r = JC.judge_exit_code(_ctx(ws))
    assert r.ok is False
    assert r.evidence_ref != "0"


def test_stdout_expectation_missing_fails():
    ws = _proj({
        "main.py": "print('hello')\n",
        ".sixcoder/cli.json": json.dumps({
            "run": "python main.py", "expect_stdout": ["永远不会出现"], "timeout": 30}),
    })
    r = JC.judge_stdout(_ctx(ws))
    assert r.ok is False
    assert "永远不会出现" in r.detail


def test_artifact_missing_fails():
    ws = _proj({
        "main.py": "print('ok')\n",
        ".sixcoder/cli.json": json.dumps({
            "run": "python main.py", "artifacts": ["dist/result.txt"], "timeout": 30}),
    })
    r = JC.judge_artifact(_ctx(ws))
    assert r.ok is False
    assert "dist/result.txt" in r.detail


def test_artifact_created_passes():
    ws = _proj({
        "main.py": (
            "import os\n"
            "os.makedirs('dist', exist_ok=True)\n"
            "open('dist/result.txt','w').write('x')\n"
        ),
        ".sixcoder/cli.json": json.dumps({
            "run": "python main.py", "artifacts": ["dist/result.txt"], "timeout": 30}),
    })
    r = JC.judge_artifact(_ctx(ws))
    assert r.ok is True, r.detail


def test_empty_artifact_fails():
    ws = _proj({
        "main.py": "import os; os.makedirs('dist', exist_ok=True); open('dist/e.txt','w').close()\n",
        ".sixcoder/cli.json": json.dumps({
            "run": "python main.py", "artifacts": ["dist/e.txt"], "timeout": 30}),
    })
    r = JC.judge_artifact(_ctx(ws))
    assert r.ok is False
    assert "空" in r.detail


def test_no_config_is_not_silent_pass():
    """没配置必须明说"没配"，而不是算通过 —— props.py:414 的教训。"""
    ws = _proj({"main.py": "print(1)\n"})
    r = JC.judge_exit_code(_ctx(ws))
    assert r.ok is False, "没配运行命令不该判通过"
    assert "没配" in r.detail


def test_no_expectation_is_explicit_skip():
    """没有期望输出时，状态要说清是"跳过"，不能伪装成"验证通过"。

    注意夹具要单独构造 —— GOOD_CLI 本身配了 expect_stdout，
    直接拿它会走"有一条期望"的分支，测不到跳过路径。
    """
    ws = _proj({
        "main.py": "print('hello')\n",
        ".sixcoder/cli.json": json.dumps({"run": "python main.py", "timeout": 30}),
    })
    r = JC.judge_stdout(_ctx(ws))
    assert r.ok is True
    assert r.evidence_ref == "no-expectation"
    assert "跳过" in r.detail


def test_http_judges_do_not_apply_to_cli():
    """固化问题的正面反例：HTTP 判据绝不能出现在 CLI 栈上。"""
    reg = J.build_default_registry()
    JC.register_cli(reg)
    cli_keys = {j.key for j in reg.for_stack(J.STACK_CLI)}
    for http_only in ("api", "db_rows", "security", "field_map"):
        assert http_only not in cli_keys, f"{http_only} 不该在 CLI 栈上"
    for cli_key in JC.CLI_JUDGES:
        assert cli_key in cli_keys, f"{cli_key} 应该在 CLI 栈上"


def test_cli_judges_do_not_apply_to_fastapi():
    """反向也成立：CLI 判据不该跑到 Web 项目上。"""
    reg = J.build_default_registry()
    JC.register_cli(reg)
    web_keys = {j.key for j in reg.for_stack(J.STACK_FASTAPI)}
    for cli_key in JC.CLI_JUDGES:
        assert cli_key not in web_keys, f"{cli_key} 不该跑到 fastapi 栈"


def test_full_registry_runs_cli_end_to_end():
    """端到端：拿 CLI 判据全套跑一个真项目，结论必须是"通过 + 有证据"。"""
    ws = _proj(GOOD_CLI)
    reg = J.build_default_registry()
    JC.register_cli(reg)
    ctx = _ctx(ws)

    results = []
    for judge in reg.for_stack(J.STACK_CLI):
        if judge.key.startswith("cli."):
            results.append((judge.key, judge.check(ctx)))

    assert results, "CLI 栈上一个 cli.* 判据都没跑"
    for key, res in results:
        assert res.ok is True, f"{key} 失败：{res.detail}"
        assert res.evidence_kind, f"{key} 没有证据类型"

def test_help_flag_exits_before_custom_prints():
    """钉住一个真实陷阱：--help 会让 argparse 提前 exit(0)，
    后面的代码不执行。所以冒烟参数不能假定"会跑到业务逻辑"。

    这不是判据的 bug，是配置要注意的事 —— 但必须记录下来，
    否则以后有人把 smoke_args 设成 --help，会得到"退出码 0 但输出没东西"的困惑。
    """
    ws = _proj({
        "main.py": (
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.parse_args()\n"
            "print('这行在 --help 下不会执行')\n"
        ),
        ".sixcoder/cli.json": json.dumps({
            "run": "python main.py", "smoke_args": "--help",
            "expect_stdout": ["这行在 --help 下不会执行"], "timeout": 30}),
    })
    # 退出码是 0（help 正常退出）
    assert JC.judge_exit_code(_ctx(ws)).ok is True
    # 但期望的文本确实没出现 -> 判红，这恰恰说明判据是有效的
    r = JC.judge_stdout(_ctx(ws))
    assert r.ok is False