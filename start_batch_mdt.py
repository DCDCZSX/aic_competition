#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

def main():
    """批量MDT处理启动脚本"""

    # Windows 中文控制台默认 GBK：emoji 会触发 UnicodeEncodeError，统一切到 UTF-8
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    try:
        print("🏥 批量MDT智能会诊系统")
        print("=" * 50)
        print("正在启动批量处理...")
        
        # 检查必要文件
        required_files = [
            'MDT-5.csv',
            'backend/services/ai_service.py',
            'backend/services/mdt_agents.py',
            'backend/services/mdt_service.py',
            'backend/models/discussion.py'
        ]
        
        missing_files = []
        for file_path in required_files:
            if not os.path.exists(file_path):
                missing_files.append(file_path)
        
        if missing_files:
            print("❌ 缺少以下必要文件:")
            for file in missing_files:
                print(f"   - {file}")
            return
        
        print("✅ 文件检查完成")
        
        # 导入并启动批处理
        from batch_mdt_processor import main as batch_main
        
        print("✅ 批处理模块加载成功")
        print("=" * 50)
        print("🚀 开始批量处理MDT案例...")
        print("📍 输入文件: MDT-5.csv")
        print("📍 输出文件: MDT-5-test.csv")
        print("=" * 50)
        
        # 启动批处理
        batch_main()
        
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断处理")
        print("已处理的数据已保存")
    except Exception as e:
        print(f"\n❌ 启动失败: {str(e)}")
        print("请检查文件路径和依赖项")

if __name__ == "__main__":
    main()