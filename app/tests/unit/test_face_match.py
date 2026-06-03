from __future__ import annotations

from app.services.vision.face_detect import DetectedFace
from app.services.vision.face_match import FaceMatcher, select_best_face


def test_select_best_face_prefers_central_large_face() -> None:
    detections = [
        DetectedFace(bbox=(5, 5, 80, 80), confidence=0.9),
        DetectedFace(bbox=(210, 130, 160, 160), confidence=0.85),
        DetectedFace(bbox=(250, 150, 100, 100), confidence=0.99),
    ]

    selected = select_best_face(detections, frame_width=640, frame_height=480)

    assert selected.bbox == (210, 130, 160, 160)


def test_face_match_returns_best_similarity() -> None:
    matcher = FaceMatcher()
    user_id, similarity = matcher.match(
        [1.0, 0.0],
        {
            "user-a": [[0.9, 0.1]],
            "user-b": [[0.1, 0.9]],
        },
    )

    assert user_id == "user-a"
    assert similarity is not None and similarity > 0.85
