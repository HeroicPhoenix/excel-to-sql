# -*- coding: utf-8 -*-
import math, json, tempfile
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional, Dict

import pandas as pd
from flask import Flask, request, jsonify, send_file, render_template

# ⭐ 新增：把 Flask 包成 ASGI
from starlette.middleware.wsgi import WSGIMiddleware

app = Flask(__name__, template_folder="templates", static_folder="static")


@app.get("/")
def index():
    return render_template("index.html")


# ---------------- 工具函数 ----------------
def is_null(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def sql_escape_str(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_serialize_value(v):
    if is_null(v):
        return "NULL"
    if isinstance(v, (pd.Timestamp, datetime)):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, date):
        return "'" + v.strftime("%Y-%m-%d") + "'"
    try:
        import pandas as _pd
        bool_type = (_pd.BooleanDtype().type,)
    except Exception:
        bool_type = ()
    if isinstance(v, (bool,) + bool_type):
        return "1" if bool(v) else "0"
    if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
        return str(v)
    return sql_escape_str(v)


def backtick(col: str) -> str:
    return "`" + str(col).replace("`", "``") + "`"


def map_col(c: str, alias_map: Dict[str, str]) -> str:
    """原列名 -> 数据库目标列名（别名），默认不变"""
    return alias_map.get(c, c)


def ensure_unique(mapped_cols: List[str]):
    """检查别名映射后是否有重复的目标列名"""
    if len(mapped_cols) != len(set(mapped_cols)):
        dup = [x for x in mapped_cols if mapped_cols.count(x) > 1]
        raise ValueError(f"映射后的目标列名存在重复：{set(dup)}，请修改别名避免冲突。")


# ---------------- SQL 生成 ----------------
def build_update_sql(
    row: pd.Series,
    table_name: str,
    primary_keys: List[str],
    update_cols: List[str],
    skip_nulls_in_set: bool,
    alias_map: Dict[str, str],
) -> str:
    where_parts = [
        f"{backtick(map_col(k, alias_map))} = {sql_serialize_value(row.get(k, None))}"
        for k in primary_keys
    ]

    set_parts = []
    for c in update_cols:
        if c in primary_keys:
            continue
        v = row.get(c, None)
        if is_null(v) and skip_nulls_in_set:
            continue
        set_parts.append(f"{backtick(map_col(c, alias_map))} = {sql_serialize_value(v)}")

    if not set_parts:
        return ""
    return f"UPDATE {backtick(table_name)} SET " + ", ".join(set_parts) + " WHERE " + " AND ".join(where_parts) + ";"


def build_insert_sql(row: pd.Series, table_name: str, cols: List[str], alias_map: Dict[str, str]) -> str:
    mapped_cols = [map_col(c, alias_map) for c in cols]
    ensure_unique(mapped_cols)
    col_list = ", ".join(backtick(c) for c in mapped_cols)
    val_list = ", ".join(sql_serialize_value(row.get(src, None)) for src in cols)
    return f"INSERT INTO {backtick(table_name)} ({col_list}) VALUES ({val_list});"


def build_upsert_sql(
    row: pd.Series,
    table_name: str,
    insert_cols_src: List[str],
    upsert_update_cols_src: List[str],
    mysql_version: str,
    skip_nulls_in_set: bool,
    alias_map: Dict[str, str],
) -> str:
    insert_cols_mapped = [map_col(c, alias_map) for c in insert_cols_src]
    ensure_unique(insert_cols_mapped)
    col_list = ", ".join(backtick(c) for c in insert_cols_mapped)
    val_list = ", ".join(sql_serialize_value(row.get(src, None)) for src in insert_cols_src)

    set_parts = []
    for src in upsert_update_cols_src:
        v = row.get(src, None)
        if is_null(v) and skip_nulls_in_set:
            continue
        mapped = map_col(src, alias_map)
        set_parts.append(f"{backtick(mapped)} = VALUES({backtick(mapped)})")

    if not set_parts:
        return f"INSERT INTO {backtick(table_name)} ({col_list}) VALUES ({val_list});"

    return (
        f"INSERT INTO {backtick(table_name)} ({col_list}) VALUES ({val_list}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(set_parts) + ";"
    )


def generate_sql_file(
    excel_file_path: str,
    sheet_name,
    table_name: str,
    primary_keys: List[str],
    update_columns: Optional[List[str]],
    stmt: str,  # UPDATE | INSERT | UPSERT
    mysql_version: str,  # mysql57 | mysql80
    skip_nulls_in_set: bool,
    alias_map: Dict[str, str],
) -> str:
    df = pd.read_excel(excel_file_path, sheet_name=sheet_name)
    all_cols = list(df.columns)

    if not set(primary_keys).issubset(all_cols):
        missing = set(primary_keys) - set(all_cols)
        raise ValueError(f"Excel 中缺少主键列：{missing}")

    if stmt == "UPDATE":
        upd_cols_src = update_columns if update_columns else [c for c in all_cols if c not in primary_keys]
    elif stmt in ("INSERT", "UPSERT"):
        if update_columns:
            insert_cols_src = []
            for c in all_cols:
                if c in primary_keys and c not in insert_cols_src:
                    insert_cols_src.append(c)
            for c in all_cols:
                if c in update_columns and c not in insert_cols_src:
                    insert_cols_src.append(c)
        else:
            insert_cols_src = all_cols[:]

        if stmt == "UPSERT":
            upsert_update_cols_src = [c for c in (update_columns or all_cols) if c not in primary_keys]
    else:
        raise ValueError(f"不支持的语句类型：{stmt}")

    tmp = tempfile.NamedTemporaryFile(prefix="gen_", suffix=".sql", delete=False)
    out_path = tmp.name
    tmp.close()

    with open(out_path, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            if stmt == "UPDATE":
                sql = build_update_sql(row, table_name, primary_keys, upd_cols_src, skip_nulls_in_set, alias_map)
            elif stmt == "INSERT":
                sql = build_insert_sql(row, table_name, insert_cols_src, alias_map)
            else:
                sql = build_upsert_sql(
                    row,
                    table_name,
                    insert_cols_src,
                    upsert_update_cols_src,
                    mysql_version,
                    skip_nulls_in_set,
                    alias_map,
                )
            if sql:
                f.write(sql + "\n")

    return out_path


# -------------- 预览：返回 sheet 或列名 --------------
from flask import request

@app.post("/preview_columns")
def preview_columns():
    try:
        uploaded = request.files.get("file")
        if not uploaded:
            return jsonify({"error": "缺少上传的 Excel 文件"}), 400

        with tempfile.NamedTemporaryFile(prefix="excel_", suffix=Path(uploaded.filename).suffix, delete=False) as tmp:
            uploaded.save(tmp.name)
            excel_path = tmp.name

        sheet_name = request.form.get("sheet_name")
        xls = pd.ExcelFile(excel_path)

        if not sheet_name:
            return jsonify({"sheets": xls.sheet_names})

        df = pd.read_excel(excel_path, sheet_name=sheet_name, nrows=0)
        return jsonify({"columns": list(df.columns)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------- 生成 SQL --------------
@app.post("/generate")
def generate():
    try:
        uploaded = request.files.get("file")
        if not uploaded:
            return jsonify({"error": "缺少上传的 Excel 文件"}), 400

        table_name = (request.form.get("table_name") or "").strip()
        if not table_name:
            return jsonify({"error": "table_name 必填"}), 400

        primary_keys = [c.strip() for c in (request.form.get("primary_keys") or "").split(",") if c.strip()]
        if not primary_keys:
            return jsonify({"error": "primary_keys 至少一个"}), 400

        update_columns = [c.strip() for c in (request.form.get("update_columns") or "").split(",") if c.strip()]
        sheet_name = request.form.get("sheet_name") or 0
        stmt = (request.form.get("stmt") or "UPDATE").upper()
        mysql_version = (request.form.get("mysql_version") or "mysql80").lower()
        skip_nulls_in_set = (request.form.get("skip_nulls_in_set", "true").lower() == "true")

        alias_json = request.form.get("column_alias_json") or "{}"
        try:
            alias_map = json.loads(alias_json)
            if not isinstance(alias_map, dict):
                raise ValueError("column_alias_json 必须是 JSON 对象")
        except Exception:
            return jsonify({"error": "column_alias_json 不是有效 JSON"}), 400

        with tempfile.NamedTemporaryFile(prefix="excel_", suffix=Path(uploaded.filename).suffix, delete=False) as tmp:
            uploaded.save(tmp.name)
            excel_path = tmp.name

        sql_path = generate_sql_file(
            excel_file_path=excel_path,
            sheet_name=sheet_name,
            table_name=table_name,
            primary_keys=primary_keys,
            update_columns=update_columns if update_columns else None,
            stmt=stmt,
            mysql_version=mysql_version,
            skip_nulls_in_set=skip_nulls_in_set,
            alias_map=alias_map,
        )

        return send_file(
            sql_path,
            as_attachment=True,
            download_name=f"{stmt.lower()}_{table_name}.sql",
            mimetype="application/sql",
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ⭐ 把 Flask 应用包成 ASGI，给 Uvicorn 跑
asgi_app = WSGIMiddleware(app)

# 本地开发：python app.py 直接跑（用 Uvicorn）
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:asgi_app", host="0.0.0.0", port=12080, reload=True, lifespan="off")
