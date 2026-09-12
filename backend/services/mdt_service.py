from .mdt_agents import MDTAgents
from .store import Store
import sys
import os

# 添加models目录到路径
models_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
sys.path.insert(0, models_path)

from discussion import Discussion
from typing import Dict, List, Optional
import threading
import time

class MDTService:
    def __init__(self, store: Optional[Store] = None):
        self.agents = MDTAgents()
        self.active_discussions = {}
        # 会话落盘（任务 0.5）：初始化失败时降级为纯内存，不影响服务启动
        if store is not None:
            self.store: Optional[Store] = store
        else:
            try:
                self.store = Store()
            except Exception as exc:  # pragma: no cover - 取决于运行环境
                print(f"[MDTService] 警告：SQLite 落盘不可用（{exc}），本次运行仅使用内存会话。")
                self.store = None

    # ------------------------------------------------------------------ 落盘辅助
    def _persist_start(self, case_id: str, patient_data: Dict, main_issue: str) -> None:
        if not self.store:
            return
        self.store.save_case(
            case_id,
            patient_name=str(patient_data.get('name', '') or ''),
            main_issue=main_issue,
            status="running",
            case_state="analyzing",
            payload=patient_data,
        )
        self.store.add_audit(case_id, "consultation_started", f"主要问题：{main_issue}")
        self.store.save_transition(case_id, "pending_materials", "analyzing",
                                   actor="system", reason="提交会诊")

    def _persist_finish(self, case_id: str) -> None:
        if not self.store:
            return
        self.store.update_case(case_id, status="completed", case_state="awaiting_doctor")
        self.store.save_transition(case_id, "analyzing", "awaiting_doctor",
                                   actor="system", reason="AI 分析完成，待医生审核")
        self.store.add_audit(case_id, "consultation_completed", "AI 分析完成，待医生审核")

    def _persist_error(self, case_id: str, error: Exception) -> None:
        """异常处理：留痕 + 转人工复核/补资料，**不静默**（规格书 §7.1）。"""
        if not self.store:
            return
        self.store.update_case(case_id, status="error", case_state="need_more_info")
        self.store.save_transition(case_id, "analyzing", "need_more_info",
                                   actor="system",
                                   reason=f"分析异常：{error}", is_anomaly=True)
        self.store.add_audit(case_id, "analysis_anomaly", str(error))

    def start_consultation(self, patient_data: Dict, main_issue: str) -> str:
        """启动MDT会诊"""
        discussion = Discussion(
            patient_info=patient_data,
            main_issue=main_issue
        )
        
        discussion_id = str(id(discussion))
        self.active_discussions[discussion_id] = discussion
        self._persist_start(discussion_id, patient_data, main_issue)
        
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
            self._persist_finish(discussion_id)
            
        except Exception as e:
            discussion.status = "error"
            discussion.add_message("系统", f"会诊过程中出现错误: {str(e)}")
            self._persist_error(discussion_id, e)

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
        """获取所有会诊记录（内存活动会话 + 落盘历史合并，重启后仍可查）。"""
        merged: Dict[str, Dict] = {}

        # 1) 落盘的历史记录（含进程重启前的）
        if self.store:
            try:
                for case in self.store.list_cases():
                    merged[case["case_id"]] = {
                        "id": case["case_id"],
                        "patient_name": case.get("patient_name") or "未知",
                        "main_issue": case.get("main_issue") or "",
                        "status": case.get("status") or "",
                        "case_state": case.get("case_state") or "",
                        "report_version": case.get("report_version", 1),
                        "created_at": case.get("created_at") or "",
                    }
            except Exception as exc:  # pragma: no cover
                print(f"[MDTService] 读取历史会诊失败：{exc}")

        # 2) 内存中的活动会话（覆盖同 id 的最新状态）
        for discussion_id, discussion in self.active_discussions.items():
            current = merged.get(discussion_id, {})
            merged[discussion_id] = {
                "id": discussion_id,
                "patient_name": discussion.patient_info.get('name', '未知'),
                "main_issue": discussion.main_issue,
                "status": discussion.status,
                "case_state": current.get("case_state", ""),
                "report_version": current.get("report_version", 1),
                "created_at": discussion.created_at,
            }

        return list(merged.values())

    def start_consultation_sync(self, patient_data: Dict, main_issue: str) -> Dict:
        """同步执行MDT会诊（用于批处理）"""
        try:
            # 步骤1: 主控智能体分析
            selected_specialties = self.agents.controller_agent(
                patient_data['medical_history'],
                main_issue
            )
            
            # 步骤2: 各专科智能体分析
            specialist_analyses = []
            
            for specialty in selected_specialties:
                analysis = self.agents.specialist_agent(
                    patient_data['medical_history'],
                    main_issue,
                    specialty
                )
                specialist_analyses.append(analysis)
            
            # 步骤3: 整合智能体综合分析
            final_conclusion = self.agents.coordinator_agent(
                patient_data['medical_history'],
                main_issue,
                specialist_analyses
            )
            
            return {
                'status': 'completed',
                'selected_specialties': selected_specialties,
                'specialist_analyses': specialist_analyses,
                'final_conclusion': final_conclusion
            }
            
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e)
            }

