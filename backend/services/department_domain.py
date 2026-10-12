"""科室域 v1 —— 全系统唯一的科室权威字典。

对应《产品开发规格书-比赛版v-competition.md》§4.2。

设计规则（写死，供 S1 规则引擎 / S3 编排校验 / 评测 / 前端共用）：

1. **37 门类**，分四组：临床内科(13) / 外科系(8) / 临床专科(10) / 辅助科(6)；
2. **辅助科可参与会诊，但不得担任牵头科室**（牵头位仅限临床科）；
3. **别名映射 `ALIAS_MAP`**：医院常见写法 → 域内标准名（分诊与评测共用同一张表）；
4. **OOV（域外科）三序处理，不静默丢弃**：
   ① 命中别名表 → ② 命中"最近似科表"并提示"已按近似科 XX 处理" → ③ 记 `oov_log`，原样呈现"建议咨询 XX（未建模）"；
5. 本模块是**唯一权威源**：新增科室必须改这里，并同步别名表与前端展示。

用法：
    from services.department_domain import resolve, resolve_many, can_be_primary

    r = resolve("心内科")          # Resolution(status='alias', name='心血管内科')
    r = resolve("口腔颌面外科")     # Resolution(status='nearest', name='普通外科', note='已按近似科 普通外科 处理')
    r = resolve("营养科")          # Resolution(status='oov', name=None, note='建议咨询 营养科（未建模）')
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 一、权威字典（37 门类）
# ---------------------------------------------------------------------------

GROUP_INTERNAL = "internal"      # 临床内科
GROUP_SURGERY = "surgery"        # 外科系
GROUP_SPECIALTY = "specialty"    # 临床专科
GROUP_AUXILIARY = "auxiliary"    # 辅助科（可参与，不得牵头）

GROUP_NAMES: Dict[str, str] = {
    GROUP_INTERNAL: "临床内科",
    GROUP_SURGERY: "外科系",
    GROUP_SPECIALTY: "临床专科",
    GROUP_AUXILIARY: "辅助科",
}


@dataclass(frozen=True)
class Department:
    """一个科室单元。code 兼容旧代码，name 为唯一标准中文名。"""

    code: str
    name: str
    group: str


DEPARTMENTS: Tuple[Department, ...] = (
    # ---- 临床内科(13) ----
    Department("cardiology", "心血管内科", GROUP_INTERNAL),
    Department("neurology", "神经内科", GROUP_INTERNAL),
    Department("pulmonology", "呼吸内科", GROUP_INTERNAL),
    Department("gastroenterology", "消化内科", GROUP_INTERNAL),
    Department("nephrology", "肾内科", GROUP_INTERNAL),
    Department("endocrinology", "内分泌科", GROUP_INTERNAL),
    Department("hematology", "血液科", GROUP_INTERNAL),
    Department("rheumatology", "风湿免疫科", GROUP_INTERNAL),
    Department("infectious_disease", "感染科", GROUP_INTERNAL),
    Department("oncology", "肿瘤科", GROUP_INTERNAL),
    Department("geriatrics", "老年医学科", GROUP_INTERNAL),
    Department("emergency", "急诊科", GROUP_INTERNAL),
    Department("icu", "重症医学科", GROUP_INTERNAL),
    # ---- 外科系(8) ----
    Department("general_surgery", "普通外科", GROUP_SURGERY),
    Department("hepatobiliary_surgery", "肝胆外科", GROUP_SURGERY),
    Department("orthopedics", "骨科", GROUP_SURGERY),
    Department("neurosurgery", "神经外科", GROUP_SURGERY),
    Department("cardiac_surgery", "心胸外科", GROUP_SURGERY),
    Department("urology", "泌尿外科", GROUP_SURGERY),
    Department("vascular_surgery", "血管外科", GROUP_SURGERY),
    Department("plastic_surgery", "整形外科", GROUP_SURGERY),
    # ---- 临床专科(10) ----
    Department("dermatology", "皮肤科", GROUP_SPECIALTY),
    Department("ophthalmology", "眼科", GROUP_SPECIALTY),
    Department("otolaryngology", "耳鼻喉科", GROUP_SPECIALTY),
    Department("psychiatry", "精神科", GROUP_SPECIALTY),
    Department("rehabilitation", "康复医学科", GROUP_SPECIALTY),
    Department("obstetrics_gynecology", "妇产科", GROUP_SPECIALTY),
    Department("pediatrics", "儿科", GROUP_SPECIALTY),
    Department("neonatology", "新生儿科", GROUP_SPECIALTY),
    Department("traditional_chinese_medicine", "中医科", GROUP_SPECIALTY),
    Department("acupuncture", "针灸科", GROUP_SPECIALTY),
    # ---- 辅助科(6)：可参与会诊，不得牵头 ----
    Department("radiology", "影像科", GROUP_AUXILIARY),
    Department("ultrasound", "超声科", GROUP_AUXILIARY),
    Department("pathology", "病理科", GROUP_AUXILIARY),
    Department("laboratory", "检验科", GROUP_AUXILIARY),
    Department("interventional", "介入科", GROUP_AUXILIARY),
    Department("anesthesiology", "麻醉科", GROUP_AUXILIARY),
)

# 对外提供稳定的 {code: 中文名} 字典，供分诊与报告模块复用
SPECIALISTS: Dict[str, str] = {d.code: d.name for d in DEPARTMENTS}

# 标准名 → 科室对象
_BY_NAME: Dict[str, Department] = {d.name: d for d in DEPARTMENTS}
# code → 科室对象
_BY_CODE: Dict[str, Department] = {d.code: d for d in DEPARTMENTS}

ALL_NAMES: Tuple[str, ...] = tuple(d.name for d in DEPARTMENTS)

# 可牵头科室（临床科：内科 + 外科 + 临床专科）
PRIMARY_ELIGIBLE_NAMES: Tuple[str, ...] = tuple(
    d.name for d in DEPARTMENTS if d.group != GROUP_AUXILIARY
)

# ---------------------------------------------------------------------------
# 二、别名映射（医院写法 → 域内标准名）
# ---------------------------------------------------------------------------

ALIAS_MAP: Dict[str, str] = {
    # 内科系
    "心内科": "心血管内科",
    "心血管科": "心血管内科",
    "心脏内科": "心血管内科",
    "神经科": "神经内科",
    "神经内": "神经内科",
    "呼吸科": "呼吸内科",
    "呼吸与危重症医学科": "呼吸内科",
    "消化科": "消化内科",
    "消化内": "消化内科",
    "肾内": "肾内科",
    "肾脏内科": "肾内科",
    "内分泌代谢科": "内分泌科",
    "内分泌二区": "内分泌科",
    "血液内科": "血液科",
    "血研所": "血液科",
    "风湿科": "风湿免疫科",
    "免疫科": "风湿免疫科",
    "感染性疾病科": "感染科",
    "传染科": "感染科",
    "肿瘤内科": "肿瘤科",
    "老年科": "老年医学科",
    "急诊医学科": "急诊科",
    "ICU": "重症医学科",
    "重症监护室": "重症医学科",
    # 外科系
    "普外": "普通外科",
    "普外科": "普通外科",
    "肝胆科": "肝胆外科",
    "肝胆胰外科": "肝胆外科",
    "骨外科": "骨科",
    "神外": "神经外科",
    "胸外科": "心胸外科",
    "心外科": "心胸外科",
    "心血管外科": "心胸外科",
    "泌尿科": "泌尿外科",
    "血管科": "血管外科",
    "整形科": "整形外科",
    # 临床专科
    "皮肤性病科": "皮肤科",
    "眼耳鼻喉科": "耳鼻喉科",
    "耳鼻咽喉科": "耳鼻喉科",
    "耳鼻咽喉头颈外科": "耳鼻喉科",
    "心理科": "精神科",
    "心理咨询科": "精神科",
    "精神心理科": "精神科",
    "康复科": "康复医学科",
    "妇科": "妇产科",
    "产科": "妇产科",
    "小儿科": "儿科",
    "儿内科": "儿科",
    "中医内科": "中医科",
    # 辅助科
    "放射科": "影像科",
    "影像诊断科": "影像科",
    "放射诊断科": "影像科",
    "彩超": "超声科",
    "超声诊断科": "超声科",
    "超声医学科": "超声科",
    "检验医学中心": "检验科",
    "化验科": "检验科",
    "介入治疗科": "介入科",
    "介入放射科": "介入科",
    "麻醉手术科": "麻醉科",
    "麻醉手术中心": "麻醉科",
}

# ---------------------------------------------------------------------------
# 三、最近似科表（OOV 兜底，附提示语）
# ---------------------------------------------------------------------------

NEAREST_MAP: Dict[str, str] = {
    "口腔科": "普通外科",
    "口腔颌面外科": "普通外科",
    "颌面外科": "普通外科",
    "肛肠科": "普通外科",
    "乳腺外科": "普通外科",
    "甲状腺外科": "普通外科",
    "疼痛科": "麻醉科",
    "运动医学科": "骨科",
    "男科": "泌尿外科",
    "生殖医学科": "妇产科",
    "全科医学科": "老年医学科",
    "儿外科": "儿科",
    "头颈外科": "耳鼻喉科",
    "职业病科": "呼吸内科",
}

# ---------------------------------------------------------------------------
# 四、解析（normalize / OOV 三序）
# ---------------------------------------------------------------------------

STATUS_EXACT = "exact"      # 命中标准名
STATUS_ALIAS = "alias"      # 命中别名表
STATUS_NEAREST = "nearest"  # 命中最近似科（附提示）
STATUS_OOV = "oov"          # 域外，未建模（原样呈现 + 记日志）


@dataclass
class Resolution:
    """一次科室名解析的结果。"""

    raw: str                      # 原始输入
    name: Optional[str]           # 归一化后的标准名；OOV 时为 None
    status: str                   # exact / alias / nearest / oov
    note: str = ""                # 面向用户的提示语（OOV 时保留原始名称）

    @property
    def ok(self) -> bool:
        """是否落在域内（含近似科处理）。"""
        return self.name is not None

    @property
    def needs_review(self) -> bool:
        """是否需要人工/规则复核（近似科与 OOV 都需要留意）。"""
        return self.status in (STATUS_NEAREST, STATUS_OOV)


class OOVLog:
    """域外科记录器（比赛版内存实现；正式版应落库并进入审计）。"""

    def __init__(self) -> None:
        self.entries: List[Resolution] = []

    def record(self, resolution: Resolution) -> None:
        if resolution.status in (STATUS_NEAREST, STATUS_OOV):
            self.entries.append(resolution)

    def __len__(self) -> int:
        return len(self.entries)

    def summary(self) -> List[Dict[str, str]]:
        return [
            {"raw": e.raw, "status": e.status, "mapped": e.name or "", "note": e.note}
            for e in self.entries
        ]


def _clean(raw: str) -> str:
    """去空白与常见后缀噪音（如"专家门诊"）。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    for suffix in ("专家门诊", "门诊", "专科", "。"):
        if len(text) > len(suffix) and text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


