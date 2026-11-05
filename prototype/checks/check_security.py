r"""安全与依赖闸门自检：高危模式必须红，正常代码不许误杀，做不到的事不许假装做过。

反例（都来自"生成代码里最容易出现的真问题"）：
  eval/exec、subprocess(shell=True)、硬编码密钥、yaml.load、pickle.loads、verify=False
正例：从环境变量读密钥、正常 subprocess 列表参数、普通业务代码。
依赖：未锁版本 → 红；锁死版本 → 绿；没有清单 → 如实跳过（不写"通过"）。

跑法: python check_security.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import gates  # noqa: E402

FAILS: list[str] = []

BAD = '''
import os, pickle, subprocess, yaml, requests

API_KEY = "sk-fake-test-key-0004"        # 硬编码密钥（假值，仅用于测检测规则；≥12 字符才会被 SECRET_RE 命中）

def run(cmd):
    os.system(cmd)                             # shell
    subprocess.run(cmd, shell=True)            # shell=True
    return eval(cmd)                           # 代码注入

def load(blob, text):
    pickle.loads(blob)                         # 不安全反序列化
    yaml.load(text)                            # 无 Loader
    return requests.get("https://x", verify=False)   # 关 TLS 校验
'''

GOOD = '''
import os, subprocess, yaml

API_KEY = os.environ.get("API_KEY", "")        # 从环境读，不是字面量

def run(cmd):
    return subprocess.run([cmd], capture_output=True, text=True)   # 列表参数，无 shell

def load(text):
    return yaml.safe_load(text)                                    # safe_load

def add(a: int, b: int) -> int:
    return a + b
'''


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="sec_"))
    (ws / "bad.py").write_text(BAD, encoding="utf-8")
    (ws / "good.py").write_text(GOOD, encoding="utf-8")

    print("[1] 安全闸门：高危必红")
    r = gates.gate_security(ws, ["bad.py"])
    check("坏文件必须判红", not r.ok, r.detail[:120])
    for kw, label in (("eval", "eval"), ("shell=True", "shell=True"),
                      ("硬编码密钥", "硬编码密钥"), ("pickle.loads", "反序列化"),
                      ("yaml.load", "yaml.load"), ("verify=False", "TLD 校验")):
        check(f"报出 {label}", kw in (r.detail or ""), r.detail[:200])
    check("证据类别是 static_rule", r.evidence_kind == "static_rule", r.evidence_kind)

    print("[2] 正常代码不许误杀")
    r2 = gates.gate_security(ws, ["good.py"])
    check("好文件必须判绿", r2.ok, r2.detail[:160])
    check("如实标注没装 bandit 时只跑内置规则", "仅内置规则" in r2.detail or "bandit" in r2.detail,
          r2.detail)

    print("[3] 依赖卫生")
    (ws / "requirements.txt").write_text("requests>=2.0\nflask\nnumpy==1.26.4\n",
                                         encoding="utf-8")
    d = gates.gate_deps(ws)
    check("未锁版本必须判红", not d.ok, d.detail[:160])
    check("点名 requests 的范围约束", "requests" in (d.detail or ""), d.detail[:200])
    check("点名 flask 没锁版本", "flask" in (d.detail or ""), d.detail[:200])
    (ws / "requirements.txt").write_text("requests==2.32.3\nnumpy==1.26.4\n", encoding="utf-8")
    d2 = gates.gate_deps(ws)
    check("全部锁死版本 → 绿", d2.ok, d2.detail[:160])
    check("没做漏洞扫描就如实说明", "未做漏洞扫描" in d2.detail or "pip-audit" in d2.detail,
          d2.detail)
    empty = Path(tempfile.mkdtemp(prefix="sec_empty_"))
    d3 = gates.gate_deps(empty)
    check("没有清单时如实跳过（不写通过）",
          d3.ok and d3.evidence_ref == "no-manifest", d3.evidence_ref + " / " + d3.detail)

    print("[4] 字段一致性（歧义探测，不阻塞）")
    (ws / "clean.py").write_text(
        "def clean_row(row):\n"
        "    row = dict(row)\n"
        "    row['age'] = int(row['age'])\n"
        "    if 'date' in row:\n"
        "        row['date'] = str(row['date'])\n"
        "    return row\n", encoding="utf-8")
    issues = gates.gate_field_map(ws, ["clean.py"], ["id", "name", "age", "email", "birth"])
    check("date 不在表列里 → 必须报歧义", any("date" in x for x in issues), str(issues))
    check("不阻塞（GateResult 永远 ok）",
          gates.gate_field_map_result(ws, ["clean.py"],
                                      "建表 people(id INTEGER PRIMARY KEY, name TEXT, "
                                      "age INTEGER, email TEXT, birth TEXT)").ok)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（高危必红、正常不误杀、依赖不可复现必报、做不到的事如实说明）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
