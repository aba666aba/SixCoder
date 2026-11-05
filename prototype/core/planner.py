"""规划器：把「需求 → 节点计划」里的不确定性拿走。

背景（实测）：同一个 4 模块需求，3B 四次跑出 2/2/8/2 个节点的计划——
它会把"必须覆盖 models/storage/service/cli"这条规则丢掉。

设计原则（对齐 docs/12「能确定就别猜」）：
1. **模块清单从需求里正则抽取**（`xxx.py` 是确定信息，不需要模型判断）
2. **每个模块强制一对节点**（test 先、impl 后），节点集合由引擎决定
3. **契约按模块切片**：把需求里提到该模块的那几句原文直接给它（比 3B 复述准）
4. 模型仍参与，但只做"补充"：追加需求没写到的模块，或给出更细的契约点
5. 依赖排序：被别的模块提到越多 → 越基础 → 越先做（拓扑序，环则按出现顺序）
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_NODES = 12

# 需求里出现的模块路径：models.py / ledger/storage.py / Main.java / ring.c / app.ts
# **多语言**：契约解析不绑定 Python —— 换语言时只需要换验证后端（见 core/lang.py）。
MODULE_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|java|cpp|cc|hpp|cxx|jsx|tsx|mjs|cjs|js|ts|go|cs|"
    r"rs|kt|swift|php|rb|cc|c|h))\b")
# 这些是"测试/配置"文件，不是被实现的模块（需求里常会把 test_*.py 也写出来）
NON_MODULE = re.compile(r"^(test_.*|.*_test|conftest|setup|__init__)\.py$")
# 模块级要求的分句
SPLIT_RE = re.compile(r"[。；\n]+")
# 常见"基础模块"关键词（表结构/模型最基础，cli 最上层）
BASE_HINTS = ("model", "entity", "schema", "types", "storage", "db", "store")
TOP_HINTS = ("cli", "main", "app", "command", "router", "api")


@dataclass
class ModuleSpec:
    path: str
    name: str
    contract: str = ""
    mentions: int = 0                 # 被其他模块的分句提及次数（越大越基础）
    deps: set[str] = field(default_factory=set)
    order: int = 0

    @property
    def test_path(self) -> str:
        p = Path(self.path)
        return str(p.with_name(f"test_{p.stem}.py"))



HEADER_RE = re.compile(
    r"^\s*(?:\d+[.)、]|[-*])\s+([\w./-]+\.(?:py|java|c|h|cpp|cc|hpp|js|mjs|cjs|ts|go|cs|rs|"
    r"kt|swift|php|rb))\s*[：:]?(.*)$")


def _module_blocks(text: str) -> dict[str, str]:
    """按"模块标题 + 其下缩进正文"收集每个模块的完整契约。

    支持两种写法：
      1) store.py：JSON 持久化 ……        （标题行自带正文）
      2) clean.py：
         - normalize_age(x: str) -> int：……
         - parse_date(s: str) -> str：……
    遇到下一个模块标题即结束上一个块。
    """
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for raw in (text or "").splitlines():
        m = HEADER_RE.match(raw)
        if m:
            path = m.group(1).lstrip("./")
            cur = path
            out.setdefault(cur, [])
            head = (m.group(2) or "").strip(" ：:")
            if head:
                out[cur].append(head)
            continue
        if cur is None or not raw.strip():
            continue
        line = raw.strip(" -\t")
        # 需求级段落（示例/要求/注意/每个模块）不属于任何模块，遇到即结束当前块；
        # 否则最后一个模块会把整段示例都吞进契约（实测 report.py 里出现了 clean 的函数名）
        if re.match(r"^(示例|例子|要求|注意|必须|每个模块)", line):
            cur = None
            continue
        out[cur].append(line)
    return {k: "；".join(v).strip("；") for k, v in out.items()}


def extract_modules(requirement: str) -> list[ModuleSpec]:
    """从需求里抽出模块清单 + 每个模块的契约切片。"""
    text = requirement or ""
    blocks = _module_blocks(text)
    seen: dict[str, ModuleSpec] = {}
    for m in MODULE_RE.finditer(text):
        path = m.group(1).lstrip("./")
        if NON_MODULE.match(Path(path).name):
            continue                      # 测试/配置文件不是模块
        if path not in seen:
            seen[path] = ModuleSpec(path=path, name=Path(path).stem, order=len(seen))
    if not seen:
        return []

    # 按分句切片：优先取"以该模块开头"的分句（如 `2) storage.py：…`），
    # 避免把别的模块那一行串进来（实测 service 的句子被算进 storage 的契约）。
    segments = [s.strip() for s in SPLIT_RE.split(text) if s.strip()]
    for spec in seen.values():
        keys = {spec.path, spec.name}
        primary: list[str] = []
        fallback: list[str] = []
        for seg in segments:
            hit_pos = min([seg.find(k) for k in keys if k and k in seg] or [-1])
            if hit_pos < 0:
                continue
            head = seg[:hit_pos]
            # 分句开头 12 字符内出现模块名（允许 "2)" "1）" "- " 之类前缀）→ 主契约
            if len(head.strip()) <= 12 and not any(x.path in head for x in seen.values() if x is not spec):
                primary.append(seg)
            else:
                fallback.append(seg)
        picked = (primary[:2] or fallback[:1])
        seg_contract = "；".join(picked).strip("；")
        body = blocks.get(spec.path) or blocks.get(spec.name + ".py") or ""
        # 标题块正文更完整（分句切法常只剩标题）→ 以它为准
        if len(body) > len(seg_contract):
            spec.contract = f"{spec.path}：" + body
        else:
            spec.contract = seg_contract

    # 依赖/基础度：一个模块被多少个"其他模块的分句"提到
    for spec in seen.values():
        cnt = 0
        for other in seen.values():
            if other.path == spec.path:
                continue
            for k in (spec.name, spec.path):
                if k and k in other.contract:
                    cnt += 1
                    break
        spec.mentions = cnt

    return list(seen.values())


def order_modules(modules: list[ModuleSpec]) -> list[ModuleSpec]:
    """基础模块优先：被引用多 → 先做；模型/存储类优先；cli 类最后。"""
    def key(m: ModuleSpec):
        low = m.name.lower()
        base = 0 if any(h in low for h in BASE_HINTS) else 1
        top = 1 if any(h in low for h in TOP_HINTS) else 0
        return (base, top, -m.mentions, m.order)

    return sorted(modules, key=key)


def build_skeleton(modules: list[ModuleSpec], exists: set[str]) -> list[dict]:
    """引擎决定节点集合：每个模块一对（test 先、impl 后）。"""
    nodes: list[dict] = []
    for m in order_modules(modules):
        nodes.append({"kind": "test", "path": m.test_path,
                      "mode": "patch" if m.test_path in exists else "whole",
                      "contract": m.contract, "module": m.path, "source": "engine"})
        nodes.append({"kind": "impl", "path": m.path,
                      "mode": "patch" if m.path in exists else "whole",
                      "contract": m.contract, "module": m.path, "source": "engine"})
    return nodes


def merge_model_plan(skeleton: list[dict], model_plan: list[dict],
                     max_nodes: int = MAX_NODES) -> tuple[list[dict], list[str]]:
    """骨架为准；模型只能补充，不能删减或替换。"""
    notes: list[str] = []
    by_path = {n["path"]: n for n in skeleton}

    for raw in model_plan or []:
        path = str(raw.get("path") or "").strip().replace("\\", "/").lstrip("./")
        if not path:
            continue
        kind = "test" if "test" in str(raw.get("kind", "")).lower() else "impl"
        if path in by_path:
            node = by_path[path]
            if node["kind"] != kind:
                continue                    # 同一文件不同角色，忽略模型这条
            detail = str(raw.get("contract") or "").strip()
            if detail and detail not in node["contract"]:
                node["contract"] = (node["contract"] + "；补充：" + detail) if node["contract"] else detail
                notes.append(f"{path}: 采纳模型的补充契约")
        else:
            # 需求没点名的模块：引擎不再无条件接受模型追加（实测会追加 test_xxx.py 这种脏节点）。
            # 只接受"看起来是被实现模块"的路径，且数量受限。
            if NON_MODULE.match(Path(path).name):
                notes.append(f"{path}: 忽略模型追加的测试/配置节点")
                continue
            if len([x for x in skeleton if x.get("source") == "model"]) >= 4:
                continue
            skeleton.append({"kind": kind, "path": path,
                             "mode": "whole" if str(raw.get("mode", "")).lower().startswith("whole") else "patch",
                             "contract": str(raw.get("contract") or "").strip(),
                             "module": path, "source": "model"})
            notes.append(f"{path}: 模型追加的节点（需求未点名）")

    # 依赖顺序：test/impl 成对相邻，基础模块在前
    order = {n["path"]: i for i, n in enumerate(skeleton)}
    seen, ordered = set(), []
    for n in skeleton:
        if n["path"] in seen:
            continue
        pair = [x for x in skeleton if x["path"] == n["path"]]
        for x in sorted(pair, key=lambda y: 0 if y["kind"] == "test" else 1):
            if x["path"] not in seen:
                ordered.append(x)
        seen.add(n["path"])
    return ordered[:max_nodes], notes


def sanitize(nodes: list[dict], required: list[ModuleSpec]) -> tuple[list[dict], list[str]]:
    """计划清洗：去掉非法/重复节点，保证 (path,kind) 唯一且角色正确。"""
    notes: list[str] = []
    required_paths = {m.path for m in required} | {m.test_path for m in required}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for n in nodes:
        path, kind = n["path"], n["kind"]
        base = Path(path).name
        if kind == "impl" and NON_MODULE.match(base):
            notes.append(f"丢弃实现节点（路径是测试/配置文件）：{path}")
            continue
        if kind == "test" and not base.startswith("test_"):
            notes.append(f"丢弃测试节点（不在 test_*.py 上）：{path}")
            continue
        if (path, kind) in seen:
            notes.append(f"丢弃重复节点：{kind} {path}")
            continue
        seen.add((path, kind))
        out.append(n)
    return out, notes


def validate(nodes: list[dict], required: list[ModuleSpec]) -> list[str]:
    """计划闸门：模块覆盖必须完整，角色必须成对。"""
    issues: list[str] = []
    have = {(n["path"], n["kind"]) for n in nodes}
    for m in required:
        if (m.path, "impl") not in have:
            issues.append(f"缺少 {m.path} 的实现节点")
        if (m.test_path, "test") not in have:
            issues.append(f"缺少 {m.test_path} 的测试节点")
    for n in nodes:
        if n["kind"] == "impl" and n["path"].startswith("test_"):
            issues.append(f"实现节点落在了测试文件上：{n['path']}")
        if n["kind"] == "test" and not Path(n["path"]).stem.startswith("test_"):
            issues.append(f"测试节点不在 test_*.py 上：{n['path']}")
    return issues


def api_surface(ws: Path, exclude: str = "", limit_chars: int = 700) -> str:
    """已有模块的公开签名（给新建模块参考，避免 import 不存在的符号）。"""
    rows: list[str] = []
    for p in sorted(Path(ws).glob("*.py")):
        rel = p.name
        if rel == exclude or rel.startswith("test_"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        items = []
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"):
                args = [a.arg for a in n.args.args if a.arg not in ("self", "cls")]
                items.append(f"{n.name}({', '.join(args)})")
            elif isinstance(n, ast.ClassDef) and not n.name.startswith("_"):
                methods = [m.name for m in n.body if isinstance(m, ast.FunctionDef)
                           and not m.name.startswith("_")]
                items.append(f"class {n.name}({', '.join(methods[:4]) or '…'})")
        if items:
            rows.append(f"{Path(rel).stem}: " + ", ".join(items))
    text = "\n".join(rows)
    return text[:limit_chars] if text else "（仓库里还没有其他模块）"
