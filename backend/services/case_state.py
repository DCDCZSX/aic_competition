"""病例/报告状态机与异常处理。

对应《产品开发规格书-比赛版v-competition.md》§7.1（主文档 §16 落地）。

规则：
1. **七态**（与 `schemas.CASE_STATES` 同一权威源）：
   资料待确认 → AI 分析中 → AI 分析完成/待医生审核 → 退回补资料
   → 医生已审阅 → 医生已确认 → 新资料进入后待复核
2. **只允许显式转移表内的跳转**，非法转移抛 `IllegalTransition`；
3. **每次流转留痕**（from / to / actor / reason / at），进入审计时间线（医生端屏 G）；
4. **异常处理（写死）**：模型超时、JSON 解析失败、证据无法核验、规则与模型冲突、
   急症风险不确定 —— 必须①记录明确原因 ②保留原有版本 ③转入人工复核或补资料，
   **不得静默生成空白报告或确定性结论**。

用法：
    sm = CaseStateMachine("C001")
    sm.transition("analyzing", actor="system", reason="患者确认画像")
    sm.record_anomaly("timeout", "内分泌科分析超时", actor="system")
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Tuple

# 允许单独运行（把 backend/ 加入路径）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import CASE_STATES, CASE_STATE_LABELS  # noqa: E402

STATE_PENDING = "pending_materials"
STATE_ANALYZING = "analyzing"
STATE_AWAITING_DOCTOR = "awaiting_doctor"
STATE_NEED_MORE_INFO = "need_more_info"
STATE_DOCTOR_REVIEWED = "doctor_reviewed"
STATE_DOCTOR_CONFIRMED = "doctor_confirmed"
STATE_AWAITING_RECHECK = "awaiting_recheck"

# 显式转移表：当前状态 → 允许进入的下一状态
TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    STATE_PENDING: (STATE_ANALYZING, STATE_NEED_MORE_INFO),
    STATE_ANALYZING: (STATE_AWAITING_DOCTOR, STATE_NEED_MORE_INFO),
    STATE_AWAITING_DOCTOR: (STATE_DOCTOR_REVIEWED, STATE_NEED_MORE_INFO, STATE_ANALYZING),
    STATE_NEED_MORE_INFO: (STATE_ANALYZING, STATE_AWAITING_DOCTOR),
    STATE_DOCTOR_REVIEWED: (STATE_DOCTOR_CONFIRMED, STATE_NEED_MORE_INFO),
    STATE_DOCTOR_CONFIRMED: (STATE_AWAITING_RECHECK,),
    STATE_AWAITING_RECHECK: (STATE_ANALYZING, STATE_AWAITING_DOCTOR),
}

# 异常类型（规格书 §7.1 列举）
ANOMALY_KINDS: Tuple[str, ...] = (
    "timeout",                # 模型/专科超时
    "json_parse_failed",      # 结构化输出解析失败
    "evidence_unverifiable",  # 证据无法核验
    "rule_model_conflict",    # 规则与模型冲突
    "crisis_uncertain",       # 急症风险不确定
    "schema_invalid",         # schema 校验失败
    "unknown",                # 其他
)

ANOMALY_LABELS: Dict[str, str] = {
    "timeout": "分析超时",
    "json_parse_failed": "结构化输出解析失败",
    "evidence_unverifiable": "证据无法核验",
    "rule_model_conflict": "规则与模型冲突",
    "crisis_uncertain": "急症风险不确定",
    "schema_invalid": "数据校验失败",
    "unknown": "未知异常",
}


class IllegalTransition(ValueError):
    """非法状态转移。"""


class UnknownState(ValueError):
    """未知状态。"""


@dataclass
class Transition:
    """一次状态流转记录（审计时间线的一条）。"""

    from_state: str
    to_state: str
    actor: str = "system"
    reason: str = ""
    at: str = ""
    is_anomaly: bool = False
    anomaly_kind: str = ""


@dataclass
class CaseStateMachine:
    """单个病例/报告的状态机。

    - `version`：报告版本号。异常**不会**重置或清空版本（保留原版本）；
      只有真正产出新报告时才由调用方 `bump_version()`。
    - `history`：完整审计时间线。
    """

    case_id: str
    state: str = STATE_PENDING
    version: int = 1
    history: List[Transition] = field(default_factory=list)
    anomalies: List[Transition] = field(default_factory=list)
    clock: Callable[[], str] = field(
        default=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def __post_init__(self) -> None:
        if self.state not in CASE_STATES:
            raise UnknownState(f"未知状态: {self.state!r}（允许: {', '.join(CASE_STATES)}）")
        if not self.case_id:
            raise ValueError("case_id 不能为空")

    # ---- 查询 ----
    @property
    def label(self) -> str:
        """中文状态名（前端/报告展示用）。"""
        return CASE_STATE_LABELS.get(self.state, self.state)

    def allowed_next(self) -> Tuple[str, ...]:
        return TRANSITIONS.get(self.state, ())

    def can_transition(self, to_state: str) -> bool:
        return to_state in self.allowed_next()

    def is_confirmed(self) -> bool:
        """是否已出医生确认版（只有此时报告才可标"审核版"）。"""
        return self.state == STATE_DOCTOR_CONFIRMED

    # ---- 变更 ----
    def transition(self, to_state: str, actor: str = "system", reason: str = "") -> Transition:
        """执行状态流转；非法转移抛 IllegalTransition。"""
        if to_state not in CASE_STATES:
            raise UnknownState(f"未知状态: {to_state!r}")
        if not self.can_transition(to_state):
            raise IllegalTransition(
                f"非法状态转移: {self.state}({self.label}) → {to_state}"
                f"（允许: {', '.join(self.allowed_next()) or '无'}）"
            )
        record = Transition(
            from_state=self.state, to_state=to_state, actor=actor,
            reason=reason, at=self.clock(),
        )
        self.state = to_state
        self.history.append(record)
        return record

    def record_anomaly(self, kind: str, detail: str, actor: str = "system") -> Transition:
        """记录异常并转入人工复核/补资料（规格书 §7.1）。

        - 未知 kind 归为 `unknown`，但仍会记录（不静默吞掉）；
        - **保留原版本**（version 不变）；
        - 若当前状态无法直接转 `need_more_info`，则先记录异常、状态保持不变，
          由调用方决定后续（但异常一定留痕、绝不静默）。
        """
        if kind not in ANOMALY_KINDS:
            kind = "unknown"
        label = ANOMALY_LABELS.get(kind, "未知异常")
        record = Transition(
            from_state=self.state, to_state=self.state, actor=actor,
            reason=f"{label}：{detail}", at=self.clock(), is_anomaly=True,
            anomaly_kind=kind,
        )

        target = STATE_NEED_MORE_INFO
        if self.can_transition(target):
            record.to_state = target
            self.state = target

        self.history.append(record)
        self.anomalies.append(record)
        return record

    def bump_version(self) -> int:
        """产出新报告时调用（异常不会自动触发）。"""
        self.version += 1
        return self.version

    def timeline(self) -> List[Dict[str, object]]:
        """审计时间线（医生端屏 G / 报告"审核记录"段）。"""
        return [
            {
                "at": t.at,
                "from": CASE_STATE_LABELS.get(t.from_state, t.from_state),
                "to": CASE_STATE_LABELS.get(t.to_state, t.to_state),
                "actor": t.actor,
                "reason": t.reason,
                "is_anomaly": t.is_anomaly,
            }
            for t in self.history
        ]
