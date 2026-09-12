#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分诊评测 v1：规则引擎 vs 纯 LLM vs 完整链路（S1+S2+S3）。

对应《产品开发规格书-比赛版v-competition.md》任务 2.5：
「分诊召回提升 —— 域内召回 ≥0.80（基线 0.51）」。

与 v0 的关系：
- `eval_triage_v0.py` 评测的是**改前**的旧管线（LLM + 关键词回退），结果见
  `评测v0结果与改进方向.md`（Macro 召回 0.447 / 0.513）；**该脚本保持只读，不改动**；
- 本脚本评测**改后**的三条路径，便于 before/after 对比。

数据集：
- `MDT-5.csv`：5 例真实脱敏 MDT 病例，金标准 = 「会诊专家」列（本脚本自动归一化）；
- 规格书 §12 的跨病种验收集（A/B/C/D 四类）作为补充用例。

用法（在 CHAIS/ 目录下）：
    python eval_triage_v1.py            # 纯规则（不消耗 API）
    python eval_triage_v1.py --with-llm # 额外跑「纯 LLM」与「完整链路」对比
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from services.department_domain import resolve  # noqa: E402
from services.rule_engine import evaluate  # noqa: E402
from services.triage import orchestrate  # noqa: E402

GOLD_CSV = os.path.join(BASE_DIR, "MDT-5.csv")
OUT_DIR = os.path.join(BASE_DIR, "eval_out")

# ---------------------------------------------------------------------------
# 规格书 §12 跨病种验收集（金标准由文档给定）
# ---------------------------------------------------------------------------

SPEC_CASES: List[Dict[str, object]] = [
    {
        "case_id": "SPEC-A",
        "text": "患者，男，30岁。咽痛3天，轻度咳嗽，无发热，无基础疾病。",
        "goal": "该看哪个科",
        "gold": ["呼吸内科"],
        "source": "规格书§12-A",
    },
    {
        "case_id": "SPEC-B",
        "text": ("患者，女，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
                 "既往2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2。"),
        "goal": "该看哪个科",
        "gold": ["内分泌科", "肾内科", "眼科"],
        "source": "规格书§12-B",
    },
    {
        "case_id": "SPEC-C",
        "text": ("患者，女，35岁。反复发热3周，伴乏力、盗汗，血常规提示三系减少，"
                 "诊断不明，查因中。"),
        "goal": "发热查因",
        "gold": ["血液科", "风湿免疫科", "感染科"],
        "source": "规格书§12-C",
    },
    {
        "case_id": "SPEC-D",
        "text": ("患者，男，45岁。房间隔缺损，拟行房间隔缺损修补术，"
                 "合并心房颤动拟行射频消融术，需术前评估。"),
        "goal": "术前准备",
        "gold": ["心胸外科", "麻醉科", "超声科"],
        "source": "规格书§12-D",
    },
]


# ---------------------------------------------------------------------------
# 金标准解析
# ---------------------------------------------------------------------------

def normalize_experts(raw: str) -> Tuple[List[str], List[str]]:
    """把「会诊专家」列归一化成科室域标准名。

    真实数据里存在换行把科名切断的情况（如 "超声\\n科麻醉科"），
    因此对解析失败的片段尝试与相邻片段拼接后再解析（只拼一次）。
    返回 (已识别, 未识别) —— 未识别项如实报告，不静默丢弃。
    """
    if not raw:
        return [], []
    fragments = [f.strip() for f in str(raw).split("\n") if f.strip()]

    recognized: List[str] = []
    unknown: List[str] = []
    i = 0
    while i < len(fragments):
        frag = fragments[i]
        r = resolve(frag)
        if r.ok:
            if r.name not in recognized:
                recognized.append(r.name)
            i += 1
            continue
        # 尝试与下一片段拼接（解决"超声"+"科麻醉科"这类断行）
        if i + 1 < len(fragments):
            merged = frag + fragments[i + 1]
            r2 = resolve(merged)
            if r2.ok:
                if r2.name not in recognized:
                    recognized.append(r2.name)
                i += 2
                continue
            # 或把下一片段的前缀补到当前片段尾部
            for cut in (1, 2):
                candidate = frag + fragments[i + 1][:cut]
                r3 = resolve(candidate)
                if r3.ok:
                    if r3.name not in recognized:
                        recognized.append(r3.name)
                    rest = fragments[i + 1][cut:].strip()
                    fragments[i + 1] = rest
                    i += 1
                    break
            else:
                unknown.append(frag)
                i += 1
            continue
        unknown.append(frag)
        i += 1
    return recognized, unknown


