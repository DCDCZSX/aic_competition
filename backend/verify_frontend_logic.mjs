/**
 * 前端渲染逻辑校验（Node + 最小 DOM 桩，不需要浏览器）
 * ---------------------------------------------------------------------------
 * 为什么需要：三轨工作台的两条轨道（患者端流程导航/病例上下文、医生工作台待办病例/审核面板）
 * 全部由内联 JS 渲染。业务单测跑不到、服务端 HTML 里也没有这些内容，而它们恰恰是
 * "页面两侧空白" 问题的解法所在。这个脚本把两页的内联 JS 抽出来，在 Node 里用最小
 * DOM 桩执行关键渲染函数，断言产出真的填进了对应的容器。
 *
 * 运行：node verify_frontend_logic.mjs      （需要 Node 18+；失败时退出码为 1）
 * 依赖：无（只用 node: 内置模块），不需要启动 Flask 服务。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TPL = path.join(HERE, 'templates');

/* ------------------------------------------------------------------ DOM 桩 */
const els = new Map();
function mkEl(id) {
  return {
    id, innerHTML: '', textContent: '', className: '', value: '', dataset: {},
    style: { setProperty() { }, width: '' },
    classList: { add() { }, remove() { }, toggle() { }, contains() { return false; } },
    setAttribute() { }, getAttribute() { return null; }, removeAttribute() { },
    addEventListener() { }, querySelector() { return null; }, querySelectorAll() { return []; },
    focus() { }, click() { },
  };
}
const el = id => (els.has(id) ? els.get(id) : (els.set(id, mkEl(id)), els.get(id)));

let fetchPayload = { status: 200, data: {} };
globalThis.document = {
  getElementById: el, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() { }, title: '', fonts: { ready: Promise.resolve() }, body: mkEl('body'),
};
globalThis.window = {
  addEventListener() { }, scrollTo() { }, innerHeight: 900, scrollY: 0,
  matchMedia: () => ({ matches: false, addEventListener() { } }),
  localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
};
globalThis.localStorage = globalThis.window.localStorage;
globalThis.IntersectionObserver = class { observe() { } unobserve() { } disconnect() { } };
globalThis.fetch = async () => ({ ok: true, status: fetchPayload.status, json: async () => fetchPayload.data });
globalThis.location = { href: '', reload() { } };
globalThis.alert = () => { };

