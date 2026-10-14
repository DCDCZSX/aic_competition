"""模型提示词里的病例正文长度控制。

病例正文上限：首次提交 8000 字，追问与补充资料合并后 16000 字
（见 intake.MAX_TEXT_LENGTH、update_service.MAX_MERGED_HISTORY_LENGTH）。
画像、辅助选科、专科分析和质询都按这个上限读取完整正文，不再只截取前
2500–4000 字，追加在末尾的新检查结果也会进入模型提示。

如果将来上限被调大，超出部分按"保留开头 + 保留最新追加内容"处理，
并在中间明确标注省略字数，不会悄悄丢掉尾部资料。
"""

from __future__ import annotations

MODEL_CASE_TEXT_LIMIT = 16000

_HEAD_RATIO = 0.4


def case_text_for_prompt(text: str, limit: int = MODEL_CASE_TEXT_LIMIT) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker_tpl = "\n……（中间省略 {n} 字，开头与最近补充的资料已完整保留）……\n"
    marker = marker_tpl.format(n=len(text))
    budget = max(0, limit - len(marker))
    head = int(budget * _HEAD_RATIO)
    tail = budget - head
    omitted = len(text) - head - tail
    return text[:head] + marker_tpl.format(n=omitted) + (text[-tail:] if tail else "")
