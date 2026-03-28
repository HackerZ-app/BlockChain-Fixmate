import os
import threading
from typing import List, Tuple

import numpy as np

# Keep sentence-transformers on the PyTorch path and reduce noisy TF startup logs.
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")

_MODEL = None
_MODEL_LOCK = threading.Lock()

# Embedding cache: maps issue problem-text key -> pre-computed unit vector.
# _CACHE_LOCK serialises writes so concurrent requests never corrupt the dict.
_CACHE: dict[str, np.ndarray] = {}
_CACHE_LOCK = threading.Lock()


def _get_model():
    global _MODEL
    # Fast path: model already loaded — skip lock acquisition.
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        # Double-checked locking: re-test inside the lock.
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer

            _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _MODEL


def embed_text(text: str) -> np.ndarray:
    cleaned = (text or "").strip()
    if not cleaned:
        return np.zeros(384, dtype=float)
    model = _get_model()
    vec = model.encode(cleaned, normalize_embeddings=True)
    return np.asarray(vec, dtype=float)


def _issue_text(issue: dict) -> str:
    problem = (issue.get("problem") or "").strip()
    details = " ".join((s.get("description") or "").strip() for s in issue.get("solutions", []))
    return f"{problem}. {details}".strip()


def precompute(issues: List[dict]) -> None:
    """Pre-embed all issues at startup so the first request pays no cold-start cost."""
    for issue in issues:
        key = (issue.get("problem") or "").strip()
        if not key:
            continue
        # Check outside the lock first to avoid unnecessary lock contention.
        if key in _CACHE:
            continue
        vec = embed_text(_issue_text(issue))
        with _CACHE_LOCK:
            # Write unconditionally — recomputing the same vector is harmless
            # and avoids a second dict lookup inside the lock.
            _CACHE[key] = vec


def find_matches(
    query: str,
    issues: List[dict],
    top_k: int = 5,
    threshold: float = 0.35,
) -> Tuple[List[Tuple[dict, float]], bool]:
    if not (query or "").strip() or not issues:
        return [], False

    query_vec = embed_text(query)
    scores: List[Tuple[dict, float]] = []

    for issue in issues:
        key = (issue.get("problem") or "").strip()
        # Read from cache without holding the lock — dict reads are safe under
        # Python's GIL and a stale miss only costs one extra embed call.
        issue_vec = _CACHE.get(key)
        if issue_vec is None:
            issue_vec = embed_text(_issue_text(issue))
            if key:
                with _CACHE_LOCK:
                    _CACHE[key] = issue_vec
        score = float(np.dot(query_vec, issue_vec))
        scores.append((issue, score))

    scores.sort(key=lambda item: item[1], reverse=True)
    top = scores[: max(1, top_k)]
    best_score = top[0][1] if top else 0.0
    return top, bool(top and best_score >= threshold)
