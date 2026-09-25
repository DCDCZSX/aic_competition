import json
import queue as queue_module
import threading
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
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
from services.ai_service import AIService
from services.auth import (AuthService, AuthError, AuthRateLimitError, ROLE_DOCTOR, ROLE_GUEST, ROLE_PATIENT,
                           auth_required, extract_token)
from services.case_flow import CaseFlow, patient_safe_mdt_screening
from services.ask_service import MAX_ANSWER_ITEMS, MAX_ANSWER_KEY_LENGTH
from services.config import read_int_env
from services.intake import MAX_MAIN_ISSUE_LENGTH, MAX_TEXT_LENGTH, sanitized_payload
from services.preview_tokens import PreviewTokenService
from services.store import Store
from schemas import CASE_STATE_LABELS
from services.request_validation import (
    InputValidationError,
    object_field,
    optional_int,
    phases_field,
    string_field,
    string_list,
)
import time
from collections import defaultdict, deque

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = read_int_env(
    'APP_MAX_CONTENT_LENGTH', 1 * 1024 * 1024,
    minimum=1, maximum=16 * 1024 * 1024
)
_cors_origins = [x.strip() for x in os.getenv(
    'APP_CORS_ORIGINS', 'http://127.0.0.1:5000,http://localhost:5000'
).split(',') if x.strip()]
CORS(app, resources={r"/api/*": {"origins": _cors_origins}}, supports_credentials=False)
# 不按字母排序 JSON 键：报告段落内部的字段顺序（判断 → 依据 → 证据状态 → 支持证据 …）
# 是给人读的，排序后会被打乱（例如"证据卡片"跑到"证据状态统计"前面）。
app.json.sort_keys = False


@app.after_request
def _security_response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "base-uri 'self'; object-src 'none'; frame-ancestors 'none'"
    )
    return response


def _json_object():
    """解析 API JSON 对象，并把非法 JSON/数组统一转换为 JSON 400。"""
    if not request.data:
        return {}, None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({
            'error': '请求体必须是合法 JSON 对象',
            'code': 'invalid_json',
        }), 400)
    return data, None


def _invalid_input(exc):
    return jsonify({'error': str(exc), 'code': 'invalid_request'}), 400


@app.errorhandler(HTTPException)
def _api_http_error(exc):
    """API 错误始终返回 JSON，避免客户端收到 Flask HTML 错误页。"""
    if request.path.startswith('/api/'):
        return jsonify({'error': exc.description or '请求失败',
                        'code': exc.name.lower().replace(' ', '_')}), exc.code
    return exc


@app.errorhandler(Exception)
def _api_unhandled_error(exc):
    """最后一道 API 兜底；不把堆栈/内部数据回显给客户端。"""
    if request.path.startswith('/api/'):
        app.logger.exception('未处理的 API 异常')
        return jsonify({'error': '服务器内部错误，请稍后重试',
                        'code': 'internal_error'}), 500
    raise exc

auth_service = AuthService()
PREVIEW_TTL_MINUTES = read_int_env(
    'APP_PREVIEW_TTL_MINUTES', 10, minimum=1, maximum=30
)
GUEST_SESSIONS_PER_HOUR = read_int_env(
    'APP_GUEST_SESSIONS_PER_HOUR', 5, minimum=1, maximum=100
)
GUEST_TRACKED_IPS = read_int_env(
    'APP_GUEST_TRACKED_IPS', 10000, minimum=100, maximum=1000000
)
GUEST_PREVIEW_LIMIT = read_int_env(
    'APP_GUEST_PREVIEW_LIMIT', 5, minimum=1, maximum=100
)
GUEST_TRIAGE_LIMIT = read_int_env(
    'APP_GUEST_TRIAGE_LIMIT', 2, minimum=1, maximum=100
)
GUEST_MAX_CHARS = read_int_env(
    'APP_GUEST_MAX_CHARS', 2000, minimum=1, maximum=100000
)
preview_tokens = PreviewTokenService(
    PREVIEW_TTL_MINUTES,
    # PreviewTokenService 保留旧版非法配额值的安全回退，兼容既有部署配置。
    per_owner_limit=os.getenv('APP_PREVIEW_MAX_PER_USER', '5'),
    global_limit=os.getenv('APP_PREVIEW_MAX_TOTAL', '5000'),
)
_guest_issue_lock = threading.Lock()
_guest_issues = defaultdict(deque)

