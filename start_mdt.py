#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MDT智能会诊系统启动脚本
"""

import sys
import os

def main():
    """主启动函数"""
    # 获取项目根目录
    project_root = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(project_root, 'backend')
    
    # 检查backend目录是否存在
    if not os.path.exists(backend_dir):
        print("❌ 错误：找不到backend目录")
        return
    
    # 添加路径到Python搜索路径
    sys.path.insert(0, backend_dir)
    sys.path.insert(0, os.path.join(backend_dir, 'models'))
    sys.path.insert(0, os.path.join(backend_dir, 'services'))
    
    # 切换工作目录
    os.chdir(backend_dir)
    
    try:
        print("🏥 MDT智能会诊系统")
        print("=" * 50)
        print("正在启动系统...")
        
        # 检查必要文件
        required_files = [
            'app.py',
            'services/ai_service.py',
            'services/mdt_agents.py',
            'services/mdt_service.py',
            'models/discussion.py',
            'templates/index.html'
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
        
        # 导入并启动应用
        from app import app
        
        print("✅ 应用加载成功")
        print("=" * 50)
        print("🚀 系统启动成功!")
        print("📍 访问地址: http://localhost:5000")
        print("🔧 调试模式: 开启")
        print("🛑 按 Ctrl+C 停止服务")
        print("=" * 50)
        
        # 启动Flask应用
        app.run(debug=True, host='0.0.0.0', port=5000)
        
    except ImportError as e:
        print(f"❌ 导入错误: {e}")
        print("请检查Python依赖是否已安装:")
        print("pip install -r backend/requirements.txt")
    except KeyboardInterrupt:
        print("\n👋 系统已停止")
    except Exception as e:
        print(f"❌ 启动失败: {e}")

if __name__ == '__main__':
    main()
