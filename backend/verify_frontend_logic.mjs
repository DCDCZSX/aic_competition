/**
 * 前端渲染逻辑校验（Node + 最小 DOM 桩，不需要浏览器）
 * ---------------------------------------------------------------------------
 * 为什么需要：工作台的流程导航、阶段内容和医生审核面板都由内联 JS 渲染
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
  const classes = new Set();
  return {
    id, innerHTML: '', textContent: '', className: '', value: '', dataset: {}, scrollTop: 0,
    style: { setProperty() { }, width: '' },
    classList: {
      add(...names) { names.forEach(name => classes.add(name)); },
      remove(...names) { names.forEach(name => classes.delete(name)); },
      toggle(name, force) {
        if (force === true || (force === undefined && !classes.has(name))) classes.add(name);
        else if (force === false || classes.has(name)) classes.delete(name);
        return classes.has(name);
      },
      contains(name) { return classes.has(name); },
    },
    setAttribute() { }, getAttribute() { return null; }, removeAttribute() { },
    addEventListener() { }, querySelector() { return null; }, querySelectorAll() { return []; },
    focus() { }, click() { },
  };
}
const el = id => (els.has(id) ? els.get(id) : (els.set(id, mkEl(id)), els.get(id)));

let fetchPayload = { status: 200, data: {} };
globalThis.document = {
  getElementById: el,
  querySelector: selector => selector === '.doctor-case-main' ? el('doctor-case-main') : null,
  querySelectorAll: selector => selector === 'main > section'
    ? ['stage1','stage2','stage3','stage4','records','update','changes','done'].map(id => el('s-'+id))
    : [],
  addEventListener() { }, title: '', fonts: { ready: Promise.resolve() }, body: mkEl('body'),
};
globalThis.window = {
  addEventListener() { }, scrollTo() { }, innerHeight: 900, scrollY: 0,
  matchMedia: () => ({ matches: false, addEventListener() { } }),
  localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
};
globalThis.localStorage = globalThis.window.localStorage;
globalThis.fetch = async () => ({ ok: true, status: fetchPayload.status, json: async () => fetchPayload.data });
globalThis.location = { href: '', reload() { } };
globalThis.alert = () => { };

/* ------------------------------------------------------------- 抽取内联 JS */
function inlineScript(file) {
  const src = fs.readFileSync(path.join(TPL, file), 'utf8');
  // 本地 /static/js 共享脚本按页面中的顺序一并载入；外链脚本不允许出现。
  const blocks = [...src.matchAll(/<script(?:\s+src="([^"]+)")?[^>]*>([\s\S]*?)<\/script>/g)].map(m => {
    if (!m[1]) return m[2];
    if (!m[1].startsWith('/static/js/')) throw new Error('unexpected external script: ' + m[1]);
    return fs.readFileSync(path.join(HERE, m[1].slice(1)), 'utf8');
  });
  return blocks.join('\n;\n');
}

const results = [];
const check = (name, cond, detail = '') => results.push([!!cond, name, detail]);

