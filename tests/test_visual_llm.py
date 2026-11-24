"""视觉兜底测试（C 层，2026-11-24）。

分层：
  test_visual.py      -> B 层：确定性检查（遮挡/可见性/对比度），参与 pass/fail
  本文件              -> C 层：截图交给模型看，只做兜底，不参与 pass/fail

关键约束（沿用 core/browser.py:10 的铁律）：
  1. 只在确定性检查之后才跑（兜底，不是第一道关）
  2. 只给结论，不改变 pass/fail
  3. 每次最多几张图（成本可控）
  4. 不可用时静默跳过，绝不影响主流程
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import visual_llm   # noqa: E402

H1_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


def _png(ws: Path, name: str = "a.png") -> Path:
    p = ws / name
    p.write_bytes(H1_PNG)
    return p


def test_max_images_is_bounded():
    """成本控制：一次看的图有上限。"""
    assert 1 <= visual_llm.MAX_IMAGES <= 5, visual_llm.MAX_IMAGES
    assert visual_llm.MAX_SIDE <= 2048, "单张图要缩放到合理尺寸"


def test_no_images_returns_ok_with_skip():
    """没给图 -> ok=True + 说明，不能报错。"""
    ws = Path(tempfile.mkdtemp(prefix="vllm-"))
    r = visual_llm.review_screenshots(ws, [])
    assert r.get("ok") is True
    assert r.get("problems") == []
    assert "截图" in str(r.get("skipped") or "")


def test_missing_file_skipped_gracefully():
    ws = Path(tempfile.mkdtemp(prefix="vllm2-"))
    r = visual_llm.review_screenshots(ws, [str(ws / "nope.png")])
    assert r.get("ok") is True, "文件不存在不能让调用方崩"
    assert r.get("problems") == []


def test_png_to_data_url():
    """真实 PNG 要能转成 data URL（模型才能读）。"""
    ws = Path(tempfile.mkdtemp(prefix="vllm3-"))
    url = visual_llm._b64_png(_png(ws))
    assert url is not None
    assert url.startswith("data:image/png;base64,")


def test_remote_unavailable_returns_ok():
    """远端不可用（本机常见）-> ok=True + skipped，不影响主流程。"""
    ws = Path(tempfile.mkdtemp(prefix="vllm4-"))
    r = visual_llm.review_screenshots(ws, [str(_png(ws, "b.png"))])
    assert r.get("ok") is True, "兜底失败不能拖垮主流程"
    assert isinstance(r.get("problems"), list)


def test_frame_extraction_with_real_video():
    """真实视频抽帧：按画面变化选，不是固定间隔。"""
    cv2 = __import__("cv2")
    np = __import__("numpy")
    ws = Path(tempfile.mkdtemp(prefix="vfrm-"))
    vid = ws / "s.mp4"
    w = cv2.VideoWriter(str(vid), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 240))
    for i in range(50):
        f = np.ones((240, 320, 3), np.uint8) * 240
        if i < 15:
            cv2.putText(f, "A", (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
        elif i < 25:
            cv2.rectangle(f, (40, 40), (280, 200), (200, 200, 255), -1)
        else:
            cv2.putText(f, "B", (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
        w.write(f)
    w.release()

    frames = visual_llm.extract_frames(vid, ws / "frames", want=3)
    assert frames, "抽不出帧"
    assert len(frames) <= 3
    for fr in frames:
        assert Path(fr["path"]).is_file()
        assert "timeSec" in fr and "changeScore" in fr
    assert max(float(f["changeScore"]) for f in frames) > 0.5


def test_extract_frames_missing_video():
    ws = Path(tempfile.mkdtemp(prefix="vfrm2-"))
    assert visual_llm.extract_frames(ws / "nope.mp4", ws / "out") == []
