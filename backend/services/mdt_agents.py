from .ai_service import AIService
from .department_domain import SPECIALISTS
from typing import List, Dict

class MDTAgents:
    def __init__(self):
        self.ai_service = AIService()
        # 科室域以 services/department_domain.py 为**唯一权威源**（规格书 §4.2）：
        # 原先进程内硬编码的 34 科字典已移除，改为引用权威源（含新增的超声科/肝胆外科/介入科）。
        self.specialists: Dict[str, str] = dict(SPECIALISTS)

    def controller_agent(self, medical_record: str, main_issue: str) -> List[str]:
        """主控智能体 - 全面分析病历并决定需要哪些专科参与"""
        prompt = f"""
        请作为MDT会诊的主控医生，全面分析以下患者病历和主要会诊问题，识别所有可能相关的专科医生。

        患者病历：
        {medical_record}

        【主要会诊问题】：{main_issue}

        可选专科列表：{list(self.specialists.keys())}

        分析要求：
        1. **重点关注主要会诊问题**：优先选择能直接回答或解决主要问题的专科
        2. 仔细分析患者的所有症状、体征、检查结果和既往病史
        3. 考虑疾病的复杂性和多系统影响
        4. 识别直接相关专科（与主要问题直接相关）
        5. 识别间接相关专科（可能受影响或需要排除的）
        6. 考虑并发症和合并症的可能性
        7. 确保能够全面回答主要会诊问题

        请选择3-8个相关专科，按对主要问题的重要性排序。
        
        返回JSON格式：
        {{
            "selected_specialties": ["specialty1", "specialty2", ...],
            "primary_specialties": ["对主要问题最重要的2-3个专科"],
            "secondary_specialties": ["次要但相关的专科"],
            "reasoning": "详细的选择理由，说明每个专科对回答主要会诊问题的必要性"
        }}
        """
        
        role_desc = "你是一位经验丰富的MDT会诊主控医生，具有全科医学背景，能够识别疾病的多系统影响，特别擅长围绕核心问题选择最合适的专科组合。"
        
        response = self.ai_service.call_ai_api(prompt, role_desc)
        
        if response:
            json_data = self.ai_service.extract_json_from_text(response)
            if json_data and "selected_specialties" in json_data:
                # 过滤有效的专科
                valid_specialties = [
                    spec for spec in json_data["selected_specialties"] 
                    if spec in self.specialists
                ]
                if valid_specialties and len(valid_specialties) >= 3:
                    return valid_specialties
        
        # 如果解析失败，使用更全面的默认专科列表
        return self._get_comprehensive_default_specialties(medical_record, main_issue)

    def _get_comprehensive_default_specialties(self, medical_record: str, main_issue: str) -> List[str]:
        """根据关键词匹配获取全面的默认专科列表"""
        default_specialties = set()
        
        # 关键词映射
        keyword_mapping = {
            # 心血管相关
            "心脏|胸痛|心律|血压|心肌|冠心病": ["cardiology"],
            # 神经相关
            "头痛|眩晕|意识|神经|脑|中风|癫痫": ["neurology"],
            # 呼吸相关
            "咳嗽|气短|胸闷|肺|呼吸": ["pulmonology"],
            # 消化相关
            "腹痛|恶心|呕吐|腹泻|胃|肠|肝": ["gastroenterology"],
            # 内分泌相关
            "糖尿病|甲状腺|血糖|激素": ["endocrinology"],
            # 肾脏相关
            "肾|尿|水肿|蛋白尿": ["nephrology"],
            # 血液相关
            "贫血|出血|血小板|白细胞": ["hematology"],
            # 感染相关
            "发热|感染|炎症": ["infectious_disease"],
            # 肿瘤相关
            "肿瘤|癌|肿块|转移": ["oncology"],
            # 外科相关
            "手术|外伤|骨折": ["general_surgery", "orthopedics"],
            # 精神相关
            "抑郁|焦虑|精神|睡眠": ["psychiatry"],
            # 皮肤相关
            "皮疹|瘙痒|皮肤": ["dermatology"],
            # 妇科相关
            "月经|妊娠|妇科": ["obstetrics_gynecology"],
            # 儿科相关
            "儿童|小儿|婴儿": ["pediatrics"]
        }
        
        text_to_analyze = f"{medical_record} {main_issue}".lower()
        
        import re
        for keywords, specialties in keyword_mapping.items():
            if re.search(keywords, text_to_analyze):
                default_specialties.update(specialties)
        
        # 确保至少有基础专科
        if len(default_specialties) < 3:
            default_specialties.update(["cardiology", "neurology", "general_surgery"])
        
        return list(default_specialties)[:8]  # 限制最多8个专科

    def specialist_agent(self, medical_record: str, main_issue: str, specialty: str) -> Dict:
        """专科智能体 - 针对主要问题进行专科分析"""
        specialty_name = self.specialists.get(specialty, specialty)
        
        # 根据专科类型定制分析重点
        specialty_focus = self._get_specialty_focus(specialty)
        
        prompt = f"""
        你是一位资深的{specialty_name}专家，请针对以下患者的情况和主要会诊问题进行专业分析。

        患者病历：
        {medical_record}

        【主要会诊问题】：{main_issue}

        专科分析重点：
        {specialty_focus}

        请从{specialty_name}的角度，**重点围绕主要会诊问题**进行分析，提供：

        1. **针对主要问题的专科观点**
           - 从{specialty_name}角度如何理解和解答主要会诊问题
           - 本专科对该问题的具体建议和处理方案

        2. **专科相关性评估**
           - 本专科与主要会诊问题的相关程度（高/中/低）
           - 相关性判断依据

        3. **专科诊断分析**
           - 与主要问题相关的{specialty_name}诊断
           - 需要排除的相关疾病

        4. **针对性检查建议**
           - 为解决主要问题需要的{specialty_name}相关检查
           - 检查的优先级和必要性

        5. **治疗建议**
           - 针对主要问题的{specialty_name}治疗方案
           - 治疗的时机和注意事项

        6. **风险评估与预后**
           - 与主要问题相关的{specialty_name}风险
           - 预后判断和监测要点

        **请确保所有分析都紧密围绕主要会诊问题展开，提供具体、实用的专科意见。**
        如果本专科与主要问题相关性较低，请简要说明原因并提供必要的排除性意见。
        """
        
        role_desc = f"你是一位具有丰富临床经验的{specialty_name}专家，能够准确评估专科与主要问题的相关性，围绕核心问题提供专业、实用的诊疗建议。"
        
        analysis = self.ai_service.call_ai_api(prompt, role_desc)
        
        return {
            "specialty": specialty,
            "specialty_name": specialty_name,
            "analysis": analysis or f"{specialty_name}分析失败，请重试",
            "timestamp": self._get_timestamp(),
            "relevance": self._extract_relevance(analysis) if analysis else "unknown"
        }

    def coordinator_agent(self, medical_record: str, main_issue: str, specialist_analyses: List[Dict]) -> str:
        """整合智能体 - 综合各专科意见形成最终诊断"""
        
        # 按相关性分类专科意见
        high_relevance = []
        medium_relevance = []
        low_relevance = []
        
        for analysis in specialist_analyses:
            relevance = analysis.get('relevance', 'medium')
            if relevance == 'high':
                high_relevance.append(analysis)
            elif relevance == 'low':
                low_relevance.append(analysis)
            else:
                medium_relevance.append(analysis)
        
        # 构建分层的专科意见摘要
        analyses_summary = self._build_layered_summary(high_relevance, medium_relevance, low_relevance)
        
        # 统计参与专科
        total_specialties = len(specialist_analyses)
        specialty_names = [analysis['specialty_name'] for analysis in specialist_analyses]
        
        prompt = f"""
        作为MDT会诊的总协调医生，请综合以下信息，**重点回答主要会诊问题**，形成最终的诊疗方案。

        患者病历：
        {medical_record}

        【核心会诊问题】：{main_issue}

        参与会诊专科统计：
        - 总计：{total_specialties}个专科
        - 专科列表：{', '.join(specialty_names)}

        各专科医生意见（按相关性分层）：
        {analyses_summary}

        **整合要求**：
        1. **核心问题回答**：必须明确、直接地回答主要会诊问题
        2. **全面性**：确保所有{total_specialties}个专科的相关意见都被考虑
        3. **层次性**：重点整合高相关性专科意见，适当参考其他专科观点
        4. **实用性**：提供可执行的具体解决方案

        请按以下结构提供MDT会诊结论：

        ## 【主要会诊问题的MDT答案】
        **问题**：{main_issue}
        **MDT综合答案**：（整合各专科意见，给出明确、具体的回答）

        ## 【综合诊断分析】
        - 主要诊断（结合多专科意见）
        - 诊断依据和鉴别要点
        - 各专科诊断的一致性分析

        ## 【针对性治疗方案】
        - 针对主要问题的综合治疗策略
        - 各专科协同治疗计划
        - 治疗优先级和时序安排

        ## 【多学科协作计划】
        - 各专科在解决主要问题中的具体分工
        - 协作机制和沟通安排
        - 关键决策节点

        ## 【检查监测方案】
        - 为解决主要问题必需的检查
        - 监测指标和评估标准
        - 复查时间安排

        ## 【风险管理与预后】
        - 与主要问题相关的风险评估
        - 预防措施和应急预案
        - 预后判断和改善策略

        ## 【随访建议】
        - 短期随访重点（围绕主要问题）
        - 长期管理计划
        - 疗效评估标准

        **请确保整个MDT结论都紧密围绕主要会诊问题展开，体现多学科协作的优势，给出明确可行的解决方案。**
        """
        
        role_desc = "你是一位经验丰富的MDT协调专家，擅长整合多学科意见，能够围绕核心问题统筹全局，形成针对性强、协调统一、实用可行的诊疗方案。"
        
        conclusion = self.ai_service.call_ai_api(prompt, role_desc)
        
        # 添加参与专科统计信息
        specialty_summary = f"\n\n【本次MDT会诊参与专科】\n共{total_specialties}个专科参与：{', '.join(specialty_names)}\n针对问题：{main_issue}"
        
        return (conclusion or "整合分析失败，请重试") + specialty_summary

    def _build_layered_summary(self, high_relevance: List[Dict], medium_relevance: List[Dict], low_relevance: List[Dict]) -> str:
        """构建分层的专科意见摘要"""
        summary_parts = []
        
        if high_relevance:
            summary_parts.append("【高相关性专科意见】")
            for analysis in high_relevance:
                summary_parts.append(f"■ {analysis['specialty_name']}：\n{analysis['analysis']}\n")
        
        if medium_relevance:
            summary_parts.append("【中等相关性专科意见】")
            for analysis in medium_relevance:
                summary_parts.append(f"● {analysis['specialty_name']}：\n{analysis['analysis']}\n")
        
        if low_relevance:
            summary_parts.append("【低相关性专科意见】")
            for analysis in low_relevance:
                summary_parts.append(f"○ {analysis['specialty_name']}：\n{analysis['analysis']}\n")
        
        return "\n".join(summary_parts)

    def _get_timestamp(self) -> str:
        """获取当前时间戳"""
        import datetime
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _get_specialty_focus(self, specialty: str) -> str:
        """获取各专科的分析重点"""
        focus_map = {
            "cardiology": "心血管系统疾病、心律失常、心力衰竭、冠心病、高血压等",
            "neurology": "神经系统疾病、脑血管病、癫痫、神经退行性疾病等",
            "pulmonology": "呼吸系统疾病、肺部感染、慢阻肺、哮喘、肺栓塞等",
            "gastroenterology": "消化系统疾病、胃肠道疾病、肝胆疾病、营养问题等",
            "endocrinology": "内分泌代谢疾病、糖尿病、甲状腺疾病、激素异常等",
            "nephrology": "肾脏疾病、水电解质平衡、尿路疾病、透析相关问题等",
            "hematology": "血液系统疾病、贫血、出血性疾病、血液肿瘤等",
            "oncology": "恶性肿瘤、肿瘤筛查、抗肿瘤治疗、肿瘤并发症等",
            "infectious_disease": "感染性疾病、抗生素使用、免疫缺陷、发热查因等",
            "rheumatology": "风湿免疫性疾病、关节炎、自身免疫病、结缔组织病等",
            "psychiatry": "精神心理疾病、情绪障碍、认知功能、睡眠障碍等",
            "emergency": "急危重症、多器官功能障碍、急救处理、生命支持等",
            "icu": "重症监护、器官支持、危重症管理、生命体征监测等",
            "general_surgery": "外科疾病、手术适应症、围手术期管理、创伤处理等",
            "orthopedics": "骨骼肌肉系统疾病、骨折、关节疾病、运动损伤等"
        }
        return focus_map.get(specialty, f"{self.specialists.get(specialty, specialty)}相关疾病的诊断和治疗")

    def _extract_relevance(self, analysis: str) -> str:
        """从分析中提取相关性等级"""
        if not analysis:
            return "unknown"
        
        analysis_lower = analysis.lower()
        if "相关程度" in analysis_lower:
            if "高" in analysis_lower:
                return "high"
            elif "中" in analysis_lower:
                return "medium"
            elif "低" in analysis_lower:
                return "low"
        
        # 根据关键词判断
        high_keywords = ["急需", "重要", "关键", "必须", "优先"]
        medium_keywords = ["建议", "考虑", "可能", "需要关注"]
        low_keywords = ["相关性较低", "暂无明显", "不是主要"]
        
        if any(keyword in analysis_lower for keyword in high_keywords):
            return "high"
        elif any(keyword in analysis_lower for keyword in low_keywords):
            return "low"
        elif any(keyword in analysis_lower for keyword in medium_keywords):
            return "medium"
        
        return "medium"  # 默认中等相关性







