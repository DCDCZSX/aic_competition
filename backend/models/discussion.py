from datetime import datetime
from typing import List, Dict, Optional

class Discussion:
    def __init__(self, patient_info: Dict, main_issue: str):
        self.patient_info = patient_info
        self.main_issue = main_issue
        self.messages: List[Dict] = []
        self.status = "started"  # started, in_progress, completed, error
        self.conclusion: Optional[str] = None
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add_message(self, agent: str, content: str):
        """添加消息"""
        self.messages.append({
            "agent": agent,
            "content": content,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        })
        
        if agent != "系统":
            self.status = "in_progress"

    def set_conclusion(self, conclusion: str):
        """设置最终结论"""
        self.conclusion = conclusion

    def get_conclusion(self) -> Optional[str]:
        """获取最终结论"""
        return self.conclusion

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "patient_info": self.patient_info,
            "main_issue": self.main_issue,
            "messages": self.messages,
            "status": self.status,
            "conclusion": self.conclusion,
            "created_at": self.created_at
        }


