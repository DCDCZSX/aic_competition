from dataclasses import dataclass
from typing import List, Dict

@dataclass
class Agent:
    name: str
    specialty: str
    role: str
    
    def generate_opinion(self, patient_info: Dict, main_issue: str, previous_discussions: List[str]) -> str:
        # 基于主要问题生成意见
        prompt = f"""
        作为{self.specialty}专家，请针对以下主要问题提供专业意见：
        
        主要问题：{main_issue}
        患者信息：{patient_info}
        之前的讨论：{previous_discussions}
        
        请提供具体的诊疗建议。
        """
        # 这里应该调用LLM API，现在先返回模拟数据
        return f"[{self.name}] 针对'{main_issue}'的专业建议：基于患者的病史和症状，建议进行进一步检查和治疗..."

