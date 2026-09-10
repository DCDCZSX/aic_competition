import pandas as pd
import re
import time
import json
from typing import Dict, List, Optional
import sys
import os

# 添加backend路径到sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from services.mdt_service import MDTService
from services.mdt_agents import MDTAgents

class BatchMDTProcessor:
    def __init__(self):
        self.mdt_service = MDTService()
        self.agents = MDTAgents()
        
    def extract_patient_info(self, medical_record: str) -> Dict[str, str]:
        """从病历中提取患者基本信息"""
        # 提取性别
        gender = "未知"
        if "男性" in medical_record or "男，" in medical_record:
            gender = "男性"
        elif "女性" in medical_record or "女，" in medical_record:
            gender = "女性"
        
        # 提取年龄
        age = "未知"
        age_pattern = r'(\d+)岁'
        age_match = re.search(age_pattern, medical_record)
        if age_match:
            age = age_match.group(1)
        
        # 生成患者姓名（使用序号）
        name = f"患者{int(time.time()) % 10000}"
        
        return {
            "name": name,
            "age": age,
            "gender": gender,
            "medical_history": medical_record
        }
    
    def process_single_case(self, row_index: int, medical_record: str, main_issue: str) -> Dict:
        """处理单个病例"""
        print(f"\n{'='*60}")
        print(f"正在处理第 {row_index + 1} 个病例...")
        print(f"病情摘要: {medical_record[:100]}...")
        print(f"讨论目的: {main_issue[:50]}...")
        
        try:
            # 提取患者信息
            patient_info = self.extract_patient_info(medical_record)
            
            # 1. 主控智能体选择专科
            print("步骤1: 主控智能体分析...")
            selected_specialties = self.agents.controller_agent(medical_record, main_issue)
            selected_specialty_names = [self.agents.specialists.get(spec, spec) for spec in selected_specialties]
            
            # 2. 各专科分析
            print("步骤2: 各专科医生分析...")
            specialist_results = []
            specialist_summaries = []
            
            for specialty in selected_specialties:
                specialty_name = self.agents.specialists.get(specialty, specialty)
                print(f"  - {specialty_name}分析中...")
                
                analysis = self.agents.specialist_agent(medical_record, main_issue, specialty)
                specialist_results.append(analysis)
                
                # 生成简化结论（50-100字）
                summary = self._summarize_specialist_opinion(analysis['analysis'], specialty_name)
                specialist_summaries.append(f"{specialty_name}: {summary}")
            
            # 3. 整合分析
            print("步骤3: 整合最终结论...")
            final_conclusion = self.agents.coordinator_agent(medical_record, main_issue, specialist_results)
            
            # 生成最终结论摘要（50-200字）
            final_summary = self._summarize_final_conclusion(final_conclusion)
            
            result = {
                "selected_specialties": "; ".join(selected_specialty_names),
                "specialist_conclusions": "; ".join(specialist_summaries),
                "final_conclusion": final_summary,
                "status": "成功"
            }
            
            print(f"✅ 第 {row_index + 1} 个病例处理完成")
            return result
            
        except Exception as e:
            print(f"❌ 第 {row_index + 1} 个病例处理失败: {str(e)}")
            return {
                "selected_specialties": "处理失败",
                "specialist_conclusions": f"错误: {str(e)}",
                "final_conclusion": "处理失败",
                "status": "失败"
            }
    
    def _summarize_specialist_opinion(self, analysis: str, specialty_name: str) -> str:
        """将专科意见总结为50-100字，重点关注对主要问题的回答"""
        # 清理格式
        summary = analysis.replace('\n', ' ').strip()
        summary = re.sub(r'\s+', ' ', summary)
        
        # 尝试提取关键观点，优先提取与主要问题相关的内容
        key_phrases = []
        
        # 查找关键句子（包含诊断、建议、治疗等关键词）
        sentences = summary.split('。')
        for sentence in sentences:
            if any(keyword in sentence for keyword in ['建议', '诊断', '治疗', '考虑', '需要', '应该', '可以']):
                key_phrases.append(sentence.strip())
                if len('。'.join(key_phrases)) >= 80:
                    break
        
        if key_phrases:
            result = '。'.join(key_phrases)
            if not result.endswith('。'):
                result += '。'
        else:
            # 如果没找到关键句子，直接截取前部分
            result = summary[:90] + '。' if len(summary) > 90 else summary
        
        return result[:100] if result else f"{specialty_name}已完成分析"
    
    def _summarize_final_conclusion(self, conclusion: str) -> str:
        """将最终结论总结为50-200字，重点保留对主要问题的回答"""
        # 清理格式
        summary = conclusion.replace('\n', ' ').strip()
        summary = re.sub(r'\s+', ' ', summary)
        
        # 尝试提取MDT答案部分
        mdt_answer_match = re.search(r'MDT综合答案.*?(?=##|$)', summary)
        if mdt_answer_match:
            mdt_answer = mdt_answer_match.group(0)
            # 清理标记
            mdt_answer = re.sub(r'\*\*.*?\*\*', '', mdt_answer)
            mdt_answer = mdt_answer.replace('MDT综合答案', '').strip()
            if mdt_answer and len(mdt_answer) <= 200:
                return mdt_answer[:200]
        
        # 如果没找到MDT答案，提取关键诊疗信息
        key_parts = []
        current_length = 0
        
        sentences = summary.split('。')
        for sentence in sentences:
            # 优先选择包含关键信息的句子
            if any(keyword in sentence for keyword in ['诊断', '治疗', '建议', '方案', '需要', '应该']):
                if current_length + len(sentence) <= 180:
                    key_parts.append(sentence.strip())
                    current_length += len(sentence)
                else:
                    break
        
        if key_parts:
            result = '。'.join(key_parts)
            if not result.endswith('。'):
                result += '。'
            return result[:200]
        
        # 最后的备选方案：直接截取
        return summary[:200] + '。' if len(summary) > 200 else summary
    
    def process_csv_file(self, input_file: str, output_file: str):
        """处理整个CSV文件"""
        print(f"🏥 批量MDT会诊处理系统")
        print(f"📁 输入文件: {input_file}")
        print(f"📁 输出文件: {output_file}")
        
        try:
            # 读取CSV文件
            df = pd.read_csv(input_file, encoding='utf-8')
            print(f"📊 共找到 {len(df)} 个病例")
            
            # 检查必要的列
            required_columns = ['病情介绍', '讨论目的']
            missing_columns = [col for col in required_columns if col not in df.columns]
            if missing_columns:
                raise ValueError(f"CSV文件缺少必要的列: {missing_columns}")
            
            # 添加新列
            df['主控体选择专科'] = ''
            df['各专科结论'] = ''
            df['最终整合结论'] = ''
            
            # 处理每一行
            for index, row in df.iterrows():
                medical_record = str(row['病情介绍'])
                main_issue = str(row['讨论目的'])
                
                # 跳过空行
                if pd.isna(medical_record) or pd.isna(main_issue) or not medical_record.strip():
                    print(f"跳过第 {index + 1} 行（数据为空）")
                    continue
                
                # 处理单个病例
                result = self.process_single_case(index, medical_record, main_issue)
                
                # 更新DataFrame
                df.at[index, '主控体选择专科'] = result['selected_specialties']
                df.at[index, '各专科结论'] = result['specialist_conclusions']
                df.at[index, '最终整合结论'] = result['final_conclusion']
                
                # 每处理5个病例保存一次（防止数据丢失）
                if (index + 1) % 5 == 0:
                    df.to_csv(output_file, index=False, encoding='utf-8-sig')
                    print(f"💾 已保存前 {index + 1} 个病例的结果")
                
                # 添加延迟避免API限制
                time.sleep(2)
            
            # 最终保存
            df.to_csv(output_file, index=False, encoding='utf-8-sig')
            print(f"\n🎉 批量处理完成！")
            print(f"📁 结果已保存到: {output_file}")
            print(f"📊 共处理 {len(df)} 个病例")
            
        except Exception as e:
            print(f"❌ 处理过程中出现错误: {str(e)}")
            raise

def main():
    """主函数"""
    input_file = "MDT-5.csv"
    output_file = "MDT-5-test.csv"
    
    if not os.path.exists(input_file):
        print(f"❌ 找不到输入文件: {input_file}")
        return
    
    processor = BatchMDTProcessor()
    processor.process_csv_file(input_file, output_file)

if __name__ == "__main__":
    main()
