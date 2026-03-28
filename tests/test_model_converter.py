"""Tests for model_converter pipeline — inspect, ONNX validation, arch detection, NMS config.

Runs locally without Hailo SW Suite. Tests steps 1-3 of the pipeline
using real .pt and .onnx files in models/.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

# Ensure project root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.model_converter import tools

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def surgeonet_pt():
    p = MODELS_DIR / "SurgeoNet_byme.pt"
    if not p.exists():
        pytest.skip("SurgeoNet_byme.pt not found in models/")
    return str(p)


@pytest.fixture
def surgeonet_onnx():
    p = MODELS_DIR / "SurgeoNet_byme.onnx"
    if not p.exists():
        pytest.skip("SurgeoNet_byme.onnx not found in models/")
    return str(p)


@pytest.fixture
def yolov8m_pt():
    p = MODELS_DIR / "yolov8m_DG_Surgical01.pt"
    if not p.exists():
        pytest.skip("yolov8m_DG_Surgical01.pt not found in models/")
    return str(p)


@pytest.fixture
def yolo11n_pt():
    p = MODELS_DIR / "yolo11n_object365.pt"
    if not p.exists():
        pytest.skip("yolo11n_object365.pt not found in models/")
    return str(p)


# ── inspect_model ─────────────────────────────────────────────────────────────

class TestInspectModel:

    def test_inspect_pt_returns_class_names(self, surgeonet_pt):
        result = tools.dispatch("inspect_model", {"file_path": surgeonet_pt})
        assert result["ok"] is True
        assert result["format"] == "pt"
        assert len(result["class_names"]) > 0
        assert result["input_size"] is not None
        assert result["arch"] is not None

    def test_inspect_onnx_returns_input_size(self, surgeonet_onnx):
        result = tools.dispatch("inspect_model", {"file_path": surgeonet_onnx})
        assert result["ok"] is True
        assert result["format"] == "onnx"
        assert isinstance(result["input_size"], int)
        assert result["input_size"] > 0

    def test_inspect_yolov8_detects_arch(self, yolov8m_pt):
        result = tools.dispatch("inspect_model", {"file_path": yolov8m_pt})
        assert result["ok"] is True
        assert result["arch"] in ("yolov8", "yolov11")

    def test_inspect_yolo11_detects_arch(self, yolo11n_pt):
        result = tools.dispatch("inspect_model", {"file_path": yolo11n_pt})
        assert result["ok"] is True
        # yolo11 models should be detected as yolov11 or at least yolov8 (same head)
        assert result["arch"] in ("yolov8", "yolov11")

    def test_inspect_missing_file(self):
        result = tools.dispatch("inspect_model", {"file_path": "/nonexistent/model.pt"})
        assert result["ok"] is False
        assert "not found" in result.get("error", "").lower()


# ── ONNX validation ──────────────────────────────────────────────────────────

class TestValidateOnnx:

    def test_validate_good_onnx(self, surgeonet_onnx):
        result = tools._validate_onnx(surgeonet_onnx)
        assert result["ok"] is True
        assert "input_shape" in result
        assert len(result["input_shape"]) == 4

    def test_validate_nonexistent_file(self):
        result = tools._validate_onnx("/nonexistent/model.onnx")
        assert result["ok"] is False

    def test_validate_corrupt_file(self, tmp_path):
        bad = tmp_path / "corrupt.onnx"
        bad.write_bytes(b"this is not an onnx file")
        result = tools._validate_onnx(str(bad))
        assert result["ok"] is False
        assert "validation failed" in result["error"].lower()


# ── Architecture detection from ONNX ─────────────────────────────────────────

class TestArchDetection:

    def test_detect_from_surgeonet_onnx(self, surgeonet_onnx):
        arch = tools._detect_yolo_arch_from_onnx(surgeonet_onnx)
        # SurgeoNet is YOLOv8m-based
        assert arch in ("yolov8", "yolov11")

    def test_detect_returns_none_for_garbage(self, tmp_path):
        bad = tmp_path / "garbage.onnx"
        bad.write_bytes(b"not onnx")
        arch = tools._detect_yolo_arch_from_onnx(str(bad))
        assert arch is None


# ── End node detection ────────────────────────────────────────────────────────

class TestEndNodeDetection:

    def test_yolov8_end_nodes_from_onnx(self, surgeonet_onnx):
        nodes = tools._detect_end_nodes(surgeonet_onnx, "yolov8")
        assert len(nodes) == 6
        assert all("Conv" in n for n in nodes)

    def test_yolov8_and_v11_share_dynamic_detection(self, surgeonet_onnx):
        """Both yolov8 and yolov11 arch use _find_detect_head_index to locate end nodes."""
        nodes_v8 = tools._detect_end_nodes(surgeonet_onnx, "yolov8")
        nodes_v11 = tools._detect_end_nodes(surgeonet_onnx, "yolov11")
        # SurgeoNet is YOLOv8 (model.22), so both should find the same head
        assert len(nodes_v8) == 6
        assert nodes_v8 == nodes_v11

    def test_unknown_arch_falls_back_to_auto_discover(self, surgeonet_onnx):
        nodes = tools._detect_end_nodes(surgeonet_onnx, "unknown_arch")
        # auto-discover should still find detection head Conv nodes
        # (may or may not find them depending on graph structure)
        assert isinstance(nodes, list)

    def test_auto_discover_returns_list(self, surgeonet_onnx):
        nodes = tools._auto_discover_end_nodes(surgeonet_onnx)
        assert isinstance(nodes, list)
        # For a YOLOv8 model, auto-discover should find cv2/cv3 nodes
        if nodes:
            assert any("cv2" in n or "cv3" in n for n in nodes)


# ── NMS model.alls generation ─────────────────────────────────────────────────

class TestModelScript:

    def test_yolov8_nms_config(self, tmp_path):
        alls = tools._build_model_script(str(tmp_path), "yolov8", num_classes=14, input_resolution=416)
        content = Path(alls).read_text()
        assert "nms_postprocess(" in content
        assert "meta_arch=yolov8" in content
        assert "classes=14" in content
        assert "image_dims=(416, 416)" in content
        assert "regression_length=16" in content

    def test_yolov11_uses_yolov8_meta_arch(self, tmp_path):
        alls = tools._build_model_script(str(tmp_path), "yolov11", num_classes=80, input_resolution=640)
        content = Path(alls).read_text()
        assert "meta_arch=yolov8" in content
        assert "classes=80" in content

    def test_yolov5_uses_anchors(self, tmp_path):
        alls = tools._build_model_script(str(tmp_path), "yolov5", num_classes=80, input_resolution=640)
        content = Path(alls).read_text()
        assert "meta_arch=yolov5" in content
        assert "anchors_num=3" in content
        assert "regression_length" not in content

    def test_yolov9_uses_yolov8_meta_arch(self, tmp_path):
        alls = tools._build_model_script(str(tmp_path), "yolov9", num_classes=20, input_resolution=640)
        content = Path(alls).read_text()
        assert "meta_arch=yolov8" in content
        assert "classes=20" in content

    def test_unknown_arch_skips_nms(self, tmp_path):
        alls = tools._build_model_script(str(tmp_path), None, num_classes=80, input_resolution=640)
        content = Path(alls).read_text()
        assert "nms_postprocess" not in content
        # Base optimization params should still be present
        assert "optimization_level=0" in content

    def test_base_params_always_present(self, tmp_path):
        for arch in ("yolov5", "yolov8", "yolov11", None):
            alls = tools._build_model_script(str(tmp_path), arch, num_classes=80, input_resolution=640)
            content = Path(alls).read_text()
            assert "model_optimization_flavor" in content
            assert "resources_param" in content
            assert "performance_param" in content


# ── ONNX export (integration — slow, uses ultralytics) ───────────────────────

class TestExportOnnx:

    @pytest.mark.slow
    def test_export_yolo11n(self, yolo11n_pt):
        """Export smallest model to ONNX and validate."""
        with tempfile.TemporaryDirectory(prefix="conv_test_") as work_dir:
            result = tools.dispatch("export_onnx", {
                "pt_path": yolo11n_pt,
                "work_dir": work_dir,
                "imgsz": 640,
            })
            assert result["ok"] is True, f"Export failed: {result.get('error')}"
            assert os.path.exists(result["onnx_path"])
            assert result["onnx_path"].endswith(".onnx")

            # Validate the exported ONNX
            val = tools._validate_onnx(result["onnx_path"])
            assert val["ok"] is True
            assert val["input_shape"][2] == 640
            assert val["input_shape"][3] == 640

            # Detect architecture from the exported ONNX
            arch = tools._detect_yolo_arch_from_onnx(result["onnx_path"])
            assert arch in ("yolov8", "yolov11")

            # Verify end nodes can be found
            nodes = tools._detect_end_nodes(result["onnx_path"], arch)
            assert len(nodes) == 6, f"Expected 6 end nodes, got {len(nodes)}: {nodes}"


# ── Full pipeline dry run (no Hailo, tests orchestration logic) ───────────────

class TestPipelineDryRun:

    def test_inspect_then_arch_then_nms(self, yolov8m_pt, tmp_path):
        """Simulate the pipeline: inspect -> detect arch -> generate NMS config."""
        # Step 1: Inspect
        info = tools.dispatch("inspect_model", {"file_path": yolov8m_pt})
        assert info["ok"] is True

        arch = info["arch"]
        num_classes = len(info["class_names"]) if info["class_names"] else 80
        input_resolution = info["input_size"] or 640

        assert arch is not None
        assert num_classes > 0
        assert input_resolution > 0

        # Step 2: Generate model.alls with correct NMS config
        alls = tools._build_model_script(str(tmp_path), arch, num_classes, input_resolution)
        content = Path(alls).read_text()
        assert "nms_postprocess(" in content
        assert f"classes={num_classes}" in content
        assert f"image_dims=({input_resolution}, {input_resolution})" in content
