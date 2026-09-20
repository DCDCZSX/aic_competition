/**
 * 报告渲染校验（需要服务在跑）：取真实病例的报告 JSON，用患者端的前端渲染器渲染一遍，
 * 断言 13 段里既没有 [object Object]，也没有内部英文键泄漏 —— 即"用户在报告页上真正看到的东西"。
 *
 * 运行：python app.py（另开一个终端）→ node verify_report_render.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.APP_BASE_URL || 'http://127.0.0.1:5000';
const PATIENT_USERNAME = process.env.APP_PATIENT_USERNAME || 'patient';
const PATIENT_PASSWORD = process.env.APP_PATIENT_PASSWORD;
if (!PATIENT_PASSWORD) {
  throw new Error('未配置 APP_PATIENT_PASSWORD，无法运行报告渲染校验');
}
const HERE = path.dirname(fileURLToPath(import.meta.url));
const js = fs.readFileSync(path.join(HERE, 'templates', 'patient.html'), 'utf8');
const inline = [...js.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n;\n');

const els = new Map();
const el = id => {
  if (!els.has(id)) els.set(id, { innerHTML: '', textContent: '', style: { setProperty() { }, width: '' },
    classList: { add() { }, remove() { }, toggle() { }, contains: () => false }, dataset: {},
    setAttribute() { }, getAttribute: () => null, addEventListener() { }, querySelector: () => null,
    querySelectorAll: () => [], focus() { }, click() { } });
  return els.get(id);
};
globalThis.document = { getElementById: el, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() { }, title: '', fonts: { ready: Promise.resolve() } };
globalThis.window = { addEventListener() { }, scrollTo() { }, innerHeight: 900, scrollY: 0,
  matchMedia: () => ({ matches: false }), localStorage: { getItem: () => null, setItem() { }, removeItem() { } } };
globalThis.localStorage = globalThis.window.localStorage;
globalThis.IntersectionObserver = class { observe() { } unobserve() { } disconnect() { } };
globalThis.location = { href: '', reload() { } };
globalThis.alert = () => { };

eval(inline + '\n;globalThis.__R = { renderSectionValue, switchReport, S };');
const { renderSectionValue, switchReport, S } = globalThis.__R;

const readJson = async (response, label) => {
  const raw = await response.text();
  let body;
  try {
    body = JSON.parse(raw);
  } catch (error) {
    throw new Error(`${label} 返回非 JSON（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    throw new Error(`${label} 失败（HTTP ${response.status}）：${body.error || raw.slice(0, 200)}`);
  }
  return body;
};

// 用环境变量中的测试凭据登录，再通过脱敏预览凭证创建一个新病例。
// 患者账号不能访问医生端的 /api/v2/cases，因此这里不读取医生队列。
const patient = await readJson(await fetch(BASE + '/api/login', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: PATIENT_USERNAME, password: PATIENT_PASSWORD }),
}), '患者登录');
const token = patient.session?.token;
if (!token) throw new Error('患者登录成功但未返回会话凭证');
const authHeaders = {
  'Content-Type': 'application/json',
  Authorization: 'Bearer ' + token,
};
const caseInput = {
  medical_history: '男，61岁。主诉：视物模糊3个月，双下肢浮肿1周。既往2型糖尿病5年，尿蛋白++。',
  main_issue: '该看哪个科',
};
const preview = await readJson(await fetch(BASE + '/api/v2/preview', {
  method: 'POST', headers: authHeaders, body: JSON.stringify(caseInput),
}), '脱敏预览');
if (!preview.preview_token) throw new Error('脱敏预览成功但未返回 preview_token');
const created = await readJson(await fetch(BASE + '/api/v2/case', {
  method: 'POST', headers: authHeaders,
  body: JSON.stringify({ preview_token: preview.preview_token, age: 61, sex: '男' }),
}), '创建病例');
const target = created.case_id;
if (!target) throw new Error('创建病例成功但未返回 case_id');
console.log('核验病例：', target);

const report = await readJson(await fetch(`${BASE}/api/v2/case/${target}/report`, {
  headers: { Authorization: 'Bearer ' + token },
}), '读取报告');
const sections = (report.full || {}).sections || {};
console.log('段落数：', Object.keys(sections).length);

let problems = [];
for (const [name, value] of Object.entries(sections)) {
  const html = renderSectionValue(value);
  if (html.includes('[object Object]')) problems.push(`${name}: 出现 [object Object]`);
  if (/undefined|NaN/.test(html)) problems.push(`${name}: 出现 undefined/NaN`);
  for (const key of ['judgement', 'evidence_state', 'supports', 'confidence', 'step_id', 'scope']) {
    if (html.includes('>' + key + '<')) problems.push(`${name}: 泄漏英文键 ${key}`);
  }
  console.log(`  · ${name}：渲染 ${html.length} 字符${html.includes('[object Object]') ? '  ❌' : '  ✅'}`);
}

const sample = renderSectionValue(sections['证据关联']);
console.log('\n证据关联渲染样例（前 600 字）：\n' + sample.replace(/\s+/g, ' ').slice(0, 600));

/* ---------------------------------------------------------------------------
   完整报告页：逐段折叠（默认收起）+ 目录对应 + 折叠不丢内容
   —— 13 段全展开太长，所以做成可折叠；但**折叠只能影响显示，不能影响内容**：
   折叠时正文仍要在 DOM 里（打印前会自动全部展开，见患者端 beforeprint 钩子）。
--------------------------------------------------------------------------- */
console.log('\n=== 完整报告页（折叠结构）===');
S.report = report;
S.reportOpen = {};
switchReport('full');
const fullHtml = els.get('report-body').innerHTML;
const secCount = Object.keys(sections).length;
const detailCount = (fullHtml.match(/<details class="sec"/g) || []).length;
const bodyCount = (fullHtml.match(/class="sec-body"/g) || []).length;
const hintCount = (fullHtml.match(/class="sec-hint"/g) || []).length;
const tocCount = (fullHtml.match(/id="report-toc"[\s\S]*?<\/nav>/)?.[0].match(/<a /g) || []).length;