def resolve(raw: str) -> Resolution:
    """按 OOV 三序解析单个科室名。"""
    text = _clean(raw)
    if not text:
        return Resolution(raw=str(raw), name=None, status=STATUS_OOV, note="科室名为空，无法识别")

    # ① 标准名
    if text in _BY_NAME:
        return Resolution(raw=text, name=text, status=STATUS_EXACT)
    # ① 别名表
    if text in ALIAS_MAP:
        return Resolution(raw=text, name=ALIAS_MAP[text], status=STATUS_ALIAS)
    # ② 最近似科
    if text in NEAREST_MAP:
        mapped = NEAREST_MAP[text]
        return Resolution(
            raw=text,
            name=mapped,
            status=STATUS_NEAREST,
            note=f"已按近似科 {mapped} 处理",
        )
    # ③ 域外，未建模：不静默丢弃，原样呈现
    return Resolution(
        raw=text,
        name=None,
        status=STATUS_OOV,
        note=f"建议咨询 {text}（未建模）",
    )


def resolve_many(raws: Iterable[str], log: Optional[OOVLog] = None) -> List[Resolution]:
    """批量解析；传入 log 时自动记录近似科与 OOV。"""
    results: List[Resolution] = []
    for raw in raws or []:
        item = resolve(raw)
        if log is not None:
            log.record(item)
        results.append(item)
    return results


