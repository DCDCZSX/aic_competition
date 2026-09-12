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

    @property
    def label(self) -> str:
        return PII_LABELS.get(self.kind, self.kind)

    @property
    def reason(self) -> str:
        return f"这是{self.label}，属于可直接识别个人身份的信息，已自动打码"

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
    # 姓名：上下文前缀 + 2-4 个中文字
    ("name", re.compile(r"(?:患者|病人|姓名|患者姓名)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})")),
    # 住址：省/市/区/县 + 路/街/号/弄/室
    ("address", re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:省|市|自治区)[\u4e00-\u9fa5]{0,10}(?:市|区|县|州)?"
        r"[\u4e00-\u9fa5]{0,12}(?:路|街|道|巷|弄)[\u4e00-\u9fa5\d]{0,8}号?"
        r"(?:[\u4e00-\u9fa5\d]{0,6}(?:弄|号楼|单元|室))?"
    )),
]

# 姓名掩码时把捕获组一起替换（保留前缀）
_GROUP_KINDS = {"record_no", "insurance", "name"}


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

    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if kind in _GROUP_KINDS and m.groups():
                start, end = m.span(1)
                raw = m.group(1)
            else:
                start, end = m.span()
                raw = m.group(0)

            if any(not (end <= s or start >= e) for s, e in taken):
                continue  # 与已命中区间重叠，跳过（保留先匹配的高优先级类型）
            taken.append((start, end))
            hits.append(PIIHit(kind=kind, original=raw, masked=_mask(kind, raw),
                               start=start, end=end))

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