# 患者端链路（接线层）。旧 MDTService 不再挂载到 HTTP，避免绕过脱敏与安全约束。
# use_llm=True 时启用 S2 受限发言与多专科分析；未配置 Key 会自动降级为纯规则分诊。
_shared_store = Store()
case_flow = CaseFlow(
    store=_shared_store,
    ai_service=AIService(),
    use_llm=True,
)


def _current_session():
    """只从 Authorization 头取当前会话，token 不进 URL。"""
    token = extract_token(request.headers.get('Authorization'))
    return auth_service.verify(token)


def _guard(required_scope=None):
    """Guard full APIs. Anonymous users must use the explicitly limited guest API."""
    session = _current_session()
    if session is None:
        return None, (jsonify({'error': '未登录或会话已过期，请先登录'}), 401)
    if required_scope and not auth_service.has_scope(session, required_scope):
        return session, (jsonify({'error': f'当前角色（{session.role}）无权限：{required_scope}'}), 403)
    return session, None


def _guard_case(case_id, required_scope=None):
    """同时校验会话、角色和病例所有权。

    医生可按 `case:authorized` 查看病例；患者只能访问 owner_id 与自己账号一致的病例。
    旧库中 owner_id 为空的病例仅医生可见，不自动归属给任意患者。
    """
    session, error = _guard(required_scope)
    if error:
        return session, None, error
    case = _shared_store.load_case(case_id)
    if not case:
        return session, None, (jsonify({'error': f'病例不存在：{case_id}'}), 404)
    if session and session.role == ROLE_DOCTOR and auth_service.has_scope(session, 'case:authorized'):
        if _shared_store.has_case_access(case_id, session.username):
            return session, case, None
        return session, None, (jsonify({'error': '病例不存在或未授权给当前审核账号'}), 404)
    if not session or session.role != ROLE_PATIENT or case.get('owner_id') != session.username:
        return session, None, (jsonify({'error': '病例不存在或无权访问'}), 404)
    return session, case, None


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


def _guard_doctor_case(case_id, required_scope='review'):
    session, error = _guard_doctor(required_scope)
    if error:
        return session, error
    if not _shared_store.has_case_access(case_id, session.username):
        return session, (jsonify({'error': '病例不存在或未授权给当前审核账号'}), 404)
    return session, None


def _guard_guest(required_scope):
    session = _current_session()
    if session is None:
        return None, (jsonify({'error': '游客会话不存在或已过期'}), 401)
    if session.role != ROLE_GUEST or not auth_service.has_scope(session, required_scope):
        return session, (jsonify({'error': '当前会话无权使用游客功能'}), 403)
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
    """登录：只允许账号密码；免密体验走独立游客接口。"""
    data, error = _json_object()
    if error:
        return error
    try:
        session = auth_service.login(
            username=string_field(data, 'username', default='', max_length=128),
            password=string_field(data, 'password', default='', max_length=256),
            role=string_field(data, 'role', default='', max_length=32),
            client_key=request.remote_addr or 'unknown',
        )
    except InputValidationError as exc:
        return _invalid_input(exc)
    except AuthRateLimitError as exc:
        return jsonify({'error': str(exc), 'code': 'rate_limited'}), 429, {
            'Retry-After': str(exc.retry_after),
        }
    except AuthError as exc:
        # 登录失败统一 401 且不区分“账号不存在/密码错误”，避免账号枚举。
        return jsonify({'error': str(exc), 'code': 'invalid_credentials'}), 401
    return jsonify({'status': 'ok', 'session': session.to_dict(), 'scopes': session.scopes})


