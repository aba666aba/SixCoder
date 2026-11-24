"""复现"链路自检"失败：把每个节点的事件与失败原因打出来。

用法：python tools/diag_demo.py [端口]
"""
import json
import sys
import time
import urllib.request

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else 8780}"

DEMO_REQ = ("给 calc.py 增加一个 divide(a, b) 函数：返回 a/b，当 b 为 0 时抛 "
            'ValueError("除数不能为 0")。并补上对应的 pytest 测试，覆盖正常除法与除零两种路径。')


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=20))


def main():
    d = post("/v1/demo", {})
    print("演示仓库:", d["repo"], "| 形态:", d["state"])
    t = post("/v1/tasks", {"repo": d["repo"], "requirement": DEMO_REQ, "target": "calc.py"})
    tid = t["task_id"]
    print("任务:", tid, "| 模型:", t["model"])
    for _ in range(120):
        time.sleep(2)
        try:
            cur = get(f"/v1/tasks/{tid}")
        except Exception as exc:
            print("查询失败:", exc)
            continue
        state = (cur.get("task") or {}).get("state")
        if state in ("done", "needs_human", "stuck", "failed"):
            print("最终 state:", state)
            break
    detail = get(f"/v1/tasks/{tid}")
    a = detail.get("audit") or {}
    print("审计:", {k: a.get(k) for k in ("nodes_ok", "nodes_total", "passed",
                                          "model_assertion_rate", "mutation_kill_rate")})
    for node in (detail.get("nodes") or []):
        print(f"  节点 {node.get('nid')} {node.get('kind')} {node.get('path') or ''} "
              f"status={node.get('status')}")
        for g in (node.get("gates") or []):
            if not g.get("ok"):
                print(f"      ✗ {g.get('gate')}: {str(g.get('detail'))[:160]}")
        if node.get("error"):
            print(f"      错误: {str(node.get('error'))[:200]}")


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    main()
