#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Taipei Open Data (Data.Taipei) Toilet JSON Import Script for Bathroom Genius
從臺北市政府開放資料平台 (Data.Taipei) 公廁 JSON (taipei_datataipei.json) 匯入至 MongoDB 的工具
支援同地點合併、評等/友善設施標籤萃取、座標自動防呆校正
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

# 確保 Windows 主控台輸出繁體中文與表情符號不會拋出 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 嘗試載入 python-dotenv
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# 嘗試載入 pymongo
try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import PyMongoError
except ImportError:
    MongoClient = None
    UpdateOne = None
    PyMongoError = Exception


def load_environment_variables(env_path: Optional[Path] = None) -> None:
    """載入 .env 環境變數，若 python-dotenv 未安裝則提供簡易手動解析"""
    if env_path is None:
        env_path = Path(__file__).resolve().parent / ".env"

    if not env_path.exists():
        return

    if load_dotenv is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key not in os.environ:
                        os.environ[key] = val


def safe_int(val: Any, default: int = 0) -> int:
    """安全地將值轉換為整數"""
    if val is None:
        return default
    try:
        s = str(val).strip()
        return int(s) if s else default
    except (ValueError, TypeError):
        return default


def clean_dict_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    """移除字典 key 前後空白與多餘空白 (例如 '改善級 ')"""
    return {k.strip() if isinstance(k, str) else k: v for k, v in d.items()}


def extract_district(address: str, district_field: str = "") -> str:
    """從行政區欄位或地址中擷取鄉鎮市區名稱（例如：士林區、大安區）"""
    if district_field and district_field.strip():
        d = district_field.strip()
        if not d.endswith("區"):
            d += "區"
        return d
    if not address:
        return ""
    m = re.search(r'^(?:.+?[縣市])?([^縣市0-9]+?[區鄉鎮市])', address)
    return m.group(1).strip() if m else ""


def extract_floor(name: str) -> str:
    """提取樓層資訊，如 1F, 2F, B1, 3樓 -> 轉為標準 1F, 2F, B1 等"""
    if not name:
        return ""
    m = re.search(r'([B\d]+)[Ff樓層]', name)
    if m:
        floor_num = m.group(1).upper()
        if not floor_num.startswith('B'):
            return f"{floor_num}F"
        return floor_num
    return ""


def get_base_name(name: str) -> str:
    """移除結尾的男廁/女廁/樓層等後綴，取得主體名稱"""
    if not name:
        return ""
    cleaned = re.sub(
        r'(?:[-_]?(?:\d+[Ff樓層]?[-_]?)?(?:男廁(?:所)?|女廁(?:所)?|無障礙廁(?:所)?|混合廁(?:所)?|性別友善廁(?:所)?|親子廁(?:所)?))$',
        '',
        name
    ).strip()
    return cleaned if cleaned else name


