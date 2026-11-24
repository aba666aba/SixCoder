"""精修模式文件 API 自检（服务端能力，2026-11-20）。

钉住四条底线：
  1. **路径牢笼**：界面能改的只能是仓库内的文件（`../` 与绝对路径一律拒）；
  2. **原文**：`/v1/fs/read` 返回的是原文件内容，**不带** `"  12| "` 这种给模型看的行号前缀；
  3. **写盘有痕迹**：保存走 `tools.write_file` → 沙箱 journal 里留一条备份记录；
  4. **树不淹没**：node_modules / .git / __pycache__ 不进编辑器树。

跑法: python checks/check_fs_api.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:260]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_fs", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()
    ws = Path(tempfile.mkdtemp(prefix="fsapi-"))
    try:
        (ws / "src").mkdir()
        (ws / "src" / "a.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
        (ws / "README.md").write_text("# demo\n", encoding="utf-8")
        for noisy in ("node_modules/pkg", ".git/objects", "__pycache__"):
            d = ws / noisy
            d.mkdir(parents=True, exist_ok=True)
            (d / "junk.txt").write_text("junk", encoding="utf-8")

        print("[1] 文件树：跳过噪音目录")
        t = srv.fs_tree(str(ws))
        paths = [x["path"] for x in t["items"]]
        ck("列出真实文件", "src/a.py" in paths and "README.md" in paths, str(paths[:8]))
        ck("node_modules 不进树", not any(p.startswith("node_modules") for p in paths))
        ck(".git 不进树", not any(p.startswith(".git") for p in paths))
        ck("__pycache__ 不进树", not any(p.startswith("__pycache__") for p in paths))
        ck("目录项标了 dir=True",
           any(x["dir"] and x["path"] == "src" for x in t["items"]), str([x for x in t["items"] if x["dir"]]))

        print("[2] 读取：原文，不带行号前缀")
        r = srv.fs_read(str(ws), "src/a.py")
        ck("内容与磁盘一致", r["text"] == "alpha = 1\nbeta = 2\n", repr(r["text"])[:120])
        ck("没有给模型看的 `  1| ` 前缀", "| alpha" not in r["text"], repr(r["text"])[:120])
        ck("行数正确", r["lines"] == 2, str(r["lines"]))
        ck("不是二进制", r["binary"] is False)

        print("[3] 路径牢笼：越界一律拒")
        for bad in ("../evil.py", "..\\evil.py", "C:/Windows/win.ini"):
            try:
                srv.fs_read(str(ws), bad)
                ck(f"读取越界被拒 {bad!r}", False, "没拦住")
            except Exception as exc:                      # noqa: BLE001
                ck(f"读取越界被拒 {bad!r}", "越界" in str(exc) or "不存在" in str(exc), str(exc)[:90])
        try:
            srv.fs_write(srv.FsWriteReq(repo=str(ws), path="../evil.py", content="x"))
            ck("写入越界被拒", False, "没拦住")
        except Exception as exc:                          # noqa: BLE001
            ck("写入越界被拒", True, str(exc)[:80])

        print("[4] 写入：真落盘 + 沙箱 journal 留痕")
        w = srv.fs_write(srv.FsWriteReq(repo=str(ws), path="src/a.py", content="alpha = 9\n"))
        ck("接口返回 ok", w.get("ok") is True and w.get("bytes") == 10, str(w))
        ck("磁盘内容已更新",
           (ws / "src" / "a.py").read_text(encoding="utf-8") == "alpha = 9\n")
        from core import sandbox as _sb
        j = _sb.Sandbox(ws).journal()
        ck("沙箱 journal 里有这次写的备份记录",
           any(x.get("rel") == "src/a.py" for x in j), str(j[-2:])[:200])

        print("[5] 搜索：命中文件+行号")
        s = srv.fs_search(str(ws), "alpha")
        ck("搜到 alpha", bool(s["hits"]) and s["hits"][0]["path"] == "src/a.py", str(s["hits"])[:160])
        ck("带行号", s["hits"][0]["line"] == 1, str(s["hits"][0]))
        s2 = srv.fs_search(str(ws), "junk")
        ck("噪音目录里的内容搜不到（与树一致）", not s2["hits"], str(s2["hits"])[:120])
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("ALL PASS")
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
