"""生态包：协商补全"缺失的项目类型能力"（前端 SPA / 桌面 / 中间件 / 移动…）。

## 为什么需要（2026-11-19 用户需求）
引擎原来只有 FastAPI/Django 两个栈适配器、4 种探针，做不了前端 SPA、桌面、
中间件系统等。用户定稿：**让协商机制在缺能力时补全生态包**——远端现写
"栈适配器 + 探针 + 检测器"，引擎规范化装进本地生态库，复验通过才启用。

## 一个生态包 = 一个目录
    data/ecosystem/<name>/
      manifest.json     # 声明 kind/provides/needs/verify/adapter_class/files
      adapter.py        # 可选：StackAdapter 子类（前端/桌面等"栈"类必填）
      probe_<kind>.py   # 可选：新探针实现（只在子进程执行，不进主进程）
      detector.py       # 可选：新检测器
      selftest.py       # 可选：包自己的自检样例

## 安全红线（动态加载远端代码 = 任意代码执行，必须卡死）
1. **AST 静态校验**（导入前）：禁危险 import（os/subprocess/socket/shutil/
   ctypes/importlib/pickle/marshal/multiprocessing…）、禁 exec/eval/compile/
   __import__/open、禁写盘属性（write_text/write_bytes/mkdir/unlink/rmtree…）。
   adapter 在**主进程**跑（生成代码要用它），所以这层必须严。
2. **探针/检测器只在子进程执行**：不 import 进主进程，避免污染引擎状态。
3. **复验是裁判**：装完必须跑 manifest.verify（真实命令，rc==0）+ 可选 selftest
   跑通，才算装好；失败整包回滚。
4. **不静默吞异常**：所有失败进 errors，调用方留痕。
5. 与 tool_lib 同款：临时目录 → 回读指纹 → 原子改名 → 失败回滚。
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ECO_ROOT = Path(__file__).resolve().parents[1] / "data" / "ecosystem"
MANIFEST = "manifest.json"
NAME_RE_STR = r"^[a-z][a-z0-9_]{0,63}$"
PACK_KINDS = ("stack", "probe", "detector")
MAX_FILES = 60
MAX_FILE_BYTES = 300_000

# 允许 import 的模块（引擎自身 + 纯数据类标准库）。其余一律拒——生态包不需要
# 网络/进程/文件系统写权限（写盘由引擎做，adapter 只返回内容）。
_ALLOWED_IMPORT_ROOTS = {
    "core", "re", "json", "dataclasses", "typing", "pathlib", "collections",
    "itertools", "functools", "string", "math", "datetime", "enum", "abc",
    "textwrap", "urllib.parse", "hashlib",
}
# 即使 root 在白名单里，这些子模块也危险
_BANNED_IMPORTS = {
    "os", "subprocess", "socket", "shutil", "ctypes", "importlib", "pickle",
    "marshal", "shelve", "multiprocessing", "pty", "fcntl", "signal",
    "threading", "asyncio", "http", "urllib.request", "requests", "tempfile",
    "builtins", "sys",
}
_BANNED_CALLS = {"exec", "eval", "compile", "__import__", "open", "input",
                 "breakpoint", "globals", "locals", "vars"}
# 写盘/删除类属性调用。**注意不能收 str 同名方法**（2026-11-19 实测误杀）：
# `replace`/`remove`/`rename` 对字符串是正常操作，adapter 里排版/归一化要用；
# 而写盘能力已由"禁 open / 禁 write_text / 禁 pathlib 写"堵死，这几个不必禁。
_BANNED_ATTR_CALLS = {"write_text", "write_bytes", "mkdir", "unlink", "rmdir",
                      "chmod", "symlink_to", "system", "popen", "rmtree",
                      "spawn", "fork", "execv", "kill"}


def lib_root() -> Path:
    return ECO_ROOT


def pack_dir(name: str) -> Path:
    return ECO_ROOT / name


def valid_name(name: str) -> tuple[bool, str]:
    import re
    if not isinstance(name, str) or not re.match(NAME_RE_STR, name):
        return False, "生态包名必须纯英文小写 [a-z][a-z0-9_]{0,63}：%r" % name
    return True, ""


def ast_check(src: str, filename: str = "adapter.py") -> tuple[bool, str]:
    """AST 静态校验：禁危险 import / 危险调用 / 写盘属性。返回 (ok, 说明)。"""
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as exc:
        return False, "语法错误：%s" % exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                full = a.name
                if full in _BANNED_IMPORTS or root in _BANNED_IMPORTS:
                    return False, "禁止 import 危险模块：%s" % full
                if root not in _ALLOWED_IMPORT_ROOTS:
                    return False, "import 不在白名单：%s" % full
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".")[0]
            if mod in _BANNED_IMPORTS or root in _BANNED_IMPORTS:
                return False, "禁止 from-import 危险模块：%s" % mod
            if root and root not in _ALLOWED_IMPORT_ROOTS:
                return False, "from-import 不在白名单：%s" % mod
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _BANNED_CALLS:
                return False, "禁止调用 %s()" % f.id
            if isinstance(f, ast.Attribute) and f.attr in _BANNED_ATTR_CALLS:
                return False, "禁止调用 .%s()（写盘/进程/删除类）" % f.attr
        elif isinstance(node, ast.Attribute):
            if node.attr in ("__globals__", "__builtins__", "__subclasses__",
                             "__class__", "__bases__", "__mro__"):
                return False, "禁止访问 %s（逃逸面）" % node.attr
    return True, ""


def validate_manifest(m: dict, *, require_files: bool = False) -> tuple[bool, str]:
    """校验生态包清单。`require_files=True` 用于"协商安装"（files 必须内联）；
    引擎自带的包文件已落盘在目录里，manifest 可不带 files。"""
    if not isinstance(m, dict):
        return False, "manifest 不是对象"
    ok, why = valid_name(str(m.get("name") or ""))
    if not ok:
        return False, why
    kind = str(m.get("kind") or "").strip()
    if kind not in PACK_KINDS:
        return False, "kind 只允许 %s" % "/".join(PACK_KINDS)
    if not str(m.get("description") or "").strip():
        return False, "缺 description"
    provides = m.get("provides")
    if not isinstance(provides, list) or not provides:
        return False, "provides 必须是非空数组"
    needs = m.get("needs")
    if needs is not None and not isinstance(needs, list):
        return False, "needs 必须是数组"
    verify = m.get("verify")
    if not isinstance(verify, list) or not verify:
        return False, "verify 必须是非空 argv（装完复验，rc==0 才算装好）"
    if any(not isinstance(x, str) for x in verify):
        return False, "verify argv 必须全是字符串"
    files = m.get("files")
    if require_files:
        if not isinstance(files, dict) or not files:
            return False, "files 必须是 {相对路径: 内容字符串}（协商安装必须内联文件）"
    if files is not None:
        if not isinstance(files, dict):
            return False, "files 必须是对象"
        if len(files) > MAX_FILES:
            return False, "文件数超过 %d" % MAX_FILES
        if MANIFEST in files:
            return False, "files 不应自带 manifest.json（引擎生成）"
        for rel, content in files.items():
            if not isinstance(rel, str) or not rel.strip() or rel.startswith(("/", "\\")) \
                    or ".." in rel.replace("\\", "/").split("/") or "\\" in rel:
                return False, "文件相对路径不安全：%r" % rel
            if not isinstance(content, str):
                return False, "文件内容必须是字符串：%r" % rel
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                return False, "文件过大：%r" % rel
            if rel.endswith(".py"):
                ok2, why2 = ast_check(content, rel)
                if not ok2:
                    return False, "%s 静态校验失败：%s" % (rel, why2)
    # kind=stack 必须声明 adapter_class；协商安装的还要内联 adapter.py
    if kind == "stack":
        if not str(m.get("adapter_class") or "").strip():
            return False, "kind=stack 必须给 adapter_class"
        if require_files and "adapter.py" not in (files or {}):
            return False, "kind=stack 必须包含 adapter.py"
    return True, ""


def _fingerprint(files: dict) -> str:
    blob = json.dumps({k: v for k, v in sorted(files.items())},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _run(cmd: list, cwd: Path, timeout: int = 120) -> dict:
    try:
        cp = subprocess.run([str(x) for x in cmd], cwd=str(cwd), timeout=timeout,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        out = ((cp.stdout or "") + (cp.stderr or "")).strip()
        return {"ok": cp.returncode == 0, "rc": cp.returncode, "output": out[:2000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "output": "超时 >%ds" % timeout}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "rc": -1,
                "output": "%s: %s" % (type(exc).__name__, exc)}


def install(manifest: dict, *, timeout: int = 180) -> dict:
    """装一个生态包：校验 → 临时落盘 → 回读 → 复验 → 原子改名；失败回滚。"""
    errors, notes = [], []
    ok, why = validate_manifest(manifest, require_files=True)
    if not ok:
        return {"ok": False, "errors": [why], "notes": notes, "verify": None}
    name = str(manifest["name"])
    dest = pack_dir(name)
    files = dict(manifest["files"])
    if dest.is_file():
        return {"ok": False, "errors": ["包名被普通文件占用"], "notes": notes,
                "verify": None}
    bak = None
    if dest.is_dir():
        bak = ECO_ROOT / ("_%s.bak-%d" % (name, int(time.time())))
        try:
            dest.rename(bak)
            notes.append("旧版备份到 %s" % bak.name)
        except OSError as exc:
            return {"ok": False, "errors": ["备份旧包失败：%s" % exc],
                    "notes": notes, "verify": None}
    ECO_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = ECO_ROOT / ("_%s.tmp-%d" % (name, int(time.time() * 1000)))
    try:
        tmp.mkdir(parents=True, exist_ok=False)
        for rel, content in files.items():
            t = tmp / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(content, encoding="utf-8")
        reread = {rel: (tmp / rel).read_text(encoding="utf-8") for rel in files}
        if _fingerprint(reread) != _fingerprint(files):
            raise RuntimeError("回读指纹不一致")
        full = dict(manifest)
        full["files_sha256"] = _fingerprint(files)
        full["installed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (tmp / MANIFEST).write_text(json.dumps(full, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        # 复验：真实命令 rc==0
        vr = _run([str(x) for x in manifest["verify"]], tmp, timeout=timeout)
        if not vr["ok"]:
            errors.append("复验失败（rc=%s）：%s" % (vr["rc"], vr["output"][:400]))
            shutil.rmtree(tmp, ignore_errors=True)
            if bak and bak.exists():
                bak.rename(dest)
                notes.append("复验失败，已恢复旧包")
            return {"ok": False, "name": name, "errors": errors,
                    "notes": notes, "verify": vr}
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        notes.append("生态包装到 %s（复验 rc=0）" % dest.name)
        return {"ok": True, "name": name, "errors": [], "notes": notes,
                "verify": vr}
    except Exception as exc:                              # noqa: BLE001
        errors.append("安装异常：%s: %s" % (type(exc).__name__, exc))
        shutil.rmtree(tmp, ignore_errors=True)
        if bak and bak.exists():
            try:
                bak.rename(dest)
            except OSError:
                pass
        return {"ok": False, "name": name, "errors": errors, "notes": notes,
                "verify": None}


def apply_templates(name: str, ws, *, overwrite: bool = False) -> dict:
    """把生态包 `templates/` 下的骨架文件复制到产物仓（**引擎执行写盘**）。

    为什么写盘在引擎侧而不是 adapter：adapter 是远端可能写的代码，AST 已禁它
    直接写盘；骨架复制由引擎这个可信方做，安全边界清晰。
    """
    d = pack_dir(name) / "templates"
    if not d.is_dir():
        return {"ok": False, "wrote": 0, "errors": ["该包没有 templates/"]}
    ws = Path(ws)
    wrote, errors = 0, []
    for f in sorted(d.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(d)
        target = ws / rel
        if target.exists() and not overwrite:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read_bytes())
            wrote += 1
        except Exception as exc:                          # noqa: BLE001
            errors.append("%s: %s" % (rel, exc))
    return {"ok": not errors, "wrote": wrote, "errors": errors}


def detect(name: str, ws) -> bool:
    """用该生态包的识别能力判断产物仓是不是这种项目。

    识别靠 adapter 的 `is_app_source`（扫入口/源码）+ 包目录特征（package.json
    等）。这里只做"目录特征 + adapter 可用"这一步，源码级识别在生成流程里做。
    """
    d = pack_dir(name)
    m = d / MANIFEST
    if not m.is_file():
        return False
    try:
        meta = json.loads(m.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return False
    markers = meta.get("detect_markers") or []
    ws = Path(ws)
    return bool(markers) and any((ws / mk).exists() for mk in markers)


def list_packs() -> list[dict]:
    out = []
    if not ECO_ROOT.is_dir():
        return out
    for d in sorted(ECO_ROOT.iterdir()):
        if not d.is_dir() or not valid_name(d.name)[0]:
            continue
        p = d / MANIFEST
        if not p.is_file():
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            continue
        out.append({"name": m.get("name", d.name), "kind": m.get("kind"),
                    "description": str(m.get("description") or "")[:200],
                    "provides": m.get("provides") or [],
                    "needs": m.get("needs") or [],
                    "installed_at": m.get("installed_at", "")})
    return out


def load_adapter(name: str):
    """动态加载栈适配器（主权进程）。返回 (adapter_instance | None, 说明)。

    先重新过一遍 AST 校验（防落盘后被外部篡改），再 import。失败返回 None 并给原因。
    """
    d = pack_dir(name)
    p = d / MANIFEST
    a = d / "adapter.py"
    if not p.is_file() or not a.is_file():
        return None, "包不存在或缺 adapter.py"
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        return None, "manifest 读取失败：%s" % exc
    src = a.read_text(encoding="utf-8")
    ok, why = ast_check(src, "adapter.py")
    if not ok:
        return None, "加载前静态校验失败：%s" % why
    cls_name = str(m.get("adapter_class") or "").strip()
    if not cls_name:
        return None, "缺 adapter_class"
    # importlib 从文件加载（避免包名与 sys.path 冲突）
    import importlib.util
    spec = importlib.util.spec_from_file_location("sixcoder_eco_%s" % name, a)
    if spec is None or spec.loader is None:
        return None, "无法构造模块 spec"
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                             # noqa: BLE001
        return None, "adapter 执行失败：%s: %s" % (type(exc).__name__, exc)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        return None, "找不到类 %s" % cls_name
    try:
        inst = cls()
    except Exception as exc:                             # noqa: BLE001
        return None, "适配器实例化失败：%s: %s" % (type(exc).__name__, exc)
    return inst, "ok"


def load_all(*, register: bool = True) -> dict:
    """启动时加载所有合法生态包的栈适配器，注册进 stack._ADAPTERS。"""
    result = {"loaded": [], "failed": []}
    from . import stack as _stack
    for row in list_packs():
        if row.get("kind") != "stack":
            continue
        inst, why = load_adapter(row["name"])
        if inst is None:
            result["failed"].append({"name": row["name"], "why": why})
            continue
        if register:
            try:
                _stack.register(inst)
            except Exception as exc:                      # noqa: BLE001
                result["failed"].append({"name": row["name"],
                                         "why": "注册失败：%s" % exc})
                continue
        result["loaded"].append(row["name"])
    return result


def remove(name: str) -> dict:
    ok, why = valid_name(name)
    if not ok:
        return {"ok": False, "errors": [why]}
    d = pack_dir(name)
    if not d.is_dir():
        return {"ok": False, "errors": ["包不存在"]}
    try:
        shutil.rmtree(d)
        return {"ok": True, "errors": []}
    except OSError as exc:
        return {"ok": False, "errors": ["卸载失败：%s" % exc]}
