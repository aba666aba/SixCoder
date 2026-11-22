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
    # 备份保留策略（2026-11-20 用户口径，两轮定稿）：
    #   第一次问"会不会爆炸"→ 实测同一文件存 30 次 = 29 个完整副本，且**原来没有清理**；
    #   第二次定策略 → **只留 3 份**（能撤销三次），第 4 次起循环覆盖最老的那份。
    # 为什么不留更多：撤销只需要"我刚才那几步"，再老的历史该由 git 负责；
    # 而每个副本都是**整文件拷贝**，80KB 的文件每天存 50 次就是 4MB/天、约 1GB/年。
    KEEP_PER_FILE = 3
    MAX_TOTAL_FILES = 200        # 多文件仓库的兜底上限（每文件 3 份 × 几十个文件）
    MAX_TOTAL_MB = 100           # 总量兜底

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
        self.prune_journal()
        return token

    def prune_journal(self) -> dict:
        """按保留策略清理旧备份（每个文件留最近 KEEP_PER_FILE 份，循环覆盖最老的）。

        返回 {removed, kept, freed_bytes}（自检与界面用）。
        只删备份文件与对应 journal 条目，**不动工作区里的源码**。
        undo 只回放最近几步（用户口径：能撤销三次），所以砍掉更老的副本不影响撤销。
        """
        entries = self.journal()
        if len(entries) <= self.KEEP_PER_FILE:
            return {"removed": 0, "kept": len(entries), "freed_bytes": 0}

        # 1) 每个文件只留最近 KEEP_PER_FILE 份（token 前缀是时分秒，字符串序 = 时间序）
        by_rel: dict[str, list[dict]] = {}
        for e in entries:
            by_rel.setdefault(str(e.get("rel") or ""), []).append(e)
        survivors: list[dict] = []
        for es in by_rel.values():
            es.sort(key=lambda x: str(x.get("token") or ""))
            survivors.extend(es[-self.KEEP_PER_FILE:])

        # 2) 全局数量 / 体积上限：从最新往回数，超额的老副本淘汰
        survivors.sort(key=lambda x: str(x.get("token") or ""))
        limit_bytes = self.MAX_TOTAL_MB * 1024 * 1024
        kept: list[dict] = []
        total = 0
        for e in reversed(survivors):
            f = self.journal_dir / str(e.get("backup") or "")
            try:
                sz = f.stat().st_size
            except OSError:
                continue                                  # 文件已不在，条目顺手丢掉
            if len(kept) >= self.MAX_TOTAL_FILES or total + sz > limit_bytes:
                break
            kept.append(e)
            total += sz
        keep_names = {str(e.get("backup") or "") for e in kept}

        # 3) 删多余副本（先删文件，再重写 journal，保证条目不会指向不存在的文件）
        removed = 0
        freed = 0
        try:
            for f in self.journal_dir.iterdir():
                if f.is_file() and f.name not in keep_names:
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        try:
            kept.sort(key=lambda x: str(x.get("token") or ""))
            tmp = self.journal_file.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for e in kept:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            tmp.replace(self.journal_file)
        except OSError:
            pass
        return {"removed": removed, "kept": len(kept), "freed_bytes": freed}

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
        """按时间倒序恢复文件（沙箱级撤销，独立于 git）。

        **撤一次就消耗一份备份**（2026-11-20 用户口径："只能撤销三次，循环覆盖原有备份"）：
        原来只回放不消费，连按 4 次 Ctrl+Z 都会回到同一个版本，第 2 次起等于没撤；
        现在恢复后把用掉的那份备份连同 journal 条目一起删掉，连撤三次就依次回到
        三个历史点，第 4 次如实返回空（而不是假装又撤了一步）。
        """
        entries = self.journal()
        if not entries:
            return []
        take = entries[-max(1, steps):][::-1]
        done: list[str] = []
        used: set[str] = set()
        for e in take:
            name = str(e.get("backup") or "")
            src = self.journal_dir / name
            dst = self.ws / str(e.get("rel") or "")
            if not name or not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            done.append(str(e.get("rel") or ""))
            used.add(name)
        if used:
            for name in used:
                try:
                    (self.journal_dir / name).unlink()
                except OSError:
                    pass
            keep = [e for e in entries if str(e.get("backup") or "") not in used]
            try:
                tmp = self.journal_file.with_suffix(".jsonl.tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for e in keep:
                        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
                tmp.replace(self.journal_file)
            except OSError:
                pass
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