@app.route('/api/guest/session', methods=['POST'])
def create_guest_session():
    """Issue a short-lived anonymous token; it cannot access persistent case APIs."""
    remote = request.remote_addr or 'unknown'
    now = time.time()
    per_hour = GUEST_SESSIONS_PER_HOUR
    with _guest_issue_lock:
        recent = _guest_issues[remote]
        while recent and recent[0] <= now - 3600:
            recent.popleft()
        if not recent:
            _guest_issues.pop(remote, None)
            recent = _guest_issues[remote]
        max_tracked = GUEST_TRACKED_IPS
        if len(_guest_issues) > max_tracked:
            # 只保留近期有记录的来源，防止伪造大量来源地址使内存字典无限增长。
            stale = sorted(
                ((key, values[-1] if values else 0) for key, values in _guest_issues.items()),
                key=lambda item: item[1],
            )
            for key, _ in stale[:max(1, len(_guest_issues) - max_tracked)]:
                if key != remote:
                    _guest_issues.pop(key, None)
        if len(recent) >= per_hour:
            return jsonify({'error': '游客会话创建过于频繁，请稍后再试或登录'}), 429
        recent.append(now)
    try:
        session = auth_service.create_guest_session()
    except AuthError as exc:
        return jsonify({'error': str(exc), 'code': 'guest_unavailable'}), 429
    return jsonify({
        'status': 'ok',
        'session': session.to_dict(),
        'scopes': session.scopes,
        'limits': {
            'preview': GUEST_PREVIEW_LIMIT,
            'triage': GUEST_TRIAGE_LIMIT,
            'max_chars': GUEST_MAX_CHARS,
        },
        'data_policy': '仅用于本次内存分析，不创建病例档案',
    }), 201


@app.route('/api/logout', methods=['POST'])
def logout():
    token = extract_token(request.headers.get('Authorization'))
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
    session, error = _guard('intake')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        history = string_field(data, 'medical_history', default='')
        issue = string_field(data, 'main_issue', default='', max_length=MAX_MAIN_ISSUE_LENGTH)
    except InputValidationError as exc:
        return _invalid_input(exc)
    if len(history) > MAX_TEXT_LENGTH:
        return jsonify({
            'error': f'病历正文过长（最多 {MAX_TEXT_LENGTH} 个字符），请分批录入',
            'code': 'invalid_request',
        }), 400
    preview = case_flow.build_preview(history, issue)
    if not preview.get('blocked') and not preview.get('blocking_hit_count'):
        masked = sanitized_payload(history, issue)
        preview['preview_token'] = preview_tokens.issue(
            session.username, masked['medical_history'], masked['main_issue']
        )
        preview['preview_expires_in_seconds'] = preview_tokens.ttl.seconds
    return jsonify(preview)


@app.route('/api/guest/preview', methods=['POST'])
def guest_preview():
    session, error = _guard_guest('guest:preview')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        history = string_field(data, 'medical_history', default='')
        issue = string_field(data, 'main_issue', default='')
    except InputValidationError as exc:
        return _invalid_input(exc)
    max_chars = GUEST_MAX_CHARS
    if len(history) + len(issue) > max_chars:
        return jsonify({'error': f'游客输入最多 {max_chars} 个字符'}), 413
    if not history.strip() and not issue.strip():
        return jsonify({'error': '请至少填写一项就医资料'}), 400
    limit = GUEST_PREVIEW_LIMIT
    if not auth_service.consume(session, 'preview', limit):
        return jsonify({'error': '游客脱敏预览次数已用完，请登录继续'}), 429
    preview = case_flow.build_preview(history, issue)
    preview['guest_mode'] = True
    preview['persistent'] = False
    return jsonify(preview)


