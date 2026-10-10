"""把旧的随机病例号（如 C1792ECDF）统一改成按日期编号（如 YX20261010-0003）。

新建病例已经自动使用日期编号；本脚本只用于把已有数据库里的旧病例号一次性换掉。

用法（在 backend 目录下，先停止 app.py 后端服务）：

    python tools/renumber_case_ids.py --dry-run     # 只预览对照表，不改数据库
    python tools/renumber_case_ids.py               # 备份后正式改号

规则：
- 按病例建档时间（created_at）的日期编号，同一天内按建档先后排流水号；已是新格式的病例保留原号，
  旧病例从当天已有的最大流水往后排。
- 改号前用 SQLite 备份接口完整备份数据库（同目录 ``*.bak-时间戳``），并写出新旧编号对照 CSV。
- 在一个事务里替换所有表的所有文本列（病例、审计、审核决定、状态流转、授权、报告快照及其中的
  JSON 正文），任何一步失败整体回滚。
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from services.store import default_db_path  # noqa: E402

OLD_ID = re.compile(r"^C[0-9A-F]{8}$")


def build_mapping(conn: sqlite3.Connection, prefix: str) -> List[Tuple[str, str, str]]:
    """返回 [(旧编号, 新编号, 建档时间)]，只包含需要改号的旧格式病例。"""
    new_id = re.compile(rf"^{re.escape(prefix)}(\d{{8}})-(\d{{4}})$")
    highest: Dict[str, int] = {}
    rows = conn.execute("SELECT case_id, COALESCE(created_at, '') FROM cases").fetchall()
    for case_id, _ in rows:
        match = new_id.match(str(case_id))
        if match:
            day, seq = match.group(1), int(match.group(2))
            highest[day] = max(highest.get(day, 0), seq)
    old_rows = sorted(
        ((str(cid), str(created)) for cid, created in rows if OLD_ID.match(str(cid))),
        key=lambda item: (item[1] or "9999", item[0]),
    )
    mapping = []
    for case_id, created in old_rows:
        digits = re.sub(r"\D", "", created[:10])
        day = digits if len(digits) == 8 else datetime.now().strftime("%Y%m%d")
        highest[day] = highest.get(day, 0) + 1
        mapping.append((case_id, f"{prefix}{day}-{highest[day]:04d}", created))
    return mapping


def text_columns(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    columns = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        for _, name, col_type, *_ in conn.execute(f'PRAGMA table_info("{table}")'):
            if "CHAR" in str(col_type).upper() or "TEXT" in str(col_type).upper() or not col_type:
                columns.append((table, name))
    return columns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=default_db_path(), help="数据库路径（默认 backend/data/mdt.db 或 APP_DB_PATH）")
    parser.add_argument("--prefix", default="YX", help="病例号前缀，默认 YX")
    parser.add_argument("--dry-run", action="store_true", help="只打印对照表，不修改数据库")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"找不到数据库：{args.db}")
        return 1
    conn = sqlite3.connect(args.db, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        mapping = build_mapping(conn, args.prefix)
        if not mapping:
            print("没有需要改号的旧病例。")
            return 0
        print(f"共 {len(mapping)} 个旧病例号需要改为日期编号：")
        for old, new, created in mapping[:20]:
            print(f"  {old}  →  {new}   （建档 {created or '时间未记录'}）")
        if len(mapping) > 20:
            print(f"  …… 其余 {len(mapping) - 20} 个见对照 CSV")
        if args.dry_run:
            print("预览模式：未修改数据库。")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{args.db}.bak-{stamp}"
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        csv_path = f"{args.db}.case-id-map-{stamp}.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["旧病例号", "新病例号", "建档时间"])
            writer.writerows(mapping)

        columns = text_columns(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for old, new, _ in mapping:
                for table, column in columns:
                    conn.execute(
                        f'UPDATE "{table}" SET "{column}" = REPLACE("{column}", ?, ?) '
                        f'WHERE instr("{column}", ?) > 0',
                        (old, new, old),
                    )
            leftover = conn.execute(
                "SELECT COUNT(*) FROM cases WHERE case_id GLOB 'C[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]'"
            ).fetchone()[0]
            if leftover:
                raise RuntimeError(f"仍有 {leftover} 个旧编号未替换")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"已完成改号。备份：{backup_path}")
        print(f"新旧编号对照：{csv_path}")
        print("请重新启动后端；浏览器里如停留在旧病例页面，刷新后从列表重新打开。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
