"""Numpy-free infer limits shared by serve and separate.

Keep window / cap numbers here so the sidecar CI job can import them
without pulling numpy or the Demucs stack.
"""

from __future__ import annotations

WINDOW_SECONDS = 600.0
WINDOW_OVERLAP_SECONDS = 1.0
MEMORY_CAP_BYTES = 2 * 1024 ** 3
FLOAT32_BYTES = 4


class JobCancelled(RuntimeError):
    pass


def pcm_nbytes(n_samples: int, channels: int, itemsize: int = FLOAT32_BYTES) -> int:
    """Integer sample count × channels × itemsize. Prefer this over duration*rate."""
    return int(n_samples) * int(channels) * int(itemsize)


def raise_if_cancelled(cancel_event: object | None) -> None:
    """Raise JobCancelled if an event is set or a callback returns true."""
    if cancel_event is None:
        return
    is_set = getattr(cancel_event, "is_set", None)
    if callable(is_set):
        if is_set():
            raise JobCancelled("job cancelled")
        return
    if callable(cancel_event) and cancel_event():
        raise JobCancelled("job cancelled")
