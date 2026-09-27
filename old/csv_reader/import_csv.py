#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Toilet CSV Import Script for Bathroom Genius
匯入公廁 CSV 資料至 MongoDB 的腳本工具
"""

import os
import sys
import csv
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
# 確保 Windows 主控台輸出中文與 emoji 不會拋出 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
# ----------------------------------------------------------------------
# 欄位映射對照表 (支援中英文、大小寫、蛇形與駝峰命名)
# ----------------------------------------------------------------------
HEADER_ALIASES: Dict[str, List[str]] = {
    "name": ["name", "名稱", "廁所名稱", "公廁名稱", "title", "toilet_name", "location_name"],
    "address": ["address", "地址", "詳細地址", "addr", "location_address"],
    "lng": ["lng", "longitude", "經度", "lon", "x", "wgs84_lng", "wgs84_x"],
    "lat": ["lat", "latitude", "緯度", "y", "wgs84_lat", "wgs84_y"],
    "landmark": ["landmark", "地標", "所在地標", "building", "place", "place_name"],
    "googleMapUrl": ["googlemapurl", "google_map_url", "googlemap_url", "map_url", "地圖連結", "google地圖"],
    "openType": ["opentype", "open_type", "開放類型", "開放性質", "type"],
    "hours": ["hours", "open_hours", "business_hours", "開放時間", "營業時間", "time"],
    "hasToiletPaper": [
        "hastoiletpaper", "has_toilet_paper", "toilet_paper", "衛生紙", "提供衛生紙", "衛生紙提供", "paper"
    ],
    "isAccessible": [
        "isaccessible", "is_accessible", "accessible", "無障礙", "無障礙設施", "身障設施", "barrier_free"
    ],
    "tags": ["tags", "tag", "標籤", "分類標籤", "features", "特色"],
    "note": ["note", "notes", "備註", "說明", "備註說明", "description", "remark"],
    "avgCleanScore": ["avgcleanscore", "avg_clean_score", "clean_score", "乾淨度評分", "乾淨度"],
    "avgConvenienceScore": ["avgconveniencescore", "avg_convenience_score", "convenience_score", "便利度評分", "便利度"],
    "reviewCount": ["reviewcount", "review_count", "reviews_count", "評論數", "總評論數"],
    "createdBy": ["createdby", "created_by", "creator", "建立者", "user_id"],
}


def load_env(env_path: Path) -> Dict[str, str]:
    """簡易解析 .env 檔案"""
    env_vars: Dict[str, str] = {}
    if not env_path.exists():
        return env_vars

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                env_vars[key] = val
    return env_vars


def parse_boolean(val: Any) -> Optional[bool]:
    """解析布林值 (支援 true/false, 1/0, 有/無, yes/no 等)"""
    if val is None:
        return None
    s = str(val).strip().lower()
    if s == "" or s in ("null", "none", "未知", "n/a", "na", "-"):
        return None
    if s in ("true", "1", "yes", "y", "t", "有", "是", "提供", "v", "ok"):
        return True
    if s in ("false", "0", "no", "n", "f", "無", "否", "未提供", "x"):
        return False
    return None


def parse_tags(val: Any) -> List[str]:
    """解析標籤陣列 (支援逗號、分號、空白、斜線分隔)"""
    if not val:
        return []
    s = str(val).strip()
    if not s or s.lower() in ("null", "none"):
        return []

    # 依序嘗試以 common 分隔符切割
    for delimiter in [",", "，", ";", "；", "|", "/"]:
        if delimiter in s:
            parts = [p.strip() for p in s.split(delimiter) if p.strip()]
            return list(dict.fromkeys(parts))

    # 若無特殊符號但有空格
    parts = [p.strip() for p in s.split() if p.strip()]
    return list(dict.fromkeys(parts))


def build_column_mapping(headers: List[str]) -> Dict[str, str]:
    """根據 CSV 欄位名稱自動對照標準 Schema 欄位名稱"""
    normalized_headers = {h.strip().lower().replace("_", "").replace("-", ""): h for h in headers}
    mapping: Dict[str, str] = {}

    for schema_field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            norm_alias = alias.lower().replace("_", "").replace("-", "")
            if norm_alias in normalized_headers:
                raw_header = normalized_headers[norm_alias]
                mapping[schema_field] = raw_header
                break

    return mapping


def transform_row(
    row: Dict[str, str], mapping: Dict[str, str], row_idx: int
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """將單列 CSV 轉換為符合 Mongoose Schema 的 Toilet Document"""
    # 1. 取得必填欄位：名稱
    name_col = mapping.get("name")
    name = row.get(name_col, "").strip() if name_col else ""
    if not name:
        return None, f"第 {row_idx} 列略過：缺少必填欄位 'name' (廁所名稱)"

    # 2. 取得必填欄位：經緯度
    lng_col = mapping.get("lng")
    lat_col = mapping.get("lat")
    if not lng_col or not lat_col:
        return None, f"第 {row_idx} 列略過：CSV 缺少經緯度欄位 ('lng' / 'lat')"

    try:
        lng_str = row.get(lng_col, "").strip()
        lat_str = row.get(lat_col, "").strip()
        if not lng_str or not lat_str:
            return None, f"第 {row_idx} 列略過：經度或緯度值為空"

        lng = float(lng_str)
        lat = float(lat_str)

        # 經緯度範圍檢查
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            return None, f"第 {row_idx} 列略過：座標超出範圍 (lng={lng}, lat={lat})"
    except ValueError:
        return None, f"第 {row_idx} 列略過：經緯度無法轉換為數值 (lng='{row.get(lng_col)}', lat='{row.get(lat_col)}')"

    # 3. 取得地址
    address_col = mapping.get("address")
    address = row.get(address_col, "").strip() if address_col else ""
    if not address:
        address = name  # 若無地址則 fallback 使用名稱

    # 4. 選填欄位
    landmark_col = mapping.get("landmark")
    landmark = row.get(landmark_col, "").strip() if landmark_col else None

    google_map_col = mapping.get("googleMapUrl")
    google_map_url = row.get(google_map_col, "").strip() if google_map_col else None

    # 5. 開放時間資訊 (openInfo)
    open_type_col = mapping.get("openType")
    hours_col = mapping.get("hours")
    open_type = row.get(open_type_col, "").strip() if open_type_col else None
    hours = row.get(hours_col, "").strip() if hours_col else None

    open_info = {}
    if open_type:
        open_info["openType"] = open_type
    if hours:
        open_info["hours"] = hours

    # 6. 布林屬性 (三態支援與座數判定)
    has_paper_col = mapping.get("hasToiletPaper")
    has_toilet_paper = parse_boolean(row.get(has_paper_col)) if has_paper_col else None

    accessible_col = mapping.get("isAccessible")
    is_accessible = parse_boolean(row.get(accessible_col)) if accessible_col else None

    # 若有「無障礙廁所座數」等欄位
    for acc_key in ["無障礙廁所座數", "無障礙座數", "accessible_seats", "barrier_free_seats"]:
        if acc_key in row and row.get(acc_key):
            try:
                seats = int(str(row[acc_key]).strip())
                if is_accessible is None:
                    is_accessible = seats > 0
            except ValueError:
                pass

    # 7. 標籤與備註 (含政府開放資料等級寫入 tags)
    tags_col = mapping.get("tags")
    tags = parse_tags(row.get(tags_col)) if tags_col else []

    # 補充類別與行政區到 tags
    for extra_tag_key in ["公廁類別", "行政區", "category", "district"]:
        if extra_tag_key in row and row[extra_tag_key].strip():
            tag_val = row[extra_tag_key].strip()
            if tag_val not in tags:
                tags.append(tag_val)

    if is_accessible and "無障礙" not in tags:
        tags.append("無障礙")

    # 親子座數
    for fam_key in ["親子廁所座數", "親子座數", "family_seats"]:
        if fam_key in row and row.get(fam_key):
            try:
                if int(str(row[fam_key]).strip()) > 0 and "親子" not in tags:
                    tags.append("親子")
            except ValueError:
                pass

    # 評價等級以備註寫在 tags (特優級 / 優等級 / 普通級 / 加強級)
    grade_fields = [
        ("特優級", ["特優級", "特優級座數", "grade_special"]),
        ("優等級", ["優等級", "優等級座數", "grade_excellent"]),
        ("普通級", ["普通級", "普通級座數", "grade_normal"]),
        ("加強級", ["加強級", "加強級座數", "grade_improve"]),
    ]
    for grade_name, grade_keys in grade_fields:
        for gk in grade_keys:
            if gk in row and row.get(gk):
                try:
                    g_val = int(str(row[gk]).strip())
                    if g_val > 0:
                        tag_str = f"{grade_name}({g_val}座)" if g_val > 1 else grade_name
                        if tag_str not in tags:
                            tags.append(tag_str)
                except ValueError:
                    pass

    # 去重
    tags = list(dict.fromkeys(tags))

    # 整合備註說明 (含一般時段位置、無障礙時段位置、管理單位)
    note_col = mapping.get("note")
    note_parts = []
    if note_col and row.get(note_col, "").strip():
        note_parts.append(row[note_col].strip())

    for loc_key, loc_prefix in [
        ("一般時段位置", "一般時段位置"),
        ("無障礙時段位置", "無障礙時段位置"),
        ("管理單位", "管理單位"),
    ]:
        if loc_key in row and row[loc_key].strip():
            val = row[loc_key].strip()
            if val != name:
                note_parts.append(f"{loc_prefix}: {val}")

    note = "；".join(note_parts) if note_parts else None

    # 8. 評分與統計
    clean_score_col = mapping.get("avgCleanScore")
    conv_score_col = mapping.get("avgConvenienceScore")
    rev_cnt_col = mapping.get("reviewCount")

    avg_clean_score = 0.0
    if clean_score_col and row.get(clean_score_col):
        try:
            avg_clean_score = float(row.get(clean_score_col, 0))
        except ValueError:
            pass

    avg_conv_score = 0.0
    if conv_score_col and row.get(conv_score_col):
        try:
            avg_conv_score = float(row.get(conv_score_col, 0))
        except ValueError:
            pass

    review_count = 0
    if rev_cnt_col and row.get(rev_cnt_col):
        try:
            review_count = int(row.get(rev_cnt_col, 0))
        except ValueError:
            pass

    created_by_col = mapping.get("createdBy")
    created_by = row.get(created_by_col, "").strip() if created_by_col else None

    now = datetime.now(timezone.utc)

    # 組合 Document
    doc: Dict[str, Any] = {
        "name": name,
        "location": {
            "type": "Point",
            "coordinates": [lng, lat],  # GeoJSON 規範: [lng, lat]
        },
        "address": address,
        "hasToiletPaper": has_toilet_paper,
        "isAccessible": is_accessible,
        "tags": tags,
        "avgCleanScore": avg_clean_score,
        "avgConvenienceScore": avg_conv_score,
        "reviewCount": review_count,
        "createdAt": now,
        "updatedAt": now,
    }

    if landmark:
        doc["landmark"] = landmark
    if google_map_url:
        doc["googleMapUrl"] = google_map_url
    if open_info:
        doc["openInfo"] = open_info
    if note:
        doc["note"] = note
    if created_by:
        doc["createdBy"] = created_by

    return doc, None


def import_csv(
    file_path: str,
    mongodb_uri: str,
    db_name: str,
    collection_name: str = "toilets",
    clear_collection: bool = False,
    batch_size: int = 1000,
    create_index: bool = True,
):
    """執行 CSV 匯入程序"""
    try:
        from pymongo import MongoClient, GEOSPHERE
        from pymongo.errors import PyMongoError
    except ImportError:
        print("\n[錯誤] 缺少必要套件 'pymongo'。請先執行安裝：")
        print("    pip install pymongo")
        print("或者：")
        print("    pip install pymongo dnspython\n")
        sys.exit(1)

    csv_file = Path(file_path)
    if not csv_file.exists():
        print(f"\n[錯誤] 找不到 CSV 檔案：{csv_file.resolve()}")
        sys.exit(1)

    print("\n==========================================")
    print("🚽 Bathroom Genius - CSV 資料匯入工具")
    print("==========================================")
    print(f"📁 來源檔案:     {csv_file.resolve()}")
    print(f"🔗 MongoDB URI:  {mongodb_uri}")
    print(f"📦 資料庫名稱:   {db_name}")
    print(f"📑 Collection:   {collection_name}")
    print("==========================================\n")

    # 連線 MongoDB
    try:
        client: MongoClient = MongoClient(mongodb_uri, serverSelectionTimeoutMS=5000)
        db = client[db_name]
        collection = db[collection_name]
        # 測試連線
        client.server_info()
    except PyMongoError as e:
        print(f"[連線失敗] 無法連線至 MongoDB: {e}")
        sys.exit(1)

    # 選擇性清空 Collection
    if clear_collection:
        deleted_count = collection.delete_many({}).deleted_count
        print(f"🗑️ 已清空 collection '{collection_name}' (共清除 {deleted_count} 筆資料)")

    # 嘗試偵測 CSV 編碼 (utf-8, utf-8-sig, big5, cp950)
    encodings_to_try = ["utf-8-sig", "utf-8", "big5", "cp950", "gbk"]
    detected_encoding = "utf-8-sig"
    reader = None

    for enc in encodings_to_try:
        try:
            with open(csv_file, "r", encoding=enc) as f:
                sample = f.read(2048)
                if sample:
                    detected_encoding = enc
                    break
        except UnicodeDecodeError:
            continue

    print(f"ℹ️ 使用檔案編碼: {detected_encoding}")

    # 讀取 CSV
    with open(csv_file, "r", encoding=detected_encoding, errors="replace") as f:
        csv_reader = csv.DictReader(f)
        headers = csv_reader.fieldnames or []
        if not headers:
            print("[錯誤] CSV 檔案內容為空或無標題列 (Headers)。")
            sys.exit(1)

        print(f"📋 讀取到 CSV 欄位 ({len(headers)} 個): {', '.join(headers)}")
        mapping = build_column_mapping(headers)
        print(f"🔍 自動對應成功欄位 ({len(mapping)} 個): {mapping}\n")

        if "name" not in mapping:
            print("[警告] 未能自動識別 'name' 欄位，請檢查 CSV 標題是否有包含廁所名稱！")
        if "lng" not in mapping or "lat" not in mapping:
            print("[警告] 未能自動識別 'lng' 或 'lat' 經緯度欄位！")

        documents: List[Dict[str, Any]] = []
        total_rows = 0
        success_count = 0
        skipped_count = 0
        skipped_reasons: List[str] = []

        for row_idx, row in enumerate(csv_reader, start=2):
            total_rows += 1
            doc, error_msg = transform_row(row, mapping, row_idx)
            if error_msg:
                skipped_count += 1
                if len(skipped_reasons) < 10:  # 僅記錄前 10 筆錯誤訊息
                    skipped_reasons.append(error_msg)
                continue

            documents.append(doc)
            success_count += 1

            # 批次寫入
            if len(documents) >= batch_size:
                try:
                    collection.insert_many(documents, ordered=False)
                    print(f"  ⚡ 已匯入 {success_count} 筆資料...")
                    documents.clear()
                except PyMongoError as pe:
                    print(f"  [批次寫入警告] 部分寫入錯誤: {pe}")
                    documents.clear()

        # 寫入剩餘文件
        if documents:
            try:
                collection.insert_many(documents, ordered=False)
                documents.clear()
            except PyMongoError as pe:
                print(f"  [批次寫入警告] 部分寫入錯誤: {pe}")

    # 建立 2dsphere 索引
    if create_index:
        try:
            print("\n🧭 正在確保 2dsphere 地理位置索引存在...")
            collection.create_index([("location", GEOSPHERE)], name="location_2dsphere")
            print("✅ 2dsphere 空間索引建立完成 ('location_2dsphere')")
        except PyMongoError as e:
            print(f"⚠️ 建立 2dsphere 索引失敗: {e}")

    # 總結
    print("\n==========================================")
    print("🎉 匯入作業完成！")
    print(f"   總讀取列數:   {total_rows}")
    print(f"   ✅ 成功匯入:  {success_count} 筆")
    print(f"   ⚠️ 略過列數:  {skipped_count} 筆")
    if skipped_reasons:
        print("\n   [略過範例說明 (最多顯示 10 筆)]:")
        for reason in skipped_reasons:
            print(f"   - {reason}")
    print("==========================================\n")


def main():
    # 取得專案根目錄
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    # 嘗試讀取 .env (優先檢查專案根目錄，次之 backend/.env)
    env_vars: Dict[str, str] = {}
    for possible_env in [project_root / ".env", project_root / "backend" / ".env"]:
        if possible_env.exists():
            env_vars.update(load_env(possible_env))

    default_uri = (
        os.environ.get("MONGODB_URI")
        or env_vars.get("MONGODB_URI")
        or "mongodb://localhost:27017"
    )
    default_db = (
        os.environ.get("MONGODB_DB_NAME")
        or env_vars.get("MONGODB_DB_NAME")
        or env_vars.get("DB_NAME")
        or "bathroom_genius"
    )
    default_file = str(project_root / "toilets.csv")

    parser = argparse.ArgumentParser(
        description="Bathroom Genius - 公廁資料 CSV 批次匯入工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例用法:
  # 1. 使用預設值 (自動讀取 backend/.env 中的 MONGODB_URI 與 MONGODB_DB_NAME):
  python utils/import_csv.py -f raw_data/臺北市公廁點位資訊_UTF8_converted.csv

  # 2. 透過參數直接指定 (旗標方式):
  python utils/import_csv.py -f raw_data/臺北市公廁點位資訊_UTF8_converted.csv -u "mongodb://localhost:27017" -d "bathroom"

  # 3. 透過位置參數 (argv) 直接指定:
  python utils/import_csv.py raw_data/臺北市公廁點位資訊_UTF8_converted.csv "mongodb://localhost:27017" "bathroom"
""",
    )

    # 支援位置參數 (可直接傳入 file, uri, db)
    parser.add_argument(
        "pos_file",
        nargs="?",
        default=None,
        help="[位置參數 1] CSV 檔案路徑",
    )
    parser.add_argument(
        "pos_uri",
        nargs="?",
        default=None,
        help="[位置參數 2] MongoDB 連線字串 (選填)",
    )
    parser.add_argument(
        "pos_db",
        nargs="?",
        default=None,
        help="[位置參數 3] MongoDB 資料庫名稱 (選填)",
    )

    # 支援具名選項 (旗標參數)
    parser.add_argument(
        "-f",
        "--file",
        dest="file",
        type=str,
        default=None,
        help=f"CSV 檔案路徑 (預設: {default_file})",
    )
    parser.add_argument(
        "-u",
        "--uri",
        dest="uri",
        type=str,
        default=None,
        help="MongoDB 連線字串 (預設讀取 backend/.env 或 mongodb://localhost:27017)",
    )
    parser.add_argument(
        "-d",
        "--db",
        dest="db",
        type=str,
        default=None,
        help="MongoDB 資料庫名稱 (預設讀取 backend/.env 或 bathroom_genius)",
    )
    parser.add_argument(
        "-c",
        "--collection",
        dest="collection",
        type=str,
        default="toilets",
        help="MongoDB Collection 名稱 (預設: toilets)",
    )
    parser.add_argument(
        "--clear",
        dest="clear",
        action="store_true",
        help="匯入前是否清空目標 Collection (預設不清除)",
    )
    parser.add_argument(
        "--batch-size",
        dest="batch_size",
        type=int,
        default=1000,
        help="批次寫入筆數 (預設: 1000)",
    )
    parser.add_argument(
        "--no-index",
        dest="no_index",
        action="store_true",
        help="不自動建立 2dsphere 索引",
    )

    args = parser.parse_args()

    # 決定最終參數優先級：旗標參數 > 位置參數 > .env / 預設值
    target_file = args.file or args.pos_file or default_file
    target_uri = args.uri or args.pos_uri or default_uri
    target_db = args.db or args.pos_db or default_db

    import_csv(
        file_path=target_file,
        mongodb_uri=target_uri,
        db_name=target_db,
        collection_name=args.collection,
        clear_collection=args.clear,
        batch_size=args.batch_size,
        create_index=not args.no_index,
    )


if __name__ == "__main__":
    main()

