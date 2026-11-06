"""远端感知/行动工具循环（§13.4 第 3 步）+ 受保护区只读（第 4 步）。

为什么需要（开发文档 §11.2 / §11.5）：远端现在只能回 JSON 补丁，看不见真实代码、只能猜
（L2）；而它最省力的「变绿」方式是删测试。工具循环给它**只读感知权**——读文件、grep、
跑指定测试、跑静态分析（结果原样回传）——同时用受保护区（第 4 步）把测试/契约/治理文件
设为对远端工具**只读**，杜绝「删测试变绿」。

设计边界（与 sandbox 同源，且更严）：
- 循环本身是**纯感知 + 记忆写入**：除 write_memory（只写 .sixcoder/knowledge.jsonl）外，
  不提供任意源码写工具；即便将来加 write_file，也必须过 `_tool_allow_write` 守卫。
- 所有读操作经 `tools` + `Sandbox`（readonly 模式），命令白名单 + 敏感路径拒绝。
- write_memory 是唯一写出口，目标固定为知识库（非受保护区），仍过 guard_write 防御纵深。
- 结果「原样回传」：不替远端解读，只把工具输出塞回对话，由远端自己判断。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import tools as _tools
from . import scale as _scale
from . import memory as _memory

# 支持两种写法：自闭合 <tool name="x" /> 与显式闭合 <tool name="x">body</tool>
_TOOL_RE = re.compile(r"<tool\b([^>]*?)\s*(?:/>|>(.*?)</tool>)", re.DOTALL | re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# 远端工具循环的写出口：测试/契约文件永远拒绝（受保护区只读，与级别无关）。
_TEST_RE = re.compile(r"(^|/)(test_.*\.py|.*_test\.py|contract.*\.py)$", re.IGNORECASE)

GUIDE = """\
# 你可用的只读工具（按需调用，不要臆测代码内容）

在推理过程中，如需要查看真实代码或运行验证，可发出如下工具调用（一次可多个）：

<tool name="read_file" path="src/foo.py" start="1" end="60" />
<tool name="list_files" sub="src" limit="50" />
<tool name="grep" pattern="def可疑函数名" path="src" limit="20" />
<tool name="run_tests" target="test_foo.py" />
<tool name="static_analysis" path="src/foo.py" />
<tool name="write_memory" type="fact" text="这里写你确认的工程事实，下轮会注入给你" />

