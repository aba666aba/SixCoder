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


def _mkvideo(ws: Path, *, seconds=6.0, fps=30, fast=False) -> Path:
    """造一个测试视频：fast=True 时每 0.1 秒变一次（模拟快速界面）。"""
    cv2 = __import__("cv2")
    np = __import__("numpy")
    vid = ws / ("v_%d.mp4" % int(seconds * 100))
    w = cv2.VideoWriter(str(vid), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (640, 480))
    total = int(seconds * fps)
    colors = [(0, 0, 0), (0, 0, 255), (0, 255, 0), (255, 0, 0)]
    for i in range(total):
        f = np.ones((480, 640, 3), np.uint8) * 240
        if fast:
            phase = (i // 3) % 4
        else:
            phase = 0 if i < total // 3 else (1 if i < total * 2 // 3 else 2)
        cv2.rectangle(f, (100, 100), (500, 400), colors[phase % 4], -1)
        w.write(f)
    w.release()
    return vid


def test_frame_extraction_basic():
    ws = Path(tempfile.mkdtemp(prefix="vfrm-"))
    vid = _mkvideo(ws)
    frames = visual_llm.extract_frames(vid, ws / "f", want=3)
    assert frames, "抽不出帧"
    assert len(frames) <= 3
    for fr in frames:
        assert "timeSec" in fr and "changeScore" in fr


def test_cleanup_removes_files_by_default():
    """**关键**：默认不能留下临时文件（用户提的问题）。"""
    ws = Path(tempfile.mkdtemp(prefix="cln-"))
    vid = _mkvideo(ws)
    outdir = ws / "frames"
    frames = visual_llm.extract_frames(vid, outdir, want=3)
    assert frames
    # 默认返回 data_url，不返回 path
    assert all("data_url" in f for f in frames), "默认应直接给 data_url"
    assert not any("path" in f for f in frames), "默认不该返回路径"
    left = list(outdir.glob("*.png")) if outdir.is_dir() else []
    assert left == [], "磁盘上不能残留 PNG：%s" % left


def test_keep_files_when_cleanup_false():
    ws = Path(tempfile.mkdtemp(prefix="cln2-"))
    vid = _mkvideo(ws)
    outdir = ws / "keep"
    frames = visual_llm.extract_frames(vid, outdir, want=3, cleanup=False)
    assert frames
    assert all("path" in f for f in frames)
    assert len(list(outdir.glob("*.png"))) == len(frames)
    n = visual_llm.cleanup_dir(outdir)
    assert n == len(frames)
    assert not outdir.is_dir(), "清干净后目录也该删掉"


def test_sampling_rate_is_configurable():
    """采样率可调：快变化下高采样率应抓到更密集的时间点。"""
    ws = Path(tempfile.mkdtemp(prefix="rate-"))
    vid = _mkvideo(ws, fast=True)
    lo = visual_llm.extract_frames(vid, ws / "lo", want=3, sample_hz=2.0)
    hi = visual_llm.extract_frames(vid, ws / "hi", want=3, sample_hz=10.0)
    assert lo and hi
    # 高采样率的候选更密，选出的帧时间点应当不同（且更贴近真实变化）
    assert [f["timeSec"] for f in lo] != [f["timeSec"] for f in hi]


def test_frames_cover_whole_video():
    """**关键**：选帧必须覆盖全程，不能全挤在开头。"""
    ws = Path(tempfile.mkdtemp(prefix="cov-"))
    vid = _mkvideo(ws, seconds=6.0, fast=True)
    frames = visual_llm.extract_frames(vid, ws / "c", want=3, sample_hz=10.0)
    assert len(frames) == 3
    times = [f["timeSec"] for f in frames]
    # 分段取样：三帧应分布在前中后
    assert max(times) - min(times) > 2.0, "选出的帧挤在一起了：%s" % times
    assert max(times) > 2.0, "没有覆盖到视频后半段：%s" % times


def test_memory_does_not_store_frames():
    """候选只存分数不存帧：峰值内存与视频长度无关。"""
    import tracemalloc
    ws = Path(tempfile.mkdtemp(prefix="mem-"))
    vid = _mkvideo(ws, seconds=6.0, fast=True)
    tracemalloc.start()
    visual_llm.extract_frames(vid, ws / "m", want=3, sample_hz=10.0)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # 旧实现会到几百 MB；这里给个宽松上限
    assert peak < 100 * 1024 * 1024, "峰值内存 %.1f MB 太高" % (peak / 1024 / 1024)


def test_extract_frames_missing_video():
    ws = Path(tempfile.mkdtemp(prefix="vfrm2-"))
    assert visual_llm.extract_frames(ws / "nope.mp4", ws / "out") == []


def test_cleanup_dir_safe_on_missing():
    ws = Path(tempfile.mkdtemp(prefix="cd-"))
    assert visual_llm.cleanup_dir(ws / "nope") == 0
