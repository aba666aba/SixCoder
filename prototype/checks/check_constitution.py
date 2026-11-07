"""项目宪法自检：版本化稳定前缀（开发文档 §18.7 第 5 件）。

这条钉的是"33,000 个函数长成一个风格"与"前缀缓存 50 倍成本差"两件事：
    · 幂等：ensure 只写一次，已存在绝不覆盖（改约定是升版本，不是原地改写）；
    · 字节稳定：prefix() 连续两次返回**逐字节相同**（否则前缀缓存失效）；
    · 无易变内容：宪法里不许出现日期/时间/任务 id/UUID（否则每一轮前缀都不同）；
    · 注入到位：作者提示词把宪法放最前（比记忆更稳定），夜班入口先落盘；
    · 治理可锁定：sha 稳定，被改/被删能发现。

跑法: python checks/check_constitution.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import constitution as con  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("项目宪法自检（幂等 / 字节稳定 / 无易变内容 / 注入到位 / 治理锁定）")

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)

        print("[1] 幂等：只写一次，绝不覆盖")
        r1 = con.ensure(ws)
        p = Path(ws) / con.CONSTITUTION_FILE
        check("第一次创建", r1["created"] is True and p.exists(), str(r1))
        text1 = p.read_text(encoding="utf-8")
        r2 = con.ensure(ws)
        check("第二次不覆盖（created=False）", r2["created"] is False, str(r2))
        check("内容一字未动", p.read_text(encoding="utf-8") == text1)
        # 故意改文件 → ensure 仍然不覆盖（尊重已有版本，改约定要升版本号）
        p.write_text("# 被改过的宪法\n", encoding="utf-8")
        con.ensure(ws)
        check("已有文件被改过，ensure 也不覆盖", p.read_text(encoding="utf-8") == "# 被改过的宪法\n")

        print("[2] 字节稳定：prefix 连续两次逐字节相同（前缀缓存的前提）")
        a = con.prefix(ws)
        b = con.prefix(ws)
        check("两次 prefix 逐字节相同", a == b, f"len {len(a)} vs {len(b)}")

        print("[3] 无易变内容：不许出现日期/任务 id/UUID")
        hits = con.volatile_hits(ws)
        check("没有易变内容（日期/任务id/UUID）", not hits, str(hits))

        print("[4] 六项约定齐全：目录/命名/分层/错误/测试/铁律")
        txt = con.default_text()
        for sec in ("目录", "命名", "分层", "错误处理", "测试", "铁律"):
            check(f"含「{sec}」约定", f"## {sec if sec != '错误处理' else '4. 错误处理'}" in txt
                  or sec in txt, sec)
        check("含'不硬编码密钥'铁律", "不硬编码密钥" in txt)
        check("含'禁止裸 except 吞异常'", "裸 except" in txt)

        print("[5] 治理锁定：sha 稳定，改了能发现")
        s1 = con.sha(ws)
        check("sha 长度 16 位", len(s1) == 16, s1)
        p.write_text("# 被改过的宪法 v2\n", encoding="utf-8")
        check("内容变了 sha 跟着变", con.sha(ws) != s1)

    print("[6] 注入到位：作者提示词最前 + 夜班入口落盘")
    root = Path(__file__).resolve().parents[1]
    author_src = (root / "core" / "author.py").read_text(encoding="utf-8", errors="replace")
    check("author.py 把宪法注入稳定前缀（在记忆之前）",
          "constitution as _const" in author_src and "_const_prefix" in author_src)
    auto_src = (root / "core" / "auto.py").read_text(encoding="utf-8", errors="replace")
    company_src = (root / "core" / "company.py").read_text(encoding="utf-8", errors="replace")
    check("夜班入口（auto.night_auto + company._night_inner）都 ensure",
          "constitution as _const0" in auto_src and "constitution as _const" in company_src)

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（幂等 / 字节稳定 / 无易变内容 / 六项约定 / 治理锁定 / 注入到位）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
