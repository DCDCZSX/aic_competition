"""就医路径与任务中心（Action Center）。

对应《产品开发规格书-比赛版v-competition.md》§7.2（患者端 P7）与主文档 §7.2/§7.3。

规则（写死）：
- 路径以**条件分支**表达，不写成确定性医疗处置流程；
- 任务来源 = 分诊路径步骤 ∪ 各科 high checks ∪ 医生补资料请求；
- 任务类型枚举：`资料准备 | 就医咨询 | 就诊后资料更新`（与 `schemas.TASK_TYPES` 一致）；
- **涉及检查/停药/换药/治疗/手术等医疗行为，只能生成"建议向医生确认是否需要××"**，
  不得下指令（`schemas.TaskItem.validate` 会直接拒绝指令式文案）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import TaskItem  # noqa: E402
from services.triage import TriageOutcome  # noqa: E402

# 医疗行为类任务统一前缀（红线：不得指令式）
CONFIRM_PREFIX = "向医生确认是否需要"

# 各专科常见"就诊前准备"提示（来自分诊结果，不是诊断）
DEPT_PREP: Dict[str, str] = {
    "内分泌科": "整理近 3 个月血糖记录（如有）",
    "肾内科": "带上既往尿常规 / 肾功能报告",
    "眼科": "带上既往眼底检查报告（如有）",
    "心血管内科": "带上心电图 / 心脏彩超报告（如有）",
    "血液科": "带上最近的血常规报告",
    "神经内科": "记录症状出现的时间与变化过程",
    "呼吸内科": "带上胸部影像报告（如有）",
    "风湿免疫科": "整理关节/皮疹等症状的发作时间",
    "感染科": "记录发热时间与最高体温",
    "普通外科": "带上既往影像与化验报告",
    "心胸外科": "带上心脏彩超 / 冠脉相关报告",
    "麻醉科": "准备好既往手术史与用药清单",
}


@dataclass
class PathStep:
    """一个路径步骤（可带条件与依赖）。"""

    step_id: str
    title: str
    detail: str = ""
    kind: str = "prepare"            # prepare(资料准备) / visit(就诊) / check(检查) / followup(复诊)
    condition: str = ""              # 分支条件（如"若肾功能结果异常"）
    depends_on: List[str] = field(default_factory=list)
    parallel: bool = False           # 是否可与前一步并行
    requires_doctor: bool = False    # 是否必须由医生决定

    def to_dict(self) -> Dict[str, Any]:
        return {"step_id": self.step_id, "title": self.title, "detail": self.detail,
                "kind": self.kind, "condition": self.condition,
                "depends_on": list(self.depends_on), "parallel": self.parallel,
                "requires_doctor": self.requires_doctor}


@dataclass
class CarePath:
    """就医路径 + 任务清单。"""

    steps: List[PathStep] = field(default_factory=list)
    tasks: List[TaskItem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "tasks": [
                {"task_id": t.task_id, "type": t.type, "content": t.content,
                 "priority": t.priority, "state": t.state, "source": t.source,
                 "depends_on": list(t.depends_on)}
                for t in self.tasks
            ],
            "notes": list(self.notes),
        }


def build_path(
    *,
    outcome: TriageOutcome,
    gaps: Optional[Sequence[str]] = None,
    checks_by_department: Optional[Dict[str, Sequence[str]]] = None,
    case_id: str = "",
    max_tasks: int = 8,
) -> CarePath:
    """由分诊结果生成就医路径与任务。"""
    path = CarePath()
    depts = outcome.selected
    primary = outcome.primary.department if outcome.primary else (depts[0] if depts else "")

    if not depts:
        path.notes.append("尚未确定建议就诊方向：请补充资料后再生成路径")
        return path

    # ---- 步骤 1：资料准备 ----
    prep_items = [DEPT_PREP[d] for d in depts[:3] if d in DEPT_PREP]
    path.steps.append(PathStep(
        step_id="S1", title="先做好资料准备",
        detail="；".join(prep_items) if prep_items else "整理现有病历、检查报告与用药清单",
        kind="prepare",
    ))

    # ---- 步骤 2：就诊牵头科 ----
    path.steps.append(PathStep(
        step_id="S2", title=f"挂 {primary}（牵头方向）",
        detail="把一页就医摘要带给医生，说明主要问题与已做检查",
        kind="visit", depends_on=["S1"],
    ))

    # ---- 步骤 3：可并行的检查/会诊准备（条件分支）----
    parallel_steps: List[PathStep] = []
    for i, dep in enumerate(depts[1:4]):
        parallel_steps.append(PathStep(
            step_id=f"S3-{i+1}", title=f"准备 {dep} 所需资料",
            detail=DEPT_PREP.get(dep, "整理该专科既往资料"),
            kind="prepare", parallel=True, depends_on=["S1"],
        ))
    if outcome.needs_mdt:
        path.steps.append(PathStep(
            step_id="S3", title="多学科协同准备",
            detail=f"按建议同时准备 {'、'.join(depts[1:4])} 的相关资料（可并行）",
            kind="prepare", parallel=True, depends_on=["S1"],
        ))
    path.steps.extend(parallel_steps)

    # ---- 步骤 4：带结果复诊（条件分支）----
    path.steps.append(PathStep(
        step_id="S4", title="带检查结果复诊",
        detail="复诊时带上新出的检查结果；若结果异常，就医方向可能调整",
        kind="followup", condition="若医生安排了检查", depends_on=["S2"], requires_doctor=True,
    ))

    # ---- 任务生成 ----
    path.tasks = _build_tasks(case_id=case_id, outcome=outcome,
                              checks_by_department=checks_by_department or {},
                              max_tasks=max_tasks)

    path.notes.append("本路径为就医信息组织建议，实际以线下医生判断为准。")
    return path


def _build_tasks(
    *,
    case_id: str,
    outcome: TriageOutcome,
    checks_by_department: Dict[str, Sequence[str]],
    max_tasks: int,
) -> List[TaskItem]:
    """生成任务清单（三种类型，医疗行为一律"确认式"表述）。"""
    tasks: List[TaskItem] = []
    idx = 0

    def add(type_: str, content: str, priority: str, source: str) -> None:
        nonlocal idx
        idx += 1
        tasks.append(TaskItem(
            task_id=f"T-{idx:03d}", case_id=case_id, type=type_, content=content,
            priority=priority, source=source, state="todo",
        ))

    depts = outcome.selected

    # ① 资料准备任务（来自路径）
    for dep in depts[:3]:
        if dep in DEPT_PREP:
            add("资料准备", DEPT_PREP[dep], "high" if dep == (outcome.primary.department if outcome.primary else "") else "medium",
                f"path:{dep}")

    # ② 就医咨询任务（来自各科 high checks，一律"确认式"）
    for dep, checks in (checks_by_department or {}).items():
        for check in list(checks)[:2]:
            add("就医咨询", f"{CONFIRM_PREFIX}：{check}", "high", f"{dep}:checks")

    # ③ 就诊后资料更新任务
    add("就诊后资料更新", "就诊后上传新检查结果与用药变化，系统会更新建议", "medium", "path:followup")

    # ④ 缺口相关的资料准备（把"信息不足"转成可执行任务）
    for gap in (outcome.validator_notes or [])[:1]:
        pass  # 说明类备注不进任务

    # 优先级排序：high → medium → low，并截断
    order = {"high": 0, "medium": 1, "low": 2}
    tasks.sort(key=lambda t: order.get(t.priority, 3))
    tasks = tasks[:max_tasks]

    # 校验：任务文案不得为指令式（schemas 会抛 SchemaError）
    for t in tasks:
        t.validate()
    return tasks
