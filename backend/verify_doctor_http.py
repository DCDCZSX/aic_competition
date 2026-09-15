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
    r = doctor.get(f"{BASE}/api/v2/cases", timeout=60)
    check("列表 200", r.status_code == 200, str(r.status_code))
    cases = r.json().get("cases") or []
    check("列表非空", bool(cases), f"{len(cases)} 例")
    row = next((c for c in cases if c["case_id"] == case_id), None)
    check("能找到刚建的病例", row is not None)
    if row:
        check("带待审条数", int(row["pending_items"]) > 0, str(row["pending_items"]))
        check("带行为类型/可靠性", bool(row["behavior_type"]) and bool(row["reliability"]),
              f"{row['behavior_type']} / {row['reliability']}")
        check("带牵头方向", bool(row["primary_department"]), str(row["primary_department"]))
    r = doctor.get(f"{BASE}/api/v2/cases?state=doctor_confirmed", timeout=30)
    check("状态筛选可用", r.status_code == 200 and isinstance(r.json().get("cases"), list))
    check("待审优先排序", all(
        cases[i]["pending_items"] >= cases[i + 1]["pending_items"] for i in range(len(cases) - 1))
        if len(cases) > 1 else True)

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
    check("初始全部待审", review["summary"]["pending"] == len(items), str(review["summary"]))
    check("未发布时不能直接发布标记", review["published"] is False)
    text = json.dumps(review, ensure_ascii=False)
    check("无概率/置信度泄露", not any(t in text for t in ("概率", "置信度", "confidence")))

    ids = [i["item_id"] for i in items]

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
    check("待审数下降", review_after["summary"]["pending"] == len(items) - 4,
          json.dumps(review_after["summary"], ensure_ascii=False))
    check("暂不确定进入待确认问题清单",
          bool(review_after.get("pending_questions"))
          and any("证据不足" in q for q in review_after["pending_questions"]),
          json.dumps(review_after.get("pending_questions"), ensure_ascii=False)[:140])

    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/decisions",
                    json={"item_id": "department_selection:不存在的科", "action": "accept"}, timeout=30)
    check("未知条目被拒", r.status_code == 400, str(r.json().get("error"))[:60])

    print("\n[6] 发布审核版")
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/publish", json={"reviewer": "审核医生"}, timeout=60)
    check("仍有待审时发布被拒", r.status_code == 400, str(r.json().get("error"))[:70])
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/publish",
                    json={"reviewer": "审核医生", "accept_all": True}, timeout=120)
    check("全部接受并发布成功", r.status_code == 200 and r.json().get("ok"),
          json.dumps(r.json(), ensure_ascii=False)[:140])
    pub = r.json()
    check("状态→doctor_confirmed", pub.get("case_state") == "doctor_confirmed", str(pub.get("case_state")))
    check("版本 +1", pub.get("report_version") == 2, str(pub.get("report_version")))
    check("批量接受逐条留痕", bool(pub.get("batch_accepted")), f"{len(pub.get('batch_accepted') or [])} 条")

    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/review", timeout=30)
    check("发布后无待审条目", r.json()["summary"]["pending"] == 0)
    check("published=True", r.json()["published"] is True)

    print("\n[7] 报告角标转绿")
    r = doctor.get(f"{BASE}/api/v2/case/{case_id}/report", timeout=60)
    check("报告 200", r.status_code == 200, str(r.status_code))
    reports = r.json()
    check("摘要 doctor_confirmed", reports["summary"]["doctor_confirmed"] is True)
    check("完整报告 doctor_confirmed", reports["full"]["doctor_confirmed"] is True)
    check("完整报告 13 段齐全", reports["full"].get("complete") is True,
          json.dumps(reports["full"].get("missing_sections"), ensure_ascii=False))

    print("\n[8] 屏 G 审核记录与版本")
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

    print("\n[9] 屏 F 录入线下实际意见（医生上传）→ AI vs 医生对照")
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

    print("\n[10] 请求补资料 → 病例退回补资料")
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
    check("部分未完成会进入待补齐（不会被当成已完成跳过）",
          all(c.get("analysis_state") != "done" for c in rows
              if c.get("case_id") in (body.get("started") or [])) if body.get("started") else True,
          json.dumps(body, ensure_ascii=False)[:160])

    print("\n[13] 页面可达性")
    r = requests.get(f"{BASE}/doctor", timeout=30)
    check("/doctor 200", r.status_code == 200, str(r.status_code))
    for token in ("s-list", "s-overview", "s-triage", "s-evidence", "s-mdt", "s-review", "s-history",
                  "playMdt", "submitOpinion", "publish(", "openModal", "analyzePending", "analysisBadge"):
        check(f"页面含 {token}", token in r.text)

    print(f"\n===== 结果：{ok_count} 通过 / {fail_count} 失败 =====")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
