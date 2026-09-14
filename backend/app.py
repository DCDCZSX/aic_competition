import json
import queue as queue_module
import threading
from flask import Flask, request, jsonify, render_template, Response
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
# 不按字母排序 JSON 键：报告段落内部的字段顺序（判断 → 依据 → 证据状态 → 支持证据 …）
# 是给人读的，排序后会被打乱（例如"证据卡片"跑到"证据状态统计"前面）。
app.json.sort_keys = False

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


def _guard_doctor(required_scope='review'):
    """医生端守卫：**始终**要求医生角色会话（规格书 §3.1：后台按角色鉴权，不能只做前端跳转）。

    与 `_guard` 的区别：这里不受 `APP_AUTH_REQUIRED` 影响——审核操作在任何情况下都必须
    由医生角色发起，使用医生账号（doctor）或从首页「医生工作台」进入即可。
    """
    session = _current_session()
    if session is None:
        return None, (jsonify({'error': '未登录：医生端操作需要先登录医生账号'}), 401)
    if session.role != ROLE_DOCTOR:
        return session, (jsonify({'error': f'当前角色（{session.role}）无权访问医生工作台'}), 403)
    if not auth_service.has_scope(session, required_scope):
        return session, (jsonify({'error': f'医生角色缺少权限：{required_scope}'}), 403)
    return session, None


@app.route('/', methods=['GET'])
def home():
    """产品首页（暖色编辑风落地页 + 患者端/医生端双入口）。

    内容全部取自本系统真实能力（评测数字、护栏、审核流程），素材为自绘 SVG，无外链资源。
    """
    return render_template('home.html')



@app.route('/patient', methods=['GET'])
def patient_home():
    """患者端页面（P0 登录 + P1-P5：目标/录入/脱敏/画像/追问）。"""
    return render_template('patient.html')


