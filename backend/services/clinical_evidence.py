"""Conservative evidence availability shared by extraction and gap updates."""
import re
from typing import Tuple

_UNAVAILABLE = re.compile(
    r"未(?:做|查|行|进行|完成|提供|完善|检查|复查|拿到|取到|收到|出|获取)|"
    r"没(?:有)?(?:做|查|提供)|缺乏|缺如|不详|未知|暂缺|缺失|"
    r"待(?:做|查|检查|完成|完善|出|报告)|预约|计划|拟(?:做|查|行)|准备(?:做|查)|"
    r"没有[^，。；]{0,10}(?:结果|报告|记录)|"
    r"(?:结果|报告).{0,6}(?:未(?!见)|没|暂无|暂缺)|(?:暂无|无)(?:检查)?(?:结果|报告|资料)"
)
_RESULT = re.compile(r"\d+(?:\.\d+)?|[+＋]+|[:：]\s*\S+|阳性|阴性|正常|异常|未见|可见|提示|显示|报告|已提供|已上传|已提交")


def information_provided(text: str, keywords: Tuple[str, ...], *, require_result: bool = False) -> bool:
    text = str(text or "").lower()
    for sentence in re.split(r"[。；;\n]", text):
        clauses = re.split(r"[，,]", sentence)
        for index, clause in enumerate(clauses):
            mentioned = [str(keyword).lower() for keyword in keywords if str(keyword).lower() in clause]
            if not mentioned:
                continue
            if _UNAVAILABLE.search(clause):
                continue
            following = clauses[index + 1] if index + 1 < len(clauses) else ""
            # A completed exam with a pending result still leaves a result gap.
            if re.match(r"\s*(?:检查)?(?:结果|报告)", following) and _UNAVAILABLE.search(following):
                continue
            results = [clause[clause.find(keyword) + len(keyword):] for keyword in mentioned]
            if not require_result or any(_RESULT.search(result) for result in results) or (
                    following and not _UNAVAILABLE.search(following) and _RESULT.search(following)):
                return True
    return False