@app.route('/api/guest/triage', methods=['POST'])
def guest_triage():
    """Run a bounded rule-only analysis in memory; no case, MDT or report is persisted."""
    session, error = _guard_guest('guest:triage')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        history = string_field(data, 'medical_history', default='')
        issue = string_field(data, 'main_issue', default='')
        goals = string_list(data, 'goals', default=())
        age = optional_int(data, 'age')
        gender = string_field(data, 'gender', default='')
        sex = string_field(data, 'sex', default='')
    except InputValidationError as exc:
        return _invalid_input(exc)
    max_chars = GUEST_MAX_CHARS
    if len(history) + len(issue) > max_chars:
        return jsonify({'error': f'游客输入最多 {max_chars} 个字符'}), 413
    # Do not spend a quota unit on malformed or oversized input.
    if not history.strip() and not issue.strip():
        return jsonify({'error': '请至少填写一项就医资料'}), 400
    limit = GUEST_TRIAGE_LIMIT
    if not auth_service.consume(session, 'triage', limit):
        return jsonify({'error': '游客分析次数已用完，请登录使用完整功能'}), 429
    guest_store = Store(':memory:')
    try:
        guest_flow = CaseFlow(store=guest_store, use_llm=False)
        result = guest_flow.create_case(
            medical_history=history,
            main_issue=issue,
            goals=goals,
            age=age,
            sex=gender or sex,
        )
        payload = result.to_dict()
    finally:
        guest_store.close()
    response = {
        key: payload.get(key) for key in (
            'ok', 'blocked', 'blocked_by', 'advice', 'errors', 'warnings',
            'masked_preview', 'profile', 'gaps', 'questions', 'triage', 'path',
            'completeness', 'direct_quote_coverage', 'passes_gate',
        )
    }
    response.update({
        'guest_mode': True,
        'persistent': False,
        'limitations': ['不保存病例', '不调用多学科模型', '不生成审核版报告'],
        'disclaimer': '仅供体验，不构成诊断或治疗建议',
    })
    return jsonify(response), (200 if payload.get('ok') else 400)


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
    data, error = _json_object()
    if error:
        return error
    try:
        preview_token = string_field(data, 'preview_token', default='')
        goals = string_list(data, 'goals', default=(), max_items=8)
        age = optional_int(data, 'age')
        gender = string_field(data, 'gender', default='')
        sex = string_field(data, 'sex', default='')
        name = string_field(data, 'name', default='', max_length=120)
    except InputValidationError as exc:
        return _invalid_input(exc)
    preview_payload = preview_tokens.consume(preview_token, session.username)
    if preview_payload is None:
        return jsonify({'error': '请先完成脱敏预览；预览凭证已过期、已使用或不属于当前账号'}), 409
    wants_stream = (request.args.get('stream') in ('1', 'true', 'yes')) or bool(data.get('stream'))
    # 两段式：画像/追问/分诊/路径先返回（约 3 秒），多学科分析放后台继续（实测约 26 秒）
    defer_mdt = (request.args.get('defer_mdt') in ('1', 'true', 'yes')) or bool(data.get('defer_mdt'))
    # 渐进式：连模型增强也不等，先给确定性规则结果（实测 0.2 秒级），模型三相全部后台
    progressive = (request.args.get('progressive') in ('1', 'true', 'yes')) or bool(data.get('progressive'))

    def _create(on_progress=None):
        result = case_flow.create_case(
            medical_history=preview_payload.medical_history,
            main_issue=preview_payload.main_issue,
            goals=goals,
            age=age,
            sex=gender or sex,
            name=name,
            owner_id=session.username if session else '',
            on_progress=on_progress,
            defer_mdt=defer_mdt,
            progressive=progressive,
        )
        if result.ok and result.case_id and os.getenv(
                'APP_AUTO_ASSIGN_REVIEWER', 'false').lower() in ('1', 'true', 'yes'):
            reviewer_id = os.getenv('APP_DOCTOR_USERNAME', 'doctor').strip()
            if auth_service.is_reviewer_account(reviewer_id):
                _shared_store.grant_case_access(
                    result.case_id, reviewer_id, granted_by=session.username
                )
        return result

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
            except Exception:                               # noqa: BLE001 - 兜底，不让流悬挂
                app.logger.exception("病例流式创建失败")
                queue.put({
                    "type": "error",
                    "code": "case_creation_failed",
                    "error": "病例处理失败，请稍后重试",
                })
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
    session, case, error = _guard_case(case_id, 'case:own')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        answers = object_field(
            data, 'answers', default={}, max_items=MAX_ANSWER_ITEMS,
            max_key_length=MAX_ANSWER_KEY_LENGTH,
        )
    except InputValidationError as exc:
        return _invalid_input(exc)
    # 科室集合变化时会重跑多学科分析（15-30 秒）：同样支持两段式，先返回分诊结果
    defer_mdt = (request.args.get('defer_mdt') in ('1', 'true', 'yes')) or bool(data.get('defer_mdt'))
    outcome = case_flow.submit_answers(case_id, answers, defer_mdt=defer_mdt)
    return jsonify(outcome), (200 if outcome.get('ok') else 400)


