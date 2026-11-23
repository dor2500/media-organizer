"""Face detection & recognition using OpenCV YuNet + SFace ONNX models.

Uses Pillow (with ImageOps.exif_transpose) to decode images so Hebrew/Unicode
paths, HEIC/HEIF files, and rotated phone photos all work reliably on Windows.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
except Exception:  # pragma: no cover
    pass

from .dates import IMAGE_EXTS, VIDEO_EXTS

YUNET_NAME = "face_detection_yunet_2023mar.onnx"
SFACE_NAME = "face_recognition_sface_2021dec.onnx"
DEFAULT_THRESHOLD = 0.363  # OpenCV SFace official cosine similarity threshold
MAX_DIM = 1600


def _models_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / "models"


def _ascii_safe_path(p: Path) -> str:
    """OpenCV's C++ ONNX loader on Windows can fail on non-ASCII paths; stage in temp if needed."""
    s = str(p)
    if s.isascii():
        return s
    stage = Path(tempfile.gettempdir()) / "media_organizer_models"
    stage.mkdir(parents=True, exist_ok=True)
    dst = stage / p.name
    if not dst.exists() or dst.stat().st_size != p.stat().st_size:
        shutil.copy2(p, dst)
    return str(dst)


class FaceMatcher:
    """Loads YuNet + SFace once and matches a reference person across images & videos."""

    def __init__(self, ref_image: Path, threshold: float = DEFAULT_THRESHOLD,
                 models_dir: Path | None = None):
        mdir = models_dir or _models_dir()
        yunet_path = mdir / YUNET_NAME
        sface_path = mdir / SFACE_NAME
        if not yunet_path.is_file() or not sface_path.is_file():
            raise FileNotFoundError(
                f"Face models not found in {mdir}. Expected {YUNET_NAME} and {SFACE_NAME}."
            )
        self.threshold = threshold
        self.detector = cv2.FaceDetectorYN.create(
            _ascii_safe_path(yunet_path), "", (320, 320), 0.75, 0.3, 5000
        )
        self.recognizer = cv2.FaceRecognizerSF.create(_ascii_safe_path(sface_path), "")
        self.ref_feature, self.ref_faces_count = self._extract_reference(Path(ref_image))

    def _load_bgr(self, path: Path) -> np.ndarray | None:
        try:
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                w, h = img.size
                if max(w, h) > MAX_DIM:
                    scale = MAX_DIM / max(w, h)
                    img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                     Image.Resampling.LANCZOS)
                rgb = np.asarray(img)
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception:
            return None

    def _detect_features(self, bgr: np.ndarray) -> list[tuple[np.ndarray, float]]:
        h, w = bgr.shape[:2]
        if h < 20 or w < 20:
            return []
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(bgr)
        if faces is None or len(faces) == 0:
            return []
        out: list[tuple[np.ndarray, float]] = []
        for face in faces:
            area = float(face[2] * face[3])
            aligned = self.recognizer.alignCrop(bgr, face)
            feat = self.recognizer.feature(aligned)
            out.append((feat, area))
        return out

    def _extract_reference(self, ref_image: Path) -> tuple[np.ndarray, int]:
        bgr = self._load_bgr(ref_image)
        if bgr is None:
            raise ValueError(f"Cannot open reference image: {ref_image}")
        feats = self._detect_features(bgr)
        if not feats:
            raise ValueError(f"No face detected in reference image: {ref_image.name}")
        # Pick the largest/most prominent face in the reference image
        best_feat, _ = max(feats, key=lambda item: item[1])
        return best_feat, len(feats)

    def _best_score(self, bgr: np.ndarray) -> float | None:
        feats = self._detect_features(bgr)
        if not feats:
            return None
        return max(
            float(self.recognizer.match(self.ref_feature, f, cv2.FaceRecognizerSF_FR_COSINE))
            for f, _ in feats
        )

    def match_file(self, path: Path) -> tuple[bool, float | None]:
        """Return (is_match, best_cosine_score)."""
        ext = path.suffix.lower()
        if ext in IMAGE_EXTS:
            bgr = self._load_bgr(path)
            if bgr is None:
                return False, None
            score = self._best_score(bgr)
            return (score is not None and score >= self.threshold), score

        if ext in VIDEO_EXTS:
            return self._match_video(path)

        return False, None

    def _match_video(self, path: Path, max_samples: int = 12) -> tuple[bool, float | None]:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return False, None
        best: float | None = None
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            step = max(int(fps * 2), 1)
            if total > 0 and total // step > max_samples:
                step = max(total // max_samples, 1)
            idx = 0
            samples = 0
            while samples < max_samples:
                if total > 0:
                    if idx >= total:
                        break
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ok, frame = cap.read()
                    idx += step
                else:
                    ok, frame = cap.read()
                    for _ in range(step - 1):
                        if not cap.grab():
                            break
                if not ok or frame is None:
                    break
                samples += 1
                h, w = frame.shape[:2]
                if max(w, h) > MAX_DIM:
                    s = MAX_DIM / max(w, h)
                    frame = cv2.resize(frame, (int(w * s), int(h * s)))
                score = self._best_score(frame)
                if score is not None:
                    if best is None or score > best:
                        best = score
                    if score >= self.threshold:
                        return True, score
        finally:
            cap.release()
        return False, best
