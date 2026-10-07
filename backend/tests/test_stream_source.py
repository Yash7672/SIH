"""StreamSource: registration resolution + video-file frame reading.

Covers the parts of the stream mode that are testable without a camera: the
`config/stream_sources.json` registry, inline-spec resolution, and reading +
encoding frames from a video file. Network kinds (MJPEG/RTSP/USB) cannot run in
CI without hardware; the MJPEG-over-HTTP parser is exercised manually against a
local multipart server in `.rakshak/_test_mjpeg.py`.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "data" / "generated" / "demo_road.mp4"

from backend.stream_source import SourceError, StreamSource, find_source, load_sources


def test_registry_contains_fixed_cameras():
    """No cameras table exists in this schema, so sources come from config."""
    sources = load_sources()
    assert "phone_ip_camera" in sources
    assert sources["phone_ip_camera"]["kind"] == "mjpeg"
    assert "demo_road" in sources


def test_find_source_resolves_registered_name():
    name, spec = find_source("demo_road")
    assert name == "demo_road"
    assert spec["kind"] == "video"


def test_find_source_accepts_inline_json_spec():
    name, spec = find_source('{"kind": "video", "path": "data/generated/demo_road.mp4"}')
    assert name == "<inline-spec>"
    assert spec["kind"] == "video"


def test_find_source_unknown_name_raises():
    with pytest.raises(SourceError):
        find_source("no-such-camera")


def test_bad_kind_raises_in_source_constructor():
    with pytest.raises(SourceError):
        StreamSource("bad", {"kind": "smoke-signal"})
    with pytest.raises(SourceError):
        StreamSource("bad", {})  # kind missing entirely


@pytest.mark.skipif(not DEMO.is_file(), reason="demo_road.mp4 not generated")
def test_video_source_reads_and_encodes_frames():
    src = StreamSource("demo", {"kind": "video", "path": str(DEMO), "quality": 90})
    assert src.open() is True
    assert src.width == 660 and src.height == 260
    jpegs = [src.read_jpeg() for _ in range(3)]
    assert all(j and len(j) > 5000 for j in jpegs), "frames must decode as JPEG"
    assert src.frames_read == 3
    src.close()
    assert src.is_open is False


@pytest.mark.skipif(not DEMO.is_file(), reason="demo_road.mp4 not generated")
def test_video_source_rewind_via_loop():
    src = StreamSource(
        "demo", {"kind": "video", "path": str(DEMO), "loop": True}
    )
    assert src.open() is True
    got = 0
    for _jpeg, _w, _h in src.iter_frames(max_frames=25):
        got += 1
    assert got == 25, "iter_frames with loop must keep yielding past EOF"
    src.close()


@pytest.mark.skipif(not DEMO.is_file(), reason="demo_road.mp4 not generated")
def test_video_source_eof_without_loop_stops():
    src = StreamSource("demo", {"kind": "video", "path": str(DEMO), "loop": False})
    assert src.open() is True
    got = 0
    for _jpeg, _w, _h in src.iter_frames():
        got += 1
    assert got > 0, "clip must have at least one frame"
    src.close()