@app.route('/api/v2/case/<case_id>', methods=['GET'])
def get_case(case_id):
    """查询病例（医生需 review 权限；患者查自己的病例）。"""
    session, case, error = _guard_case(case_id)
    if error:
        return error
    if session and session.role == ROLE_PATIENT:
        # 医生初筛保存在病例 payload 中，但患者只需要看到进度和下一步，
        # 不应看到内部审核人账号或权限字段。
        safe_case = dict(case)
        safe_payload = dict(case.get('payload') or {})
        safe_payload['mdt_screening'] = patient_safe_mdt_screening(
            safe_payload.get('mdt_screening'))
        safe_case['payload'] = safe_payload
        case = safe_case
    return jsonify({'case': case, 'audit': _shared_store.list_audit(case_id, limit=50)})


@app.route('/api/v2/case/<case_id>/access', methods=['POST', 'DELETE'])
def manage_case_access(case_id):
    """The case owner explicitly grants or revokes access for a reviewer account."""
    session, case, error = _guard_case(case_id, 'case:own')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        reviewer_id = string_field(data, 'reviewer_id', default='', max_length=128).strip()
    except InputValidationError as exc:
        return _invalid_input(exc)
    if not auth_service.is_reviewer_account(reviewer_id):
        return jsonify({'error': '目标账号不存在或不是审核角色'}), 400
    if request.method == 'DELETE':
        changed = _shared_store.revoke_case_access(
            case_id, reviewer_id, revoked_by=session.username
        )
    else:
        changed = _shared_store.grant_case_access(
            case_id, reviewer_id, granted_by=session.username
        )
    return jsonify({'ok': True, 'changed': changed, 'reviewer_id': reviewer_id})


@app.route('/api/v2/case/<case_id>/analysis', methods=['GET'])
def get_analysis(case_id):
    """后台分析任务状态（渐进式/两段式的统一下拉口）。

    返回三个相位（画像增强 / 就医方向 / 多学科）的状态、MDT 细分进度（第几科 + 子步骤）、
    已就绪的结果片段与警告 —— 前端据此**增量渲染**：规则结果先显示，模型结果到了再替换。
    """
    session, case, error = _guard_case(case_id)
    if error:
        return error
    data = case_flow.get_analysis(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)


