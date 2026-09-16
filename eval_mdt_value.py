# -*- coding: utf-8 -*-
"""实验三：MDT 增量价值对比（同一病例：单科 AI 问诊 vs 本系统 MDT）。

对应《评测实验设计方案-MDT》§3 实验三 —— 设计文档里标注为"必须做"的核心实验。
它回答的是：**多专科协同比"把病历丢给一个科室的 AI"多发现了什么？**

做法（同一份病例，两条链路）：
- 对照组 A：单科 AI 问诊 —— 只把病例交给"最相关的那一科"的模型视角，让它给出建议；
- 实验组 B：本系统 MDT —— 规则分诊 → 逐专科独立意见卡（真实模型）→ 聚合；
- 指标：
  * 科室覆盖数（B 的分诊方向数 vs A 恒为 1）
  * **单科漏项检出数（最重要）**：B 提出的检查/待确认项里，A **没有提到**的条数（按规范主题归一后比较）
  * 结构化字段完整率（B 每张卡的 focus/facts/inferences/checks/gaps 是否齐备；A 是自由文本）
  * 分歧是否如实呈现（B 的聚合层分歧数）
  * 成本代理：模型调用次数 + 耗时（AIService 未暴露 token 计数，如实用调用次数代替）

运行（在 CHAIS 目录下，需要 .env 里的模型 Key）：
    python eval_mdt_value.py                 # 默认取自建集里 B/C 型共病/疑难病例
    python eval_mdt_value.py --limit 4       # 只跑前 4 例（省 token）
输出：控制台表格 + `eval_out/eval_mdt_value_<时间戳>.json`（含逐例明细与会诊卡样例）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Dict, List, Sequence, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

try:
    from dotenv import load_dotenv  # noqa: PLC0415
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    load_dotenv(os.path.join(BASE_DIR, "backend", ".env"))
except ImportError:                                          # pragma: no cover
    pass

from services.ai_service import AIService, call_structured  # noqa: E402
from services.json_recovery import loads_lenient  # noqa: E402
from services.mdt_analyst import aggregate, analyze_specialty, canonical_topic  # noqa: E402
from services.rule_engine import evaluate as evaluate_rules  # noqa: E402
from services.triage import orchestrate  # noqa: E402

SELF_JSON = os.path.join(BASE_DIR, "eval_cases_selfbuilt.json")
OUT_DIR = os.path.join(BASE_DIR, "eval_out")
MDT_CAP = 3                                                  # 与产品一致：多学科分析方向上限

SINGLE_SYSTEM = (
    "你是一位{dept}的门诊医生助手（不是真实医生，不构成诊断）。"
    "请只做与本科相关的信息整理与分析提示，不做诊断结论、不开处方、不给治疗方案。"
    "所有判断必须区分事实与推断。输出严格 JSON。"
)

SINGLE_PROMPT = """【病例资料】
{text}

【输出要求】只输出 JSON：
{{
  "assessments": ["你作为{dept}认为需要进一步确认的问题（每条≤40字）"],
  "checks": ["建议向医生确认是否需要做的检查（每条≤40字）"],
  "information_gaps": ["缺失且影响判断的信息"],
  "referral": "若超出本科范围，写出应转给哪个科；否则填 null"
}}