@app.route('/doctor', methods=['GET'])
def doctor_home():
    """医生端审核台（屏 A-G，见 `UI设计说明-医生端审核台.md`）。"""
    return render_template('doctor.html')


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
    """创建病例：返回脱敏预览、画像卡、信息缺口与追问列表。

    带 `?stream=1`（或 body 里 `"stream": true`）时改为 **SSE 流式返回阶段进度**：
    这条链路含 3 次模型调用（画像增强 / S2 受限发言 / 多专科意见卡 + 质询），实测 10-20 秒，
    流式推送"当前在第几步、在做什么"，前端据此显示真实进度条，不让用户以为卡死。
    """
    session, error = _guard('intake')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    wants_stream = (request.args.get('stream') in ('1', 'true', 'yes')) or bool(data.get('stream'))
    # 两段式：画像/追问/分诊/路径先返回（约 3 秒），多学科分析放后台继续（实测约 26 秒）
    defer_mdt = (request.args.get('defer_mdt') in ('1', 'true', 'yes')) or bool(data.get('defer_mdt'))

    def _create(on_progress=None):
        return case_flow.create_case(
            medical_history=data.get('medical_history', ''),
            main_issue=data.get('main_issue', ''),
            goals=data.get('goals') or [],
            age=data.get('age'),
            sex=data.get('gender') or data.get('sex') or '',
            name=data.get('name') or '',
            case_id=data.get('case_id'),
            on_progress=on_progress,
            defer_mdt=defer_mdt,
        )

    if not wants_stream:
        result = _create()
        status = 200 if result.ok else 400
        return jsonify(result.to_dict()), status

    def _events():
        queue: "queue.Queue" = queue_module.Queue()

        def on_progress(payload):
            queue.put({"type": "progress", **payload})

        def worker():
            try:
                result = _create(on_progress=on_progress)
                queue.put({"type": "done", "status": 200 if result.ok else 400,
                           "result": result.to_dict()})
            except Exception as exc:                       # noqa: BLE001 - 兜底，不让流悬挂
                queue.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                queue.put(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        yield "retry: 60000\n\n"
        while True:
            item = queue.get()
            if item is None:
                break
            yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"

    return Response(
        _events(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            # 必须显式关闭连接：流式响应结束后若客户端复用这条 keep-alive 连接发下一个请求，
            # werkzeug 开发服务器不会处理（实测会一直挂到客户端超时）。
            'Connection': 'close',
        },
    )


@app.route('/api/v2/case/<case_id>/answers', methods=['POST'])
def submit_answers(case_id):
    """提交追问回答（可跳过；跳过不编造，只降低可靠性）。"""
    session, error = _guard('case:own')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    # 科室集合变化时会重跑多学科分析（15-30 秒）：同样支持两段式，先返回分诊结果
    defer_mdt = (request.args.get('defer_mdt') in ('1', 'true', 'yes')) or bool(data.get('defer_mdt'))
    outcome = case_flow.submit_answers(case_id, data.get('answers') or {}, defer_mdt=defer_mdt)
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


@app.route('/api/v2/case/<case_id>/mdt', methods=['GET'])
def get_mdt(case_id):
    """多学科分析进度与结果（P8 轮询用）。

    两段式下多学科分析在后台跑，前端进 P8 时轮询这里：
    `status.state` ∈ running / done / failed / none，`ready=true` 时 `mdt` 可用。
    """
    session, error = _guard()
    if error:
        return error
    data = case_flow.get_mdt_status(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)


@app.route('/api/v2/case/<case_id>/report', methods=['GET'])
def get_report(case_id):
    """生成患者版一页摘要 + 医生审核版完整报告（P9）。"""
    session, error = _guard()
    if error:
        return error
    reports = case_flow.build_reports(case_id)
    if not reports:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(reports)


@app.route('/api/v2/case/<case_id>/tasks/<task_id>', methods=['POST'])
def update_task(case_id, task_id):
    """任务状态机（P7）：todo → doing → done / cancelled / reopened。"""
    session, error = _guard('case:own')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    result = case_flow.update_task_state(
        case_id, task_id, str(data.get('state') or ''),
        actor=(session.username if session else (data.get('actor') or 'patient')),
        reason=str(data.get('reason') or ''),
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/updates', methods=['POST'])
def add_case_update(case_id):
    """就诊后资料更新（P10）：患者上传手上资料，或医生端录入线下意见。

    来源为"医生上传/医院文档/已确认资料"时必须由**医生角色**发起——
    患者端不承担"替医生写意见"（UI 设计说明 §6 第 7 条）。
    """
    data = request.get_json(silent=True) or {}
    source = str(data.get('source') or '患者上传')
    doctor_sources = {'医生上传', '医院文档', '已确认资料'}
    if source in doctor_sources:
        session, error = _guard_doctor('decision')
    else:
        session, error = _guard()
    if error:
        return error
    result = case_flow.add_update(
        case_id,
        kind=str(data.get('kind') or ''),
        content=str(data.get('content') or ''),
        source=source,
        department=str(data.get('department') or ''),
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/changes', methods=['GET'])
def get_changes(case_id):
    """变化摘要（P11）：新增了什么、哪些判断变了、哪些未受影响。"""
    session, error = _guard()
    if error:
        return error
    data = case_flow.get_change_summary(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)


# ---------------------------------------------------------------------------
# 医生端审核台（屏 A-G，规格书 §5.6 / §7.1，任务 4.1-4.5）
# 注：医生端接口**始终**要求医生角色（`_guard_doctor`），不受 APP_AUTH_REQUIRED 影响。
# ---------------------------------------------------------------------------

@app.route('/api/v2/cases', methods=['GET'])
def list_review_cases():
    """屏 A 病例列表（待审优先）。"""
    session, error = _guard_doctor()
    if error:
        return error
    state_filter = request.args.get('state') or ''
    return jsonify({
        'cases': case_flow.list_review_cases(limit=int(request.args.get('limit') or 100),
                                             state_filter=state_filter),
        'reviewer': session.to_dict(include_token=False),
    })


@app.route('/api/v2/case/<case_id>/review', methods=['GET'])
def get_review(case_id):
    """屏 B-F 数据：决策条目 + 审核进度 + 待确认问题 + 线下意见。"""
    session, error = _guard_doctor()
    if error:
        return error
    data = case_flow.get_review(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)


@app.route('/api/v2/case/<case_id>/decisions', methods=['POST'])
def submit_decision(case_id):
    """屏 F 逐条审核：accept / modify / reject / uncertain / request_more_info。"""
    session, error = _guard_doctor('decision')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    result = case_flow.submit_decision(
        case_id,
        item_id=str(data.get('item_id') or ''),
        action=str(data.get('action') or ''),
        after=str(data.get('after') or ''),
        reason=str(data.get('reason') or ''),
        reviewer_name=str(data.get('reviewer') or (session.label if session else '审核医生')),
        reviewer_dept=str(data.get('reviewer_dept') or ''),
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/publish', methods=['POST'])
def publish_review(case_id):
    """屏 F 底部：发布审核版报告（状态→doctor_confirmed、版本+1、角标转绿）。"""
    session, error = _guard_doctor('report:confirm')
    if error:
        return error
    data = request.get_json(silent=True) or {}
    result = case_flow.publish_review(
        case_id,
        reviewer_name=str(data.get('reviewer') or (session.label if session else '审核医生')),
        accept_all=bool(data.get('accept_all')),
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/audit', methods=['GET'])
def get_case_audit(case_id):
    """屏 G 审核记录与版本：审计时间线 + 决策记录 + 条目状态。"""
    session, error = _guard_doctor()
    if error:
        return error
    data = case_flow.get_review_history(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)

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

    # 绑定与调试开关走环境变量：默认只监听本机、关闭 debug（合规要求）
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