@app.route('/api/v2/case/<case_id>/analysis/retry', methods=['POST'])
def retry_analysis(case_id):
    """重试未完成/失败的分析相位（患者端"重新分析"按钮）。"""
    session, case, error = _guard_case(case_id, 'case:own')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        phases = phases_field(data, case_flow.ANALYSIS_PHASES)
    except InputValidationError as exc:
        return _invalid_input(exc)
    result = case_flow.retry_analysis(case_id, phases=phases)
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/cases/analyze', methods=['POST'])
def analyze_pending_cases():
    """医生端批处理：把"分析未完成"的病例排进后台任务（不阻塞请求，逐例可见进度）。"""
    session, error = _guard_doctor()
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        requested_limit = optional_int(data, 'limit')
    except InputValidationError as exc:
        return _invalid_input(exc)
    limit = 3 if requested_limit is None else requested_limit
    if not 1 <= limit <= 10:
        return jsonify({'error': 'limit 必须是 1 到 10 之间的整数',
                        'code': 'invalid_request'}), 400
    result = case_flow.analyze_pending_cases(
        limit=max(1, min(limit, 10)), reviewer_id=session.username
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/mdt', methods=['GET'])
def get_mdt(case_id):
    """多学科分析进度与结果（P8 轮询用）。

    两段式下多学科分析在后台跑，前端进 P8 时轮询这里：
    `status.state` ∈ running / done / failed / none，`ready=true` 时 `mdt` 可用。
    """
    session, case, error = _guard_case(case_id)
    if error:
        return error
    data = case_flow.get_mdt_status(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)


@app.route('/api/v2/case/<case_id>/mdt-screening', methods=['POST'])
def submit_mdt_screening(case_id):
    """医生提交 MDT 初筛决定；初筛是病例子状态，不改变 case_state。"""
    session, error = _guard_doctor_case(case_id, 'decision')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        decision = string_field(data, 'decision', default='', max_length=64)
        reason = string_field(data, 'reason', default='', max_length=2000)
        purpose = string_field(data, 'purpose', default='', max_length=2000)
        departments = string_list(data, 'departments', default=(), max_items=6)
    except InputValidationError as exc:
        return _invalid_input(exc)
    result = case_flow.submit_mdt_screening(
        case_id,
        decision=decision,
        reason=reason,
        purpose=purpose,
        departments=departments,
        reviewer_id=session.username,
    )
    if not result.get('ok'):
        result.setdefault('code', 'invalid_request')
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/report', methods=['GET'])
def get_report(case_id):
    """生成患者版一页摘要 + 医生审核版完整报告（P9）。"""
    session, case, error = _guard_case(case_id)
    if error:
        return error
    reports = case_flow.build_reports(case_id)
    if not reports:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    if session and session.role == ROLE_PATIENT:
        reports['mdt_screening'] = patient_safe_mdt_screening(
            (case.get('payload') or {}).get('mdt_screening'))
    return jsonify(reports)


@app.route('/api/v2/case/<case_id>/tasks/<task_id>', methods=['POST'])
def update_task(case_id, task_id):
    """任务状态机（P7）：todo → doing → done / cancelled / reopened。"""
    session, case, error = _guard_case(case_id, 'case:own')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        task_state = string_field(data, 'state', default='')
        reason = string_field(data, 'reason', default='', max_length=1000)
        actor = string_field(data, 'actor', default='patient', max_length=128)
    except InputValidationError as exc:
        return _invalid_input(exc)
    result = case_flow.update_task_state(
        case_id, task_id, task_state,
        actor=(session.username if session else actor),
        reason=reason,
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/updates', methods=['POST'])
def add_case_update(case_id):
    """就诊后资料更新（P10）：患者上传手上资料，或医生端录入线下意见。

    来源为"医生上传/医院文档/已确认资料"时必须由**医生角色**发起——
    患者端不承担"替医生写意见"（UI 设计说明 §6 第 7 条）。
    """
    data, error = _json_object()
    if error:
        return error
    try:
        kind = string_field(data, 'kind', default='')
        content = string_field(data, 'content', default='')
        source = string_field(data, 'source', default='患者上传')
        department = string_field(data, 'department', default='', max_length=120)
    except InputValidationError as exc:
        return _invalid_input(exc)
    doctor_sources = {'医生上传', '医院文档', '已确认资料'}
    if source in doctor_sources:
        session, error = _guard_doctor_case(case_id, 'decision')
    else:
        session, case, error = _guard_case(case_id, 'update:own')
    if error:
        return error
    result = case_flow.add_update(
        case_id,
        kind=kind,
        content=content,
        source=source,
        department=department,
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/changes', methods=['GET'])
def get_changes(case_id):
    """变化摘要（P11）：新增了什么、哪些判断变了、哪些未受影响。"""
    session, case, error = _guard_case(case_id)
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
    """屏 A 病例队列（服务端筛选、排序与分页）。"""
    session, error = _guard_doctor()
    if error:
        return error
    state_filter = request.args.get('state') or ''
    allowed_states = set(CASE_STATE_LABELS)
    if state_filter and state_filter not in allowed_states:
        return jsonify({'error': f'不支持的病例状态：{state_filter}',
                        'code': 'invalid_request'}), 400
    filter_kind = request.args.get('filter') or ''
    allowed_filters = {key for key, _ in case_flow.QUEUE_FILTERS}
    if filter_kind and filter_kind not in allowed_filters:
        return jsonify({'error': f'不支持的队列筛选：{filter_kind}',
                        'code': 'invalid_request'}), 400
    sort = request.args.get('sort') or 'risk'
    allowed_sorts = {key for key, _ in case_flow.QUEUE_SORTS}
    if sort not in allowed_sorts:
        return jsonify({'error': f'不支持的排序方式：{sort}',
                        'code': 'invalid_request'}), 400
    try:
        raw_limit = request.args.get('limit')
        limit = 20 if raw_limit in (None, '') else int(raw_limit)
    except (TypeError, ValueError):
        return jsonify({'error': 'limit 必须是整数', 'code': 'invalid_request'}), 400
    if not 1 <= limit <= 100:
        return jsonify({'error': 'limit 必须是 1 到 100 之间的整数',
                        'code': 'invalid_request'}), 400
    try:
        raw_offset = request.args.get('offset')
        offset = 0 if raw_offset in (None, '') else int(raw_offset)
    except (TypeError, ValueError):
        return jsonify({'error': 'offset 必须是整数', 'code': 'invalid_request'}), 400
    if offset < 0:
        return jsonify({'error': 'offset 不能小于 0', 'code': 'invalid_request'}), 400
    page = case_flow.list_review_cases(
        limit=limit,
        offset=offset,
        state_filter=state_filter,
        filter_kind=filter_kind,
        sort=sort,
        department=request.args.get('department') or '',
        reviewer_id=session.username,
    )
    return jsonify({
        'cases': page['rows'],
        'total': page['total'],
        'has_more': page['has_more'],
        'filters': page['filters'],
        'sorts': page['sorts'],
        'reviewer': session.to_dict(include_token=False),
    })


@app.route('/api/v2/case/<case_id>/review', methods=['GET'])
def get_review(case_id):
    """屏 B-F 数据：决策条目 + 审核进度 + 待确认问题 + 线下意见。"""
    session, error = _guard_doctor_case(case_id)
    if error:
        return error
    data = case_flow.get_review(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)


@app.route('/api/v2/case/<case_id>/decisions', methods=['POST'])
def submit_decision(case_id):
    """屏 F 逐条审核：accept / modify / reject / uncertain / request_more_info。"""
    session, error = _guard_doctor_case(case_id, 'decision')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        item_id = string_field(data, 'item_id', default='', max_length=200)
        action = string_field(data, 'action', default='', max_length=80)
        after = string_field(data, 'after', default='', max_length=4000)
        reason = string_field(data, 'reason', default='', max_length=2000)
        reviewer = string_field(data, 'reviewer', default='', max_length=128)
        reviewer_dept = string_field(data, 'reviewer_dept', default='', max_length=120)
    except InputValidationError as exc:
        return _invalid_input(exc)
    result = case_flow.submit_decision(
        case_id,
        item_id=item_id,
        action=action,
        after=after,
        reason=reason,
        reviewer_name=reviewer or (session.label if session else '模拟审核员'),
        reviewer_dept=reviewer_dept,
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/publish', methods=['POST'])
def publish_review(case_id):
    """屏 F 底部：发布审核版报告（状态→doctor_confirmed、版本+1、角标转绿）。"""
    session, error = _guard_doctor_case(case_id, 'report:confirm')
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    try:
        reviewer = string_field(data, 'reviewer', default='', max_length=128)
    except InputValidationError as exc:
        return _invalid_input(exc)
    result = case_flow.publish_review(
        case_id,
        reviewer_name=reviewer or (session.label if session else '模拟审核员'),
        accept_all=data.get('accept_all') is True,
    )
    return jsonify(result), (200 if result.get('ok') else 400)


@app.route('/api/v2/case/<case_id>/audit', methods=['GET'])
def get_case_audit(case_id):
    """屏 G 审核记录与版本：审计时间线 + 决策记录 + 条目状态。"""
    session, error = _guard_doctor_case(case_id)
    if error:
        return error
    data = case_flow.get_review_history(case_id)
    if data is None:
        return jsonify({'error': f'病例不存在：{case_id}'}), 404
    return jsonify(data)

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
    port = read_int_env('APP_PORT', 5000, minimum=1, maximum=65535)
    debug = os.getenv('APP_DEBUG', 'false').strip().lower() in ('1', 'true', 'yes')

    print("=" * 60)
    print("[启动] 多学科就医智助系统")
    print(f"[首页] http://{host}:{port}/")
    print(f"[患者端] http://{host}:{port}/patient")
    print(f"[医生端] http://{host}:{port}/doctor")
    print(f"[模式] 调试={'开启（仅限本地开发）' if debug else '关闭'}")
    print(f"[模型] {os.getenv('AI_MODEL_NAME', 'qwen-max-latest')}")
    if not os.getenv('AI_API_KEY'):
        print("[提示] 未配置 AI_API_KEY（参考 .env.example），AI 分析将不可用")
    print("[停止] 按 Ctrl+C")
    print("=" * 60)
    app.run(debug=debug, host=host, port=port)
