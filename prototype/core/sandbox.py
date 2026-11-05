"""沙箱：路径牢笼 + 命令策略 + 备份日志 + 只读模式。

必须说清楚它的边界（docs 里也要写）：
- 这一层是**进程内策略沙箱**：防误操作、防路径越界、防网络外联、防破坏性命令，**不是**安全边界。
- 真正的强隔离需要 OS 层：Windows Sandbox / 容器 / 专用用户账户。本项目不声称提供后者。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


class SandboxError(Exception):
    pass


# 允许调用的命令首词（工具链相关）
ALLOW_HEADS = {"python", "py", "pytest", "git", "npm", "npx", "mvn", "mvnw",
               "node", "docker", "java", "javac", "dir", "ls", "type", "echo", "cat"}

# 无论怎么组合都拒绝：网络、破坏、提权、包安装、git 危险子命令
DENY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"\b(curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|ftp)\b",
    r"\b(ssh|scp|sftp|telnet|nc|ncat|netcat)\b",
    r"\bpip\s+(install|uninstall)\b", r"\bnpm\s+(i|install|add)\b", r"\byarn\s+add\b",
    r"\b(choco|winget|scoop)\s+install\b",
    r"\b(rm|rmdir|rd|del)\b[^\n]*\b(-rf|/s|/q|/\-rf)\b",
    r"\bformat\b", r"\b(shutdown|reboot|restart-computer)\b",
    r"\b(reg|netsh|sc|taskkill|bcdedit|certutil|mshta|wmic)\b",
    r"\b(sudo|runas|Start-Process)\b",
    r"\b(powershell|pwsh|cmd\s+/c)\b",
    r"\bgit\s+(push|remote|clean|reset\s+--hard|filter-branch|gc)\b",
    r"\bgit\s+config\b",
    r"\+[a-z]{2,}://",          # 任何 URL
    r">\s*[A-Za-z]:\\",         # 重定向写到盘符根/任意绝对路径
]]

# 环境变量脱敏：子进程看不到凭据
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AWS_|AZURE_|OPENAI|ANTHROPIC|DEEPSEEK|HF_TOKEN)", re.I)

SAFE_ENV_KEYS = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
                 "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
                 "JAVA_HOME", "M2_HOME", "PYTHONPATH", "PYTHONIOENCODING",
                 "PYTHONDONTWRITEBYTECODE", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
                 "HOMEDRIVE", "HOMEPATH", "ProgramFiles", "ProgramFiles(x86)", "DOCKER_HOST"}

SENSITIVE_PATH = [re.compile(p, re.I) for p in [
    r"\.ssh", r"\.aws", r"\.gnupg", r"AppData[\\/]Roaming[\\/]npm", r"\\Windows\\System32",
    r"\.git[\\/]config$", r"id_rsa", r"\.env$", r"keystore", r"\.jks$", r"\.pem$", r"\.pfx$",
]]


@dataclass
class JournalEntry:
    token: str
    ts: str
    rel: str
    backup: str
    action: str


class Sandbox:
    """一个工作区一个沙箱实例。所有读写/命令都必须过它。"""

    def __init__(self, ws: Path, mode: str = "write"):
        self.ws = Path(ws).resolve()
        self.mode = mode if mode in ("readonly", "write") else "write"
        self.meta = self.ws / ".sixcoder"
        self.journal_dir = self.meta / "journal"
        self.journal_file = self.meta / "journal.jsonl"
        self.audit_file = self.meta / "audit.jsonl"
        self.meta.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- 路径牢笼
    def jail(self, rel: str, write: bool = False) -> Path:
        if write and self.mode == "readonly":
            raise SandboxError("沙箱处于只读模式，禁止写入")
        p = Path(rel)
        target = (self.ws / p).resolve() if not p.is_absolute() else p.resolve()
        if target != self.ws and self.ws not in target.parents:
            raise SandboxError(f"路径越界：{rel} 不在工作区 {self.ws} 内")
        for pat in SENSITIVE_PATH:
            if pat.search(str(target)):
                raise SandboxError(f"沙箱禁止访问敏感路径：{rel}")
        return target

    # -------------------------------------------------------------- 命令策略
    def check_cmd(self, cmd: str) -> str:
        if self.mode == "readonly" and not re.match(r"^(dir|ls|type|cat|echo|git (status|log|diff|ls-files|rev-parse|show))", cmd.strip()):
            raise SandboxError("沙箱处于只读模式，仅允许查看类命令")
        head = cmd.strip().split()
        if not head:
            raise SandboxError("空命令")
        exe = Path(head[0]).stem.lower()
        if exe not in ALLOW_HEADS:
            raise SandboxError(f"命令 `{head[0]}` 不在白名单 {sorted(ALLOW_HEADS)}")
        for pat in DENY_PATTERNS:
            if pat.search(cmd):
                raise SandboxError(f"命令被沙箱拒绝（匹配规则 {pat.pattern}）：{cmd[:80]}")
        return cmd

    def env(self) -> dict:
        """脱敏环境：只保留白名单键，丢弃一切凭据类变量。"""
        base = {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS and not SECRET_ENV.search(k)}
        base["PYTHONDONTWRITEBYTECODE"] = "1"
        base["PYTHONIOENCODING"] = "utf-8"
        return base

    # -------------------------------------------------------------- 备份 / 撤销
    def backup(self, path: Path, action: str = "write") -> str | None:
        if not path.exists():
            return None
        token = time.strftime("%H%M%S") + "-" + str(int(time.time() * 1000) % 100000)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, self.journal_dir / f"{token}__{path.name}")
        entry = JournalEntry(token=token, ts=time.strftime("%Y-%m-%d %H:%M:%S"),
                             rel=path.relative_to(self.ws).as_posix(),
                             backup=f"{token}__{path.name}", action=action)
        with self.journal_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        return token

    def journal(self) -> list[dict]:
        if not self.journal_file.exists():
            return []
        out = []
        for line in self.journal_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def undo(self, steps: int = 1) -> list[str]:
        """按时间倒序恢复文件（沙箱级撤销，独立于 git）。"""
        entries = self.journal()[-steps:][::-1]
        done = []
        for e in entries:
            src = self.journal_dir / e["backup"]
            dst = self.ws / e["rel"]
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                done.append(e["rel"])
        return done

    # -------------------------------------------------------------- 审计
    def audit(self, action: str, detail: str, ok: bool = True) -> None:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
               "ok": ok, "detail": detail[:500]}
        try:
            with self.audit_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def describe(self) -> dict:
        return {"workspace": str(self.ws), "mode": self.mode,
                "allow_heads": sorted(ALLOW_HEADS),
                "deny_rules": len(DENY_PATTERNS),
                "journal_entries": len(self.journal()),
                "boundary": "进程内策略沙箱（非 OS 级隔离）"}
