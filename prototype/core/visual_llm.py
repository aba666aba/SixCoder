"""视觉兜底（2026-11-24）—— 让模型 "看" 一眼界面截图。

## 定位：**兜底**，不是主判据

core/visual.py 做的是确定性检查（遮挡/可见性/对比度），
**能查的就别问模型**。本模块只处理它查不了的：

    · canvas / WebGL 里画的东西
    · 布局"看起来别扭"（对齐、间距、溢出）
    · 整体观感（是不是白屏了、是不是错位）

## 硬约束（沿用 core/browser.py:10 的决策）

「不靠截图喂模型」这条铁律的**真实含义**是：
**不能用视觉判据替代机械判据**。所以本模块：

1. **只在确定性检查全绿后**才跑（兜底，不是第一道关）
2. **只给结论，不参与 pass/fail**：返回 problems 列表，
   由调用方决定是"告警"还是"下一轮的证据"
3. **每次最多 1 张图**（成本可控）
4. **不可用时静默跳过**（没配模型 / 模型不支持图片 -> 不影响主流程）

## 视频：抽帧后看关键帧

录屏不是本模块负责的（那是测试夹具的事）。
如果给了视频，用 cv2 按画面变化抽帧，取变化最大的几帧给模型看。

抽帧算法参考 DSH local-media：缩到 64x36 灰度，算相邻帧平均绝对差，
按 diff 排序取前 N 帧 —— 静止段只留一张，变化密集处取差异最大的。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

#: 一次最多看几张（成本控制）
MAX_IMAGES = 3
#: 单张图最长边（超过就缩小，省 token）
MAX_SIDE = 1280


def _b64_png(path: Path, max_side: int = MAX_SIDE) -> Optional[str]:
    """把图片读成 data URL。超过 max_side 就缩小（cv2 可选）。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # 试着用 cv2 缩小（装了才做，没装就原图发）
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if arr is not None:
            h, w = arr.shape[:2]
            longest = max(h, w)
            if longest > max_side:
                scale = max_side / float(longest)
                arr = cv2.resize(arr, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".png", arr)
            if ok:
                raw = buf.tobytes()
    except Exception:                                    # noqa: BLE001
        pass                                             # 缩小失败就用原图
    b64 = base64.b64encode(raw).decode("ascii")
    return "data:image/png;base64," + b64


