"""证据板：时间线 + 判断↔证据关联 + 证据卡片（支持/反对双列）。

对应《产品开发规格书-比赛版v-competition.md》任务 3.1/3.2 与主文档 §9。

三条红线（写死）：
1. **"相关"不等于"支持"**——只有同源直引才标 `direct`，否则 `indirect`/`missing`；
2. **反对证据为空也要显式标注**"未见反对证据"，不能只展示有利的一面；
3. **外部医学证据必须可追溯**：比赛版只认白名单条目（教材/公开指南），未命中就写
   "暂无可靠外部依据"，**不接 RAG、不伪造文献**。

比赛版范围（规格书 §6.2）：只做 ①时间线 ②判断↔证据关联 ③证据卡片双列；
全量 what-if 与会诊级重算属 P1。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 证据状态（与 schemas.EVIDENCE_STATES 一致）
STATE_DIRECT = "direct"
STATE_INDIRECT = "indirect"
STATE_MISSING = "missing"
STATE_CONFLICT = "conflict"
STATE_DOCTOR_CONFIRMED = "doctor_confirmed"
STATE_AWAITING_DOCTOR = "awaiting_doctor"

STATE_LABELS: Dict[str, str] = {
    STATE_DIRECT: "有明确证据",
    STATE_INDIRECT: "证据间接支持",
    STATE_MISSING: "缺少证据",
    STATE_CONFLICT: "与现有证据冲突",
    STATE_DOCTOR_CONFIRMED: "已被医生确认",
    STATE_AWAITING_DOCTOR: "等待医生确认",
}

# ---------------------------------------------------------------------------
# 外部医学证据白名单（比赛版：人工维护、可追溯；不做 RAG 检索）
# 命中条件 = 判断文本里出现关键词；未命中一律显示"暂无可靠外部依据"。
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExternalEvidence:
    """一条可追溯的外部依据（教材/公开指南）。"""

    keywords: tuple
    title: str
    source: str
    note: str = ""
    version: str = ""
    identifier: str = ""
    url: str = ""
    scope: str = ""
    excerpt: str = ""
    reviewed_at: str = ""

    @property
    def traceable(self) -> bool:
        return bool(self.version and self.identifier and self.url and self.reviewed_at)


EXTERNAL_WHITELIST: tuple = (
    ExternalEvidence(
        ("糖尿病视网膜病变", "视网膜", "眼底"),
        "糖尿病视网膜病变防治专家共识", "中华医学会眼科学分会（公开共识）",
        "糖尿病患者应定期行眼底检查",
    ),
    ExternalEvidence(
        ("糖尿病肾病", "蛋白尿", "尿蛋白"),
        "糖尿病肾病临床诊疗指南", "中华医学会肾脏病学分会（公开指南）",
        "尿白蛋白/肌酐比值与 eGFR 是筛查核心指标",
    ),
    ExternalEvidence(
        ("缺血性心脏病", "冠脉", "心肌缺血", "胸痛"),
        "稳定性冠心病诊断与治疗指南", "中华医学会心血管病学分会（公开指南）",
        "胸痛评估需结合心电图与危险分层",
    ),
    ExternalEvidence(
        ("心力衰竭", "心衰", "浮肿", "水肿"),
        "中国心力衰竭诊断和治疗指南", "中华医学会心血管病学分会（公开指南）",
        "容量负荷评估与利钠肽检测是基础评估项",
    ),
    ExternalEvidence(
        ("卒中", "脑梗", "偏瘫", "肢体无力"),
        "中国急性缺血性脑卒中诊治指南", "中华医学会神经病学分会（公开指南）",
        "急性期需尽快完成影像与血管评估",
    ),
    ExternalEvidence(
        ("噬血", "三系减少", "血细胞减少"),
        "噬血细胞性淋巴组织细胞增生症诊疗指南", "中华医学会血液学分会（公开指南）",
        "需评估铁蛋白、sCD25、NK 细胞活性等指标",
    ),
    ExternalEvidence(
        ("发热待查", "发热查因", "不明原因发热"),
        "发热待查诊治专家共识", "中华医学会感染病学分会（公开共识）",
        "需系统排查感染、肿瘤与结缔组织病",
    ),
    ExternalEvidence(
        ("围术期", "术前评估", "麻醉评估"),
        "围术期麻醉评估与准备指南", "中华医学会麻醉学分会（公开指南）",
        "术前应完善心肺功能与气道评估",
    ),
)


def external_evidence_for(text: str) -> List[Dict[str, str]]:
    """按关键词匹配外部依据；未命中返回空列表（调用方显示"暂无可靠外部依据"）。"""
    if not text:
        return []
    found: List[Dict[str, str]] = []
    for item in EXTERNAL_WHITELIST:
        if any(k in text for k in item.keywords):
            found.append({
                "title": item.title,
                "source": item.source,
                "note": item.note,
                "version": item.version,
                "identifier": item.identifier,
                "url": item.url,
                "scope": item.scope,
                "excerpt": item.excerpt,
                "reviewed_at": item.reviewed_at,
                "traceable": item.traceable,
                "verification_status": "verified" if item.traceable else "metadata_incomplete",
            })
    return found[:3]


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------

def build_timeline(profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把画像里的症状/疾病/检查整理成时间线（按"多久以前"粗略排序）。

    比赛版不做日期解析：用文本里的时长描述表达相对时间，并标注来源。
    """
    profile = profile or {}
    events: List[Dict[str, Any]] = []

    for item in profile.get("symptoms_timeline") or []:
        events.append({
            "kind": "symptom",
            "title": str(item.get("symptom") or ""),
            "when": str(item.get("onset") or "时间不详"),
            "detail": f"趋势：{item.get('trend')}" if item.get("trend") else "",
            "source_refs": list(item.get("source_refs") or []),
            "confidence": item.get("confidence") or "medium",
        })
    for item in profile.get("diseases") or []:
        events.append({
            "kind": "diagnosis",
            "title": str(item.get("name") or ""),
            "when": str(item.get("duration") or "时长不详"),
            "detail": f"控制情况：{item.get('control')}" if item.get("control") else "",
            "source_refs": list(item.get("source_refs") or []),
            "confidence": item.get("confidence") or "medium",
        })
    for item in profile.get("exams") or []:
        events.append({
            "kind": "exam",
            "title": f"{item.get('item')} {item.get('value') or ''}".strip(),
            "when": "检查结果",
            "detail": "异常" if item.get("abnormal") else "",
            "source_refs": list(item.get("source_refs") or []),
            "confidence": "high",
        })
    for item in profile.get("medications") or []:
        events.append({
            "kind": "medication",
            "title": f"{item.get('name')} {item.get('dose') or ''}".strip(),
            "when": "当前用药",
            "detail": "",
            "source_refs": [],
            "confidence": "high",
        })

    # 有 source_refs 的优先靠前展示（可追溯），其余保持原顺序
    events.sort(key=lambda e: (0 if e["source_refs"] else 1))
    return events


