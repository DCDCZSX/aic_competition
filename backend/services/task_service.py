"""Action Center 任务状态机（规格书 §5.5 / 任务 3.4）。

状态机（与 `schemas.TASK_STATES` 同一权威源）：

    todo ──▶ doing ──▶ done ──▶ reopened ──▶ doing/done
      │        │
      └────────┴──▶ cancelled ──▶ todo（可重新激活）

规则（写死）：
- **只允许显式转移表内的跳转**，非法转移抛 `IllegalTaskTransition`；
- 每次流转留痕（from / to / actor / reason / at）；
- **完成任务不直接触发重算**，但"就诊后资料更新"类任务完成后应触发**局部重算**
  （规格书 §7：任务完成 → 依赖矩阵定受影响模块 → 重算 → 变化摘要）；
- 任务来源三类：分诊路径步骤 ∪ 各科 high checks ∪ 医生补资料请求。
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import TASK_STATES, TASK_TYPES  # noqa: E402

TODO = "todo"
DOING = "doing"
DONE = "done"
CANCELLED = "cancelled"
REOPENED = "reopened"

TRANSITIONS: Dict[str, tuple] = {
    TODO: (DOING, DONE, CANCELLED),
    DOING: (DONE, CANCELLED),
    DONE: (REOPENED,),
    CANCELLED: (TODO,),
    REOPENED: (DOING, DONE, CANCELLED),
}

STATE_LABELS: Dict[str, str] = {
    TODO: "待办",
    DOING: "进行中",
    DONE: "已完成",
    CANCELLED: "已取消",
    REOPENED: "需重做",
}

# 完成后需要触发局部重算的任务类型（结果类任务）
RECOMPUTE_TRIGGER_TYPES = ("就诊后资料更新",)


class TaskError(ValueError):
    """任务操作错误（找不到任务 / 非法状态转移）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class TaskUpdate:
    """一次任务状态变更的结果。"""

    task_id: str
    from_state: str
    to_state: str
    actor: str = "patient"
    reason: str = ""
    at: str = ""
    trigger_recompute: bool = False       # 是否应触发局部重算（就诊后资料更新类）
    record: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "from_state": self.from_state,
            "from_label": STATE_LABELS.get(self.from_state, self.from_state),
            "to_state": self.to_state,
            "to_label": STATE_LABELS.get(self.to_state, self.to_state),
            "actor": self.actor,
            "reason": self.reason,
            "at": self.at,
            "trigger_recompute": self.trigger_recompute,
        }


def find_task(tasks: Sequence[Dict[str, Any]], task_id: str) -> Dict[str, Any]:
    for task in tasks or []:
        if str(task.get("task_id")) == str(task_id):
            return task
    raise TaskError(f"任务不存在：{task_id}")


def merge_progress(old_tasks: Sequence[Dict[str, Any]], new_tasks: Sequence[Dict[str, Any]],
                   *, reason: str) -> List[Dict[str, Any]]:
    """Preserve task identity/history; reopen a uniquely matched changed completed task."""
    old = [deepcopy(task) for task in old_tasks if isinstance(task, dict)]
    new = [deepcopy(task) for task in new_tasks if isinstance(task, dict)]
    def signature(task):
        return tuple(str(task.get(key) or "") for key in ("type", "source", "content"))
    def origin(task):
        return signature(task)[:2]
    origins = Counter(origin(task) for task in new)
    matched = set()
    reserved = {str(task.get("task_id")) for task in old if task.get("task_id")}
    used = set()
    next_id = 1
    # Match every unchanged task first, so changed cards cannot steal their identity.
    matches = {}
    for index, task in enumerate(new):
        for old_index, previous in enumerate(old):
            if old_index not in matched and signature(task) == signature(previous):
                matches[index] = (previous, False)
                matched.add(old_index)
                break
    for index, task in enumerate(new):
        if index not in matches and origins[origin(task)] == 1:
            candidates = [(i, value) for i, value in enumerate(old)
                          if i not in matched and origin(value) == origin(task)]
            if len(candidates) == 1:
                old_index, previous = candidates[0]
                matches[index] = (previous, True)
                matched.add(old_index)
        if index in matches:
            previous, changed = matches[index]
            task["task_id"] = previous.get("task_id")
            task["state"] = previous.get("state") or TODO
            task["history"] = deepcopy(previous.get("history") or [])
            if changed:
                task["previous_content"] = previous.get("content")
                if task["state"] in (DONE, CANCELLED):
                    update_state([task], task["task_id"], REOPENED if task["state"] == DONE else TODO,
                                 actor="system", reason=reason)
        candidate = str(task.get("task_id") or "")
        if not candidate or candidate in used or (index not in matches and candidate in reserved):
            while f"T-{next_id:03d}" in reserved or f"T-{next_id:03d}" in used:
                next_id += 1
            candidate = f"T-{next_id:03d}"
        task["task_id"] = candidate
        used.add(candidate)
    return new


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, ())


