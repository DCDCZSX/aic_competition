# -*- coding: utf-8 -*-
"""分诊评测 v2：把评测集从 n=9 扩到 n≥30（规格书 §5.1），并**分子集报告**。

为什么要分子集：
- `MDT-5.csv`（n=5）是**真实会诊记录**，金标准来自「会诊专家」列 —— 最可信，但数据量小；
- 规格书 §12 验收集（n=4）是**文档给定的期望行为**（含 A/B/C/D 四类）；
- `eval_cases_selfbuilt.json`（n=25）是**自建/改写样例**，期望科室由团队按临床常识标注，
  **未经执业医师签字确认**，且写法接近系统输入格式，因此分数会偏乐观 —— 必须单独列出，绝不混进"真实金标准"里报数。
- 急症用例（behavior=E）不参与召回：它们只验「能否被 G1 拦住」，单独统计拦截正确率。

对照方法沿用 v1 三条：纯规则（S1+S3）／纯 LLM 选科／完整链路（S1+S2+S3）。
运行（在 CHAIS 目录下）：
    python eval_triage_v2.py                # 只跑纯规则（不烧 token）
    python eval_triage_v2.py --with-llm     # 追加纯 LLM 与完整链路（消耗 API）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Sequence

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 独立脚本不像 app.py 会自动加载 .env：这里显式加载，否则 --with-llm 会因没有 Key 而静默跳过
try:
    from dotenv import load_dotenv  # noqa: PLC0415
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    load_dotenv(os.path.join(BASE_DIR, "backend", ".env"))
except ImportError:                                          # pragma: no cover
    pass

from eval_triage_v1 import (  # noqa: E402
    SPEC_CASES,
    load_mdt_cases,
    predict_full_chain,
    predict_llm_only,
    predict_rules_only,
    score,
    summarize,
)

SELF_JSON = os.path.join(BASE_DIR, "eval_cases_selfbuilt.json")
OUT_DIR = os.path.join(BASE_DIR, "eval_out")

SUBSETS = ("真实会诊记录", "规格书§12", "自建用例")


# ---------------------------------------------------------------------------
def load_selfbuilt() -> List[Dict[str, object]]:
    with open(SELF_JSON, encoding="utf-8") as f:
        data = json.load(f)
    cases: List[Dict[str, object]] = []
    for item in data.get("cases", []):
        cases.append({
            "case_id": item["case_id"],
            "text": item["text"],
            "goal": item.get("goal", ""),
            "gold": list(item.get("gold") or []),
            "behavior": item.get("behavior", ""),
            "source": "eval_cases_selfbuilt.json",
            "subset": "自建用例",
            "note": item.get("note", ""),
        })
    return cases


def load_all() -> List[Dict[str, object]]:
    cases: List[Dict[str, object]] = []
    try:
        real = load_mdt_cases()
        for case in real:
            case["subset"] = "真实会诊记录"
        cases += real
    except FileNotFoundError:
        print("[提示] 未找到 MDT-5.csv（真实会诊记录不进 git）→ 本次跳过该子集")
    for case in SPEC_CASES:
        case = dict(case)
        case["subset"] = "规格书§12"
        case["behavior"] = case["case_id"].split("-")[-1]      # SPEC-A → A
        cases.append(case)
    cases += load_selfbuilt()
    return cases


def evaluate(method: str, cases: Sequence[Dict[str, object]], predictor) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for case in cases:
        if case.get("behavior") == "E":
            continue                                            # 急症单独统计
        try:
            predicted = predictor(case["text"])
            error = ""
        except Exception as exc:                                # noqa: BLE001
            predicted, error = [], f"{type(exc).__name__}: {exc}"
        s = score(predicted, case["gold"])
        rows.append({"case": case, "method": method, "predicted": predicted, "score": s,
                     "error": error})
    return rows


def report(rows: Sequence[Dict[str, object]], cases: Sequence[Dict[str, object]]) -> None:
    """按方法 × 子集打印表格（这是"数字可追溯"的关键：每个数字都能追到哪个子集）。"""
    methods = []
    for row in rows:
        if row["method"] not in methods:
            methods.append(row["method"])
    print()
    print("=" * 96)
    print(f"{'方法':<18}{'子集':<16}{'用例':>5}{'Macro召回':>11}{'Macro精确':>11}{'MacroF1':>10}{'全命中':>8}{'平均选科':>9}")
    print("-" * 96)
    for method in methods:
        for subset in SUBSETS:
            group = [r for r in rows if r["method"] == method and r["case"].get("subset") == subset]
            if not group:
                continue
            s = summarize([r["score"] for r in group])
            print(f"{method:<18}{subset:<16}{s['cases']:>5}{s['macro_recall']:>11.3f}"
                  f"{s['macro_precision']:>11.3f}{s['macro_f1']:>10.3f}"
                  f"{int(s['hit_all_cases']):>8}{s['avg_pred']:>9.2f}")
        group = [r for r in rows if r["method"] == method]
        s = summarize([r["score"] for r in group])
        print(f"{method:<18}{'合计':<16}{s['cases']:>5}{s['macro_recall']:>11.3f}"
              f"{s['macro_precision']:>11.3f}{s['macro_f1']:>10.3f}"
              f"{int(s['hit_all_cases']):>8}{s['avg_pred']:>9.2f}")
        print("-" * 96)

    # 错分明细：召回不足的用例逐条列出（改进方向的直接依据）
    worst = [r for r in rows if r["method"] == "完整链路" or r["method"] == "纯规则（S1+S3）"]
    worst = [r for r in worst if r["score"]["recall"] < 1.0]
    if worst:
        print("\n召回未满分的用例（改进方向依据）：")
        for row in sorted(worst, key=lambda r: r["score"]["recall"])[:24]:
            case = row["case"]
            missed = [d for d in case["gold"] if d not in row["predicted"]]
            extra = [d for d in row["predicted"] if d not in case["gold"]]
            print(f"  [{row['method']}] {case['case_id']:<9} 召回 {row['score']['recall']:.2f} "
                  f"漏 {('、'.join(missed) or '—'):<22} 多 {('、'.join(extra) or '—')}")

    # 急症拦截
    print("\n急症用例（behavior=E，只验是否被 G1 拦住）：")
    for case in cases:
        if case.get("behavior") != "E":
            continue
        from services.guardrail import screen
        verdict = screen(case["text"])
        blocked = bool(getattr(verdict, "blocked", False))
        print(f"  {case['case_id']:<9} {'✅ 已拦截' if blocked else '❌ 未拦截'}  "
              f"{case['text'][:34]}…")


def main() -> int:
    parser = argparse.ArgumentParser(description="分诊评测 v2（评测集 n≥30，分子集报告）")
    parser.add_argument("--with-llm", action="store_true", help="追加纯 LLM 与完整链路（消耗 API）")
    args = parser.parse_args()

    cases = load_all()
    subsets = {}
    for case in cases:
        subsets.setdefault(case.get("subset", "其他"), 0)
        subsets[case["subset"]] += 1
    print("=" * 96)
    print(f"分诊评测 v2 | 合计 {len(cases)} 例：" +
          "，".join(f"{k} {v}" for k, v in subsets.items()))
    print("=" * 96)

    rows: List[Dict[str, object]] = []
    rows += evaluate("纯规则（S1+S3）", cases, predict_rules_only)

    if args.with_llm:
        from services.ai_service import AIService
        ai = AIService()
        if not getattr(ai, "api_key", ""):
            print("[跳过 LLM 评测] 未配置 AI_API_KEY")
        else:
            for method, predictor in (("纯 LLM 选科", lambda t: predict_llm_only(t, ai)),
                                      ("完整链路（S1+S2+S3）", lambda t: predict_full_chain(t, ai))):
                t0 = time.time()
                print(f"[{method}] 评测中…")
                rows += evaluate(method, cases, predictor)
                print(f"[{method}] 完成，耗时 {time.time() - t0:.1f}s")
    report(rows, cases)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUT_DIR, f"eval_triage_v2_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"cases": cases,
                   "rows": [{"case_id": r["case"]["case_id"], "subset": r["case"].get("subset"),
                             "method": r["method"], "predicted": r["predicted"],
                             "gold": r["case"]["gold"], "score": r["score"],
                             "error": r["error"]} for r in rows]},
                  f, ensure_ascii=False, indent=2)
    print(f"\n逐例明细已落盘：{os.path.relpath(out, BASE_DIR)}（eval_out/ 不进 git）")

    rules = summarize([r["score"] for r in rows if r["method"] == "纯规则（S1+S3）"])
    print(f"\n[验收] 纯规则 Macro 召回 {rules['macro_recall']:.3f}（规格书 §5.1 目标 ≥0.80）"
          f" → {'达标 ✅' if rules['macro_recall'] >= 0.80 else '未达标 ❌'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
