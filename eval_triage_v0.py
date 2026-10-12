# -*- coding: utf-8 -*-
"""
分诊评测 v0 —— 主控智能体选科 vs 真实 MDT 金标准（会诊专家列）

用法：
    python eval_triage_v0.py

读取：
    MDT-5.csv           金标准（会诊专家列，按换行分隔）
    MDT-5-test.csv      预测（主控体选择专科，按 ; 分隔）
    MDT-5-test1.csv     预测（另一轮运行）

输出：
    eval_out/triage_v0_report.csv   每例明细
    eval_out/triage_v0_summary.csv  汇总指标
    控制台逐例报告（漏选/多余科室 = 最可操作的改进线索）

注意：
    - ALIAS 只做"同义科室名归一化"（心内科→心血管内科等），不改语义
    - 系统未建模的金标准科室（如肝胆外科、超声科）单独列出，不计入召回分子
"""
import os
import re
import pandas as pd

from eval_paths import data_path, output_root

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = str(output_root())

GOLD_FILE = str(data_path('MDT-5.csv'))
PRED_FILES = [str(data_path(name)) for name in ('MDT-5-test.csv', 'MDT-5-test1.csv')]

# 系统建模的 36 个专科（canonical 名称，与 mdt_agents.specialists 中文名一致）
SYSTEM_SPECIALTIES = set([
    '心血管内科', '神经内科', '肿瘤科', '内分泌科', '呼吸内科', '消化内科',
    '肾内科', '血液科', '风湿免疫科', '感染科', '老年医学科',
    '普通外科', '骨科', '神经外科', '心胸外科', '泌尿外科', '整形外科', '血管外科',
    '皮肤科', '眼科', '耳鼻喉科', '精神科', '康复医学科', '急诊科', '重症医学科',
    '麻醉科', '影像科', '病理科', '检验科', '妇产科', '儿科', '新生儿科',
    '中医科', '针灸科',
])

# 同义/简称 → canonical（安全归一化，不跨专科）
ALIAS = {
    '风湿科': '风湿免疫科', '心内科': '心血管内科', '呼吸科': '呼吸内科',
    '血液内科': '血液科', '感染性疾病科': '感染科', '耳鼻咽喉科': '耳鼻喉科',
    '耳鼻咽喉头颈外科': '耳鼻喉科', '康复科': '康复医学科', '内分泌二区': '内分泌科',
}

def normalize(raw: str) -> str:
    raw = raw.strip().replace('，', '、').replace(' ', '')
    return ALIAS.get(raw, raw)

def fix_concat(tokens):
    """处理 '超声\\n科麻醉科' 这类换行丢失：续行以'科'开头时，
    说明上一行末尾的'科'被换行拆开了，应还原为 '超声科' + '麻醉科'"""
    fixed = []
    for tk in tokens:
        if tk.startswith('科') and fixed:
            fixed[-1] = fixed[-1] + tk[0]          # 上一行补回'科'
            rest = tk[1:]
            if rest:
                fixed.append(rest)                 # 剩余部分自成一行
        else:
            fixed.append(tk)
    return fixed

def parse_gold(cell: str):
    tokens = [s.strip() for s in str(cell).replace('\r', '').split('\n') if s.strip()]
    tokens = fix_concat(tokens)
    names = [normalize(t) for t in tokens]
    return [n for n in names if n]

def parse_pred(cell: str):
    tokens = [s.strip() for s in str(cell).split(';') if s.strip()]
    names = [normalize(t) for t in tokens]
    return [n for n in names if n]

def evaluate(gold_names, pred_names):
    """gold/pred 均为 canonical 名单；仅统计系统已建模的科室"""
    gold = set(gold_names)
    pred = set(pred_names)
    modeled_gold = {g for g in gold if g in SYSTEM_SPECIALTIES}
    unmodeled_gold = gold - modeled_gold
    pred_valid = {p for p in pred if p in SYSTEM_SPECIALTIES}

    tp = len(modeled_gold & pred_valid)
    recall = tp / len(modeled_gold) if modeled_gold else float('nan')
    precision = tp / len(pred_valid) if pred_valid else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        'gold_total': len(gold), 'gold_modeled': len(modeled_gold),
        'unmodeled_gold': '、'.join(sorted(unmodeled_gold)) or '-',
        'pred_total': len(pred), 'pred_valid': len(pred_valid),
        'tp': tp, 'recall': round(recall, 3), 'precision': round(precision, 3),
        'f1': round(f1, 3),
        'missed': '、'.join(sorted(modeled_gold - pred_valid)) or '-',
        'extra': '、'.join(sorted(pred_valid - modeled_gold)) or '-',
        'all_hit': 1 if modeled_gold and modeled_gold <= pred_valid else 0,
    }