/* ------------------------------------------------------------- 抽取内联 JS */
function inlineScript(file) {
  const src = fs.readFileSync(path.join(TPL, file), 'utf8');
  const blocks = [...src.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  return blocks.join('\n;\n');
}

const results = [];
const check = (name, cond, detail = '') => results.push([!!cond, name, detail]);

/* -------------------------------------------------------------- 患者端断言 */
{
  eval(inlineScript('patient.html') + '\n;globalThis.__P = { S, renderSteps, renderContext, STEPS, renderSectionValue, mdtProgressHtml, analysisBanner };');
  const { S, renderSteps, renderContext, STEPS, renderSectionValue, mdtProgressHtml, analysisBanner } = globalThis.__P;

  check('患者端主流程为 9 步', STEPS.length === 9, String(STEPS.length));

  renderSteps('goal');
  const h1 = el('steps').innerHTML;
  const count = (h1.match(/class="step[ "]/g) || []).length;
  check('左轨渲染出 9 个步骤项', count === 9, String(count));
  check('左轨含步骤名与两位序号', h1.includes('就医目标') && h1.includes('09'), '');
  check('当前步骤高亮', h1.includes('class="step active"'), '');
  check('第 1 步进度 0%', el('steps-bar').style.width === '0%', el('steps-bar').style.width);
  check('第 1 步说明为「第 1 / 9 步」', el('steps-note').textContent.includes('第 1 / 9 步'),
    el('steps-note').textContent);

  renderSteps('triage');
  const h6 = el('steps').innerHTML;
  const done = (h6.match(/class="step done"/g) || []).length;
  check('已走过的 5 步标记为 done', done === 5, String(done));
  check('已走过的步骤可点击回跳', h6.includes(`onclick="go('goal')"`), '');
  check('第 6 步进度 63%', el('steps-bar').style.width === '63%', el('steps-bar').style.width);

  // 就诊后更新 / 变化摘要不在 9 步主流程内 → 应显示"附加环节"而不是"尚未开始"
  renderSteps('update');
  const hu = el('steps').innerHTML;
  check('附加环节：主流程 9 步全部完成', (hu.match(/class="step done"/g) || []).length === 9, '');
  check('附加环节：进度条 100%', el('steps-bar').style.width === '100%', el('steps-bar').style.width);
  check('附加环节：说明不写「尚未开始」',
    !el('steps-note').textContent.includes('尚未开始') && el('steps-note').textContent.includes('附加环节'),
    el('steps-note').textContent);

  // 右轨上下文（有数据）
  S.caseId = 'C1234ABCD';
  S.profile = { chief_complaint: '视物模糊3月', age: 58, sex: '男' };
  S.triage = { primary: { department: '内分泌科' }, reliability: { level: '中', reasons: ['仍有信息缺口'] } };
  S.gaps = ['眼底检查：信息不足', '心脏/冠脉检查：信息不足', '血常规：信息不足'];
  S.path = { tasks: [{ state: 'done' }, { state: 'todo' }, { state: 'todo' }] };
  renderContext();
  const rail = el('ctx-panel').innerHTML;
  check('右轨含当前病例与病例号', rail.includes('当前病例') && rail.includes('C1234ABCD'), '');
  check('右轨含可靠性等级', rail.includes('可靠性 中'), '');
  check('右轨列出信息缺口与条数', rail.includes('眼底检查') && rail.includes('(3)'), '');
  check('右轨含任务进度 1 / 3', rail.includes('已完成 1 / 3'), '');
  check('窄屏整宽上下文与右轨同源', el('ctx-inline-panel').innerHTML === rail, '');
  check('顶栏病例号同步', el('ctx-case').textContent.includes('C1234ABCD'), el('ctx-case').textContent);
  check('右轨声明未经医生审核', rail.includes('尚未经医生审核'), '');

  /* 报告段落渲染器：绝不能把对象拼成 [object Object]（"报告里冒出英文/乱码"缺陷的回归） */
  const nested = {
    '时间线': [{ '时间': '3月前', '类型': '症状', '内容': '视物模糊', '原文出处': 'S-1:8' }],
    '信息缺口': ['眼底检查：信息不足'],
    '是否需医生复核': true,
    '空字段': null,
    '证据卡片': [{
      '判断': '建议就诊方向：眼科（协作）', '证据状态': '证据间接支持',
      '支持证据': [{ '证据': '视物模糊 3 月', '原文出处': 'S-1:8', '强度': '直接引证' }],
      '反向 / 不足证据': ['（未发现反向证据）'],
      '外部依据': [{ '依据': '糖尿病视网膜病变防治专家共识', '来源': '中华医学会眼科学分会' }],
    }],
  };
  const html = renderSectionValue(nested);
  check('段落渲染不出现 [object Object]', !html.includes('[object Object]'), '');
  check('段落渲染不出现 undefined/NaN', !html.includes('undefined') && !html.includes('NaN'), '');
  check('对象渲染成定义列表', html.includes('<dl class="def">') && html.includes('<dt>证据状态</dt>'), '');
  check('对象数组渲染成卡片', (html.match(/class="rec"/g) || []).length >= 1,
    String((html.match(/class="rec"/g) || []).length));
  check('字符串数组渲染成列表', html.includes('<ul class="bullet">') && html.includes('眼底检查：信息不足'), '');
  check('布尔渲染成中文', html.includes('>是<'), '');
  check('空字段被跳过而不是显示怪值', !html.includes('空字段'), '');
  check('整段为空时显示（无）', renderSectionValue(null).includes('（无）')
    && renderSectionValue([]).includes('（无）') && renderSectionValue('').includes('（无）'), '');
  check('文本内容完整保留', html.includes('建议就诊方向：眼科（协作）') && html.includes('糖尿病视网膜病变防治专家共识'), '');
  check('纯字符串输入可直接渲染', renderSectionValue('一句话').includes('一句话'), '');
  check('数字输入用等宽展示', renderSectionValue(0.62).includes('class="mono"'), '');

  /* 后台 MDT 进度渲染（两段式）：必须显示"第几科/共几科"，失败时如实说明 */
  const running = mdtProgressHtml({ state: 'running', done: 1, total: 3, current: '眼科' });
  check('进度显示正在分析的科室', running.includes('正在分析 眼科') && running.includes('第 2 科'),
    running.slice(0, 80));
  check('进度显示已完成科数', running.includes('已完成 1 / 3'), '');
  check('进度条按科数计算', running.includes('width:33%'), '');
  check('进度文案解释为什么不显示概率', running.includes('独立研判'), '');
  /* 后台分析任务的状态横幅（渐进式）：三相位 + MDT 子步骤 + 失败可重试 */
  const bannerRun = analysisBanner({ state: 'running',
    phases: { profile: { state: 'done' }, triage: { state: 'running' }, mdt: { state: 'pending' } },
    mdt: { state: 'running', done: 1, total: 3, current: '眼科', step: '正在读取资料并形成意见卡' } });
  check('横幅显示三个相位', bannerRun.includes('画像模型增强') && bannerRun.includes('就医方向（模型发言）')
    && bannerRun.includes('多学科独立意见'), bannerRun.slice(0, 90));
  check('横幅区分完成/进行中', bannerRun.includes('已完成') && bannerRun.includes('进行中'), '');
  check('横幅显示 MDT 子步骤与第几科', bannerRun.includes('眼科') && bannerRun.includes('第 2 科')
    && bannerRun.includes('正在读取资料并形成意见卡'), '');
  check('横幅说明规则优先', bannerRun.includes('模型不会推翻规则命中的强项'), '');
  const single = analysisBanner({ state: 'done',
    phases: { mdt: { state: 'skipped' } },
    mdt: { state: 'skipped', step: '仅 1 个就医方向，无需多学科会诊' } });
  check('单科跳过时不显示为失败', !single.includes('danger') && !single.includes('未完成'), single.slice(0, 60));
  const bad = analysisBanner({ state: 'partial_failed',
    phases: { profile: { state: 'failed', note: 'RuntimeError: 网关不可用' } },
    mdt: { state: 'pending' } });
  check('失败横幅给出原因与重试入口',
    bad.includes('网关不可用') && bad.includes('retryAnalysis') && bad.includes('不会编造结论'),
    bad.slice(0, 120));
  const failed = mdtProgressHtml({ state: 'failed', error: 'ReadTimeout' });
  check('失败时如实说明且不编造', failed.includes('未完成') && failed.includes('ReadTimeout')
    && failed.includes('不会因此编造结论'), failed.slice(0, 80));
}

