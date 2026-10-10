/*
 * 医生审核后发布给患者的最终报告：按线下会诊意见单排版。
 * 报头 → 基本信息栏 → 各科意见 → 会诊结论 → 医生建议 → 后续安排 → 签发栏 → 免责声明。
 * 医生端发布预览 / 历史版本与患者端结果页 / 打印 / 历史版本共用这一份渲染，
 * 保证医生预览到的就是患者收到的。所有字段一律按文本转义输出。
 */
(function (global) {
  'use strict';

  var NUMERALS = ['一', '二', '三', '四', '五', '六', '七', '八'];
  var TITLES = {
    '医生审核后的专科评估意见': '专科评估意见',
    '多学科会诊结论': '多学科会诊结论',
    '医生最终建议': '医生建议',
    '后续安排': '后续安排'
  };

  function frEsc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* 把任意值压成一行可读文本；兼容旧快照里的对象式条目。 */
  function finalReportText(value) {
    if (value === null || value === undefined) return '';
    if (Array.isArray(value)) return value.map(finalReportText).filter(Boolean).join('；');
    if (typeof value === 'object') {
      return Object.keys(value).map(function (key) {
        var text = finalReportText(value[key]);
        return text ? key + '：' + text : '';
      }).filter(Boolean).join('；');
    }
    return String(value).trim();
  }

  function finalReportList(value) {
    if (Array.isArray(value)) return value.map(finalReportText).filter(Boolean);
    var text = finalReportText(value);
    return text ? text.split(/\n+/).map(function (x) { return x.trim(); }).filter(Boolean) : [];
  }

  function opinionRows(value) {
    var rows = Array.isArray(value) ? value : [value];
    var html = rows.map(function (row) {
      if (!row || typeof row !== 'object') {
        var text = finalReportText(row);
        return text ? '<p class="fr-empty">' + frEsc(text) + '</p>' : '';
      }
      var opinions = finalReportList(row['专科评估意见']);
      var checks = finalReportList(row['建议向医生确认']);
      var status = row['审核'] || row['来源'] || '';
      var body = opinions.length > 1
        ? '<ol>' + opinions.map(function (x) { return '<li>' + frEsc(x) + '</li>'; }).join('') + '</ol>'
        : '<p>' + frEsc(opinions[0] || '（无）') + '</p>';
      return '<div class="fr-dept"><div class="fr-dept-name"><strong>' + frEsc(row['科室'] || '专科') + '</strong>' +
        (status ? '<small>' + frEsc(status) + '</small>' : '') + '</div><div class="fr-dept-body">' + body +
        (checks.length ? '<p class="fr-ask"><span>可向医生确认</span>' + frEsc(checks.join('；')) + '</p>' : '') +
        '</div></div>';
    }).join('');
    return html || '<p class="fr-empty">（暂无已由医生确认的专科评估意见）</p>';
  }

  function finalReportDocumentHtml(report, options) {
    report = report || {};
    var preview = !!(options && options.preview);
    var header = report.header || {};
    var sections = report.sections || {};
    var order = (report.section_order || Object.keys(sections)).filter(function (key) { return key in sections; });
    var departments = Array.isArray(header['会诊科室'])
      ? header['会诊科室'].filter(Boolean).join('、') : String(header['会诊科室'] || '');
    var sexAge = [header['性别'], header['年龄']].filter(Boolean).join(' · ');
    var info = [
      ['病历编号', header['病历编号']], ['性别 / 年龄', sexAge], ['处理方式', header['处理方式']],
      ['主责科室', header['主责科室']], ['会诊科室', departments, 'wide'], ['主诉', header['主诉'], 'full']
    ].filter(function (item) { return String(item[1] || '').trim(); });
    var title = header['报告名称'] || '医生审核意见报告';

    var body = order.map(function (key, index) {
      var value = sections[key];
      var content;
      if (key === '医生审核后的专科评估意见') {
        content = opinionRows(value);
      } else if (key === '后续安排') {
        var items = finalReportList(value);
        content = items.length
          ? '<ol class="fr-steps">' + items.map(function (x) { return '<li>' + frEsc(x) + '</li>'; }).join('') + '</ol>'
          : '<p class="fr-empty">（无）</p>';
      } else {
        content = '<div class="fr-prose">' + frEsc(finalReportText(value) || '（无）') + '</div>';
      }
      return '<section class="fr-sec' + (key === '医生最终建议' ? ' fr-advice' : '') + '"><h4><span>' +
        (NUMERALS[index] || String(index + 1)) + '、</span>' + frEsc(TITLES[key] || key) + '</h4>' + content + '</section>';
    }).join('');

    return '<article class="final-report" aria-label="' + frEsc(title) + '">' +
      '<header class="fr-head">' +
        '<div class="fr-org"><span>医序｜多学科就医信息协作平台</span><span>报告编号：' +
          frEsc(header['报告编号'] || report.case_id || '—') + '</span></div>' +
        '<h3 class="fr-title">' + frEsc(title) + '</h3>' +
        (preview ? '<span class="fr-stamp">预览 · 尚未发布</span>' : '') +
      '</header>' +
      (info.length ? '<dl class="fr-info">' + info.map(function (item) {
        return '<div class="' + (item[2] || '') + '"><dt>' + frEsc(item[0]) + '</dt><dd>' + frEsc(item[1]) + '</dd></div>';
      }).join('') + '</dl>' : '') +
      body +
      '<footer class="fr-sign"><div><span>审核医生</span><strong>' + frEsc(header['审核医生'] || '—') +
        '</strong></div><div><span>报告日期</span><strong>' + frEsc(header['报告日期'] || '—') + '</strong></div></footer>' +
      (report.disclaimer ? '<p class="fr-note">' + frEsc(report.disclaimer) + '</p>' : '') +
    '</article>';
  }

  global.finalReportDocumentHtml = finalReportDocumentHtml;
  global.finalReportText = finalReportText;
  global.finalReportList = finalReportList;
})(typeof globalThis !== 'undefined' ? globalThis : window);
