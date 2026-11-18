"""契约的**通用约束段**（单一实现）。

背景（2026-11-15 用户定稿）：
> SixCoder 的架构不够通用，契约改一下，尽量让远端先写，本地补齐各种工程的安全措施诊断。

之前契约里混着**框架形态**的要求（"凭据参数必须叫 authorization"、"请求体参数叫 body"、
"函数名要含路径末段"）—— 那些是**接线事实**，属于本地该补的东西。把它们写进契约有两个坏处：
1. 换栈就作废（FastAPI 的 body / Django 的 request 不是一回事）；
2. 远端为了满足这些形式要求，会把业务函数写成"长得像视图"的东西 →
   业务逻辑与传输层耦合，本地再想适配别的栈就得重写。

新口径：**契约只描述领域事实**（有哪些操作、输入什么、输出什么、什么情况算违反），
**形态交给本地**：
   远端先写  = 纯业务函数（纯数据进、可 JSON 出的数据出、错误用 ValueError）
   本地补齐  = import / 路由 / 适配视图 / 序列化 / 错误码映射 / 鉴权取参 / 全部诊断（D1–D8）

这也是"本地是远端的放大器"的另一种形态：**本地做的事越多，远端要猜的越少**。
"""
from __future__ import annotations

# 幂等标记：**两种都要认**。旧契约里是"接线约束（引擎要求"，新的是"契约约束（引擎要求"
# —— 只认一种的话，老契约重跑会重复追加整段（实测 2026-11-15 幂等断言红）。
MARKS = ("契约约束（引擎要求", "接线约束（引擎要求")

# ── ① 与栈无关的部分（任何项目都适用）────────────────────────────────
GENERIC_RULES = """\
# 契约约束（引擎要求，必须遵守）

## 一、你只写**业务实现**，工程脚手架由引擎本地补齐
- **不要**写路由注册、入口文件、settings/配置、`if __name__ == "__main__"`。
- **不要** import 任何 Web 框架（fastapi / django / flask / starlette …）。
  业务函数只用**标准库** + 项目内领域模块；框架 import 由引擎按目标栈补。
- **不要**写"防御性样板"（包天 try/except、重试、日志、指标）。
  只写领域判断；异常处理、错误码映射、日志、鉴权取参都是引擎的事。

## 二、函数的输入输出是**纯数据**
- 输入：`dict / list / str / int / float / bool / None`（可以嵌套）。
- 输出：同上，**必须能直接 JSON 序列化**。不要返回 ORM 对象、不要返回框架的 Response。
- 需要持久化时，返回"要写什么"的纯数据，由引擎/仓储层落地。

## 三、错误这样表达（引擎负责翻译成 HTTP）
- 业务规则不满足 → `raise ValueError("人可读的原因")` → 引擎映射 **422**。
- 未认证 / 无权限 → `raise PermissionError("原因")` → 引擎映射 **401**（越权用 403 的语义可在文案里写清）。
- 找不到实体 → `raise LookupError("...不存在")` → 引擎映射 **404**。
- **不要** `raise HTTPException(...)`；**不要**在返回值里塞 `status` 字段 —— 状态码是传输层的事。
  （引擎的适配层也兼容 `{"status": 201, ...}` 这种历史写法：会取出来当状态码并从 body 摘掉，
  但新契约不该再这么写。）

## 四、凭据只以字符串形式进入业务函数
- 已认证身份如果需要，**只从参数 `authorization: str` 拿**（这是唯一的传输层事实）。
- 需要用户对象时，由函数自己用凭据解析（查库/解 token），**不要**要求调用方传对象。

## 五、命名与语义以契约为准
- 表名、字段名、枚举值、错误文案的口径**逐字**照契约，不要另起同义词
  （引擎用它们做跨模块一致性诊断：字段名对不上会被判成真缺陷）。
- 每个函数写一段 docstring：**做什么 + 边界（什么情况拒绝/返回空）**。
  引擎按 docstring 与契约对账，缺了会被判"语义不明"。
"""

# ── ② 与栈有关的**补充事实**（只在必要时追加一小段）──────────────────
STACK_NOTES = {
    "fastapi": """\
## 六、目标栈补充（FastAPI，只影响接线，不影响你写的函数）
- 引擎会把业务函数接到 `@router.<verb>(path)` 上；请求体整体作为 `body` 传入。
- 返回的 dict/list 由引擎包成 JSON 响应；`ValueError` 由引擎映射为 422。
""",
    "django": """\
## 六、目标栈补充（Django，只影响接线，不影响你写的函数）
- 路由由引擎写进各 `apps/<域>/urls.py` 的 `path()`；业务函数**不要**自己接路由。
- 请求体整体作为 `body` 传入，路径参数按契约名字传入（如 `topic_id: int`）。
- 返回的 dict/list 由引擎包成 `JsonResponse`；`ValueError` 由引擎映射为 422。
- **`models.py` 例外**：模型定义必须 `from django.db import models`（那是数据层，不是传输层）。
""",
}


def block(stack_name: str = "", *, with_stack_note: bool = True) -> str:
    """生成要追加到契约末尾的约束段（幂等由调用方用 `MARK` 判断）。"""
    text = GENERIC_RULES
    note = STACK_NOTES.get(str(stack_name or "").strip().lower(), "")
    if with_stack_note and note:
        text = text + "\n" + note
    return "\n" + text


def is_present(text: str) -> bool:
    """契约里是否已经带了约束段（新旧两种标题都算）。"""
    t = text or ""
    return any(m in t for m in MARKS)


def apply(text: str, stack_name: str = "") -> str:
    """把约束段追加到契约文本（幂等）。"""
    if is_present(text):
        return text
    return (text or "") + block(stack_name)


def describe() -> str:
    return "契约约束段：%d 条通用规则 + %d 个栈补充" % (
        GENERIC_RULES.count("\n- "), len(STACK_NOTES))
