"""探针超时必须真的超时：孙进程持有 stdout 时也不许挂死。

背景（2026-11-10 实测事故）：
    夜班 18:01 起跑端到端探针，18:05 到超时，之后**零输出零花费挂了 13 分钟**。
    py-spy 栈停在 `_http_exec → subprocess.run → Popen._communicate → Thread.join`。
    原因：`subprocess.run(capture_output=True, timeout=...)` 到期只 kill 直接子进程，
    而子进程起的孙进程（uvicorn/worker/后台线程）**继承了 stdout 管道** →
    communicate() 要等所有写端关闭 → 孙进程活着就永远等下去。

修法：`core/probe._run_child` 把输出写**文件**（不用管道）+ 超时杀**整棵树**。
本自检就是钉住这两点：真的会超时返回、真的不挂死。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import probe as P                                    # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED.append(name)
    return bool(cond)


# 一个"子进程起孙进程、孙进程继承 stdout 并长睡"的剧本：
# 子进程自己还挂着不退 → 触发超时，且孙进程持有写端（老写法正死在这里）。
HANG = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print('child-started', flush=True)\n"
    "time.sleep(120)\n"
)
# 父进程打印完就退出，但孙进程继续持有 stdout（老写法会等到孙进程结束）
EXIT_EARLY = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print('done-early', flush=True)\n"
)


def main() -> int:
    print("[1] 超时 + 孙进程持有 stdout → 必须按时返回，不许挂死")
    t0 = time.time()
    r = P._run_child([sys.executable, "-c", HANG], cwd=".", timeout=5)
    took = time.time() - t0
    check("返回了（没挂死）", isinstance(r, dict) and "timed_out" in r)
    check("判定为超时", r.get("timed_out") is True, str(r.get("timed_out")))
    # 5 秒超时 + 10 秒杀树兜底 = 正常在 15 秒内；老写法会永远不返回
    check(f"在 20 秒内返回（实测 {took:.1f}s）", took < 20.0, f"{took:.1f}s")
    check("超时前已写下的输出没丢", "child-started" in (r.get("out") or ""),
          repr((r.get("out") or "")[-40:]))

    print("[2] 子进程正常退出、孙进程还活着 → 不许等到孙进程结束")
    t0 = time.time()
    r2 = P._run_child([sys.executable, "-c", EXIT_EARLY], cwd=".", timeout=60)
    took2 = time.time() - t0
    check(f"立刻返回（实测 {took2:.1f}s < 20s）", took2 < 20.0, f"{took2:.1f}s")
    check("退出码是 0", int(r2.get("code") or 0) == 0, str(r2.get("code")))
    check("输出完整", "done-early" in (r2.get("out") or ""), repr((r2.get("out") or "")[-40:]))

    print("[3] 源码层钉子：探针里不许再用管道抓输出")
    import ast
    src = (Path(__file__).resolve().parents[1] / "core" / "probe.py").read_text(encoding="utf-8")
    # 只看真正传参的代码（注释里那段"为什么不用 capture_output"不能算）
    bad: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords or []:
            if kw.arg == "capture_output":
                bad.append(f"line {getattr(kw.value, 'lineno', '?')}: capture_output")
            elif kw.arg in ("stdout", "stderr") and isinstance(kw.value, ast.Attribute) \
                    and kw.value.attr == "PIPE":
                bad.append(f"line {getattr(kw.value, 'lineno', '?')}: {kw.arg}=PIPE")
    check("没有任何子进程调用用管道抓输出", not bad, "；".join(bad[:3]))
    check("统一走 _run_child", src.count("_run_child(") >= 5, str(src.count("_run_child(")))
    check("杀树用的是 taskkill /T", "/T" in src and "taskkill" in src)

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)} 项：" + "；".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
