"""AI 多专科视角分析（自研部件③）与聚合。

对应《产品开发规格书-比赛版v-competition.md》任务 2.7/2.8 与主文档 §8。

两条红线（写死）：
1. **第一轮独立研判**：每个专科只读"同一份结构化画像 + 本科任务"，**互不预读他科结论**（防锚定）；
2. **专科模块是 AI 分析视角，不是真实医生**，不得输出诊断结论、处方或治疗命令。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import SpecialistOpinionCard  # noqa: E402
from services.ai_service import call_structured  # noqa: E402
from services.department_domain import ALL_NAMES, is_department  # noqa: E402
from services.json_recovery import (  # noqa: E402
    STRATEGY_SALVAGE,
    loads_lenient,
    result_is_repaired,
)

SPECIALTY_SYSTEM_PROMPT = (
    "你是一位{dept}的 AI 分析视角（不是真实医生，也不构成会诊意见）。"
    "你只做与本科相关的信息整理与分析提示，不做诊断结论、不开处方、不给治疗方案。"
    "所有判断必须区分「事实」与「推断」，并明确标注缺失信息。输出严格 JSON。"
)

# 各专科关注点（用于提示词，提升相关性）
SPECIALTY_FOCUS: Dict[str, str] = {
    "内分泌科": "血糖/代谢控制水平、并发症筛查（眼底、肾、神经、心血管）",
    "肾内科": "肾功能与蛋白尿评估、肾病分期、容量状态",
    "眼科": "视力变化与眼底病变筛查",
    "心血管内科": "心血管危险因素、缺血/心律失常评估",
    "神经内科": "神经定位体征、卒中危险因素、发作性症状",
    "血液科": "血细胞异常、发热查因中的血液系统病因",
    "风湿免疫科": "自身免疫证据、多系统受累",
    "感染科": "感染源、发热热型与病原学证据",
    "肿瘤科": "占位性质、肿瘤标志物与病理证据",
    "呼吸内科": "呼吸道症状、影像与肺功能",
    "消化内科": "消化道症状、肝功能与内镜证据",
    "普通外科": "手术指征相关的外科评估",
    "心胸外科": "心脏/胸腔结构病变与手术指征",
    "神经外科": "颅内/脊髓病变与手术指征",
    "骨科": "骨骼与关节结构性病变",
    "麻醉科": "围术期麻醉风险评估",
    "影像科": "影像征象与病灶分布",
    "超声科": "超声可及的结构与血流评估",
}

METHOD_LLM = "llm"
METHOD_LLM_REPAIRED = "llm_repaired"   # 输出有格式缺陷或被截断，已按已写出字段恢复
METHOD_SKIPPED = "skipped"
METHOD_FAILED = "failed"
SOURCE_AI_REFERENCE = "ai_reference"
SOURCE_AI_REFERENCE_LABEL = "专科 AI 参考意见"

# 输出预算：实测 deepseek-chat 的血液科意见卡需要 ~2335 token，而默认的 2000 会把
# JSON 截断（finish_reason=length）→ 非法 JSON → 整个科室被丢弃。这里给足余量。
MDT_MAX_TOKENS = 4000
# 被截断时追加的"更精炼"要求（重试一次，仍是同一个科室、同一份资料）
TERSE_NOTE = (
    "\n\n【重要】上一次输出因为太长被截断了。这次请务必精炼："
    "facts 最多 5 条、inferences 最多 3 条、checks 最多 4 条、information_gaps 最多 4 条，"
    "每条不超过 40 个字；referral 只写科室名。只输出 JSON，不要任何额外文字。"
)

# 分歧检测 v0：**方向性词组配对**（宁少勿多，只做方向比对，不做语义判断）。
# 每项为 (正向词组, 反向词组)：一个科室落正向、另一科室落反向 → 记为分歧。
OPPOSITE_PAIRS = (
    (("建议手术", "手术指征", "手术评估", "手术治疗", "尽快手术"),
     ("保守治疗", "暂不手术", "继续观察", "保守观察", "暂缓手术")),
    (("建议住院", "需要住院", "收住院"),
     ("门诊随访", "门诊观察", "居家观察")),
    (("立即", "尽快", "急诊"), ("暂缓", "择期", "不急")),
    (("考虑恶性", "需排除恶性", "恶性可能"), ("考虑良性", "良性可能大")),
    (("支持感染", "感染可能性大"), ("不支持感染", "感染依据不足")),
    (("建议增加剂量", "加量"), ("建议减少剂量", "减量", "不建议加量")),
    (("必须检查", "应尽快检查"), ("暂不必检查", "无需检查", "可不做")),
    (("建议复查", "需要复查"), ("无需复查", "不必复查")),
)

# 检查/信息主题的规范化表：把 LLM 生成的自由措辞归一到统一主题，才能聚合出"共识"。
# 只做关键词归类（确定性、可解释），宁少勿多。
TOPIC_KEYWORDS: Dict[str, tuple] = {
    "眼底检查": ("眼底", "视网膜", "散瞳", "检眼镜"),
    "肾功能评估": ("肾功能", "肌酐", "egfr", "尿素氮", "肾小球滤过"),
    "尿蛋白定量": ("尿蛋白定量", "24小时尿蛋白", "尿蛋白/肌酐", "尿白蛋白"),
    "血糖评估": ("糖化血红蛋白", "血糖谱", "糖化", "空腹血糖", "餐后血糖"),
    "心脏评估": ("心电图", "心脏超声", "心超", "bnp", "冠脉", "心脏彩超"),
    "血常规": ("血常规", "白细胞", "血红蛋白", "血小板", "血细胞"),
    "影像检查": ("ct", "mri", "核磁", "影像学", "胸片"),
    "肝功能评估": ("肝功能", "转氨酶", "胆红素"),
    "甲状腺功能": ("甲功", "tsh", "t3", "t4", "甲状腺功能"),
    "电解质与容量": ("电解质", "容量", "钠", "钾", "出入量"),
    "用药核对": ("用药清单", "用药核对", "药物相互", "合并用药"),
    "过敏史": ("过敏",),
    "血压评估": ("血压监测", "血压控制", "动态血压"),
    "神经系统评估": ("神经系统", "肌力", "神经传导", "头颅"),
}


def canonical_topic(text: str) -> Optional[str]:
    """把一段自然语言建议/缺口归一到一个规范主题；无法归类返回 None。"""
    lowered = (text or "").lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw in lowered:
                return topic
    return None


@dataclass
class SpecialtyAnalysis:
    """一次专科分析的产出。"""

    department: str
    card: Optional[SpecialistOpinionCard] = None
    method: str = METHOD_SKIPPED
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        if self.card is None:
            return {"department": self.department, "status": "unavailable",
                    "method": self.method, "error": self.error,
                    "source_type": SOURCE_AI_REFERENCE,
                    "source_label": SOURCE_AI_REFERENCE_LABEL}
        c = self.card
        return {
            "department": c.specialty_name,
            "focus": c.focus,
            "facts": list(c.facts),
            "inferences": [{"text": i.text, "confidence": i.confidence,
                            "evidence_refs": list(i.evidence_refs)} for i in c.inferences],
            "checks": [{"priority": k.priority, "content": k.content} for k in c.checks],
            "information_gaps": list(c.information_gaps),
            "referral": c.referral,
            "status": "ok",
            "method": self.method,
            "source_type": SOURCE_AI_REFERENCE,
            "source_label": SOURCE_AI_REFERENCE_LABEL,
            # 卡片可用但有需要如实说明的情况（如"输出被截断，已按已写出字段恢复"）
            "notice": self.error,
        }


def _build_specialty_prompt(dept: str, text: str, focus: str,
                            profile: Optional[Dict[str, Any]],
                            questions: Optional[Dict[str, str]] = None) -> str:
    focus_hint = SPECIALTY_FOCUS.get(dept, "")
    gaps = (profile or {}).get("information_gaps") or []
    answered = ""
    if questions:
        answered = "【患者对追问的回答】\n" + "\n".join(
            f"- {k}：{v}" for k, v in questions.items()) + "\n"
    return f"""【病例资料（已脱敏）】
{text[:3000]}

