"""巡检代理自检：缓存命中率趋势能不能抓下滑、文档腐烂能不能抓出来。

跑法: python check_doctor.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import doctor  # noqa: E402

FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _run(task: str, hit: int, prompt: int) -> dict:
    return {"task": task, "hit": hit, "prompt": prompt}


def main() -> int:
    print("巡检代理自检")

    print("[1] 命中率趋势：数据不足不谈趋势")
    t = doctor.hit_trend([_run("a", 50, 100)])
    check("1 次运行 → 不谈趋势", not t["enough"], str(t))
    check("说明里写清为什么", "只有" in t.get("note", ""), t.get("note"))

    print("[2] 命中率趋势：稳定 → 不报警")
    stable = [_run(f"t{i}", 60, 100) for i in range(5)]
    t = doctor.hit_trend(stable)
    check("够 4 次的判据", t["enough"])
    check("中位数 60%", abs(t["median"] - 0.6) < 1e-6, str(t["median"]))
    check("稳定 → 不判下滑", not t["regressed"], str(t["drop"]))

    print("[3] 命中率趋势：掉了 15 个百分点以上 → 报警")
    dropped = [_run("t0", 70, 100), _run("t1", 70, 100), _run("t2", 70, 100),
               _run("t3", 70, 100), _run("t4", 20, 100)]
    t2 = doctor.hit_trend(dropped)
    check("判为下滑", t2["regressed"], f"drop={t2['drop']}")
    check("给出下滑幅度", t2["drop"] >= doctor.CACHE_DROP_ALERT, str(t2["drop"]))
    check("差 10 个点不算下滑（阈值没写死成 0）",
          not doctor.hit_trend([_run("a", 70, 100), _run("b", 70, 100),
                                _run("c", 70, 100), _run("d", 60, 100)])["regressed"])

    print("[4] cache_doctor：报警时带修法（不是只报数字）")
    fs = doctor.cache_doctor(Path("."), dropped)
    check("有 warn 级发现", any(f.level == "warn" for f in fs), str([f.level for f in fs]))
    w = [f for f in fs if f.level == "warn"][0]
    check("说清了差多少", "百分点" in w.what, w.what)
    check("给了修法（移出易变内容）", "稳定前缀" in w.fix or "易变" in w.fix, w.fix)
    check("带证据（哪一次）", bool(w.evidence), w.evidence)

    print("[4b] token 加权口径（2026-11-19：按次/按任务会被小调用绑架，测的是影子）")
    # 真实形态：小调用（6 千 token、命中 2%）+ 大调用（17 万 token、命中 70%）
    mixed = [_run("handoff", 128, 6130), _run("handoff", 128, 6130),
             _run("whole_repo", 124000, 178000), _run("whole_repo", 130000, 178000)]
    wt = doctor.weighted_trend(mixed)
    check("加权不被小调用绑架（应≈69%，不是 2%）",
          wt["enough"] and wt["overall"] > 0.6, str(wt))
    check("大调用占比高的窗口不判下滑（drop 为负不能报警）",
          not wt["regressed"] and wt["drop"] < 0, str(wt))
    t2_runs = [_run("big", 120000, 178000)] * 3 + [_run("big", 20000, 178000)]
    wt2 = doctor.weighted_trend(t2_runs)
    check("大调用真掉档 → 判下滑（25 个百分点）",
          wt2["regressed"] and wt2["drop"] >= doctor.CACHE_DROP_ALERT, str(wt2))
    fs2 = doctor.cache_doctor(Path("."), t2_runs)
    w2 = [f for f in fs2 if f.level == "warn"]
    check("报警文案写明口径是 token 加权（不让人误读成按次）",
          bool(w2) and "加权" in w2[0].what, w2[0].what if w2 else "")
    check("正常文案也写明口径 + 前后半窗",
          "加权" in doctor.cache_doctor(Path("."), mixed)[0].what,
          doctor.cache_doctor(Path("."), mixed)[0].what)

    print("[5] doc_doctor：抓超长指南 / 失效引用 / 目录过期 / TODO")
    with _sandbox_temp() as td:
        ws = Path(td)
        gdir = ws / "docs" / "guides"
        gdir.mkdir(parents=True)
        (gdir / "big.md").write_text("\n".join(["x"] * 350), encoding="utf-8")
        (ws / "AGENTS.md").write_text(
            "# AGENTS\n\n- `pkg` — 模块目录\n\n见 `core/missing_file.py`\n\nTODO 补文档\n",
            encoding="utf-8")
        fs = doctor.doc_doctor(ws, "M")
        what = " | ".join(f.what for f in fs)
        check("抓到超长指南", "过长" in what, what[:120])
        check("抓到失效引用", "不存在的文件" in what, what[:120])
        check("抓到目录过期", "已不存在" in what, what[:120])
        check("抓到 TODO", "TODO" in what, what[:120])
        check("每条 warn 都有修法", all(f.fix for f in fs if f.level == "warn"),
              str([f.what for f in fs if f.level == "warn" and not f.fix]))

    print("[6] doc_doctor：S 级跳过；健康工作区不误报")
    with _sandbox_temp() as td:
        ws = Path(td)
        fs = doctor.doc_doctor(ws, "S")
        check("S 级跳过（不做 docs 知识库）", any("跳过" in f.what for f in fs), str(fs))
        (ws / "AGENTS.md").write_text("# AGENTS\n\n- `.` — 全部产物\n", encoding="utf-8")
        fs2 = doctor.doc_doctor(ws, "M")
        check("健康工作区没有 warn", not [f for f in fs2 if f.level in ("warn", "error")],
              str([f.what for f in fs2]))

    print("[7] 统一入口与报告")
    res = doctor.run(Path("."), "S", [_run("a", 70, 100), _run("b", 70, 100),
                                      _run("c", 70, 100), _run("d", 20, 100)])
    check("run 返回 findings/warn/ok", {"findings", "warn", "ok"} <= set(res), str(res.keys()))
    lines = doctor.report_lines(res)
    txt = "\n".join(lines)
    check("报告小节有标题", "定期巡检" in txt, txt[:80])
    check("报告里带修法箭头", "→" in txt or "改" in txt, txt[:160])

    print("[8] 已接进夜班与 CLI")
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("开跑巡检并发事件", "_doctor.run(ws" in comp and '"doctor"' in comp)
    check("交付报告含巡检小节", "_doctor2.report_lines" in comp)
    cli = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
    check("CLI 有 inspect 子命令", "cmd_inspect_docs" in cli and 'add_parser("inspect"' in cli)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（趋势判据 / 报警带修法 / 文档腐败四类都抓得住 / 接入夜班与 CLI）")
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