def main():
    gold_df = pd.read_csv(GOLD_FILE, encoding='utf-8')
    n = len(gold_df)
    print('=' * 90)
    print('分诊评测 v0：%d 例真实 MDT 病例，金标准=会诊专家列（已做科室名归一化）' % n)
    print('=' * 90)

    summaries = []
    for pfile in PRED_FILES:
        fp = pfile
        if not os.path.exists(fp):
            print('跳过（不存在）:', pfile)
            continue
        pred_df = pd.read_csv(fp, encoding='utf-8')
        rows = []
        print('\n########## 预测来源：%s ##########' % pfile)
        for i in range(n):
            gold_names = parse_gold(gold_df.loc[i, '会诊专家'])
            pred_names = parse_pred(pred_df.loc[i, '主控体选择专科'])
            r = evaluate(gold_names, pred_names)
            r['case'] = '病例%d' % (i + 1)
            r['gold'] = '、'.join(gold_names)
            r['pred'] = '、'.join(pred_names)
            rows.append(r)
            print('\n病例%d  金标准(%d): %s' % (i + 1, len(gold_names), '、'.join(gold_names)))
            print('        预测  (%d): %s' % (len(pred_names), '、'.join(pred_names)))
            print('        召回 %.3f | 精确率 %.3f | F1 %.3f' % (r['recall'], r['precision'], r['f1']))
            if r['missed'] != '-':
                print('        ⚠ 漏选(已建模): %s' % r['missed'])
            if r['extra'] != '-':
                print('        ➕ 多余: %s' % r['extra'])
            if r['unmodeled_gold'] != '-':
                print('        ? 金标准中系统未建模科室: %s（建议扩充专科或映射）' % r['unmodeled_gold'])

        report = pd.DataFrame(rows)[['case', 'gold', 'pred', 'gold_total', 'gold_modeled',
                                     'unmodeled_gold', 'pred_total', 'pred_valid', 'tp',
                                     'recall', 'precision', 'f1', 'missed', 'extra', 'all_hit']]
        tag = os.path.splitext(os.path.basename(pfile))[0]
        report.to_csv(os.path.join(OUT, 'triage_v0_report_%s.csv' % tag),
                      index=False, encoding='utf-8-sig')
        # 汇总
        rcol = report['recall'].astype(float)
        pcol = report['precision'].astype(float)
        fcol = report['f1'].astype(float)
        micro_tp = int(report['tp'].sum())
        micro_gold = int(report['gold_modeled'].sum())
        micro_pred = int(report['pred_valid'].sum())
        micro_r = micro_tp / micro_gold if micro_gold else float('nan')
        micro_p = micro_tp / micro_pred if micro_pred else 0.0
        s = {
            'source': pfile, 'cases': n,
            'macro_recall': round(rcol.mean(), 3), 'macro_precision': round(pcol.mean(), 3),
            'macro_f1': round(fcol.mean(), 3),
            'micro_recall': round(micro_r, 3), 'micro_precision': round(micro_p, 3),
            'micro_f1': round(2 * micro_p * micro_r / (micro_p + micro_r), 3) if (micro_p + micro_r) else 0.0,
            'all_hit_cases': int(report['all_hit'].sum()),
            'avg_pred_count': round(float(report['pred_total'].mean()), 1),
        }
        summaries.append(s)
        print('\n── 汇总(%s) ──' % pfile)
        print('macro: 召回 %.3f  精确率 %.3f  F1 %.3f' % (s['macro_recall'], s['macro_precision'], s['macro_f1']))
        print('micro: 召回 %.3f  精确率 %.3f  F1 %.3f' % (s['micro_recall'], s['micro_precision'], s['micro_f1']))
        print('全命中病例: %d/%d | 平均预测科室数: %.1f' % (s['all_hit_cases'], n, s['avg_pred_count']))

    if summaries:
        pd.DataFrame(summaries).to_csv(os.path.join(OUT, 'triage_v0_summary.csv'),
                                       index=False, encoding='utf-8-sig')
        print('\n明细与汇总已保存至: %s' % OUT)

if __name__ == '__main__':
    main()
