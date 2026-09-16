"""Guardrail 输入侧拦截：G1 危象 + G6 非医学内容（规格书 §4.1/§4.4）。

**G1（危象拦截，全系统唯一触发器）**
- 词表集中维护、可审计（每条规则带中文名与分类），内置 ≥40 条 + 组合规则；
- 触发后：**阻断分诊与 MDT → 提示立即联系急救/急诊 → 不生成任何分析与报告 → 记录触发依据**；
- 明确声明：**未触发拦截不代表不存在急症**，现实症状与专业医疗判断优先（主文档 §6.1）。

**G6（非医学内容）**：闲聊/广告/试题等 → 礼貌拒绝并说明用途。

用法：
    result = screen(text)
    if result.blocked:
        return crisis_page(result.advice, result.reasons)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

# 触发后统一话术（规格书 §4.4 G1）
CRISIS_ADVICE = (
    "疑似急症：请立即拨打 120 或马上前往最近医院急诊科。"
    "本系统不处理急症，也不会为这种情况生成分析或报告。"
)
CRISIS_DISCLAIMER = "未触发本提示不代表不存在急症；现实症状与专业医疗判断优先。"

NON_MEDICAL_REPLY = (
    "抱歉，本系统只用于整理就医信息与多学科分析辅助，"
    "不处理闲聊、广告或试题类内容。请提供病历或就医相关问题。"
)


@dataclass(frozen=True)
class CrisisRule:
    """一条危象规则（可审计：词 → 理由 → 分类）。"""

    label: str                                   # 规则中文名（审计清单用）
    category: str                                # 分类
    keywords: Tuple[str, ...] = ()               # 任一命中即触发
    all_of: Tuple[str, ...] = ()                 # 组合规则：需全部出现
    reason: str = ""                             # 为什么要拦（审计说明）

    def match(self, text: str) -> bool:
        if self.all_of:
            return all(k in text for k in self.all_of)
        return any(k in text for k in self.keywords)


# ---------------------------------------------------------------------------
# 危象规则表（48 条，含组合规则）
# ---------------------------------------------------------------------------

CRISIS_RULES: Tuple[CrisisRule, ...] = (
    # ---- 心血管急症 ----
    CrisisRule("胸痛伴大汗", "心血管", all_of=("胸痛", "大汗"),
               reason="急性冠脉综合征典型表现"),
    CrisisRule("持续胸痛不缓解", "心血管", ("持续胸痛", "胸痛不缓解", "压榨性胸痛"),
               reason="可能为急性心肌梗死"),
    CrisisRule("胸痛放射痛", "心血管", ("放射至左肩", "放射到左肩", "胸痛放射", "后背痛伴胸痛"),
               reason="心肌缺血放射痛"),
    CrisisRule("晕厥伴心悸", "心血管", ("晕厥", "晕倒", "突然倒地"),
               reason="可能为恶性心律失常"),
    CrisisRule("高血压危象", "心血管", ("血压极高", "血压 200", "高血压危象", "血压230", "血压220"),
               reason="高血压急症，可致靶器官损害"),
    CrisisRule("脉搏测不到", "心血管", ("脉搏微弱", "测不到脉搏", "摸不到脉搏"),
               reason="休克/循环衰竭征象"),
    CrisisRule("下肢剧痛伴苍白", "心血管", ("下肢剧痛伴苍白", "肢体冰冷发紫"),
               reason="急性动脉栓塞可能"),

    # ---- 神经急症 ----
    CrisisRule("意识障碍", "神经", ("意识障碍", "意识不清", "意识模糊", "嗜睡叫不醒", "叫不醒",
                                  "呼之不应", "昏迷", "意识水平下降"),
               reason="中枢神经系统急症"),
    CrisisRule("突发言语不能", "神经", ("说不出话", "言语不能", "失语", "口齿不清突发"),
               reason="急性脑卒中可能"),
    CrisisRule("口角歪斜伴肢体无力", "神经", all_of=("口角歪斜", "无力"),
               reason="急性脑卒中典型表现"),
    CrisisRule("突发肢体瘫痪", "神经", ("突发瘫痪", "一侧肢体无力", "半身不遂"),
               reason="急性脑血管事件"),
    # 评测 v2（SB-E1）发现的漏拦：原词表只认"剧烈头痛突发/雷击样头痛"，而
    # "突发剧烈头痛"（同义不同语序）与单纯的"剧烈头痛"都没被拦住 —— 这是**安全漏洞**，
    # 按规格书 §风险筛查的保守策略，宁可多拦（提示急诊），不可漏拦。
    CrisisRule("雷击样头痛", "神经", ("雷击样头痛", "剧烈头痛突发", "突发剧烈头痛",
                                  "剧烈头痛", "最严重的头痛", "爆炸样头痛"),
               reason="蛛网膜下腔出血可能"),
    CrisisRule("抽搐不止", "神经", ("抽搐不止", "癫痫持续", "持续抽搐", "全身抽搐"),
               reason="癫痫持续状态，需急诊处理"),
    CrisisRule("颈项强直伴高热", "神经", all_of=("颈项强直", "发热"),
               reason="中枢神经系统感染可能"),
    CrisisRule("突发视力丧失", "神经", ("突然看不见", "视力骤降", "突发失明"),
               reason="视网膜或脑血管急症"),

    # ---- 呼吸急症 ----
    CrisisRule("严重呼吸困难", "呼吸", ("严重呼吸困难", "呼吸困难加重", "喘不上气", "憋气严重"),
               reason="呼吸衰竭风险"),
    CrisisRule("端坐呼吸", "呼吸", ("端坐呼吸", "不能平卧"),
               reason="心衰/重度呼吸困难的体位表现"),
    CrisisRule("紫绀", "呼吸", ("口唇发紫", "面色青紫", "紫绀"),
               reason="严重缺氧"),
    CrisisRule("窒息", "呼吸", ("窒息", "异物卡喉", "气道梗阻"),
               reason="气道阻塞，危及生命"),
    CrisisRule("大咯血", "呼吸", ("大咯血", "咯血不止", "咯血200", "咯血500"),
               reason="可能窒息或失血性休克"),

    # ---- 出血 / 外伤 ----
    CrisisRule("大出血", "出血", ("大出血", "出血不止", "失血性休克"),
               reason="循环容量快速丢失"),
    CrisisRule("呕血", "出血", ("呕血", "吐鲜血"),
               reason="上消化道大出血"),
    CrisisRule("便血伴头晕", "出血", all_of=("便血", "头晕"),
               reason="下消化道大出血可能"),
    CrisisRule("产后大出血", "出血", ("产后大出血", "产后出血不止"),
               reason="产科急症"),
    CrisisRule("严重外伤", "外伤", ("车祸", "高处坠落", "严重外伤", "重物压伤"),
               reason="多发伤需急诊评估"),
    CrisisRule("颅脑外伤伴呕吐", "外伤", all_of=("头部外伤", "呕吐"),
               reason="颅内出血风险"),
    CrisisRule("大面积烧伤", "外伤", ("大面积烧伤", "烧伤面积"),
               reason="休克与感染风险"),
    CrisisRule("板状腹", "急腹症", ("板状腹", "腹部板状"),
               reason="消化道穿孔/腹膜炎"),
    CrisisRule("剧烈腹痛伴休克", "急腹症", ("剧烈腹痛伴冷汗", "腹痛伴面色苍白"),
               reason="急腹症合并循环不稳"),
    CrisisRule("睾丸剧痛", "急腹症", ("睾丸剧痛", "睾丸突然疼痛"),
               reason="睾丸扭转需数小时内处理"),

    # ---- 中毒 / 过敏 ----
    CrisisRule("药物过量", "中毒", ("药物过量", "服药过量", "吃了一把药", "过量服药"),
               reason="急性中毒风险"),
    CrisisRule("误服农药/化学品", "中毒", ("误服农药", "喝了农药", "误服化学", "喝了洁厕"),
               reason="急性中毒，需洗胃/解毒"),
    CrisisRule("一氧化碳中毒", "中毒", ("一氧化碳", "煤气中毒", "炭火取暖后头晕"),
               reason="一氧化碳中毒可致死"),
    CrisisRule("喉头水肿", "过敏", ("喉头水肿", "喉咙肿"),
               reason="可致气道梗阻"),
    CrisisRule("过敏性休克", "过敏", ("过敏性休克", "严重过敏反应"),
               reason="需立即肾上腺素急救"),
    CrisisRule("毒蛇/毒虫咬伤", "中毒", ("毒蛇咬伤", "被蛇咬", "毒虫咬伤"),
               reason="需抗毒血清"),
    CrisisRule("酒精中毒昏迷", "中毒", all_of=("醉酒", "昏迷"),
               reason="急性酒精中毒风险"),

    # ---- 精神危机 ----
    CrisisRule("自杀风险", "精神", ("自杀", "不想活", "轻生", "结束生命", "自尽"),
               reason="自伤风险，需危机干预"),
    CrisisRule("自伤行为", "精神", ("自伤", "割腕", "自残"),
               reason="自伤风险，需危机干预"),
    CrisisRule("伤人风险", "精神", ("想杀人", "伤害他人", "报复杀人"),
               reason="伤人风险，需危机干预"),
    CrisisRule("严重幻觉伴冲动", "精神", ("幻觉严重", "听到声音让我", "控制不住想动手"),
               reason="精神科急症"),

    # ---- 特殊人群 / 其他高危 ----
    CrisisRule("孕妇腹痛伴出血", "产科", all_of=("孕妇", "出血"),
               reason="异位妊娠/胎盘早剥可能"),
    CrisisRule("胎动消失", "产科", ("胎动消失", "感觉不到胎动"),
               reason="胎儿窘迫可能"),
    CrisisRule("儿童高热伴抽搐", "儿科", all_of=("高热", "抽搐"),
               reason="热性惊厥/中枢感染"),
    CrisisRule("糖尿病急性并发症", "内分泌", ("血糖极高伴意识", "酮症酸中毒", "血糖测不出"),
               reason="糖尿病急性代谢紊乱"),
    CrisisRule("突发无尿", "泌尿", ("突然无尿", "24小时无尿", "一天没有小便"),
               reason="急性肾损伤/尿路梗阻"),
    CrisisRule("新生儿异常", "儿科", ("新生儿黄疸加重", "新生儿不吃奶", "新生儿发热"),
               reason="新生儿病情变化快"),
)


# ---------------------------------------------------------------------------
# G6：非医学内容特征
# ---------------------------------------------------------------------------

_NON_MEDICAL_MARKERS: Tuple[str, ...] = (
    "加微信", "扫二维码", "优惠", "促销", "打折", "代理", "下单", "包邮", "带货",
    "试题", "考试题", "选择题答案", "帮我写作文", "写一篇", "翻译一下",
    "讲个笑话", "聊天", "天气怎么样", "股票", "彩票",
)

# 医学语料特征词（用于判断"像不像医疗内容"）
_MEDICAL_MARKERS: Tuple[str, ...] = (
    "症状", "主诉", "病史", "诊断", "检查", "化验", "CT", "MRI", "超声", "心电图",
    "血常规", "用药", "疼痛", "发热", "咳嗽", "头晕", "乏力", "恶心", "呕吐",
    "医院", "医生", "门诊", "住院", "治疗", "复查", "剂量",
)


@dataclass
class ScreenResult:
    """输入侧 Guardrail 结果。"""

    blocked: bool = False
    code: str = ""                       # G1 / G6 / ""
    advice: str = ""
    reasons: List[str] = field(default_factory=list)   # 命中的规则名（可审计）
    categories: List[str] = field(default_factory=list)
    is_crisis: bool = False
    is_non_medical: bool = False

    @property
    def disclaimer(self) -> str:
        return CRISIS_DISCLAIMER if self.is_crisis else ""


def detect_crisis(text: str) -> List[CrisisRule]:
    """返回命中的危象规则（可能多条）。"""
    if not text:
        return []
    return [rule for rule in CRISIS_RULES if rule.match(text)]


def looks_non_medical(text: str) -> bool:
    """G6：是否像非医学内容（广告/闲聊/试题）。"""
    if not text:
        return False
    stripped = text.strip()
    hit_marker = any(m in stripped for m in _NON_MEDICAL_MARKERS)
    if not hit_marker:
        return False
    # 同时缺少任何医学特征词，才判定为非医学（避免误伤夹杂广告的真实病历）
    has_medical = any(m in stripped for m in _MEDICAL_MARKERS)
    return not has_medical


def screen(text: str) -> ScreenResult:
    """输入侧统一入口：先 G1（危象，优先级最高），再 G6（非医学）。"""
    result = ScreenResult()

    crisis = detect_crisis(text or "")
    if crisis:
        result.blocked = True
        result.is_crisis = True
        result.code = "G1"
        result.advice = CRISIS_ADVICE
        result.reasons = [f"{r.label}（{r.category}）：{r.reason}" for r in crisis]
        result.categories = sorted({r.category for r in crisis})
        return result

    if looks_non_medical(text or ""):
        result.blocked = True
        result.is_non_medical = True
        result.code = "G6"
        result.advice = NON_MEDICAL_REPLY
        result.reasons = ["命中非医学内容特征（广告/闲聊/试题）"]
        return result

    return result


def crisis_rule_count() -> int:
    """危象规则条数（规格书要求 ≥40，单测与审计用）。"""
    return len(CRISIS_RULES)


def audit_list() -> List[dict]:
    """可审计清单：词 → 理由 → 分类（规格书 §4.1 要求）。"""
    return [
        {"label": r.label, "category": r.category,
         "keywords": list(r.keywords) + [f"ALL:{k}" for k in r.all_of],
         "reason": r.reason}
        for r in CRISIS_RULES
    ]
