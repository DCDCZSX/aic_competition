"""医生端真实 HTTP 验证：分流、会诊记录、逐项审核、预览发布与资料复核。

先启动应用，再运行 python verify_doctor_http.py。账号从项目 .env 或环境变量读取。
全部使用本脚本创建的合成病例；既检查成功流程，也检查权限和发布阻断。
"""
import json
import os
import sys
import time

import requests
from verify_http_helpers import login, preview_token

BASE = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
CASE = ("女，62岁。视物模糊3月，双下肢浮肿1周。既往2型糖尿病5年，高血压10年。"
        "尿蛋白++，空腹血糖9.2mmol/L。")
ADVICE = "请携带已有资料复诊，向接诊医生确认后续评估安排。"
ok_count = 0
fail_count = 0


def check(name, cond, detail=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        fail_count += 1
        print(f"  [FAIL] {name} — {detail}")


def api(session, method, path, body=None, expected=200):
    response = session.request(method, BASE + path, json=body, timeout=120)
    data = response.json()
    if response.status_code != expected:
        raise RuntimeError(f"{path} 返回 HTTP {response.status_code}，预期 {expected}：{data.get('error', '')}")
    return data


def main():
    patient, doctor, anon = requests.Session(), requests.Session(), requests.Session()
    print("[1] 建立合成病例并检查身份边界")
    login(patient, BASE, "patient")
    created = api(patient, "POST", "/api/v2/case", {
        "preview_token": preview_token(patient, BASE, CASE, "就诊前资料准备"),
        "age": 62, "sex": "女",
    })
    case_id = created["case_id"]
    check("建病例并返回编号", created.get("ok") is True and bool(case_id))
    base = f"/api/v2/case/{case_id}"
    access = api(patient, "POST", f"/api/v2/case/{case_id}/access", {
        "reviewer_id": os.getenv("APP_DOCTOR_USERNAME", "doctor").strip(),
    })
    check("患者显式授权医生", access.get("ok") is True)
    submitted = api(patient, "POST", base + "/submit-review", {
        "department_code": os.getenv("APP_DOCTOR_DEPARTMENT", "endocrinology"),
    })
    check("正式提交主责科室后医生获得主责身份", submitted.get("ok") is True)
    for path in ("/api/v2/cases", base + "/review"):
        check("未登录访问医生接口被拒", anon.get(BASE + path, timeout=30).status_code == 401)
    check("患者不能读取医生队列", patient.get(BASE + "/api/v2/cases", timeout=30).status_code == 403)
    denied = api(patient, "POST", base + "/updates", {
        "kind": "医生意见", "content": "患者转述意见", "source": "医生上传", "department": "眼科",
    }, expected=403)
    check("患者不能冒充医生上传意见", bool(denied.get("error")))
    body = login(doctor, BASE, "doctor")
    check("医生身份正确", body["session"]["role"] == "doctor")
    me = api(doctor, "GET", "/api/me")
    check("医生具有审核与决策权限", {"review", "decision"} <= set(me.get("scopes") or []))

    print("\n[2] 按当前流程确认方向并记录实际会诊")
    blocked = api(doctor, "POST", base + "/publish", {"final_advice": ADVICE}, expected=400)
    check("分流未确认时禁止发布",
          {"triage", "mdt_screening"} <= {b["code"] for b in blocked.get("publication_blockers", [])})
    routing = api(doctor, "POST", base + "/doctor-routing", {
        "primary_department": "内分泌科", "secondary_departments": ["眼科"],
        "decision": "recommend_mdt", "reason": "合成病例需要两个专业共同核对资料",
        "purpose": "共同确认已有资料与下一步准备事项",
    })
    check("一次保存科室与MDT分流", routing.get("ok") is True)
    if os.getenv("AI_API_KEY", "").strip():
        api(doctor, "POST", base + "/doctor-mdt-simulation", {})
        deadline = time.monotonic() + 180
        while True:
            simulation = api(doctor, "GET", base + "/doctor-mdt-simulation")["simulation"]
            if simulation["state"] != "running" or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        check("医生确认范围后生成本次独立AI模拟", simulation["state"] == "done",
              simulation.get("message", ""))
    else:
        unavailable = api(doctor, "POST", base + "/doctor-mdt-simulation", {}, expected=400)
        check("无模型时明确拒绝AI模拟", unavailable.get("code") == "model_not_configured")
        simulation = api(doctor, "GET", base + "/doctor-mdt-simulation")["simulation"]
        check("无模型拒绝后不留下虚假的模拟任务", simulation["state"] == "none")
    blocked = api(doctor, "POST", base + "/publish", {"final_advice": ADVICE}, expected=400)
    check("未记录完成实际MDT时禁止发布",
          "mdt_record" in {b["code"] for b in blocked.get("publication_blockers", [])})
    formal = api(doctor, "POST", base + "/mdt-formal-record", {
        "status": "completed", "participants": ["模拟主责医生", "模拟眼科医生"],
        "conclusion": "合成会诊已核对资料，后续评估由接诊医生确认。",
        "next_step": "携带已有检查资料复诊。",
    })
    check("保存独立会诊结论", formal.get("ok") is True)

    print("\n[3] 队列、条目类型与审核责任")
    queue = api(doctor, "GET", "/api/v2/cases?limit=100")
    rows = queue.get("cases") or []
    check("队列含当前病例", any(row["case_id"] == case_id for row in rows))
    check("队列有分页信息", isinstance(queue.get("total"), int) and isinstance(queue.get("has_more"), bool))
    page = api(doctor, "GET", "/api/v2/cases?limit=1&offset=1")
    check("分页最多一条", len(page.get("cases") or []) <= 1)
    filtered = api(doctor, "GET", "/api/v2/cases?state=doctor_confirmed")
    check("状态筛选准确", all(row["case_state"] == "doctor_confirmed" for row in filtered.get("cases", [])))
    check("队列按风险优先", all(rows[i].get("risk_rank", 0) >= rows[i + 1].get("risk_rank", 0)
                              for i in range(len(rows) - 1)))
    review = api(doctor, "GET", base + "/review")
    items = review["items"]
    check("分流与正式记录分别保存",
          review["mdt_screening"]["decision"] == "recommend_mdt"
          and review["mdt_formal_record"]["status"] == "completed")
    types = {item["target_type"] for item in items}
    check("存在就医路径条目", "path_step" in types)
    check("科室选择和患者最终建议不重复进入审核清单",
          not ({"department_selection", "patient_advice"} & types))
    specialty = [item for item in items if item["target_type"] == "ai_analysis"]
    if os.getenv("AI_API_KEY", "").strip():
        check("配置模型时专科意见逐科必审", bool(specialty)
              and all(item["review_tier"] == "required" for item in specialty))
    else:
        check("无模型时不伪造专科AI意见", not specialty)
    required = [item for item in items if item["review_tier"] == "required"]
    automatic = [item for item in items if item["review_tier"] == "automatic"]
    check("待审只统计必审未处理项", review["summary"]["pending"] == len(required))
    check("自动项单独记录", bool(automatic) and all(item["state"] == "auto_processed" for item in automatic))
    check("未发布状态明确", review["published"] is False)
    # 模拟原始数据中的 high/medium/low 是内部证据元数据；核验展示给医生的审核条目和资料包。
    public_review = {key: review.get(key) for key in
                     ("items", "pending_questions", "mdt_preparation_packet", "disagreements")}
    text = json.dumps(public_review, ensure_ascii=False)
    check("可读审核条目和资料包不展示概率或模型置信度",
          not any(t in text for t in ("概率", "置信度", "confidence")))
    blocked = api(doctor, "POST", base + "/publish", {"accept_all": True, "final_advice": ADVICE}, expected=400)
    check("批量发布不能跳过关键必审项",
          "batch_items" in {b["code"] for b in blocked.get("publication_blockers", [])})
    untouched = api(doctor, "GET", base + "/review")
    check("被拒的批量操作不写入审核决定", untouched["decisions"] == review["decisions"])

    print("\n[4] 接受、修改、驳回和暂不确定")
    chosen = (required + [item for item in items if item["review_tier"] != "required"])[:4]
    if len(chosen) != 4:
        raise RuntimeError("合成验收病例没有提供四项可审核内容")
    chosen_ids = [item["item_id"] for item in chosen]

    def decide(item_id, action, expected=200, **fields):
        current = api(doctor, "GET", base + "/review")
        item = next(item for item in current["items"] if item["item_id"] == item_id)
        return api(doctor, "POST", base + "/decisions", {
            "item_id": item_id, "action": action,
            "expected_content_revision": item["content_revision"], **fields,
        }, expected=expected)

    check("接受成功", decide(chosen_ids[0], "accept")["ok"] is True)
    invalid = decide(chosen_ids[1], "modify", expected=400, after="合成修改内容")
    check("修改缺少原因被拒", "原因" in invalid["error"])
    invalid = decide(chosen_ids[1], "modify", expected=400, after="", reason="说明修改依据")
    check("修改缺少内容被拒", "内容" in invalid["error"])
    modified = decide(chosen_ids[1], "modify", after="建议向接诊医生确认后续资料核对安排。",
                      reason="合成验证：记录医生修订内容")
    card = next(item for item in modified["items"] if item["item_id"] == chosen_ids[1])
    check("修改后保留原稿与修订标识", card["state"] == "modified"
          and bool(card["ai_content"]) and card["changed_by_doctor"])
    invalid = decide(chosen_ids[2], "reject", expected=400)
    check("驳回缺少原因被拒", "原因" in invalid["error"])
    check("驳回成功", decide(chosen_ids[2], "reject", reason="合成验证：该条不适用")["ok"] is True)
    uncertain = decide(chosen_ids[3], "uncertain", reason="证据不足，待线下确认")
    check("暂不确定形成待确认事项",
          any("证据不足" in q for q in uncertain.get("pending_questions", [])))
    required_touched = sum(item["review_tier"] == "required" for item in chosen)
    check("待审按实际处理的必审项减少",
          uncertain["summary"]["pending"] == review["summary"]["pending"] - required_touched)
    check("人工动作单独计数", uncertain["summary"]["doctor_reviewed"] == 4)
    api(doctor, "POST", base + "/decisions", {"item_id": "path_step:不存在", "action": "accept"}, expected=400)
    check("未知审核条目被拒", True)

    for item in api(doctor, "GET", base + "/review")["items"]:
        if item["review_tier"] == "required" and item["state"] == "awaiting":
            decide(item["item_id"], "accept")
    current = api(doctor, "GET", base + "/review")
    revisions = {item["item_id"]: item["content_revision"] for item in current["items"]}
    for disagreement in current.get("disagreements") or []:
        identifier = disagreement["分歧编号"]
        api(doctor, "POST", base + "/disagreement-decisions", {
            "disagreement_id": identifier, "status": "unresolved_with_followup",
            "reason": "合成验证：保留差异并安排进一步核对",
            "next_step": "由接诊医生核对原始检查资料。",
            "expected_content_revision": revisions["consensus_item:" + identifier],
        })
    current = api(doctor, "GET", base + "/review")
    check("必审事项均已处理", current["summary"]["pending"] == 0)
    automatic_untouched = {item["item_id"] for item in automatic} - set(chosen_ids)
    original_version = api(doctor, "GET", base)["case"]["report_version"]

    print("\n[5] 预览发布、患者读取与历史版本")
    preview = api(doctor, "POST", base + "/publication-preview", {"final_advice": ADVICE})
    check("发布预览有修订标识", bool(preview.get("preview_revision")))
    published = api(doctor, "POST", base + "/publish", {
        "final_advice": ADVICE, "preview_revision": preview["preview_revision"],
    })
    check("发布状态正确", published["case_state"] == "doctor_confirmed")
    check("发布后版本递增", published["report_version"] == original_version + 1)
    after = api(doctor, "GET", base + "/review")
    check("发布成功并保留自动处理状态", after["published"] is True
          and all(item["state"] == "auto_processed" for item in after["items"]
                  if item["item_id"] in automatic_untouched))
    reports = api(doctor, "GET", base + "/report")
    check("内部完整报告结构完整", reports["full"]["complete"] is True)
    patient_report = api(patient, "GET", base + "/report")
    check("患者读取相同建议与版本",
          patient_report["doctor_result"]["published"]
          and patient_report["doctor_result"]["final_advice"] == ADVICE
          and patient_report["doctor_result"]["report_version"] == published["report_version"])
    history = api(doctor, "GET", base + "/audit")
    events = {event["event"] for event in history["timeline"]}
    check("创建、审核与发布都有留痕", {"case_created", "decision_recorded", "report_published"} <= events)
    check("医生动作有中文标签", all(item["action_label"] for item in history["decisions"]))
    check("医生动作与自动项分开统计",
          after["summary"]["doctor_reviewed"] >= 4 and after["summary"]["auto_processed"] == len(automatic_untouched))

    print("\n[6] 医生意见、资料复核与补资料")
    updated = api(doctor, "POST", base + "/updates", {
        "kind": "医生意见", "content": "建议结合眼底报告核对下一步资料准备。",
        "source": "医生上传", "department": "眼科",
    })
    check("医生意见来源经过确认",
          "眼科" in updated["change_summary"]["doctor_opinion_comparison"]["agree"])
    after_update = api(doctor, "GET", base + "/review")
    check("医生意见标记为已验证", after_update["doctor_opinions"][-1]["is_verified"] is True)
    check("已发布病例更新后进入复核", api(patient, "GET", base)["case"]["case_state"] == "awaiting_recheck")
    versions = api(patient, "GET", base + "/versions")["versions"]
    check("上一发布版本仍保留", any(v["version"] == published["report_version"] for v in versions))
    item = after_update["items"][0]
    requested = decide(item["item_id"], "request_more_info", reason="请补充眼底检查结果")
    check("请求补资料转入对应状态", requested.get("state_changed") == "need_more_info")
    check("补资料状态已持久化", api(patient, "GET", base)["case"]["case_state"] == "need_more_info")

    print("\n[7] 分析恢复与当前医生页面")
    queued = api(doctor, "POST", "/api/v2/cases/analyze", {"limit": 3})
    check("批处理返回排入与跳过列表",
          isinstance(queued.get("started"), list) and isinstance(queued.get("skipped"), list))
    check("同一病例不会同时排入与跳过", not (set(queued["started"]) & set(queued["skipped"])))
    if not os.getenv("AI_API_KEY", "").strip():
        check("无模型时不排入后台模型任务", queued["started"] == []
              and all("未配置模型" in reason for reason in queued.get("skipped_reasons", {}).values()))
    page = anon.get(BASE + "/doctor", timeout=30)
    check("医生页面可访问", page.status_code == 200)
    for token in ("s-list", "s-overview", "s-packet", "s-triage", "s-mdt", "s-review", "s-history",
                  "submitTriageConfirmation", "submitMdtFormalRecord", "startDoctorMdtSimulation",
                  "publication-preview", "expected_content_revision", "publishPanelHtml"):
        check(f"医生页面包含当前流程入口 {token}", token in page.text)


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ValueError, KeyError, StopIteration, RuntimeError) as exc:
        check("HTTP验收流程执行", False, str(exc))
    print(f"\n===== 结果：{ok_count} 通过 / {fail_count} 失败 =====")
    sys.exit(1 if fail_count else 0)
