/**
 * 真实 HTTP 报告校验：内部段落渲染、患者详情筛选、打印内容与发布后的医生建议。
 * 先启动应用，再运行 node verify_report_render.mjs；支持项目 .env 和环境变量凭据。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
if (typeof process.loadEnvFile === 'function') {
  const files = [process.env.CHAIS_ENV_FILE, path.join(ROOT, '.env'), path.join(HERE, '.env'),
    path.join(ROOT, '..', 'competition-materials', 'local-config', '.env')];
  for (const file of files) if (file && fs.existsSync(file)) process.loadEnvFile(file);
}
const BASE = (process.env.APP_BASE_URL || 'http://127.0.0.1:5000').replace(/\/$/, '');
const credentials = {
  PATIENT: { username: process.env.APP_PATIENT_USERNAME || 'patient', password: process.env.APP_PATIENT_PASSWORD },
  DOCTOR: { username: process.env.APP_DOCTOR_USERNAME || 'doctor', password: process.env.APP_DOCTOR_PASSWORD },
};
for (const [role, account] of Object.entries(credentials)) {
  if (!account.password) throw new Error('未配置 APP_' + role + '_PASSWORD');
}
const source = fs.readFileSync(path.join(HERE, 'templates', 'patient.html'), 'utf8');
const inline = [...source.matchAll(/<script(?:\s+src="([^"]+)")?[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => {
    if (!match[1]) return match[2];
    if (!match[1].startsWith('/static/js/')) throw new Error('不允许载入外部脚本：' + match[1]);
    return fs.readFileSync(path.join(HERE, match[1].slice(1)), 'utf8');
  }).join('\n;\n');
const els = new Map();
const el = id => {
  if (!els.has(id)) els.set(id, { innerHTML: '', textContent: '', style: { setProperty() {}, width: '' },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, dataset: {},
    setAttribute() {}, getAttribute: () => null, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], focus() {}, click() {} });
  return els.get(id);
};
globalThis.document = { getElementById: el, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, title: '', fonts: { ready: Promise.resolve() } };
globalThis.window = { addEventListener() {}, scrollTo() {}, innerHeight: 900, scrollY: 0,
  matchMedia: () => ({ matches: false }), localStorage: { getItem: () => null, setItem() {}, removeItem() {} } };
globalThis.localStorage = globalThis.window.localStorage;
globalThis.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.location = { href: '', reload() {} };
globalThis.alert = () => {};

eval(inline + '\n;globalThis.__R = { renderSectionValue, patientFullReportEntries, patientPathFlowValue, switchReport, S };');
const { renderSectionValue, patientFullReportEntries, patientPathFlowValue, switchReport, S } = globalThis.__R;

let passed = 0;
const problems = [];
const check = (name, condition) => {
  if (condition) { passed++; console.log('  [PASS] ' + name); }
  else { problems.push(name); console.log('  [FAIL] ' + name); }
};
const json = async (route, { token, method = 'GET', body } = {}) => {
  const response = await fetch(BASE + route, { method, signal: AbortSignal.timeout(120000),
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  let data;
  try { data = await response.json(); }
  catch { throw new Error(route + ' 返回非 JSON（HTTP ' + response.status + '）'); }
  if (!response.ok) throw new Error(route + ' 失败（HTTP ' + response.status + '）：' + (data.error || '未知错误'));
  return data;
};
const login = async role => {
  const data = await json('/api/login', { method: 'POST', body: credentials[role] });
  if (!data.session?.token) throw new Error(role + ' 登录没有返回会话凭据');
  return data.session.token;
};
const patient = await login('PATIENT');
const doctor = await login('DOCTOR');
const preview = await json('/api/v2/preview', { token: patient, method: 'POST', body: {
  medical_history: '男，61岁。视物模糊3个月，双下肢浮肿1周。既往2型糖尿病5年，尿蛋白++。',
  main_issue: '就诊前资料准备',
} });
if (!preview.preview_token) throw new Error('脱敏预览没有返回 preview_token');
const created = await json('/api/v2/case', { token: patient, method: 'POST',
  body: { preview_token: preview.preview_token, age: 61, sex: '男' } });
const target = created.case_id;
if (!target) throw new Error('创建病例没有返回编号');
const base = '/api/v2/case/' + target;
await json(base + '/access', { token: patient, method: 'POST',
  body: { reviewer_id: process.env.APP_DOCTOR_USERNAME || 'doctor' } });
await json(base + '/submit-review', { token: patient, method: 'POST',
  body: { department_code: process.env.APP_DOCTOR_DEPARTMENT || 'endocrinology' } });

console.log('核验合成病例：' + target);
const report = await json(base + '/report', { token: patient });
const sections = report.full?.sections || {};
check('后端内部完整报告仍有13段', Object.keys(sections).length === 13 && report.full.complete === true);
for (const [name, value] of Object.entries(sections)) {
  const html = renderSectionValue(value);
  check('内部段落可读：' + name, !html.includes('[object Object]') && !/undefined|NaN/.test(html)
    && !/>(judgement|evidence_state|supports|confidence|step_id|scope)</.test(html));
}

// 以哨兵验证内部分析与额外字段不会进入患者详情或打印视图。
const safeReport = structuredClone(report);
safeReport.full.sections['证据关联'] = { '内部材料': 'INTERNAL_EVIDENCE_SENTINEL' };
safeReport.full.sections['各专科 AI 视角'] = ['INTERNAL_SPECIALTY_SENTINEL'];
safeReport.full.sections['分诊结果']['规则决策轨迹'] = 'INTERNAL_ROUTE_SENTINEL';
const allowed = new Set(['病例画像', '时间线', '分诊结果', '就医路径', '医生最终意见']);

function inspectPatientPage(value, published) {
  S.report = value;
  S.reportOpen = {};
  switchReport('full');
  const html = els.get('report-body').innerHTML;
  const visible = patientFullReportEntries(value.full);
  const names = visible.map(([name]) => name);
  check('患者详情只展示规定范围的内容', names.length > 0 && names.every(name => allowed.has(name)));
  check('患者详情保留病例概况、就医方向和路径',
    ['病例画像', '分诊结果', '就医路径'].every(name => names.includes(name)));
  check('未发布时不展示医生确认意见', published || !names.includes('医生最终意见'));
  check('每项患者内容有折叠段落及正文',
    (html.match(/<details class="sec"/g) || []).length === visible.length
    && (html.match(/class="sec-body"/g) || []).length === visible.length);
  check('患者详情默认收起', !html.includes(' open>'));
  check('患者详情有展开全部入口', html.includes('id="rep-toggle-all"'));
  const blocks = [...html.matchAll(/<details class="sec"[\s\S]*?<\/details>/g)].map(match => match[0]);
  for (const [index, [name, value]] of visible.entries()) {
    const rendered = name === '就医路径' ? patientPathFlowValue(value) : renderSectionValue(value);
    check('折叠时已保留正文：' + name,
      blocks[index]?.includes('id="sec-' + index + '"')
      && blocks[index].replace(/\s+/g, '').includes(rendered.replace(/\s+/g, '')));
  }
  check('详情和打印不含内部分析哨兵', !/INTERNAL_(EVIDENCE|SPECIALTY|ROUTE)_SENTINEL/.test(html));
  check('详情和打印没有裸对象或异常值', !/\[object Object\]|undefined|NaN/.test(html));
  check('存在独立患者打印视图', html.includes('class="print-report'));
  S.reportOpen = { 0: true };
  switchReport('full');
  check('重新渲染保留展开状态', els.get('report-body').innerHTML.includes('data-idx="0" open>'));
}
console.log('\n[1] 医生发布前的患者详情和打印');
inspectPatientPage(safeReport, false);
check('发布前明确提示医生尚未发布', els.get('doctor-result-body').innerHTML.includes('医生尚未发布'));

console.log('\n[2] 按现行分流和审核流程发布');
await json(base + '/doctor-routing', { token: doctor, method: 'POST', body: {
  primary_department: '内分泌科', secondary_departments: [], decision: 'single_specialty',
  reason: '合成验证：由主责科室核对就诊资料',
} });
let review = await json(base + '/review', { token: doctor });
for (const item of review.items) {
  if (item.review_tier === 'required' && item.state === 'awaiting') {
    await json(base + '/decisions', { token: doctor, method: 'POST', body: {
      item_id: item.item_id, action: 'accept', expected_content_revision: item.content_revision,
    } });
  }
}
review = await json(base + '/review', { token: doctor });
const revisions = new Map(review.items.map(item => [item.item_id, item.content_revision]));
for (const item of review.disagreements || []) {
  await json(base + '/disagreement-decisions', { token: doctor, method: 'POST', body: {
    disagreement_id: item['分歧编号'], status: 'unresolved_with_followup',
    reason: '合成验证：保留分歧并继续核对原始资料', next_step: '向接诊医生核对原始检查报告。',
    expected_content_revision: revisions.get('consensus_item:' + item['分歧编号']),
  } });
}
const advice = '请携带已有资料复诊，向接诊医生确认后续评估安排。';
const publication = await json(base + '/publication-preview', {
  token: doctor, method: 'POST', body: { final_advice: advice },
});
await json(base + '/publish', { token: doctor, method: 'POST',
  body: { final_advice: advice, preview_revision: publication.preview_revision } });
const published = await json(base + '/report', { token: patient });
check('患者读取已发布状态及相同最终建议', published.doctor_result.published === true
  && published.doctor_result.final_advice === advice && published.summary.doctor_confirmed === true);
inspectPatientPage(published, true);
check('医生发布后最终建议进入患者结果', els.get('doctor-result-body').innerHTML.includes(advice));
check('医生发布后最终建议进入打印视图', els.get('report-body').innerHTML.includes(advice)
  && els.get('report-body').innerHTML.includes('print-final-report'));

console.log('\n===== 报告渲染：' + passed + ' 通过 / ' + problems.length + ' 失败 =====');
if (problems.length) console.log(problems.join('\n'));
process.exitCode = problems.length ? 1 : 0;
