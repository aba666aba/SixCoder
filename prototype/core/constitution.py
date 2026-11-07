"""项目宪法：目录/命名/分层/错误/测试/铁律 的**版本化稳定前缀**（开发文档 §18.7 第 5 件）。

为什么它值一个模块：
    1) 一致性 —— 33,000 个函数要长成一个风格，靠"每片提示词各写一遍约定"必然漂移；
       宪法是**唯一**约定源，逐字进每一片简报，模型看到的是同一份。
    2) 前缀缓存 —— 远端 API 按"prompt 前缀"计缓存命中：命中 ¥0.02/M，未命中 ¥1/M（50 倍）。
       所以宪法必须**字节稳定**（无时间戳、无任务 id、无轮次），放在 prompt 最前，
       才能让整仓的调用都命中同一段前缀。

两个入口，语义分开（与 layout.py 的分工一致）：
    · ensure(ws)   —— 写 .sixcoder/constitution.md（不存在才写；已存在绝不覆盖，改约定升版本）
    · prefix(ws)   —— 读宪法渲染"稳定前缀块"给作者提示词；文件缺失时用**默认文本**注入
                     （不落盘、不改动工作区 —— 读路径不许写盘）。

诚实边界：这是**通用**六项约定（目录/命名/分层/错误/测试/铁律）。Web 项目专属约定
（路由形状、鉴权、幂等）由 shipgate 的机制能力注入，不混进这份跨项目通约。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

CONSTITUTION_FILE = ".sixcoder/constitution.md"
VERSION = 1

# 默认文本必须字节稳定：没有日期、没有任务 id、没有轮次。改它要升 VERSION。
_DEFAULT = """# 项目宪法（全项目唯一约定，任何一轮都必须遵守）

## 1. 目录
- 入口脚本（main.py / app/main.py）只做接线：解析参数 → 调服务函数 → 打印 / 退出码。
- 业务逻辑落在按职责命名的模块（models / store / service / api / cli）。
- 测试放 tests/，与源文件同名对应（test_<模块>.py）。

## 2. 命名
- 模块 / 函数 / 变量用 snake_case；类用 PascalCase；常量用 UPPER_SNAKE_CASE。
- 测试文件 test_*.py；测试函数 test_<行为>。

## 3. 分层（只向下依赖，禁止环）
- 数据层（models / store）→ 算法层（service / matching）→ 编排层（api / cli / main）→ 展示层。
- 上层可以 import 下层；同层不互相 import；禁止下层 import 上层。

## 4. 错误处理
- 非法输入抛 ValueError（带中文原因）；资源不存在抛 KeyError 或自定义异常。
- 禁止裸 except: pass 吞异常；禁止残留 print 调试输出。

## 5. 测试
- 一个测试函数验一个行为；断言真实返回值，不写 assert True 空壳。
- 不改契约声明的签名；不删既有测试；不加 skip 逃避验收。

## 6. 铁律
- 一次只改本片声明的模块；不动其它模块的对外签名。
- 不新增第三方依赖（除非契约 / 简报明说）；不硬编码密钥。
"""

_PREFIX_HEAD = "【项目宪法（全项目唯一约定，字节稳定，任何一轮都必须遵守）】\n"
_PREFIX_TAIL = "\n【宪法结束】\n"


def default_text() -> str:
    return _DEFAULT


def ensure(ws: Path) -> dict:
    """写宪法（幂等）。已存在绝不覆盖 —— 改约定是升版本号，不是原地改写。

    返回 {created, path, version, sha256}。"""
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "constitution.md"
    created = False
    if not p.exists():
        p.write_text(_DEFAULT, encoding="utf-8")
        created = True
    return {"created": created, "path": str(p), "version": VERSION,
            "sha256": sha(ws)}


def sha(ws: Path) -> str:
    """宪法内容哈希（治理锁定用：被改 / 被删都能发现）。"""
    p = Path(ws) / CONSTITUTION_FILE
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else _DEFAULT
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def prefix(ws: Path) -> str:
    """稳定前缀块（给作者提示词最前面）。文件缺失时用默认文本注入，不落盘。"""
    p = Path(ws) / CONSTITUTION_FILE
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else _DEFAULT
    return _PREFIX_HEAD + text.rstrip() + _PREFIX_TAIL


# 易变内容检测（时间戳 / 日期 / 任务 id / UUID）：前缀里出现这些，缓存就废了
_VOLATILE_RE = re.compile(r"\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|task[-_]?id|"
                          r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                          re.I)


def volatile_hits(ws: Path) -> list[str]:
    """宪法里有没有易变内容（有 = 缓存失效）。空 = 干净。"""
    p = Path(ws) / CONSTITUTION_FILE
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else _DEFAULT
    return _VOLATILE_RE.findall(text)