def load_mdt_cases() -> List[Dict[str, object]]:
    """加载 MDT-5.csv（只读，不修改）。"""
    cases: List[Dict[str, object]] = []
    with open(GOLD_CSV, encoding="utf-8-sig", newline="") as f:
        for idx, row in enumerate(csv.DictReader(f), 1):
            gold, unknown = normalize_experts(row.get("会诊专家", ""))
            text = f"{row.get('病情介绍', '')}\n{row.get('讨论目的', '')}".strip()
            if not text:
                continue
            cases.append({
                "case_id": f"MDT-{idx}",
                "text": text,
                "goal": row.get("讨论目的", ""),
                "gold": gold,
                "gold_unknown": unknown,
                "source": "MDT-5.csv",
            })
    return cases


# ---------------------------------------------------------------------------
# 三种预测方法
# ---------------------------------------------------------------------------

def predict_rules_only(text: str) -> List[str]:
    """路径①：纯规则路径（S1 规则引擎 + S3 编排，**不调用模型**）。

    注意：真实决策链里 S1 之后必经 S3（数量控制 ≤6、牵头科约束、校验器），
    因此评测也应按 S1+S3 口径，而不是裸 S1 的候选全集。
    """
    ev = evaluate(text)
    return orchestrate(text=text, evaluation=ev).selected


def predict_full_chain(text: str, ai_service) -> List[str]:
    """路径③：完整链路 S1 + S2（受限发言）+ S3（编排）。"""
    from services.s2_analyst import analyze as s2_analyze

    ev = evaluate(text)
    s2 = s2_analyze(ai_service=ai_service, text=text, evaluation=ev)
    outcome = orchestrate(text=text, evaluation=ev, model_picks=s2.orchestrator_picks)
    return outcome.selected


def predict_llm_only(text: str, ai_service) -> List[str]:
    """路径②：纯 LLM 选科（对照组，模拟"直接把病历丢给大模型"）。"""
    from services.department_domain import ALL_NAMES

    prompt = (
        "请根据下面的病例，判断应该由哪些临床科室参与诊治。\n"
        f"只能从下列科室中选择：{'、'.join(ALL_NAMES)}\n"
        "最多选 6 个，按相关性从高到低排列。\n"
        '只输出 JSON：{"departments": ["科室1", "科室2"]}\n\n'
        f"病例：\n{text[:3000]}"
    )
    raw = ai_service.call_ai_api(prompt, role_description="临床分诊助手")
    if not raw:
        return []
    import json
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: List[str] = []
    for name in (data.get("departments") or []):
        r = resolve(str(name))
        if r.ok and r.name not in out:
            out.append(r.name)
    return out


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def score(predicted: Sequence[str], gold: Sequence[str]) -> Dict[str, float]:
    """单例指标（与 v0 口径一致）。"""
    pred, g = set(predicted), set(gold)
    if not g:
        return {"recall": 0.0, "precision": 0.0, "f1": 0.0, "hit_all": 0.0,
                "n_pred": len(pred), "n_gold": 0}
    tp = len(pred & g)
    recall = tp / len(g)
    precision = tp / len(pred) if pred else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"recall": recall, "precision": precision, "f1": f1,
            "hit_all": 1.0 if g <= pred else 0.0,
            "n_pred": len(pred), "n_gold": len(g)}


