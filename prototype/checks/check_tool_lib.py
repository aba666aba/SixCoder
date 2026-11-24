"""自主工具库自检（2026-11-19 用户需求：远端协商认定缺工具 → 引擎规范化装进本地库）。

钉住的铁律，每条都是踩过的坑：
  1. **纯英文小写目录名**：中文/空格/`..`/绝对路径一律拒（防路径穿越与乱码目录）。
  2. **复验是裁判**：远端说装好了不算，跑 manifest.verify 且 rc==0 才算装成。
  3. **失败必回滚**：复验失败/写盘异常 → 不留半装目录，不覆盖掉好的旧版。
  4. **原子落盘 + 回读比对**：写临时目录 → 回读指纹一致 → 才原子改名。
  5. **verify 白名单**：pi p/npm install、curl/wget 这类安装下载命令必须拒
     （沙箱本身也禁，工具是"代码+自检"，不是下载器）。
  6. **协商接线**：install_tool 处方要能被 valid_patch_op 校验、被 apply_patch 执行；
     两名审核者各写一份实现要"取第一份"而不是判冲突跳过。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import tool_lib as tl  # noqa: E402
from core import judgefix as jf  # noqa: E402

FAILS: list[str] = []
_TMP_NAMES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def _tool(name="t_demo", provides=None, verify=None, files=None):
    return {
        "name": name,
        "version": "1",
        "description": "自检用工具",
        "provides": provides if provides is not None else ["cap.demo"],
        "files": files if files is not None else {"entry.py": "VALUE = 1\n"},
        "verify": verify if verify is not None else
        [sys.executable, "-c", "import entry; assert entry.VALUE == 1"],
    }


def _install(m):
    _TMP_NAMES.append(m["name"])
    return tl.install(m)


def main() -> int:
    print("[1] 目录名强约束：纯英文小写")
    for bad in ("中文工具", "has space", "UPPER", "_lead", "9num", "a/b", ".."):
        ok, _ = tl.valid_name(bad)
        check(f"拒绝非法名 {bad!r}", not ok)
    for good in ("pdf_parser", "connector1", "a"):
        ok, _ = tl.valid_name(good)
        check(f"接受合法名 {good!r}", ok)

    print("[2] 清单契约校验")
    check("缺 description 拒", not tl.validate_manifest(
        {**_tool(), "description": ""})[0])
    check("provides 空拒", not tl.validate_manifest(
        {**_tool(), "provides": []})[0])
    check("verify 空拒", not tl.validate_manifest(
        {**_tool(), "verify": []})[0])
    check("files 空拒", not tl.validate_manifest({**_tool(), "files": {}})[0])
    check("自带 manifest.json 拒", not tl.validate_manifest(
        {**_tool(), "files": {"manifest.json": "x"}})[0])
    check("文件路径 ../ 越界拒", not tl.validate_manifest(
        {**_tool(), "files": {"../evil.py": "x"}})[0])
    check("文件绝对路径拒", not tl.validate_manifest(
        {**_tool(), "files": {"/etc/passwd": "x"}})[0])
    check("文件内容非字符串拒", not tl.validate_manifest(
        {**_tool(), "files": {"a.py": 123}})[0])

    print("[3] verify 白名单：不许安装/下载")
    for argv in (["pip", "install", "requests"], ["npm", "i", "x"],
                 ["curl", "http://x"], ["wget", "x"]):
        check(f"拒绝 verify {argv[:2]}",
              not tl.validate_manifest({**_tool(), "verify": argv})[0])
    check("python -c 自检放行", tl.validate_manifest(_tool())[0])

    print("[4] 正常安装：原子落盘 + 复验 rc==0")
    m = _tool("t_ok", provides=["cap.ok"])
    r = _install(m)
    check("安装成功", r["ok"], str(r.get("errors")))
    check("目录已生成", tl.installed("t_ok"))
    check("manifest 落盘", (tl.tool_dir("t_ok") / "manifest.json").is_file())
    check("入口文件落盘", (tl.tool_dir("t_ok") / "entry.py").is_file())
    check("verify 结果记录 rc=0", (r.get("verify") or {}).get("rc") == 0)
    check("按 provides 能查到工具", tl.find_by_provide("cap.ok") == "t_ok")

    print("[5] 幂等：同名同能力重复装不重写")
    r2 = _install(_tool("t_ok", provides=["cap.ok"]))
    check("重复安装仍成功", r2["ok"])
    check("重复安装走了幂等分支",
          any("幂等" in n for n in (r2.get("notes") or [])))

    print("[6] 复验失败 → 整目录回滚，不留半装垃圾")
    mbad = _tool("t_bad", verify=[sys.executable, "-c", "import sys; sys.exit(7)"])
    _TMP_NAMES.append("t_bad")
    rb = tl.install(mbad)
    check("复验失败判 ok=False", not rb["ok"])
    check("失败后目录不存在（已回滚）", not tl.installed("t_bad"))

    print("[7] 覆盖安装：旧版能备份、新版复验通过才替换")
    _ = _install(_tool("t_up", provides=["cap.up"],
                       files={"entry.py": "VALUE = 1\n"}))
    # 新版内容 VALUE=2，verify 必须同步改成断言 2（复验是裁判：内容与断言不符
    # 就该回滚，这正是 [6] 验证过的行为，不能拿旧断言验新内容）
    r3 = _install(_tool("t_up", provides=["cap.different"],
                        files={"entry.py": "VALUE = 2\n"},
                        verify=[sys.executable, "-c",
                                "import entry; assert entry.VALUE == 2"]))
    check("新版安装成功", r3["ok"], str(r3.get("errors")))
    body = (tl.tool_dir("t_up") / "entry.py").read_text(encoding="utf-8")
    check("新版内容已生效", "VALUE = 2" in body, body)
    check("旧版备份存在",
          any(p.name.startswith("_t_up.bak") for p in tl.lib_root().iterdir())
          if tl.lib_root().is_dir() else False)

    print("[8] list_tools 只列合法工具，坏清单跳过不炸")
    rows = tl.list_tools()
    names = {r["name"] for r in rows}
    check("列到已装工具", "t_ok" in names and "t_up" in names, str(names))

    print("[9] 协商接线：install_tool 能被校验并执行")
    op = {"op": "install_tool", "cap": "cap.x", "tool": _tool("t_wire",
                                                              provides=["cap.wire"])}
    ok9, why9 = jf.valid_patch_op(op)
    check("valid_patch_op 放行合法 install_tool", ok9, why9)
    check("valid_patch_op 拒绝非法工具（中文名）",
          not jf.valid_patch_op({"op": "install_tool", "cap": "cap.x",
                                 "tool": _tool("中文")})[0])
    check("valid_patch_op 拒绝装包 verify",
          not jf.valid_patch_op({"op": "install_tool", "cap": "cap.x",
                                 "tool": {**_tool("t_x"),
                                          "verify": ["npm", "install", "y"]}})[0])
    # 多份不同实现 → 取第一份（不能判冲突跳过）
    a = {"op": "install_tool", "cap": "cap.x",
         "tool": _tool("t_wire", provides=["cap.wire"],
                       files={"entry.py": "VALUE = 1\n"})}
    b = {"op": "install_tool", "cap": "cap.x",
         "tool": _tool("t_wire", provides=["cap.wire"],
                       files={"entry.py": "VALUE = 2  # v2\n"})}
    ws = Path(tempfile.mkdtemp())
    (ws / ".sixcoder").mkdir()
    (ws / ".sixcoder" / "blueprint.json").write_text(
        json.dumps({"caps": [{"id": "cap.x", "probe": {"steps": []}}]}),
        encoding="utf-8")
    merged, notes = jf.merge_patches([[a], [b]], ws=ws)
    check("两份不同实现合并为一条（取第一份）", len(merged) == 1, str(notes))
    _TMP_NAMES.append("t_wire")
    res = jf.apply_patch(ws, merged)
    check("apply_patch 执行安装 changed>=1", res["changed"] >= 1, str(res))
    check("接线后工具真的装上了", tl.installed("t_wire"))
    shutil.rmtree(ws, ignore_errors=True)

    print("[10] 卸载带名字校验")
    check("卸载合法工具成功", tl.remove("t_up")["ok"])
    check("卸载不存在/非法名不误删", not tl.remove("不存在的")["ok"])

    # 清理所有自检工具与备份
    for n in set(_TMP_NAMES):
        shutil.rmtree(tl.tool_dir(n), ignore_errors=True)
    if tl.lib_root().is_dir():
        for p in list(tl.lib_root().iterdir()):
            if p.name.startswith("_t_") and (".bak" in p.name or ".tmp" in p.name):
                shutil.rmtree(p, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
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