# ---------------------------------------------------------------------------
# 判断 ↔ 证据关联 + 证据卡片
# ---------------------------------------------------------------------------

def _evidence_state(supports: Sequence[Dict[str, Any]],
                    opposes: Sequence[Dict[str, Any]],
                    doctor_confirmed: bool = False) -> str:
    # Human review is tracked separately; it must never upgrade evidence quality.
    if opposes:
        return STATE_CONFLICT
    direct = [s for s in supports if s.get("level") == STATE_DIRECT]
    if direct:
        return STATE_DIRECT
    if supports:
        return STATE_INDIRECT
    return STATE_MISSING


def _dimensions(supports: Sequence[Dict[str, Any]],
                opposes: Sequence[Dict[str, Any]],
                review_status: str = "ai_unreviewed") -> Dict[str, str]:
    direct_count = sum(1 for item in supports if item.get("level") == STATE_DIRECT)
    if direct_count:
        sufficiency = "sufficient" if not opposes else "partial"
    elif supports:
        sufficiency = "partial"
    else:
        sufficiency = "insufficient"
    consistency = "conflict" if opposes else ("consistent" if supports else "unable_to_compare")
    return {
        "evidence_sufficiency": sufficiency,
        "information_consistency": consistency,
        "review_status": review_status,
    }


