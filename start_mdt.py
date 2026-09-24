#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MDT智能会诊系统启动脚本
"""

import sys
import os

try:
    from dotenv import load_dotenv, find_dotenv
except ImportError:  # python-dotenv 为可选依赖
    load_dotenv = None
    find_dotenv = None

def main():
    """主启动函数"""
    # 获取项目根目录
    project_root = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(project_root, 'backend')
    
    # 检查backend目录是否存在
    if not os.path.exists(backend_dir):
        print("[错误] 找不到backend目录")
        return
    
    # 添加路径到Python搜索路径
    sys.path.insert(0, backend_dir)
    sys.path.insert(0, os.path.join(backend_dir, 'models'))
    sys.path.insert(0, os.path.join(backend_dir, 'services'))
    
    # 切换工作目录
    os.chdir(backend_dir)

    # Windows 中文控制台默认 GBK：emoji 会触发 UnicodeEncodeError 导致启动失败，
    # 统一把标准输出切到 UTF-8（失败则忽略）。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    # 加载 .env（可选依赖；从 backend/ 向上查找 CHAIS/.env 或 backend/.env）
    if load_dotenv is not None:
        load_dotenv(find_dotenv(usecwd=True))
    
    try:
        print("[启动] 多学科就医智助系统")
        print("=" * 50)
        print("正在启动系统...")
        
        # 检查必要文件
        required_files = [
            'app.py',
            'services/ai_service.py',
            'services/mdt_agents.py',
            'services/mdt_service.py',
            'models/discussion.py',
            'templates/home.html',
            'templates/patient.html'
        ]
        
        missing_files = []
        for file_path in required_files:
            if not os.path.exists(file_path):
                missing_files.append(file_path)
        
        if missing_files:
            print("[错误] 缺少以下必要文件:")
            for file in missing_files:
                print(f"   - {file}")
            return
        
        print("[OK] 文件检查完成")
        
        # 导入并启动应用
        from app import app

        # 绑定与调试开关走环境变量：默认只监听本机、关闭 debug（演示/提交要求）
        host = os.getenv('APP_HOST', '127.0.0.1').strip()
        port = int(os.getenv('APP_PORT', '5000'))
        debug = os.getenv('APP_DEBUG', 'false').strip().lower() in ('1', 'true', 'yes')

        print("[OK] 应用加载成功")
        print("=" * 50)
        print("[启动] 系统启动成功!")
        print(f"[首页] http://{host}:{port}/")
        print(f"[患者端] http://{host}:{port}/patient")
        print(f"[医生端] http://{host}:{port}/doctor")
        print(f"[模式] 调试={'开启（仅限本地开发）' if debug else '关闭'}")
        if not os.getenv('AI_API_KEY'):
            print("[提示] 未配置 AI_API_KEY（参考 .env.example），AI 分析将不可用")
        print("[停止] 按 Ctrl+C")
        print("=" * 50)

        # 启动Flask应用
        app.run(debug=debug, host=host, port=port)
        
    except ImportError as e:
        print(f"[错误] 导入错误: {e}")
        print("请检查Python依赖是否已安装:")
        print("pip install -r backend/requirements.txt")
    except KeyboardInterrupt:
        print("\n[停止] 系统已退出")
    except Exception as e:
        print(f"[错误] 启动失败: {e}")

if __name__ == '__main__':
    main()
