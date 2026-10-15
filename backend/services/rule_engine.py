"""S1 规则引擎（确定性分诊第一段）。

对应同级 `competition-materials/docs/产品开发规格书-比赛版v-competition.md` §4.3 决策链第一段。

职责：
1. `evaluate(text)` —— 命中规则 → 每科带 `score_R`（strong=3 / medium=2 / weak=1）与**命中信号文本**（可解释）；
2. `behave(text, hits)` —— 判定行为类型 A-E（D 型由手术/介入词表判定；E 型由 Guardrail G1 拦截）；
3. 规则表**只依赖科室域标准名**（`department_domain`），新增科室必须同步这里。

设计依据：同级 `competition-materials/evaluation/评测v0结果与改进方向.md` 指出的四类系统性漏科（手术决策 / 康复 / 风湿 / 神外 / 麻醉超声介入）已纳入规则表。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.department_domain import is_department  # noqa: E402
from services.clinical_context import is_excluded  # noqa: E402

# 权重 → 分数（规格书 §4.3）
WEIGHT_SCORE: Dict[str, int] = {"strong": 3, "medium": 2, "weak": 1}

# 科室牵头倾向（0-2）：基础病/主症科室适合牵头；并发症、检查与康复类通常不牵头。
# 用于在"同为 strong 命中"时决定谁来牵头（规格书 §7.1「牵头方向」）。
DEFAULT_LEAD: Dict[str, int] = {
    # 适合牵头（常见主要问题所在科）
    "内分泌科": 2, "心血管内科": 2, "神经内科": 2, "呼吸内科": 2, "消化内科": 2,
    "血液科": 2, "风湿免疫科": 2, "感染科": 2, "肿瘤科": 2,
    "神经外科": 2, "心胸外科": 2, "普通外科": 2, "肝胆外科": 2,
    "骨科": 2, "泌尿外科": 2, "妇产科": 2, "儿科": 2, "新生儿科": 2,
    # 常为协同/并发症方向
    "肾内科": 1, "眼科": 1, "皮肤科": 1, "耳鼻喉科": 1, "精神科": 1,
    "老年医学科": 1, "急诊科": 1, "重症医学科": 1, "血管外科": 1,
    "整形外科": 1, "介入科": 1, "麻醉科": 1,
    # 不牵头
    "康复医学科": 0, "中医科": 0, "针灸科": 0,
    "影像科": 0, "超声科": 0, "检验科": 0, "病理科": 0,
}

# 行为类型
BEHAVIOR_A = "A"   # 单科简单
BEHAVIOR_B = "B"   # 多病共病
BEHAVIOR_C = "C"   # 疑难排查
BEHAVIOR_D = "D"   # 手术/介入决策
BEHAVIOR_E = "E"   # 危象（由 G1 拦截，本层不产出）


# 否定语境（病历常见写法）："无发热""否认咳嗽""未见异常"不应算命中
_NEGATION_PATTERN = re.compile(r"(无|没有|否认|不伴|未见|未|非|不)\s*$")


def is_negated(text: str, start: int, window: int = 3) -> bool:
    """关键词位置之前是否是否定语境。

    "无发热" → True（不算命中）；"无痛性血尿" → False（"血尿"是症状，中间隔了"痛性"）。
    """
    prefix = text[max(0, start - window):start]
    return bool(_NEGATION_PATTERN.search(prefix))


@dataclass(frozen=True)
class Rule:
    """一条分诊规则（可解释、可审计）。"""

    rule_id: str
    department: str                       # 科室域标准名
    weight: str                           # strong / medium / weak
    keywords: Tuple[str, ...] = ()        # 任一命中
    all_of: Tuple[str, ...] = ()          # 组合规则：需全部出现
    any_of: Tuple[str, ...] = ()          # 与 all_of 同时成立：这一组里**至少**命中一个
    signal: str = ""                      # 命中后展示给用户的信号描述
    pattern: str = ""                     # 可选正则（优先级高于 keywords）

    @staticmethod
    def _present(text: str, keyword: str) -> bool:
        """出现且不处于否定、正常结果或既往已缓解语境（"无发热""血糖正常""既往发热已治愈"均不算命中）。"""
        idx = text.find(keyword)
        while idx >= 0:
            if not is_excluded(text, idx, idx + len(keyword), keyword):
                return True
            idx = text.find(keyword, idx + 1)
        return False

    def match(self, text: str) -> Optional[str]:
        """命中则返回命中的信号文本，否则 None（会跳过否定语境，如"无发热"）。"""
        if self.pattern:
            for m in re.finditer(self.pattern, text):
                if not is_excluded(text, m.start(), m.end(), m.group(0)):
                    return m.group(0)
            return None
        if self.all_of or self.any_of:
            hit: List[str] = []
            if self.all_of:
                for kw in self.all_of:
                    if not self._present(text, kw):
                        return None
                    hit.append(kw)
            if self.any_of:
                # 「至少命中一个」：用于表达"发热 **且** 血象异常"这类多条件规则，
                # 避免只因为文中出现了某个检查名（如"血常规"）就误命中
                for kw in self.any_of:
                    if self._present(text, kw):
                        hit.append(kw)
                        break
                else:
                    return None
            return " + ".join(hit)
        for kw in self.keywords:
            if self._present(text, kw):
                return kw
        return None


# ---------------------------------------------------------------------------
# 规则表（覆盖 37 科室的主要映射；含评测 v0 点名的缺失类别）
# ---------------------------------------------------------------------------

RULES: Tuple[Rule, ...] = (
    # ===== 心血管 =====
    Rule("cardio_chest_pain", "心血管内科", "strong", ("胸痛", "胸闷", "心前区不适"),
         signal="胸痛/胸闷 → 心血管内科"),
    Rule("cardio_palpitation", "心血管内科", "medium", ("心悸", "心慌", "心律不齐", "房颤", "心房颤动"),
         signal="心悸/心律失常 → 心血管内科"),
    Rule("cardio_htn", "心血管内科", "medium", ("高血压", "血压高"), signal="高血压 → 心血管内科"),
    # 评测 v2（SB-C4）：真实文本常只写"血压 176/108mmHg"这个数值，不写"高血压"
    Rule("cardio_htn_value", "心血管内科", "medium",
         pattern=r"血压[^0-9]{0,8}(?:1[4-9]\d|2\d\d)\s*/\s*\d{2,3}",
         signal="血压偏高（实测值 ≥140mmHg）→ 心血管内科"),
    Rule("cardio_hf", "心血管内科", "medium", ("心衰", "心力衰竭", "下肢浮肿", "端坐呼吸"),
         signal="心衰表现 → 心血管内科"),
    Rule("cardio_ecg", "心血管内科", "weak", ("心电图", "心脏彩超", "冠脉", "心超", "射血分数"),
         signal="心脏相关检查 → 心血管内科"),

    # ===== 神经内科 =====
    Rule("neuro_headache", "神经内科", "strong", ("头痛", "头晕", "眩晕", "晕厥"),
         signal="头痛/头晕 → 神经内科"),
    Rule("neuro_stroke_sign", "神经内科", "strong", ("肢体无力", "口角歪斜", "言语不清", "偏瘫", "麻木"),
         signal="神经系统定位体征 → 神经内科"),
    Rule("neuro_seizure", "神经内科", "strong", ("抽搐", "癫痫", "意识障碍", "记忆减退"),
         signal="发作性/意识相关症状 → 神经内科"),
    Rule("neuro_insomnia", "神经内科", "weak", ("失眠", "手脚发麻", "面瘫"), signal="神经症状 → 神经内科"),

    # ===== 呼吸内科 =====
    Rule("resp_cough", "呼吸内科", "strong", ("咳嗽", "咳痰", "咯血", "气促", "呼吸困难", "喘息"),
         signal="呼吸道症状 → 呼吸内科"),
    Rule("resp_copd", "呼吸内科", "medium", ("慢阻肺", "慢性阻塞性肺疾病", "哮喘", "肺结节", "肺部感染"),
         signal="慢性气道/肺部疾病 → 呼吸内科"),
    Rule("resp_imaging", "呼吸内科", "weak", ("胸片", "胸部CT", "肺功能"), signal="胸部检查 → 呼吸内科"),

    # ===== 消化内科 =====
    Rule("gi_abdomen", "消化内科", "strong", ("腹痛", "腹胀", "反酸", "烧心", "腹泻", "便秘", "恶心", "呕吐"),
         signal="消化道症状 → 消化内科"),
    Rule("gi_liver", "消化内科", "medium", ("肝功能异常", "转氨酶", "肝硬化", "脂肪肝", "黄疸", "肝炎"),
         signal="肝功能/肝病 → 消化内科"),
    Rule("gi_bleed", "消化内科", "medium", ("黑便", "呕血", "便血"), signal="消化道出血 → 消化内科"),

    # ===== 泌尿外科（评测 v2：SB-A6 尿频尿急尿痛原本一条规则都没命中）=====
    Rule("uro_lower", "泌尿外科", "strong",
         ("尿频", "尿急", "尿痛", "尿路感染", "膀胱炎", "排尿困难", "尿潴留", "血尿"),
         signal="下尿路症状 → 泌尿外科"),
    Rule("uro_stone", "泌尿外科", "medium", ("肾结石", "输尿管结石", "泌尿系结石", "肾绞痛"),
         signal="泌尿系结石 → 泌尿外科"),
    Rule("uro_prostate", "泌尿外科", "medium", ("前列腺", "前列腺增生", "前列腺特异抗原"),
         signal="前列腺相关 → 泌尿外科"),

    # ===== 肾内科 =====
    Rule("neph_proteinuria", "肾内科", "strong", ("尿蛋白", "蛋白尿", "血尿", "肌酐升高", "肾功能不全", "尿少", "水肿"),
         signal="蛋白尿/肾功能异常 → 肾内科"),
    Rule("neph_dialysis", "肾内科", "medium", ("透析", "肾病综合征", "慢性肾病", "肾炎"),
         signal="肾脏疾病 → 肾内科"),

    # ===== 内分泌科 =====
    Rule("endo_dm", "内分泌科", "strong", ("糖尿病", "血糖", "糖化血红蛋白", "多饮", "多尿"),
         signal="糖尿病/血糖异常 → 内分泌科"),
    Rule("endo_thyroid", "内分泌科", "medium", ("甲状腺", "甲亢", "甲减", "TSH", "突眼"),
         signal="甲状腺问题 → 内分泌科"),
    Rule("endo_metabolic", "内分泌科", "weak", ("肥胖", "血脂异常", "痛风", "高尿酸"),
         signal="代谢异常 → 内分泌科"),

    # ===== 血液科 =====
    Rule("hema_cytopenia", "血液科", "strong", ("贫血", "三系减少", "白细胞减少", "血小板减少", "血红蛋白低", "血象异常"),
         signal="血细胞异常 → 血液科"),
    Rule("hema_fever_unknown", "血液科", "medium", all_of=("发热",),
         any_of=("三系减少", "白细胞减少", "血小板减少", "血红蛋白低", "血象异常", "贫血", "脾大", "铁蛋白"),
         signal="发热 + 血象异常 → 血液科（需排查血液系统疾病）"),
    Rule("hema_hlh", "血液科", "strong", ("噬血", "噬血细胞综合征", "淋巴瘤", "白血病"),
         signal="血液系统重症 → 血液科"),
    # ---- 评测 v2 补的"看数值不看词"规则 ----
    # 真实病历写的是"血红蛋白 92g/L""血小板 43×10^9/L"，而不是"贫血""血小板减少"，
    # 原来只认后者 → 漏诊（SB-A7 / SB-B5 都漏了血液科）。阈值按成人常用下限的保守口径：
    # 血红蛋白 <120 g/L（未分性别）、血小板 <100×10^9/L。
    Rule("hema_anemia_value", "血液科", "medium",
         pattern=r"血红蛋白[^0-9]{0,6}(?:[5-9]\d|1[01]\d)\s*(?:g/L|g/l|克/升)",
         signal="血红蛋白偏低（实测值）→ 血液科"),
    Rule("hema_plt_value", "血液科", "medium",
         pattern=r"血小板[^0-9]{0,6}(?:[1-9]?\d)\s*[×xX*]",
         signal="血小板偏低（实测值）→ 血液科"),
    Rule("hema_lymphnode", "血液科", "medium",
         ("淋巴结肿大", "淋巴结增大", "颈部淋巴结", "腋窝淋巴结", "腹股沟淋巴结"),
         signal="淋巴结肿大 → 血液科（需排查淋巴瘤等）"),

    # ===== 风湿免疫科（评测 v0：原关键词表完全缺失）=====
    Rule("rheum_autoimmune", "风湿免疫科", "strong",
         ("风湿", "关节痛", "关节肿", "狼疮", "红斑狼疮", "自身免疫", "血管炎", "噬血", "干燥综合征", "ANA", "抗核抗体"),
         signal="自身免疫/风湿表现 → 风湿免疫科"),
    Rule("rheum_multi", "风湿免疫科", "medium", ("皮疹", "口腔溃疡", "晨僵", "雷诺现象", "发热待查"),
         signal="多系统免疫相关表现 → 风湿免疫科"),
    # 发热查因 / 血象异常时，风湿免疫病是常规排查方向（规格书 §12-C 验收集）
    Rule("rheum_fever_workup", "风湿免疫科", "medium",
         all_of=("发热", "查因"),
         signal="发热查因 → 风湿免疫科（需排查自身免疫病）"),
    Rule("rheum_cytopenia", "风湿免疫科", "medium",
         any_of=("三系减少", "白细胞减少", "血小板减少", "血红蛋白低", "血象异常"),
         signal="血象减少 → 风湿免疫科（需排查免疫相关血细胞减少）"),

    # ===== 感染科 =====
    Rule("infect_fever", "感染科", "strong", ("发热", "高热", "体温", "畏寒", "寒战", "盗汗"),
         signal="发热 → 感染科（需排查感染源）"),
    Rule("infect_specific", "感染科", "medium", ("结核", "乙肝", "丙肝", "病毒", "细菌感染", "真菌", "寄生虫"),
         signal="感染性疾病 → 感染科"),

    # ===== 肿瘤科 =====
    Rule("onco_tumor", "肿瘤科", "strong", ("肿瘤", "癌", "占位", "结节", "新生物", "化疗", "放疗"),
         signal="肿瘤相关 → 肿瘤科"),
    Rule("onco_weightloss", "肿瘤科", "medium", all_of=("体重下降", "乏力"),
         signal="消瘦 + 乏力 → 肿瘤科（需排查消耗性疾病）"),

    # ===== 老年医学科 =====
    Rule("geri_elderly", "老年医学科", "weak", ("老年", "高龄", "行动不便"), signal="高龄患者 → 老年医学科"),

    # ===== 急诊科 / 重症（G1 之外的急诊相关）=====
    # 注意：本规则**不宜过宽**——"急性/加重"等在病历里极常见，若给 medium 会挤占名额，
    # 因此降为 weak（只在名额富余时作为"可咨询"入选）。
    Rule("er_acute", "急诊科", "weak", ("急性加重", "急性发作", "突发", "剧烈"),
         signal="急性起病/急性加重 → 急诊科（可咨询）"),
    Rule("icu_critical", "重症医学科", "medium", ("休克", "呼吸衰竭", "多器官", "重症"), signal="危重状态 → 重症医学科"),

    # ===== 外科系 =====
    Rule("gs_abdomen", "普通外科", "medium", ("阑尾炎", "胆囊", "疝", "甲状腺结节手术", "乳腺", "肛肠", "痔"),
         signal="普外相关疾病 → 普通外科"),
    Rule("hepatobiliary", "肝胆外科", "medium",
         ("胆结石", "胆囊", "胆道", "胆管", "肝占位", "胰腺", "肝胆"),
         signal="肝胆胰疾病 → 肝胆外科"),
    Rule("ortho_bone", "骨科", "medium", ("骨折", "腰痛", "颈椎", "关节置换", "骨质疏松", "腰椎间盘"),
         signal="骨关节问题 → 骨科"),
    Rule("ortho_trauma", "骨科", "medium", ("外伤", "扭伤", "摔伤", "车祸"), signal="外伤 → 骨科"),
    Rule("neurosur", "神经外科", "strong",
         ("颅内占位", "脑肿瘤", "脑出血", "蛛网膜下腔出血", "脊髓", "椎管内", "颅脑外伤", "脑积水", "开颅"),
         signal="颅内/脊髓病变 → 神经外科"),
    Rule("cards_heart_surgery", "心胸外科", "strong",
         ("心脏手术", "搭桥", "瓣膜", "房间隔缺损", "室间隔缺损", "先心病", "冠脉搭桥", "肺叶切除"),
         signal="心脏/胸腔手术 → 心胸外科"),
    Rule("urol", "泌尿外科", "medium", ("前列腺", "肾结石", "输尿管", "膀胱", "血尿伴疼痛", "尿路结石"),
         signal="泌尿系统疾病 → 泌尿外科"),
    Rule("vasc", "血管外科", "medium", ("动脉瘤", "静脉曲张", "下肢动脉", "深静脉血栓", "血管狭窄"),
         signal="血管疾病 → 血管外科"),
    Rule("plastic", "整形外科", "weak", ("瘢痕", "整形", "创面修复"), signal="整形修复 → 整形外科"),

    # ===== 手术/介入决策（评测 v0：原表完全缺失）=====
    Rule("surgery_decision", "心胸外科", "strong", pattern=r"(修补术|射频消融|冠脉造影|搭桥|瓣膜置换|支架植入|置换术)",
         signal="术式决策 → 心胸外科牵头"),
    Rule("surgery_general", "普通外科", "medium", pattern=r"(切除术|吻合|重建|开腹|腹腔镜)",
         signal="手术操作 → 普通外科"),
    Rule("anesthesia_periop", "麻醉科", "strong", ("麻醉评估", "术中", "围术期", "麻醉科会诊", "术前评估"),
         signal="围术期/麻醉评估 → 麻醉科"),
    Rule("ultrasound_exam", "超声科", "medium", ("超声", "彩超", "心超", "超声心动图", "B超"),
         signal="超声检查 → 超声科"),
    Rule("interventional", "介入科", "medium", ("介入治疗", "介入手术", "栓塞术", "射频消融术"),
         signal="介入操作 → 介入科"),

    # ===== 临床专科 =====
    Rule("ophth_vision", "眼科", "strong", ("视物模糊", "视力下降", "眼底", "视网膜", "眼痛", "白内障", "青光眼", "飞蚊"),
         signal="视力/眼底问题 → 眼科"),
    Rule("derm_rash", "皮肤科", "medium", ("皮疹", "瘙痒", "湿疹", "银屑病", "皮肤", "脱发"),
         signal="皮肤问题 → 皮肤科"),
    Rule("ent", "耳鼻喉科", "medium", ("耳鸣", "听力下降", "鼻塞", "咽痛", "扁桃体", "声音嘶哑", "打鼾",
                                      "鼻出血", "鼻咽", "流鼻血", "喉部", "声带", "耳痛", "鼻窦"),
         signal="耳鼻喉症状/出血 → 耳鼻喉科"),
    Rule("psych", "精神科", "medium",
         ("焦虑", "抑郁", "情绪低落", "情绪不稳", "失眠严重", "精神异常", "精神症状",
          "幻觉", "妄想", "躯体化"),
         # 不再用裸词"精神"：真实文本里"精神可/精神好/精神佳"是**查体正常描述**，
         # 会被误判成精神科问题（评测 v2 的 SB-A4 就是这么误报的）
         signal="精神心理问题 → 精神科"),
    Rule("rehab", "康复医学科", "strong",
         ("康复", "功能锻炼", "理疗", "吞咽训练", "运动疗法", "偏瘫康复", "术后康复"),
         signal="康复需求 → 康复医学科"),
    # 肿瘤综合治疗后 / 神经系统疾病 / 骨科术后：功能康复是常规协同方向
    Rule("rehab_post_treatment", "康复医学科", "medium",
         ("鼻咽癌", "综合治疗", "放疗后", "化疗后", "肿瘤治疗后", "术后功能", "肢体功能"),
         signal="治疗后功能康复需求 → 康复医学科"),
    Rule("rehab_neuro_ortho", "康复医学科", "medium",
         all_of=("神经", "功能"),
         signal="神经功能受损 → 康复医学科"),
    Rule("rehab_neurosurg", "康复医学科", "weak",
         ("脊髓", "颅内", "颅脑", "偏瘫", "截瘫", "肌力下降"),
         signal="神经系统病变 → 康复医学科（功能康复方向）"),
    Rule("obgyn", "妇产科", "medium", ("月经", "阴道出血", "妊娠", "怀孕", "子宫", "卵巢", "妇科", "孕期"),
         signal="妇科/妊娠相关 → 妇产科"),
    Rule("pediatric", "儿科", "medium", ("儿童", "患儿", "小儿", "婴儿", "婴幼儿"), signal="儿童患者 → 儿科"),
    # 评测 v2（SB-A4）：文本常写"男，4岁"而不是"儿童"；用负向后顾避免把"58岁"里的"8岁"当成儿童
    Rule("pediatric_age", "儿科", "medium", pattern=r"(?<![\d])(?:[1-9]|1[0-4])\s*岁",
         signal="14 岁以下患者 → 儿科"),
    Rule("neonatal", "新生儿科", "medium", ("新生儿", "早产儿"), signal="新生儿 → 新生儿科"),
    Rule("tcm", "中医科", "weak", ("中医", "中药调理", "体质"), signal="中医调理 → 中医科"),
    Rule("acupuncture", "针灸科", "weak", ("针灸",), signal="针灸 → 针灸科"),
)

# 手术/介入/围术期词（判定行为类型 D）
SURGERY_MARKERS: Tuple[str, ...] = (
    "手术", "术式", "修补术", "置换", "搭桥", "射频消融", "消融", "切除", "吻合",
    "造影", "支架", "介入", "围术期", "术前", "麻醉", "开腹", "腹腔镜", "胸腔镜",
)

# 疑难/多系统词（判定行为类型 C）
COMPLEX_MARKERS: Tuple[str, ...] = (
    "查因", "待查", "不明原因", "诊断不明", "多系统", "多器官", "复发", "反复",
    "三系减少", "发热待查", "疑难",
)

# 慢性病词（判定行为类型 B）
CHRONIC_MARKERS: Tuple[str, ...] = (
    "糖尿病", "高血压", "冠心病", "慢阻肺", "慢性肾病", "肝硬化", "心衰", "心力衰竭",
    "脑梗死", "房颤", "心房颤动", "甲状腺功能减退", "类风湿", "慢性",
)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

@dataclass
class RuleHit:
    """一条规则命中记录。"""

    rule_id: str
    department: str
    weight: str
    score: int
    signal: str
    matched_text: str

    def to_dict(self) -> Dict[str, object]:
        return {"rule_id": self.rule_id, "department": self.department,
                "weight": self.weight, "score": self.score,
                "signal": self.signal, "matched": self.matched_text}


@dataclass
class RuleEvaluation:
    """S1 的完整输出。"""

    hits: List[RuleHit] = field(default_factory=list)
    scores: Dict[str, int] = field(default_factory=dict)          # 科室 → 最高权重分
    signals: Dict[str, List[str]] = field(default_factory=dict)   # 科室 → 命中信号
    behavior_type: str = BEHAVIOR_A
    strong_departments: List[str] = field(default_factory=list)

    @property
    def departments(self) -> List[str]:
        """按分数降序的候选科室。"""
        return sorted(self.scores, key=lambda d: -self.scores[d])

    def lead_score(self, department: str) -> int:
        """科室牵头倾向（0-2），用于同分时决定牵头科。"""
        return DEFAULT_LEAD.get(department, 1)

    def to_dict(self) -> Dict[str, object]:
        return {
            "behavior_type": self.behavior_type,
            "departments": [{"department": d, "score": self.scores[d],
                             "signals": self.signals.get(d, [])} for d in self.departments],
            "strong": list(self.strong_departments),
            "hits": [h.to_dict() for h in self.hits],
        }


def evaluate(text: str) -> RuleEvaluation:
    """对病历文本执行 S1 规则匹配。"""
    result = RuleEvaluation()
    content = text or ""

    for rule in RULES:
        matched = rule.match(content)
        if not matched:
            continue
        score = WEIGHT_SCORE.get(rule.weight, 1)
        result.hits.append(RuleHit(rule_id=rule.rule_id, department=rule.department,
                                   weight=rule.weight, score=score,
                                   signal=rule.signal, matched_text=matched))
        # 科室得分取最高权重（不是累加，避免长文本堆分）
        result.scores[rule.department] = max(result.scores.get(rule.department, 0), score)
        result.signals.setdefault(rule.department, [])
        if rule.signal not in result.signals[rule.department]:
            result.signals[rule.department].append(rule.signal)

    result.strong_departments = [d for d, s in result.scores.items() if s == WEIGHT_SCORE["strong"]]
    result.behavior_type = infer_behavior(content, result)
    return result


def infer_behavior(text: str, evaluation: Optional[RuleEvaluation] = None) -> str:
    """判定行为类型 A-E（E 由 Guardrail G1 负责，此处不产出）。

    优先级：D（手术/介入） > C（疑难/多系统） > B（多病共病） > A（单科简单）
    """
    content = text or ""

    if any(m in content for m in SURGERY_MARKERS):
        return BEHAVIOR_D

    chronic_hits = sum(1 for m in CHRONIC_MARKERS if m in content)
    if any(m in content for m in COMPLEX_MARKERS) and chronic_hits >= 2:
        return BEHAVIOR_C
    if any(m in content for m in ("查因", "待查", "诊断不明", "诊断不清", "三系减少")):
        return BEHAVIOR_C
    if chronic_hits >= 2:
        return BEHAVIOR_B

    # 多系统受累（命中 ≥3 个不同专科的规则）也视为复杂
    if evaluation is not None and len(evaluation.scores) >= 3:
        return BEHAVIOR_B if chronic_hits >= 1 else BEHAVIOR_C

    return BEHAVIOR_A


def requires_mdt(behavior_type: str) -> bool:
    """是否需要多学科会诊（A 型不硬凑 MDT）。"""
    return behavior_type in (BEHAVIOR_B, BEHAVIOR_C, BEHAVIOR_D)


def rule_count() -> int:
    return len(RULES)


def audit_rules() -> List[Dict[str, object]]:
    """规则审计清单（答辩/文档用）。"""
    return [
        {"rule_id": r.rule_id, "department": r.department, "weight": r.weight,
         "keywords": (list(r.keywords) + list(r.all_of) + list(r.any_of)
                      + ([r.pattern] if r.pattern else [])),
         "signal": r.signal}
        for r in RULES
    ]


def validate_rule_table() -> List[str]:
    """自检：规则里的科室必须都在科室域内。返回问题列表。"""
    problems: List[str] = []
    for rule in RULES:
        if not is_department(rule.department):
            problems.append(f"{rule.rule_id} 引用了域外科室「{rule.department}」")
        if rule.weight not in WEIGHT_SCORE:
            problems.append(f"{rule.rule_id} 权重非法：{rule.weight}")
    return problems