def update_state(
    tasks: List[Dict[str, Any]],
    task_id: str,
    to_state: str,
    *,
    actor: str = "patient",
    reason: str = "",
) -> TaskUpdate:
    """变更单个任务状态（就地修改传入列表中的任务字典）。"""
    if to_state not in TASK_STATES:
        raise TaskError(f"未知任务状态：{to_state}")
    task = find_task(tasks, task_id)
    from_state = str(task.get("state") or TODO)
    if not can_transition(from_state, to_state):
        raise TaskError(
            f"非法任务状态转移：{STATE_LABELS.get(from_state, from_state)} → "
            f"{STATE_LABELS.get(to_state, to_state)}"
            f"（允许：{', '.join(STATE_LABELS.get(s, s) for s in TRANSITIONS.get(from_state, ())) or '无'}）"
        )

    task["state"] = to_state
    history = task.setdefault("history", [])
    history.append({"from": from_state, "to": to_state, "actor": actor,
                    "reason": reason, "at": _now()})

    return TaskUpdate(
        task_id=str(task_id), from_state=from_state, to_state=to_state,
        actor=actor, reason=reason, at=_now(),
        trigger_recompute=(to_state == DONE and task.get("type") in RECOMPUTE_TRIGGER_TYPES),
    )


def summarize(tasks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """任务概览（前端进度条/徽章用）。"""
    counts = {state: 0 for state in TASK_STATES}
    for task in tasks or []:
        state = str(task.get("state") or TODO)
        counts[state] = counts.get(state, 0) + 1
    total = len(tasks or [])
    done = counts.get(DONE, 0)
    return {
        "total": total,
        "by_state": counts,
        "done": done,
        "open": total - done - counts.get(CANCELLED, 0),
        "progress": round(done / total, 2) if total else 0.0,
    }


def validate_tasks(tasks: Sequence[Dict[str, Any]]) -> List[str]:
    """自检：类型/状态是否在枚举内（供单测与数据校验）。"""
    problems: List[str] = []
    for task in tasks or []:
        if task.get("type") not in TASK_TYPES:
            problems.append(f"{task.get('task_id')} 类型非法：{task.get('type')}")
        if task.get("state") not in TASK_STATES:
            problems.append(f"{task.get('task_id')} 状态非法：{task.get('state')}")
    return problems


def create_task(case_id: str, index: int, type_: str, content: str,
                priority: str = "medium", source: str = "") -> Dict[str, Any]:
    """构造一条任务（与 schemas.TaskItem 字段一致，但以 dict 形式存放在病例 payload 中）。"""
    if type_ not in TASK_TYPES:
        raise TaskError(f"未知任务类型：{type_}")
    return {
        "task_id": f"T-{index:03d}",
        "case_id": case_id,
        "type": type_,
        "content": content,
        "priority": priority if priority in ("high", "medium", "low") else "medium",
        "source": source,
        "state": TODO,
        "history": [],
    }