【患者主要问题】{(profile or {}).get('chief_complaint') or '未提供'}
【已知信息缺口】{'、'.join(gaps[:6]) or '未列出'}
{answered}
【你作为 {dept} 的分析视角】
{f'本专科关注点：{focus_hint}' if focus_hint else ''}
{focus}

【输出要求】只输出 JSON：
{{
  "focus": "本科关注点（一句话）",
  "facts": ["仅来自病例资料、与本科相关的客观事实"],
  "inferences": [{{"text": "分析判断（区分于事实）", "confidence": "high|medium|low", "evidence_refs": ["引用的事实或检查名"]}}],
  "checks": [{{"priority": "high|medium|low", "content": "建议向医生确认是否需要做的检查"}}],
  "information_gaps": ["缺失且影响本科判断的信息"],
  "referral": "若超出本科范围，写出应转给哪个科；否则填 null"
}}

【篇幅要求（重要）】只输出 JSON 本身，不要任何解释、不要用 ``` 包裹；
facts ≤ 6 条、inferences ≤ 4 条、checks ≤ 5 条、information_gaps ≤ 6 条，每条 ≤ 50 字。

【硬性禁止】
- 不得给出诊断结论（可写"需排查…"，不得写"确诊…"）；
- 不得开药、不得给剂量、不得给治疗方案；
- 不得凭空编造病例中没有的检查或病史；缺什么就写进 information_gaps；
- checks 一律写成"建议向医生确认是否需要…"，不得写成医嘱。
"""


def _parse_card(dept: str, raw: str) -> Tuple[SpecialistOpinionCard, str]:
    """解析专科意见卡，返回 `(卡片, 解析策略)`。

    解析交给 `services.json_recovery`：模型输出被截断、混用全角标点、内部引号没转义、
    尾逗号等情况都能恢复，且**只取已经写完的内容**（不补半句话）。
    """
    data, strategy = loads_lenient(
        raw, fields=("focus", "facts", "inferences", "checks", "information_gaps",
                     "referral"))
    if data is None:
        if raw.find("{") < 0:
            raise ValueError("输出中未找到 JSON")
        raise ValueError("JSON 无法解析（结尾可能被截断）")
    if not isinstance(data, dict):
        raise ValueError("JSON 根节点不是对象")

    from schemas import CheckItem, Inference

    inferences = []
    for item in (data.get("inferences") or [])[:6]:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            conf = str(item.get("confidence") or "medium").lower()
            inferences.append(Inference(
                text=str(item["text"]).strip(),
                confidence=conf if conf in ("high", "medium", "low") else "medium",
                evidence_refs=[str(x) for x in (item.get("evidence_refs") or [])],
            ))

    checks = []
    for item in (data.get("checks") or [])[:6]:
        if isinstance(item, dict) and str(item.get("content") or "").strip():
            pri = str(item.get("priority") or "medium").lower()
            content = str(item["content"]).strip()
            # 强制"确认式"表述（红线：不得写成医嘱）
            if not content.startswith(("建议", "向医生确认", "请医生确认")):
                content = f"建议向医生确认是否需要：{content}"
            checks.append(CheckItem(
                priority=pri if pri in ("high", "medium", "low") else "medium",
                content=content,
            ))

    referral = data.get("referral")
    referral = str(referral).strip() if referral and str(referral).strip().lower() != "null" else None
    if referral and not is_department(referral):
        referral = None  # 转诊方向不在域内则留空（不编造科室）

    card = SpecialistOpinionCard(
        case_id="", specialty_code=dept, specialty_name=dept,
        focus=str(data.get("focus") or "").strip(),
        facts=[str(x) for x in (data.get("facts") or [])][:8],
        inferences=inferences,
        checks=checks,
        information_gaps=[str(x) for x in (data.get("information_gaps") or [])][:8],
        referral=referral,
    )
    if strategy == STRATEGY_SALVAGE and not (card.focus or card.facts or card.inferences
                                             or card.checks or card.information_gaps):
        # 抢救出来的东西一个字段都没有（例如只剩 "facts": []）→ 视为失败，不塞空卡
        raise ValueError("输出中没有可用的意见卡字段")
    return card, strategy


def _parse_or_raise(dept: str, raw: str, truncated: bool) -> Tuple[SpecialistOpinionCard, str]:
    card, strategy = _parse_card(dept, raw)
    if truncated:
        return card, "模型输出被长度截断，已按已写出的字段恢复"
    if result_is_repaired(strategy):
        return card, "模型输出格式异常，已按可读字段恢复"
    return card, ""


def analyze_specialty(
    *,
    ai_service: Any,
    department: str,
    text: str,
    profile: Optional[Dict[str, Any]] = None,
    case_id: str = "",
    answers: Optional[Dict[str, str]] = None,
    focus: str = "",
) -> SpecialtyAnalysis:
    """对单个专科做独立研判（**不传入其他科室的结论**，防锚定）。

    输出异常时的处理顺序：**直接解析 → 追加"更精炼"要求重试一次 → 容错恢复**。
    只有彻底拿不到内容才判失败，且失败原因如实说明（截断 / 格式 / 调用）。
    """
    if not is_department(department):
        return SpecialtyAnalysis(department=department, method=METHOD_FAILED,
                                 error=f"「{department}」不在科室域内")
    if ai_service is None or not getattr(ai_service, "api_key", ""):
        return SpecialtyAnalysis(department=department, method=METHOD_SKIPPED,
                                 error="未配置模型，无法生成专科意见卡")

    prompt = _build_specialty_prompt(department, text, focus, profile, answers)
    system = SPECIALTY_SYSTEM_PROMPT.format(dept=department)
    content, truncated, error = call_structured(ai_service, prompt, system, MDT_MAX_TOKENS)

    if not content:
        detail = f"模型调用异常：{error}" if error else "模型未返回内容"
        return SpecialtyAnalysis(department=department, method=METHOD_FAILED, error=detail)

    notice = ""
    card: Optional[SpecialistOpinionCard] = None
    last_error: Optional[Exception] = None

    def attempt(text: str, was_truncated: bool) -> bool:
        """解析一次；成功则写入 card/notice，失败记录原因。"""
        nonlocal card, notice, last_error
        try:
            card, notice = _parse_or_raise(department, text, was_truncated)
            return True
        except Exception as exc:                   # noqa: BLE001 - 按内容缺陷分类处理
            last_error, card = exc, None
            return False

    if not (attempt(content, truncated) and not truncated):
        # 被截断，或第一次就没解析出来 → 追加"更精炼"要求重试一次（预算不变，靠少写换完整）
        retry_content, retry_truncated, _ = call_structured(
            ai_service, prompt + TERSE_NOTE, system, MDT_MAX_TOKENS)
        if retry_content:
            attempt(retry_content, retry_truncated)
        if card is None and content:
            attempt(content, truncated)            # 重试也没救回来 → 回到第一次输出做容错恢复

    if card is None:
        hint = "（输出被长度截断）" if truncated else ""
        return SpecialtyAnalysis(department=department, method=METHOD_FAILED,
                                 error=f"意见卡解析失败{hint}：模型输出无法解析")

    card.case_id = case_id
    try:
        # `analyze_specialty` 也被离线评测单独调用，评测场景可能没有病例号；
        # 运行时有 case_id 时仍执行完整 schema 校验。无病例号时只跳过身份字段，
        # 内容与嵌套条目的校验不能跳过，避免 `{}` 被当成成功意见卡。
        if not (card.focus or card.facts or card.inferences or card.checks
                or card.information_gaps):
            raise ValueError("专科意见卡不能为空")
        for inference in card.inferences:
            inference.validate()
        for check in card.checks:
            check.validate()
        if case_id:
            card.validate()
    except Exception as exc:
        return SpecialtyAnalysis(department=department, method=METHOD_FAILED,
                                 error="意见卡校验失败：模型输出未通过校验")
    method = METHOD_LLM_REPAIRED if notice else METHOD_LLM
    return SpecialtyAnalysis(department=department, card=card, method=method, error=notice)


# ---------------------------------------------------------------------------
# 聚合与分歧检测
# ---------------------------------------------------------------------------

@dataclass
class Aggregation:
    """共识 / 分歧 / 不确定性 / 待确认问题。"""

    consensus: List[Dict[str, Any]] = field(default_factory=list)
    disagreements: List[Dict[str, Any]] = field(default_factory=list)
    uncertainties: List[Dict[str, Any]] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "consensus": list(self.consensus),
            "disagreements": list(self.disagreements),
            "uncertainties": list(self.uncertainties),
            "open_questions": list(self.open_questions),
            "notes": list(self.notes),
        }


def _text_of(card: SpecialistOpinionCard) -> str:
    parts = list(card.facts) + [i.text for i in card.inferences] + \
            [c.content for c in card.checks] + list(card.information_gaps)
    return "；".join(parts)


def detect_disagreements(cards: Sequence[SpecialistOpinionCard]) -> List[Dict[str, Any]]:
    """分歧检测 v0：**只对方向性词组做比对**，宁少勿多（规格书 §6.2 / 任务 2.8）。

    不做语义级判断；同方向表述不算分歧；同一对科室同一主题只记一条。
    """
    found: List[Dict[str, Any]] = []
    by_dept = {c.specialty_name: _text_of(c) for c in cards}

    for i, (dept_a, text_a) in enumerate(by_dept.items()):
        for dept_b, text_b in list(by_dept.items())[i + 1:]:
            for positives, negatives in OPPOSITE_PAIRS:
                pos_a = next((w for w in positives if w in text_a), None)
                neg_a = next((w for w in negatives if w in text_a), None)
                pos_b = next((w for w in positives if w in text_b), None)
                neg_b = next((w for w in negatives if w in text_b), None)

                opposite = (pos_a and neg_b) or (neg_a and pos_b)
                # 同一科室内部矛盾不算"科室间分歧"，只在两科方向相反时记录
                if not opposite or (pos_a and neg_a) or (pos_b and neg_b):
                    continue

                found.append({
                    "topic": f"{positives[0]} vs {negatives[0]}",
                    "positions": {
                        dept_a: pos_a or neg_a,
                        dept_b: pos_b or neg_b,
                    },
                    "reason": "两个专科在同一方向上给出了相反倾向",
                    "resolving_info": "需要补充更明确的检查依据后再判断",
                    "evidence_state": "conflict",
                })
                break
    return found


def aggregate(cards: Sequence[SpecialistOpinionCard],
              rejections: Optional[Sequence[str]] = None) -> Aggregation:
    """把各科独立意见卡聚合成共识 / 分歧 / 不确定性 / 待确认问题。

    说明（诚实原则）：
    - "共识"只统计**多个科室都提到**的关键检查/信息缺口，不做语义推断；
    - 分歧来自 `detect_disagreements`（宁少勿多）；
    - 信息缺口与"仅单科提到"的内容一律进 `uncertainties`，不硬凑成共识。
    """
    agg = Aggregation()
    if not cards:
        agg.notes.append("没有可用的专科意见卡（模型不可用或全部失败）")
        return agg

    # ---- 共识：≥2 个科室指向同一"规范主题"（检查建议或信息缺口合并计算）----
    # 用 canonical_topic 归一措辞，避免"眼底照相"与"检眼镜检查"被当成两件事。
    topic_supporters: Dict[str, List[str]] = {}
    topic_samples: Dict[str, str] = {}
    topic_sources: Dict[str, set] = {}
    unclassified_checks: List[tuple] = []

    def _record(topic: str, dept: str, source: str, sample: str = "") -> None:
        supporters = topic_supporters.setdefault(topic, [])
        if dept not in supporters:
            supporters.append(dept)
        topic_sources.setdefault(topic, set()).add(source)
        if sample and topic not in topic_samples:
            topic_samples[topic] = sample

    for card in cards:
        for check in card.checks:
            topic = canonical_topic(check.content)
            if topic is None:
                unclassified_checks.append((card.specialty_name, check))
                continue
            _record(topic, card.specialty_name, "check", check.content)
        for gap in card.information_gaps:
            topic = canonical_topic(gap)
            if topic is None:
                continue
            _record(topic, card.specialty_name, "gap", gap)

    for topic, depts in topic_supporters.items():
        if len(depts) >= 2:
            entry = {"topic": topic, "supporters": depts,
                     "sources": sorted(topic_sources.get(topic, set()))}
            if topic_samples.get(topic):
                entry["example"] = topic_samples[topic]
            agg.consensus.append(entry)

    # ---- 分歧 ----
    agg.disagreements = detect_disagreements(cards)

    # ---- 不确定性：仅单科提到、未能形成共识的内容 + 低置信推断 ----
    for topic, depts in topic_supporters.items():
        if len(depts) < 2:
            agg.uncertainties.append({"topic": topic, "impact": depts})
    for dept, check in unclassified_checks:
        agg.uncertainties.append({"topic": check.content, "impact": [dept]})
    for card in cards:
        for inf in card.inferences:
            if inf.confidence == "low":
                agg.uncertainties.append({
                    "topic": inf.text, "impact": [f"{card.specialty_name}（低置信推断）"]})

    # ---- 待医生确认的问题清单 ----
    for card in cards:
        for check in card.checks:
            if check.priority == "high" and check.content not in agg.open_questions:
                agg.open_questions.append(check.content)
    for item in agg.disagreements:
        topic = f"分歧待裁决：{item['topic']}"
        if topic not in agg.open_questions:
            agg.open_questions.append(topic)
    if rejections:
        agg.notes.append("模型曾反驳以下科室（强规则命中已保留，进入分歧区）：" + "、".join(rejections))

    agg.notes.append("以上为 AI 多学科分析视角的聚合结果，不是真实会诊意见；分歧如实呈现并需医生复核。")
    return agg
