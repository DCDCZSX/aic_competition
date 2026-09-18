"""P10/P11 真实 HTTP 端到端验证（任务 3.4 / 3.6）。

在服务已启动的情况下运行：
    python verify_update_http.py
验证：任务状态机、就诊后资料更新、缺口填补、变化摘要、患者转述医生意见的处置。
"""

import json
import sys

import requests
from verify_http_helpers import login, preview_token

BASE = "http://127.0.0.1:5000"

CASE = ("患者：李*，男，61岁。主诉：视物模糊3月，双下肢浮肿1周。"
        "既往2型糖尿病5年，高血压10年，未规律服药。"
        "检查：尿蛋白++，空腹血糖9.2mmol/L。")

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
    s = requests.Session()

    login(s, BASE, "patient")
    check("登录并建立 Bearer 会话", True)

    print("\n[1] 建病例")
    r = s.post(f"{BASE}/api/v2/case", json={
        "preview_token": preview_token(s, BASE, CASE, "该看哪个科"),
        "age": 61, "sex": "男"}, timeout=300)
    check("建病例 200", r.status_code == 200, str(r.status_code))
    data = r.json()
    case_id = data.get("case_id")
    check("返回 case_id", bool(case_id), str(case_id))
    gaps_before = data.get("gaps") or []
    selected = (data.get("triage") or {}).get("selected") or []
    print(f"       selected={selected}")
    print(f"       gaps({len(gaps_before)})={gaps_before[:4]}")
    check("有分诊科室", bool(selected))

    print("\n[2] 任务状态机")
    tasks = (data.get("path") or {}).get("tasks") or []
    check("生成了任务", bool(tasks), f"{len(tasks)} 条")
    if tasks:
        tid = tasks[0]["task_id"]
        r = s.post(f"{BASE}/api/v2/case/{case_id}/tasks/{tid}",
                   json={"state": "doing", "reason": "开始整理"}, timeout=30)
        check("todo→doing", r.status_code == 200 and r.json().get("ok"),
              json.dumps(r.json().get("update") or r.json(), ensure_ascii=False)[:120])
        r2 = s.post(f"{BASE}/api/v2/case/{case_id}/tasks/{tid}",
                    json={"state": "reopened"}, timeout=30)
        check("doing→reopened 被拒", r2.status_code == 400 and not r2.json().get("ok"),
              str(r2.json().get("error"))[:60])
        r3 = s.post(f"{BASE}/api/v2/case/{case_id}/tasks/{tid}",
                    json={"state": "done"}, timeout=30)
        check("doing→done", r3.status_code == 200 and r3.json().get("ok"))
        check("任务概览", isinstance(r3.json().get("tasks_summary"), dict),
              json.dumps(r3.json().get("tasks_summary"), ensure_ascii=False))

    print("\n[3] P10 就诊后资料更新：新检查结果填补缺口")
    r = s.post(f"{BASE}/api/v2/case/{case_id}/updates", json={
        "kind": "检查结果",
        "content": "眼底检查：双眼糖尿病视网膜病变II期，建议眼科随诊",
        "source": "患者上传"}, timeout=60)
    check("更新 200", r.status_code == 200, str(r.status_code))
    upd = r.json()
    check("ok=True", bool(upd.get("ok")), json.dumps(upd.get("error"), ensure_ascii=False))
    affected = upd.get("affected") or {}
    check("受影响科室非空", bool(affected.get("departments")),
          json.dumps(affected.get("departments"), ensure_ascii=False))
    cs = upd.get("change_summary") or {}
    added = cs.get("added") or []
    check("摘要记录了新增资料", any("眼底" in a for a in added),
          json.dumps(added, ensure_ascii=False)[:160])
    check("摘要记录填补缺口", any("已填补信息缺口" in a for a in added),
          json.dumps([a for a in added if "填补" in a], ensure_ascii=False)[:160])

    print("\n[4] 缺口确实减少")
    r = s.get(f"{BASE}/api/v2/case/{case_id}", timeout=60)
    body = r.json()
    # 注意：GET /api/v2/case/<id> 返回 {"case": {...}, "audit": [...]}
    case_obj = body.get("case") or {}
    check("GET case 200", r.status_code == 200 and bool(case_obj), str(r.status_code))
    gaps_after = (case_obj.get("payload") or {}).get("gaps") or []
    check("缺口数下降", len(gaps_after) < len(gaps_before),
          f"{len(gaps_before)} → {len(gaps_after)}")
    check("眼底检查缺口已消除", not any("眼底" in str(g) for g in gaps_after),
          json.dumps(gaps_after, ensure_ascii=False)[:200])
    check("未相关缺口仍保留", any("血常规" in str(g) for g in gaps_after),
          json.dumps(gaps_after, ensure_ascii=False)[:200])
    # 病例状态机口径（PR #9 修正，当前口径见开发进度与交接.md §9）：
    #   仅"已由医生确认"的病例补充资料后进入 awaiting_recheck（待复核）；
    #   尚未医生确认的病例保持 awaiting_doctor —— 避免"尚未确认却等待重新审核"的矛盾。
    # 本例走的是患者自己补充资料、尚未经医生确认，所以期望 awaiting_doctor；
    # 反向分支（已确认 → awaiting_recheck）由 tests/test_update_flow.py 覆盖。
    check("病例状态=待医生审核（未确认病例不该变成待复核）",
          case_obj.get("case_state") == "awaiting_doctor", str(case_obj.get("case_state")))
    events = [a.get("event") for a in (body.get("audit") or [])]
    check("留痕 case_updated", "case_updated" in events, json.dumps(events, ensure_ascii=False)[:200])

    print("\n[5] P10 患者转述医生意见（红线：不得当作已验证）")
    r = s.post(f"{BASE}/api/v2/case/{case_id}/updates", json={
        "kind": "医生意见", "content": "医生说不用太担心，先控制血糖",
        "source": "患者上传", "department": "眼科"}, timeout=60)
    cmp_ = ((r.json().get("change_summary") or {}).get("doctor_opinion_comparison") or {})
    check("未进入 agree 列表", "眼科" not in (cmp_.get("agree") or []),
          json.dumps(cmp_, ensure_ascii=False)[:200])
    check("列入 unverified", bool(cmp_.get("unverified")))
    check("提示需医生端确认",
          any("医生端确认" in n for n in (r.json().get("change_summary") or {}).get("needs_doctor_review") or []))

    print("\n[6] P10 医生端录入线下意见（需医生角色：非医生会话应被拒）")
    r = s.post(f"{BASE}/api/v2/case/{case_id}/updates", json={
        "kind": "医生意见", "content": "建议眼科评估后行眼底激光，同步内分泌科控糖",
        "source": "医生上传", "department": "眼科"}, timeout=60)
    check("非医生会话以「医生上传」来源提交被拒", r.status_code in (401, 403), str(r.status_code))
    doctor = requests.Session()
    login(doctor, BASE, "doctor")
    r = doctor.post(f"{BASE}/api/v2/case/{case_id}/updates", json={
        "kind": "医生意见", "content": "建议眼科评估后行眼底激光，同步内分泌科控糖",
        "source": "医生上传", "department": "眼科"}, timeout=60)
    check("医生会话提交成功", r.status_code == 200 and r.json().get("ok"),
          json.dumps(r.json().get("error"), ensure_ascii=False))
    cmp2 = ((r.json().get("change_summary") or {}).get("doctor_opinion_comparison") or {})
    check("医生意见进入 agree", "眼科" in (cmp2.get("agree") or []),
          json.dumps(cmp2, ensure_ascii=False)[:200])

    print("\n[7] P11 变化摘要接口")
    r = s.get(f"{BASE}/api/v2/case/{case_id}/changes", timeout=60)
    check("changes 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    cs = body.get("change_summary") or {}
    check("updates 累积", len(body.get("updates") or []) == 3,
          str(len(body.get("updates") or [])))
    check("has_changes=True", cs.get("has_changes") is True)
    for key in ("added", "changed", "unchanged", "task_changes", "needs_doctor_review"):
        check(f"摘要含 {key}", isinstance(cs.get(key), list))
    text = json.dumps(cs, ensure_ascii=False)
    check("无概率数字/置信度泄露",
          not any(t in text for t in ("概率", "置信度", "可能性为")), text[:120])

    print("\n[8] 报告可读性（不得出现内部英文字段名 / 代码值 / 置信度 / 裸对象）")
    r = s.get(f"{BASE}/api/v2/case/{case_id}/report", timeout=60)
    full = (r.json() or {}).get("full") or {}
    sections = full.get("sections") or {}
    check("报告 200 且有段落", r.status_code == 200 and bool(sections), f"{len(sections)} 段")
    report_text = json.dumps(sections, ensure_ascii=False)
    forbidden_keys = ("judgement", "evidence_state", "supports", "opposes", "external",
                      "confidence", "scope", "resolving_info", "doctor_confirmed",
                      "step_id", "depends_on", "requires_doctor", "information_gaps",
                      "source_refs", "behavior_type", "decision_path", "target_type",
                      "supporters", "positions", "reliability")
    leaked = [k for k in forbidden_keys if k in report_text]
    check("无内部英文字段名泄漏", not leaked, json.dumps(leaked, ensure_ascii=False))
    leaked_values = [v for v in ("rule-dominant", "awaiting_doctor", "department_selection",
                                 "consensus_item", "patient_advice", "specialty")
                     if f'"{v}"' in report_text]
    check("无内部代码值泄漏", not leaked_values, json.dumps(leaked_values, ensure_ascii=False))
    check("不展示模型自报置信度",
          not any(t in report_text for t in ("置信度", "confidence", "概率")), "")
    cards = (sections.get("证据关联") or {}).get("证据卡片") or []
    check("证据卡片为中文键结构",
          bool(cards) and all("判断" in c and "证据状态" in c for c in cards),
          json.dumps(list(cards[0].keys()) if cards else [], ensure_ascii=False))
    timeline = sections.get("时间线") or []
    check("时间线为中文键事件",
          bool(timeline) and all({"时间", "内容"} <= set(e.keys()) for e in timeline),
          json.dumps(timeline[:1], ensure_ascii=False)[:110])
    steps = sections.get("就医路径") or []
    check("就医路径为中文键步骤",
          bool(steps) and all({"步骤", "内容"} <= set(t.keys()) for t in steps),
          json.dumps(steps[:1], ensure_ascii=False)[:110])

    print("\n[11] 两段式：先交付画像，MDT 后台跑并有真实进度")
    import time as _time
    t0 = _time.time()
    r = s.post(f"{BASE}/api/v2/case", json={
        "preview_token": preview_token(s, BASE, CASE, "该看哪个科"), "age": 61, "sex": "男",
        "defer_mdt": True}, timeout=120)
    fast = _time.time() - t0
    two = r.json() or {}
    check("建病例返回 200", r.status_code == 200 and two.get("ok"), str(r.status_code))
    check("标记 MDT 仍在后台", two.get("mdt_pending") is True, str(two.get("mdt_pending")))
    check("主请求不带 MDT 结果", two.get("mdt") is None, str(two.get("mdt"))[:60])
    check("画像/分诊/路径已就绪",
          bool(two.get("profile")) and bool(two.get("triage")) and bool(two.get("path")), "")
    check("交付够快（<10 秒）", fast < 10, f"{fast:.2f}s")

    case2 = two.get("case_id")
    seen = []
    ready = False
    t1 = _time.time()
    while _time.time() - t1 < 150:
        d = s.get(f"{BASE}/api/v2/case/{case2}/mdt", timeout=60).json()
        st = d.get("status") or {}
        seen.append(st.get("state"))
        if d.get("ready"):
            ready = True
            check("后台 MDT 完成", True, f"用时 {_time.time()-t1:.1f}s，"
                  f"卡片 {len((d.get('mdt') or {}).get('analyses') or [])} 张")
            break
        if st.get("state") == "failed":
            check("后台 MDT 完成", False, str(st.get("error"))[:120])
            break
        _time.sleep(2)
    check("轮询能看到 running 状态", "running" in seen, json.dumps(seen[:6], ensure_ascii=False))
    check("结束后 ready=true", ready)

    print("\n[12] 渐进式：规则秒开 + 模型三相后台 + 可重试")
    t0 = _time.time()
    r = s.post(f"{BASE}/api/v2/case", json={
        "preview_token": preview_token(s, BASE, CASE, "该看哪个科"), "age": 61, "sex": "男",
        "progressive": True}, timeout=60)
    first = _time.time() - t0
    prog = r.json() or {}
    check("渐进式建病例 200", r.status_code == 200 and prog.get("ok"), str(r.status_code))
    check("首屏为规则画像", prog.get("profile_is_rules_only") is True, str(prog.get("profile_is_rules_only")))
    check("后台任务已排入", prog.get("analysis_pending") is True, str(prog.get("analysis_pending")))
    check("规则分诊/路径/追问已就绪",
          bool(prog.get("triage")) and bool(prog.get("path")) and bool(prog.get("questions")), "")
    check("首屏足够快（<2 秒）", first < 2, f"{first*1000:.0f} ms")

    case3 = prog.get("case_id")
    a = s.get(f"{BASE}/api/v2/case/{case3}/analysis", timeout=30).json()
    check("analysis 接口可用", bool(a.get("analysis")), "")
    check("三个相位齐备", set((a["analysis"].get("phases") or {})) == {"profile", "triage", "mdt"},
          json.dumps(list((a["analysis"].get("phases") or {}).keys()), ensure_ascii=False))
    seen_states = []
    t1 = _time.time()
    while _time.time() - t1 < 180:
        a = s.get(f"{BASE}/api/v2/case/{case3}/analysis", timeout=30).json()
        st = a["analysis"]
        seen_states.append(st.get("state"))
        if st.get("state") != "running":
            break
        _time.sleep(2)
    check("相位全部完成", st.get("state") in ("done", "partial_failed"), str(st.get("state")))
    check("画像模型增强完成", st["phases"]["profile"]["state"] == "done", st["phases"]["profile"])
    check("就医方向相位完成", st["phases"]["triage"]["state"] == "done", st["phases"]["triage"])
    check("多学科已就绪", a.get("mdt_ready") is True and st["mdt"]["state"] == "done",
          json.dumps(st.get("mdt"), ensure_ascii=False))
    check("完成后病例状态推进",
          a.get("case_state") in ("awaiting_doctor", "analyzing"), str(a.get("case_state")))
    check("结果可被前端直接替换渲染",
          bool(a.get("profile")) and bool(a.get("triage")) and bool(a.get("path")), "")
    # 逐科失败的前端契约（真实故障：血液科意见卡被 max_tokens 截断 → 永远"未完成"且无法重试）
    check("多学科状态里带 failed 列表（前端据此提示哪一科没跑出来）",
          isinstance((st.get("mdt") or {}).get("failed"), list),
          json.dumps(st.get("mdt"), ensure_ascii=False))
    check("mdt 相位取值合法（done/partial/failed/skipped）",
          st["phases"]["mdt"]["state"] in ("done", "partial", "failed", "skipped"),
          st["phases"]["mdt"]["state"])
    check("全部科室成功时 failed 为空",
          (st.get("mdt") or {}).get("failed") == [], json.dumps(st.get("mdt"), ensure_ascii=False))
    r = s.post(f"{BASE}/api/v2/case/{case3}/analysis/retry", json={}, timeout=30)
    check("全部完成后重试被拒（400 且有说明）",
          r.status_code == 400 and "无需重试" in (r.json().get("error") or ""),
          json.dumps(r.json(), ensure_ascii=False)[:120])

    print("\n[9] 非法输入被拒")
    r = s.post(f"{BASE}/api/v2/case/{case_id}/updates",
               json={"kind": "胡编", "content": "x", "source": "患者上传"}, timeout=30)
    check("非法类别 400", r.status_code == 400, str(r.status_code))
    r = s.post(f"{BASE}/api/v2/case/{case_id}/updates",
               json={"kind": "检查结果", "content": "  ", "source": "患者上传"}, timeout=30)
    check("空内容 400", r.status_code == 400, str(r.status_code))

    print("\n[10] 脱敏预览（姓名误报回归：正文里的「患者转运」不该被当人名）")
    clean = "患者转运至上级医院。患者活动后气促，患者出院后反应良好，患者出现胸痛。"
    r = s.post(f"{BASE}/api/v2/preview", json={"medical_history": clean, "main_issue": "该看哪个科"}, timeout=30)
    preview = r.json() or {}
    check("预览 200", r.status_code == 200, str(r.status_code))
    check("正文无误报", not (preview.get("hits") or []),
          json.dumps(preview.get("hits"), ensure_ascii=False)[:160])
    check("脱敏文本未改动正文", clean in (preview.get("masked_text") or ""),
          (preview.get("masked_text") or "")[:80])

    with_name = "患者：李某某，男，58岁。视物模糊3月。"
    r = s.post(f"{BASE}/api/v2/preview", json={"medical_history": with_name, "main_issue": "该看哪个科"}, timeout=30)
    preview2 = r.json() or {}
    name_hits = [h for h in (preview2.get("hits") or []) if h.get("kind") == "name"]
    check("真人名仍被识别", len(name_hits) == 1, json.dumps(name_hits, ensure_ascii=False)[:160])
    check("给出命中规则", bool(name_hits and "命中规则" in (name_hits[0].get("reason") or "")),
          (name_hits[0].get("reason") if name_hits else ""))
    check("打码后不含原名", "李某某" not in (preview2.get("masked_text") or ""),
          (preview2.get("masked_text") or "")[:80])

    print(f"\n===== 结果：{ok_count} 通过 / {fail_count} 失败 =====")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
