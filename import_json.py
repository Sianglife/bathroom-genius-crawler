#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Toilet JSON Import Script for Bathroom Genius
從環境部公廁開放資料 JSON (bathroom_taiwan_moenv.json) 匯入至 MongoDB 的工具
支援同地點合併、樓層別性別標籤判定 (如 1F僅男廁、2F男女廁、混合性別等)
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


def extract_district(address: str) -> str:
    """從地址中擷取鄉鎮市區名稱（例如：大安區、鶯歌區、礁溪鄉）"""
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
    # 移除結尾的廁所標記 (如: -男廁, 1F-女廁, 2F-混合廁所, 男廁所, 男廁, -無障礙廁所, 無障礙廁)
    cleaned = re.sub(
        r'(?:[-_]?(?:\d+[Ff樓層]?[-_]?)?(?:男廁(?:所)?|女廁(?:所)?|無障礙廁(?:所)?|混合廁(?:所)?|性別友善廁(?:所)?|親子廁(?:所)?))$',
        '',
        name
    ).strip()
    return cleaned if cleaned else name


def generate_gender_tags_by_floor(items: List[Dict[str, Any]]) -> List[str]:
    """
    依樓層（若有）分組並產出性別與使用別標籤
    例如：['1F男女廁', '2F僅男廁', '3F僅女廁'] 或未分樓層時的 ['男女廁']
    """
    floor_items = defaultdict(list)
    for it in items:
        floor = extract_floor(str(it.get("name", "")))
        floor_items[floor].append(it)

    tags: List[str] = []

    # 排序樓層：無樓層優先，接著地下樓層 B2, B1，地上 1F, 2F, 3F...
    def sort_key(fl: str):
        if not fl:
            return (0, 0)
        if fl.startswith('B'):
            num_part = fl[1:]
            return (1, -int(num_part) if num_part.isdigit() else 0)
        num = re.findall(r'\d+', fl)
        return (2, int(num[0]) if num else 0)

    for floor in sorted(floor_items.keys(), key=sort_key):
        sub_items = floor_items[floor]
        types = [str(x.get("type", "")).strip() for x in sub_items]
        names = [str(x.get("name", "")).strip() for x in sub_items]

        has_male = any("男" in t and "無障礙" not in t and "性別友善" not in t and "混合" not in t for t in types)
        has_female = any("女" in t and "無障礙" not in t and "性別友善" not in t and "混合" not in t for t in types)
        has_unisex = any("混合" in t or "混合" in n for t, n in zip(types, names))
        has_gender_friendly = any("性別友善" in t or "性別友善" in n for t, n in zip(types, names))

        prefix = floor  # 例如 "1F" 或 ""

        if has_male and has_female:
            tags.append(f"{prefix}男女廁")
        elif has_male and not has_female and not has_unisex and not has_gender_friendly:
            tags.append(f"{prefix}僅男廁")
        elif has_female and not has_male and not has_unisex and not has_gender_friendly:
            tags.append(f"{prefix}僅女廁")

        if has_unisex:
            tags.append(f"{prefix}混合性別")
        if has_gender_friendly:
            tags.append(f"{prefix}性別友善")

    return tags