规则：
- 只有 read_file / list_files / grep / run_tests / static_analysis / write_memory 六种。
- 结果是原样回传的，请结合它们做判断；不要凭空编造文件内容。
- write_memory 是唯一能写的东西，只能写 .sixcoder/knowledge.jsonl，且只记事实/约束/坑。
- 测试/契约/治理文件对工具循环只读，写入会被拒绝。
"""


def parse_tool_calls(text: str) -> list[tuple[str, dict, str]]:
    """解析回复里的 <tool ...> 调用，返回 [(name, attrs, body)]。解析失败安全返回空。"""
    out: list[tuple[str, dict, str]] = []
    try:
        for m in _TOOL_RE.finditer(text or ""):
            attrs = {k: v for k, v in _ATTR_RE.findall(m.group(1))}
            name = (attrs.get("name") or "").strip().lower()
            if name:
                out.append((name, attrs, (m.group(2) or "").strip()))
    except Exception:                            # noqa: BLE001
        pass
    return out


def _grep(ws: Path, pattern: str, path: str = ".", limit: int = 200) -> str:
    """在仓库里按正则搜代码（只读，跳过非源码与体积过大文件）。"""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"grep 正则非法：{exc}"
    ws_r = Path(ws).resolve()
    root = (ws_r / path) if path and not Path(path).is_absolute() else ws_r
    hits: list[str] = []
    for p in sorted(root.rglob("*")):
        if len(hits) >= limit:
            break
        if not p.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__", ".pytest_cache",
                        ".venv", ".sixcoder", ".mypy_cache", "dist", "build"}
               for part in p.parts):
            continue
        if p.suffix not in (".py", ".js", ".ts", ".md", ".txt", ".yml", ".yaml", ".json", ".toml"):
            continue
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = p.relative_to(ws_r).as_posix()
        for i, ln in enumerate(lines, 1):
            if rx.search(ln):
                hits.append(f"{rel}:{i}: {ln.strip()[:200]}")
                if len(hits) >= limit:
                    break
    return "\n".join(hits) if hits else f"在 {path} 下未匹配到「{pattern}」"


def _static_analysis(ws: Path, rel: str) -> str:
    """轻量静态分析：py_compile 检查语法 + 行数/复杂度粗报（不依赖外部工具）。"""
    p = _tools.safe_path(ws, rel)
    if not p.exists():
        return f"文件不存在：{rel}"
    try:
        code = p.read_text(encoding="utf-8-sig", errors="replace")
        compile(code, str(p), "exec")         # 语法/编译检查（最便宜的真判据）
    except SyntaxError as exc:
        return f"语法错误 {p.name}:{exc.lineno}: {exc.msg}"
    except Exception as exc:                  # noqa: BLE001
        return f"静态分析异常：{type(exc).__name__}: {exc}"
    nlines = len(code.splitlines())
    nfuncs = code.count("def ")
    return f"{p.name} 语法 OK（{nlines} 行，约 {nfuncs} 个 def）；未装更复杂静态分析时为仅此一项"


class ToolLoop:
    """在 remote.complete 之上做多轮工具往返：模型发 tool 调用 → 系统执行 → 回结果 → 再 complete。

    complete_fn: 可选 (instruction, messages) -> str，用于测试注入假模型；缺省走 remote.complete。
    """

    def __init__(self, ws: Path, level: str = "S", max_rounds: int = 6,
                 emit=None, complete_fn=None, memory=None):
        self.ws = Path(ws)
        self.level = str(level).upper() or "S"
        self.max_rounds = max_rounds if max_rounds > 0 else 6
        self.emit = emit
        self.complete_fn = complete_fn
        self.memory = memory or _memory.MemoryStore(self.ws)
        self.rounds = 0
        self.tool_calls: list[str] = []
        self.memory_written: list[dict] = []
        self.denied: list[str] = []

    # -------------------------------------------------- 受保护区（第 4 步）
    def _tool_allow_write(self, rel: str) -> tuple[bool, str]:
        low = str(rel).replace("\\", "/").lower()
        # 测试/契约文件：对远端工具永远只读（与规模级别无关，因为工具循环没有人在回路里兜底）
        if _TEST_RE.search(low):
            return False, f"远端工具循环禁止写测试/契约文件（受保护区只读）：{rel}"
        # 认证/部署/治理/AGENTS.md 等：对远端工具也永远只读（不受 L+ 才 block 的限制）
        hits = _scale.protected_hits(self.ws, [rel])
        if hits:
            return False, f"受保护区（远端工具只读）：{hits[0]} 不允许工具循环写入"
        return True, ""

    # -------------------------------------------------- 工具分发
    def _run_tool(self, name: str, attrs: dict, body: str) -> tuple[bool, str]:
        try:
            if name == "read_file":
                out = _tools.read_file(self.ws, attrs.get("path", ""),
                                       int(attrs["start"]) if attrs.get("start") else None,
                                       int(attrs["end"]) if attrs.get("end") else None)
                return True, out
            if name == "list_files":
                out = "\n".join(_tools.list_files(self.ws, attrs.get("sub", ""),
                                                  int(attrs.get("limit") or 200)))
                return True, out or "(空)"
            if name == "grep":
                return True, _grep(self.ws, attrs.get("pattern", ""),
                                   attrs.get("path", "."), int(attrs.get("limit") or 200))
            if name == "run_tests":
                rc, out = _tools.run_bash(self.ws, f"pytest {attrs.get('target','')} -q", timeout=120)
                return rc == 0, f"pytest rc={rc}\n{out[:1500]}"
            if name == "static_analysis":
                return True, _static_analysis(self.ws, attrs.get("path", ""))
            if name == "write_memory":
                text = (body or attrs.get("text") or "").strip()
                if not text:
                    return False, "write_memory 缺少 text"
                entry = self.memory.append(text, attrs.get("type", "fact"),
                                           attrs.get("src", "tool_loop"))
                if entry is None:
                    return False, "记忆写入失败（路径不可写或内容为空）"
                self.memory_written.append(entry)
                return True, f"已写入记忆 [{entry['type']}]：{entry['text'][:80]}"
            return False, f"未知工具：{name}（可用：read_file/list_files/grep/run_tests/static_analysis/write_memory）"
        except Exception as exc:                # noqa: BLE001
            return False, f"工具 {name} 执行异常：{type(exc).__name__}: {exc}"

    def _complete(self, messages: list[dict]) -> str:
        if self.complete_fn:
            return self.complete_fn(self.instruction, messages) or ""
        from . import remote
        body = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
        r = remote.complete(prompt=body or self.instruction, workspace=str(self.ws),
                            task_id="toolloop", messages=messages)
        return (r.get("answer") or "") if isinstance(r, dict) else str(r)

    def run(self, instruction: str) -> dict:
        """跑一轮工具循环，返回 {answer, rounds, tool_calls, memory_written, denied}。"""
        self.instruction = instruction
        messages: list[dict] = [
            {"role": "system", "content": GUIDE},
            {"role": "user", "content": instruction},
        ]
        last = ""
        for rnd in range(1, self.max_rounds + 1):
            self.rounds = rnd
            reply = self._complete(messages)
            last = reply
            calls = parse_tool_calls(reply)
            if not calls:
                break
            results: list[str] = []
            for name, attrs, body in calls:
                self.tool_calls.append(f"{name}({attrs.get('path') or attrs.get('target') or ''})")
                # 写类工具的受保护区守卫（防御纵深）
                if name in ("write_file",) or (name == "write_memory"):
                    if name == "write_file":
                        allowed, reason = self._tool_allow_write(attrs.get("path", ""))
                        if not allowed:
                            self.denied.append(reason)
                            results.append(f"<tool_result name=\"{name}\">\n拒绝：{reason}\n</tool_result>")
                            continue
                ok, out = self._run_tool(name, attrs, body)
                results.append(f"<tool_result name=\"{name}\" ok=\"{ok}\">\n{out}\n</tool_result>")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": "工具结果：\n" + "\n".join(results)})
            if self.emit:
                self.emit("tool_loop", {"round": rnd, "tools": len(calls),
                                        "memory": len(self.memory_written),
                                        "denied": len(self.denied)})
        return {
            "answer": last, "rounds": self.rounds,
            "tool_calls": list(self.tool_calls),
            "memory_written": list(self.memory_written),
            "denied": list(self.denied),
            "hit_max_rounds": self.rounds >= self.max_rounds and bool(parse_tool_calls(last)),
        }


def tool_loop_guide() -> str:
    """给 author_module 注入的工具说明（远端开启 tool_loop 时追加到提示词）。"""
    return GUIDE
