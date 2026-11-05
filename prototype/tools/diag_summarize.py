"""诊断：为什么 summarize 这类"返回 dict 字面量"的函数反复生成不出可解析代码。

做法：复制一份工作区，按夜班的真实提示词调用一次本地模型，把**原始输出**打出来，
再看 symedit 的解析器在哪里炸。不猜，只看真东西。

用法：python tools/diag_summarize.py [工作区] [模型]
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent))

from core import contract as cm  # noqa: E402
from core import iterate, ollama, workorder  # noqa: E402

ws_src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent.parent / "projects" / "night6"
model = sys.argv[2] if len(sys.argv) > 2 else "qwen2.5:7b"
req = (HERE.parent / "fixtures" / "hq_requirement.txt").read_text(encoding="utf-8")

tmp = Path(tempfile.mkdtemp(prefix="diag_sum_"))
for f in ws_src.glob("*.py"):
    shutil.copy(f, tmp / f.name)

contracts = cm.parse_requirement(req)
order = {"kind": "implement", "target": "report.py", "symbol": "summarize",
         "acceptance": "契约判据通过（summarize）",
         "detail": "按契约实现 summarize：返回包含 count / avg_age / domains 的字典"}

# 拦下模型原始输出，其余走真实代码路径
raw = {}
_real_chat = ollama.chat


def spy(messages, model_, **kw):
    res = _real_chat(messages, model_, **kw)
    raw["prompt"] = messages[-1]["content"] if messages else ""
    raw["text"] = res.get("text") or ""
    raw["tokens"] = res.get("eval_count")
    return res


ollama.chat = spy
try:
    r = iterate.execute_local(tmp, order, model, attempts=1)
finally:
    ollama.chat = _real_chat

print("=== 执行结果 ===")
print(json.dumps(r, ensure_ascii=False)[:500])
print("\n=== 模型原始输出（前 1200 字符）===")
print(raw.get("text", "（没有调用模型）")[:1200])
print("\n=== 结束状态 ===")
print("report.py 现状：")
print((tmp / "report.py").read_text(encoding="utf-8")[:400])
print(f"\n临时工作区：{tmp}")
