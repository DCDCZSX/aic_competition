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
    id, innerHTML: '', textContent: '', className: '', value: '', dataset: {},
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
  eval(inlineScript('patient.html') + '\n;globalThis.__P = { S, renderSteps, renderContext, syncStagePanels, STEPS, renderSectionValue, mdtProgressHtml, analysisBanner, sectionDigest, toggleAllReport, openAllForPrint, restoreAfterPrint, switchReport, renderTriage, renderPath, renderMdt, doPreview };');
  const { S, renderSteps, renderContext, STEPS, renderSectionValue, mdtProgressHtml, analysisBanner,
          syncStagePanels, sectionDigest, toggleAllReport, openAllForPrint, restoreAfterPrint, switchReport, renderTriage,
          renderPath, renderMdt,
          doPreview } = globalThis.__P;

  check('患者端主流程为 4 个阶段', STEPS.length === 4, String(STEPS.length));

  renderSteps('stage1');
  const h1 = el('steps').innerHTML;
  const count = (h1.match(/class="step[ "]/g) || []).length;
  check('左轨渲染出 4 个阶段项', count === 4, String(count));
  check('左轨含阶段名与两位序号', h1.includes('填写情况') && h1.includes('04'), '');
  check('当前阶段高亮', h1.includes('class="step active"'), '');
  check('第 1 阶段进度 0%', el('steps-bar').style.width === '0%', el('steps-bar').style.width);
  check('第 1 阶段说明为「第 1 / 4 步」', el('steps-note').textContent.includes('第 1 / 4 步'),
    el('steps-note').textContent);

  renderSteps('stage3');
  const h3 = el('steps').innerHTML;
  const done = (h3.match(/class="step done"/g) || []).length;
  check('已走过的 2 个阶段标记为 done', done === 2, String(done));
  check('已走过的阶段可点击回跳', h3.includes('onclick="go(' + "'" + 'stage1' + "'" + ')"'), '');
  check('第 3 阶段进度 67%', el('steps-bar').style.width === '67%', el('steps-bar').style.width);

  syncStagePanels('stage2');
  check('回看核对资料阶段直接显示画像和追问',
    !el('s-profile').classList.contains('hide') && !el('s-ask').classList.contains('hide'), '');
  syncStagePanels('stage3');
  check('回看查看建议阶段显示三个建议面板',
    !el('s-triage').classList.contains('hide') && !el('s-path').classList.contains('hide')
      && !el('s-mdt').classList.contains('hide'), '');

  // 就诊后更新 / 变化摘要不在首次准备的四个阶段内 → 主流程显示为已完成
  renderSteps('update');
  const hu = el('steps').innerHTML;
  check('附加环节：四个阶段全部完成', (hu.match(/class="step done"/g) || []).length === 4, '');
  check('附加环节：进度条 100%', el('steps-bar').style.width === '100%', el('steps-bar').style.width);
  check('附加环节：说明不写「尚未开始」',
    !el('steps-note').textContent.includes('尚未开始') && el('steps-note').textContent.includes('附加环节'),
    el('steps-note').textContent);

  // 患者端保留顶栏病例号，不再渲染常驻病例上下文栏
  S.caseId = 'C1234ABCD';
  renderContext();
  check('顶栏病例号同步', el('ctx-case').textContent.includes('C1234ABCD'), el('ctx-case').textContent);

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

  /* 完整报告的逐段折叠：默认收起、目录点击自动展开、打印前自动全展开
     （13 段全展开太长；折叠后不能让打印或无障碍丢内容） */
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

  /* 完整报告 HTML：默认收起 + 目录与段落 id 对应 + 内容仍在 DOM 里 */
  S.report = { mdt_screening: { decision: 'recommend_mdt', departments: ['内分泌科', '眼科'], patient_next_step: '医生建议多学科共同评估，请按后续就医安排准备资料。' }, summary: { stage_label: 'AI 就诊准备摘要', report_version: 1, review_status_label: 'AI 生成，尚未经医生审核', doctor_confirmed: false, case_id: 'C1234ABCD', generated_at: '2026-09-24', sections: { '主要问题': '视物模糊', '建议咨询方向': '眼科', '当前任务': ['资料准备｜带上近期检查结果'], '需要向医生确认的问题': ['是否需要完善眼底检查'] } }, full: { sections: { '病例概况': '一句话概况', '检查与证据': ['条目一', '条目二'], '就医路径': [{ '步骤': 'S1', '类型': '资料准备', '内容': '带上近期检查结果' }] },
                       section_order: ['病例概况', '检查与证据'], complete: true } };
  S.reportOpen = {};
  switchReport('full');
  const fullHtml = el('report-body').innerHTML;
  switchReport('summary');
  const summaryHtml = el('report-body').innerHTML;
  check('摘要默认显示 AI 就诊准备阶段', summaryHtml.includes('AI 就诊准备摘要') && summaryHtml.includes('版本 1'), '');
  check('摘要不把 AI 阶段写成医生审核版', !summaryHtml.includes('已发布医生审核版'), '');
  check('打印版准备单保留行动信息', summaryHtml.includes('class="print-report"')
    && summaryHtml.includes('患者就医准备单') && summaryHtml.includes('就医步骤')
    && summaryHtml.includes('当前待办') && summaryHtml.includes('就诊时可问医生'), '');
  check('患者看到医生初筛的下一步', summaryHtml.includes('医生建议多学科共同评估')
    && summaryHtml.includes('相关科室：内分泌科、眼科'), summaryHtml);

  check('完整报告渲染成可折叠段落', (fullHtml.match(/<details class="sec"/g) || []).length === 2,
    String((fullHtml.match(/<details class="sec"/g) || []).length));
  check('默认全部收起（没有 open 属性）', !fullHtml.includes(' open>'), '');
  check('每段都有独立的正文容器', (fullHtml.match(/class="sec-body"/g) || []).length === 2, '');
  check('收起状态也保留正文（打印/无障碍不丢内容）', fullHtml.includes('一句话概况')
    && fullHtml.includes('条目一'), '');
  check('目录链接与段落 id 一一对应',
    fullHtml.includes('data-sec="sec-0"') && fullHtml.includes('id="sec-0"')
    && fullHtml.includes('data-sec="sec-1"') && fullHtml.includes('id="sec-1"'), '');
  check('折叠段带内容摘要提示', (fullHtml.match(/class="sec-hint"/g) || []).length === 2, '');
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
  check('分诊页保留"牵头方向 ≠ 确诊"的免责说明', tri.includes('牵头方向 ≠ 最终诊断科室'), '');

  /* 路径与任务合并：每个路径步骤下直接挂对应任务，不再渲染独立任务栏 */
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
  check('路径页面将任务挂在对应步骤下',
    pathHtml.includes('路径中的任务') && pathHtml.includes('整理近 3 个月血糖记录')
      && pathHtml.includes('向医生确认是否需要复查'), pathHtml);
  check('路径页面不再单独渲染任务栏', !pathHtml.includes('我的任务（'), pathHtml);

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
  check('横幅显示 MDT 子步骤与第几科', bannerRun.includes('眼科') && bannerRun.includes('第 2 科')
    && bannerRun.includes('正在整理眼科的参考意见'), '');
  check('横幅不泄漏内部阶段术语', !bannerRun.includes('模型发言')
    && !bannerRun.includes('独立意见'), '');
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

  /* API 失败时必须把服务端原因告诉患者，不能统一吞成“预览失败” */
  el('history').value = '2型糖尿病5年，视物模糊3个月。';
  el('main-issue').value = '该看哪个科';
  fetchPayload = { status: 401, data: { error: '未登录或会话已过期，请先登录' } };
  await doPreview();
  check('预览失败显示服务端原因', el('intake-msg').innerHTML.includes('会话已过期'), el('intake-msg').innerHTML);
}

