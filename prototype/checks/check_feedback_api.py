"""自检：/v1/night/feedback 接口（运行中提修改意见，2026-11-24）。

钉住四件事：
  1. 两个路由真的挂在 app 上（不是只写了函数没注册）；
  2. POST 能写入、GET 能读出，两边数据一致；
  3. 非法输入返回 400（不许静默降级成 global）；
  4. 没在跑的仓库，返回文案要说清"下次开跑才生效"（不能让人误以为立即生效）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def ck(name: str, cond: bool, extra: str = "") -> None:
    if not cond:
        FAILS.append(name + (": " + extra if extra else ""))


def main() -> int:
    from fastapi.testclient import TestClient

    import server as S

    paths = {getattr(r, "path", "") for r in S.app.routes}
    ck("POST /v1/night/feedback 已挂载", "/v1/night/feedback" in paths)
    ck("GET  /v1/night/feedback 已挂载", "/v1/night/feedback" in paths)

    c = TestClient(S.app)
    ws = Path(tempfile.mkdtemp(prefix="fbr-"))

    # --- 1) 空仓库查询：不该 500 ---
    r = c.get("/v1/night/feedback", params={"repo": str(ws)})
    ck("空仓库 GET 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        d = r.json()
        ck("空仓库 total=0", d.get("total") == 0, str(d))
        ck("空仓库 running=False", d.get("running") is False, str(d))

    # --- 2) 写一条 global ---
    r = c.post("/v1/night/feedback",
               json={"repo": str(ws), "text": "整体别用 React", "scope": "global"})
    ck("POST global 200", r.status_code == 200, str(r.status_code) + " " + r.text[:200])
    if r.status_code == 200:
        d = r.json()
        ck("返回 ok", d.get("ok") is True)
        ck("返回 id", bool(d.get("id")))
        ck("没在跑时文案说明下次生效",
           "下次开跑" in (d.get("note") or ""), str(d.get("note")))

    # --- 3) 写一条 forbid + 一条 module ---
    c.post("/v1/night/feedback",
           json={"repo": str(ws), "text": "不许引入新依赖", "scope": "forbid"})
    r = c.post("/v1/night/feedback",
               json={"repo": str(ws), "text": "登录改 JWT", "scope": "module",
                     "target": "app/user.py"})
    ck("POST module 200", r.status_code == 200, str(r.status_code))

    # --- 4) GET 能读到全部 ---
    r = c.get("/v1/night/feedback", params={"repo": str(ws)})
    d = r.json()
    ck("total=3", d.get("total") == 3, str(d.get("total")))
    ck("pending=3", d.get("pending") == 3, str(d.get("pending")))
    ck("rules 含 global", d["rules"]["global"] == ["整体别用 React"], str(d["rules"]))
    ck("rules 含 forbid", d["rules"]["forbid"] == ["不许引入新依赖"], str(d["rules"]))

    # --- 5) 非法输入必须 400（不许静默降级）---
    for bad, why in (
        ({"repo": str(ws), "text": "", "scope": "global"}, "空文本"),
        ({"repo": str(ws), "text": "x", "scope": "nonsense"}, "非法 scope"),
        ({"repo": str(ws), "text": "x", "scope": "module"}, "module 缺 target"),
    ):
        r = c.post("/v1/night/feedback", json=bad)
        ck("非法输入被拒：" + why, r.status_code == 400, f"{why} -> {r.status_code}")

    # --- 6) 不存在的仓库 → 404 ---
    r = c.get("/v1/night/feedback", params={"repo": str(ws / "nope")})
    ck("不存在仓库 404", r.status_code == 404, str(r.status_code))

    if FAILS:
        print("FAILS:")
        for x in FAILS:
            print("  - " + x)
        return 1
    print("OK /v1/night/feedback 接口全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