def merge_taipei_toilet_group(
    address: str,
    base_name: str,
    items: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    將 Data.Taipei 同一地點 (address, base_name) 的紀錄合併為符合 MongoDB Schema 的單一 Document
    """
    if not base_name and not address:
        return None, "略過：缺少名稱與地址"

    # 1. 取得第一筆合法經緯度，並支援經緯度顛倒自動校正
    lng: Optional[float] = None
    lat: Optional[float] = None
    for raw_item in items:
        item = clean_dict_keys(raw_item)
        lat_str = str(item.get("緯度") or item.get("lat") or item.get("latitude") or "").strip()
        lng_str = str(item.get("經度") or item.get("lng") or item.get("longitude") or "").strip()
        if lat_str and lng_str:
            try:
                cur_lat = float(lat_str)
                cur_lng = float(lng_str)
                # 若經緯度顛倒 (台灣緯度約 21~26, 經度約 119~123) 則自動校正對調
                if cur_lat > 50.0 and cur_lng < 50.0:
                    cur_lat, cur_lng = cur_lng, cur_lat
                if -180.0 <= cur_lng <= 180.0 and -90.0 <= cur_lat <= 90.0:
                    lng = cur_lng
                    lat = cur_lat
                    break
            except ValueError:
                continue

    if lng is None or lat is None:
        return None, f"略過地點 '{base_name}' ({address})：無有效經緯度數值"

    # 2. 彙整各項屬性
    place_categories: List[str] = []
    districts: List[str] = []
    administrations: List[str] = []
    notes: List[str] = []
    other_facilities_list: List[str] = []
    bed_locations: List[str] = []
    basin_locations: List[str] = []
    general_locations: List[str] = []
    accessible_locations: List[str] = []

    total_accessible_seats = 0
    total_family_seats = 0
    special_grade_count = 0
    excellent_grade_count = 0
    normal_grade_count = 0
    improve_grade_count = 0

    for raw_item in items:
        item = clean_dict_keys(raw_item)

        # 行政區與類別
        dist = str(item.get("行政區") or item.get("district") or "").strip()
        if dist:
            districts.append(dist)
        cat = str(item.get("公廁類別") or item.get("type2") or item.get("category") or "").strip()
        if cat and cat != "其他":
            place_categories.append(cat)

        # 管理單位
        admin = str(item.get("管理單位") or item.get("administration") or "").strip()
        if admin:
            administrations.append(admin)

        # 座數與設施座數
        acc_seats = safe_int(item.get("無障礙廁座數") or item.get("無障礙廁所座數") or 0)
        fam_seats = safe_int(item.get("親子廁座數") or item.get("親子廁所座數") or 0)
        total_accessible_seats += acc_seats
        total_family_seats += fam_seats

        # 評等統計
        special_grade_count += safe_int(item.get("特優級") or 0)
        excellent_grade_count += safe_int(item.get("優等級") or 0)
        normal_grade_count += safe_int(item.get("普通級") or 0)
        improve_grade_count += safe_int(item.get("改善級") or item.get("加強級") or 0)

        # 其他設施與位置細節
        other_fac = str(item.get("其他設施") or "").strip()
        if other_fac:
            for f in re.split(r'[,，、;；]', other_fac):
                f_clean = f.strip()
                if f_clean:
                    other_facilities_list.append(f_clean)

        bed_loc = str(item.get("照護床位置") or "").strip()
        if bed_loc:
            bed_locations.append(bed_loc)

        basin_loc = str(item.get("污物盆位置") or "").strip()
        if basin_loc:
            basin_locations.append(basin_loc)

        gen_loc = str(item.get("一般時段位置") or "").strip()
        if gen_loc:
            general_locations.append(gen_loc)

        acc_loc = str(item.get("無障礙時段位置") or "").strip()
        if acc_loc:
            accessible_locations.append(acc_loc)

        raw_note = str(item.get("備註") or "").strip()
        if raw_note:
            notes.append(raw_note)

    # 3. 設施屬性判定
    has_accessible = total_accessible_seats > 0 or any("無障礙" in f for f in other_facilities_list)
    has_family = total_family_seats > 0 or any("親子" in f for f in other_facilities_list)

    # 4. 組合 Tags (場所類別 + 友善設施 + 其他設施 + 評等等級)
    tags: List[str] = []

    # (1) 場所分類
    if place_categories:
        tags.extend(list(dict.fromkeys(place_categories)))

    # (2) 友善設施標籤
    if has_accessible:
        tags.append("無障礙")
    if has_family:
        tags.append("親子")

    # (3) 其他特殊設施標籤 (如：照護床、污物盆)
    for fac in other_facilities_list:
        if fac not in ("無障礙", "親子") and fac not in tags:
            tags.append(fac)

    # (4) 評等標籤 (依有座數之評等加入)
    if special_grade_count > 0:
        tags.append("特優級")
    if excellent_grade_count > 0:
        tags.append("優等級")
    if normal_grade_count > 0:
        tags.append("普通級")
    if improve_grade_count > 0:
        tags.append("改善級")

    # 去重並維持原有順序
    tags = list(dict.fromkeys(tags))

    # 5. 地標 (landmark)
    district = extract_district(address, districts[0] if districts else "")
    main_place_type = place_categories[0] if place_categories else ""
    if district and main_place_type:
        landmark = f"{district}{main_place_type}"
    elif district:
        landmark = district
    elif main_place_type:
        landmark = main_place_type
    else:
        landmark = None

    # 6. 備註 (note: 整合管理單位、照護床/污物盆位置、一般/無障礙時段與備註)
    note_parts = []
    unique_admins = list(dict.fromkeys(administrations))
    if unique_admins:
        note_parts.append(f"管理單位: {', '.join(unique_admins)}")

    unique_beds = list(dict.fromkeys(bed_locations))
    if unique_beds:
        note_parts.append(f"照護床位置: {', '.join(unique_beds)}")

    unique_basins = list(dict.fromkeys(basin_locations))
    if unique_basins:
        note_parts.append(f"污物盆位置: {', '.join(unique_basins)}")

    unique_gens = list(dict.fromkeys(general_locations))
    if unique_gens:
        note_parts.append(f"一般時段位置: {', '.join(unique_gens)}")

    unique_accs = list(dict.fromkeys(accessible_locations))
    if unique_accs:
        note_parts.append(f"無障礙時段位置: {', '.join(unique_accs)}")

    unique_notes = list(dict.fromkeys(notes))
    if unique_notes:
        note_parts.append(f"備註: {', '.join(unique_notes)}")

    note = " / ".join(note_parts) if note_parts else None

    # 7. 時間戳記
    now = datetime.now(timezone.utc)

    # 組合 Document
    doc: Dict[str, Any] = {
        "name": base_name,
        "location": {
            "type": "Point",
            "coordinates": [lng, lat],  # GeoJSON 規範: [經度, 緯度]
        },
        "address": address,
        "hasToiletPaper": None,
        "isAccessible": has_accessible,
        "tags": tags,
        "avgCleanScore": 0.0,
        "avgConvenienceScore": 0.0,
        "reviewCount": 0,
        "createdAt": now,
        "updatedAt": now,
        "landmark": landmark,
        "note": note,
    }

    return doc, None


def load_taipei_json_records(json_path: Path) -> List[Dict[str, Any]]:
    """解析 Data.Taipei JSON 檔案並擷取公廁紀錄列表"""
    with open(json_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    # 支援 {"result": {"results": [...]}} 或 {"results": [...]} 或 [...]
    if isinstance(raw_data, dict):
        if "result" in raw_data and isinstance(raw_data["result"], dict) and "results" in raw_data["result"]:
            return raw_data["result"]["results"]
        elif "results" in raw_data and isinstance(raw_data["results"], list):
            return raw_data["results"]
        elif "data" in raw_data and isinstance(raw_data["data"], list):
            return raw_data["data"]
        else:
            raise ValueError(f"無法從 JSON 物件中找到公廁資料列表，已知鍵: {list(raw_data.keys())}")
    elif isinstance(raw_data, list):
        return raw_data
    else:
        raise ValueError(f"未知的 JSON 格式型態: {type(raw_data)}")


def run_import(
    file_path: Optional[str] = None,
    env_file: Optional[str] = None,
    uri: Optional[str] = None,
    db: Optional[str] = None,
    collection: str = "toilets",
    drop: bool = False,
    dry_run: bool = False,
    batch_size: int = 500
) -> Dict[str, Any]:
    """執行臺北市 Data.Taipei JSON 匯入程序，並回傳統計字典"""
    # 1. 載入環境變數
    env_path = Path(env_file).resolve() if env_file else Path(__file__).resolve().parent / ".env"
    load_environment_variables(env_path)

    # 2. 決定 MongoDB 連線參數
    mongo_uri = uri or os.getenv("MONGODB_URI")
    mongo_db_name = db or os.getenv("MONGODB_DB_NAME", "bathroom_online")

    print("=" * 68)
    print("🚽 Bathroom Genius - 臺北市 Data.Taipei 公廁 JSON 匯入工具")
    print("=" * 68)

    # 3. 讀取 JSON 檔案
    target_file = file_path or "data/taipei_datataipei.json"
    json_path = Path(target_file)
    if not json_path.is_absolute():
        json_path = Path(__file__).resolve().parent / json_path

    if not json_path.exists():
        print(f"❌ 找不到 JSON 檔案：{json_path}")
        return {"raw_records": 0, "valid_docs": 0, "upserted": 0, "modified": 0, "matched": 0, "db_total": 0}

    print(f"📂 正在讀取檔案：{json_path}")
    try:
        raw_records = load_taipei_json_records(json_path)
    except Exception as e:
        print(f"❌ 解析 JSON 檔案失敗：{e}")
        return {"raw_records": 0, "valid_docs": 0, "upserted": 0, "modified": 0, "matched": 0, "db_total": 0}

    total_records = len(raw_records)
    print(f"📊 讀取完成，共 {total_records} 筆原始公廁資料")

    # 4. 依地點與主體名稱分組
    grouped_data = defaultdict(list)
    for raw_item in raw_records:
        item = clean_dict_keys(raw_item)
        addr = str(item.get("公廁地址") or item.get("address") or "").strip()
        name = str(item.get("公廁名稱") or item.get("name") or "").strip()
        bname = get_base_name(name)
        grouped_data[(addr, bname)].append(item)

    merged_locations_count = len(grouped_data)
    multi_toilet_locations = sum(1 for v in grouped_data.values() if len(v) > 1)
    print(f"🏢 辨識出 {merged_locations_count} 個獨立公廁地點（其中 {multi_toilet_locations} 處包含多筆合併紀錄）")

    # 5. 轉換與校驗
    valid_docs: List[Dict[str, Any]] = []
    skipped_count = 0

    for (addr, bname), items in grouped_data.items():
        doc, err = merge_taipei_toilet_group(addr, bname, items)
        if err:
            skipped_count += 1
            if skipped_count <= 5:
                print(f"  ⚠️  {err}")
        else:
            valid_docs.append(doc)

    print(f"✅ 資料合併完成：成功產出 {len(valid_docs)} 筆公廁 Document / 略過 {skipped_count} 筆")

    # 若為 Dry-Run 模式則印出示範並結束
    if dry_run:
        print("\n🔍 [Dry-Run 模式] 不會寫入 MongoDB。以下為具代表性的轉換與合併範例：")
        sample_count = min(3, len(valid_docs))
        for i in range(sample_count):
            sample = valid_docs[i]
            sample_copy = {**sample}
            if isinstance(sample_copy.get("createdAt"), datetime):
                sample_copy["createdAt"] = sample_copy["createdAt"].isoformat()
            if isinstance(sample_copy.get("updatedAt"), datetime):
                sample_copy["updatedAt"] = sample_copy["updatedAt"].isoformat()
            print(f"\n--- 範例 #{i + 1} ({sample_copy['name']}) ---")
            print(json.dumps(sample_copy, ensure_ascii=False, indent=2))
        print("\n✨ Dry-Run 檢驗完畢！")
        return {"raw_records": total_records, "valid_docs": len(valid_docs), "upserted": 0, "modified": 0, "matched": 0, "db_total": 0}

    # 6. 連線至 MongoDB 並寫入
    if MongoClient is None:
        raise ImportError("缺少 pymongo 套件，請先執行: pip install pymongo dnspython python-dotenv")

    if not mongo_uri:
        raise ValueError("未提供 MONGODB_URI，請檢查 .env 檔案或使用 uri 參數傳入")

    print(f"\n🔌 正在連線至 MongoDB 資料庫：{mongo_db_name} (Collection: {collection}) ...")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    db_obj = client[mongo_db_name]
    col_obj = db_obj[collection]
    print(" Connected to MongoDB successfully!")

    # 若指定 drop 則清空集合
    if drop:
        print(f"🧹 正在清空集合 '{collection}' ...")
        col_obj.drop()
        print("✨ 集合已清空")

    # 建立 2dsphere 空間索引
    print("📍 正在確認/建立 2dsphere 空間索引 (location) ...")
    try:
        col_obj.create_index([("location", "2dsphere")])
        print("✅ 2dsphere 索引已建立/確認完畢")
    except Exception as e:
        print(f"⚠️ 建立 2dsphere 索引時發生警告：{e}")

    # 7. 批次 Upsert 寫入
    print(f"\n🚀 開始批次匯入 {len(valid_docs)} 筆公廁資料至 MongoDB (每批 {batch_size} 筆) ...")
    
    total_upserted = 0
    total_modified = 0
    total_matched = 0

    batch_operations = []
    for i, doc in enumerate(valid_docs):
        filter_query = {
            "name": doc["name"],
            "address": doc["address"],
        }
        update_doc = {
            "$set": {
                "location": doc["location"],
                "hasToiletPaper": doc["hasToiletPaper"],
                "isAccessible": doc["isAccessible"],
                "tags": doc["tags"],
                "landmark": doc["landmark"],
                "note": doc["note"],
                "updatedAt": doc["updatedAt"],
            },
            "$setOnInsert": {
                "name": doc["name"],
                "address": doc["address"],
                "avgCleanScore": doc["avgCleanScore"],
                "avgConvenienceScore": doc["avgConvenienceScore"],
                "reviewCount": doc["reviewCount"],
                "createdAt": doc["createdAt"],
            }
        }
        batch_operations.append(UpdateOne(filter_query, update_doc, upsert=True))

        if len(batch_operations) >= batch_size:
            result = col_obj.bulk_write(batch_operations, ordered=False)
            total_upserted += len(result.upserted_ids)
            total_modified += result.modified_count
            total_matched += result.matched_count
            batch_operations.clear()
            print(f"   已處理 {i + 1}/{len(valid_docs)} 筆...")

    if batch_operations:
        result = col_obj.bulk_write(batch_operations, ordered=False)
        total_upserted += len(result.upserted_ids)
        total_modified += result.modified_count
        total_matched += result.matched_count
        batch_operations.clear()

    current_db_total = col_obj.count_documents({})
    client.close()

    print("\n" + "=" * 68)
    print("🎉 匯入完成！統計報告：")
    print(f"  • 原始資料筆數       ：{total_records}")
    print(f"  • 合併後獨立公廁總數 ：{len(valid_docs)}")
    print(f"  • 新增筆數 (Upserted)：{total_upserted}")
    print(f"  • 更新筆數 (Modified)：{total_modified}")
    print(f"  • 比對相符 (Matched) ：{total_matched}")
    print(f"  • 目前資料庫總筆數   ：{current_db_total}")
    print("=" * 68)

    return {
        "raw_records": total_records,
        "valid_docs": len(valid_docs),
        "upserted": total_upserted,
        "modified": total_modified,
        "matched": total_matched,
        "db_total": current_db_total
    }


def main():
    parser = argparse.ArgumentParser(
        description="Bathroom Genius - 臺北市 (Data.Taipei) 公廁 JSON 資料匯入工具",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--file",
        "-f",
        default="data/taipei_datataipei.json",
        help="Data.Taipei 公廁 JSON 檔案路徑 (預設: data/taipei_datataipei.json)",
    )
    parser.add_argument(
        "--env-file",
        "-e",
        default=None,
        help=".env 檔案路徑 (預設自動偵測專案目錄 .env)",
    )
    parser.add_argument(
        "--uri",
        "-u",
        default=None,
        help="MongoDB 連線 URI (優先於 .env 設定)",
    )
    parser.add_argument(
        "--db",
        "-d",
        default=None,
        help="MongoDB 資料庫名稱 (優先於 .env 中的 MONGODB_DB_NAME)",
    )
    parser.add_argument(
        "--collection",
        "-c",
        default="toilets",
        help="目標 Collection 名稱 (預設: toilets)",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="匯入前清空目標 Collection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="僅執行解析、分組、合併與校驗，不實際連線與寫入 MongoDB",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="批次寫入筆數 (預設: 500)",
    )

    args = parser.parse_args()

    run_import(
        file_path=args.file,
        env_file=args.env_file,
        uri=args.uri,
        db=args.db,
        collection=args.collection,
        drop=args.drop,
        dry_run=args.dry_run,
        batch_size=args.batch_size
    )


if __name__ == "__main__":
    main()
