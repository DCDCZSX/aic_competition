"""模型结构化输出的容错解析（自研工具，零第三方依赖）。

**为什么需要它（真实故障记录）**：`deepseek-chat` 生成血液科意见卡时，输出被
`max_tokens=2000` 截断（`finish_reason=length`，3780 字符），`json.loads` 报
`Expecting ',' delimiter: line 64 column 6`，于是整个科室的意见卡被丢弃、页面上
永远显示"本次未完成分析"。截断只是模型输出缺陷的一种，常见还有：

- 全角标点（`，` `：`）当分隔符；
- 中文引号 `“”` 或未转义的内部英文引号（`"患者称"血糖高""`）；
- 字符串里的裸换行；
- 尾逗号、`//` 注释、Python 字面量（`True/False/None`）；
- 前面写了说明文字 / 用 ```json 包裹 / 一次输出两个对象。

本模块提供**三层递进**的处理，全部只做**格式层面**的修复，绝不编造内容：

1. `_scan_balanced`：字符串与转义感知地找出**配平**的 JSON 对象（而不是简单
   取首个 `{` 到末尾 `}` —— 后者在输出里还有别的花括号时会切错）；
2. `_normalize` + `_close_truncated`：修全角/引号/尾逗号/注释/裸换行/Python 字面量，
   被截断时回退到"最后一个完整成员"再补上收尾括号；
3. `salvage_fields`：仍然解析不了时，按字段正则把**已经完整写出**的字段抢救出来。

对外只用 `loads_lenient()`，返回 `(数据, 策略名)`；策略名可用于如实标注
"该结果经过格式修复"，避免把修复过的结果当成正常结果。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 策略名（调用方可用于提示"经过格式修复"）
STRATEGY_STRICT = "strict"            # 直接就是合法 JSON
STRATEGY_PY_LITERAL = "python_literal"  # Python 字面量（单引号 / True / None）
STRATEGY_REPAIRED = "repaired"        # 修复标点/引号/尾逗号等后解析成功
STRATEGY_TRUNCATED = "truncated_repaired"  # 被截断 → 回退到最后一个完整成员
STRATEGY_SALVAGE = "salvage"          # 逐字段抢救（只保证写出过的字段可用）

# 结构化输出的常见字段（抢救时用；调用方也可自带字段名）
COMMON_FIELDS = (
    "focus", "facts", "inferences", "checks", "information_gaps", "referral",
    "chief_complaint", "picks", "stance", "reason", "follow_up_evidence",
)

_STRUCTURAL_MAP = {
    "，": ",", "：": ":", "；": ";",
    "｛": "{", "｝": "}", "［": "[", "］": "]",
    "“": '"', "”": '"', "„": '"', "‟": '"', "＂": '"',
    "‘": "'", "’": "'",
}
_STRUCTURAL_RE = re.compile("|".join(map(re.escape, _STRUCTURAL_MAP)))
_LITERAL_RE = re.compile(r"\b(True|False|None)\b")
_TRAILING_COMMA_RE = re.compile(r",(\s*)([}\]])")


# ---------------------------------------------------------------------------
# 一层：字符串感知的扫描
# ---------------------------------------------------------------------------

def _terminates_string(rest: str) -> bool:
    """判断紧跟其后的内容是否像"字符串结束了"（后接 , } ] : 或已到末尾）。

    半角与**全角**分隔符都算——模型经常混用（`"a"，"b"`），若只认半角，
    就会把真正的结束引号误判成"内容里的引号"，整段被吞掉。
    """
    if re.match(r"\s*[,}\]:，：；｝］]", rest):
        return True
    # 到末尾了（被截断的最后一个值）也算结束
    return '"' not in rest


def _split_segments(text: str) -> List[Tuple[bool, str]]:
    """把文本切成 (是否字符串, 片段) 列表。

    内部引号（如 `"患者称"血糖高""`）会被判定为**字符串内容**并转义，
    这样后续的修复才有意义；转义与反斜杠按 JSON 规则处理。
    """
    segments: List[Tuple[bool, str]] = []
    buf: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != '"':
            buf.append(ch)
            i += 1
            continue
        # 进入字符串
        if buf:
            segments.append((False, "".join(buf)))
            buf = []
        i += 1
        body: List[str] = []
        closed = False
        while i < n:
            cur = text[i]
            if cur == "\\" and i + 1 < n:
                body.append(cur)
                body.append(text[i + 1])
                i += 2
                continue
            if cur == '"':
                if _terminates_string(text[i + 1:]):
                    i += 1
                    closed = True
                    break
                body.append('\\"')          # 内部引号 → 转义，不结束字符串
                i += 1
                continue
            if cur == "\n":
                body.append("\\n")
            elif cur == "\r":
                body.append("\\r")
            elif cur == "\t":
                body.append("\\t")
            elif ord(cur) < 0x20:
                body.append("\\u%04x" % ord(cur))
            else:
                body.append(cur)
            i += 1
        segments.append((True, '"' + "".join(body) + ('"' if closed else "")))
        # 注意：文本在这里被截断时**不补右引号**——否则半句话会被当成完整值入库
    if buf:
        segments.append((False, "".join(buf)))
    return segments


def _strip_comments(text: str) -> str:
    out = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _normalize(text: str) -> str:
    """格式修复：全角标点、内部引号、裸换行、尾逗号、注释、Python 字面量。"""
    pieces: List[str] = []
    for is_string, chunk in _split_segments(text):
        if is_string:
            pieces.append(chunk)
            continue
        chunk = _strip_comments(chunk)
        chunk = _STRUCTURAL_RE.sub(lambda m: _STRUCTURAL_MAP[m.group(0)], chunk)
        chunk = _LITERAL_RE.sub(lambda m: m.group(1).lower(), chunk)
        chunk = _TRAILING_COMMA_RE.sub(r"\2", chunk)
        pieces.append(chunk)
    return "".join(pieces)


def _scan_balanced(text: str, start_at: int = 0) -> Optional[str]:
    """返回第一个**配平**的 `{...}` 片段（字符串与转义感知）；找不到返回 None。"""
    start = text.find("{", start_at)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _cut_points(text: str) -> List[int]:
    """可安全切断的位置：结构闭合符 `}`/`]`，以及**真正结束字符串**的引号。

    刻意不包含"字符串中间的引号"——否则会把半句话补上引号当成完整值入库，
    那等于编造内容（例：`"focus": "全血细胞` 不能变成 `"focus": "全血细胞"`）。
    """
    positions: List[int] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                if _terminates_string(text[i + 1:]):
                    positions.append(i)
                    in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch in "}]":
            positions.append(i)
        i += 1
    return positions


def _close_truncated(text: str) -> Optional[str]:
    """被截断的 JSON：回退到最后一个**完整成员**，再补上未闭合的括号。

    做法：从后往前挑安全切断点，逐个尝试"切到这里 + 补齐括号"，
    第一个能解析的就是最长的可用前缀（只包含已经写完整的内容）。
    """
    if not text.startswith("{"):
        return None
    for cut in reversed(_cut_points(text)[-80:]):
        head = text[:cut + 1].rstrip().rstrip(",")
        stack: List[str] = []
        in_string = False
        escaped = False
        for ch in head:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        fixed = head + "".join(reversed(stack))
        try:
            data = json.loads(fixed)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return fixed
    return None


# ---------------------------------------------------------------------------
# 三层：逐字段抢救
# ---------------------------------------------------------------------------

def _unescape(value: str) -> str:
    try:
        return json.loads('"' + value.replace('"', '\\"') + '"')
    except json.JSONDecodeError:
        return value.replace('\\"', '"').replace("\\n", "\n")


def salvage_fields(text: str, keys: Iterable[str] = COMMON_FIELDS) -> Dict[str, Any]:
    """按字段把**已经完整写出**的内容抢救出来（不编造、缺失就是缺失）。"""
    found: Dict[str, Any] = {}
    for key in keys:
        if key in found:
            continue
        k = re.escape(key)
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % k, text, re.S)
        if m:
            found[key] = _unescape(m.group(1)).strip()
            continue
        if re.search(r'"%s"\s*:\s*null' % k, text):
            found[key] = None
            continue
        m = re.search(r'"%s"\s*:\s*\[(.*?)(?:\]|$)' % k, text, re.S)
        if m:
            body = m.group(1)
            items: List[Any] = []
            if "{" in body:
                # 对象数组（如 inferences）：**只取完整对象**，避免把键名当成条目塞进去
                for raw_item in re.finditer(r"\{[^{}]*\}", body, re.S):
                    piece = raw_item.group(0).rstrip().rstrip(",")
                    try:
                        item = json.loads(piece)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        items.append(item)
            else:
                # 字符串数组：取字符串，但排除"后面跟着冒号"的键名
                for raw_item in re.finditer(r'"((?:[^"\\]|\\.)*)"(?!\s*:)', body):
                    value = _unescape(raw_item.group(1)).strip()
                    if value:
                        items.append(value)
            found[key] = items
            continue
        m = re.search(r'"%s"\s*:\s*(true|false|-?\d+(?:\.\d+)?)' % k, text)
        if m:
            token = m.group(1)
            found[key] = {"true": True, "false": False}.get(token, token)
    return found


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def loads_lenient(raw: str, *,
                  fields: Iterable[str] = COMMON_FIELDS) -> Tuple[Optional[Dict[str, Any]], str]:
    """尽最大努力把模型输出解析成 dict；返回 `(数据, 策略)`，失败为 `(None, "")`。"""
    if not raw or not raw.strip():
        return None, ""
    text = raw.strip().lstrip("\ufeff")

    def try_strict(candidate: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def try_python(candidate: str) -> Optional[Dict[str, Any]]:
        try:
            data = ast.literal_eval(candidate)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
        return data if isinstance(data, dict) else None

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidates: List[str] = [text]
    if fence:
        candidates.insert(0, fence.group(1).strip())
    balanced = _scan_balanced(text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        data = try_strict(candidate)
        if data is not None:
            return data, STRATEGY_STRICT

    for candidate in candidates:
        data = try_python(candidate)
        if data is not None:
            return data, STRATEGY_PY_LITERAL
        fixed = _normalize(candidate)
        data = try_strict(fixed)
        if data is not None:
            return data, STRATEGY_REPAIRED

    for candidate in list(candidates) + [_normalize(c) for c in candidates]:
        closed = _close_truncated(candidate)
        if closed:
            data = try_strict(closed)
            if data is not None:
                return data, STRATEGY_TRUNCATED

    salvaged = salvage_fields(_normalize(text), fields)
    if salvaged:
        return salvaged, STRATEGY_SALVAGE
    return None, ""


def result_is_repaired(strategy: str) -> bool:
    """该结果是否经过格式修复（用于如实告知"这条结果被修复过"）。"""
    return strategy in (STRATEGY_REPAIRED, STRATEGY_TRUNCATED, STRATEGY_SALVAGE)
