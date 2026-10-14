"""医生确认范围下的模拟投影；与患者提交时的预分析分开。"""
import hashlib
import json

NON_CLINICAL_UPDATE_KINDS = ("复查安排",)


def input_fingerprint(payload):
    confirmation = payload.get('triage_doctor_confirmation') or {}
    screening = payload.get('mdt_screening') or {}
    inputs = {
        key: payload.get(key) for key in
        ('masked_history', 'masked_main_issue', 'profile', 'answers', 'updates')
    }
    # 复查安排等非临床资料不改变专科判断，不应使已有会诊模拟整轮失效。
    inputs['updates'] = [u for u in (inputs.get('updates') or [])
                         if not (isinstance(u, dict) and u.get('kind') in NON_CLINICAL_UPDATE_KINDS)]
    # 结构化画像会随补充资料刷新，其中的刷新时间戳不属于病例依据。
    profile = inputs.get('profile')
    if isinstance(profile, dict) and 'refreshed_at' in profile:
        inputs['profile'] = {k: v for k, v in profile.items() if k != 'refreshed_at'}
    inputs.update(primary=confirmation.get('primary_department'),
                  scope=confirmation.get('selected'), decision=screening.get('decision'),
                  departments=screening.get('departments'), purpose=screening.get('purpose'))
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def simulation_view(payload):
    record = dict(payload.get('doctor_mdt_simulation') or {})
    if not record:
        return {'state': 'none', 'message': '尚未生成本次 AI 会诊模拟。'}
    if record.get('input_fingerprint') != input_fingerprint(payload):
        record.update(state='stale', message='病例资料、参与科室或目标已变化，请生成本次模拟。')
    # 过期结果仅保留在存储历史中，不投影为本次结果。
    if record.get('state') == 'stale':
        record.pop('result', None)
    return record


def review_mdt(payload):
    screening = payload.get('mdt_screening') or {}
    if screening.get('departments_source') != 'doctor_routing':
        return payload.get('mdt') or {}  # 兼容已经使用旧流程的病例。
    if screening.get('decision') != 'recommend_mdt':
        return {}
    view = simulation_view(payload)
    if view.get('state') in ('done', 'partial'):
        return view.get('result') or {}
    return {}


def review_analysis(payload):
    """选择本轮需要医生审核的分析，不把无 MDT 的病例置为空白。"""
    screening = payload.get('mdt_screening') or {}
    if screening.get('departments_source') != 'doctor_routing':
        return payload.get('mdt') or {}
    if screening.get('decision') == 'recommend_mdt':
        return review_mdt(payload)
    selected = (payload.get('triage_doctor_confirmation') or {}).get('selected') or []
    return {'analyses': [a for a in (payload.get('mdt') or {}).get('analyses') or []
                         if a.get('department') in selected], 'aggregation': {}}


def analysis_source(payload):
    screening = payload.get('mdt_screening') or {}
    return 'simulation' if screening.get('decision') == 'recommend_mdt' and screening.get('departments_source') == 'doctor_routing' else 'preanalysis'


def analysis_target_id(department, kind, index, source='preanalysis'):
    # 科室顺序变化不改变条目身份；内容变化由修订号处理。
    dept_key = hashlib.sha256(str(department).encode()).hexdigest()[:12]
    return f'{source}-{dept_key}-{kind}-{index:02d}'
