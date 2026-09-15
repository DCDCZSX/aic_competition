"""病例画像抽取器（自研部件①）。

对应《产品开发规格书-比赛版v-competition.md》任务 1.3 与主文档 §5.4：
自由文本 → 结构化 `PatientProfile`，**只抽取文本中实际存在的信息**，每条带 `source_refs`。

设计：
- **规则优先**：年龄/性别/主诉/症状时长/已知疾病/用药/检查异常 用确定性规则抽取（可解释、可离线测试）；
- **LLM 可选**：提供 `use_llm=True` 时调用模型补全语义字段，失败或违约自动降级回规则结果；
- **字段校验器**：产出后用 `schemas.PatientProfile.validate()` 校验，不合法即标记；
- **门禁 G4**：`direct_quote_coverage < 0.60` → 阻断进入分析（规格书 §4.4）。

红线：**AI 推断不得直接写成"已确认事实"**；规则/模型没抽到的字段一律进"信息缺口"，不得用默认值伪造（主文档 §5.5）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (  # noqa: E402
    Disease,
    ExamResult,
    Medication,
    PatientProfile,
    SchemaError,
    SymptomEvent,
)
from services.ai_service import call_structured  # noqa: E402
from services.json_recovery import loads_lenient, result_is_repaired  # noqa: E402

# 画像增强的输出很短（主诉 + 缺口），2000 token 足够
PORTRAIT_MAX_TOKENS = 2000

# 常见慢性病（用于既往史抽取）
COMMON_DISEASES: Tuple[str, ...] = (
    "2型糖尿病", "1型糖尿病", "糖尿病", "高血压", "冠心病", "心房颤动", "心力衰竭",
    "慢阻肺", "慢性阻塞性肺疾病", "哮喘", "脑梗死", "脑出血", "慢性肾病", "肾病综合征",
    "肝硬化", "脂肪肝", "甲状腺功能减退", "甲状腺功能亢进", "贫血", "类风湿关节炎",
    "系统性红斑狼疮", "肿瘤", "肺结核", "乙型肝炎", "丙型肝炎", "高脂血症", "痛风",
)

# 关键检查项（异常抽取与缺口识别）
KEY_EXAMS: Tuple[str, ...] = (
    "尿蛋白", "血糖", "空腹血糖", "糖化血红蛋白", "肌酐", "血红蛋白", "白细胞",
    "血小板", "血沉", "CRP", "C反应蛋白", "肝功能", "白蛋白", "血脂", "血压",
    "心电图", "心脏彩超", "冠脉", "CT", "MRI", "核磁", "超声", "眼底",
)

# 时间单位
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(年|个月|月|周|天|小时|余年|余月)")
_AGE_RE = re.compile(r"(?:年龄)?\s*(\d{1,3})\s*岁")
_SEX_RE = re.compile(r"(男性|女性|男|女)")
_CHIEF_RE = re.compile(r"主诉\s*[:：]\s*([^\n。；;]{2,60})")
_SYMPTOM_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,8}?)(?:约|近|有)?\s*(\d+(?:\.\d+)?\s*(?:年|个月|月|周|天|小时))"
)
_MED_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9\-]{1,14}?)"
    r"\s*(\d+(?:\.\d+)?\s*(?:mg|g|片|粒|ml|IU|单位))"
    r"(?:\s*(bid|tid|qd|qod|每日\d次|每天\d次|每日一次|每日两次))?"
)
_EXAM_ABNORMAL_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,10})\s*(?:\+\+|--|阳性|偏高|升高|降低|异常|\+\b)"
)


# 缺口检测对照表：(文本关键词, 依赖矩阵字段 key)
# 缺口文案由矩阵标准字段名生成，保证与追问/增量更新使用同一套字段名。
_GAP_CHECKS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("血糖", "空腹血糖", "糖化"), "blood_glucose_control"),
    (("尿蛋白", "尿检", "肌酐", "肾功能"), "urine_protein"),
    (("眼底", "视网膜"), "fundus_exam"),
    (("心电图", "心脏彩超", "冠脉", "心超"), "cardiac_exam"),
    (("血常规", "白细胞", "血红蛋白", "血小板"), "blood_count"),
    (("CT", "MRI", "核磁", "影像"), "imaging"),
    (("用药", "口服", "mg", "片"), "medication_list"),
    (("过敏",), "allergy"),
    (("体重",), "weight_change"),
)


def _source_ref(start: int) -> str:
    """原文位置引用：S-1:<起始偏移>（供前端 hover 显示原文）。"""
    return f"S-1:{start}"


@dataclass
class ExtractionResult:
    """一次抽取的产出。"""

    profile: PatientProfile
    method: str = "rule"                      # rule | llm | hybrid
    warnings: List[str] = field(default_factory=list)
    schema_error: str = ""                    # 校验失败信息（非空表示不合法）

    @property
    def direct_quote_coverage(self) -> float:
        return self.profile.direct_quote_coverage

    @property
    def passes_gate(self) -> bool:
        """G4 门禁：原文直引覆盖率是否达标。"""
        return self.profile.passes_gate

    @property
    def gaps(self) -> List[str]:
        return list(self.profile.information_gaps)


# ---------------------------------------------------------------------------
# 规则抽取
# ---------------------------------------------------------------------------

def _extract_age(text: str) -> Tuple[Optional[int], List[str]]:
    m = _AGE_RE.search(text)
    if not m:
        return None, []
    return int(m.group(1)), [_source_ref(m.start())]


def _extract_sex(text: str) -> Tuple[str, List[str]]:
    m = _SEX_RE.search(text)
    if not m:
        return "", []
    raw = m.group(1)
    sex = "男" if raw in ("男", "男性") else "女"
    return sex, [_source_ref(m.start())]


def _extract_chief_complaint(text: str) -> Tuple[str, List[str]]:
    m = _CHIEF_RE.search(text)
    if m:
        return m.group(1).strip(), [_source_ref(m.start(1))]
    # 退化：取第一句作为主诉候选
    first = re.split(r"[。\n；;]", text.strip())[0].strip()
    if 2 <= len(first) <= 60:
        return first, [_source_ref(0)]
    return "", []


def _extract_symptoms(text: str) -> List[SymptomEvent]:
    events: List[SymptomEvent] = []
    seen: set = set()
    for m in _SYMPTOM_RE.finditer(text):
        symptom = m.group(1).strip()
        onset = m.group(2).strip()
        if len(symptom) < 2 or symptom in seen:
            continue
        # 过滤明显不是症状的词：文书词、疾病名（"2型糖尿病5年"里的"型糖尿病"）
        if any(token in symptom for token in ("患者", "入院", "出院", "诊断", "治疗", "病史")):
            continue
        if any(d in symptom for d in ("糖尿病", "高血压", "冠心病", "房颤", "心衰",
                                      "慢阻肺", "肝硬化", "贫血", "肿瘤", "癌")):
            continue
        seen.add(symptom)
        events.append(SymptomEvent(
            symptom=symptom, onset=onset, trend="", confidence="high",
            source_refs=[_source_ref(m.start(1))],
        ))
    return events


def _extract_diseases(text: str) -> List[Disease]:
    diseases: List[Disease] = []
    seen: set = set()
    for name in COMMON_DISEASES:
        idx = text.find(name)
        if idx < 0 or name in seen:
            continue
        # 避免"糖尿病"覆盖"2型糖尿病"
        if any(name in other and name != other and other in text for other in COMMON_DISEASES):
            if name == "糖尿病" and "2型糖尿病" in text:
                continue
        seen.add(name)
        # 尝试在同句里找病程
        window = text[idx: idx + 40]
        dm = _DURATION_RE.search(window)
        duration = dm.group(0).strip() if dm else ""
        diseases.append(Disease(
            name=name, duration=duration, control="", confidence="high",
            source_refs=[_source_ref(idx)],
        ))
    return diseases


def _extract_medications(text: str) -> List[Medication]:
    meds: List[Medication] = []
    seen: set = set()
    for m in _MED_RE.finditer(text):
        name = m.group(1).strip()
        dose = m.group(2).strip()
        if name in seen or len(name) < 2:
            continue
        seen.add(name)
        meds.append(Medication(name=name, dose=dose))
    return meds


def _extract_exams(text: str) -> List[ExamResult]:
    exams: List[ExamResult] = []
    seen: set = set()
    for m in _EXAM_ABNORMAL_RE.finditer(text):
        item = m.group(1).strip()
        if item in seen:
            continue
        seen.add(item)
        exams.append(ExamResult(
            item=item, value=m.group(0).strip(), abnormal=True,
            source_refs=[_source_ref(m.start())],
        ))
    return exams


# 表示"没做/未提供"的否定表述（注意：不含"未见"，因为"未见异常"表示做了检查且结果正常）
_NEGATION_OPS: Tuple[str, ...] = (
    "未做", "未查", "未行", "未提供", "未完善", "未检查", "未复查",
    "没做", "没查", "没有做", "缺乏", "缺如", "不详",
)


def _mentioned_positively(text: str, keywords: Tuple[str, ...]) -> bool:
    """关键词是否以"肯定"方式出现。

    "未做眼底检查" → 视为**未提供**（进缺口）；
    "眼底检查未见异常" → 视为**已提供**（阴性结果也是资料）。
    """
    for kw in keywords:
        start = 0
        while True:
            idx = text.find(kw, start)
            if idx < 0:
                break
            window = text[max(0, idx - 8): idx]
            if not any(op in window for op in _NEGATION_OPS):
                return True
            start = idx + len(kw)
    return False


def _detect_gaps(text: str) -> List[str]:
    """识别信息缺口。

    缺口文案**由依赖矩阵的标准字段名生成**（如"眼底检查：信息不足"），
    而不是自由文本 —— 这样 `dependency_matrix.BY_LABEL` 才能稳定匹配，
    追问与增量更新才不会因为"带括号的字段名"而漏配。
    """
    from services.dependency_matrix import depends_on  # 局部导入避免循环依赖

    gaps: List[str] = []
    for keywords, field in _GAP_CHECKS:
        if _mentioned_positively(text, keywords):
            continue
        dep = depends_on(field)
        label = dep.label if dep else field
        gaps.append(f"{label}：信息不足")
    return gaps


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def extract(
    text: str,
    *,
    case_id: str = "C001",
    main_issue: str = "",
    use_llm: bool = False,
    ai_service: Any = None,
) -> ExtractionResult:
    """从病历文本抽取结构化画像。

    `use_llm=True` 时需要传入 `ai_service`（带 `call_ai_api`）；任何失败都会降级为规则结果，
    绝不因为模型问题产出空画像或编造字段。
    """
    raw = (text or "").strip()
    warnings: List[str] = []

    age, age_refs = _extract_age(raw)
    sex, sex_refs = _extract_sex(raw)
    chief, chief_refs = _extract_chief_complaint(raw)
    symptoms = _extract_symptoms(raw)
    diseases = _extract_diseases(raw)
    meds = _extract_medications(raw)
    exams = _extract_exams(raw)
    gaps = _detect_gaps(raw)

    method = "rule"

    # ---- LLM 增强（可选，失败即降级）----
    if use_llm and ai_service is not None:
        try:
            enhanced, note = _extract_with_llm(ai_service, raw, main_issue)
            if note:
                warnings.append(note)
            if enhanced:
                if enhanced.get("chief_complaint") and not chief:
                    chief = str(enhanced["chief_complaint"])
                    chief_refs = [_source_ref(0)]
                for gap in enhanced.get("information_gaps", []) or []:
                    if gap not in gaps:
                        gaps.append(str(gap))
                method = "hybrid"
        except Exception as exc:  # pragma: no cover - 取决于外部服务
            warnings.append(f"模型增强失败，已回退为规则抽取：{exc}")

    profile = PatientProfile(
        case_id=case_id,
        behavior_type="",
        age=age,
        sex=sex,
        chief_complaint=chief,
        diseases=diseases,
        symptoms_timeline=symptoms,
        exams=exams,
        medications=meds,
        doctor_opinions=[],
        information_gaps=gaps,
    )

    # ---- 计算关键字段完整度与原文直引覆盖率 ----
    key_slots = [
        bool(chief),
        bool(symptoms),
        bool(exams),
        bool(meds),
        bool(main_issue.strip() or chief),
    ]
    profile.completeness = round(sum(key_slots) / len(key_slots), 2)
    profile.direct_quote_coverage = round(_coverage(profile, raw), 2)

    # ---- 字段校验器 ----
    schema_error = ""
    try:
        profile.validate()
    except SchemaError as exc:
        schema_error = str(exc)
        warnings.append(f"画像字段校验失败：{exc}")

    # ---- G4 门禁 ----
    if not profile.passes_gate:
        warnings.append(
            f"原文直引覆盖率 {profile.direct_quote_coverage:.2f} 低于门槛 "
            f"{PatientProfile.MIN_DIRECT_QUOTE_COVERAGE}：请补充或修正后再开始分析（G4）"
        )

    return ExtractionResult(profile=profile, method=method,
                            warnings=warnings, schema_error=schema_error)


def _coverage(profile: PatientProfile, raw: str) -> float:
    """原文直引覆盖率 = 带可验证 source_ref 的条目数 / 已抽取条目总数。"""
    items = (list(profile.diseases) + list(profile.symptoms_timeline)
             + list(profile.exams))
    if not items:
        return 0.0
    ok = 0
    for item in items:
        refs = getattr(item, "source_refs", []) or []
        if not refs:
            continue
        # 引用必须指向文本内的真实位置
        valid = False
        for ref in refs:
            try:
                offset = int(str(ref).split(":")[-1])
            except (TypeError, ValueError):
                continue
            if 0 <= offset < max(len(raw), 1):
                valid = True
                break
        if valid:
            ok += 1
    return ok / len(items)


def _extract_with_llm(ai_service: Any, text: str,
                      main_issue: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """调用模型做补充抽取。

    返回 `(抽取结果, 警告文案)`：失败时结果为空、**警告非空**，由调用方如实写进
    warnings（不静默降级）；结果可用但经过格式修复时也会给出说明。
    """
    prompt = (
        "你是病历结构化助手。请只提取病历中【明确写出】的信息，不要推断、不要补全。\n"
        "以 JSON 返回，字段：chief_complaint(string)、information_gaps(string[])。\n"
        f"病历：\n{text[:4000]}\n主要问题：{main_issue}\n"
    )
    # 结构化输出走 call_structured：不做 Markdown 清理（否则 ```json 包裹会被整段删掉），
    # 并且能识别"输出被长度截断"，交给调用方按降级处理。
    content, truncated, error = call_structured(
        ai_service, prompt, role_description="病历结构化助手",
        max_tokens=PORTRAIT_MAX_TOKENS)
    if not content:
        return None, f"模型增强失败，已回退为规则抽取：{error or '模型未返回内容'}"
    if truncated:
        return None, "模型增强失败，已回退为规则抽取：模型输出被长度截断"
    data, strategy = loads_lenient(content,
                                   fields=("chief_complaint", "information_gaps"))
    if data is None:
        return None, "模型增强失败，已回退为规则抽取：模型输出无法解析"
    if result_is_repaired(strategy):
        return data, "模型增强输出格式异常，已按可读字段恢复"
    return data, ""
