"""三岗位审批:人事、薪资、证券管理分别确认自己负责的数据快照。

规则:
- 每个角色只对当前数据快照确认;快照哈希变化后旧确认自动失效;
- 退回必须附意见,退回记录永久保留(append-only);
- 退回后重新确认时,新记录携带退回快照与当前快照的字段级差异,
  原意见与差异全程可追溯。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .enums import ReviewDecision, Role
from .errors import ValidationError
from .models import ApprovalCase, ReviewRecord


def snapshot_hash(snapshot: dict) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def diff_snapshots(old: Any, new: Any, path: str = "") -> dict:
    """字段级差异:{路径: {"old": 旧值, "new": 新值}}。"""
    changes: dict[str, dict] = {}
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in old:
                changes[sub] = {"old": None, "new": new[key]}
            elif key not in new:
                changes[sub] = {"old": old[key], "new": None}
            else:
                changes.update(diff_snapshots(old[key], new[key], sub))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            changes[path or "$"] = {"old": old, "new": new}
    elif old != new:
        changes[path or "$"] = {"old": old, "new": new}
    return changes


def submit_review(
    case: ApprovalCase,
    *,
    role: Role,
    decision: ReviewDecision,
    opinion: str,
    snapshot: dict,
    at,
) -> ReviewRecord:
    """向 case 追加一条审批记录;退回后再确认时自动附带差异。"""
    if decision == ReviewDecision.RETURNED and not opinion.strip():
        raise ValidationError("退回必须填写意见", code="opinion_required")

    diff = None
    if decision == ReviewDecision.CONFIRMED:
        last_return = next(
            (
                r
                for r in reversed(case.reviews)
                if r.role == role and r.decision == ReviewDecision.RETURNED
            ),
            None,
        )
        if last_return is not None:
            diff = diff_snapshots(last_return.snapshot, snapshot)

    record = ReviewRecord(
        role=role,
        decision=decision,
        opinion=opinion,
        snapshot_hash=snapshot_hash(snapshot),
        snapshot=snapshot,
        diff_from_previous=diff,
        at=at,
    )
    case.reviews.append(record)
    return record


def is_complete(case: ApprovalCase, expected_hashes: dict[Role, str]) -> bool:
    """三岗位均对当前快照确认,审批方算齐全。"""
    latest = case.latest_by_role()
    for role, expected in expected_hashes.items():
        record = latest.get(role)
        if record is None:
            return False
        if record.decision != ReviewDecision.CONFIRMED:
            return False
        if record.snapshot_hash != expected:
            return False
    return True
