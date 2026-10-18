"""Phase 3 (Improve): retraining / fine-tuning. Only installed in concord-full (FEATURE_IMPROVE=1).

On hold (PLAN §7, Q5): scope and launch mechanism are not confirmed, so this package only carries
the router the variant plumbing mounts. It may read `segment`, `dataset` and `model`, and will own
`training_run`; nothing in `concord` imports it (import-linter enforces this).
"""

from fastapi import APIRouter

router = APIRouter(tags=["improve"])


@router.get("/status")
def status() -> dict:
    return {"phase": 3, "state": "on hold", "reason": "Scope and training launch mechanism await the owner (Q5)."}
