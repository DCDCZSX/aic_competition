from .mdt_agents import MDTAgents
import sys
import os

# 添加models目录到路径
models_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
sys.path.insert(0, models_path)

from discussion import Discussion
from typing import Dict, List
import threading
import time

class MDTService:
    def __init__(self):
        self.agents = MDTAgents()
        self.active_discussions = {}

    def start_consultation(self, patient_data: Dict, main_issue: str) -> str:
        """启动MDT会诊"""
        discussion = Discussion(
            patient_info=patient_data,
            main_issue=main_issue
        )
        
        discussion_id = str(id(discussion))
        self.active_discussions[discussion_id] = discussion
        
        # 启动异步会诊流程
        thread = threading.Thread(
            target=self._conduct_consultation_async,
            args=(discussion_id,)
        )
        thread.daemon = True
        thread.start()
        
        return discussion_id

    def _conduct_consultation_async(self, discussion_id: str):
        """异步执行会诊流程"""
        discussion = self.active_discussions.get(discussion_id)
        if not discussion:
            return

        try:
            # 步骤1: 主控智能体分析
            discussion.add_message("系统", "MDT会诊开始，主控医生正在分析病历...")
            
            selected_specialties = self.agents.controller_agent(
                discussion.patient_info['medical_history'],
                discussion.main_issue
            )
            
            discussion.add_message(
                "主控医生", 
                f"经过分析，本次会诊将邀请以下专科参与：{', '.join([self.agents.specialists[s] for s in selected_specialties])}"
            )
            
            # 步骤2: 各专科智能体分析
            specialist_analyses = []
            
            for specialty in selected_specialties:
                specialty_name = self.agents.specialists[specialty]
                discussion.add_message("系统", f"{specialty_name}专家正在分析...")
                
                analysis = self.agents.specialist_agent(
                    discussion.patient_info['medical_history'],
                    discussion.main_issue,
                    specialty
                )
                
                specialist_analyses.append(analysis)
                discussion.add_message(
                    f"{specialty_name}专家",
                    analysis['analysis']
                )
                
                # 模拟思考时间
                time.sleep(1)
            
            # 步骤3: 整合智能体综合分析
            discussion.add_message("系统", "正在整合各专科意见，形成最终诊疗方案...")
            
            final_conclusion = self.agents.coordinator_agent(
                discussion.patient_info['medical_history'],
                discussion.main_issue,
                specialist_analyses
            )
            
            discussion.set_conclusion(final_conclusion)
            discussion.add_message("MDT协调医生", f"【最终会诊结论】\n\n{final_conclusion}")
            
            discussion.status = "completed"
            discussion.add_message("系统", "MDT会诊完成")
            
        except Exception as e:
            discussion.status = "error"
            discussion.add_message("系统", f"会诊过程中出现错误: {str(e)}")

    def get_discussion_status(self, discussion_id: str) -> Dict:
        """获取会诊状态"""
        discussion = self.active_discussions.get(discussion_id)
        if not discussion:
            return {"status": "not_found", "message": "会诊不存在"}
        
        return {
            "status": discussion.status,
            "messages": discussion.messages,
            "main_issue": discussion.main_issue,
            "patient_info": discussion.patient_info,
            "conclusion": discussion.conclusion
        }

    def get_all_discussions(self) -> List[Dict]:
        """获取所有会诊记录"""
        return [
            {
                "id": discussion_id,
                "patient_name": discussion.patient_info.get('name', '未知'),
                "main_issue": discussion.main_issue,
                "status": discussion.status,
                "created_at": discussion.created_at
            }
            for discussion_id, discussion in self.active_discussions.items()
        ]