/* -------------------------------------------------------------- 患者端断言 */
{
  eval(inlineScript('patient.html') + '\n;globalThis.__caseFlow = {startNewCase, startCaseFlow, renderProfile, renderQuestions, go, loadPatientUploads, handlePatientImages, recognizePatientImage, goReport, openPatientCase, pollAnalysis, pollMdt, showPatientCaseHistory, showPatientReportVersion: typeof showPatientReportVersion === "function" ? showPatientReportVersion : null};' + '\n;globalThis.__P = { S, renderSteps, renderContext, syncStagePanels, STEPS, renderSectionValue, mdtProgressHtml, analysisBanner, sectionDigest, toggleAllReport, openAllForPrint, restoreAfterPrint, switchReport, patientFullReportEntries, patientFullReportValue, patientTimelineValue, patientPathFlowValue, renderTriage, renderPath, renderMdt, doPreview, registerForm: typeof registerForm === "function" ? registerForm : null, loadDepartments: typeof loadDepartments === "function" ? loadDepartments : null, renderReviewSubmission: typeof renderReviewSubmission === "function" ? renderReviewSubmission : null, submitCaseForReview: typeof submitCaseForReview === "function" ? submitCaseForReview : null, autoSubmitCaseForReview: typeof autoSubmitCaseForReview === "function" ? autoSubmitCaseForReview : null };');
  const { S, renderSteps, renderContext, STEPS, renderSectionValue, mdtProgressHtml, analysisBanner,
          syncStagePanels, sectionDigest, toggleAllReport, openAllForPrint, restoreAfterPrint, switchReport, patientFullReportEntries, patientFullReportValue, patientTimelineValue, patientPathFlowValue, renderTriage,
          renderPath, renderMdt, autoSubmitCaseForReview: pAutoSubmitCaseForReview,
          doPreview, registerForm: pRegisterForm, loadDepartments: pLoadDepartments,
          renderReviewSubmission: pRenderReviewSubmission, submitCaseForReview: pSubmitCaseForReview } = globalThis.__P;

  check('患者端主流程为 4 个阶段', STEPS.length === 4, String(STEPS.length));
  check('患者端提供注册函数', typeof pRegisterForm === 'function', 'registerForm');
  check('患者端提供科室目录加载函数', typeof pLoadDepartments === 'function', 'loadDepartments');
  check('患者端提供提交前分流渲染', typeof pRenderReviewSubmission === 'function', 'renderReviewSubmission');
  check('患者端提供病例提交审核函数', typeof pSubmitCaseForReview === 'function', 'submitCaseForReview');

  S.caseId = 'C-ROUTE';
  S.triage = {primary:{department:'内分泌科'}};
  S.departments = [{code:'endocrinology', name:'内分泌科', group:'internal'}];
  if(typeof pRenderReviewSubmission === 'function') pRenderReviewSubmission();
  check('患者端提供自动提交函数', typeof pAutoSubmitCaseForReview === 'function', 'autoSubmitCaseForReview');

  renderSteps('stage1');
  const h1 = el('steps').innerHTML;
  const count = (h1.match(/class="step[ "]/g) || []).length;
  check('左轨渲染出 4 个阶段项', count === 4, String(count));
  check('左轨含阶段名、序号圆点和状态说明', h1.includes('填写情况') && h1.includes('class="step-n" aria-hidden="true">4<') && h1.includes('当前步骤') && h1.includes('待开始'), '');
  check('当前阶段高亮', h1.includes('class="step active"'), '');
  check('第 1 阶段进度 0%', el('steps-bar').style.width === '0%', el('steps-bar').style.width);
  check('第 1 阶段说明为「第 1 / 4 步」', el('steps-note').textContent.includes('第 1 / 4 步'),
    el('steps-note').textContent);

  renderSteps('stage3');
  const h3 = el('steps').innerHTML;
  const done = (h3.match(/class="step done"/g) || []).length;
  check('已走过的 2 个阶段标记为 done', done === 2, String(done));
  check('已走过的阶段可点击回跳', h3.includes('href="#s-stage1"')
    && h3.includes('onclick="event.preventDefault();go(' + "'" + 'stage1' + "'" + ')"'), h3);
  check('第 3 阶段进度 67%', el('steps-bar').style.width === '67%', el('steps-bar').style.width);

  syncStagePanels('stage2');
  check('回看核对资料阶段直接显示画像和追问',
    !el('s-profile').classList.contains('hide') && !el('s-ask').classList.contains('hide'), '');
  syncStagePanels('stage3');
  check('回看查看建议阶段显示三个建议面板',
    !el('s-triage').classList.contains('hide') && !el('s-path').classList.contains('hide')
      && !el('s-mdt').classList.contains('hide'), '');

  // 就诊后更新 / 变化摘要不在首次准备的四个阶段内 → 主流程显示为已完成
  renderSteps('records');
  const hu = el('steps').innerHTML;
  check('附加环节：四个阶段全部完成', (hu.match(/class="step done"/g) || []).length === 4, '');
  check('附加环节：进度条 100%', el('steps-bar').style.width === '100%', el('steps-bar').style.width);
  check('附加环节：说明不写「尚未开始」',
    !el('steps-note').textContent.includes('尚未开始') && el('steps-note').textContent.includes('附加环节'),
    el('steps-note').textContent);

  // 患者端将病例号和状态放在顶栏下方的上下文行
  S.caseId = 'C1234ABCD';
  renderContext();
  check('病例上下文行同步', el('patient-ctx-case').textContent.includes('C1234ABCD'), el('patient-ctx-case').textContent);

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

  /* 患者端就医详情的逐段折叠：默认收起、展开状态可记忆、打印前自动全展开
     （打印版保留行动信息，屏幕折叠不能让无障碍丢内容） */
  check('段落摘要：数组显示条数', sectionDigest([1, 2, 3]).includes('3 条'), sectionDigest([1, 2, 3]));
  check('段落摘要：对象显示项数', sectionDigest({ a: 1, b: 2 }).includes('2 项'), sectionDigest({ a: 1, b: 2 }));
  check('段落摘要：空值显示"空"', sectionDigest(null).includes('空') && sectionDigest([]).includes('空'), '');
  check('段落摘要：长文本截断加省略号', sectionDigest('这是一段很长的说明文字需要被截断显示').includes('…'),
    sectionDigest('这是一段很长的说明文字需要被截断显示'));

  const fakeSecs = [
    { open: false, dataset: { idx: '0' }, addEventListener() { } },
    { open: true, dataset: { idx: '1' }, addEventListener() { } },
  ];
  const realQSA = globalThis.document.querySelectorAll;
  globalThis.document.querySelectorAll = sel => (String(sel).includes('details.sec') ? fakeSecs : []);
  S.reportOpen = {};
  toggleAllReport();
  check('「展开全部」把所有段落展开', fakeSecs.every(d => d.open), JSON.stringify(fakeSecs.map(d => d.open)));
  check('展开后按钮文案变为「收起全部」', el('rep-toggle-all').textContent === '收起全部',
    el('rep-toggle-all').textContent);
  check('展开状态被记住（用于重渲染）', S.reportOpen['0'] === true && S.reportOpen['1'] === true, '');
  toggleAllReport();
  check('再点一次全部收起', fakeSecs.every(d => !d.open), JSON.stringify(fakeSecs.map(d => d.open)));
  check('收起后按钮文案回到「展开全部」', el('rep-toggle-all').textContent === '展开全部',
    el('rep-toggle-all').textContent);

  /* 完整报告的屏幕折叠状态：打印版独立，打印后仍恢复原来的折叠状态 */
  fakeSecs[0].open = true;
  S.reportOpen = { 0: true };
  openAllForPrint();
  check('打印前自动展开全部段落', fakeSecs.every(d => d.open), JSON.stringify(fakeSecs.map(d => d.open)));
  restoreAfterPrint();
  check('打印后恢复原来的折叠状态', fakeSecs[0].open === true && fakeSecs[1].open === false,
    JSON.stringify(fakeSecs.map(d => d.open)));
  globalThis.document.querySelectorAll = realQSA;

  /* 患者端就医详情 HTML：默认收起 + 段落内容仍在 DOM 里 */
  S.report = { mdt_screening: { decision: 'recommend_mdt', departments: ['内分泌科', '眼科'], patient_next_step: '医生建议多学科共同评估，请按后续就医安排准备资料。' }, summary: { stage_label: 'AI 就诊准备摘要', report_version: 1, review_status_label: 'AI 生成，尚未经医生审核', doctor_confirmed: false, case_id: 'C1234ABCD', generated_at: '2026-09-24', sections: { '主要问题': '视物模糊', '建议咨询方向': '眼科', '当前任务': ['资料准备｜带上近期检查结果'], '需要向医生确认的问题': ['是否需要完善眼底检查'] } }, full: { sections: { '病例画像': '一句话概况', '时间线': [
    { '时间': '3月前', '类型': '症状', '内容': '视物模糊', '说明': '趋势：反复' },
    { '时间': '检查结果', '类型': '检查结果', '内容': '尿蛋白 ++' },
    { '时间': '当前用药', '类型': '当前用药', '内容': '二甲双胍 0.5g' }], '分诊结果': { '病例类型': 'C 型（诊断未明）', '牵头方向': '眼科', '协同方向': ['内分泌科'], '结果可靠性': '中', '规则校验说明': ['内部说明不展示'] }, '就医路径': [
      { '步骤': 'S1', '类型': '资料准备', '内容': '带上近期检查结果' },
      { '步骤': 'S2', '类型': '就诊', '内容': '先看眼科', '说明': '把摘要带给医生', '依赖步骤': 'S1' },
      { '步骤': 'S3', '类型': '资料准备', '内容': '同时整理用药清单', '依赖步骤': 'S1', '备注': '可与前一步并行' },
      { '步骤': 'S4', '类型': '复诊', '内容': '带结果复诊', '执行条件': '若医生安排了检查', '依赖步骤': 'S2', '备注': '需医生决定' }], '各专科 AI 视角': ['不应展示的专科推理'], '证据关联': ['不应展示的证据细节'], '审核记录与版本': { '报告版本': 1 } },
                       section_order: ['病例画像', '时间线', '分诊结果', '就医路径', '各专科 AI 视角', '证据关联', '审核记录与版本'], complete: true } };
  const patientTimeline = patientTimelineValue(S.report.full.sections['时间线']);
  check('患者病程变化只保留有时间的症状', patientTimeline.length === 1
    && patientTimeline[0]['时间'] === '3月前'
    && patientTimeline[0]['内容'] === '视物模糊', JSON.stringify(patientTimeline));
  check('患者病程变化不混入检查和用药', !JSON.stringify(patientTimeline).includes('尿蛋白')
    && !JSON.stringify(patientTimeline).includes('二甲双胍'), JSON.stringify(patientTimeline));
  check('没有真实病程事件时隐藏该段', patientFullReportEntries({
    section_order:['时间线'], sections:{'时间线':[
      { '时间':'检查结果', '类型':'检查结果', '内容':'尿蛋白 ++' },
      { '时间':'当前用药', '类型':'当前用药', '内容':'二甲双胍 0.5g' }]}
  }).length === 0, '');
  const pathFlow = patientPathFlowValue(S.report.full.sections['就医路径']);
  check('就医路径渲染为流程图', pathFlow.includes('patient-path-flow')
    && (pathFlow.match(/data-flow-step=/g) || []).length === 4, pathFlow);
  check('并行步骤进入同一阶段并列展示', pathFlow.includes('patient-path-flow-parallel-group')
    && pathFlow.includes('data-flow-stage="2"'), pathFlow);
  check('流程图使用纵向阶段轨道', pathFlow.includes('patient-path-flow-stage-marker')
    && pathFlow.includes('patient-path-flow-stage-content'), pathFlow);
  check('流程图保留必要条件但隐藏内部标签', pathFlow.includes('若医生安排了检查')
    && !pathFlow.includes('需医生确认') && !pathFlow.includes('就医咨询'), pathFlow);
  check('患者完整报告只保留就医相关段落', patientFullReportEntries(S.report.full).map(([key]) => key).join('|') === '病例画像|时间线|分诊结果|就医路径', '');
  const triageForPatient = patientFullReportValue('分诊结果', S.report.full.sections['分诊结果']);
  check('患者就医方向隐藏可靠性和规则校验', !JSON.stringify(triageForPatient).includes('可靠性')
    && !JSON.stringify(triageForPatient).includes('规则校验'), JSON.stringify(triageForPatient));
  S.reportOpen = {};
  switchReport('full');
  const fullHtml = el('report-body').innerHTML;
  switchReport('summary');
  const summaryHtml = el('report-body').innerHTML;
  check('摘要默认显示 AI 就诊准备阶段且不重复显示状态横条', summaryHtml.includes('AI 就诊准备摘要')
    && !summaryHtml.includes('版本 1') && !summaryHtml.includes('尚未经医生审核'), '');
  check('摘要不把 AI 阶段写成医生审核版', !summaryHtml.includes('已发布医生审核版'), '');
  check('打印版准备单保留行动信息', summaryHtml.includes('class="print-report"')
    && summaryHtml.includes('患者就医报告') && summaryHtml.includes('就医步骤')
    && summaryHtml.includes('当前待办') && summaryHtml.includes('就诊时可问医生')
    && summaryHtml.includes('若医生安排了检查'), '');
  check('患者看到医生初筛的下一步', summaryHtml.includes('医生建议多学科共同评估')
    && summaryHtml.includes('相关科室：内分泌科、眼科'), summaryHtml);

  check('完整报告渲染成可折叠段落', (fullHtml.match(/<details class="sec"/g) || []).length === 4,
    String((fullHtml.match(/<details class="sec"/g) || []).length));
  check('默认全部收起（没有 open 属性）', !fullHtml.includes(' open>'), '');
  check('每段都有独立的正文容器', (fullHtml.match(/class="sec-body"/g) || []).length === 4, '');
  check('收起状态也保留正文（打印/无障碍不丢内容）', fullHtml.includes('一句话概况')
    && fullHtml.includes('视物模糊'), '');
  check('就医详情不重复渲染左侧目录', !fullHtml.includes('id="report-toc"')
    && !fullHtml.includes('class="toc"'), fullHtml);
  check('折叠段带内容摘要提示', (fullHtml.match(/class="sec-hint"/g) || []).length === 4, '');
  check('患者报告将时间线命名为病程变化', fullHtml.includes('病程变化'), fullHtml);
  check('完整报告隐藏专业审核段落', !fullHtml.includes('各专科 AI 视角')
    && !fullHtml.includes('证据关联') && !fullHtml.includes('审核记录与版本')
    && !fullHtml.includes('可靠性') && !fullHtml.includes('规则校验说明'), fullHtml);
  const confirmedEntries = patientFullReportEntries({doctor_confirmed:true,
    section_order:['病例画像','医生最终意见','AI 与医生意见差异'],
    sections:{'病例画像':'一句话概况', '医生最终意见':[{ '内容':'按医生安排复诊' }], 'AI 与医生意见差异':['内部差异']}});
  check('有医生确认内容时保留医生最终意见', confirmedEntries.map(([key])=>key).join('|') === '病例画像|医生最终意见', '');
  check('有「展开全部」按钮', fullHtml.includes('id="rep-toggle-all"') && fullHtml.includes('展开全部'), '');
  S.reportOpen = { 1: true };
  switchReport('full');
  check('记过的展开状态在重渲染后保留', el('report-body').innerHTML.includes(' open>'), '');

  /* 分诊页（P6）：患者看到的必须是中文，不能是内部代码/内部术语
     （真实缺陷：`决策路径 rule-dominant`、`（G4 门禁）`、`编排结果（安全约束后）` 都直接怼给患者） */
  S.triage = {
    primary: { department: '内分泌科', role: '牵头', signals: ['血糖异常 → 内分泌科'] },
    secondary: [{ department: '眼科', role: '可咨询', signals: ['视物模糊 → 眼科'] }],
    selected: ['内分泌科', '眼科'],
    reliability: { level: '中', reasons: ['部分资料缺失'] },
    decision_path: 'rule-dominant', decision_path_label: '以规则为主（模型仅作补充）',
    behavior_type: 'B', behavior_type_label: 'B 型（多病共病）',
    rule_hits: [{ department: '内分泌科' }], needs_mdt: true, needs_doctor_review: true,
    conflicts: [], validator_notes: [],
  };
  renderTriage();
  const tri = el('triage-body').innerHTML;
  check('分诊页只展示就医方向',
    tri.includes('内分泌科') && tri.includes('眼科') && !tri.includes('规则命中')
      && !tri.includes('模型受限发言') && !tri.includes('rule-dominant'), tri);
  check('分诊页隐藏可靠性和病例类型等内部信息',
    !tri.includes('可靠性') && !tri.includes('病例类型') && !tri.includes('决策方式'), tri);
  check('分诊页不重复展示全局审核状态提示', !tri.includes('未经医生审核'), tri);

  /* 路径与任务合并：以纵向流程图展示步骤和对应行动，不渲染任务控件 */
  S.path = {steps: [
    {step_id: 'S1', title: '先做好资料准备', detail: '整理近期检查报告'},
    {step_id: 'S2', title: '挂内分泌科（牵头方向）', detail: '带上就医摘要'},
    {step_id: 'S4', title: '带检查结果复诊', detail: '若医生安排了检查'},
  ], tasks: [
    {task_id: 'T-001', type: '资料准备', content: '整理近 3 个月血糖记录', priority: 'high', state: 'todo', source: 'path:内分泌科'},
    {task_id: 'T-002', type: '就医咨询', content: '向医生确认是否需要复查', priority: 'high', state: 'todo', source: '内分泌科:checks'},
    {task_id: 'T-003', type: '就诊后资料更新', content: '就诊后上传新检查结果', priority: 'medium', state: 'todo', source: 'path:followup'},
  ]};
  renderPath();
  const pathHtml = el('path-body').innerHTML;
  check('路径页面展示为纵向流程图并合并行动内容',
    pathHtml.includes('patient-path-flow') && pathHtml.includes('整理近 3 个月血糖记录')
      && pathHtml.includes('向医生确认是否需要复查'), pathHtml);
  check('路径页面不展示任务标签和操作控件', !pathHtml.includes('路径中的任务')
    && !pathHtml.includes('开始') && !pathHtml.includes('重要'), pathHtml);

  /* 专科参考意见：保留通俗摘要，隐藏置信度、确认问题、不确定性和聚合技术词 */
  S.mdt = {card_count: 1, departments: ['眼科'], analyses: [
    {department: '眼科', status: 'ok', focus: '眼底与视网膜',
      inferences: [{text: '可能需要进一步了解眼底情况', confidence: 'medium'}],
      checks: [{content: '建议向医生确认是否需要眼底检查'}], referral: null},
  ], aggregation: {
    consensus: [{topic: '眼底检查'}], disagreements: [], uncertainties: [{topic: '信息不足'}],
    open_questions: ['是否需要眼科随诊'],
  }};
  renderMdt();
  const mdtHtml = el('mdt-body').innerHTML;
  check('专科参考意见使用患者能理解的摘要',
    mdtHtml.includes('参考提示') && mdtHtml.includes('可能需要进一步了解眼底情况'), mdtHtml);
  check('专科参考意见隐藏确认问题和不确定性',
    !mdtHtml.includes('建议向医生确认') && !mdtHtml.includes('还不确定')
      && !mdtHtml.includes('共识') && !mdtHtml.includes('medium'), mdtHtml);

  /* 后台 MDT 进度渲染（两段式）：必须显示"第几科/共几科"，失败时如实说明 */
  const running = mdtProgressHtml({ state: 'running', done: 1, total: 3, current: '眼科' });
  check('进度显示正在分析的科室', running.includes('正在分析 眼科') && running.includes('第 2 科'),
    running.slice(0, 80));
  check('进度显示已完成科数', running.includes('已完成 1 / 3'), '');
  check('进度条按科数计算', running.includes('width:33%'), '');
  check('进度文案使用患者能理解的表达', running.includes('正在整理各专科建议')
    && !running.includes('独立研判') && !running.includes('共识与分歧聚合'), '');
  /* 后台分析任务的状态横幅（渐进式）：三相位 + MDT 子步骤 + 失败可重试 */
  const bannerRun = analysisBanner({ state: 'running',
    phases: { profile: { state: 'done' }, triage: { state: 'running' }, mdt: { state: 'pending' } },
    mdt: { state: 'running', done: 1, total: 3, current: '眼科', step: '正在读取资料并形成意见卡' } });
  check('横幅显示三个相位', bannerRun.includes('整理资料') && bannerRun.includes('判断就医方向')
    && bannerRun.includes('整理相关科室建议'), bannerRun.slice(0, 90));
  check('横幅区分完成/进行中', bannerRun.includes('已完成') && bannerRun.includes('进行中'), '');
  check('横幅不堆叠相关科室过程小字', !bannerRun.includes('第 2 科')
    && !bannerRun.includes('正在整理眼科的参考意见'), '');
  check('横幅不泄漏内部阶段术语', !bannerRun.includes('模型发言')
    && !bannerRun.includes('独立意见'), '');
  check('整理进度卡显示大号百分比、当前步骤一句话、等待秒数与三步状态',
    bannerRun.includes('data-analysis-pct') && bannerRun.includes('正在判断就医方向') && bannerRun.includes('data-analysis-elapsed')
      && (bannerRun.match(/class="apc-step /g)||[]).length===3 && bannerRun.includes('apc-step is-running'), '');
  {
    const doneCard = analysisBanner({ state: 'done', phases: { profile:{state:'done'}, triage:{state:'done'}, mdt:{state:'done'} }, mdt:{state:'done'} });
    check('整理完成后进度卡为 100% 且不再显示进行中动画', doneCard.includes('is-done') && doneCard.includes('width:100%') && !doneCard.includes('apc-bar is-running'), '');
  }
  const single = analysisBanner({ state: 'done',
    phases: { mdt: { state: 'skipped' } },
    mdt: { state: 'skipped', step: '仅 1 个就医方向，无需多学科会诊' } });
  check('单科跳过时不显示为失败', !single.includes('danger') && !single.includes('未完成'), single.slice(0, 60));
  const bad = analysisBanner({ state: 'partial_failed',
    phases: { profile: { state: 'failed', note: 'RuntimeError: 网关不可用' } },
    mdt: { state: 'pending' } });
  check('失败横幅给出原因与重试入口',
    bad.includes('部分建议暂未整理完成') && bad.includes('整理资料：暂未完成')
      && bad.includes('retryAnalysis'),
    bad.slice(0, 120));
  const failed = mdtProgressHtml({ state: 'failed', error: 'ReadTimeout' });
  check('失败时如实说明且不编造', failed.includes('未完成') && failed.includes('ReadTimeout')
    && failed.includes('不会因此编造结论'), failed.slice(0, 80));

  /* 完成一轮后新建：空提交只提示填写，不请求接口、不显示上一病例。 */
  const savedFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, opts) => {
    requests.push({url, body: JSON.parse(opts.body || '{}')});
    const data = opts.method === 'DELETE' ? {ok:true} : url === '/api/v2/preview'
      ? {preview_token:'preview-new', blocked:false, blocking_hit_count:0}
      : JSON.parse(opts.body).preview_token === 'preview-new' && el('history').value.trim()
        ? {ok:true, passes_gate:true, case_id:'C-NEW', profile:{chief_complaint:el('history').value.trim()},
           gaps:[], questions:[], analysis_mode:'rules_only'}
        : {blocked:true, advice:'请填写病历正文或主要问题'};
    return {ok:true, status:200, json:async () => data};
  };
  S.caseId='C-OLD'; S.profile={chief_complaint:'旧病历的视物模糊'};
  S.questions=[{text:'旧病历的追问', why:'补充病情', affects:[]}];
  S.report={summary:'旧病历报告'};
  __caseFlow.renderProfile({profile:S.profile, gaps:['旧病历缺口']});
  __caseFlow.renderQuestions();
  el('report-body').innerHTML='旧病历报告';
  el('f-sex').value='男';
  __caseFlow.go('stage4');
  await __caseFlow.startNewCase();
  check('新建病例清空已渲染的旧画像、缺口、追问和报告',
    ['profile-body','gap-body','ask-body','report-body'].every(id => el(id).innerHTML === ''),
    el('profile-body').innerHTML);
  check('新建病例清空病例状态和性别输入',
    S.caseId === null && S.profile === null && S.report === null && !S.questions.length && el('f-sex').value === '', '');
  for(const blank of ['', ' \n\t ']){
    el('history').value=blank; el('main-issue').value=blank;
    requests.length=0;
    await __caseFlow.startCaseFlow();
    check('空白新病例停留填写阶段：'+JSON.stringify(blank),
      !el('s-stage1').classList.contains('hide') && el('s-stage2').classList.contains('hide'), '');
    check('空白新病例只提示填写且不请求预览或创建：'+JSON.stringify(blank),
      el('intake-msg').innerHTML.includes('请填写') && requests.length === 0 && S.caseId === null,
      el('intake-msg').innerHTML);
  }
  el('history').value='新病历的膝关节疼痛两周';
  await __caseFlow.startCaseFlow();
  check('补充新内容后正常创建新病例并进入核对阶段',
    S.caseId === 'C-NEW' && !el('s-stage2').classList.contains('hide')
      && el('profile-body').innerHTML.includes('新病历的膝关节疼痛两周')
      && !el('profile-body').innerHTML.includes('视物模糊') && requests.length === 2, '');
  globalThis.fetch=savedFetch;

  /* 上传草稿：刷新保留，新增清空；迟到的恢复、OCR 和上传结果不跨病例。 */
  const {loadPatientUploads, handlePatientImages, recognizePatientImage} = __caseFlow;
  const imageBlob = new Blob(['image-data'], {type:'image/png'});
  let storedUploads=[{upload_id:'old-image', original_name:'old.png', file_url:'/test-image/old-image'}];
  const uploadResponse = (data, status=200) => ({ok:status<400, status, json:async()=>data, blob:async()=>imageBlob});
  const normalUploadFetch = async (url, opts={}) => {
    if(url === '/api/v2/patient/uploads' && opts.method === 'DELETE'){
      storedUploads=[]; return uploadResponse({ok:true});
    }
    if(url === '/api/v2/patient/uploads' && opts.method === 'POST'){
      const item={upload_id:'next-image', original_name:'next.png', file_url:'/test-image/next-image'};
      storedUploads.push(item); return uploadResponse({uploads:[item]},201);
    }
    if(url === '/api/v2/patient/uploads') return uploadResponse({uploads:[...storedUploads]});
    if(url.startsWith('/test-image/')) return uploadResponse({});
    if(url.endsWith('/ocr')) return uploadResponse({text:''});
    if(opts.method === 'DELETE'){
      storedUploads=storedUploads.filter(item=>!url.endsWith('/'+item.upload_id));
      return uploadResponse({ok:true});
    }
    throw new Error('Unexpected upload request '+url);
  };
  globalThis.fetch=normalUploadFetch;
  await loadPatientUploads(); await loadPatientUploads();
  check('刷新恢复当前上传图片且不删除暂存文件',
    S.imageUploads.length === 1 && storedUploads.length === 1
      && el('patient-upload-grid').innerHTML.includes('old.png'), '');
  el('patient-images').value='old.png'; el('patient-upload-msg').innerHTML='旧图片已识别';
  await __caseFlow.startNewCase();
  check('新增病例清空缩略图、文件选择和上传提示',
    S.imageUploads.length === 0 && el('patient-upload-grid').innerHTML === ''
      && el('patient-images').value === '' && el('patient-upload-msg').innerHTML === '', '');
  await loadPatientUploads();
  check('新增后刷新不会恢复上一份图片', storedUploads.length === 0 && S.imageUploads.length === 0, '');
  await handlePatientImages({target:{files:[Object.assign(imageBlob,{name:'next.png'})],value:'next.png'}});
  await loadPatientUploads();
  check('新病例上传的新图片仍可在刷新后恢复',
    S.imageUploads.length === 1 && el('patient-upload-grid').innerHTML.includes('next.png'), '');

  let finishRestore;
  globalThis.fetch=(url,opts={}) => url === '/api/v2/patient/uploads' && !opts.method
    ? new Promise(resolve=>{finishRestore=resolve;}) : normalUploadFetch(url,opts);
  const restoring=loadPatientUploads();
  await __caseFlow.startNewCase();
  finishRestore(uploadResponse({uploads:[{upload_id:'next-image',original_name:'next.png',file_url:'/test-image/next-image'}]}));
  await restoring;
  check('新增后迟到的图片恢复响应不能重新填回旧缩略图',
    S.imageUploads.length === 0 && el('patient-upload-grid').innerHTML === '', '');

  let finishOcr;
  const oldItem={upload_id:'ocr-old', name:'ocr-old.png', url:URL.createObjectURL(imageBlob)};
  S.imageUploads=[oldItem];
  globalThis.fetch=(url,opts) => url.endsWith('/ocr')
    ? new Promise(resolve=>{finishOcr=resolve;}) : normalUploadFetch(url,opts);
  const recognizing=recognizePatientImage(oldItem);
  await __caseFlow.startNewCase();
  finishOcr(uploadResponse({text:'上一病例的识别文字'}));
  await recognizing;
  check('新增后迟到的 OCR 不写入新病例正文或上传提示',
    el('history').value === '' && el('patient-upload-msg').innerHTML === '', el('history').value);

  let finishUpload;
  globalThis.fetch=(url,opts={}) => url === '/api/v2/patient/uploads' && opts.method === 'POST'
    ? new Promise(resolve=>{finishUpload=resolve;}) : normalUploadFetch(url,opts);
  const uploading=handlePatientImages({target:{files:[imageBlob],value:'next.png'}});
  await __caseFlow.startNewCase();
  storedUploads=[{upload_id:'late-image',original_name:'late.png',file_url:'/test-image/late-image'}];
  finishUpload(uploadResponse({uploads:[...storedUploads]},201));
  await uploading; await loadPatientUploads();
  check('新增前发出的上传晚完成时清理后端文件且不恢复旧图片',
    S.imageUploads.length === 0 && storedUploads.length === 0, '');

  S.caseId='C-KEPT'; S.imageUploads=[{upload_id:'kept-image',name:'kept.png',url:URL.createObjectURL(imageBlob)}];
  globalThis.fetch=async()=>uploadResponse({error:'暂存图片清理失败'},500);
  await __caseFlow.startNewCase();
  check('清理失败时提示重试并保留当前病例和图片',
    S.caseId === 'C-KEPT' && S.imageUploads.length === 1 && el('patient-upload-msg').innerHTML.includes('失败'), '');
  globalThis.fetch=normalUploadFetch;
  await __caseFlow.startNewCase();
  globalThis.fetch=savedFetch;

  /* 医生发布后，已有报告和病例列表入口必须读取最新结果。 */
  const resultRequests=[];
  globalThis.fetch=async url=>{
    resultRequests.push(url);
    if(url.endsWith('/patient-record')) return {status:200,json:async()=>({record:{main_issue:'已保存的主要问题',answers:{symptom_duration:'已保存的追问回答'},submitted_material:{medical_history:'选中病例的原始资料'}}})};
    if(url.endsWith('/analysis')) return {status:200,json:async()=>({profile:{age:58,chief_complaint:'选中病例的病情'},triage:{primary:{department:'眼科'}},path:{steps:[]},questions:[{id:'Q1',field:'symptom_duration',text:'不适持续多久？',why:'核对病程'}],analysis:{state:'done'}})};
    return {status:200,json:async()=>({
      summary:{doctor_confirmed:true,report_version:2,sections:{}},full:{sections:{}},
      doctor_result:{published:true,final_advice:'医生新发布的复诊安排',published_by:'审核医生',published_at:'2026-10-07',report_version:2}
    })};
  };
  S.caseId='C-RESULT'; S.routing={status:'submitted'};
  S.report={summary:{doctor_confirmed:false,sections:{}},full:{sections:{}}};
  await __caseFlow.goReport();
  check('再次打开报告重新获取医生已发布结果，不使用旧缓存',
    resultRequests.length === 1 && el('doctor-result-body').innerHTML.includes('医生新发布的复诊安排'), '');
  S.report=null; S.analysisStatus=null; S.triage=null; S.path=null; S.mdt=null;
  S.profile={age:99,chief_complaint:'另一条病例的病情'}; el('history').value='另一条病例的原文';
  await __caseFlow.openPatientCase('C-HISTORY');
  check('从病例列表打开已保存报告不受新建分析进度门禁阻挡',
    resultRequests.includes('/api/v2/case/C-HISTORY/report') && S.report.summary.doctor_confirmed, '');
  check('打开病例同步恢复选中病例资料和核对信息，不沿用上一条病例',
    S.profile.age===58 && S.triage.primary.department==='眼科' && el('history').value==='选中病例的原始资料' && el('main-issue').value==='已保存的主要问题', '');
  check('回看核对资料时恢复对应问题的已保存回答',el('ans-0').value==='已保存的追问回答',el('ans-0').value);
  S.report={summary:{doctor_confirmed:false,sections:{}},full:{sections:{}},doctor_result:{published:false,final_advice:'未发布草稿'}};
  switchReport('summary');
  check('未发布时仅显示审核等待状态，不冒充医生最终建议',
    el('doctor-result-body').innerHTML.includes('尚未发布') && !el('doctor-result-body').innerHTML.includes('未发布草稿'), '');
  {
    const finalReport={case_id:'C-FINAL',header:{报告名称:'多学科会诊意见报告',报告编号:'C-FINAL-v2',主责科室:'眼科',审核医生:'审核医生'},
      section_order:['医生审核后的专科评估意见','医生最终建议','后续安排'],
      sections:{医生审核后的专科评估意见:[{科室:'眼科',专科评估意见:['眼科要点<b>'],建议向医生确认:['是否需要复查眼底'],审核:'医生已确认'}],
        医生最终建议:'两周后眼科复诊',后续安排:['会诊后续安排：携带检查资料']},disclaimer:'合成免责声明'};
    S.report={summary:{doctor_confirmed:true,sections:{主要问题:'就诊前问题'}},full:{sections:{}},patient_final_report:finalReport,
      doctor_result:{published:true,final_advice:'两周后眼科复诊',published_by:'审核医生',published_at:'2026-10-10T09:30:00',report_version:2}};
    switchReport('summary');
    const resultHtml=el('doctor-result-body').innerHTML;
    check('医生发布后患者结果页展示会诊意见单样式的最终报告',
      resultHtml.includes('final-report') && resultHtml.includes('多学科会诊意见报告') && resultHtml.includes('眼科要点&lt;b&gt;') && resultHtml.includes('两周后眼科复诊') && !resultHtml.includes('预览 · 尚未发布'), resultHtml.slice(0,200));
    check('发布后结果页元信息时间去掉 ISO 分隔符',resultHtml.includes('2026-10-10 09:30'),'');
    check('发布后原摘要区改称就诊前准备资料',el('report-prep-heading').textContent==='就诊前准备资料' && el('report-prep-note').hidden===false,'');
    const printHtml=el('report-body').innerHTML;
    check('发布后打印版改为最终报告，不再打印就诊前门诊病历',
      printHtml.includes('print-final-report') && printHtml.includes('C-FINAL-v2') && !printHtml.includes('门诊病历'),'');
    S.report={summary:{doctor_confirmed:false,sections:{主要问题:'就诊前问题'}},full:{sections:{}},patient_final_report:finalReport,doctor_result:{published:false}};
    switchReport('summary');
    check('未发布时即使带有最终报告字段也不展示，打印仍为就诊准备单',
      !el('doctor-result-body').innerHTML.includes('final-report') && el('report-body').innerHTML.includes('门诊病历') && el('report-prep-heading').textContent==='重点摘要与就医详情','');
    check('就诊准备单打印版末尾不再附免责声明与“本系统不能做什么”两行',
      el('report-body').innerHTML.includes('门诊病历') && !el('report-body').innerHTML.includes('print-disclaimer') && !el('report-body').innerHTML.includes('本系统不能做什么'), '');
  }
  globalThis.fetch=savedFetch;

  /* 历史记录只展示事件，报告必须来自已保存快照并与当前四步内容分离。 */
  const __history=__caseFlow;
  const historyRequests=[];
  const historyRecord={title:'病例 #C-HISTORY · 视物模糊',submitted_material:{submitted_at:'2026-10-01 09:00',medical_history:'不应重复展示的原文',profile:{exams:[{item:'眼底',value:'旧结果'}]}},updates:[{kind:'检查结果',content:'新增检查资料',at:'2026-10-03 09:00'}]};
  globalThis.fetch=async url=>{
    historyRequests.push(url);
    const data=url.endsWith('/patient-record')?{record:historyRecord}:url.endsWith('/versions')?{versions:[{version:2,published_at:'2026-10-02 10:00'}]}:{
      version:2,published_at:'2026-10-02 10:00',report:{summary:{sections:{'主要问题':'当时的问题<script>unsafe</script>'}},full:{sections:{}},doctor_result:{published:true,final_advice:'旧版本的医生安排'}}};
    return {status:200,json:async()=>data};
  };
  await __history.showPatientCaseHistory('C-HISTORY');
  const historyHtml=el('records-body').innerHTML;
  check('历史记录只保留提交补充发布事件，不重复原文或画像 JSON',
    historyHtml.includes('首次提交') && historyHtml.includes('新增检查资料') && historyHtml.includes('查看此版本') && !historyHtml.includes('不应重复展示') && !historyHtml.includes('结构化资料摘要') && !historyHtml.includes('"item"'),historyHtml);
  check('历史时间线按时间排序且不请求无关变化分析接口',
    historyHtml.indexOf('2026-10-01')<historyHtml.indexOf('2026-10-02') && historyHtml.indexOf('2026-10-02')<historyHtml.indexOf('2026-10-03') && historyRequests.length===2, '');
  const currentReport=S.report;
  if(__history.showPatientReportVersion) await __history.showPatientReportVersion('C-HISTORY',2);
  check('历史报告展示保存的建议并隔离当前病例报告状态',
    !!__history.showPatientReportVersion && el('patient-history-report').innerHTML.includes('旧版本的医生安排') && S.report===currentReport, '');
  globalThis.fetch=async url=>({status:200,json:async()=>({version:3,published_at:'2026-10-09 10:00',report:{
    summary:{sections:{'主要问题':'当时的就诊前问题'}},full:{sections:{}},
    patient_final_report:{header:{报告名称:'专科评估意见报告',报告编号:'C-HISTORY-v3'},section_order:['医生审核后的专科评估意见','医生最终建议','后续安排'],
      sections:{医生审核后的专科评估意见:[{科室:'眼科',专科评估意见:['历史快照要点'],审核:'医生已修改'}],医生最终建议:'历史快照建议',后续安排:['按医生建议执行']}},
    doctor_result:{published:true,final_advice:'历史快照建议'}}})});
  if(__history.showPatientReportVersion) await __history.showPatientReportVersion('C-HISTORY',3);
  {
    const versionHtml=el('patient-history-report').innerHTML;
    check('历史版本有最终报告快照时按会诊意见单展示，就诊前资料收进折叠区',
      versionHtml.includes('final-report') && versionHtml.includes('C-HISTORY-v3') && versionHtml.includes('历史快照要点')
        && versionHtml.includes('patient-history-prep') && versionHtml.indexOf('final-report')<versionHtml.indexOf('当时的就诊前问题'), '');
  }
  globalThis.fetch=async url=>({status:200,json:async()=>({version:2,published_at:'2026-10-02 10:00',report:{summary:{sections:{'主要问题':'当时的问题<script>unsafe</script>'}},full:{sections:{}},doctor_result:{published:true,final_advice:'旧版本的医生安排'}}})});
  if(__history.showPatientReportVersion) await __history.showPatientReportVersion('C-HISTORY',2);
  check('历史报告内容转义且提供返回历史操作',
    el('patient-history-report').innerHTML.includes('&lt;script&gt;') && !el('patient-history-report').innerHTML.includes('<script>') && el('patient-history-report').innerHTML.includes('返回历史记录'), '');
  globalThis.fetch=async url=>({status:200,json:async()=>url.endsWith('/patient-record')?{record:{title:'旧病例',updates:[]}}:{versions:[]}});
  await __history.showPatientCaseHistory('C-EMPTY');
  check('历史为空时不编造提交或发布记录',el('records-body').innerHTML.includes('暂无可显示的历史记录') && el('records-body').innerHTML.includes('暂无已保存的报告版本') && !el('records-body').innerHTML.includes('首次提交病情资料'), '');
  const pendingHistory=[];
  globalThis.fetch=url=>url.includes('/C-SLOW/')?new Promise(resolve=>pendingHistory.push(()=>resolve({status:200,json:async()=>url.endsWith('/patient-record')?{record:{title:'迟到的旧病例'}}:{versions:[]}}))):Promise.resolve({status:200,json:async()=>url.endsWith('/patient-record')?{record:{title:'最新选择的病例'}}:{versions:[]}});
  const slowHistory=__history.showPatientCaseHistory('C-SLOW');
  await __history.showPatientCaseHistory('C-NEW');
  pendingHistory.forEach(finish=>finish()); await slowHistory;
  check('迟到的历史请求不能覆盖新选择的病例',el('records-body').innerHTML.includes('最新选择的病例') && !el('records-body').innerHTML.includes('迟到的旧病例'), '');
  globalThis.fetch=async()=>({status:404,json:async()=>({error:'该报告版本未保存'})});
  if(__history.showPatientReportVersion) await __history.showPatientReportVersion('C-NEW',9);
  check('历史版本加载失败提示真实原因且不回退当前报告',el('patient-history-report').innerHTML.includes('该报告版本未保存') && !el('patient-history-report').innerHTML.includes('医生新发布') && S.report===currentReport, '');
  const activeProfile=S.profile;
  S.caseId='C-OLD-POLL';
  let finishAnalysis;
  globalThis.fetch=()=>new Promise(resolve=>{finishAnalysis=resolve;});
  const oldAnalysis=__caseFlow.pollAnalysis();
  S.caseId='C-NEW-POLL';
  finishAnalysis({ok:true,status:200,json:async()=>({case_id:'C-OLD-POLL',profile:{age:99},analysis:{state:'done'}})});
  await oldAnalysis;
  check('切换病例后迟到的旧分析不能覆盖新病例资料',S.profile===activeProfile,'');
  S.caseId='C-OLD-MDT'; S.mdt={case:'new'}; const activeMdt=S.mdt;
  let finishMdt;
  globalThis.fetch=()=>new Promise(resolve=>{finishMdt=resolve;});
  const oldMdt=__caseFlow.pollMdt();
  S.caseId='C-NEW-MDT';
  finishMdt({status:200,json:async()=>({ready:true,mdt:{case:'old'},status:{state:'done'}})});
  await oldMdt;
  check('切换病例后迟到的旧专科意见不能覆盖新病例报告',S.mdt===activeMdt && S.report===currentReport,'');
  globalThis.fetch=savedFetch;

  /* API 失败时必须把服务端原因告诉患者，不能统一吞成“预览失败” */
  el('history').value = '2型糖尿病5年，视物模糊3个月。';
  el('main-issue').value = '该看哪个科';
  fetchPayload = { status: 401, data: { error: '未登录或会话已过期，请先登录' } };
  await doPreview();
  check('预览失败显示服务端原因', el('intake-msg').innerHTML.includes('会话已过期'), el('intake-msg').innerHTML);
}

/* ----------------------------------------------------- 医生工作台断言 */
{
  const doctorHtml = fs.readFileSync(path.join(TPL, 'doctor.html'), 'utf8');
  eval(inlineScript('doctor.html') + '\n;globalThis.__D = { S, go, openCase, doctorWorkflowNav, openModal, submitModal, decide, submitTriageConfirmation, startDoctorMdtSimulation: typeof startDoctorMdtSimulation === "function" ? startDoctorMdtSimulation : null, refreshDoctorMdtSimulation: typeof refreshDoctorMdtSimulation === "function" ? refreshDoctorMdtSimulation : null, renderPacket: typeof renderPacket === "function" ? renderPacket : null, submitDisagreement, submitMdtFormalRecord, publish, toggleQueueDetails: typeof toggleQueueDetails === "function" ? toggleQueueDetails : null, goPendingReviewItem: typeof goPendingReviewItem === "function" ? goPendingReviewItem : null, previewPublication, publicationPreviewHtml, finalReportDocumentHtml, updateFinalAdvice, loadList, renderOverview, renderTriage, renderEvidence, renderMdt, renderReview, renderHistory, renderMdtScope, renderQueueFilters, setQueueFilter, goQueuePage, renderCaseContext, openHistoryDecision, openHistoryVersion, openReviewCategory: (...args) => window.openReviewCategory(...args) };');
  const D = globalThis.__D;
  check('病例详情导航只突出主要任务',
    ['overview','triage','mdt','review'].every(key=>doctorHtml.includes(`data-s="${key}"`))
      && !doctorHtml.includes('data-s="evidence"'), '');
  check('病例详情目录位于左侧布局栏',
    doctorHtml.includes('class="doctor-case-layout"')
      && doctorHtml.includes('class="doctor-case-nav view-nav"')
      && doctorHtml.includes('class="doctor-case-main"'), '');
  check('病例详情桌面端仅主内容区滚动',
    doctorHtml.includes('body.doctor-case-mode')
      && doctorHtml.includes('#doctor-shell.has-case .doctor-case-main')
      && doctorHtml.includes('overflow-y:auto'), '');
  D.S.caseId = 'C84AA54AE';
  D.renderCaseContext({ behavior_type: 'C', reliability: { level: '高' } });
  check('病例上下文拆分为病例号、类型和可靠性',
    el('case-context-id').textContent.includes('C84AA54AE')
      && el('case-context-type').textContent === 'C'
      && el('case-context-reliability').textContent === '高',
    `${el('case-context-id').textContent} / ${el('case-context-type').textContent} / ${el('case-context-reliability').textContent}`);

  check('待审队列不重复显示当前科室提示', !doctorHtml.includes('id="doctor-scope-note"')
    && !doctorHtml.includes('function renderDoctorScope'), '');

  D.S.caseData = { payload: { triage: { selected: ['内分泌科', '眼科'] } } };
  D.S.review = { mdt_screening: null,triage_doctor_confirmation:{status:'confirmed',selected:['内分泌科']} };
  const scopePanel = D.renderMdtScope();
  check('未确认处理方向时只引导回分流，不重复选择科室',scopePanel.includes('先完成分流确认') && scopePanel.includes("go('triage')") && !scopePanel.includes('data-mdt-department'),scopePanel);

  fetchPayload = {
    status: 200,
    data: {
      cases: [
        { case_id: 'CAAA1111', sex: '男', age: 58, behavior_type: 'B', reliability: '中',
          case_state: 'awaiting_doctor', case_state_label: '待医生审核', pending_items: 3,
          primary_department: '内分泌科', report_version: 1, chief_complaint: '视物模糊3月',
          risk_level: 'high', analysis_state: 'done', exempt_items: 2 },
        { case_id: 'CBBB2222', sex: '女', age: 62, behavior_type: 'C', reliability: '高',
          case_state: 'doctor_confirmed', case_state_label: '医生已确认', pending_items: 0,
          primary_department: '眼科', report_version: 2, chief_complaint: '眼痛1周',
          risk_level: 'low', analysis_state: 'done', exempt_items: 0 },
      ],
      total: 2, has_more: false, filters: {}, sorts: {},
    },
  };
  await D.loadList();
  const queue = el('list-body').innerHTML;
  check('医生主区渲染完整病例队列', queue.includes('CAAA1111') && queue.includes('CBBB2222'), '');
  check('队列显示风险与待医生处理量', queue.includes('高风险') && queue.includes('<strong>3</strong>必审'), '');
  check('队列显示自动处理条数', queue.includes('<strong>2</strong>免审'), '');
  check('病例队列使用服务端分页元数据', el('list-pager').innerHTML.includes('共 2 例'), '');
  check('左轨改为筛选入口而非病例堆叠', !el('rail-filters').innerHTML.includes('CAAA1111'), '');
  check('队列默认只显示五个核心字段',
    ['病例','主诉 / 牵头科室','风险','处理状态','更新'].every(x=>queue.includes(x))
    && !queue.includes('>可靠性</div>') && !queue.includes('>分析</div>'), queue);
  check('队列保留低频详情入口', queue.includes('data-queue-details') && queue.includes('查看详情') && queue.includes('可靠性'), queue);
  check('队列详情改为行尾按钮 + 默认隐藏的整行面板', queue.includes('class="queue-detail-toggle"') && queue.includes('aria-expanded="false"') && queue.includes('data-queue-details hidden') && !queue.includes('<details class="queue-details"'), '');
  {
    const panel={hidden:true}, attrs={'aria-controls':'qd-TEST','aria-expanded':'false'};
    const button={getAttribute:k=>attrs[k], setAttribute:(k,v)=>{attrs[k]=v;}};
    const savedGet=document.getElementById; document.getElementById=id=>id==='qd-TEST'?panel:savedGet.call(document,id);
    if(D.toggleQueueDetails) D.toggleQueueDetails(button);
    const opened=panel.hidden===false && attrs['aria-expanded']==='true';
    if(D.toggleQueueDetails) D.toggleQueueDetails(button);
    document.getElementById=savedGet;
    check('查看详情按钮切换面板显隐和 aria-expanded', !!D.toggleQueueDetails && opened && panel.hidden===true && attrs['aria-expanded']==='false', '');
  }
  fetchPayload = {status:200,data:{cases:[{case_id:'CCCC3333',sex:'女',age:35,case_state:'awaiting_doctor',case_state_label:'待医生审核',pending_items:2,
    primary_department:'血液科',chief_complaint:'反复发热',risk_level:'高',updated_at:'2026-10-09T18:52:12'}],total:1,has_more:false,filters:{},sorts:{}}};
  await D.loadList();
  {
    const zh = el('list-body').innerHTML;
    check('后端中文风险等级“高”显示为红色高风险，不误用绿色', zh.includes('pill danger">高风险') && !zh.includes('pill ok">高'), zh);
    check('队列更新时间去掉 ISO 的 T 与秒', zh.includes('2026-10-09 18:52') && !zh.includes('T18:52'), '');
  }
  const railFilters = el('rail-filters').innerHTML;
  check('队列左轨只保留四个按下一步工作划分的入口', (railFilters.match(/class="filter-link /g)||[]).length === 4
    && ['待我处理','待复核','已发布','全部病例'].every(label=>railFilters.includes(label))
    && !railFilters.includes('<details') && !railFilters.includes('存在分歧'), railFilters);
  check('默认进入“待我处理”', D.S.queue.filter === 'doctor_pending'
    && /aria-pressed="true" onclick="setQueueFilter\('doctor_pending'\);"/.test(railFilters), '');
  D.S.queue.filter = 'published';
  D.S.queueMeta.filters = {all:2, published:1, doctor_pending:1, recheck:0};
  D.renderQueueFilters();
  check('切换到已发布后高亮并显示计数', /aria-pressed="true" onclick="setQueueFilter\('published'\);"/.test(el('rail-filters').innerHTML)
    && el('rail-filters').innerHTML.includes('class="filter-count">1<'), '');
  D.S.queue.filter = 'doctor_pending';
  D.renderQueueFilters();
  check('列表计数文案', el('list-note').textContent.includes('第 1 页'), el('list-note').textContent);
  const queueSummary = el('queue-tier-summary').innerHTML;
  check('队列审核分类统计拆分为独立数字格',
    (queueSummary.match(/class="queue-tier-stat"/g)||[]).length === 7
      && queueSummary.includes('必审') && queueSummary.includes('建议抽查')
      && queueSummary.includes('自动处理') && queueSummary.includes('分析失败'), queueSummary);

  D.S.caseData = { case_id:'CAAA1111', main_issue:'视物模糊', payload: {
    profile: { chief_complaint:'视物模糊3月', symptoms_timeline:[{symptom:'视物模糊', onset:'3个月'}], diseases:[], exams:[{item:'视力', value:'下降'}], medications:[] },
    triage: { primary:{department:'内分泌科', signals:['规则命中']}, secondary:[], selected:['内分泌科'], rule_hits:[], conflicts:[], decision_path:'规则优先' },
    evidence: { timeline:[{time:'2026-09-01', label:'视物模糊', kind:'症状'}], judgements:[{judgement:'需进一步评估', department:'内分泌科', evidence_state:'direct', support:['视力下降']} ] },
    mdt: { analyses:[{department:'内分泌科', facts:['血糖偏高'], inferences:[], checks:[]}], aggregation:{consensus:[{topic:'先完善检查', supporters:['内分泌科']}], disagreements:[], open_questions:['是否需要复查']}, challenge:{} },
    gaps:['缺少近期检查结果']
  }};
  D.S.review = { published:false, case_state_label:'待医生审核', summary:{pending:2}, mdt_screening:{status:'decided',decision:'recommend_mdt',decision_label:'建议 MDT',purpose:'明确共同处理重点',departments:['内分泌科','眼科']}, mdt_formal_record:null, mdt_preparation_packet:{
    specialty_opinions:[{department:'内分泌科',status:'available',focus:'评估代谢异常',facts:['血糖偏高','血糖偏高','视物模糊','体重变化','家族史','血压记录'],inferences:[{text:'建议结合糖化血红蛋白',evidence_refs:['检验单']}],checks:[{content:'是否需要补充复查'}],information_gaps:['缺少近期检查结果','缺少近期检查结果','缺少用药记录','缺少复查时间','缺少既往评估结果']}]
  } };
  D.renderOverview();
  const overview = el('ov-body').innerHTML;
  check('摘要页按阅读顺序继续资料包，不提前跳到待办',!overview.includes('当前待办') && overview.includes("go('packet')") && !overview.includes("go('mdt')"),'');
  check('摘要页保留主诉与诉求及原始病历入口',overview.includes('视物模糊3月') && overview.includes('患者本次诉求') && overview.includes('查看原始脱敏病历'),'');
  check('摘要恢复完整结构化病历',overview.includes('查看完整病历资料') && overview.includes('症状持续情况') && overview.includes('检查异常') && overview.includes('缺少近期检查结果'),'');
  D.renderTriage();
  const triage = el('tri-body').innerHTML;
  check('分流页只确认方向与科室，不重复展示推荐依据',triage.includes('内分泌科') && triage.includes('triage-routing-decision') && !triage.includes('triage-department-list'),triage);
  D.renderPacket();
  const packetHtml=el('packet-body').innerHTML;
  check('分流前资料包直接展示推荐科室与依据',packetHtml.includes('AI 推荐科室与判断依据') && packetHtml.includes('规则命中'),packetHtml);
  D.S.review.doctor_mdt_simulation={state:'done',result:D.S.caseData.payload.mdt};
  D.renderMdt();
  const mdt=el('mdt-body').innerHTML;
  check('MDT 表单后只保留返回，不重复滚回同一表单的继续按钮',!mdt.includes('继续 → 记录实际会诊结果') && mdt.includes('完成会诊并进入报告审核') && mdt.includes('返回分流确认'),'');
  check('MDT 范围、AI 模拟、正式记录按顺序排列',mdt.indexOf('已确认的会诊范围') < mdt.indexOf('AI 会诊模拟') && mdt.indexOf('AI 会诊模拟') < mdt.indexOf('正式 MDT 会诊记录') && !mdt.includes('data-mdt-department'),mdt);
  const mdtGroupingTokens=['mdt-specialty-card','mdt-specialty-inferences','mdt-specialty-checks','mdt-specialty-gaps','AI 参考判断','待医生判断','信息缺口','查看全部'];
  check('资料包专科参考内容按分析类型分组并可展开',mdtGroupingTokens.every(x=>packetHtml.includes(x)), '');
  check('资料包只呈现 AI 分析，不重复病例事实',!packetHtml.includes('血糖偏高') && !packetHtml.includes('现有病例事实与检查') && !packetHtml.includes('已掌握事实') && packetHtml.includes('建议结合糖化血红蛋白') && (packetHtml.match(/缺少近期检查结果/g)||[]).length===1, '');
  check('模拟分开展示独立研判、真实生成的质询和归纳', ['各科独立研判','质询与回应','模拟归纳与待确认事项'].every(x=>mdt.includes(x)) && !mdt.includes('不代表真实参会医生'),mdt);
  check('MDT 不重复展示患者画像和证据侧栏',
    !mdt.includes('左栏 · 患者画像') && !mdt.includes('右栏 · 证据与裁决'), mdt);
  check('MDT 不再使用播放过程交互',
    !doctorHtml.includes('播放过程') && !doctorHtml.includes('mdt-note'), '');
  D.S.caseData.payload.mdt.analyses[0].checks = [{content:'建议向医生确认是否需要复查'}];
  D.S.caseData.payload.mdt.aggregation.open_questions = ['建议向医生确认是否需要复查'];
  D.renderMdt();
  const doctorMdtCopy = el('mdt-body').innerHTML;
  check('医生端建议讨论问题使用决策语义',
    doctorMdtCopy.includes('建议讨论问题') && doctorMdtCopy.includes('是否需要复查')
      && !doctorMdtCopy.includes('向医生确认'), doctorMdtCopy);

  D.S.caseId = 'CAAA1111';
  D.S.review = {
    items: [], published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 10, pending: 4, human_touched: 6, doctor_reviewed: 6, auto_processed: 0,
               by_state: { awaiting: 4, confirmed: 4, modified: 1, rejected: 1, uncertain: 0 } },
  };
  D.renderReview();
  check('审核页移除顶部汇总面板',
    !doctorHtml.includes('id="pub-badge"')
      && !doctorHtml.includes('id="review-task-banner"')
      && !doctorHtml.includes('id="rev-summary-inline"'), '');

  /* 发布入口必须"两处都有"：≥1440px 右轨常驻，<1440px 右轨被 CSS 隐藏 → 屏 F 主区兜底
     （真实缺陷：入口只在右轨里，1366/1280 屏上医生审完发不出去） */
  D.S.review = {
    items: [{ item_id: 'IT-1', title: '待医生确认的医疗安排', target_type: 'path_step',
              ai_content: '建议内分泌科牵头', state: 'awaiting', state_label: '待审',
              changed_by_doctor: false, reason_hint: '规则命中', evidence_refs: ['S-1:3'] }],
    published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 1, pending: 1, human_touched: 0, doctor_reviewed: 0, auto_processed: 0, by_state: { awaiting: 1 } },
  };
  D.renderReview();
  const inlinePub = el('rev-publish-inline').innerHTML;
  check('主区发布面板包含患者可读的最终建议', inlinePub.includes('医生最终建议'), '');
  check('主区渲染发布面板',
    inlinePub.includes('预览患者报告') && !inlinePub.includes('批量接受可批量必审项并发布'), '');
  check('发布入口不因待办而静默禁用', !inlinePub.includes('disabled'), '');
  check('审核条目默认只显示摘要并可进入详情',
    el('rev-body').innerHTML.includes('查看本类')
    && doctorHtml.includes('id="rev-detail"')
    && doctorHtml.includes('openReviewDetail')
    && doctorHtml.includes('openReviewCategory'), el('rev-body').innerHTML);
  check('详情页移除低价值审核须知', !doctorHtml.includes('class="review-help"') && !doctorHtml.includes('审核须知'), '');
  check('唯一发布入口使用一致的输入前缀',
    inlinePub.includes('id="inline-op-text"')
    && !inlinePub.includes('id="op-text"'), '');
  check('最终建议通过主区发布入口一起提交',
    inlinePub.includes("previewPublication('inline')"), '');

  const doctorFetch=globalThis.fetch;
  const publications=[];
  globalThis.fetch=async(url,opts)=>{
    publications.push({url,data:JSON.parse(opts.body)});
    return {status:400,json:async()=>({error:'还有待审核项目'})};
  };
  el('inline-op-text').value='  ';
  await D.publish(false);
  check('医生未填写最终建议时阻止发布并提示填写',
    publications.length === 0 && el('inline-op-msg').textContent.includes('最终建议'), '');
  el('inline-op-text').value='给患者的复诊安排';
  D.S.publicationPreview={caseId:D.S.caseId,advice:'给患者的复诊安排',preview_revision:'fixture'};
  await D.publish(false);
  check('最终建议随发布请求提交并保留未通过的审核门禁提示',
    publications.length === 1 && publications[0].url.endsWith('/publish')
      && publications[0].data.final_advice === '给患者的复诊安排'
      && el('inline-op-msg').textContent.includes('待审核'), '');
  {
    const attack={reports:{patient_final_report:{header:{报告名称:'<b>x</b>',主诉:'<script>attack()</script>'},
      section_order:['医生审核后的专科评估意见','医生最终建议','后续安排'],
      sections:{医生审核后的专科评估意见:[{科室:'<i>眼科</i>',专科评估意见:['<img onerror=attack()>'],建议向医生确认:['<svg onload=x>'],审核:'医生已确认'}],
        医生最终建议:'<img onerror=attack()>',后续安排:['<a href=x>复诊</a>']}}}};
    const html=D.publicationPreviewHtml(attack,'inline');
    check('患者预览把报头、专科意见、建议和后续安排作为文本展示，转义 HTML',
      html.includes('&lt;script&gt;') && !/<(img|svg|script|i>|b>|a )/.test(html.replace(/<(?:article|section|header|footer|div|span|strong|small|p|ol|li|dl|dt|dd|h3|h4|button)\b[^>]*>/g,'')),'');
    const sample={reports:{patient_final_report:{case_id:'SYN-1',header:{报告名称:'多学科会诊意见报告',报告编号:'SYN-1-v2',病历编号:'SYN-1',性别:'女',年龄:'42 岁',主责科室:'眼科',会诊科室:['肾内科'],审核医生:'合成医生',报告日期:'发布时记录'},
      section_order:['医生审核后的专科评估意见','多学科会诊结论','医生最终建议','后续安排'],
      sections:{医生审核后的专科评估意见:[{科室:'眼科',专科评估意见:['意见甲','意见乙'],建议向医生确认:['是否需要检查丙'],审核:'医生已确认'},{科室:'肾内科',专科评估意见:'旧快照整段意见',来源:'AI 生成，医生已修改'}],
        多学科会诊结论:'会诊结论丁',医生最终建议:'复诊安排戊',后续安排:['会诊后续安排：两周复诊',{未解决:'旧分歧',后续安排:'待补资料'}]}}}};
    const doc=D.publicationPreviewHtml(sample,'inline');
    const order=['专科评估意见','多学科会诊结论','医生建议','后续安排'].map(t=>doc.indexOf(t));
    check('患者预览按会诊意见单单栏排序：专科意见 → 会诊结论 → 医生建议 → 后续安排',
      order.every(i=>i>0) && order.every((v,i)=>i===0||v>order[i-1]) && doc.includes('一、') && doc.includes('四、'),'');
    check('患者预览含报头、基本信息、预览印章和签发栏',
      ['多学科会诊意见报告','SYN-1-v2','女 · 42 岁','肾内科','预览 · 尚未发布','合成医生','fr-sign'].every(t=>doc.includes(t)),'');
    check('兼容旧快照：整段意见与对象式后续安排仍按文本显示',
      doc.includes('旧快照整段意见') && doc.includes('未解决：旧分歧；后续安排：待补资料'),'');
    check('历史版本渲染不显示预览印章',!D.finalReportDocumentHtml(sample.reports.patient_final_report).includes('预览 · 尚未发布'),'');
  }
  D.S.publicationPreview={caseId:D.S.caseId,advice:'旧建议',preview_revision:'old'};el('inline-publication-preview').innerHTML='旧预览';
  D.updateFinalAdvice('新建议');
  check('修改最终建议立即使旧预览失效',D.S.publicationPreview===null && el('inline-publication-preview').innerHTML==='','');
  publications.length=0;el('inline-op-text').value='新建议';
  await D.publish(false);
  check('未经当前预览不能直接从 UI 发布',publications.length===0 && el('inline-op-msg').textContent.includes('先预览'),'');
  let finishPreview;
  globalThis.fetch=()=>new Promise(resolve=>{finishPreview=resolve;});
  el('inline-op-text').value='请求时建议';D.S.review.publication_blockers=[];
  const pendingPreview=D.previewPublication();D.updateFinalAdvice('修改后建议');
  finishPreview({status:200,json:async()=>({ok:true,preview_revision:'old',reports:{}})});
  await pendingPreview;
  check('编辑建议后迟到的预览不能重新启用确认发布',D.S.publicationPreview===null,'');
  const oldPreviewCase=D.S.caseId;el('inline-op-text').value='修改后建议';
  const otherPreview=D.previewPublication();D.S.caseId='NEW-PREVIEW-CASE';
  finishPreview({status:200,json:async()=>({ok:true,preview_revision:'old',reports:{}})});
  await otherPreview;
  check('切换病例后旧患者预览不覆盖新病例',D.S.publicationPreview===null,'');
  D.S.caseId=oldPreviewCase;
  globalThis.fetch=doctorFetch;

  D.S.caseData = {case_id:'CAAA1111',payload:{triage:{primary:{department:'骨科'},selected:['骨科','眼科'],secondary:[{department:'眼科'}]}}};
  D.S.review = {triage_doctor_confirmation:{status:'confirmed',primary_department:'骨科',secondary_departments:['眼科'],selected:['骨科','眼科']},mdt_screening:{status:'decided',decision:'recommend_mdt',departments:['骨科','眼科']}};
  D.renderTriage();
  check('分流确认后提供返回资料包与继续 MDT 协作入口',
    el('tri-body').innerHTML.includes("go('packet')") && el('tri-body').innerHTML.includes("go('mdt')"), '');
  D.S.review = {};
  const beforeRouting = D.renderMdtScope();
  check('未确认分流时 MDT 判断引导回前置步骤而不提供错误顺序表单',
    beforeRouting.includes("go('triage')") && !beforeRouting.includes('id="mdt-screening-decision"'), '');
  D.S.review = {mdt_screening:{status:'decided',decision:'single_specialty'},mdt_preparation_packet:{handling:{disagreements:[{'分歧编号':'X-01','分歧主题':'复查安排'}]}}};
  D.renderMdt();
  check('单专科分支仍可记录 AI 分歧处理且有报告审核下一步',
    el('mdt-body').innerHTML.includes('submitDisagreement') && el('mdt-body').innerHTML.includes("go('review')"), '');
  D.S.review = {
    items:[{item_id:'IT-1',title:'安排',state:'confirmed',review_tier:'required'}],
    summary:{total:1,pending:0},case_state:'awaiting_doctor',published:false,
    publication_blockers:[{code:'triage',message:'请先确认主责和协同科室',screen:'triage'},
      {code:'mdt_screening',message:'请保存是否需要正式 MDT 的判断',screen:'mdt'}]};
  D.renderReview();
  const blockedPanel=el('rev-publish-inline').innerHTML;
  check('零待审时发布入口仍可点击以解释其他未完成事项',
    (blockedPanel.match(/<button[^>]+onclick="previewPublication[^>]*>/g)||[]).length===1 &&
    !(blockedPanel.match(/<button[^>]+onclick="previewPublication[^>]*>/g)||[]).some(tag=>tag.includes('disabled')), '');
  let blockedRequests=0;
  globalThis.fetch=async()=>{blockedRequests++;return {status:200,json:async()=>({})};};
  el('inline-op-text').value='';
  await D.publish(false);
  check('点击发布一次列出分流、MDT 判断、最终建议并给出跳转入口',
    blockedRequests===0 && el('inline-publish-blockers').innerHTML.includes("goDoctorTask('triage'") &&
    el('inline-publish-blockers').innerHTML.includes("goDoctorTask('mdt'") &&
    el('inline-publish-blockers').innerHTML.includes('最终建议'), '');
  globalThis.fetch=doctorFetch;

  D.S.review = {
    items: [
      { item_id: 'ai_analysis:血液科', target_type: 'ai_analysis', title: 'AI 专科意见：血液科', ai_content: '血液科意见',
        state: 'awaiting', state_label: '待审', review_tier: 'required', department: '血液科' },
      { item_id: 'ai_analysis:眼科', target_type: 'ai_analysis', title: 'AI 专科意见：眼科', ai_content: '眼科意见',
        state: 'confirmed', state_label: '已接受', review_tier: 'required', department: '眼科', decision_id: 'D1' }],
    published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 2, pending: 1, doctor_reviewed: 1, auto_processed: 0, by_state: { awaiting: 1, confirmed: 1 } },
  };
  D.renderReview();
  {
    const body = el('rev-body').innerHTML;
    check('专科意见待确认数用醒目计数块显示（数量 + 已确认进度）',
      body.includes('specialty-review-counter is-pending') && body.includes('<strong>1</strong>') && body.includes('已确认 1 科'), '');
    check('报告审核页不再显示“返回 / 继续 → 填写最终建议”导航',
      !el('rev-publish-inline').innerHTML.includes('继续 → 填写最终建议') && !el('rev-publish-inline').innerHTML.includes('doctor-workflow-nav'), '');
  }
  {
    // 发布门槛上下文已审完、专科意见还没审完：“去逐条审核”应回到专科意见，而不是打开必审上下文清单。
    D.S.review.items.push({ item_id: 'path_step:S2', target_type: 'path_step', title: '路径', ai_content: '安排',
      state: 'confirmed', state_label: '已接受', review_tier: 'required', decision_id: 'D2' });
    D.renderReview();
    el('rev-category').innerHTML=''; el('rev-category').classList.add('hide');
    D.goPendingReviewItem();
    check('专科意见未审完时“去逐条审核”回到专科意见卡，不打开发布门槛清单',
      el('rev-category').classList.contains('hide') && !el('rev-category').innerHTML.includes('发布门槛：必审上下文'), el('rev-category').innerHTML.slice(0,80));
    D.S.review.items[0].state='confirmed'; D.S.review.items[0].decision_id='D3';
    D.S.review.items[2].state='awaiting'; delete D.S.review.items[2].decision_id;
    D.renderReview();
    D.goPendingReviewItem();
    check('专科意见都确认后“去逐条审核”打开发布门槛必审清单',
      el('rev-category').innerHTML.includes('发布门槛：必审上下文'), '');
    D.openReviewCategory && el('rev-category').classList.add('hide');
  }

  D.S.review = {
    items: [
      { item_id: 'consensus_item:X-01', target_type: 'consensus_item', title: '分歧待裁决：先查眼底 vs 先查肾功',
        ai_content: '眼科：先查眼底', state: 'handled_in_mdt', state_label: '已在 MDT 处理', review_tier: 'required',
        handled_in_mdt: true, handled_note: '医生已在 MDT 页记录：已解决——先查眼底', conflict: true },
      { item_id: 'consensus_item:X-02', target_type: 'consensus_item', title: '分歧待裁决：是否住院',
        ai_content: '肾内科：建议住院', state: 'awaiting', state_label: '待审', review_tier: 'required', conflict: true }],
    published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 2, pending: 1, doctor_reviewed: 0, auto_processed: 0, by_state: { awaiting: 1, handled_in_mdt: 1 } },
  };
  D.renderReview();
  D.openReviewCategory('processed');
  {
    const processed = el('rev-category').innerHTML;
    check('已在 MDT 记录的分歧归入已完成，显示处理结果且不再提供接受/修改',
      processed.includes('已在 MDT 处理') && processed.includes('先查眼底') && !processed.includes("decide('consensus_item:X-01'"), '');
  }
  D.openReviewCategory('required');
  {
    const required = el('rev-category').innerHTML;
    check('未记录的分歧只引导去 MDT 页处理，不在审核卡上重复裁决',
      required.includes("goDoctorTask('mdt','disagreements')") && !required.includes("decide('consensus_item:X-02'"), '');
  }

  D.S.review = {
    items: [{ item_id: 'AUTO-1', title: '路径步骤：常规准备', target_type: 'path_step',
              ai_content: '准备资料', state: 'auto_processed', state_label: '免审（自动）',
              needs_doctor_review: false, auto_reason: '规则判定免审' }],
    published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 1, pending: 0, human_touched: 0, doctor_reviewed: 0, auto_processed: 1,
               by_state: { auto_processed: 1 } },
  };
  D.renderReview();
  check('审核分类卡片显示自动处理入口', el('rev-body').innerHTML.includes('已自动处理') && el('rev-body').innerHTML.includes("openReviewCategory('auto')"), '');
  D.openReviewCategory('auto');
  check('自动处理分类进入独立清单视图', el('rev-category').innerHTML.includes('接管并确认') && el('rev-category').innerHTML.includes('返回审核分类'), el('rev-category').innerHTML);

  D.S.audit = { report_version:2, review:{published:true, published_by:'总审医生', published_at:'2026-09-30'},
    versions:[{version:2, published_by:'总审医生', published_at:'2026-09-30', review_summary:{disagreements:0}}],
    decisions:[{action_label:'接受', target_label:'科室入选', target_id:'IT-1', content_revision:2, reviewer:'总审医生', created_at:'2026-09-30', before:'AI 原稿', after:'医生版本', reason:'资料充分'}],
    timeline:[{at:'2026-09-30', event:'report_published', detail:'审核人=总审医生 版本=2'}], items:[{title:'科室入选', state:'confirmed', state_label:'已接受'}] };
  D.renderHistory();
  const history = el('hist-body').innerHTML;
  check('历史记录提供版本与医生处理入口', history.includes('查看报告') && history.includes('医生处理记录') && history.includes('查看处理详情') && history.includes('关键流程记录') && history.includes('报告发布') && !history.includes('查看完整审计日志') && !history.includes('条目状态快照'), '');
  check('关键流程记录准确提取处理人', history.includes('history-event-actor">总审医生</div>') && !history.includes('history-event-actor">总审医生 版本=2</div>'), history);
  window.openHistoryDecision(0);
  const decisionDetail = el('hist-body').innerHTML;
  check('历史操作详情展示真实修改内容和理由', decisionDetail.includes('AI 原稿') && decisionDetail.includes('医生版本') && decisionDetail.includes('资料充分') && !decisionDetail.includes('系统事件'), '');
  el('doctor-case-main').scrollTop = 480;
  window.openHistoryVersion(2);
  check('查看历史版本前将病例主区滚动到顶部', el('doctor-case-main').scrollTop === 0, String(el('doctor-case-main').scrollTop));
  check('缺少历史正文时明确显示空状态', el('hist-body').innerHTML.includes('该版本暂无报告正文'), '');

  const initialFetch=globalThis.fetch;
  const queued=[];
  globalThis.fetch=(url)=>new Promise(resolve=>queued.push({url,resolve}));
  const requestA=D.openCase('C-A'), requestB=D.openCase('C-B');
  await D.publish(false);
  check('病例加载期间不能用旧页面内容发布新病例',queued.length===6 && el('inline-op-msg').textContent.includes('加载'),'');
  const response=id=>({status:200,json:async()=>({case:{case_id:id,payload:{}},case_id:id,items:[],summary:{total:0,pending:0}})});
  queued.filter(req=>req.url.includes('C-B')).forEach(req=>req.resolve(response('C-B')));
  await requestB;
  queued.filter(req=>req.url.includes('C-A')).forEach(req=>req.resolve(response('C-A')));
  await requestA;
  check('较早病例请求晚返回时不覆盖新病例的报告与建议归属',
    D.S.caseId==='C-B' && D.S.caseData.case_id==='C-B' && D.S.review.case_id==='C-B', '');
  globalThis.fetch=async()=>({status:200,json:async()=>({ok:true,case:{case_id:'C-B',payload:{}},items:[],summary:{total:0,pending:0},mdt_screening:{status:'decided',decision:'single_specialty'}})});
  D.S.screen='mdt';
  await D.submitDisagreement('X-01','X01');
  check('保存分歧后留在 MDT 页面继续处理', D.S.screen==='mdt', '');
  const originalMatch=window.matchMedia,originalScroll=window.scrollTo;
  let windowScrolled=false;
  window.matchMedia=()=>({matches:true}); window.scrollTo=()=>{windowScrolled=true;};
  D.S.screen='triage';D.go('mdt');
  check('小屏跳转新步骤时重置实际窗口滚动位置',windowScrolled,'');
  windowScrolled=false;
  window.matchMedia=()=>({matches:false});
  el('doctor-case-main').scrollTop=320;
  D.S.screen='overview';D.go('packet');
  check('桌面端从病例摘要进入 AI 资料包时回到页面顶部（窗口与主区都复位）',
    windowScrolled && el('doctor-case-main').scrollTop===0,'');
  window.matchMedia=originalMatch;window.scrollTo=originalScroll;globalThis.fetch=initialFetch;

  /* MDT 保存进度与完成操作应有持久反馈，并同步服务器的完成门槛。 */
  D.S.caseId='C-MDT-SAVE'; D.S.caseRole='primary'; D.S.caseLoading=false;
  const mdtCase={case_id:D.S.caseId,payload:{profile:{},triage:{primary:{department:'内分泌科'}}}};
  const mdtReview={items:[{item_id:'path_step:S1',target_type:'path_step',state:'awaiting',review_tier:'required',content:'复诊安排'}],summary:{pending:1},mdt_screening:{status:'decided',decision:'recommend_mdt',purpose:'共同评估',departments:['内分泌科','眼科']},triage_doctor_confirmation:{status:'confirmed',selected:['内分泌科','眼科']}};
  let savedMdt=null;
  const mdtPosts=[];
  globalThis.fetch=async(url,options={})=>{
    if(url.endsWith('/mdt-formal-record')){
      const body=JSON.parse(options.body);mdtPosts.push(body);
      savedMdt={...body,status_label:body.status==='completed'?'已完成':'待开始'};
      return {status:200,json:async()=>({ok:true,mdt_formal_record:savedMdt})};
    }
    const data=url.endsWith('/review')?{...mdtReview,mdt_formal_record:savedMdt,publication_blockers:savedMdt?.status==='completed'?[]:[{code:'mdt_record',message:'正式 MDT 尚未完成',screen:'mdt'}]}:url.endsWith('/audit')?{}:{case:mdtCase,case_role:'primary'};
    return {status:200,json:async()=>data};
  };
  D.S.caseData=mdtCase;D.S.review=mdtReview;D.S.screen='mdt';
  el('mdt-formal-status').value='planned';el('mdt-formal-conclusion').value='';
  await D.submitMdtFormalRecord();
  check('保存待开始进度后保留状态说明，并明确仍需完成会诊',D.S.screen==='mdt' && el('mdt-body').innerHTML.includes('会诊进度已保存') && D.S.review.publication_blockers.some(b=>b.code==='mdt_record'),'');
  el('mdt-formal-conclusion').value='已实际共同讨论并确认安排';
  await D.submitMdtFormalRecord('completed');
  check('明确完成 MDT 后进入报告审核且刷新后不再显示未完成门槛',mdtPosts.at(-1).status==='completed' && D.S.screen==='review' && !D.S.review.publication_blockers.some(b=>b.code==='mdt_record') && el('rev-body').innerHTML.includes('正式 MDT 已完成并保存'),'');
  const beforeInvalid=mdtPosts.length;
  el('mdt-formal-conclusion').value='';
  await D.submitMdtFormalRecord('completed');
  check('完成会诊前结论为空时在表单提示，保留内容并不发出完成请求',mdtPosts.length===beforeInvalid && el('mdt-formal-msg').textContent.includes('结论'),'');
  el('mdt-formal-conclusion').value='保留的会诊结论';
  globalThis.fetch=async()=>{throw new Error('网络中断');};
  await D.submitMdtFormalRecord('completed');
  check('会诊保存网络失败有可见错误且不丢失输入',el('mdt-formal-msg').textContent.includes('保存失败') && el('mdt-formal-conclusion').value==='保留的会诊结论' && !el('mdt-formal-complete').disabled,'');
  globalThis.fetch=async(url,options={})=>url.endsWith('/mdt-formal-record')?{status:200,json:async()=>({ok:true,mdt_formal_record:{status:'completed',conclusion:'已保存的结论'}})}:{status:500,json:async()=>({error:'读取审核状态失败'})};
  await D.submitMdtFormalRecord('completed');
  check('保存成功但刷新失败时明确区分并提供重试入口',el('mdt-body').innerHTML.includes('会诊记录已保存，但审核状态刷新失败') && el('mdt-body').innerHTML.includes('重新加载审核状态') && D.S.review.mdt_formal_record.status==='completed','');
  globalThis.fetch=initialFetch;
  check('摘要下一步先进入 AI 资料包',D.doctorWorkflowNav('overview').includes("go('packet')"),'');
  check('独立 AI 资料包步骤位于摘要和分流之间',doctorHtml.indexOf('data-s="packet"')>doctorHtml.indexOf('data-s="overview"') && doctorHtml.indexOf('data-s="packet"')<doctorHtml.indexOf('data-s="triage"'),'');
  {
    D.S.caseId='C-SAVED';D.S.caseData={case_id:'C-SAVED',payload:{triage:{selected:['血液科','肿瘤科']}}};D.S.caseRole='primary';D.S.editTriage=false;
    D.S.review={items:[],summary:{},triage_doctor_confirmation:{status:'confirmed',primary_department:'血液科',secondary_departments:['肿瘤科'],reason:'合成理由',confirmed_at:'2026-10-10T19:29:28'},
      mdt_screening:{status:'decided',decision:'recommend_mdt',decision_label:'建议 MDT',departments:['血液科','肿瘤科'],purpose:'合成目标'}};
    D.renderTriage();
    const saved=el('tri-body').innerHTML;
    check('已确认分流按字段表展示：会诊目标、确认理由各占一行，确认时间单独放在底部且去掉 T 与秒',
      saved.includes('<dt>会诊目标</dt>') && saved.includes('<dt>确认理由</dt>') && saved.includes('确认时间　2026-10-10 19:29')
        && !saved.includes('确认理由：合成理由 ·') && saved.includes('triage-dept-chip is-primary'), '');
  }
  D.S.caseId='C-ROUTING';D.S.caseData={case_id:'C-ROUTING',payload:{triage:{selected:['内分泌科','眼科']}}};D.S.caseRole='primary';D.S.review={items:[],summary:{},triage_doctor_confirmation:null,mdt_screening:null};
  D.S.caseLoading=false;
  const routingQuery=document.querySelectorAll;
  document.querySelectorAll=selector=>selector==='[data-triage-secondary]:checked'?[{value:'眼科'}]:routingQuery(selector);
  el('triage-primary-department').value='内分泌科';el('triage-confirmation-reason').value='共同判断';el('triage-routing-decision').value='recommend_mdt';el('triage-routing-purpose').value='确认共同安排';
  let unifiedBody;
  const confirmedRouting={triage_doctor_confirmation:{status:'confirmed',primary_department:'内分泌科',selected:['内分泌科','眼科']},mdt_screening:{status:'decided',decision:'recommend_mdt',purpose:'确认共同安排',departments:['内分泌科','眼科']},items:[],summary:{}};
  globalThis.fetch=async(url,options={})=>{if(url.endsWith('/doctor-routing')){unifiedBody=JSON.parse(options.body);return {status:200,json:async()=>({ok:true})};}return {status:200,json:async()=>url.endsWith('/review')?confirmedRouting:url.endsWith('/audit')?{}:{case:D.S.caseData,case_role:'primary'}};};
  await D.submitTriageConfirmation();
  check('分流一次提交科室、MDT 方向和目标，保存后进入协作',unifiedBody?.decision==='recommend_mdt' && unifiedBody?.purpose==='确认共同安排' && unifiedBody?.secondary_departments.join('、')==='眼科' && D.S.screen==='mdt','');
  document.querySelectorAll=routingQuery;
  if(D.startDoctorMdtSimulation){
    el('mdt-formal-conclusion').value='正在填写的真实医生结论';
    globalThis.fetch=async()=>{throw new Error('测试网络失败');};
    await D.startDoctorMdtSimulation();
    check('模拟网络失败有提示，并保留真实记录输入',el('mdt-simulation-msg').textContent.includes('失败') && el('mdt-formal-conclusion').value==='正在填写的真实医生结论','');
    const completedSimulation={state:'done',result:{analyses:[{department:'眼科',status:'ok'}],aggregation:{}}};
    const completedReview={...confirmedRouting,doctor_mdt_simulation:completedSimulation,mdt_disagreements:[]};
    globalThis.fetch=async url=>({status:200,json:async()=>url.endsWith('/review')?completedReview:{ok:true,simulation:completedSimulation}});
    await D.refreshDoctorMdtSimulation();
    check('模拟完成刷新审核状态时保留正在填写的正式结论',D.S.review.doctor_mdt_simulation.state==='done' && el('mdt-formal-conclusion').value==='正在填写的真实医生结论' && el('mdt-simulation-body').innerHTML.includes('已保存'),'');
    globalThis.fetch=async url=>({status:url.endsWith('/review')?500:200,json:async()=>url.endsWith('/review')?{error:'测试刷新失败'}:{ok:true,simulation:completedSimulation}});
    await D.refreshDoctorMdtSimulation();
    check('模拟完成但审核加载失败提供就地重试并保留输入',el('mdt-simulation-msg').innerHTML.includes('重新加载模拟与审核状态') && el('mdt-formal-conclusion').value==='正在填写的真实医生结论','');
    let lateSimulation;
    globalThis.fetch=()=>new Promise(resolve=>{lateSimulation=resolve;});
    const oldPoll=D.refreshDoctorMdtSimulation();
    const newCaseId='C-NEW-SIMULATION';
    D.S.caseId=newCaseId;D.S.review={doctor_mdt_simulation:{state:'none'}};
    el('mdt-simulation-body').innerHTML='新病例模拟区';
    lateSimulation({status:200,json:async()=>({ok:true,simulation:completedSimulation})});
    await oldPoll;
    check('旧病例模拟请求晚返回不会覆盖新病例状态与模拟区',D.S.caseId===newCaseId && D.S.review.doctor_mdt_simulation.state==='none' && el('mdt-simulation-body').innerHTML==='新病例模拟区','');
    D.S.review={items:[{item_id:'consensus_item:C-01',content_revision:2}]};
    D.openModal('modify','consensus_item:C-01');
    D.S.review.items[0].content_revision=3;
    el('m-after').value='针对旧内容的修改';el('m-reason').value='旧依据';
    let reviewSubmission;
    globalThis.fetch=async(url,options={})=>{reviewSubmission=JSON.parse(options.body);return {status:400,json:async()=>({ok:false,error:'审核内容已变化，请重新核对'})};};
    await D.submitModal();
    check('修改弹窗提交绑定打开时所见修订，过期失败保留输入',reviewSubmission.expected_content_revision===2 && el('m-after').value==='针对旧内容的修改' && D.S.modal?.contentRevision===2,'');
    el('dis-revision-X01').value='4';
    await D.submitDisagreement('X-01','X01');
    check('分歧处理提交绑定表单所见修订，过期失败给出提示',reviewSubmission.expected_content_revision===4 && el('dis-msg-X01').textContent.includes('已变化'),'');
    D.S.screen='mdt';D.S.caseId='C-REENTER';D.S.caseData={case_id:'C-REENTER',payload:{}};
    D.S.review={...confirmedRouting,doctor_mdt_simulation:{state:'none'}};
    const reads=[];
    globalThis.fetch=async url=>{reads.push(url);return {status:200,json:async()=>url.endsWith('/review')?completedReview:{ok:true,simulation:{...completedSimulation,finished_at:'2026-10-08T12:00:00'}}};};
    D.renderMdt();
    await new Promise(resolve=>setTimeout(resolve,0));
    check('重新进入 MDT 时读取已保存结果，不依赖页面缓存的未生成状态',reads.some(url=>url.endsWith('/doctor-mdt-simulation')) && D.S.review.doctor_mdt_simulation.state==='done' && el('mdt-simulation-body').innerHTML.includes('已保存'),'');
    D.S.review={...confirmedRouting,doctor_mdt_simulation:{state:'none'}};
    globalThis.fetch=async()=>({status:500,json:async()=>({error:'测试读取失败'})});
    await D.refreshDoctorMdtSimulation();
    check('读取失败不会误显示未生成，并提供读取重试',!el('mdt-simulation-body').innerHTML.includes('生成本次 AI 会诊模拟') && el('mdt-simulation-msg').textContent.includes('尚不能确认') && el('mdt-simulation-msg').innerHTML.includes('重新读取已保存模拟'),'');
  }else check('模拟网络失败有提示，并保留真实记录输入',false,'缺少医生确认后生成模拟入口');
  globalThis.fetch=initialFetch;
}

/* ------------------------------------------------------------------ 输出 */
let failed = 0;
for (const [ok, name, detail] of results) {
  if (!ok) failed++;
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' — ' + detail : ''}`);
}
console.log(`\n===== 前端渲染逻辑：${results.length - failed} 通过 / ${failed} 失败 =====`);
process.exit(failed ? 1 : 0);
