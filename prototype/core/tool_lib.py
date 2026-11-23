"""自主工具库：远端协商认定"缺工具"时，把工具代码规范落盘到本地并复验。

## 为什么需要（2026-11-19 用户需求）
引擎验证产物时会遇到"缺工具"：缺第三方 provider 连接器、缺某语言的
工具链适配、缺一个探针辅助脚本……以前这些信号只写日志/报告，远端看不到，
更没法自己补。用户定稿：让三方协商机制在认定缺工具时，由远端**现写工具代码**，
引擎把它规范化装进本地工具库，每个工具一个纯英文子文件夹，复验通过才算装好。

## 设计边界（都是实测踩出来的铁律）
1. **纯英文目录名**：`^[a-z][a-z0-9_]{0,63}$`。防止中文/空格/路径穿越。
2. **复验是裁判（铁律6）**：远端说"装好了"不算。必须跑 manifest.verify（argv），
   rc==0 才算装成；失败整目录回滚，绝不留半装的垃圾。
3. **不许静默吞异常（铁律7）**：任何失败都写进返回的 errors，调用方留痕。
4. **原子落盘 + 回滚（铁律1）**：先写临时目录 → 回读 manifest 校验 sha256 →
   复验 → 原子改名。中途任何一步失败，删临时目录；覆盖安装先备份旧目录。
5. **verify 命令白名单**：拒绝 pip/npm install、curl/wget、绝对路径、`..` ——
   沙箱（core/sandbox.py）明令禁止包安装，工具库不走沙箱但也绝不执行安装命令。
   工具是"代码 + 自检脚本"，不是"下载器"。

## 位置
`prototype/data/tool_lib/<name>/`（data/ 是不入库的运行期数据，引擎级共享）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 引擎内 data/tool_lib/，与 set_key.py 同款解析（parents[1] = prototype/）
TOOL_LIB = Path(__file__).resolve().parents[1] / "data" / "tool_lib"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MANIFEST = "manifest.json"
MAX_FILES = 40
MAX_FILE_BYTES = 200_000          # 单文件 200KB，工具是代码不是数据
MAX_VERIFY_ARGV = 32
# verify 里禁止的命令头（沙箱 DENY_PATTERNS 同款口径：不下载、不装包）
_VERIFY_DENY = ("pip", "pip3", "npm", "yarn", "pnpm", "curl", "wget",
                "apt", "apt-get", "yum", "brew", "powershell", "cmd")
_IN_KWARGS = ("install", "uninstall", "add", "i")   # npm i / pip install


def lib_root() -> Path:
    return TOOL_LIB


def tool_dir(name: str) -> Path:
    return TOOL_LIB / name


def valid_name(name: str) -> tuple[bool, str]:
    if not isinstance(name, str) or not NAME_RE.match(name):
        return False, "工具名必须是纯英文小写 [a-z][a-z0-9_]{0,63}：%r" % name
    if name in (".", "..") or "/" in name or "\\" in name:
        return False, "工具名不能含路径分隔符"
    return True, ""


def _safe_rel(p: str) -> bool:
    """文件相对路径必须安全：非绝对、无 ..、不含反斜杠、非空。"""
    if not isinstance(p, str) or not p.strip():
        return False
    if p.startswith(("/", "\\")) or ".." in p.replace("\\", "/").split("/"):
        return False
    if "\\" in p:
        return False
    return True


def validate_manifest(m: dict) -> tuple[bool, str]:
    """校验工具清单形状。返回 (ok, 说明)。"""
    if not isinstance(m, dict):
        return False, "manifest 不是对象"
    ok, why = valid_name(str(m.get("name") or ""))
    if not ok:
        return False, why
    if not str(m.get("description") or "").strip():
        return False, "缺 description"
    provides = m.get("provides")
    if not isinstance(provides, list) or not provides:
        return False, "provides 必须是非空数组（工具补上什么能力/缺工具 token）"
    if any(not isinstance(x, str) or not x.strip() for x in provides):
        return False, "provides 每项必须是非空字符串"
    verify = m.get("verify")
    if not isinstance(verify, list) or not verify:
        return False, "verify 必须是非空 argv 数组（复验命令，rc==0 才算装好）"
    if len(verify) > MAX_VERIFY_ARGV:
        return False, "verify argv 过长"
    if any(not isinstance(x, str) for x in verify):
        return False, "verify argv 必须全是字符串"
    head = str(verify[0]).strip().lower().split("/")[-1].split("\\")[-1]
    if head in _VERIFY_DENY:
        return False, "verify 不允许安装/下载类命令：%s" % head
    # verify 里出现 npm i / pip install 这类组合也拦（argv 里 install 紧跟包管理器）
    for i, tok in enumerate(verify[:-1]):
        if str(tok).strip().lower() in _VERIFY_DENY and \
                str(verify[i + 1]).strip().lower() in _IN_KWARGS:
            return False, "verify 不允许包安装：%s %s" % (tok, verify[i + 1])
    files = m.get("files")
    if not isinstance(files, dict) or not files:
        return False, "files 必须是 {相对路径: 内容字符串} 对象"
    if len(files) > MAX_FILES:
        return False, "文件数超过 %d" % MAX_FILES
    if MANIFEST in files:
        return False, "files 不应自带 manifest.json（由引擎生成）"
    for rel, content in files.items():
        if not _safe_rel(rel):
            return False, "文件相对路径不安全：%r" % rel
        if not isinstance(content, str):
            return False, "文件内容必须是字符串：%r" % rel
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return False, "文件过大：%r" % rel
    return True, ""


def _fingerprint(files: dict) -> str:
    """落盘内容的确定性指纹（按路径排序）——用于回读比对，确认没写坏。"""
    blob = json.dumps({k: v for k, v in sorted(files.items())},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def installed(name: str) -> bool:
    d = tool_dir(name)
    return (d / MANIFEST).is_file()


def list_tools() -> list[dict]:
    """列出已装工具（读 manifest，坏清单跳过不炸）。"""
    out = []
    if not TOOL_LIB.is_dir():
        return out
    for d in sorted(TOOL_LIB.iterdir()):
        if not d.is_dir() or not valid_name(d.name)[0]:
            continue
        p = d / MANIFEST
        if not p.is_file():
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            continue
        out.append({"name": m.get("name", d.name),
                    "description": str(m.get("description") or "")[:200],
                    "provides": m.get("provides") or [],
                    "version": m.get("version", ""),
                    "installed_at": m.get("installed_at", "")})
    return out


def provides(name: str) -> list[str]:
    """工具补上哪些能力/缺工具 token（用于幂等判定）。"""
    p = tool_dir(name) / MANIFEST
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        return [str(x) for x in (m.get("provides") or [])]
    except Exception:                                     # noqa: BLE001
        return []


def find_by_provide(token: str) -> str | None:
    """查哪个已装工具补上了这个缺工具 token。"""
    tok = str(token or "").strip()
    if not tok:
        return None
    for row in list_tools():
        if tok in (row.get("provides") or []):
            return row.get("name")
    return None


def _run_verify(cwd: Path, argv: list[str], timeout: int = 60) -> dict:
    """在工具目录里真跑复验命令。返回 {ok, rc, output}。不吞异常。

    内置 Python 的 `._pth` 下，`python -c` 的 cwd **不在 sys.path**，因此
    `python -c "import entry"` 会 ModuleNotFound（2026-11-23 实测：工具明明装对了，
    复验却失败 → 整包被回滚）。这里对 `-c` 和 `script.py` 两种形态显式注入工具目录。
    """
    argv = [str(x) for x in argv]
    _cwd = str(Path(cwd).resolve())
    head = Path(argv[0]).name.lower() if argv else ""
    if head in ("python", "python.exe", "python3", "py") and len(argv) >= 2:
        if argv[1] == "-c" and len(argv) >= 3:
            argv = [argv[0], "-c",
                    f"import sys; sys.path.insert(0, {_cwd!r})\n" + argv[2], *argv[3:]]
        elif argv[1].endswith(".py"):
            _sp = str((Path(cwd) / argv[1]).resolve())
            argv = [argv[0], "-c",
                    "import sys, runpy\n"
                    f"sys.path.insert(0, {_cwd!r})\n"
                    f"sys.argv = {([_sp, *argv[2:]])!r}\n"
                    f"runpy.run_path({_sp!r}, run_name='__main__')\n"]
    try:
        cp = subprocess.run(argv, cwd=str(cwd),
                            capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
        out = ((cp.stdout or "") + (cp.stderr or "")).strip()
        return {"ok": cp.returncode == 0, "rc": cp.returncode,
                "output": out[:2000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "output": "verify 超时 >%ds" % timeout}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "rc": -1,
                "output": "%s: %s" % (type(exc).__name__, exc)}


def install(manifest: dict, *, timeout: int = 60) -> dict:
    """把工具规范化落盘 + 复验。返回 {ok, name, notes, errors, verify}。

    流程（任何一步失败都回滚，绝不留半装目录）：
      校验 manifest → 临时目录写文件 → 回读比对指纹 → 跑 verify →
      原子改名到 tool_lib/<name> → 再读一次确认。
    """
    errors: list[str] = []
    notes: list[str] = []
    ok, why = validate_manifest(manifest)
    if not ok:
        return {"ok": False, "errors": [why], "notes": notes, "verify": None}
    name = str(manifest["name"])
    files = dict(manifest["files"])
    dest = tool_dir(name)
    # 幂等：同名已装且提供相同能力 → 直接成功，不重写（远端可能重复开同处方）
    if dest.is_file():
        return {"ok": False, "errors": ["工具名被普通文件占用"], "notes": notes,
                "verify": None}
    if installed(name):
        existing = set(provides(name))
        if existing & set(manifest["provides"]):
            notes.append("已装且提供相同能力，幂等跳过")
            return {"ok": True, "name": name, "notes": notes,
                    "errors": [], "verify": {"ok": True, "rc": 0,
                                             "output": "already installed"}}
        # 提供不同能力 → 覆盖安装，先备份旧目录
        bak = TOOL_LIB / ("_%s.bak-%d" % (name, int(time.time())))
        try:
            dest.rename(bak)
            notes.append("旧版已备份到 %s" % bak.name)
        except OSError as exc:
            return {"ok": False,
                    "errors": ["备份旧工具失败：%s" % exc], "notes": notes,
                    "verify": None}

    TOOL_LIB.mkdir(parents=True, exist_ok=True)
    tmp = TOOL_LIB / ("_%s.tmp-%d" % (name, int(time.time() * 1000)))
    bak_dir: Path | None = None
    try:
        tmp.mkdir(parents=True, exist_ok=False)
        for rel, content in files.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # 回读比对：确认写盘内容与设计一致（铁律：写盘机制自带复验）
        reread = {}
        for rel in files:
            reread[rel] = (tmp / rel).read_text(encoding="utf-8")
        if _fingerprint(reread) != _fingerprint(files):
            raise RuntimeError("回读指纹不一致，写盘被篡改")
        # 写 manifest（内容指纹也记进去，便于事后核对）
        full = dict(manifest)
        full["files_sha256"] = _fingerprint(files)
        full["installed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (tmp / MANIFEST).write_text(
            json.dumps(full, ensure_ascii=False, indent=1), encoding="utf-8")
        # 复验：rc==0 才算装好（裁判是执行结果，不是远端的话）
        vr = _run_verify(tmp, [str(x) for x in manifest["verify"]], timeout=timeout)
        if not vr["ok"]:
            errors.append("复验失败（rc=%s）：%s" % (vr["rc"], vr["output"][:400]))
            shutil.rmtree(tmp, ignore_errors=True)
            # 恢复旧版备份（如果有）
            if 'bak' in locals() and bak.exists():
                try:
                    bak.rename(dest)
                    notes.append("复验失败，已恢复旧版")
                except OSError:
                    errors.append("恢复旧版失败：%s" % bak.name)
            return {"ok": False, "name": name, "errors": errors,
                    "notes": notes, "verify": vr}
        # 原子改名落位
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        notes.append("工具已装到 %s（复验 rc=0）" % dest.relative_to(TOOL_LIB.parent.parent))
        return {"ok": True, "name": name, "notes": notes,
                "errors": [], "verify": vr}
    except Exception as exc:                              # noqa: BLE001
        errors.append("安装异常：%s: %s" % (type(exc).__name__, exc))
        shutil.rmtree(tmp, ignore_errors=True)
        if 'bak' in locals() and bak.exists():
            try:
                bak.rename(dest)
            except OSError:
                pass
        return {"ok": False, "name": name, "errors": errors,
                "notes": notes, "verify": None}


def remove(name: str) -> dict:
    """卸载工具（带名字校验，绝不按未校验路径删）。"""
    ok, why = valid_name(name)
    if not ok:
        return {"ok": False, "errors": [why]}
    d = tool_dir(name)
    if not d.is_dir():
        return {"ok": False, "errors": ["工具不存在：%s" % name]}
    try:
        shutil.rmtree(d)
        return {"ok": True, "errors": []}
    except OSError as exc:
        return {"ok": False, "errors": ["卸载失败：%s" % exc]}
