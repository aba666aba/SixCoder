"""红灯落点不许被公共前置端点污染（2026-11-11）。

背景（毕设项目实测，覆盖停滞的根因）：
  几乎每条 http 能力的探针链第一步都是 `POST /api/v1/auth/register`
  （有的还带 `POST /api/v1/datasets/seed` 造种子数据）—— 那是**公共前置**，
  不是这条能力自己的落点。

  `openhouse.red_modules` 原来把链上**所有**路径都拿去反查落点 →
  56 条红灯里 **32 条的落点集合被 auth 污染**，
  `app/api/auth.py` / `app/authz.py` 落点频次冲到 **33（全场最高）**
  → 引擎每轮把工作单派给**早就契约满分的 auth 模块**
  → 远端重写 → 改坏别的能力 → 回滚 → 空转
  → 实测 8.5 小时净增 **0** 条、状态倒退 **4** 次。

本自检钉住：`/auth/` 段与 `/datasets/seed` 是公共前置，不参与落点反查；
而认证能力**自己**的落点仍然必须是 auth 模块（不许修过头）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as bp  # noqa: E402
from core import openhouse as oh  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    import shutil
    import tempfile

    print("[1] 公共前置端点识别")
    check("注册是公共前置", oh._is_common_prep_path("/api/v1/auth/register"))
    check("登录是公共前置", oh._is_common_prep_path("/api/v1/auth/login"))
    check("me 是公共前置", oh._is_common_prep_path("/api/v1/auth/me"))
    check("种子数据是公共前置", oh._is_common_prep_path("/api/v1/datasets/seed"))
    check("业务端点不是公共前置",
          not oh._is_common_prep_path("/api/v1/experiments/run"))
    check("空路径算公共前置（不许当落点）", oh._is_common_prep_path(""))

    tmp = Path(tempfile.mkdtemp(prefix="six-landing-"))
    try:
        (tmp / "app").mkdir(parents=True, exist_ok=True)
        (tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
        (tmp / "app" / "api").mkdir(parents=True, exist_ok=True)
        (tmp / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
        (tmp / "app" / "api" / "auth.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "@router.post('/api/v1/auth/register')\n"
            "def register():\n"
            "    return {}\n",
            encoding="utf-8")
        (tmp / "app" / "experiments.py").write_text(
            "def run_experiment():\n    return {}\n", encoding="utf-8")
        (tmp / "app" / "api" / "experiments.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "@router.post('/api/v1/experiments/run')\n"
            "def handle_run():\n"
            "    return {}\n",
            encoding="utf-8")

        cap = bp.Cap(
            id="cap.experiment.run", desc="跑实验", funcs_est=1,
            modules=["app/api/experiments.py"],
            probe={"kind": "http", "steps": [
                {"method": "POST", "path": "/api/v1/auth/register",
                 "json": {"username": "u1", "password": "p1", "role": "teacher"},
                 "expect_status": 201},
                {"method": "POST", "path": "/api/v1/experiments/run",
                 "json": {"alg": "da"}, "expect_status": 200},
            ]})
        b = bp.Blueprint(goal="t", caps=[cap])
        cov = {"caps": {"cap.experiment.run": {"status": "absent", "detail": "红"}}}

        print("[2] 落点反查：auth 前置不许污染业务落点")
        mods, detail = oh.red_modules(cov, b, ws=tmp)
        check("业务落点 app/experiments.py 被找到",
              "app/experiments.py" in mods, str(sorted(mods)))
        check("auth 前置**没有**把 app/api/auth.py 拉进落点",
              "app/api/auth.py" not in mods, str(sorted(mods)))
        check("落点数量明显少于链上路径数（没被公共路径撑大）",
              len(mods) <= 3, str(sorted(mods)))

        print("[3] 反向：认证能力自己的落点仍然是 auth（不许修过头）")
        cap2 = bp.Cap(
            id="cap.auth.register", desc="注册", funcs_est=1,
            modules=["app/api/auth.py"],
            probe={"kind": "http", "steps": [
                {"method": "POST", "path": "/api/v1/auth/register",
                 "json": {"username": "u1", "password": "p1", "role": "student"},
                 "expect_status": 201},
            ]})
        b2 = bp.Blueprint(goal="t", caps=[cap2])
        mods2, _ = oh.red_modules(
            {"caps": {"cap.auth.register": {"status": "absent", "detail": "红"}}},
            b2, ws=tmp)
        check("认证能力仍落到 app/api/auth.py（能力表点名的模块保留）",
              "app/api/auth.py" in mods2, str(sorted(mods2)))

        print("[4] 失败详情仍带着能力 id（可追溯）")
        check("detail 里有能力 id",
              any("cap.experiment.run" in s
                  for v in detail.values() for s in v),
              str(detail)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
