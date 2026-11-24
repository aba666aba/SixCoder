"""闸门行为快照测试（阶段 0 安全网）。

把 tools/gate_snapshot.py 的 --verify 接进 pytest，
让"行为漂移"在 CI 里就是一个红叉，而不是靠人记得手动跑。

**重构期间这条必须一直绿。**
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "prototype"
sys.path.insert(0, str(PROTO))

import json  # noqa: E402


def test_gate_snapshot_matches_baseline():
    """所有闸门的行为必须与基线逐字段一致。"""
    sys.path.insert(0, str(PROTO / "tools"))
    import gate_snapshot as GS  # noqa: E402

    assert GS.SNAPSHOT.exists(), f"基线缺失：{GS.SNAPSHOT}，先跑 --record"

    base = json.loads(GS.SNAPSHOT.read_text(encoding="utf-8"))
    now = GS.collect()

    missing = sorted(set(base) - set(now))
    assert not missing, f"基线里有、现在没有的闸门：{missing}"

    added = sorted(set(now) - set(base))
    assert not added, (
        f"新增了闸门 {added} 但没有更新基线 —— "
        "如果是有意的，重新 --record 并单独提交这一次基线变更"
    )

    diffs = [k for k in base if base[k] != now.get(k)]
    assert not diffs, (
        "以下闸门行为发生变化（重构期间应为 0）：\n  " + "\n  ".join(diffs) +
        "\n\n如果是有意改动，请单独提交并说明原因。"
    )
