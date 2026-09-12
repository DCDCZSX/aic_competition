"""字段-专科依赖矩阵（规格书 §6.1，主文档 §13 落地）。

一张手写、规则驱动的映射表，供三处复用：
1. **追问高价值判定**：缺失字段 ∈ 本矩阵 且影响 ≥1 个专科判断 → 值得问（规格书 §6.1）；
2. **增量更新影响范围**：新资料到达 → 查矩阵 → 只重跑受影响模块（避免全量重跑）；
3. **what-if / 信息影响分析**：补某字段会改变哪些判断。

字段 key 与 `schemas.PatientProfile` 及画像抽取器的输出保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FieldDependency:
    """一个画像字段对哪些专科判断有影响。"""

    field: str                      # 字段 key（英文，内部用）
    label: str                      # 中文名（追问文案用）
    departments: Tuple[str, ...]    # 依赖它的专科（标准名，见 department_domain）
    affects_triage: bool = False    # 是否影响分诊方向/候选科室
    question: str = ""              # 追问模板（面向患者的口语问题）
    options: Tuple[str, ...] = ()   # 快捷回答选项（可选）


# ---------------------------------------------------------------------------
# 矩阵本体（30 条核心映射）
# ---------------------------------------------------------------------------

MATRIX: Tuple[FieldDependency, ...] = (
    # ---- 分诊直接相关 ----
    FieldDependency("symptom_duration", "症状持续时间",
                    ("急诊科", "神经内科", "感染科", "血液科"), True,
                    "这些不舒服大概持续多久了？", ("几天", "1-2 周", "1 个月以上", "说不清")),
    FieldDependency("pain_character", "疼痛部位与性质",
                    ("急诊科", "普通外科", "心血管内科", "消化内科"), True,
                    "疼在什么位置？是胀痛、刺痛还是绞痛？", ("能说清", "说不清")),
    FieldDependency("consciousness", "意识状态",
                    ("急诊科", "神经内科", "重症医学科"), True,
                    "有没有出现过意识模糊、叫不醒或突然说不清话？", ("没有", "有过", "不确定")),
    FieldDependency("neuro_signs", "神经系统体征（肌力/言语）",
                    ("神经内科", "神经外科", "急诊科"), True,
                    "有没有手脚无力、嘴歪、走路不稳的情况？", ("没有", "有过", "不确定")),
    FieldDependency("surgery_terms", "是否涉及手术或介入",
                    ("普通外科", "心胸外科", "骨科", "麻醉科", "影像科"), True,
                    "医生有没有提到要做手术或介入治疗（比如放支架、做消融）？", ("提到了", "没有", "不清楚")),
    FieldDependency("pregnancy", "妊娠/哺乳状态",
                    ("妇产科", "麻醉科", "影像科"), True,
                    "目前是否怀孕或在哺乳期？", ("是", "否", "不适用")),
    FieldDependency("age", "年龄",
                    ("老年医学科", "儿科", "新生儿科", "麻醉科"), True),

    # ---- 专科判断依赖 ----
    FieldDependency("blood_glucose_control", "血糖控制水平",
                    ("内分泌科", "眼科", "肾内科"), True,
                    "最近血糖大概控制在什么范围？有没有测过空腹和餐后？",
                    ("基本正常", "偏高", "没测过", "不清楚")),
    FieldDependency("urine_protein", "尿蛋白 / 肾功能",
                    ("肾内科", "内分泌科", "心血管内科"), True,
                    "最近有没有查过尿蛋白或肾功能（肌酐）？结果是什么？", ("查过（有结果）", "没查过", "不清楚")),
    FieldDependency("fundus_exam", "眼底检查",
                    ("眼科", "内分泌科"), False,
                    "做过眼底检查吗？", ("做过", "没做过", "不清楚")),
    FieldDependency("cardiac_exam", "心脏/冠脉检查",
                    ("心血管内科", "麻醉科", "心胸外科"), True,
                    "做过心电图、心脏彩超或冠脉相关检查吗？", ("做过", "没做过", "不清楚")),
    FieldDependency("blood_count", "血常规（三系）",
                    ("血液科", "感染科", "肿瘤科"), True,
                    "最近的血常规结果有吗？白细胞、血红蛋白、血小板大概是多少？", ("有结果", "没有", "不清楚")),
    FieldDependency("fever_pattern", "发热热型与病程",
                    ("感染科", "血液科", "风湿免疫科"), True,
                    "发热多少天了？是持续发热还是忽高忽低？最高多少度？", ("能说清", "说不清")),
    FieldDependency("autoimmune_antibody", "自身免疫抗体",
                    ("风湿免疫科", "肾内科", "血液科"), False,
                    "查过自身抗体（如 ANA、ANCA）吗？", ("查过", "没查过", "不清楚")),
    FieldDependency("thyroid_function", "甲状腺功能",
                    ("内分泌科",), False, "查过甲功（TSH、T3、T4）吗？", ("查过", "没查过", "不清楚")),
    FieldDependency("liver_function", "肝功能",
                    ("消化内科", "感染科", "肿瘤科"), False,
                    "最近查过肝功能吗？", ("查过", "没查过", "不清楚")),
    FieldDependency("renal_function", "肾功能（肌酐/eGFR）",
                    ("肾内科", "心血管内科", "内分泌科"), True,
                    "最近查过肌酐或肾小球滤过率吗？", ("查过", "没查过", "不清楚")),
    FieldDependency("imaging", "影像检查（CT/MRI）",
                    ("神经内科", "神经外科", "呼吸内科", "骨科"), True,
                    "做过 CT 或核磁吗？报告上写了什么？", ("做过", "没做过", "不清楚")),
    FieldDependency("chest_imaging", "胸部影像",
                    ("呼吸内科", "感染科", "肿瘤科"), False,
                    "做过胸片或胸部 CT 吗？", ("做过", "没做过", "不清楚")),
    FieldDependency("pathology", "病理结果",
                    ("肿瘤科", "病理科"), False,
                    "有没有做过活检或病理检查？", ("做过", "没做过", "不清楚")),
    FieldDependency("ecg", "心电图",
                    ("心血管内科", "麻醉科"), False, "做过心电图吗？", ("做过", "没做过", "不清楚")),
    FieldDependency("coagulation", "凝血/出血倾向",
                    ("血液科", "麻醉科", "普通外科"), False,
                    "有没有容易出血、牙龈出血或正在吃抗凝药？", ("没有", "有", "不清楚")),
    FieldDependency("medication_list", "当前用药清单",
                    ("内分泌科", "肾内科", "眼科", "心血管内科"), False,
                    "现在正在吃哪些药？剂量是多少？", ("能列出", "记不清")),
    FieldDependency("allergy", "过敏史",
                    ("急诊科", "感染科", "普通外科"), False,
                    "有没有药物或食物过敏？", ("有", "没有", "不清楚")),
    FieldDependency("weight_change", "近期体重变化",
                    ("内分泌科", "肿瘤科", "消化内科"), False,
                    "最近半年体重有明显变化吗？", ("明显下降", "明显增加", "没变化")),
    FieldDependency("night_sweats", "盗汗 / 夜间症状",
                    ("感染科", "血液科", "肿瘤科"), False,
                    "有没有夜间盗汗、反复发热的情况？", ("有", "没有", "不清楚")),
    FieldDependency("joint_pain", "关节痛 / 皮疹",
                    ("风湿免疫科", "皮肤科", "骨科"), False,
                    "有没有关节痛、皮疹或口腔溃疡？", ("有", "没有", "不清楚")),
    FieldDependency("smoking_alcohol", "吸烟饮酒史",
                    ("呼吸内科", "消化内科", "肿瘤科"), False,
                    "有吸烟或长期饮酒的习惯吗？", ("有", "没有", "已戒")),
    FieldDependency("family_history", "家族史",
                    ("肿瘤科", "心血管内科", "内分泌科"), False,
                    "家里直系亲属有没有类似疾病或肿瘤史？", ("有", "没有", "不清楚")),
    FieldDependency("symptom_trend", "症状变化趋势",
                    ("急诊科", "神经内科", "心血管内科"), True,
                    "这些症状是在加重、减轻，还是差不多？", ("加重", "减轻", "差不多")),
)

# 字段 key → 依赖定义
BY_FIELD: Dict[str, FieldDependency] = {d.field: d for d in MATRIX}

# 中文标签 → 字段 key（便于从画像的"信息缺口"文本反查）
BY_LABEL: Dict[str, str] = {d.label: d.field for d in MATRIX}


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

def depends_on(field: str) -> Optional[FieldDependency]:
    """按字段 key 取依赖定义。"""
    return BY_FIELD.get(field)


def fields_for_department(department: str) -> List[str]:
    """某专科依赖哪些字段（用于增量更新影响范围）。"""
    return [d.field for d in MATRIX if department in d.departments]


def departments_for_field(field: str) -> Tuple[str, ...]:
    """某字段影响哪些专科判断。"""
    dep = BY_FIELD.get(field)
    return dep.departments if dep else ()


def affected_departments(fields: Iterable[str]) -> List[str]:
    """一批字段（如新资料涉及的字段）会影响哪些专科 —— 增量更新只重跑这些模块。"""
    out: List[str] = []
    for field in fields or []:
        for dept in departments_for_field(field):
            if dept not in out:
                out.append(dept)
    return out


def is_high_value(field: str, departments: Optional[Sequence[str]] = None) -> bool:
    """是否"高价值"缺失字段。

    规格书 §6.1：缺失字段 ∈ 依赖矩阵 且 影响 ≥1 个专科判断（可选：限定在候选科室范围内）。
    """
    dep = BY_FIELD.get(field)
    if dep is None or not dep.departments:
        return False
    if departments is None:
        return True
    return any(d in dep.departments for d in departments)


def high_value_gaps(gaps: Iterable[str], departments: Optional[Sequence[str]] = None) -> List[str]:
    """从画像的"信息缺口"文本中筛出高价值字段（按矩阵匹配，匹配不到则忽略）。"""
    found: List[str] = []
    for gap in gaps or []:
        text = str(gap)
        for label, field in BY_LABEL.items():
            if label in text and field not in found:
                if is_high_value(field, departments):
                    found.append(field)
                break
    return found


def triage_critical_fields() -> List[str]:
    """影响分诊方向的字段（追问排序时优先）。"""
    return [d.field for d in MATRIX if d.affects_triage]


def matrix_size() -> int:
    """矩阵条数（单测与文档校验用）。"""
    return len(MATRIX)
