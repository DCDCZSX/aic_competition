"""病历语境判断：否定病史、正常结果、既往已缓解症状。

规则引擎和病例画像共用这一套判断，避免两边口径不一致（例如画像把
"否认糖尿病病史"收录为糖尿病，而规则选科没有命中）。

判断都限定在关键词所在的分句内（以逗号、句号、分号等切分），只看
当前这一句话的语境，不跨句传播。

注意：危急症状识别（guardrail）不使用这里的放宽判断，仍只按紧邻
否定词排除，宁可多报，不漏报。
"""

from __future__ import annotations

import re
from typing import Tuple

# 分句边界
_CLAUSE_BREAK = re.compile(r"[，,。；;！!？?\n\r]")

# 紧邻否定："无发热""未见异常""不伴咳嗽"
_ADJACENT_NEGATION = re.compile(r"(无|没有|否认|不伴|未见|未|非|不)\s*$")

# 分句级否定：否认/排除 可以管到同一分句里后面列出的多个项目，
# 如"否认高血压、糖尿病病史""排除结核"
_CLAUSE_NEGATION = re.compile(r"(否认|排除|除外|没有.{0,4}病史)")

# 正常结果：关键词之后同一分句内出现
_NORMAL_RESULT = re.compile(
    r"(正常|未见异常|未见明显异常|无异常|阴性|\(-\)|（-）|在正常范围|达标|控制良好|控制可)")
# 同一分句内若同时出现异常描述，以异常为准
_ABNORMAL_RESULT = re.compile(
    r"(异常|不正常|非正常|升高|增高|偏高|降低|减低|偏低|阳性|\(\+\)|（\+）|↑|↓|超标|不佳|不良|欠佳|未达标)")

# 已缓解/已治愈：同一分句出现即视为历史信息
_RESOLVED = re.compile(r"(已治愈|已痊愈|治愈|痊愈|已愈|已消失|已消退|已缓解|已恢复|已好转)")
# 既往标记：只对"症状类"关键词生效，慢病史（如既往糖尿病）仍然有效
_HISTORY = re.compile(r"(既往|曾经|曾有|曾出现|曾|以往|过去|幼时|儿时|多年前)")

# 症状类关键词：既往出现过的症状不作为当前症状
SYMPTOM_TERMS: Tuple[str, ...] = (
    "发热", "高热", "低热", "畏寒", "寒战", "盗汗", "咳嗽", "咳痰", "咯血", "气促", "气短",
    "呼吸困难", "喘息", "胸痛", "胸闷", "心悸", "心慌", "腹痛", "腹胀", "腹泻", "便秘",
    "恶心", "呕吐", "呕血", "黑便", "便血", "头痛", "头晕", "眩晕", "晕厥", "抽搐",
    "乏力", "水肿", "浮肿", "皮疹", "瘙痒", "关节痛", "关节肿", "腰痛", "背痛", "尿频",
    "尿急", "尿痛", "血尿", "鼻出血", "耳鸣", "视物模糊", "多饮", "多尿", "体重下降",
    "消瘦", "黄疸",
)

# 结果类关键词：带"正常/阴性"时不算异常信号（检查、指标、生命体征）
RESULT_TERMS: Tuple[str, ...] = (
    "血糖", "糖化血红蛋白", "血压", "心率", "体温", "血常规", "白细胞", "血红蛋白", "血小板",
    "肝功能", "肾功能", "肌酐", "尿素氮", "尿蛋白", "尿常规", "转氨酶", "胆红素",
    "血脂", "尿酸", "甲状腺", "TSH", "心电图", "心脏彩超", "胸片", "CT", "MRI", "核磁",
    "B超", "彩超", "超声", "眼底", "肿瘤标志物", "CEA", "AFP", "D-二聚体", "BNP",
    "肌钙蛋白", "降钙素原", "CRP", "C反应蛋白", "血沉", "ESR", "凝血", "电解质", "血钾",
    "血钠", "血气", "肺功能", "胃镜", "肠镜", "骨密度", "抗核抗体", "ANA", "补体",
)


def clause_bounds(text: str, start: int, end: int) -> Tuple[int, int]:
    """返回 [start, end) 所在分句的边界。"""
    left = start
    while left > 0 and not _CLAUSE_BREAK.match(text[left - 1]):
        left -= 1
    right = end
    n = len(text)
    while right < n and not _CLAUSE_BREAK.match(text[right]):
        right += 1
    return left, right


def is_negated(text: str, start: int, end: int = -1) -> bool:
    """紧邻否定（3 字以内）或同一分句前部出现"否认/排除"。"""
    if _ADJACENT_NEGATION.search(text[max(0, start - 3):start]):
        return True
    left, _ = clause_bounds(text, start, start if end < 0 else end)
    prefix = text[left:start]
    m = _CLAUSE_NEGATION.search(prefix)
    if not m:
        return False
    # "否认外伤史，但有糖尿病"已被逗号切开；同一分句内出现转折则不再否定
    return not re.search(r"(但|然而|却|现有|目前有)", prefix[m.end():])


def is_normal_result(text: str, start: int, end: int, keyword: str = "") -> bool:
    """关键词是检查/指标，且同一分句内描述为正常、没有异常描述。"""
    term = keyword or text[start:end]
    if not any(t in term or term in t for t in RESULT_TERMS):
        return False
    tail = _item_tail(text, start, end, term)
    if not _NORMAL_RESULT.search(tail):
        return False
    return not _ABNORMAL_RESULT.search(tail.replace("未见异常", "").replace("未见明显异常", "")
                                       .replace("无异常", ""))


def _item_tail(text: str, start: int, end: int, term: str) -> str:
    """将结果与缓解描述限定在当前事项，遇到另一指标或症状即停止。"""
    _, right = clause_bounds(text, start, end)
    tail = text[end:right]
    boundary = re.search(r"但(?:是)?|然而|却|伴(?:有)?|合并|同时", tail)
    stop = boundary.start() if boundary else len(tail)
    for other in (*SYMPTOM_TERMS, *RESULT_TERMS):
        if other in term or term in other:
            continue
        index = tail.find(other)
        if index >= 0:
            stop = min(stop, index)
    return tail[:stop]


def is_resolved_history(text: str, start: int, end: int, keyword: str = "") -> bool:
    """同一分句标明已治愈/已缓解；或症状类关键词前有"既往/曾"。"""
    left, right = clause_bounds(text, start, end)
    term = keyword or text[start:end]
    if _RESOLVED.search(_item_tail(text, start, end, term)):
        return True
    if any(s in term for s in SYMPTOM_TERMS) and _HISTORY.search(text[left:start]):
        # "既往无明显诱因"之类不是症状描述；"曾…现又…"表示当前再次出现
        if re.search(r"(现|目前|再次|又|复发|反复|加重)", text[start:right]):
            return False
        return True
    return False


def is_excluded(text: str, start: int, end: int, keyword: str = "") -> bool:
    """命中位置是否应排除：否定、正常结果或既往已缓解。"""
    return (is_negated(text, start, end)
            or is_normal_result(text, start, end, keyword)
            or is_resolved_history(text, start, end, keyword))