def dedupe_names(names: Iterable[str]) -> List[str]:
    """按域内标准名去重并保持顺序（别名归一后合并重复科室）。"""
    seen: Dict[str, None] = {}
    for raw in names or []:
        r = resolve(raw)
        if r.name and r.name not in seen:
            seen[r.name] = None
    return list(seen.keys())


# ---------------------------------------------------------------------------
# 五、查询与校验
# ---------------------------------------------------------------------------

def is_department(name: str) -> bool:
    """是否为域内标准科室名。"""
    return _clean(name) in _BY_NAME


def to_code(name: str) -> Optional[str]:
    """标准名 → code（兼容旧代码）。"""
    dept = _BY_NAME.get(_clean(name))
    return dept.code if dept else None


def to_name(code: str) -> Optional[str]:
    """code → 标准名。"""
    dept = _BY_CODE.get(str(code).strip())
    return dept.name if dept else None


def group_of(name: str) -> Optional[str]:
    dept = _BY_NAME.get(_clean(name))
    return dept.group if dept else None


def is_auxiliary(name: str) -> bool:
    """是否辅助科（可参与，不得牵头）。"""
    return group_of(name) == GROUP_AUXILIARY


def can_be_primary(name: str) -> bool:
    """是否可担任牵头科室（仅限临床科）。"""
    return group_of(name) is not None and not is_auxiliary(name)


def group_counts() -> Dict[str, int]:
    """各分组科室数量（用于单测与文档校验）。"""
    counts: Dict[str, int] = {g: 0 for g in GROUP_NAMES}
    for dept in DEPARTMENTS:
        counts[dept.group] += 1
    return counts
