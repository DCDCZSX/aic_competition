#!/usr/bin/env python3
"""汇总患者/医生流程影响评测的脱敏 JSONL 记录。

这个脚本只处理结构化观察值，不读取病例正文、报告正文或数据库。输入记录
使用不可逆匿名 ID 配对人工整理组与系统辅助组，输出中的配对结果只保留计数
和汇总值，不回显任何 ID。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "workflow-impact.v1"
ACTORS = ("patient", "doctor")
CONDITIONS = ("manual", "system")
OUTCOMES = ("completed", "failed", "excluded")
STAGES = ("fill", "verify", "advice", "summary")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

ROOT_FIELDS = {
    "schema_version", "pair_id", "participant_id", "actor", "condition", "outcome",
    "duration_sec", "protocol_deviation", "issue_code", "operations", "patient", "doctor",
}
OPERATION_FIELDS = {"back", "error", "stage_return", "summary_expand", "decision", "material_round_trip"}
PATIENT_FIELDS = {
    "stage_seconds", "stage_completed", "summary_tasks_total", "summary_tasks_found",
    "summary_readability_score", "summary_comprehension_score",
}
DOCTOR_FIELDS = {
    "required_review_items", "reviewed_items", "modified_items", "rejected_items",
    "disagreement_items", "disagreements_resolved", "missing_info_round_trips",
    "high_risk_items", "high_risk_reviewed_items",
    "specialty_expected_count", "specialty_covered_count",
}
ACTOR_FIELDS = {"patient": PATIENT_FIELDS, "doctor": DOCTOR_FIELDS}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_nonnegative_number(value: Any, code: str, *, maximum: float = 86400.0) -> List[str]:
    if not _is_number(value):
        return [code]
    if float(value) < 0 or float(value) > maximum:
        return [code]
    return []


def _validate_count(value: Any, code: str, *, maximum: int = 100000) -> List[str]:
    if not _is_int(value) or value < 0 or value > maximum:
        return [code]
    return []


def _validate_actor_payload(record: Mapping[str, Any], actor: str) -> List[str]:
    errors: List[str] = []
    payload = record.get(actor)
    other = "doctor" if actor == "patient" else "patient"
    if payload is None:
        if record.get("outcome") == "completed":
            errors.append("missing_actor_payload")
        return errors
    if not isinstance(payload, dict):
        return ["invalid_actor_payload"]
    unknown = set(payload) - ACTOR_FIELDS[actor]
    errors.extend(f"unknown_{actor}_field" for _ in unknown)
    if record.get(other) is not None:
        errors.append("unexpected_actor_payload")

    if actor == "patient":
        stage_seconds = payload.get("stage_seconds")
        if stage_seconds is not None:
            if not isinstance(stage_seconds, dict):
                errors.append("invalid_stage_seconds")
            else:
                errors.extend("unknown_stage" for key in set(stage_seconds) - set(STAGES))
                for value in stage_seconds.values():
                    errors.extend(_validate_nonnegative_number(value, "invalid_stage_seconds"))
        stage_completed = payload.get("stage_completed")
        if stage_completed is not None:
            if not isinstance(stage_completed, dict):
                errors.append("invalid_stage_completed")
            else:
                errors.extend("unknown_stage" for key in set(stage_completed) - set(STAGES))
                if any(not isinstance(value, bool) for value in stage_completed.values()):
                    errors.append("invalid_stage_completed")
        for key in ("summary_tasks_total", "summary_tasks_found"):
            if key in payload:
                errors.extend(_validate_count(payload[key], f"invalid_{key}"))
        total = payload.get("summary_tasks_total")
        found = payload.get("summary_tasks_found")
        if _is_int(total) and _is_int(found) and found > total:
            errors.append("summary_tasks_found_gt_total")
        for key in ("summary_readability_score", "summary_comprehension_score"):
            if key in payload and (not _is_int(payload[key]) or not 1 <= payload[key] <= 5):
                errors.append(f"invalid_{key}")
    else:
        for key in DOCTOR_FIELDS:
            if key in payload:
                errors.extend(_validate_count(payload[key], f"invalid_{key}"))
        relationships = (
            ("reviewed_items", "required_review_items", "reviewed_gt_required"),
            ("modified_items", "reviewed_items", "modified_gt_reviewed"),
            ("rejected_items", "reviewed_items", "rejected_gt_reviewed"),
            ("disagreements_resolved", "disagreement_items", "resolved_gt_disagreements"),
            ("high_risk_reviewed_items", "high_risk_items", "high_risk_reviewed_gt_total"),
            ("specialty_covered_count", "specialty_expected_count", "covered_gt_expected"),
        )
        for child, parent, error_code in relationships:
            if _is_int(payload.get(child)) and _is_int(payload.get(parent)) and payload[child] > payload[parent]:
                errors.append(error_code)
    return errors


def validate_record(record: Any) -> List[str]:
    """返回记录中的错误代码；不返回输入文本，便于安全地写入错误报告。"""
    if not isinstance(record, dict):
        return ["record_not_object"]
    errors: List[str] = []
    errors.extend(f"unknown_field" for _ in (set(record) - ROOT_FIELDS))
    required = ("schema_version", "pair_id", "participant_id", "actor", "condition", "outcome")
    errors.extend(f"missing_{key}" for key in required if key not in record)
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported_schema_version")
    for key in ("pair_id", "participant_id"):
        value = record.get(key)
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            errors.append(f"invalid_{key}")
    actor = record.get("actor")
    condition = record.get("condition")
    outcome = record.get("outcome")
    if actor not in ACTORS:
        errors.append("invalid_actor")
    if condition not in CONDITIONS:
        errors.append("invalid_condition")
    if outcome not in OUTCOMES:
        errors.append("invalid_outcome")
    if "protocol_deviation" in record and not isinstance(record["protocol_deviation"], bool):
        errors.append("invalid_protocol_deviation")
    if "issue_code" in record:
        value = record["issue_code"]
        if not isinstance(value, str) or not CODE_RE.fullmatch(value):
            errors.append("invalid_issue_code")
    if "duration_sec" in record:
        errors.extend(_validate_nonnegative_number(record["duration_sec"], "invalid_duration_sec"))
        if outcome == "completed" and _is_number(record["duration_sec"]) and float(record["duration_sec"]) <= 0:
            errors.append("completed_duration_not_positive")
    elif outcome == "completed":
        errors.append("missing_duration_sec")
    operations = record.get("operations")
    if operations is not None:
        if not isinstance(operations, dict):
            errors.append("invalid_operations")
        else:
            errors.extend("unknown_operation" for _ in (set(operations) - OPERATION_FIELDS))
            for key, value in operations.items():
                errors.extend(_validate_count(value, f"invalid_operation_{key}"))
    if actor in ACTORS:
        errors.extend(_validate_actor_payload(record, actor))
    return sorted(set(errors))


def load_jsonl(path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """读取并校验 JSONL；错误中只保留行号和错误代码。"""
    records: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                issues.append({"line": line_number, "code": "invalid_json"})
                continue
            codes = validate_record(record)
            if codes:
                issues.extend({"line": line_number, "code": code} for code in codes)
                continue
            records.append(record)
    return records, issues


def _numeric_summary(values: Iterable[float]) -> Dict[str, Any]:
    numbers = [float(value) for value in values if _is_number(value)]
    if not numbers:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(numbers),
        "mean": round(statistics.mean(numbers), 3),
        "median": round(statistics.median(numbers), 3),
        "min": round(min(numbers), 3),
        "max": round(max(numbers), 3),
    }


def _ratio_summary(pairs: Iterable[Tuple[float, float]]) -> Dict[str, Any]:
    numerator = 0.0
    denominator = 0.0
    observations = 0
    for num, den in pairs:
        if not _is_number(num) or not _is_number(den) or float(den) <= 0:
            continue
        numerator += float(num)
        denominator += float(den)
        observations += 1
    return {
        "n": observations,
        "numerator": round(numerator, 3),
        "denominator": round(denominator, 3),
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def _record_metric(record: Mapping[str, Any], actor: str, metric: str) -> Optional[float]:
    payload = record.get(actor) or {}
    if metric == "duration_sec":
        return float(record["duration_sec"]) if _is_number(record.get("duration_sec")) else None
    operations = record.get("operations") or {}
    if metric in OPERATION_FIELDS:
        return float(operations[metric]) if _is_number(operations.get(metric)) else None
    if actor == "patient":
        if metric.startswith("stage_") and metric.endswith("_seconds"):
            stage = metric[len("stage_"):-len("_seconds")]
            value = (payload.get("stage_seconds") or {}).get(stage)
            return float(value) if _is_number(value) else None
        if metric in ("summary_readability_score", "summary_comprehension_score"):
            value = payload.get(metric)
            return float(value) if _is_number(value) else None
    if actor == "doctor":
        value = payload.get(metric)
        return float(value) if _is_number(value) else None
    return None


def _condition_metrics(records: Sequence[Mapping[str, Any]], actor: str) -> Dict[str, Any]:
    completed = [record for record in records if record.get("outcome") == "completed"]
    output: Dict[str, Any] = {
        "records": len(records),
        "completed": len(completed),
        "failed": sum(record.get("outcome") == "failed" for record in records),
        "excluded": sum(record.get("outcome") == "excluded" for record in records),
    }
    metric_names = ["duration_sec", "back", "error", "stage_return", "summary_expand"]
    if actor == "patient":
        metric_names.extend(
            [f"stage_{stage}_seconds" for stage in STAGES]
            + ["summary_readability_score", "summary_comprehension_score"]
        )
    else:
        metric_names.extend(
            ["reviewed_items", "modified_items", "rejected_items", "missing_info_round_trips"]
        )
    output.update({metric: _numeric_summary(_record_metric(record, actor, metric) for record in completed)
                  for metric in metric_names})

    if actor == "patient":
        output["summary_task_completion"] = _ratio_summary(
            ((record.get("patient", {}).get("summary_tasks_found"),
              record.get("patient", {}).get("summary_tasks_total")) for record in completed)
        )
        for stage in STAGES:
            output[f"stage_{stage}_completion"] = _ratio_summary(
                ((1 if (record.get("patient", {}).get("stage_completed") or {}).get(stage) else 0, 1)
                 for record in completed
                 if stage in (record.get("patient", {}).get("stage_completed") or {}))
            )
    else:
        output["review_coverage"] = _ratio_summary(
            ((record.get("doctor", {}).get("reviewed_items"),
              record.get("doctor", {}).get("required_review_items")) for record in completed)
        )
        output["modification_rate"] = _ratio_summary(
            ((record.get("doctor", {}).get("modified_items"),
              record.get("doctor", {}).get("reviewed_items")) for record in completed)
        )
        output["rejection_rate"] = _ratio_summary(
            ((record.get("doctor", {}).get("rejected_items"),
              record.get("doctor", {}).get("reviewed_items")) for record in completed)
        )
        output["disagreement_resolution_rate"] = _ratio_summary(
            ((record.get("doctor", {}).get("disagreements_resolved"),
              record.get("doctor", {}).get("disagreement_items")) for record in completed)
        )
        output["high_risk_review_rate"] = _ratio_summary(
            ((record.get("doctor", {}).get("high_risk_reviewed_items"),
              record.get("doctor", {}).get("high_risk_items")) for record in completed)
        )
        output["specialty_coverage"] = _ratio_summary(
            ((record.get("doctor", {}).get("specialty_covered_count"),
              record.get("doctor", {}).get("specialty_expected_count")) for record in completed)
        )
    return output


def analyze_records(
    records: Sequence[Mapping[str, Any]],
    *,
    dataset_label: str,
    evidence_status: str,
    load_issues: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """汇总已通过 ``validate_record`` 的记录。"""
    if not isinstance(dataset_label, str) or not CODE_RE.fullmatch(dataset_label):
        raise ValueError("dataset_label 只能包含匿名评测标签字符")
    if evidence_status not in {"dry_run", "pilot", "final"}:
        raise ValueError("evidence_status 不受支持")
    status_counts = Counter(str(record.get("outcome")) for record in records)
    status_counts.setdefault("completed", 0)
    status_counts.setdefault("failed", 0)
    status_counts.setdefault("excluded", 0)
    exclusions: Counter[str] = Counter()
    issue_list = list(load_issues or [])
    issue_counts = Counter(str(issue.get("code")) for issue in issue_list)
    invalid_lines = {issue.get("line") for issue in issue_list if issue.get("line") is not None}

    actors: Dict[str, Any] = {}
    for actor in ACTORS:
        actor_records = [record for record in records if record.get("actor") == actor]
        by_condition = {
            condition: _condition_metrics(
                [record for record in actor_records if record.get("condition") == condition], actor
            )
            for condition in CONDITIONS
        }

        grouped: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
        for record in actor_records:
            grouped[(str(record.get("pair_id")), str(record.get("condition")))].append(record)
        pair_ids = {key[0] for key in grouped}
        matched_pairs: List[Tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for pair_id in sorted(pair_ids):
            manual = grouped.get((pair_id, "manual"), [])
            system = grouped.get((pair_id, "system"), [])
            if len(manual) > 1 or len(system) > 1:
                exclusions["duplicate_observation"] += 1
                continue
            if not manual or not system:
                exclusions["missing_condition"] += 1
                continue
            left, right = manual[0], system[0]
            if left.get("protocol_deviation") or right.get("protocol_deviation"):
                exclusions["protocol_deviation"] += 1
                continue
            if left.get("outcome") != "completed" or right.get("outcome") != "completed":
                exclusions["incomplete_pair"] += 1
                continue
            matched_pairs.append((left, right))

        deltas = []
        for manual, system in matched_pairs:
            manual_duration = manual.get("duration_sec")
            system_duration = system.get("duration_sec")
            if _is_number(manual_duration) and _is_number(system_duration):
                deltas.append(float(system_duration) - float(manual_duration))
        actors[actor] = {
            "records": len(actor_records),
            "matched_pairs": len(matched_pairs),
            "conditions": by_condition,
            "paired_duration_delta_sec": _numeric_summary(deltas),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_label": str(dataset_label),
        "evidence_status": str(evidence_status),
        "total_records": len(records),
        "invalid_records": len(invalid_lines) if invalid_lines else len(issue_list),
        "validation_errors": sum(issue_counts.values()),
        "status_counts": dict(status_counts),
        "validation_issues": dict(issue_counts),
        "exclusions": dict(exclusions),
        "actors": actors,
        "limitations": [
            "当前汇总只反映记录到输入文件中的流程和工作量，不是诊断准确性或临床结局证据。",
            "dry_run 或没有合格人工评审者的样本不能用于宣称患者获益、医生节省时间或临床有效。",
            "配对差值只使用两侧均完成且没有协议偏离的观察；缺一侧、失败和重复记录不会被补零。",
        ],
    }


def _format_metric(metric: Mapping[str, Any]) -> str:
    if metric.get("n", 0) == 0:
        return "未记录"
    if "rate" in metric:
        return f"{metric['rate']:.1%}（{metric['numerator']}/{metric['denominator']}，n={metric['n']}）"
    return f"均值 {metric['mean']}，中位数 {metric['median']}（n={metric['n']}）"


def render_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# 流程影响评测结果",
        "",
        f"- 数据集：`{result.get('dataset_label')}`",
        f"- 证据状态：`{result.get('evidence_status')}`",
        f"- 有效输入记录：{result.get('total_records', 0)}；校验拒绝记录：{result.get('invalid_records', 0)}",
        "",
        "> 本报告只汇总脱敏结构化观察，不包含患者原文、报告正文或匿名 ID。",
        "> dry-run / pilot 结果不能解释为临床有效性或实际节省时间。",
        "",
        "## 记录状态与分母",
        "",
        "| 状态 | 记录数 |",
        "|---|---:|",
    ]
    for status in OUTCOMES:
        lines.append(f"| {status} | {result.get('status_counts', {}).get(status, 0)} |")
    lines.extend([
        "",
        "所有比例都在对应指标表中给出分子、分母和 n；失败、缺一侧、重复及协议偏离不会补零进入有效配对。",
        "",
        "## 患者端",
        "",
    ])
    patient = result.get("actors", {}).get("patient", {})
    lines.append(f"有效配对数：{patient.get('matched_pairs', 0)}；记录数：{patient.get('records', 0)}。")
    for condition in CONDITIONS:
        data = patient.get("conditions", {}).get(condition, {})
        lines.append(f"\n### {condition}\n")
        lines.append(f"记录 {data.get('records', 0)}，完成 {data.get('completed', 0)}；总耗时：{_format_metric(data.get('duration_sec', {}))}。")
        lines.append(f"四阶段完成率：填写 {_format_metric(data.get('stage_fill_completion', {}))}；核对 {_format_metric(data.get('stage_verify_completion', {}))}；建议 {_format_metric(data.get('stage_advice_completion', {}))}；摘要 {_format_metric(data.get('stage_summary_completion', {}))}。")
        lines.append(f"摘要查找完成率：{_format_metric(data.get('summary_task_completion', {}))}；可读性评分：{_format_metric(data.get('summary_readability_score', {}))}。")
    lines.extend(["", "## 医生端", ""])
    doctor = result.get("actors", {}).get("doctor", {})
    lines.append(f"有效配对数：{doctor.get('matched_pairs', 0)}；记录数：{doctor.get('records', 0)}。")
    for condition in CONDITIONS:
        data = doctor.get("conditions", {}).get(condition, {})
        lines.append(f"\n### {condition}\n")
        lines.append(f"记录 {data.get('records', 0)}，完成 {data.get('completed', 0)}；审核耗时：{_format_metric(data.get('duration_sec', {}))}。")
        lines.append(f"必审覆盖率：{_format_metric(data.get('review_coverage', {}))}；修改率：{_format_metric(data.get('modification_rate', {}))}；驳回率：{_format_metric(data.get('rejection_rate', {}))}。")
        lines.append(f"分歧处理完成率：{_format_metric(data.get('disagreement_resolution_rate', {}))}；高风险条目复核率：{_format_metric(data.get('high_risk_review_rate', {}))}；专科覆盖率：{_format_metric(data.get('specialty_coverage', {}))}。")
    lines.extend(["", "## 配对差值", ""])
    for actor, label in (("patient", "患者"), ("doctor", "医生")):
        delta = result.get("actors", {}).get(actor, {}).get("paired_duration_delta_sec", {})
        lines.append(f"- {label}：系统组耗时 − 人工组耗时：{_format_metric(delta)}。")
    lines.extend(["", "## 校验、失败与排除", ""])
    lines.append(f"校验错误代码计数：`{json.dumps(result.get('validation_issues', {}), ensure_ascii=False, sort_keys=True)}`。")
    lines.append(f"配对排除计数：`{json.dumps(result.get('exclusions', {}), ensure_ascii=False, sort_keys=True)}`。")
    lines.extend(["", "## 局限", ""])
    lines.extend(f"- {item}" for item in result.get("limitations", []))
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="脱敏 workflow-impact.v1 JSONL")
    parser.add_argument("--output", required=True, help="Markdown 输出路径")
    parser.add_argument("--json-output", help="可选 JSON 输出路径")
    parser.add_argument("--dataset-label", default="unspecified", help="数据集标签，不含病例原文")
    parser.add_argument("--evidence-status", choices=("dry_run", "pilot", "final"), default="dry_run")
    parser.add_argument("--fail-on-invalid", action="store_true", help="存在校验错误时返回退出码 2")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    records, issues = load_jsonl(args.input)
    result = analyze_records(
        records,
        dataset_label=args.dataset_label,
        evidence_status=args.evidence_status,
        load_issues=issues,
    )
    markdown = render_markdown(result)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    if args.json_output:
        json_output = Path(args.json_output)
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"records={result['total_records']} invalid={result['invalid_records']} output={output}")
    if args.fail_on_invalid and issues:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
