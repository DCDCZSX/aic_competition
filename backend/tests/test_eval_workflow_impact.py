import json
import os
import sys
import tempfile
import unittest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval_workflow_impact import analyze_records, load_jsonl, render_markdown, validate_record


def doctor_record(pair_id, condition, *, outcome="completed", duration=100,
                  issue_code=None, protocol_deviation=False):
    record = {
        "schema_version": "workflow-impact.v1",
        "pair_id": pair_id,
        "participant_id": f"participant-{condition}",
        "actor": "doctor",
        "condition": condition,
        "outcome": outcome,
        "duration_sec": duration,
        "protocol_deviation": protocol_deviation,
        "doctor": {
            "required_review_items": 5,
            "reviewed_items": 5,
            "modified_items": 1,
            "rejected_items": 0,
            "disagreement_items": 2,
            "disagreements_resolved": 1,
            "missing_info_round_trips": 1,
            "high_risk_items": 2,
            "high_risk_reviewed_items": 2,
            "specialty_expected_count": 3,
            "specialty_covered_count": 3,
        },
    }
    if issue_code:
        record["issue_code"] = issue_code
    return record


def patient_record(pair_id, *, outcome="completed", duration=240):
    return {
        "schema_version": "workflow-impact.v1",
        "pair_id": pair_id,
        "participant_id": "participant-patient",
        "actor": "patient",
        "condition": "system",
        "outcome": outcome,
        "duration_sec": duration,
        "protocol_deviation": False,
        "operations": {"back": 1, "error": 0, "stage_return": 0, "summary_expand": 2},
        "patient": {
            "stage_seconds": {"fill": 60, "verify": 70, "advice": 80, "summary": 30},
            "stage_completed": {"fill": True, "verify": True, "advice": True, "summary": True},
            "summary_tasks_total": 4,
            "summary_tasks_found": 3,
            "summary_readability_score": 4,
            "summary_comprehension_score": 4,
        },
    }


class WorkflowImpactEvaluationTests(unittest.TestCase):
    def test_valid_record_and_pii_fields_are_distinguished(self):
        valid = doctor_record("pair-001", "manual")
        self.assertEqual(validate_record(valid), [])

        invalid = dict(valid)
        invalid["main_issue"] = "患者有发热"
        self.assertIn("unknown_field", validate_record(invalid))

        invalid_nested = doctor_record("pair-002", "manual")
        invalid_nested["doctor"] = dict(invalid_nested["doctor"], review_note="原始病例文本")
        self.assertIn("unknown_doctor_field", validate_record(invalid_nested))

    def test_jsonl_loader_reports_line_errors_without_echoing_content(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(json.dumps(doctor_record("pair-001", "manual"), ensure_ascii=False) + "\n")
            handle.write('{"pair_id": "bad", "main_issue": "不应输出"}\n')
            path = handle.name
        try:
            records, issues = load_jsonl(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(records), 1)
        self.assertTrue(any(issue["code"] == "unknown_field" for issue in issues))
        self.assertNotIn("不应输出", json.dumps(issues, ensure_ascii=False))

    def test_analysis_uses_only_completed_matched_pairs(self):
        records = [
            doctor_record("pair-001", "manual", duration=100),
            doctor_record("pair-001", "system", duration=80),
            doctor_record("pair-002", "manual", duration=120),
            doctor_record("pair-002", "system", outcome="failed", duration=30,
                          issue_code="timeout"),
            patient_record("pair-patient"),
        ]
        result = analyze_records(records, dataset_label="dry-run", evidence_status="dry_run")

        self.assertEqual(result["evidence_status"], "dry_run")
        self.assertEqual(result["status_counts"]["completed"], 4)
        self.assertEqual(result["status_counts"]["failed"], 1)
        doctor = result["actors"]["doctor"]
        self.assertEqual(doctor["matched_pairs"], 1)
        self.assertEqual(doctor["conditions"]["manual"]["duration_sec"]["n"], 2)
        self.assertEqual(doctor["conditions"]["system"]["duration_sec"]["n"], 1)
        self.assertEqual(doctor["paired_duration_delta_sec"]["n"], 1)
        self.assertEqual(doctor["paired_duration_delta_sec"]["mean"], -20.0)
        self.assertEqual(doctor["conditions"]["manual"]["high_risk_review_rate"]["rate"], 1.0)
        patient = result["actors"]["patient"]
        self.assertEqual(patient["matched_pairs"], 0)
        self.assertEqual(patient["conditions"]["system"]["summary_readability_score"]["n"], 1)

    def test_duplicate_and_protocol_deviation_are_excluded_from_pairs(self):
        records = [
            doctor_record("pair-dup", "manual"),
            doctor_record("pair-dup", "manual", duration=101),
            doctor_record("pair-dup", "system"),
            doctor_record("pair-deviation", "manual", protocol_deviation=True),
            doctor_record("pair-deviation", "system"),
        ]
        result = analyze_records(records, dataset_label="pilot", evidence_status="pilot")
        self.assertEqual(result["actors"]["doctor"]["matched_pairs"], 0)
        self.assertGreaterEqual(result["exclusions"]["duplicate_observation"], 1)
        self.assertGreaterEqual(result["exclusions"]["protocol_deviation"], 1)

    def test_markdown_reports_denominators_and_dry_run_boundary(self):
        result = analyze_records(
            [doctor_record("pair-001", "manual"), doctor_record("pair-001", "system")],
            dataset_label="dry-run",
            evidence_status="dry_run",
        )
        markdown = render_markdown(result)
        self.assertIn("dry_run", markdown)
        self.assertIn("分母", markdown)
        self.assertIn("不能解释为临床有效性或实际节省时间", markdown)
        self.assertIn("医生", markdown)

    def test_invalid_record_count_is_distinct_from_error_count(self):
        result = analyze_records(
            [],
            dataset_label="dry-run",
            evidence_status="dry_run",
            load_issues=[
                {"line": 2, "code": "unknown_field"},
                {"line": 2, "code": "invalid_actor"},
                {"line": 4, "code": "invalid_json"},
            ],
        )
        self.assertEqual(result["invalid_records"], 2)
        self.assertEqual(result["validation_errors"], 3)

    def test_dataset_label_cannot_contain_free_text(self):
        with self.assertRaises(ValueError):
            analyze_records([], dataset_label="患者原文", evidence_status="dry_run")


if __name__ == "__main__":
    unittest.main()