/* ----------------------------------------------------- 医生工作台断言 */
{
  eval(inlineScript('doctor.html') + '\n;globalThis.__D = { S, loadList, renderReview };');
  const D = globalThis.__D;

  fetchPayload = {
    status: 200,
    data: {
      cases: [
        { case_id: 'CAAA1111', sex: '男', age: 58, behavior_type: 'B', reliability: '中',
          case_state: 'awaiting_doctor', case_state_label: '待医生审核', pending_items: 3,
          primary_department: '内分泌科', report_version: 1, chief_complaint: '视物模糊3月' },
        { case_id: 'CBBB2222', sex: '女', age: 62, behavior_type: 'C', reliability: '高',
          case_state: 'doctor_confirmed', case_state_label: '医生已确认', pending_items: 0,
          primary_department: '眼科', report_version: 2, chief_complaint: '眼痛1周' },
      ],
    },
  };
  await D.loadList();
  const rc = el('rail-cases').innerHTML;
  check('医生左轨渲染病例列表', rc.includes('CAAA1111') && rc.includes('CBBB2222'), '');
  check('左轨病例带待审标记', rc.includes('待审 3'), '');
  check('左轨病例带已审完标记', rc.includes('已审完'), '');
  check('主区列表同样渲染', el('list-body').innerHTML.includes('CAAA1111'), '');
  check('列表计数文案', el('list-note').textContent.includes('共 2 例'), el('list-note').textContent);

  D.S.caseId = 'CAAA1111';
  D.S.review = {
    items: [], published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 10, pending: 4, human_touched: 6,
               by_state: { awaiting: 4, confirmed: 4, modified: 1, rejected: 1, uncertain: 0 } },
  };
  D.renderReview();
  check('左轨审核进度条 60%', el('rev-bar').style.width === '60%', el('rev-bar').style.width);
  check('左轨审核进度说明', el('rev-note').textContent.includes('人工操作 6 / 10'), el('rev-note').textContent);
  check('右轨审核面板有内容', el('rev-summary').innerHTML.includes('共 10 条'), '');
}

/* ------------------------------------------------------------------ 输出 */
let failed = 0;
for (const [ok, name, detail] of results) {
  if (!ok) failed++;
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' — ' + detail : ''}`);
}
console.log(`\n===== 前端渲染逻辑：${results.length - failed} 通过 / ${failed} 失败 =====`);
process.exit(failed ? 1 : 0);
