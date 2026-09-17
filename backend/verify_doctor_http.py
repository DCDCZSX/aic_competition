"""医生端审核台真实 HTTP 端到端验证（阶段 4.1-4.5）。

前置：服务已启动（`cd CHAIS/backend && python app.py`），且库中已有至少一个病例
（本脚本会自己用患者账号建一个病例，全程走真实 HTTP）。

运行：
    python verify_doctor_http.py
"""

import json
import sys

import requests

BASE = "http://127.0.0.1:5000"

CASE = ("患者：王*，女，62岁。主诉：视物模糊3月，双下肢浮肿1周。"
        "既往2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2mmol/L。")

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


def main():
    print("[0] 患者端建病例（用于审核）")
    patient = requests.Session()
    r = patient.post(f"{BASE}/api/login",
                     json={"username": "patient", "password": "patient"}, timeout=30)
    patient_token = r.json()["session"]["token"]
    patient.headers["Authorization"] = f"Bearer {patient_token}"
    r = patient.post(f"{BASE}/api/v2/case", json={
        "medical_history": CASE, "main_issue": "该看哪个科", "age": 62, "sex": "女"}, timeout=300)
    check("建病例", r.status_code == 200, str(r.status_code))
    case_id = r.json().get("case_id")
    print(f"       case_id={case_id}")

    print("\n[1] 未登录访问医生端接口必须被拒（后台鉴权，不是前端跳转）")
    anon = requests.Session()
    r = anon.get(f"{BASE}/api/v2/cases", timeout=30)
    check("未登录 /api/v2/cases → 401", r.status_code == 401, str(r.status_code))
    r = anon.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30)
    check("未登录 /review → 401", r.status_code == 401, str(r.status_code))
    r = patient.get(f"{BASE}/api/v2/cases", timeout=30)
    check("患者角色访问医生端 → 403", r.status_code == 403, str(r.status_code))
    r = patient.post(f"{BASE}/api/v2/case/{case_id}/updates",
                     json={"kind": "医生意见", "content": "患者替医生写的意见",
                           "source": "医生上传", "department": "眼科"}, timeout=30)
    check("患者角色不能以「医生上传」来源录入意见 → 403", r.status_code == 403, str(r.status_code))

    print("\n[2] 医生登录")
    doctor = requests.Session()
    r = doctor.post(f"{BASE}/api/login",
                    json={"username": "doctor", "password": "doctor"}, timeout=30)
    check("doctor 登录", r.status_code == 200, str(r.status_code))
    session = r.json()["session"]
    check("角色=doctor", session["role"] == "doctor", session["role"])
    doctor.headers["Authorization"] = f"Bearer {session['token']}"
    r = doctor.get(f"{BASE}/api/me", timeout=30)
    check("scopes 含 review/decision", {"review", "decision"} <= set(r.json().get("scopes") or []),
          json.dumps(r.json().get("scopes"), ensure_ascii=False))

    print("\n[3] 屏 A 病例列表")
    # 取大页用于"按 id 找刚建的病例"；默认页（limit=50）的分页行为在下面单独验
    r = doctor.get(f"{BASE}/api/v2/cases?limit=500", timeout=60)
    check("列表 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    cases = body.get("cases") or []
    check("列表非空", bool(cases), f"{len(cases)} 例")
    check("返回真实总数与分页信息",
          isinstance(body.get("total"), int) and body["total"] >= len(cases)
          and "has_more" in body and isinstance(body.get("filters"), list),
          json.dumps({"total": body.get("total"), "has_more": body.get("has_more"),
                      "filters": len(body.get("filters") or [])}, ensure_ascii=False))
    row = next((c for c in cases if c["case_id"] == case_id), None)
    check("能找到刚建的病例", row is not None)
    if row:
        check("带待审条数", int(row["pending_items"]) > 0, str(row["pending_items"]))
        check("带行为类型/可靠性", bool(row["behavior_type"]) and bool(row["reliability"]),
              f"{row['behavior_type']} / {row['reliability']}")
        check("带牵头方向", bool(row["primary_department"]), str(row["primary_department"]))
        # 队列主屏新列（2026-09-17）
        check("带风险分级（高/中/低）", row.get("risk_level") in ("高", "中", "低"),
              str(row.get("risk_level")))
        check("带冲突数/缺口数/免审条数",
              all(k in row for k in ("conflict_count", "gap_count", "exempt_items")),
              json.dumps({k: row.get(k) for k in ("conflict_count", "gap_count", "exempt_items")},
                         ensure_ascii=False))
        check("带更新时间", bool(row.get("updated_at")), str(row.get("updated_at")))
    r = doctor.get(f"{BASE}/api/v2/cases?state=doctor_confirmed", timeout=30)
    check("状态筛选可用", r.status_code == 200 and isinstance(r.json().get("cases"), list))
    check("默认按风险优先排序（风险等级单调不增）", all(
        cases[i]["risk_rank"] >= cases[i + 1]["risk_rank"] for i in range(len(cases) - 1))
        if len(cases) > 1 else True,
        json.dumps([c["risk_level"] for c in cases[:6]], ensure_ascii=False))
    r = doctor.get(f"{BASE}/api/v2/cases?sort=pending", timeout=30)
    pending_sorted = r.json().get("cases") or []
    check("按待审最多排序可用", all(
        pending_sorted[i]["pending_items"] >= pending_sorted[i + 1]["pending_items"]
        for i in range(len(pending_sorted) - 1)) if len(pending_sorted) > 1 else True)
    r = doctor.get(f"{BASE}/api/v2/cases?filter=doctor_pending", timeout=30)
    filtered = r.json().get("cases") or []
    check("按「待我处理」筛选可用", all(int(c["pending_items"]) > 0 for c in filtered),
          f"{len(filtered)} 例")
    r = doctor.get(f"{BASE}/api/v2/cases?filter=analysis_failed", timeout=30)
    check("按「分析失败/部分」筛选可用",
          all(c["analysis_state"] in ("failed", "partial_failed") for c in r.json().get("cases") or []),
          f"{len(r.json().get('cases') or [])} 例")
    r1 = doctor.get(f"{BASE}/api/v2/cases?limit=2&offset=0", timeout=30)
    r2 = doctor.get(f"{BASE}/api/v2/cases?limit=2&offset=2", timeout=30)
    p1 = [c["case_id"] for c in r1.json().get("cases") or []]
    p2 = [c["case_id"] for c in r2.json().get("cases") or []]
    check("分页不重不漏（offset 生效）",
          len(p1) <= 2 and not set(p1) & set(p2) and r1.json().get("total") == r2.json().get("total"),
          json.dumps({"page1": p1, "page2": p2}, ensure_ascii=False))

    print("\n[4] 屏 F 审核条目")
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=60)
    check("review 200", r.status_code == 200, str(r.status_code))
    review = r.json()
    items = review.get("items") or []
    check("条目非空", bool(items), f"{len(items)} 条")
    types = {i["target_type"] for i in items}
    check("含科室入选条目", "department_selection" in types, json.dumps(sorted(types), ensure_ascii=False))
    check("含路径步骤条目", "path_step" in types)
    check("含患者建议条目", "patient_advice" in types)
    # 待审口径（2026-09-17 修正）：只统计**必审**条目；未要求医生决定的路径步骤是"免审"
    # （state=auto_processed，不计入待审、不阻塞发布、也不算医生确认）
    required = [i for i in items if i["needs_doctor_review"]]
    exempt = [i for i in items if not i["needs_doctor_review"]]
    summary = review["summary"]
    check("有必审条目", bool(required), f"{len(required)} 条")
    check("初始待审数=必审条目数（免审不计入）",
          summary["pending"] == len(required) and summary["pending"] <= len(items),
          json.dumps({"pending": summary["pending"], "required": len(required),
                      "total": len(items)}, ensure_ascii=False))
    check("免审条目被标为 auto_processed 且带原因",
          bool(exempt) and all(i["state"] == "auto_processed" and i.get("auto_reason")
                               for i in exempt),
          f"免审 {len(exempt)} 条 / auto_processed {summary.get('auto_processed')}")
    check("免审条目不计入医生处理",
          summary["doctor_reviewed"] == 0 and summary.get("auto_processed") == len(exempt),
          json.dumps(summary, ensure_ascii=False)[:160])
    check("未发布时不能直接发布标记", review["published"] is False)
    text = json.dumps(review, ensure_ascii=False)
    check("无概率/置信度泄露", not any(t in text for t in ("概率", "置信度", "confidence")))

    # 后续逐条操作只挑"必审"条目（对免审条目做决定会把它们从 auto_processed 变成医生结论）
    ids = [i["item_id"] for i in required]

    print("\n[5] 逐条审核：接受 / 修改（原因必填）/ 驳回（原因必填）/ 暂不确定")
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[0], "action": "accept", "reviewer": "审核医生"}, timeout=30)
    check("接受成功", r.status_code == 200 and r.json().get("ok"), json.dumps(r.json(), ensure_ascii=False)[:120])

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[1], "action": "modify", "after": "改一下"}, timeout=30)
    check("修改缺原因被拒", r.status_code == 400 and not r.json().get("ok"),
          str(r.json().get("error"))[:60])
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[1], "action": "modify", "after": ""}, timeout=30)
    check("修改缺内容被拒", r.status_code == 400, str(r.json().get("error"))[:60])
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[1], "action": "modify",
                          "after": "建议向医生确认是否需要优先评估眼部情况",
                          "reason": "改写为中性表述并调整优先级", "reviewer": "审核医生"}, timeout=30)
    check("修改成功且记录 before/after", r.status_code == 200 and r.json().get("ok"))
    modified = next(i for i in r.json()["items"] if i["item_id"] == ids[1])
    check("条目状态=已修改", modified["state"] == "modified", modified["state"])
    check("保留 AI 原稿", bool(modified["ai_content"]) and modified["changed_by_doctor"] is True)

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[2], "action": "reject"}, timeout=30)
    check("驳回缺原因被拒", r.status_code == 400, str(r.json().get("error"))[:60])
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[2], "action": "reject", "reason": "与既往线下意见不符"}, timeout=30)
    check("驳回成功", r.status_code == 200 and r.json().get("ok"))

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": ids[3], "action": "uncertain", "reason": "证据不足，待线下确认"}, timeout=30)
    check("暂不确定成功", r.status_code == 200 and r.json().get("ok"))
    review_after = r.json()
    check("待审数下降（只对必审条目计数）",
          review_after["summary"]["pending"] == len(required) - 4,
          json.dumps(review_after["summary"], ensure_ascii=False))
    check("医生处理数=4（免审不算）", review_after["summary"].get("doctor_reviewed") == 4,
          json.dumps(review_after["summary"], ensure_ascii=False)[:160])
    check("暂不确定进入待确认问题清单",
          bool(review_after.get("pending_questions"))
          and any("证据不足" in q for q in review_after["pending_questions"]),
          json.dumps(review_after.get("pending_questions"), ensure_ascii=False)[:140])

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": "department_selection:不存在的科", "action": "accept"}, timeout=30)
    check("未知条目被拒", r.status_code == 400, str(r.json().get("error"))[:60])

    print("\n[6] 批量接受（7.3：预览 → 执行 → 整批撤销）")
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/batch/preview", timeout=30)
    check("批量预览可用", r.status_code == 200 and r.json().get("ok"),
          str(r.json().get("error"))[:80])
    pv = r.json()
    check("预览只给低风险条目（分歧与冲突不参与批量）",
          all(not a["item_id"].startswith("consensus_item") for a in pv.get("accept") or []),
          json.dumps([a["item_id"] for a in pv.get("accept") or []], ensure_ascii=False)[:120])
    check("被排除的条目带原因",
          all(e.get("why") for e in pv.get("excluded") or []),
          json.dumps(pv.get("excluded") or [], ensure_ascii=False)[:140])
    before_plan = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30).json()
    check("预览不落库（条数不变）",
          before_plan["summary"]["pending"] == len(required) - 4,
          str(before_plan["summary"]["pending"]))

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/batch/undo",
                    json={"batch_id": "B-不存在", "reason": "验证"}, timeout=30)
    check("未知批次撤销被拒", r.status_code == 400, str(r.json().get("error"))[:70])

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/batch/accept",
                    json={"reviewer": "审核医生"}, timeout=60)
    check("批量接受成功", r.status_code == 200 and r.json().get("ok"),
          json.dumps(r.json(), ensure_ascii=False)[:140])
    batch = r.json()
    batch_id = batch.get("batch_id") or ""
    check("批次号已生成", batch_id.startswith("B-"), batch_id)
    pending_after_batch = batch["summary"]["pending"]
    check("批量只覆盖低风险条目（分歧仍在待审）",
          pending_after_batch == len(pv.get("excluded") or []),
          f"待审 {pending_after_batch} / 预期 {len(pv.get('excluded') or [])}")

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/batch/undo",
                    json={"batch_id": batch_id, "reason": ""}, timeout=30)
    check("撤销必须填原因", r.status_code == 400 and r.json().get("need_reason"),
          str(r.json().get("error"))[:60])

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/batch/undo",
                    json={"batch_id": batch_id, "reason": "误点批量接受，需要逐条看"}, timeout=60)
    check("整批撤销成功", r.status_code == 200 and r.json().get("ok"),
          json.dumps(r.json(), ensure_ascii=False)[:140])
    check("撤销后条目回到待审", r.json()["summary"]["pending"] == len(required) - 4,
          str(r.json()["summary"]["pending"]))
    check("撤销后不再显示可撤销批次", r.json().get("active_batch") == "",
          str(r.json().get("active_batch")))
    hist = json.dumps(doctor.get(f"{BASE}/api/v2/case/{case_id}/audit", timeout=30).json(),
                      ensure_ascii=False)
    check("撤销留痕（接受与撤销记录都在）",
          "batch_undone" in hist and "撤销" in hist, "")

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/sample",
                    json={"k": 2, "reviewer": "审核医生"}, timeout=30)
    check("随机抽检免审条目可用", r.status_code == 200 and r.json().get("ok"),
          str(r.json().get("error"))[:80])
    smp = r.json()
    check("抽检只抽免审条目且不超过总数",
          len(smp.get("sample") or []) == min(2, smp.get("exempt_total") or 0),
          f"{len(smp.get('sample') or [])} 条 / 共 {smp.get('exempt_total')}")
    ids_sampled = {s["item_id"] for s in smp.get("sample") or []}
    after_sample = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30).json()
    check("抽检不改状态、不计入医生处理（红线）",
          after_sample["summary"]["doctor_reviewed"] == 4
          and after_sample["summary"]["pending"] == len(required) - 4,
          json.dumps(after_sample["summary"], ensure_ascii=False)[:160])
    check("抽检后条目仍是免审态",
          all(i["state"] == "auto_processed" for i in after_sample["items"]
              if i["item_id"] in ids_sampled and not i["needs_doctor_review"]),
          str(sorted(ids_sampled))[:120])
    audit_text2 = json.dumps(doctor.get(f"{BASE}/api/v2/case/{case_id}/audit", timeout=30).json(),
                             ensure_ascii=False)
    check("抽检留痕（抽了哪几条可追溯）", "exempt_sampled" in audit_text2, "")

    print("\n[7] 发布审核版")
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/publish", json={"reviewer": "审核医生"}, timeout=60)
    check("仍有必审条目时发布被拒", r.status_code == 400, str(r.json().get("error"))[:70])
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/publish",
                    json={"reviewer": "审核医生", "accept_all": True}, timeout=120)
    if r.status_code != 200 or not r.json().get("ok"):
        # 批量通道**不替医生裁决分歧**：剩下的必须逐条处理（这正是 7.3 要的行为）
        blocked = r.json().get("pending_items") or []
        check("批量后仍有分歧阻塞发布且如实列名",
              bool(blocked) and "不能发布" in str(r.json().get("error")), str(blocked)[:120])
        for item_id in blocked:
            d = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                            json={"item_id": item_id, "action": "accept",
                                  "reason": "分歧逐条确认", "reviewer": "审核医生"}, timeout=30)
            check(f"逐条接受分歧 {item_id[:24]}", d.status_code == 200 and d.json().get("ok"),
                  str(d.json().get("error"))[:60])
        r = doctor.post(f"{BASE}/api/v2/case/{case_id}/publish",
                        json={"reviewer": "审核医生"}, timeout=60)
    check("发布成功", r.status_code == 200 and r.json().get("ok"),
          json.dumps(r.json(), ensure_ascii=False)[:140])
    pub = r.json()
    check("状态→doctor_confirmed", pub.get("case_state") == "doctor_confirmed", str(pub.get("case_state")))
    check("版本 +1", pub.get("report_version") == 2, str(pub.get("report_version")))
    check("批量接受逐条留痕（只接受低风险条目）",
          all(not i.startswith("consensus_item") for i in pub.get("batch_accepted") or []),
          f"{len(pub.get('batch_accepted') or [])} 条")

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/batch/undo",
                    json={"batch_id": pub.get("batch_id") or batch_id, "reason": "发布后想改"}, timeout=30)
    check("已发布后不允许直接撤销批次", r.status_code == 400 and "已发布" in str(r.json().get("error")),
          str(r.json().get("error"))[:70])

    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30)
    check("发布后无待审条目", r.json()["summary"]["pending"] == 0)
    check("published=True", r.json()["published"] is True)
    check("发布后免审条目仍标为自动处理（没被写成医生接受）",
          all(i["state"] == "auto_processed" for i in r.json()["items"] if not i["needs_doctor_review"]),
          json.dumps(r.json()["summary"], ensure_ascii=False)[:140])
    audit_text = json.dumps(doctor.get(f"{BASE}/api/v2/case/{case_id}/audit", timeout=30).json(),
                            ensure_ascii=False)
    check("审计区分医生处理与免审自动处理",
          "医生处理=" in audit_text and "免审自动处理=" in audit_text, "")

    print("\n[8] 报告角标转绿")
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/report", timeout=60)
    check("报告 200", r.status_code == 200, str(r.status_code))
    reports = r.json()
    check("摘要 doctor_confirmed", reports["summary"]["doctor_confirmed"] is True)
    check("完整报告 doctor_confirmed", reports["full"]["doctor_confirmed"] is True)
    check("完整报告 13 段齐全", reports["full"].get("complete") is True,
          json.dumps(reports["full"].get("missing_sections"), ensure_ascii=False))

    print("\n[9] 屏 G 审核记录与版本")
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/audit", timeout=30)
    check("audit 200", r.status_code == 200, str(r.status_code))
    hist = r.json()
    check("有决策记录", len(hist["decisions"]) >= 4, f"{len(hist['decisions'])} 条")
    check("决策带中文动作标签", all(d["action_label"] for d in hist["decisions"]))
    check("有审计时间线", bool(hist["timeline"]), f"{len(hist['timeline'])} 条")
    events = {t["event"] for t in hist["timeline"]}
    check("留痕 decision_recorded", "decision_recorded" in events)
    check("留痕 report_published", "report_published" in events)
    check("留痕 case_created", "case_created" in events)

    print("\n[10] 屏 F 录入线下实际意见（医生上传）→ AI vs 医生对照")
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/updates",
                    json={"kind": "医生意见", "content": "建议眼科评估后行眼底激光，同步内分泌科控糖",
                          "source": "医生上传", "department": "眼科"}, timeout=60)
    check("医生端录入成功", r.status_code == 200 and r.json().get("ok"),
          json.dumps(r.json(), ensure_ascii=False)[:140])
    cmp_ = ((r.json().get("change_summary") or {}).get("doctor_opinion_comparison") or {})
    check("医生意见进入 agree（眼科在建议科室中）", "眼科" in (cmp_.get("agree") or []),
          json.dumps(cmp_, ensure_ascii=False)[:160])
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30)
    ops = r.json().get("doctor_opinions") or []
    check("线下意见已登记且已验证", bool(ops) and ops[-1]["is_verified"] is True,
          json.dumps(ops[-1:] if ops else [], ensure_ascii=False)[:140])

    print("\n[11] 请求补资料 → 病例退回补资料")
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30)
    item0 = (r.json().get("items") or [])[0]
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": item0["item_id"], "action": "request_more_info",
                          "reason": "缺少眼底检查结果"}, timeout=30)
    check("请求补资料成功", r.status_code == 200 and r.json().get("ok"))
    check("状态→need_more_info", r.json().get("state_changed") == "need_more_info",
          str(r.json().get("state_changed")))
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}", timeout=30)
    check("病例状态已退回补资料",
          (r.json().get("case") or {}).get("case_state") == "need_more_info",
          str((r.json().get("case") or {}).get("case_state")))

    print("\n[12] 医生端批处理：补齐未完成的分析 + 列表分析状态")
    r = doctor.get(f"{BASE}/api/v2/cases", timeout=60)
    rows = r.json().get("cases") or []
    check("列表带分析状态字段", bool(rows) and all("analysis_state" in c for c in rows),
          json.dumps(rows[0].get("analysis_state") if rows else None, ensure_ascii=False))
    check("分析状态取值合法",
          all(c.get("analysis_state") in ("running", "done", "partial_failed", "failed", "none")
              for c in rows),
          json.dumps(sorted({c.get("analysis_state") for c in rows}), ensure_ascii=False))
    r = doctor.post(f"{BASE}/api/v2/cases/analyze", json={"limit": 3}, timeout=60)
    body = r.json() or {}
    check("批处理接口 200", r.status_code == 200 and body.get("ok"),
          json.dumps(body, ensure_ascii=False)[:160])
    check("返回已排入与已跳过清单",
          isinstance(body.get("started"), list) and isinstance(body.get("skipped"), list),
          json.dumps(body, ensure_ascii=False)[:160])
    check("同一条病例不会既排入又跳过",
          not (set(body.get("started") or []) & set(body.get("skipped") or [])),
          json.dumps(body, ensure_ascii=False)[:160])
    # 逐科失败口径：医生端要能看到"哪一科没跑出来"，并且这类病例必须能被批处理捡起来
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/analysis", timeout=60)
    ana = (r.json() or {}).get("analysis") or {}
    check("医生端可查分析明细（含 mdt.failed）",
          r.status_code == 200 and isinstance((ana.get("mdt") or {}).get("failed"), list),
          json.dumps(ana.get("mdt"), ensure_ascii=False)[:160])
    # 逐科失败口径：医生端要能看到"哪一科没跑出来"，并且这类病例必须能被批处理捡起来。
    # 注意：排入的病例**不一定** analysis_state != done —— 老病例就是"相位都算完成、
    # 但里面有 unavailable 的科室"，所以这里断言的不变量是"确实还有没分析完的东西"。
    if body.get("started"):
        cid = body["started"][0]
        r2 = doctor.get(f"{BASE}/api/v2/case/{cid}/analysis", timeout=60)
        st2 = (r2.json() or {}).get("analysis") or {}
        pending = [p for p, v in (st2.get("phases") or {}).items()
                   if (v or {}).get("state") != "done"]
        failed_depts = (st2.get("mdt") or {}).get("failed") or []
        check("排入的病例确实还没分析完（有未完成相位或未完成科室）",
              bool(pending) or bool(failed_depts),
              json.dumps({"pending": pending, "failed": failed_depts}, ensure_ascii=False))

    print("\n[13] 页面可达性")
    r = requests.get(f"{BASE}/doctor", timeout=30)
    check("/doctor 200", r.status_code == 200, str(r.status_code))
    for token in ("s-list", "s-overview", "s-triage", "s-evidence", "s-mdt", "s-review", "s-history",
                  "playMdt", "submitOpinion", "publish(", "openModal", "analyzePending", "analysisBadge",
                  "rev-publish-inline", "publishPanelHtml"):
        check(f"页面含 {token}", token in r.text)

    print(f"\n===== 结果：{ok_count} 通过 / {fail_count} 失败 =====")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
