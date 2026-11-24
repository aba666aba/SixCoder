"""多语言夜班真跑：每种语言一个真实任务，看识别、验证、报告是否诚实。

跑法：python tools/lang_run.py [--only java]
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company, lang, remote  # noqa: E402

WS = Path("D:/Download/cal")
OUT = PROTO.parent / "_logs" / "lang"

TASKS = {
    "java": """1) Calc.java：
   - public class Calc：add(int a, int b) -> int：两数相加
   - 必须完整可编译：包含 class Calc 与 add 方法（public static）
""",
    "c": """1) ringbuf.c：
   - int ringbuf_push(int *buf, int cap, int *len, int val)：环形缓冲入队，满了丢最旧
   - 必须完整可编译：包含 #include <stddef.h> 与 ringbuf_push 的定义
""",
    "cpp": """1) stats.cpp：
   - double mean_sum(double *xs, int n)：返回元素平均值（n 为 0 时返回 0.0）
   - 必须完整可编译：包含 <vector> 或 <cstddef> 与 mean_sum 的定义
""",
    "javascript": """1) util.js：
   - const add = (a, b) => a + b
   - 必须完整可运行：用 module.exports 导出 add
""",
    "typescript": """1) util.ts：
   - export function add(a: number, b: number): number：两数相加
   - 必须完整可运行
""",
    "go": """1) main.go：
   - func Add(a int, b int) int：两数相加
""",
}


def reset(ws: Path) -> None:
    if not ws.exists():
        ws.mkdir(parents=True)
        return
    for p in list(ws.iterdir()):
        if p.name == ".gitignore":
            continue
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    keys = [a.only] if a.only else list(TASKS)
    total = 0.0
    for name in keys:
        req = TASKS[name]
        print("=" * 60)
        print(f"=== [{name}] {req.splitlines()[0]}")
        reset(WS)
        before = remote.usage_summary("")
        t0 = time.time()
        evp = OUT / f"{name}.jsonl"
        evp.write_text("", encoding="utf-8")

        def emit(kind, payload, _f=evp):
            with _f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": kind, "payload": payload},
                                    ensure_ascii=False, default=str) + "\n")

        res = company.night(WS, req, "qwen2.5-coder:7b", use_remote=True,
                            emit=emit)
        after = remote.usage_summary("")
        cost = round(float(after["cost_cny"]) - float(before["cost_cny"]), 4)
        total += cost
        print(f"  用时 {time.time() - t0:.1f}s｜¥{cost}｜累计 ¥{round(total, 4)}")
        print(f"  结果：{res.reason[:220]}")
        files = sorted(p.name for p in WS.iterdir() if p.is_file())
        print(f"  产出文件：{files}")
        for f in files:
            if f.endswith((".java", ".c", ".cpp", ".js", ".ts", ".go")):
                info = lang.detect("", [f])
                v = lang.verify(WS, f, info["lang"]) if info["ok"] else {"ok": None,
                                                                        "detail": info["why"]}
                print(f"    {f}: {info['lang']} 独立复核 ok={v.get('ok')} "
                      f"{str(v.get('detail'))[:100]}")
    print(f"\n合计 ¥{round(total, 4)}｜证据 {OUT}")
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