def summarize(rows: Sequence[Dict[str, object]]) -> Dict[str, float]:
    if not rows:
        return {}
    n = len(rows)
    return {
        "cases": n,
        "macro_recall": sum(r["recall"] for r in rows) / n,
        "macro_precision": sum(r["precision"] for r in rows) / n,
        "macro_f1": sum(r["f1"] for r in rows) / n,
        "hit_all_cases": sum(r["hit_all"] for r in rows),
        "avg_pred": sum(r["n_pred"] for r in rows) / n,
        "avg_gold": sum(r["n_gold"] for r in rows) / n,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="分诊评测 v1")
    parser.add_argument("--with-llm", action="store_true",
                        help="额外评测「纯 LLM」与「完整链路」（会消耗 API）")
    args = parser.parse_args()

    cases = load_mdt_cases()
    cases.extend(SPEC_CASES)

    print("=" * 74)
    print(f"分诊评测 v1 | 用例数 {len(cases)}"
          f"（MDT-5: {sum(1 for c in cases if c['source'] == 'MDT-5.csv')}，"
          f"规格书验收集: {sum(1 for c in cases if c['source'].startswith('规格书'))}）")
    print("=" * 74)

    for case in cases:
        gold = case["gold"]
        unknown = case.get("gold_unknown") or []
        flag = f"  ⚠ 金标准含未识别项: {unknown}" if unknown else ""
        print(f"  {case['case_id']:<9} 金标准({len(gold)}): {'、'.join(gold) or '—'}{flag}")
    print()

    methods: Dict[str, List[Dict[str, object]]] = {}

    # ---- 路径①：纯规则（不消耗 API）----
    rows = []
    for case in cases:
        predicted = predict_rules_only(str(case["text"]))
        s = score(predicted, case["gold"])
        s.update({"case_id": case["case_id"], "method": "rules_only",
                  "predicted": "、".join(predicted),
                  "gold": "、".join(case["gold"]), "source": case["source"]})
        rows.append(s)
    methods["rules_only"] = rows

    # ---- 路径②③：需要 LLM ----
    if args.with_llm:
        try:
            from dotenv import load_dotenv, find_dotenv
            load_dotenv(find_dotenv(usecwd=True))
            from services.ai_service import AIService
            ai = AIService()
            if not ai.api_key:
                raise RuntimeError("未配置 AI_API_KEY")
        except Exception as exc:
            print(f"[跳过 LLM 评测] {exc}")
            ai = None

        if ai is not None:
            for method, fn in (("llm_only", predict_llm_only),
                               ("full_chain", predict_full_chain)):
                rows = []
                print(f"[{method}] 开始评测…")
                for case in cases:
                    t0 = time.time()
                    try:
                        predicted = fn(str(case["text"]), ai)
                    except Exception as exc:
                        print(f"    {case['case_id']} 失败: {exc}")
                        predicted = []
                    s = score(predicted, case["gold"])
                    s.update({"case_id": case["case_id"], "method": method,
                              "predicted": "、".join(predicted),
                              "gold": "、".join(case["gold"]), "source": case["source"],
                              "seconds": round(time.time() - t0, 1)})
                    rows.append(s)
                    print(f"    {case['case_id']:<9} {s['recall']:.2f} 召回 | "
                          f"{s['precision']:.2f} 精确 | {s['n_pred']} 选科 | "
                          f"{s['seconds']}s")
                methods[method] = rows

    # ---- 汇总输出 ----
    os.makedirs(OUT_DIR, exist_ok=True)
    detail_path = os.path.join(OUT_DIR, "triage_v1_detail.csv")
    with open(detail_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "case_id", "method", "source", "gold", "predicted",
            "recall", "precision", "f1", "hit_all", "n_pred", "n_gold", "seconds"])
        writer.writeheader()
        for rows in methods.values():
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in writer.fieldnames})

    print()
    print("=" * 74)
    print(f"{'方法':<14}{'用例':>4}{'Macro召回':>11}{'Macro精确':>11}{'MacroF1':>9}"
          f"{'全命中':>7}{'平均选科':>9}{'金标准':>8}")
    print("-" * 74)
    summary_rows = []
    for method, rows in methods.items():
        s = summarize(rows)
        print(f"{method:<14}{s['cases']:>4}{s['macro_recall']:>11.3f}"
              f"{s['macro_precision']:>11.3f}{s['macro_f1']:>9.3f}"
              f"{s['hit_all_cases']:>7.0f}{s['avg_pred']:>9.1f}{s['avg_gold']:>8.1f}")
        summary_rows.append({"method": method, **{k: round(v, 4) for k, v in s.items()}})
    print("=" * 74)

    # v0 基线对照
    print("\n[v0 基线对照] 旧管线（LLM + 关键词回退）：Macro 召回 0.447 / 0.513，"
          "Macro 精确 0.207 / 0.232（见 评测v0结果与改进方向.md）")

    # 达标判定
    rr = summarize(methods["rules_only"])
    target = 0.80
    print(f"\n[验收] 纯规则路径 Macro 召回 {rr['macro_recall']:.3f} vs 目标 {target}"
          f" → {'达标 ✅' if rr['macro_recall'] >= target else '未达标 ⚠'}")

    summary_path = os.path.join(OUT_DIR, "triage_v1_summary.csv")
    with open(summary_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"\n明细: {detail_path}")
    print(f"汇总: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