if (detailCount !== secCount) problems.push(`折叠段落数 ${detailCount} ≠ 报告段数 ${secCount}`);
if (bodyCount !== secCount) problems.push(`正文容器数 ${bodyCount} ≠ 报告段数 ${secCount}`);
if (hintCount !== secCount) problems.push(`摘要提示数 ${hintCount} ≠ 报告段数 ${secCount}`);
if (tocCount !== secCount) problems.push(`目录链接数 ${tocCount} ≠ 报告段数 ${secCount}`);
if (fullHtml.includes(' open>')) problems.push('默认应为全部收起，但有段落带 open 属性');
if (!fullHtml.includes('id="rep-toggle-all"')) problems.push('缺少「展开全部」按钮');
for (let i = 0; i < secCount; i++) {
  if (!fullHtml.includes(`id="sec-${i}"`)) problems.push(`缺少段落容器 sec-${i}`);
  if (!fullHtml.includes(`data-sec="sec-${i}"`)) problems.push(`目录缺少指向 sec-${i} 的链接`);
}
if (fullHtml.includes('[object Object]')) problems.push('完整报告 HTML 里出现 [object Object]');
if (/undefined|NaN/.test(fullHtml)) problems.push('完整报告 HTML 里出现 undefined/NaN');
// 折叠状态下正文必须已经在 DOM 里（否则打印/复制会丢内容）
for (const [name, value] of Object.entries(sections)) {
  const rendered = renderSectionValue(value);
  const key = rendered.replace(/\s+/g, '').slice(0, 60);
  if (key.length > 0 && !fullHtml.replace(/\s+/g, '').includes(key)) {
    problems.push(`折叠状态下段落「${name}」的正文不在 DOM 里`);
  }
}
console.log(`  · 折叠段落 ${detailCount} / 目录链接 ${tocCount} / 正文容器 ${bodyCount} / 摘要提示 ${hintCount}`);
console.log(`  · 默认收起：${fullHtml.includes(' open>') ? '❌ 有段落默认展开' : '✅ 全部收起'}`);
console.log(`  · 折叠时正文仍在 DOM（打印不丢）：${problems.some(p => p.includes('不在 DOM')) ? '❌' : '✅'}`);
console.log(`  · 「展开全部」按钮：${fullHtml.includes('id="rep-toggle-all"') ? '✅' : '❌'}`);

/* 展开状态记忆：记过的段落在重渲染后应保持展开 */
S.reportOpen = { 0: true };
switchReport('full');
const reopened = els.get('report-body').innerHTML;
console.log(`  · 重渲染后保留展开状态：${reopened.includes(' open>') ? '✅' : '❌'}`);
if (!reopened.includes(' open>')) problems.push('展开状态在重渲染后丢失');

console.log('\n结论：' + (problems.length ? '❌ 仍有问题\n - ' + problems.join('\n - ') : '✅ 全部段落渲染正常，折叠结构正确、无 [object Object]、无英文键泄漏'));
process.exit(problems.length ? 1 : 0);