/* ----------------------------------------------------- 医生工作台断言 */
{
  eval(inlineScript('doctor.html') + '\n;globalThis.__D = { S, loadList, renderReview, renderMdtScreening, setQueueFilter, goQueuePage };');
  const D = globalThis.__D;

  D.S.caseData = { payload: { triage: { selected: ['内分泌科', '眼科'] } } };
  D.S.review = { mdt_screening: null };
  const screeningPanel = D.renderMdtScreening();
  check('医生端显示 MDT 初筛表单', screeningPanel.includes('MDT 初筛与病例分流')
    && screeningPanel.includes('recommend_mdt') && screeningPanel.includes('保存初筛决定'), screeningPanel);

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
  check('队列显示风险与待医生处理量', queue.includes('高风险') && queue.includes('必审 3'), '');
  check('队列显示自动处理条数', queue.includes('免审 2'), '');
  check('病例队列使用服务端分页元数据', el('list-pager').innerHTML.includes('共 2 例'), '');
  check('左轨改为筛选入口而非病例堆叠', !el('rail-filters').innerHTML.includes('CAAA1111'), '');
  check('列表计数文案', el('list-note').textContent.includes('第 1 页'), el('list-note').textContent);

  D.S.caseId = 'CAAA1111';
  D.S.review = {
    items: [], published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 10, pending: 4, human_touched: 6, doctor_reviewed: 6, auto_processed: 0,
               by_state: { awaiting: 4, confirmed: 4, modified: 1, rejected: 1, uncertain: 0 } },
  };
  D.renderReview();
  check('审核进度条按医生处理与自动处理合计', el('rev-bar').style.width === '60%', el('rev-bar').style.width);
  check('审核进度说明区分医生与自动处理', el('rev-note').textContent.includes('医生处理 6') && el('rev-note').textContent.includes('免审自动 0'), el('rev-note').textContent);
  check('右轨审核面板有内容', el('rev-summary').innerHTML.includes('共 10 条'), '');

  /* 发布入口必须"两处都有"：≥1440px 右轨常驻，<1440px 右轨被 CSS 隐藏 → 屏 F 主区兜底
     （真实缺陷：入口只在右轨里，1366/1280 屏上医生审完发不出去） */
  D.S.review = {
    items: [{ item_id: 'IT-1', title: '科室入选：内分泌科', target_type: 'department_selection',
              ai_content: '建议内分泌科牵头', state: 'awaiting', state_label: '待审',
              changed_by_doctor: false, reason_hint: '规则命中', evidence_refs: ['S-1:3'] }],
    published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 1, pending: 1, human_touched: 0, doctor_reviewed: 0, auto_processed: 0, by_state: { awaiting: 1 } },
  };
  D.renderReview();
  const railPub = el('rev-publish').innerHTML;
  const inlinePub = el('rev-publish-inline').innerHTML;
  check('右轨渲染发布面板', railPub.includes('确认发布审核版') && railPub.includes('录入线下实际意见'), '');
  check('主区镜像渲染同一份发布面板（窄屏右轨被隐藏时仍可发布）',
    inlinePub.includes('确认发布审核版') && inlinePub.includes('处理剩余必审并发布'), '');
  check('仍有待审条目时发布按钮禁用', railPub.includes('disabled') && inlinePub.includes('disabled'), '');
  check('两处入口 id 不重复（用前缀区分）',
    railPub.includes('id="rail-op-text"') && inlinePub.includes('id="inline-op-text"')
    && !inlinePub.includes('id="op-text"'), '');
  check('镜像里的录入按钮带自己的前缀（不会写进另一处输入框）',
    inlinePub.includes("submitOpinion('inline')") && railPub.includes("submitOpinion('rail')"), '');

  D.S.review = {
    items: [{ item_id: 'AUTO-1', title: '路径步骤：常规准备', target_type: 'path_step',
              ai_content: '准备资料', state: 'auto_processed', state_label: '免审（自动）',
              needs_doctor_review: false, auto_reason: '规则判定免审' }],
    published: false, case_state: 'awaiting_doctor', review: {}, doctor_opinions: [],
    summary: { total: 1, pending: 0, human_touched: 0, doctor_reviewed: 0, auto_processed: 1,
               by_state: { auto_processed: 1 } },
  };
  D.renderReview();
  check('详情页单独折叠免审自动分组', el('rev-body').innerHTML.includes('免审（自动）') && el('rev-body').innerHTML.includes('auto_processed'), '');
  check('免审条目提供接管入口而非强制医生逐条确认', el('rev-body').innerHTML.includes('接管并确认'), '');
}

/* ------------------------------------------------------------------ 输出 */
let failed = 0;
for (const [ok, name, detail] of results) {
  if (!ok) failed++;
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ' — ' + detail : ''}`);
}
console.log(`\n===== 前端渲染逻辑：${results.length - failed} 通过 / ${failed} 失败 =====`);
process.exit(failed ? 1 : 0);
