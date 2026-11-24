"""apikb 自检：索引 stdlib 子集，验证"能查签名"和"能抓幻觉 API"。

跑法： python check_apikb.py    （全部通过 exit=0）
"""
from __future__ import annotations

import sys
import sysconfig
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import apikb

STDLIB = Path(sysconfig.get_paths()["stdlib"])   # 换机器/换解释器都能用


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    tmp = Path(tempfile.mkdtemp()) / "api_index.db"
    st = apikb.build([str(STDLIB)], tmp)
    print(f"索引: {st}")
    if st["files"] == 0:
        # 内置 Python 是嵌入式发行版：标准库打进 python3xx.zip，`Lib/` 下只剩
        # 第三方子目录（site-packages 被 apikb.SKIP 排除）→ os.walk 拿不到标准库 .py。
        # 这不是 apikb 的缺陷，更不是产物错；产品里 apikb 索引的是**项目 venv 的
        # site-packages**（有真实 .py），不受影响（2026-11-23 查明）。
        print("SKIP：本解释器标准库以 zip 分发（无 .py 源文件），标准库索引不适用。")
        print("      换普通 CPython 或对项目依赖建索引时本自检有效（不算通过，只是没覆盖）。")
        return 0
    ok &= check("索引构建成功", st["api_total"] > 500, str(st))
    ok &= check("耗时在可接受范围（<60s）", st["seconds"] < 60, str(st["seconds"]))

    kb = apikb.ApiKB(tmp)

    # 1) 查签名：冷门 API 也要能查到，而且给出的 import 路径必须真的能用
    def usable(row) -> bool:
        module, name = row[0], row[1].split(".")[0]
        try:
            import importlib
            return hasattr(importlib.import_module(module), name)
        except Exception:                            # noqa: BLE001
            return False

    for name in ("compare_digest", "groupby", "sha256", "dedent", "choice"):
        rows = kb.lookup(name)
        ok &= check(f"lookup({name}) 给出可用的 import 路径",
                    any(usable(r) for r in rows),
                    str([(r[0], r[1], r[2]) for r in rows[:3]]))
    ok &= check("C 扩展符号有运行期兜底（math.factorial，无 .py 源文件）",
                any(r[0] == "math" for r in kb.lookup("factorial")),
                str([(r[0], r[1], r[2]) for r in kb.lookup("factorial")[:3]]))
    ok &= check("拼错名字给近似建议", bool(kb.lookup("compare_digets")), str(kb.lookup("compare_digets")[:1]))

    # 2) 从需求文本回填真实签名
    block = kb.block_for("用 hashlib.sha256 计算摘要，再用 hmac.compare_digest 校验，同时支持 json.dumps")
    ok &= check("需求文本 → 签名事实块", "compare_digest" in block and "from" in block, block[:200])
    print("    回填示例:\n" + "\n".join("      " + ln for ln in block.splitlines()[:4]))

    # 3) 抓幻觉 API（这是"冷门 API 短板"的主要死法）
    bad = '''
from json import loads_
import sqlite3

def f(path):
    conn = sqlite3.connectx(path)
    return loads_("{}")
'''
    fs = apikb.verify_source(bad, kb)
    kinds = {f.kind for f in fs}
    ok &= check("抓到不存在的导入名", "missing_name" in kinds, str([str(f) for f in fs]))
    # 属性判定只对"无源码的纯 C 模块"生效（避免 datetime.strptime 这类误报），
    # 所以 sqlite3 的属性幻觉现由运行时测试兜底；这里改成验证纯 C 模块能被抓到
    c_mod = '''
import math

def g():
    return math.factorialx(3)
'''
    fs_c = apikb.verify_source(c_mod, kb)
    ok &= check("抓到纯 C 模块的属性幻觉",
                any(f.kind == "missing_attr" for f in fs_c), str([str(f) for f in fs_c]))
    # 回归守卫：有源码模块的运行期成员（datetime.strptime）不能被误报
    dt_mod = '''
import datetime

def h(s):
    return datetime.strptime(s, '%Y')
'''
    fs_dt = apikb.verify_source(dt_mod, kb)
    ok &= check("不误报 datetime.strptime", not fs_dt, str([str(f) for f in fs_dt]))
    # 回归守卫：用了模块却没 import 必须抓到
    miss_mod = '''
def k(s):
    return datetime.strptime(s, '%Y')
'''
    fs_miss = apikb.verify_source(miss_mod, kb)
    ok &= check("抓到忘记 import 的模块",
                any(f.kind == "missing_import" for f in fs_miss), str([str(f) for f in fs_miss]))
    ok &= check("给出接近的正确写法", any(f.suggestion == "loads" for f in fs),
                str([(f.detail, f.suggestion) for f in fs]))
    print("    检查结果:", apikb.describe(fs))

    # 4) 正确代码不能被误报（假阳性必须为 0）
    good = '''
import json
import hmac
import hashlib

def sign(secret: bytes, msg: bytes) -> str:
    mac = hmac.new(secret, msg, hashlib.sha256)
    return json.dumps({"digest": mac.hexdigest()})
'''
    fs2 = apikb.verify_source(good, kb)
    ok &= check("正确代码零误报", not fs2, str([str(f) for f in fs2]))

    # 5) 增量重建：没改动时应全部跳过
    st2 = apikb.build([str(STDLIB)], tmp)
    ok &= check("二次构建全部跳过（mtime 增量）",
                st2["skipped"] == st2["files"] and st2["new_api"] == 0, str(st2))

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