【硬性禁止】不得给出诊断结论；不得开药或给剂量；不得凭空编造病例中没有的检查或病史。
"""


# ---------------------------------------------------------------------------
def load_cases(limit: int = 0) -> List[Dict[str, object]]:
    with open(SELF_JSON, encoding="utf-8") as f:
        data = json.load(f)
    cases = [c for c in data.get("cases", []) if c.get("behavior") in ("B", "C")]
    return cases[:limit] if limit else cases


def norm(text: str) -> str:
    return re.sub(r"[\s，。；、,.;:：（）()\[\]]+", "", str(text or ""))


def bigrams(text: str) -> set:
    t = norm(text)
    return {t[i:i + 2] for i in range(max(0, len(t) - 1))}


def covered(item: str, reference: Sequence[str], ref_topics: set) -> bool:
    """B 的某一项是否已被 A 覆盖：先比规范主题，再退化为二元组重叠。"""
    topic = canonical_topic(item)
    if topic and topic in ref_topics:
        return True
    item_grams = bigrams(item)
    if not item_grams:
        return False
    for ref in reference:
        ref_grams = bigrams(ref)
        if not ref_grams:
            continue
        overlap = len(item_grams & ref_grams)
        if overlap >= max(2, int(0.34 * len(item_grams))):
            return True
    return False


# ---------------------------------------------------------------------------
def run_single(case: Dict[str, object], dept: str, ai: AIService) -> Dict[str, object]:
    """对照组 A：单科 AI 问诊。"""
    prompt = SINGLE_PROMPT.format(text=str(case["text"])[:3000], dept=dept)
    content, truncated, error = call_structured(
        ai, prompt, role_description=SINGLE_SYSTEM.format(dept=dept), max_tokens=1200)
    if not content:
        return {"ok": False, "error": error or "模型未返回内容", "items": [], "dept": dept}
    data, strategy = loads_lenient(content, fields=("assessments", "checks",
                                                    "information_gaps", "referral"))
    if data is None:
        return {"ok": False, "error": "输出无法解析", "items": [], "dept": dept}
    items: List[str] = []
    for key in ("assessments", "checks", "information_gaps"):
        items += [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]
    return {"ok": True, "dept": dept, "items": items, "truncated": truncated,
            "strategy": strategy, "referral": data.get("referral")}


def run_mdt(case: Dict[str, object], ai: AIService) -> Dict[str, object]:
    """实验组 B：本系统 MDT（规则分诊 → 逐专科意见卡 → 聚合）。"""
    text = str(case["text"])
    evaluation = evaluate_rules(text)
    outcome = orchestrate(text=text, evaluation=evaluation)
    departments = outcome.selected[:MDT_CAP]
    cards, analyses = [], []
    for dept in departments:
        analysis = analyze_specialty(ai_service=ai, department=dept, text=text)
        analyses.append(analysis)
        if analysis.card is not None:
            cards.append(analysis.card)
    agg = aggregate(cards)
    items: List[str] = []
    for card in cards:
        items += [f"{k.content}" for k in card.checks]
        items += list(card.information_gaps)
        items += [i.text for i in card.inferences]
    return {"ok": True, "departments": departments, "selected": outcome.selected,
            "cards": cards, "analyses": analyses, "aggregation": agg, "items": items}


def structured_completeness(cards: Sequence[object]) -> float:
    """结构化字段完整率：每张卡 focus/facts/inferences/checks 齐备即为完整。"""
    if not cards:
        return 0.0
    ok = sum(1 for c in cards if (c.focus and c.facts and c.inferences and c.checks))
    return ok / len(cards)


def main() -> int:
    parser = argparse.ArgumentParser(description="实验三：MDT 增量价值对比")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 例（省 token）")
    args = parser.parse_args()

    ai = AIService()
    if not getattr(ai, "api_key", ""):
        print("未配置 AI_API_KEY（.env），无法运行实验三")
        return 2

    cases = load_cases(args.limit)
    print("=" * 96)
    print(f"实验三：MDT 增量价值 | 用例 {len(cases)} 例（自建集 B/C 型共病与疑难病例）| 模型 {ai.model_name}")
    print("=" * 96)

    rows = []
    for case in cases:
        t0 = time.time()
        text = str(case["text"])
        evaluation = evaluate_rules(text)
        preview = orchestrate(text=text, evaluation=evaluation)
        primary = (preview.primary.department if preview.primary else "（未定）")
        single = run_single(case, primary, ai)
        single_seconds = time.time() - t0

        t1 = time.time()
        mdt = run_mdt(case, ai)
        mdt_seconds = time.time() - t1

        ref_topics = {canonical_topic(x) for x in single["items"]}
        ref_topics.discard(None)
        missed = [x for x in mdt["items"] if not covered(x, single["items"], ref_topics)]
        # 反向指标：单科给出的建议有多少被 MDT 保住（否则 MDT 只是"说得更多"却漏掉了单科的重点）
        mdt_topics = {canonical_topic(x) for x in mdt["items"]}
        mdt_topics.discard(None)
        lost = [x for x in single["items"] if not covered(x, mdt["items"], mdt_topics)]
        row = {
            "case_id": case["case_id"],
            "behavior": case.get("behavior"),
            "single_dept": primary,
            "single_items": len(single["items"]),
            "mdt_depts": mdt["departments"],
            "mdt_items": len(mdt["items"]),
            "missed_by_single": missed,
            "lost_by_mdt": lost,
            "missed_rate": round(len(missed) / max(1, len(mdt["items"])), 2),
            "keep_rate": round(1 - len(lost) / max(1, len(single["items"])), 2),
            "cards": len(mdt["cards"]),
            "structured": structured_completeness(mdt["cards"]),
            "disagreements": len(mdt["aggregation"].disagreements),
            "single_seconds": round(single_seconds, 1),
            "mdt_seconds": round(mdt_seconds, 1),
            "single_calls": 1,
            "mdt_calls": len(mdt["departments"]),
            "single_error": single.get("error", ""),
        }
        rows.append(row)
        print(f"\n[{case['case_id']}] {case.get('note', '')}")
        print(f"  A 单科问诊（{primary}）：{row['single_items']} 条建议，{single_seconds:.1f}s，1 次调用")
        print(f"  B 本系统 MDT（{'、'.join(mdt['departments'])}）：{row['cards']} 张意见卡，"
              f"{row['mdt_items']} 条，{mdt_seconds:.1f}s，{len(mdt['departments'])} 次调用")
        print(f"  → 单科漏项检出：{len(missed)} 条（占 B 的 {row['missed_rate']:.0%}）"
              + (f"｜例：{missed[0][:34]}" if missed else ""))
        print(f"  → 单科建议被 MDT 保全：{row['keep_rate']:.0%}"
              + (f"（未保住 {len(lost)} 条，例：{lost[0][:30]}）" if lost else ""))
        if row["disagreements"]:
            print(f"  → 聚合层检出分歧 {row['disagreements']} 处（如实呈现，不强行统一）")

    print("\n" + "=" * 96)
    print(f"{'用例':<9}{'型':<3}{'A条数':>6}{'B条数':>6}{'单科漏项':>9}{'漏项率':>7}{'保全率':>7}{'卡片':>5}{'结构化':>8}{'B耗时':>8}")
    print("-" * 96)
    for row in rows:
        print(f"{row['case_id']:<9}{row['behavior']:<3}{row['single_items']:>6}{row['mdt_items']:>6}"
              f"{len(row['missed_by_single']):>9}{row['missed_rate']:>7.0%}{row['keep_rate']:>7.0%}"
              f"{row['cards']:>5}{row['structured']:>8.2f}{row['mdt_seconds']:>8.1f}")
    print("-" * 96)
    n = len(rows) or 1
    total_missed = sum(len(r["missed_by_single"]) for r in rows)
    total_items = sum(r["mdt_items"] for r in rows)
    keep = sum(r["keep_rate"] for r in rows) / n
    print(f"合计：{len(rows)} 例｜单科漏项检出共 {total_missed} 条（平均 {total_missed / n:.1f} 条/例，"
          f"占 MDT 产出的 {total_missed / max(1, total_items):.0%}）｜"
          f"单科建议被 MDT 保全 {keep:.0%}")
    print(f"B 平均科室数 {sum(len(r['mdt_depts']) for r in rows) / n:.2f}（A 恒为 1）｜"
          f"B 平均耗时 {sum(r['mdt_seconds'] for r in rows) / n:.1f}s")
    print(f"结构化字段完整率：{sum(r['structured'] for r in rows) / n:.2f}（A 为自由文本，无法逐字段核对）")
    print(f"模型调用次数：A {sum(r['single_calls'] for r in rows)} 次 vs B {sum(r['mdt_calls'] for r in rows)} 次"
          f"（成本代理；AIService 未暴露 token 计数）")
    print("=" * 96)
    print("⚠️ 口径说明（务必一起引用）：")
    print("  1) MDT 的输出**条数天然更多**（3 张卡 vs 1 次单科问诊），因此『漏项检出条数』受**产出量**影响，")
    print("     不能直接读成『有价值的信息多了 18 倍』；『漏项率』(占 MDT 产出的比例) 与『保全率』")
    print("     (单科建议被 MDT 保住的比例) 是控制产出量后的两个参照。")
    print("  2) 严格的『价值』评估应如设计文档所述：1-2 名医学背景同学按 checklist 双盲打分 —— 本脚本未做。")
    print("  3) 单次运行受模型随机性影响（temperature=0.3），正式报数建议重复 3 次取均值。")

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUT_DIR, f"eval_mdt_value_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"model": ai.model_name, "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"\n逐例明细已落盘：{os.path.relpath(out, BASE_DIR)}（eval_out/ 不进 git）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
