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
from services.auth import AuthService, AuthError, ROLE_DOCTOR, auth_required, extract_token
from services.case_flow import CaseFlow
from services.store import Store
import time

app = Flask(__name__)
CORS(app)

mdt_service = MDTService()
auth_service = AuthService()

# 患者端链路（接线层）：与 mdt_service 共用 Store 与同一个 LLM 客户端。
# use_llm=True 时启用 S2 受限发言与多专科分析；未配置 Key 会自动降级为纯规则分诊。
_shared_store = mdt_service.store or Store()
case_flow = CaseFlow(
    store=_shared_store,
    ai_service=getattr(mdt_service.agents, "ai_service", None),
    use_llm=True,
)


def _current_session():
    """从 Authorization 头或 ?token= 取当前会话。"""
    token = extract_token(request.headers.get('Authorization'), request.args.get('token'))
    return auth_service.verify(token)


def _guard(required_scope=None):
    """接口守卫：APP_AUTH_REQUIRED=true 时强制登录；返回 (session, error_response)。"""
    session = _current_session()
    if not auth_required():
        return session, None
    if session is None:
        return None, (jsonify({'error': '未登录或会话已过期，请先登录'}), 401)
    if required_scope and not auth_service.has_scope(session, required_scope):
        return session, (jsonify({'error': f'当前角色（{session.role}）无权限：{required_scope}'}), 403)
    return session, None


@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')


@app.route('/patient', methods=['GET'])
def patient_home():
    """患者端页面（P0 登录 + P1-P5：目标/录入/脱敏/画像/追问）。"""
    return render_template('patient.html')


# ---------------------------------------------------------------------------
# 登录与角色（规格书 §3.1；主文档 §11）
# ---------------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def login():
    """登录：支持账号密码，或角色快捷入口 {"role": "patient"|"doctor"}。"""
    data = request.get_json(silent=True) or {}
    try:
        session = auth_service.login(
            username=data.get('username'),
            password=data.get('password'),
            role=data.get('role'),
        )
    except AuthError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'status': 'ok', 'session': session.to_dict(), 'scopes': session.scopes})


@app.route('/api/logout', methods=['POST'])
def logout():
    token = extract_token(request.headers.get('Authorization'),
                          (request.get_json(silent=True) or {}).get('token'))
    removed = auth_service.logout(token)
    return jsonify({'status': 'ok', 'logged_out': removed})


@app.route('/api/me', methods=['GET'])
def me():
    session = _current_session()
    if not session:
        return jsonify({'authenticated': False, 'auth_required': auth_required()}), 200
    return jsonify({'authenticated': True, 'session': session.to_dict(include_token=False),
                    'scopes': session.scopes, 'auth_required': auth_required()})


# ---------------------------------------------------------------------------
# 患者端链路 v2（接线层）：录入 → 校验(G1/G2/G3) → 脱敏 → 画像(G4) → 追问
# ---------------------------------------------------------------------------

@app.route('/api/v2/preview', methods=['POST'])
def preview_intake():
    """提交前脱敏预览（患者端 P3）：只返回打码结果与命中明细，不落库、不分析。"""
    data = request.get_json(silent=True) or {}
    preview = case_flow.build_preview(
        data.get('medical_history', ''), data.get('main_issue', '')
    )
    return jsonify(preview)


@app.route('/api/v2/case', methods=['POST'])
def create_case():
    """创建病例：返回脱敏预览、画像卡、信息缺口与追问列表。"""
    session, error = _guard('intake')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    result = case_flow.create_case(
        medical_history=data.get('medical_history', ''),
        main_issue=data.get('main_issue', ''),
        goals=data.get('goals') or [],
        age=data.get('age'),
        sex=data.get('gender') or data.get('sex') or '',
        name=data.get('name') or '',
        case_id=data.get('case_id'),
    )
    status = 200 if result.ok else 400
    return jsonify(result.to_dict()), status


@app.route('/api/v2/case/<case_id>/answers', methods=['POST'])
def submit_answers(case_id):
    """提交追问回答（可跳过；跳过不编造，只降低可靠性）。"""
    session, error = _guard('case:own')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    outcome = case_flow.submit_answers(case_id, data.get('answers') or {})
    return jsonify(outcome), (200 if outcome.get('ok') else 404)


@app.route('/api/v2/case/<case_id>', methods=['GET'])
def get_case(case_id):
    """查询病例（医生需 review 权限；患者查自己的病例）。"""
    session, error = _guard()
    if error:
        return error
    case = case_flow.get_case(case_id)
    if not case:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify({'case': case, 'audit': _shared_store.list_audit(case_id, limit=50)})

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
    # Windows 中文控制台默认 GBK，emoji 会导致 UnicodeEncodeError 直接崩溃；
    # 统一把标准输出切到 UTF-8（失败则忽略，不影响服务）。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    # 绑定与调试开关走环境变量：默认只监听本机、关闭 debug（演示/提交要求）
    host = os.getenv('APP_HOST', '127.0.0.1').strip()
    port = int(os.getenv('APP_PORT', '5000'))
    debug = os.getenv('APP_DEBUG', 'false').strip().lower() in ('1', 'true', 'yes')

    print("=" * 60)
    print("[启动] 多学科就医智助系统")
    print(f"[地址] http://{host}:{port}")
    print(f"[模式] 调试={'开启（仅限本地开发）' if debug else '关闭'}")
    print(f"[模型] {os.getenv('AI_MODEL_NAME', 'qwen-max-latest')}")
    if not os.getenv('AI_API_KEY'):
        print("[提示] 未配置 AI_API_KEY（参考 .env.example），AI 分析将不可用")
    print("[停止] 按 Ctrl+C")
    print("=" * 60)
    app.run(debug=debug, host=host, port=port)