def extract_frames(video: Path, out_dir: Path, want: int = 3,
                   *, sample_hz: float = 4.0, max_candidates: int = 2400,
                   cleanup: bool = True) -> list[dict]:
    """按**画面变化**抽帧。

    ## 采样率（2026-11-24 修：0.5s 太粗）

    原来固定"每 0.5 秒采一格" —— 30fps 下每 15 帧才看一次。
    界面在 0.3 秒内完成一次切换（弹窗弹出、路由跳转）就会整段被跳过。

    现在默认 **4 次/秒**（sample_hz=4.0，即每 0.25 秒一格），
    快速变化也接得住。代价是候选帧变多，但候选只存**灰度小图 + 分数**（见下）。

    需要更细就调 sample_hz（8~10 适合动画密集型界面）；
    视频很长时用 max_candidates 兜底，避免扫描过久。

    ## 内存（2026-11-24 修：会爆）

    原来 cands 里存的是**完整 numpy 帧**：
        320x240x3 = 230KB/帧，2400 帧 = 552 MB 常驻 -> 直接 OOM。
    现在候选只存 (帧号, 时间, 差异分)，**帧本身不保留**；
    等选出前 N 个之后，再 seek 回去读那几帧。内存占用与视频长度无关。

    ## 临时文件（2026-11-24 修：不删会爆炸）

    cleanup=True（默认）：返回的是**已编码的 data URL**，写出的 PNG 立刻删掉，
    磁盘上不留东西。调用方拿到就能直接发给模型。
    cleanup=False：保留 PNG 并返回路径（调试/人看现场时用，用完自己清）。

    返回 [{"timeSec", "changeScore", "path"?, "data_url"?}]。
    """
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except Exception:                                    # noqa: BLE001
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    step = max(1, int(round(fps / max(0.5, sample_hz))))

    # 第一遍：只记分数，**不保留帧**（内存与视频长度无关）
    cands: list[tuple] = []          # (帧号, 时间, 差异分)
    prev = None
    i = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if i % step == 0:
                small = cv2.cvtColor(cv2.resize(frame, (64, 36)),
                                     cv2.COLOR_BGR2GRAY)
                diff = 0.0 if prev is None else float(
                    np.abs(small.astype(np.int16) - prev.astype(np.int16)).mean())
                cands.append((i, i / fps, diff))
                prev = small
                del small
            i += 1
            if len(cands) >= max_candidates:
                break
    finally:
        cap.release()
    if not cands:
        return []

    # 选出要看的帧 —— **不是简单取变化最大的 N 个**。
    #
    # 为什么（2026-11-24 实测暴露）：只按 diff 排序取前 N，
    # 快速变化时前 N 个全挤在开头几秒（实测 6 秒视频，
    # hz=10 选出的 3 帧全在 0.7~1.5s），**后半段完全没被看过**。
    #
    # 改成**分段取样**：把时间轴切成 want 段，每段内取变化最大的一帧。
    # 这样既覆盖全程，又保证每段抓到的是该段最"动"的时刻。
    top = []
    if cands:
        n = max(1, want)
        total = cands[-1][0] + 1
        seg = max(1, total // n)
        for k in range(n):
            lo, hi = k * seg, (k + 1) * seg if k < n - 1 else total
            bucket = [c for c in cands if lo <= c[0] < hi]
            if bucket:
                top.append(max(bucket, key=lambda x: x[2]))
        # 段内全是 0 变化时 bucket 仍会出一帧（静止段也要有代表帧）

    # 第二遍：seek 回去只读那几帧
    out = []
    cap2 = cv2.VideoCapture(str(video))
    try:
        for idx, (fi, t, diff) in enumerate(top, 1):
            cap2.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap2.read()
            if not ok or frame is None:
                continue
            p = out_dir / ("frame_%02d.png" % idx)
            try:
                cv2.imwrite(str(p), frame)
            except Exception:                            # noqa: BLE001
                continue
            item: dict = {"timeSec": round(t, 1), "changeScore": round(diff, 1)}
            if cleanup:
                url = _b64_png(p)
                try:
                    p.unlink()                           # 立刻删，磁盘不留东西
                except OSError:
                    pass
                if url:
                    item["data_url"] = url
            else:
                item["path"] = str(p)
            out.append(item)
    finally:
        cap2.release()
    return out


def cleanup_dir(path) -> int:
    """删掉一个目录里的 frame_*.png，返回删除个数。

    给 cleanup=False 的调用方兜底用：调试完调一下，别让临时文件堆着。
    **只删本模块自己生成的文件名**（frame_NN.png），不碰别的。
    """
    d = Path(path)
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob("frame_*.png"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    try:
        d.rmdir()                                        # 空了就删目录
    except OSError:
        pass                                             # 还有别的文件就留着
    return n


_PROMPT = """你是 UI 检查员。下面是一组界面截图（可能是同一页面的不同状态，也可能来自录屏）。
请**只报告确实看得见的问题**，不要臆测。按 JSON 返回：

{"problems": [{"severity": "high|medium|low",
               "where": "哪张图/哪个区域",
               "what": "看到什么现象",
               "why": "为什么这是问题"}],
 "looks_ok": true|false}

重点看这些：
  · 白屏 / 空白区域占比过大 / 内容没渲染出来
  · 文字被截断、溢出容器、互相重叠
  · 元素明显错位、间距异常
  · 颜色搭配刺眼或看不清（与背景糊在一起）
  · 加载态/错误提示残留（如 "undefined"、"NaN"、"null" 直接显示）

**没问题就返回 {"problems": [], "looks_ok": true}**。不要为了凑数编问题。"""


def review_screenshots(ws, images: list[str], *, emit=None) -> dict:
    """让远端"看"截图并返回问题列表。

    返回 {"ok": bool, "problems": [...], "skipped": str}。
    **任何不可用都返回 ok=True + skipped=原因**，绝不因兜底失败拖垮主流程。
    """
    shots = [p for p in (images or []) if Path(str(p)).is_file()][:MAX_IMAGES]
    if not shots:
        return {"ok": True, "problems": [], "skipped": "没有截图"}

    try:
        from . import remote as _remote   # noqa: PLC0415
    except Exception as exc:                             # noqa: BLE001
        return {"ok": True, "problems": [], "skipped": "remote 不可用：%s" % exc}

    ok_avail, why = _remote.available()
    if not ok_avail:
        return {"ok": True, "problems": [], "skipped": "远端不可用：%s" % why}

    content: list[dict] = [{"type": "text", "text": _PROMPT}]
    for p in shots:
        url = _b64_png(Path(p))
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
    if len(content) <= 1:
        return {"ok": True, "problems": [], "skipped": "截图读不出来"}

    try:
        r = _remote.complete(
            "", task_id="visual_review", node_id="visual_review",
            workspace=str(ws), emit=emit, thinking="high", json_mode=True,
            messages=[{"role": "user", "content": content}])
    except Exception as exc:                             # noqa: BLE001
        return {"ok": True, "problems": [], "skipped": "调用失败：%s: %s"
                % (type(exc).__name__, exc)}

    if isinstance(r, dict) and not r.get("ok"):
        return {"ok": True, "problems": [], "skipped": "远端返回失败：%s"
                % str(r.get("error") or "")[:200]}
    ans = r.get("answer") if isinstance(r, dict) else str(r or "")
    try:
        data = json.loads(str(ans))
    except (ValueError, TypeError):
        s, e = str(ans).find("{"), str(ans).rfind("}")
        if s < 0 or e <= s:
            return {"ok": True, "problems": [], "skipped": "模型没返回 JSON"}
        try:
            data = json.loads(str(ans)[s:e + 1])
        except ValueError:
            return {"ok": True, "problems": [], "skipped": "模型返回的 JSON 解析不了"}
    probs = data.get("problems") if isinstance(data, dict) else None
    return {"ok": True, "problems": list(probs or []),
            "looks_ok": bool((data or {}).get("looks_ok", True)),
            "images": shots}
