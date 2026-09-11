from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import sys
import os

# 加载 .env（python-dotenv 为可选依赖：未安装时仅使用系统环境变量）
try:
    from dotenv import load_dotenv, find_dotenv

    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 修改为绝对导入
from services.mdt_service import MDTService
import time

app = Flask(__name__)
CORS(app)

mdt_service = MDTService()

@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')

@app.route('/api/start_consultation', methods=['POST'])
def start_consultation():
    try:
        data = request.json
        
        # 验证必需字段
        required_fields = ['name', 'age', 'gender', 'medical_history', 'main_issue']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'error': f'缺少必需字段: {field}'}), 400
        
        patient_data = {
            'name': data['name'],
            'age': int(data['age']),
            'gender': data['gender'],
            'medical_history': data['medical_history']
        }
        
        discussion_id = mdt_service.start_consultation(patient_data, data['main_issue'])
        
        return jsonify({
            'status': 'started',
            'discussion_id': discussion_id,
            'message': 'MDT会诊已启动'
        })
        
    except Exception as e:
        return jsonify({'error': f'启动会诊失败: {str(e)}'}), 500

@app.route('/api/get_discussion_status/<discussion_id>', methods=['GET'])
def get_discussion_status(discussion_id):
    try:
        status = mdt_service.get_discussion_status(discussion_id)
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': f'获取状态失败: {str(e)}'}), 500

@app.route('/api/get_all_discussions', methods=['GET'])
def get_all_discussions():
    try:
        discussions = mdt_service.get_all_discussions()
        return jsonify({'discussions': discussions})
    except Exception as e:
        return jsonify({'error': f'获取讨论列表失败: {str(e)}'}), 500

if __name__ == '__main__':
    # 绑定与调试开关走环境变量：默认只监听本机、关闭 debug（演示/提交要求）
    host = os.getenv('APP_HOST', '127.0.0.1').strip()
    port = int(os.getenv('APP_PORT', '5000'))
    debug = os.getenv('APP_DEBUG', 'false').strip().lower() in ('1', 'true', 'yes')

    print("=" * 60)
    print("🏥 多学科就医智助系统启动中...")
    print(f"📍 访问地址: http://{host}:{port}")
    print(f"🔧 调试模式: {'开启（仅限本地开发）' if debug else '关闭'}")
    print(f"⚡ 使用模型: {os.getenv('AI_MODEL_NAME', 'qwen-max-latest')}")
    if not os.getenv('AI_API_KEY'):
        print("⚠️  未配置 AI_API_KEY（参考 .env.example），AI 分析将不可用")
    print("🛑 按 Ctrl+C 停止服务")
    print("=" * 60)
    app.run(debug=debug, host=host, port=port)



