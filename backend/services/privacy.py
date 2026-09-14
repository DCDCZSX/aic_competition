"""隐私脱敏与 PII 拦截（规格书 §4.4 G3、主文档 §5.3）。

职责：
1. `detect(text)`        —— 识别姓名/身份证/手机号/住址/病历号/医保号/邮箱/银行卡；
2. `desensitize(text)`   —— 返回"打码后的文本 + 打码明细（含打码原因）"，供患者端提交前预览；
3. `is_blocked(text)`    —— 对应 Guardrail **G3**：命中 PII 即阻断分析，提示清除。

安全约定：
- **绝不把命中原文写进日志**（`PIIHit.original` 仅用于本次预览，不落库、不打日志）；
- 脱敏不可逆（比赛版不提供"撤销打码"）；
- 原始资料与脱敏资料分开存储（主文档 §5.3），本模块只负责文本层处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Pattern, Tuple

# 类型 → 中文说明（用于前端"为什么打码"提示）
PII_LABELS: Dict[str, str] = {
    "id_card": "身份证号",
    "phone": "手机号",
    "name": "姓名",
    "address": "住址",
    "record_no": "病历号/住院号",
    "insurance": "医保号/社保号",
    "email": "邮箱",
    "bank_card": "银行卡号",
}

# 需要阻断分析的敏感类型（G3）：能直接定位到个人的标识
BLOCKING_KINDS: Tuple[str, ...] = ("id_card", "phone", "record_no", "insurance", "bank_card", "email")


@dataclass
class PIIHit:
    """一次 PII 命中。`original` 仅供当次预览，禁止落库/打日志。"""

    kind: str
    original: str
    masked: str
    start: int
    end: int
    rule: str = ""                      # 命中规则（姓名为 name_* 系列，便于前端解释"为什么打码"）

    @property
    def label(self) -> str:
        return PII_LABELS.get(self.kind, self.kind)

    @property
    def rule_label(self) -> str:
        return NAME_RULE_LABELS.get(self.rule, "") if self.rule else ""

    @property
    def reason(self) -> str:
        base = f"这是{self.label}，属于可直接识别个人身份的信息，已自动打码"
        if self.rule_label:
            return f"{base}（命中规则：{self.rule_label}）"
        return base

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_KINDS


@dataclass
class DesensitizeResult:
    """脱敏结果。"""

    masked_text: str
    hits: List[PIIHit] = field(default_factory=list)

    @property
    def has_pii(self) -> bool:
        return bool(self.hits)

    @property
    def blocking_hits(self) -> List[PIIHit]:
        return [h for h in self.hits if h.blocking]

    @property
    def blocked(self) -> bool:
        """是否触发 G3（命中需清除的敏感项）。"""
        return bool(self.blocking_hits)

    def summary(self) -> List[Dict[str, str]]:
        """给前端预览用的明细（**不含原文**）。"""
        return [
            {"kind": h.kind, "label": h.label, "masked": h.masked, "reason": h.reason}
            for h in self.hits
        ]


# ---------------------------------------------------------------------------
# 姓名识别（专用逻辑，不用"标签 + 可选冒号 + 任意 2-4 字"的粗暴正则）
# ---------------------------------------------------------------------------
# 曾经的缺陷：`(?:患者|病人|姓名|患者姓名)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})`
# 把冒号写成可选，于是正文里的「患者转运」「患者活动后」「患者出院」「患者反应」
# 都被当成姓名打码 —— 既误报，又会破坏送进分析链路的文本。
#
# 现在的判定顺序（先强后弱，全部候选都要过 `_looks_like_name` 姓氏校验）：
#   A 标签 + 冒号：`姓名：张三` / `患者：李某某，男` / `我叫王小明`
#   B 「姓氏 + 某/某某」：`张某` / `李某某`
#   C 姓氏 + 性别年龄上下文：`张某某，男，58岁`（无标签时的兜底）
# 任何一条不满足姓氏校验都不打码 —— 宁可漏，也不要把正常文字打掉。

# 常见单姓（百家姓常用部分）；不在表里的字一律不认为可能是名字
SINGLE_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双"
    "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
    "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空"
    "曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)

# 常见复姓（两字，命中即认为是名字开头）
COMPOUND_SURNAMES = frozenset((
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "梁丘", "左丘", "西门", "第五", "南荣",
    "东里", "仲长", "即墨", "达奚", "褚师", "子桑", "淳于", "单于", "叱干", "鲜卑",
))

# 明显不是名字的常见词（双保险；这些词经常出现在"患者___"或"___，男，58岁"位置）
NON_NAME_WORDS = frozenset((
    "患者 病人 家属 本人 姓名 神志 精神 发育 营养 体型 面容 体位 步态 转运 转至 "
    "转入 转出 活动 出院 入院 出现 反应 既往 家族 一般 情况 主诉 现病 查体 门诊 "
    "急诊 住院 手术 治疗 症状 体征 血压 心率 呼吸 体温 脉搏 检查 化验 影像 结果 "
    "诊断 处理 建议 随访 复查 儿童 老年 中年 青年 成人 男性 女性 不详 陪同 自述 "
    "未见 明显 双侧 左侧 右侧 上肢 下肢 全身 皮肤 黏膜 关节 肌肉 神经 心肺 腹部 "
    "肝脾 阳性 阴性 好转 加重 缓解 稳定 无 有 诉 男 女 岁 元 天 月 年"
).split())

# A 标签 + 冒号（冒号必填，避免"患者转运"这类正文被误判）；同时支持外文姓名
_NAME_LABELLED = re.compile(
    r"(?:姓名|患者姓名|病人姓名|姓\s*名|患者|病人|联系人|家属)\s*[:：]\s*"
    r"([\u4e00-\u9fa5·]{1,12}|[A-Za-z][A-Za-z .'\-]{1,30})"
)
# 自述式：我叫王小明 / 本人李某某 / 姓名是张三
_NAME_SELF = re.compile(
    r"(?:我叫|本人叫|本人是|姓名是|名字叫)\s*([\u4e00-\u9fa5·]{1,12}|[A-Za-z][A-Za-z .'\-]{1,30})"
)
# B 姓氏 + 某（张某 / 李某某）
_NAME_MOU = re.compile(r"(?<![\u4e00-\u9fa5])([\u4e00-\u9fa5])某{1,2}(?![\u4e00-\u9fa5])")
# C 姓氏 + 性别年龄上下文（张某某，男，58岁）
_NAME_AGE_CONTEXT = re.compile(
    r"(?<![\u4e00-\u9fa5])([\u4e00-\u9fa5·]{2,4})\s*[，,、]\s*(?:男|女)\s*[，,]?\s*\d{1,3}\s*岁"
)

NAME_RULE_LABELS: Dict[str, str] = {
    "name_labelled": "姓名标签（如「姓名：」「患者：」）",
    "name_self": "自述姓名（如「我叫」）",
    "name_mou": "「姓氏 + 某」写法（如「张某」）",
    "name_age_context": "姓氏 + 性别年龄上下文（如「张某某，男，58岁」）",
}


def _trim_candidate(raw: str) -> str:
    """把标签后面的候选串收窄成一个像名字的串。

    「患者：李某某，男，58岁」的正则只吃到中文，可能带上"男"等字；
    这里按性别/岁/常见非姓名字收尾裁剪，并把复姓一并考虑。外文名走另一条分支。
    """
    text = raw.strip("· ")
    if not text:
        return ""
    # 外文姓名：标签后直接整体保留（只做长度与形态校验）
    if text[0].isascii():
        latin = text.strip(" .'-")
        return latin if len(latin) >= 2 else ""
    # 遇到明显的非名字字（性别、年龄）就截断
    for stop in ("男", "女", "岁", "。"):
        idx = text.find(stop)
        if idx > 0:
            text = text[:idx]
    # 逐字收窄：从最长 5 字（含复姓）往下试，取第一个通过校验的
    for length in (5, 4, 3, 2):
        candidate = text[:length]
        if len(candidate) == length and _looks_like_name(candidate):
            return candidate
    return ""


def _looks_like_name(text: str) -> bool:
    """名字校验：2-5 个汉字（可含 ·），以常见姓氏开头，且不是常见非人名词。"""
    if not text or len(text) < 2 or len(text) > 5:
        return False
    if not re.fullmatch(r"[\u4e00-\u9fa5·]+", text):
        return False
    if text in NON_NAME_WORDS:
        return False
    body = text.replace("·", "")
    if len(set(body)) == 1:
        return False                      # 全是同一个字，不像名字
    for compound in COMPOUND_SURNAMES:
        if body.startswith(compound) and len(body) > len(compound):
            return True
    return body[0] in SINGLE_SURNAMES


def _find_names(text: str) -> List[Tuple[int, int, str, str]]:
    """返回 (start, end, 命中原文, 规则名)。"""
    found: List[Tuple[int, int, str, str]] = []

    for pattern, rule in ((_NAME_LABELLED, "name_labelled"),
                          (_NAME_SELF, "name_self")):
        for m in pattern.finditer(text):
            name = _trim_candidate(m.group(1))
            if name:
                start = m.start(1)
                found.append((start, start + len(name), name, rule))

    for m in _NAME_MOU.finditer(text):
        candidate = m.group(1) + "某" * (len(m.group(0)) - 1)
        if _looks_like_name(candidate):
            found.append((m.start(), m.end(), m.group(0), "name_mou"))

    for m in _NAME_AGE_CONTEXT.finditer(text):
        candidate = m.group(1)
        if _looks_like_name(candidate):
            found.append((m.start(1), m.end(1), candidate, "name_age_context"))

    return found


# ---------------------------------------------------------------------------
# 识别规则（按优先级：先长后短，避免身份证被手机号规则截断）
# ---------------------------------------------------------------------------

_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    # 身份证：18 位（末位可为 X）；先于手机号匹配
    ("id_card", re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")),
    # 银行卡：16-19 位连续数字（避免与身份证冲突，放其后）
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    # 手机号：11 位，1[3-9] 开头
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 邮箱
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # 病历号 / 住院号 / 门诊号 / 病案号
    ("record_no", re.compile(r"(?:病历号|住院号|门诊号|病案号|就诊号)\s*[:：]?\s*([A-Za-z0-9\-]{4,})")),
    # 医保号 / 社保号
    ("insurance", re.compile(r"(?:医保卡号|医保号|社保号|社保卡号)\s*[:：]?\s*([A-Za-z0-9\-]{6,})")),
    # 住址：省/市/区/县 + 路/街/号/弄/室
    ("address", re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:省|市|自治区)[\u4e00-\u9fa5]{0,10}(?:市|区|县|州)?"
        r"[\u4e00-\u9fa5]{0,12}(?:路|街|道|巷|弄)[\u4e00-\u9fa5\d]{0,8}号?"
        r"(?:[\u4e00-\u9fa5\d]{0,6}(?:弄|号楼|单元|室))?"
    )),
]

# 姓名掩码时把捕获组一起替换（保留前缀）
_GROUP_KINDS = {"record_no", "insurance"}


def _mask(kind: str, raw: str) -> str:
    """按类型打码：保留可读轮廓，抹掉可识别部分。"""
    if kind == "id_card":
        return f"{raw[:6]}{'*' * (len(raw) - 10)}{raw[-4:]}"
    if kind == "phone":
        return f"{raw[:3]}****{raw[-4:]}"
    if kind == "name":
        return raw[0] + "*" * (len(raw) - 1)
    if kind == "address":
        # 只保留省市前缀，其余打码
        head = raw[:3]
        return head + "*" * max(len(raw) - 3, 2)
    if kind == "email":
        local, _, domain = raw.partition("@")
        head = local[:1] if local else "*"
        return f"{head}***@{domain}"
    if kind == "bank_card":
        return f"{raw[:4]}{'*' * (len(raw) - 8)}{raw[-4:]}"
    if kind in ("record_no", "insurance"):
        return f"{raw[:2]}{'*' * max(len(raw) - 4, 2)}{raw[-2:]}"
    return "*" * len(raw)


def detect(text: str) -> List[PIIHit]:
    """识别文本中的 PII，按出现位置排序。同一区间只保留优先级最高的一条。"""
    if not text:
        return []

    hits: List[PIIHit] = []
    taken: List[Tuple[int, int]] = []

    # 姓名走专用逻辑（带姓氏校验），排在号码类规则之后、住址之前
    name_candidates = _find_names(text)
    numeric_patterns = [p for p in _PATTERNS if p[0] != "address"]
    address_patterns = [p for p in _PATTERNS if p[0] == "address"]

    def _take(kind: str, start: int, end: int, raw: str, rule: str = "") -> None:
        if any(not (end <= s or start >= e) for s, e in taken):
            return                        # 与已命中区间重叠 → 保留高优先级那条
        taken.append((start, end))
        hits.append(PIIHit(kind=kind, original=raw, masked=_mask(kind, raw),
                           start=start, end=end, rule=rule))

    for kind, pattern in numeric_patterns:
        for m in pattern.finditer(text):
            if kind in _GROUP_KINDS and m.groups():
                start, end, raw = m.span(1)[0], m.span(1)[1], m.group(1)
            else:
                start, end, raw = m.span()[0], m.span()[1], m.group(0)
            _take(kind, start, end, raw)

    for start, end, raw, rule in name_candidates:
        _take("name", start, end, raw, rule)

    for kind, pattern in address_patterns:
        for m in pattern.finditer(text):
            _take(kind, m.start(), m.end(), m.group(0))

    hits.sort(key=lambda h: h.start)
    return hits


def desensitize(text: str) -> DesensitizeResult:
    """返回打码后的文本与打码明细（从后往前替换，避免位移错乱）。"""
    hits = detect(text)
    if not hits:
        return DesensitizeResult(masked_text=text or "", hits=[])

    masked_text = text
    for hit in sorted(hits, key=lambda h: h.start, reverse=True):
        masked_text = masked_text[: hit.start] + hit.masked + masked_text[hit.end:]

    return DesensitizeResult(masked_text=masked_text, hits=hits)


def is_blocked(text: str) -> bool:
    """是否触发 G3（命中需清除的敏感项 → 阻断分析并提示清除）。"""
    return desensitize(text).blocked


def mask_for_display(text: str) -> str:
    """便捷方法：只要打码后的文本。"""
    return desensitize(text).masked_text