def merge_toilet_group(
    address: str,
    base_name: str,
    items: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    將同一地點 (address, base_name) 的多筆公廁紀錄合併為符合 MongoDB Schema 的單一 Document
    """
    if not base_name and not address:
        return None, "略過：缺少名稱與地址"

    # 取得第一筆合法經緯度
    lng: Optional[float] = None
    lat: Optional[float] = None
    for item in items:
        lat_str = str(item.get("latitude", "")).strip()
        lng_str = str(item.get("longitude", "")).strip()
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

    # 蒐集所有子項目的類型與屬性
    types = [str(x.get("type", "")).strip() for x in items]
    names = [str(x.get("name", "")).strip() for x in items]
    grades = [str(x.get("grade", "")).strip() for x in items if x.get("grade")]
    place_types = [str(x.get("type2", "")).strip() for x in items if x.get("type2") and x.get("type2") != "其他"]
    diapers = [str(x.get("diaper", "0")).strip() for x in items]

    # 1. 設施屬性判定
    has_accessible = any("無障礙" in t or "無障礙" in n for t, n in zip(types, names))
    has_family = any(
        d not in ("0", "", "無", "none", "None", "null") or "親子" in t or "親子" in n
        for d, t, n in zip(diapers, types, names)
    )

    # 2. 組合 tags (排除行政區)
    tags: List[str] = []

    # (1) 場所分類
    if place_types:
        tags.extend(list(dict.fromkeys(place_types)))

    # (2) 樓層別性別標籤 (例如：1F男女廁、2F僅男廁、男女廁、混合性別等)
    gender_floor_tags = generate_gender_tags_by_floor(items)
    tags.extend(gender_floor_tags)

    # (3) 友善設施標籤
    if has_accessible:
        tags.append("無障礙")
    if has_family:
        tags.append("親子")

    # (4) 評等等級
    if grades:
        tags.extend(list(dict.fromkeys(grades)))

    # 去重並保持順序
    tags = list(dict.fromkeys(tags))

    # 3. 地標 (landmark)
    district = extract_district(address)
    main_place_type = place_types[0] if place_types else ""
    if district and main_place_type:
        landmark = f"{district}{main_place_type}"
    elif district:
        landmark = district
    elif main_place_type:
        landmark = main_place_type
    else:
        landmark = None

    # 4. 備註 (note: 整合管理單位與維護單位)
    admins = list(dict.fromkeys(str(x.get("administration", "")).strip() for x in items if x.get("administration")))
    execs = list(dict.fromkeys(str(x.get("exec", "")).strip() for x in items if x.get("exec")))
    note_parts = []
    if admins:
        note_parts.append(f"管理單位: {', '.join(admins)}")
    exec_diff = [e for e in execs if e not in admins]
    if exec_diff:
        note_parts.append(f"維護單位: {', '.join(exec_diff)}")
    note = " / ".join(note_parts) if note_parts else None

    # 5. 時間戳記
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


def main():
    parser = argparse.ArgumentParser(
        description="匯入公廁 JSON 資料至 MongoDB (Bathroom Genius - 支援同地點與樓層性別合併)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--file",
        "-f",
        default="data/bathroom_taiwan_moenv.json",
        help="JSON 資料檔案路徑 (預設: data/bathroom_taiwan_moenv.json)",
    )
    parser.add_argument(
        "--env-file",
        "-e",
        default=None,
        help=".env 檔案路徑 (預設自動偵測專案根目錄 .env)",
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

    # 1. 載入環境變數
    env_path = Path(args.env_file).resolve() if args.env_file else Path(__file__).resolve().parent / ".env"
    load_environment_variables(env_path)

    # 2. 決定 MongoDB 連線參數
    mongo_uri = args.uri or os.getenv("MONGODB_URI")
    mongo_db_name = args.db or os.getenv("MONGODB_DB_NAME", "bathroom_online")

    print("=" * 68)
    print("🚽 Bathroom Genius - 公廁 JSON 匯入工具 (同地點合併 & 樓層性別標籤)")
    print("=" * 68)

    # 3. 讀取 JSON 檔案
    json_path = Path(args.file)
    if not json_path.is_absolute():
        json_path = Path(__file__).resolve().parent / json_path

    if not json_path.exists():
        print(f"❌ 找不到 JSON 檔案：{json_path}")
        sys.exit(1)

    print(f"📂 正在讀取檔案：{json_path}")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception as e:
        print(f"❌ 解析 JSON 檔案失敗：{e}")
        sys.exit(1)

    if not isinstance(raw_data, list):
        print("❌ JSON 根節點格式錯誤，必須為 Array/List")
        sys.exit(1)

    total_records = len(raw_data)
    print(f"📊 讀取完成，共 {total_records} 筆原始公廁資料")

    # 4. 依地點與主體名稱分組
    grouped_data = defaultdict(list)
    for item in raw_data:
        addr = str(item.get("address", "")).strip()
        name = str(item.get("name", "")).strip()
        bname = get_base_name(name)
        grouped_data[(addr, bname)].append(item)

    merged_locations_count = len(grouped_data)
    multi_toilet_locations = sum(1 for v in grouped_data.values() if len(v) > 1)
    print(f"🏢 辨識出 {merged_locations_count} 個獨立公廁地點（其中 {multi_toilet_locations} 處包含多座/多樓層分開紀錄並已合併）")

    # 5. 轉換與校驗
    valid_docs: List[Dict[str, Any]] = []
    skipped_count = 0

    for (addr, bname), items in grouped_data.items():
        doc, err = merge_toilet_group(addr, bname, items)
        if err:
            skipped_count += 1
            if skipped_count <= 5:
                print(f"  ⚠️  {err}")
        else:
            valid_docs.append(doc)

    print(f"✅ 資料合併完成：成功產出 {len(valid_docs)} 筆公廁 Document / 略過 {skipped_count} 筆")

    # 若為 Dry-Run 模式則印出示範並結束
    if args.dry_run:
        print("\n🔍 [Dry-Run 模式] 不會寫入 MongoDB。以下為具代表性的轉換與合併範例：")
        
        # 尋找包含多樓層、單樓層、無樓層的範例
        multi_floor_sample = next((d for d in valid_docs if any(re.match(r'^\d+F', t) for t in d['tags']) and len([t for t in d['tags'] if 'F' in t]) > 1), valid_docs[0])
        single_floor_sample = next((d for d in valid_docs if any(re.match(r'^\d+F', t) for t in d['tags']) and len([t for t in d['tags'] if 'F' in t]) == 1), valid_docs[1])
        no_floor_sample = next((d for d in valid_docs if not any('F' in t for t in d['tags'])), valid_docs[2])

        for i, sample in enumerate([multi_floor_sample, single_floor_sample, no_floor_sample]):
            sample_copy = {**sample}
            sample_copy["createdAt"] = sample_copy["createdAt"].isoformat()
            sample_copy["updatedAt"] = sample_copy["updatedAt"].isoformat()
            print(f"\n--- 範例 #{i + 1} ({sample_copy['name']}) ---")
            print(json.dumps(sample_copy, ensure_ascii=False, indent=2))
        print("\n✨ Dry-Run 檢驗完畢！")
        return

    # 6. 連線至 MongoDB 並寫入
    if MongoClient is None:
        print("❌ 缺少 pymongo 套件，請先執行: pip install pymongo dnspython python-dotenv")
        sys.exit(1)

    if not mongo_uri:
        print("❌ 未提供 MONGODB_URI，請檢查 .env 檔案或使用 --uri 參數傳入")
        sys.exit(1)

    print(f"\n🔌 正在連線至 MongoDB 資料庫：{mongo_db_name} (Collection: {args.collection}) ...")
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
        # 測試連線
        client.admin.command("ping")
        db = client[mongo_db_name]
        collection = db[args.collection]
        print(" Connected to MongoDB successfully!")
    except Exception as e:
        print(f"❌ MongoDB 連線失敗：{e}")
        sys.exit(1)

    # 若指定 --drop 則清空集合
    if args.drop:
        print(f"🧹 正在清空集合 '{args.collection}' ...")
        collection.drop()
        print("✨ 集合已清空")

    # 建立 2dsphere 空間索引
    print("📍 正在確認/建立 2dsphere 空間索引 (location) ...")
    try:
        collection.create_index([("location", "2dsphere")])
        print("✅ 2dsphere 索引已建立/確認完畢")
    except Exception as e:
        print(f"⚠️ 建立 2dsphere 索引時發生警告：{e}")

    # 7. 批次 Upsert 寫入
    print(f"\n🚀 開始批次匯入 {len(valid_docs)} 筆公廁資料至 MongoDB (每批 {args.batch_size} 筆) ...")
    
    total_upserted = 0
    total_modified = 0
    total_matched = 0

    batch_operations = []
    for i, doc in enumerate(valid_docs):
        # 以 (name, address) 作為唯一鍵進行 Upsert
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

        if len(batch_operations) >= args.batch_size:
            result = collection.bulk_write(batch_operations, ordered=False)
            total_upserted += len(result.upserted_ids)
            total_modified += result.modified_count
            total_matched += result.matched_count
            batch_operations.clear()
            print(f"   已處理 {i + 1}/{len(valid_docs)} 筆...")

    if batch_operations:
        result = collection.bulk_write(batch_operations, ordered=False)
        total_upserted += len(result.upserted_ids)
        total_modified += result.modified_count
        total_matched += result.matched_count
        batch_operations.clear()

    print("\n" + "=" * 68)
    print("🎉 匯入完成！統計報告：")
    print(f"  • 原始資料筆數       ：{total_records}")
    print(f"  • 合併後獨立公廁總數 ：{len(valid_docs)}")
    print(f"  • 新增筆數 (Upserted)：{total_upserted}")
    print(f"  • 更新筆數 (Modified)：{total_modified}")
    print(f"  • 比對相符 (Matched) ：{total_matched}")
    print(f"  • 目前資料庫總筆數   ：{collection.count_documents({})}")
    print("=" * 68)


if __name__ == "__main__":
    main()