def build_evidence_cards(profile: Optional[Dict[str, Any]],
                         triage: Optional[Dict[str, Any]],
                         mdt: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """为每条关键判断生成一张证据卡（支持/反对双列 + 外部依据）。

    数据来源：
    - 分诊判断 → `triage.primary/secondary` 的 signals（规则命中信号 = 患者证据）；
    - 专科判断 → `mdt.analyses[].inferences[].evidence_refs`；
    - 外部依据 → 白名单匹配（未命中则如实说明）。
    """
    profile = profile or {}
    cards: List[Dict[str, Any]] = []

    # 画像中可用于"支持"的事实（带来源引用）
    fact_pool: List[Dict[str, Any]] = []
    for item in (profile.get("exams") or []):
        fact_pool.append({"text": f"{item.get('item')} {item.get('value') or ''}".strip(),
                          "source_refs": list(item.get("source_refs") or []), "kind": "exam"})
    for item in (profile.get("diseases") or []):
        fact_pool.append({"text": str(item.get("name") or ""),
                          "source_refs": list(item.get("source_refs") or []), "kind": "disease"})
    for item in (profile.get("symptoms_timeline") or []):
        fact_pool.append({"text": f"{item.get('symptom')} {item.get('onset') or ''}".strip(),
                          "source_refs": list(item.get("source_refs") or []), "kind": "symptom"})

    def _has_common_fragment(a: str, b: str, min_len: int = 2, cap: int = 4) -> bool:
        """两个文本是否存在 ≥min_len 的公共中文片段（用于弱匹配，宁少勿多）。"""
        if not a or not b:
            return False
        for n in range(min(cap, len(a)), min_len - 1, -1):
            for i in range(len(a) - n + 1):
                seg = a[i:i + n]
                if seg.strip() and seg in b:
                    return True
        return False

    def match_facts(text: str, evidence_refs: Optional[Sequence[str]] = None
                    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return verified supports and text-only related candidates separately."""
        requested_refs = {str(ref).strip() for ref in (evidence_refs or []) if str(ref).strip()}
        supports: List[Dict[str, Any]] = []
        related: List[Dict[str, Any]] = []
        for fact in fact_pool:
            exact_refs = requested_refs.intersection(str(ref).strip() for ref in fact["source_refs"])
            if exact_refs:
                supports.append({"text": fact["text"], "source_refs": sorted(exact_refs),
                                 "level": STATE_DIRECT, "citation_valid": True})
            elif _has_common_fragment(str(fact["text"]), text or "", min_len=4, cap=8):
                related.append({"text": fact["text"], "source_refs": fact["source_refs"],
                                "relationship": "related_only"})
        return supports, related

    # ① 分诊判断
    triage = triage or {}
    picks = ([triage.get("primary")] if triage.get("primary") else []) + list(triage.get("secondary") or [])
    for pick in picks:
        reason = str(pick.get("reason") or "；".join(pick.get("signals") or []))
        supports, related = match_facts(reason, pick.get("evidence_refs") or [])
        supports = supports or [
            {"text": s, "source_refs": [], "level": STATE_INDIRECT}
            for s in (pick.get("signals") or [])[:3]
        ]
        card = {
            "scope": "triage",
            "judgement": f"建议就诊方向：{pick.get('department')}（{pick.get('role')}）",
            "reason": reason,
            "supports": supports,
            "related_candidates": related,
            "opposes": [],          # 分诊层暂未收集反向证据 → 显式标注为空
            "external": external_evidence_for(reason),
            "evidence_state": _evidence_state(supports, []),
            "doctor_confirmed": False,
        }
        card.update(_dimensions(supports, []))
        cards.append(card)

    # ② 各专科判断
    mdt = mdt or {}
    for analysis in (mdt.get("analyses") or []):
        if analysis.get("status") != "ok":
            continue
        dept = analysis.get("department")
        for inf in (analysis.get("inferences") or []):
            text = str(inf.get("text") or "")
            refs = list(inf.get("evidence_refs") or [])
            supports, related = match_facts(text, refs)
            invalid_refs = [ref for ref in refs
                            if not any(ref in fact["source_refs"] for fact in fact_pool)]
            supports.extend({"text": ref, "source_refs": [], "level": STATE_INDIRECT,
                             "citation_valid": False} for ref in invalid_refs)
            # 信息缺口本身构成"反对/不足"证据（诚实呈现）
            opposes = [{"text": gap, "reason": "该项信息缺失，暂无法支持该判断"}
                       for gap in (analysis.get("information_gaps") or [])[:2]]
            card = {
                "scope": "specialty",
                "department": dept,
                "judgement": text,
                "reason": f"{dept}的 AI 分析视角（未经医生审核）",
                "supports": supports,
                "related_candidates": related,
                "opposes": opposes,
                "external": external_evidence_for(text),
                "evidence_state": _evidence_state(supports, opposes),
                "doctor_confirmed": False,
            }
            card.update(_dimensions(supports, opposes))
            cards.append(card)

    # ③ 分歧条目也是"判断"（证据状态=冲突）
    for dis in ((mdt.get("aggregation") or {}).get("disagreements") or []):
        positions = dis.get("positions") or {}
        card = {
            "scope": "disagreement",
            "judgement": f"分歧：{dis.get('topic')}",
            "reason": dis.get("reason") or "",
            "supports": [{"text": f"{k}：{v}", "source_refs": [], "level": STATE_INDIRECT}
                         for k, v in positions.items()],
            "opposes": [{"text": "存在相反倾向的专科意见", "reason": "需医生裁决"}],
            "external": external_evidence_for(str(dis.get("topic") or "")),
            "evidence_state": STATE_CONFLICT,
            "resolving_info": dis.get("resolving_info") or "",
            "doctor_confirmed": False,
        }
        card.update(_dimensions(card["supports"], card["opposes"]))
        cards.append(card)

    return cards


def summarize_evidence(cards: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """证据状态统计（供报告"证据关联"段与前端徽章）。"""
    counts = {state: 0 for state in STATE_LABELS}
    for card in cards:
        state = card.get("evidence_state") or STATE_MISSING
        counts[state] = counts.get(state, 0) + 1
    counts["total"] = len(cards)
    return counts
