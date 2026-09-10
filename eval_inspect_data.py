# -*- coding: utf-8 -*-
"""探查 MDT-5 系列数据：金标准 vs 预测，用于建归一化映射"""
import pandas as pd
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

df = pd.read_csv(os.path.join(ROOT, 'MDT-5.csv'), encoding='utf-8')
print('===== MDT-5.csv 全部 %d 例 =====' % len(df))
for i, row in df.iterrows():
    gold = [s.strip() for s in str(row['会诊专家']).split('\n') if s.strip()]
    print('--- 病例%d | 入院:%s | 申请:%s ---' % (i + 1, row['入院科室'], row['申请科室']))
    print('会诊专家(gold):', gold)
    print('讨论目的:', str(row['讨论目的'])[:90])

for fn in ['MDT-5-test.csv', 'MDT-5-test1.csv']:
    fp = os.path.join(ROOT, fn)
    if not os.path.exists(fp):
        continue
    t = pd.read_csv(fp, encoding='utf-8')
    print('\n===== %s 预测 =====' % fn)
    for i, row in t.iterrows():
        pred = [s.strip() for s in str(row['主控体选择专科']).split(';') if s.strip()]
        print('病例%d 预测(%d):' % (i + 1, len(pred)), pred)
