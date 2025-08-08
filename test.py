# -*- coding: utf-8 -*-
import pandas as pd
import math
from datetime import datetime, date
from pathlib import Path

# ======== 必填配置 ========
EXCEL_PATH = "数仓表清单.xlsx"        # Excel 文件路径
SHEET_NAME = 1                  # sheet 名或索引
TABLE_NAME = "your_table"       # 目标 MySQL 表
PRIMARY_KEYS = ["原始表名", "核验表名"]           # 主键列名，支持多个，如 ["biz_id","date"]
UPDATE_COLUMNS = None           # 需要更新的列；None 表示自动 = 所有列 - 主键列
OUTPUT_SQL = "updates.sql"      # 输出 .sql 文件路径

# 遇到空值/NaN：True=在SET中跳过该列；False=写成 NULL
SKIP_NULLS_IN_SET = True

# 每多少行写一次磁盘（避免内存太大）
FLUSH_EVERY = 1000
# ======== 可选配置结束 ========


def is_null(v):
    return v is None or (isinstance(v, float) and math.isnan(v))

def sql_escape_str(s: str) -> str:
    """转义为SQL字符串常量：包单引号并转义内部单引号和反斜杠"""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"

def sql_serialize_value(v):
    """把 Python 值序列化为 MySQL 5.7 可用的字面量"""
    if is_null(v):
        return "NULL"
    # pandas 的 Timestamp/日期
    if isinstance(v, (pd.Timestamp, datetime)):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, date):
        return "'" + v.strftime("%Y-%m-%d") + "'"
    # 布尔 -> 0/1
    if isinstance(v, (bool, pd.BooleanDtype().type)):
        return "1" if v else "0"
    # 数字原样
    if isinstance(v, (int, float)) and not math.isnan(v):
        return str(v)
    # 其他一律按字符串处理并转义
    return sql_escape_str(v)

def backtick(col: str) -> str:
    """列名/表名加反引号，内部反引号转义"""
    return "`" + str(col).replace("`", "``") + "`"

def build_update_sql(row: pd.Series, update_cols):
    # WHERE 主键
    where_parts = []
    for k in PRIMARY_KEYS:
        if k not in row:
            raise ValueError(f"主键列 {k} 不在Excel列中！")
        where_parts.append(f"{backtick(k)} = {sql_serialize_value(row[k])}")
    where_sql = " AND ".join(where_parts)

    # SET 更新列
    set_parts = []
    for c in update_cols:
        if c in PRIMARY_KEYS:
            continue
        v = row[c] if c in row else None
        if is_null(v) and SKIP_NULLS_IN_SET:
            # 跳过空值，不更新该列
            continue
        set_parts.append(f"{backtick(c)} = {sql_serialize_value(v)}")

    if not set_parts:
        # 该行没有需要更新的列，返回空字符串表示跳过
        return ""

    return f"UPDATE {backtick(TABLE_NAME)} SET " + ", ".join(set_parts) + " WHERE " + where_sql + ";"

def main():
    # 读 Excel
    df = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_NAME)
    if not set(PRIMARY_KEYS).issubset(df.columns):
        missing = set(PRIMARY_KEYS) - set(df.columns)
        raise ValueError(f"Excel 中缺少主键列：{missing}")

    update_cols = UPDATE_COLUMNS
    if update_cols is None:
        update_cols = [c for c in df.columns if c not in PRIMARY_KEYS]

    # 输出文件准备
    Path(OUTPUT_SQL).parent.mkdir(parents=True, exist_ok=True)
    written = 0
    buffer = []

    with open(OUTPUT_SQL, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            sql = build_update_sql(row, update_cols)
            if not sql:
                continue
            buffer.append(sql + "\n")
            if len(buffer) >= FLUSH_EVERY:
                f.writelines(buffer)
                written += len(buffer)
                buffer.clear()
        if buffer:
            f.writelines(buffer)
            written += len(buffer)

    print(f"已生成 {written} 条 UPDATE 到 {OUTPUT_SQL}")

if __name__ == "__main__":
    main()

