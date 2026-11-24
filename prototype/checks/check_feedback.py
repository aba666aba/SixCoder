"""自检：core/feedback.py（运行中提修改意见）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import feedback as FB   # noqa: E402

FAILS: list[str] = []


def ck(name: str, cond: bool, extra: str = "") -> None:
    if not cond:
        FAILS.append(name + (": " + extra if extra else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fb-"))

    # 1) 空仓库：没有任何意见，不该抛
    ck("空仓库 pending 为空", FB.pending(tmp) == [])
    ck("空仓库 rules 为空", FB.rules(tmp) == {"global": [], "forbid": []})
    ck("空仓库 render 为空串", FB.render_for_prompt(tmp) == "")

    # 2) 追加一条 global
    a = FB.append(tmp, "整体别用 React", scope="global")
    ck("global 有 id", bool(a.id))
    ck("global 进 pending", len(FB.pending(tmp)) == 1)
    ck("global 进 rules", FB.rules(tmp)["global"] == ["整体别用 React"])

    # 3) 追加 forbid
    FB.append(tmp, "不许引入新依赖", scope="forbid")
    r = FB.rules(tmp)
    ck("forbid 进 rules", r["forbid"] == ["不许引入新依赖"])
    ck("forbid 不混进 global", r["global"] == ["整体别用 React"])

    # 4) module 粒度
    m = FB.append(tmp, "登录改 JWT", scope="module", target="app/user.py")
    ck("module 进 module_pending", len(FB.module_pending(tmp)) == 1)
    ck("module target 归一", FB.module_pending(tmp)[0].target == "app/user.py")

    # 5) module 消费后消失，global/forbid 不受影响
    n = FB.consume(tmp, [m.id], rnd=3)
    ck("消费 1 条", n == 1, f"实际 {n}")
    ck("消费后 module_pending 空", FB.module_pending(tmp) == [])
    ck("消费后 global 仍在", FB.rules(tmp)["global"] == ["整体别用 React"])
    ck("消费后 forbid 仍在", FB.rules(tmp)["forbid"] == ["不许引入新依赖"])

    # 6) global/forbid 不可被消费（它们是长期规矩）
    all_ids = [f.id for f in FB.load(tmp)]
    FB.consume(tmp, all_ids, rnd=4)
    ck("global 不可被消费", FB.rules(tmp)["global"] == ["整体别用 React"])
    ck("forbid 不可被消费", FB.rules(tmp)["forbid"] == ["不许引入新依赖"])

    # 7) 去重：同一条 global 追加两次，rules 里只出现一次
    tmp2 = Path(tempfile.mkdtemp(prefix="fb2-"))
    FB.append(tmp2, "用 Vue", scope="global")
    FB.append(tmp2, "用 Vue", scope="global")
    ck("rules 去重", FB.rules(tmp2)["global"] == ["用 Vue"])

    # 8) 非法输入要报错，不静默降级
    for bad, kw in (
        ("", {}),
        ("内容", {"scope": "nonsense"}),
        ("内容", {"scope": "module"}),           # 缺 target
    ):
        try:
            FB.append(tmp2, bad, **kw)
        except ValueError:
            pass
        else:
            FAILS.append(f"非法输入没报错: {bad!r} {kw}")

    # 9) 坏行不影响其它意见
    tmp3 = Path(tempfile.mkdtemp(prefix="fb3-"))
    FB.append(tmp3, "好的一条", scope="global")
    with (tmp3 / ".sixcoder" / "feedback.jsonl").open("a", encoding="utf-8") as f:
        f.write("{ 这不是合法JSON\n")
        f.write('{"broken": true}\n')    # 缺 text
    ck("坏行被跳过", len(FB.load(tmp3)) == 1)

    # 10) render 内容正确
    txt = FB.render_for_prompt(tmp)
    ck("render 含红线段", "红线" in txt)
    ck("render 含全局段", "全局调整" in txt)
    ck("render 含 forbid 原文", "不许引入新依赖" in txt)

    # 11) stats
    s = FB.stats(tmp)
    ck("stats total", s["total"] == 3, str(s))
    ck("stats by_scope", s["by_scope"]["forbid"] == 1, str(s))

    if FAILS:
        print("FAILS:")
        for x in FAILS:
            print("  - " + x)
        return 1
    print("OK 全部通过（21 项断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
