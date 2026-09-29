#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Toilet Data Cleaner & Gender Normalizer for Bathroom Genius
公廁資料清洗與性別合併工具

功能說明：
1. 處理地址相同、名稱相同（僅差男、女或男廁/女廁等標記）的重複項目，將其合併為單一地點紀錄。
2. 根據合併結果自動賦予對應的性別標籤：
   - 同時具備男廁與女廁（或合併後涵蓋男女） -> '男女廁'
   - 僅有男廁 -> '僅男廁'
   - 僅有女廁 -> '僅女廁'
3. 針對只有男或女的單筆資料，同樣去除名稱中的性別詞綴，讓 name 欄位保留乾淨主體名稱，並標註 '僅男廁' 或 '僅女廁'。
4. 支援直接清洗與更新 MongoDB 資料庫中已存在的公廁資料 (--clean-db)。
5. 支援從本機 JSON 檔案清洗並輸出或匯入至 MongoDB。
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
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
    from pymongo import MongoClient, UpdateOne, DeleteMany
    from pymongo.errors import PyMongoError
except ImportError:
    MongoClient = None
    UpdateOne = None
    DeleteMany = None
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


def normalize_and_extract_gender(name: str, type_str: str = "") -> Tuple[str, Set[str]]:
    """
    從公廁名稱中萃取性別資訊，並去除男/女/男廁/女廁等標記，產出乾淨的主體名稱 (Base Name)。

    支援模式：
    - 括號性別：(男), (女), (男女), （男）, （女）, （男女）, (男廁), (女廁), (男女廁)
    - 複合括號：(前站男廁) -> (前站), (後站女廁) -> (後站)
    - 結尾括號前性別：-女廁(含無障礙) -> (含無障礙), 女廁(中油加盟) -> (中油加盟)
    - 結尾後綴：健康國小地下停車場男 -> 健康國小地下停車場, 府前廣場1號男廁 -> 府前廣場1號
    - 中間嵌入詞：女廁一 -> 一、1F女廁二 -> 1F二、特力家居南崁店女廁B1 -> 特力家居南崁店B1、1樓男長安西 -> 1樓長安西
    - 輔助型態：type 欄位包含 男廁所 / 女廁所 / 混合廁所 等
    """
    if not name:
        return "", set()

    original = name.strip()
    detected_genders: Set[str] = set()
    cleaned = original

    # 0. 輔助 type 欄位檢測
    if type_str:
        if "男女" in type_str or "混合" in type_str:
            detected_genders.add("男女")
        elif "男" in type_str and "無障礙" not in type_str and "性別友善" not in type_str:
            detected_genders.add("男")
        elif "女" in type_str and "無障礙" not in type_str and "性別友善" not in type_str:
            detected_genders.add("女")

    # 1. 括號內的純性別標記
    def sub_paren_gender(m: re.Match) -> str:
        content = m.group(1).strip()
        if "男女" in content or ("男" in content and "女" in content):
            detected_genders.add("男女")
        elif "男" in content:
            detected_genders.add("男")
        elif "女" in content:
            detected_genders.add("女")
        return ""

    # 處理純性別括號
    cleaned = re.sub(
        r'[\(（]\s*(男(?:廁(?:所)?)?|女(?:廁(?:所)?)?|男女(?:廁(?:所)?)?)\s*[\)）]',
        sub_paren_gender,
        cleaned
    )

    # 處理含有其他字樣的括號，如 (前站男廁) -> (前站)
    def paren_inner_sub(m: re.Match) -> str:
        full = m.group(0)
        inner = m.group(1)
        if any(w in inner for w in ["男廁", "女廁", "男女廁", "男女", "男", "女"]):
            if "男女" in inner or ("男" in inner and "女" in inner):
                detected_genders.add("男女")
            elif "男" in inner:
                detected_genders.add("男")
            elif "女" in inner:
                detected_genders.add("女")
            new_inner = re.sub(r'[-_]?(?:男廁(?:所)?|女廁(?:所)?|男女廁(?:所)?|男女|[男女])', '', inner).strip()
            if not new_inner:
                return ""
            return f"({new_inner})"
        return full

    cleaned = re.sub(r'[\(（]([^\)）]+)[\)）]', paren_inner_sub, cleaned)

    # 2. 處理在結尾括號之前的性別標記，如: -女廁(含無障礙) -> (含無障礙), 女廁(中油加盟) -> (中油加盟)
    def sub_pre_paren_gender(m: re.Match) -> str:
        g = m.group(1)
        suffix_paren = m.group(2) or ""
        if "男女" in g or ("男" in g and "女" in g):
            detected_genders.add("男女")
        elif "男" in g:
            detected_genders.add("男")
        elif "女" in g:
            detected_genders.add("女")
        return suffix_paren.strip()

    cleaned = re.sub(
        r'[-_]?(男女廁(?:所)?|男廁(?:所)?|女廁(?:所)?|男女|[男女])(\s*[\(（][^\)）]+[\)）])$',
        sub_pre_paren_gender,
        cleaned
    )

    # 3. 結尾性別標記 (如: -男廁, -女廁, 1號男, 男, 女, 男廁所, 女廁所, 男女廁, 男女...)
    m_end = re.search(r'[-_]?(男女廁(?:所)?|男廁(?:所)?|女廁(?:所)?|男女|[男女])$', cleaned)
    if m_end:
        matched = m_end.group(1)
        if "男女" in matched or ("男" in matched and "女" in matched):
            detected_genders.add("男女")
        elif "男" in matched:
            detected_genders.add("男")
        elif "女" in matched:
            detected_genders.add("女")
        cleaned = cleaned[:m_end.start()].strip()

    # 4. 中間嵌入的性別詞（如：女廁一 -> 一、男廁一 -> 一、1F女廁二 -> 1F二、女一廁 -> 一廁、女廁B1 -> B1、1樓男長安西 -> 1樓長安西）
    def sub_mid_gender(m: re.Match) -> str:
        matched = m.group(0)
        if "男女" in matched or ("男" in matched and "女" in matched):
            detected_genders.add("男女")
        elif "男" in matched:
            detected_genders.add("男")
        elif "女" in matched:
            detected_genders.add("女")
        return ""

    cleaned = re.sub(
        r'[-_]?(?:男女廁(?:所)?|男廁(?:所)?|女廁(?:所)?)(?=[一二三四五六七八九十\dBFbf]|旁|外|$)',
        sub_mid_gender,
        cleaned
    )
    cleaned = re.sub(
        r'(?<=[0-9Ff樓層棟])[-_]?(?:男|女)(?=[長安西|無障礙|外|旁|研討|\d一二三四五六七八九十])',
        sub_mid_gender,
        cleaned
    )

    # 清理多餘的連字號、空白與空括號
    cleaned = re.sub(r'[-_]+$', '', cleaned).strip()
    cleaned = re.sub(r'^[_-]+', '', cleaned).strip()
    cleaned = re.sub(r'\(\s*\)|（\s*）', '', cleaned).strip()

    final_name = cleaned if cleaned else original
    return final_name, detected_genders


def parse_coordinates(items: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """從紀錄群組中取得第一組有效經緯度，並支援經緯度顛倒自動校正"""
    for item in items:
        lat_val = item.get("latitude") or item.get("lat") or item.get("緯度") or ""
        lng_val = item.get("longitude") or item.get("lng") or item.get("經度") or ""

        # 若是 GeoJSON Point 結構
        if isinstance(item.get("location"), dict) and "coordinates" in item["location"]:
            coords = item["location"]["coordinates"]
            if len(coords) >= 2:
                lng_val, lat_val = coords[0], coords[1]

        if lat_val and lng_val:
            try:
                cur_lat = float(str(lat_val).strip())
                cur_lng = float(str(lng_val).strip())
                # 若經緯度顛倒 (台灣緯度約 21~26, 經度約 119~123) 則自動校正對調
                if cur_lat > 50.0 and cur_lng < 50.0:
                    cur_lat, cur_lng = cur_lng, cur_lat
                if -180.0 <= cur_lng <= 180.0 and -90.0 <= cur_lat <= 90.0:
                    return cur_lng, cur_lat
            except (ValueError, TypeError):
                continue
    return None, None


def clean_and_merge_records(
    records: List[Dict[str, Any]],
    output_format: str = "standard"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    清洗並合併公廁紀錄列表：
    - 將相同地址 (address) 與相同主體名稱 (base_name) 的紀錄合為一體。
    - 只留下名稱在 name 欄位（去除性別詞綴）。
    - 根據性別覆蓋情況加入 tags ('男女廁', '僅男廁', '僅女廁')。
    - 支援 standard (標準 MongoDB Document) 或 raw (精簡原始 JSON) 格式。
    """
    # 依 (address, base_name) 分組
    grouped = defaultdict(list)
    for r in records:
        addr = str(r.get("address") or r.get("公廁地址") or "").strip()
        raw_name = str(r.get("name") or r.get("公廁名稱") or "").strip()
        type_str = str(r.get("type") or r.get("公廁類別") or "").strip()
        
        # 若已有 tags 輔助判斷
        raw_tags = r.get("tags") or []
        for t in raw_tags:
            if "男" in str(t) or "女" in str(t):
                type_str += " " + str(t)

        base_name, genders = normalize_and_extract_gender(raw_name, type_str)
        grouped[(addr, base_name)].append({
            "raw": r,
            "base_name": base_name,
            "genders": genders,
        })

    cleaned_records: List[Dict[str, Any]] = []
    stats = {
        "total_input": len(records),
        "total_output": 0,
        "merged_multi_items_groups": 0,
        "single_item_groups": 0,
        "both_gender_count": 0,
        "male_only_count": 0,
        "female_only_count": 0,
        "no_gender_count": 0,
        "skipped_no_coords": 0,
    }

    now = datetime.now(timezone.utc)

    for (addr, base_name), items in grouped.items():
        if not base_name and not addr:
            continue

        # 彙整群組內所有檢測到的性別
        all_genders: Set[str] = set()
        for it in items:
            all_genders.update(it["genders"])

        # 性別標籤判定
        gender_tag: Optional[str] = None
        if "男女" in all_genders or ("男" in all_genders and "女" in all_genders):
            gender_tag = "男女廁"
            stats["both_gender_count"] += 1
        elif "男" in all_genders:
            gender_tag = "僅男廁"
            stats["male_only_count"] += 1
        elif "女" in all_genders:
            gender_tag = "僅女廁"
            stats["female_only_count"] += 1
        else:
            stats["no_gender_count"] += 1

        if len(items) > 1:
            stats["merged_multi_items_groups"] += 1
        else:
            stats["single_item_groups"] += 1

        first_raw = items[0]["raw"]

        # 解析經緯度
        lng, lat = parse_coordinates([it["raw"] for it in items])
        if output_format == "standard" and (lng is None or lat is None):
            stats["skipped_no_coords"] += 1
            continue

        # 彙整 tags (清理舊的性別標籤)
        tags: List[str] = []

        # 1. 保留原本已存在的非性別 tags
        for it in items:
            raw_t = it["raw"].get("tags")
            if isinstance(raw_t, list):
                for t in raw_t:
                    t_str = str(t).strip()
                    if re.search(r'^\d*[Ff樓層]?(?:僅男廁|僅女廁|男女廁|混合性別)$', t_str):
                        continue
                    if t_str and t_str not in tags:
                        tags.append(t_str)

        # 2. 加入正規化後的性別標籤 (男女廁 / 僅男廁 / 僅女廁)
        if gender_tag:
            tags.insert(1 if len(tags) > 0 else 0, gender_tag)

        # 3. 彙整場所類別 (type2)、友善設施 (無障礙 / 親子) 與評等
        place_types: List[str] = []
        has_accessible = False
        has_family = False
        grades: List[str] = []
        admins: List[str] = []
        execs: List[str] = []

        for it in items:
            raw_doc = it["raw"]
            t2 = str(raw_doc.get("type2") or raw_doc.get("category") or raw_doc.get("公廁類別") or "").strip()
            if t2 and t2 != "其他":
                place_types.append(t2)

            tp = str(raw_doc.get("type") or "").strip()
            nm = str(raw_doc.get("name") or "").strip()
            diaper = str(raw_doc.get("diaper", "0")).strip()
            grade = str(raw_doc.get("grade") or "").strip()
            admin = str(raw_doc.get("administration") or raw_doc.get("管理單位") or "").strip()
            exec_unit = str(raw_doc.get("exec") or "").strip()

            if "無障礙" in tp or "無障礙" in nm or raw_doc.get("isAccessible") is True or "無障礙" in (raw_doc.get("tags") or []):
                has_accessible = True
            if diaper not in ("0", "", "無", "none", "None", "null") or "親子" in tp or "親子" in nm or "親子" in (raw_doc.get("tags") or []):
                has_family = True
            if grade:
                grades.append(grade)
            if admin:
                admins.append(admin)
            if exec_unit:
                execs.append(exec_unit)

        if place_types:
            tags.extend(place_types)
        if has_accessible and "無障礙" not in tags:
            tags.append("無障礙")
        if has_family and "親子" not in tags:
            tags.append("親子")
        if grades:
            tags.extend(grades)

        # 去重且保持原始順序
        tags = list(dict.fromkeys(tags))

        # 輸出格式分流
        if output_format == "raw":
            # 保留原始格式但套用清洗與合併結果
            merged_item = dict(first_raw)
            merged_item["name"] = base_name
            merged_item["address"] = addr
            merged_item["tags"] = tags
            if lng is not None and lat is not None:
                merged_item["longitude"] = str(lng)
                merged_item["latitude"] = str(lat)
            cleaned_records.append(merged_item)
        else:
            # 標準 MongoDB Document Schema 格式
            district = extract_district(addr)
            main_place_type = place_types[0] if place_types else ""
            if district and main_place_type:
                landmark = f"{district}{main_place_type}"
            elif district:
                landmark = district
            elif main_place_type:
                landmark = main_place_type
            else:
                landmark = first_raw.get("landmark") or None

            # 組合備註 note
            note_parts = []
            unique_admins = list(dict.fromkeys(admins))
            unique_execs = list(dict.fromkeys([e for e in execs if e not in unique_admins]))
            if unique_admins:
                note_parts.append(f"管理單位: {', '.join(unique_admins)}")
            if unique_execs:
                note_parts.append(f"維護單位: {', '.join(unique_execs)}")
            if first_raw.get("note"):
                for part in str(first_raw["note"]).split(" / "):
                    p_clean = part.strip()
                    if p_clean and p_clean not in note_parts:
                        note_parts.append(p_clean)
            note = " / ".join(note_parts) if note_parts else None

            # 彙整評分與評論數
            total_reviews = sum(int(it["raw"].get("reviewCount", 0)) for it in items)
            earliest_created = min((it["raw"].get("createdAt") for it in items if it["raw"].get("createdAt")), default=now)

            doc: Dict[str, Any] = {
                "name": base_name,
                "location": {
                    "type": "Point",
                    "coordinates": [lng, lat],
                },
                "address": addr,
                "hasToiletPaper": first_raw.get("hasToiletPaper", None),
                "isAccessible": has_accessible,
                "tags": tags,
                "avgCleanScore": float(first_raw.get("avgCleanScore", 0.0)),
                "avgConvenienceScore": float(first_raw.get("avgConvenienceScore", 0.0)),
                "reviewCount": total_reviews,
                "createdAt": earliest_created,
                "updatedAt": now,
                "landmark": landmark,
                "note": note,
            }
            if "_id" in first_raw:
                doc["_id"] = first_raw["_id"]

            cleaned_records.append(doc)

    stats["total_output"] = len(cleaned_records)
    return cleaned_records, stats


def clean_existing_database(
    uri: str,
    db_name: str,
    collection_name: str = "toilets",
    dry_run: bool = False,
    batch_size: int = 500
) -> Dict[str, Any]:
    """
    直接讀取並清洗 MongoDB 資料庫中已存在的公廁資料：
    1. 抓取所有現存公廁 Document。
    2. 依 (address, base_name) 分組，識別相同地點之男廁、女廁紀錄。
    3. 執行主體名稱去除性別詞綴，並統一標註 '男女廁' / '僅男廁' / '僅女廁'。
    4. 保留各組 primary document 進行更新，並將其他重複 document 刪除。
    5. 自動更新關聯之 reviews 的 toiletId，保持參照完整性。
    """
    if MongoClient is None:
        raise ImportError("未安裝 pymongo，請執行 pip install pymongo")

    print(f"\n🔌 正在連線至 MongoDB: {db_name}.{collection_name} ...")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    db = client[db_name]
    col = db[collection_name]
    reviews_col = db["reviews"]

    print("📥 正在讀取資料庫中所有現存公廁資料...")
    all_docs = list(col.find({}))
    total_before = len(all_docs)
    print(f"📊 讀取完成，共 {total_before} 筆公廁 Document")

    # 分組
    grouped = defaultdict(list)
    for d in all_docs:
        addr = str(d.get("address", "")).strip()
        raw_name = str(d.get("name", "")).strip()
        type_str = ""
        tags = d.get("tags") or []
        for t in tags:
            if "男" in str(t) or "女" in str(t):
                type_str += " " + str(t)
        
        bname, genders = normalize_and_extract_gender(raw_name, type_str)
        grouped[(addr, bname)].append((d, bname, genders))

    updates_to_perform = []
    deletes_to_perform = []
    review_updates = []

    stats = {
        "total_before": total_before,
        "total_after": len(grouped),
        "merged_groups": 0,
        "single_updates": 0,
        "both_gender_count": 0,
        "male_only_count": 0,
        "female_only_count": 0,
        "no_gender_count": 0,
        "deleted_count": 0,
    }

    now = datetime.now(timezone.utc)

    for (addr, bname), items in grouped.items():
        all_genders = set()
        for d, b, g in items:
            all_genders.update(g)

        # 判定最終性別 tag
        final_gender_tag = None
        if "男女" in all_genders or ("男" in all_genders and "女" in all_genders):
            final_gender_tag = "男女廁"
            stats["both_gender_count"] += 1
        elif "男" in all_genders:
            final_gender_tag = "僅男廁"
            stats["male_only_count"] += 1
        elif "女" in all_genders:
            final_gender_tag = "僅女廁"
            stats["female_only_count"] += 1
        else:
            stats["no_gender_count"] += 1

        primary_doc = items[0][0]
        primary_id = primary_doc["_id"]

        # 彙整所有 tags
        combined_tags = []
        for d, b, g in items:
            for t in (d.get("tags") or []):
                t_str = str(t).strip()
                if re.search(r'^\d*[Ff樓層]?(?:僅男廁|僅女廁|男女廁|混合性別)$', t_str):
                    continue
                if t_str and t_str not in combined_tags:
                    combined_tags.append(t_str)

        if final_gender_tag:
            combined_tags.insert(1 if len(combined_tags) > 0 else 0, final_gender_tag)

        # 彙整無障礙與衛生紙
        is_accessible = any(d.get("isAccessible") is True or "無障礙" in (d.get("tags") or []) for d, b, g in items)
        if is_accessible and "無障礙" not in combined_tags:
            combined_tags.append("無障礙")

        has_tp = None
        for d, b, g in items:
            if d.get("hasToiletPaper") is True:
                has_tp = True
                break
            elif d.get("hasToiletPaper") is False:
                has_tp = False

        # 彙整 notes
        notes = []
        for d, b, g in items:
            n = d.get("note")
            if n and str(n).strip():
                for part in str(n).split(" / "):
                    p_clean = part.strip()
                    if p_clean and p_clean not in notes:
                        notes.append(p_clean)
        merged_note = " / ".join(notes) if notes else None

        # 經緯度補齊與校驗
        lng, lat = parse_coordinates([d for d, b, g in items])
        loc = primary_doc.get("location")
        if lng is not None and lat is not None:
            loc = {"type": "Point", "coordinates": [lng, lat]}

        # 評論數與最早建立時間
        total_reviews = sum(int(d.get("reviewCount", 0)) for d, b, g in items)
        earliest_created = min((d.get("createdAt") for d, b, g in items if d.get("createdAt")), default=now)

        district = extract_district(addr)
        place_type = next((t for t in combined_tags if t not in ["男女廁", "僅男廁", "僅女廁", "無障礙", "親子", "特優級", "優等級", "普通級", "改善級"]), "")
        landmark = primary_doc.get("landmark")
        if district and place_type:
            landmark = f"{district}{place_type}"
        elif district:
            landmark = district

        update_fields = {
            "name": bname,
            "address": addr,
            "tags": list(dict.fromkeys(combined_tags)),
            "isAccessible": is_accessible,
            "hasToiletPaper": has_tp,
            "note": merged_note,
            "location": loc,
            "landmark": landmark,
            "reviewCount": total_reviews,
            "createdAt": earliest_created,
            "updatedAt": now,
        }

        updates_to_perform.append((primary_id, update_fields))

        if len(items) > 1:
            stats["merged_groups"] += 1
            other_ids = [d["_id"] for d, b, g in items[1:]]
            deletes_to_perform.extend(other_ids)
            stats["deleted_count"] += len(other_ids)
            for oid in other_ids:
                review_updates.append((oid, primary_id))
        else:
            stats["single_updates"] += 1

    print("\n📈 資料庫清洗分析結果：")
    print(f"  • 資料庫原始公廁筆數: {stats['total_before']}")
    print(f"  • 清洗合併後公廁筆數: {stats['total_after']}")
    print(f"  • 識別重複並合併組數: {stats['merged_groups']}")
    print(f"  • 單筆公廁紀錄清洗數: {stats['single_updates']}")
    print(f"  • 預計移除多餘重複筆數: {stats['deleted_count']}")
    print(f"  • 標籤分佈 - 男女廁: {stats['both_gender_count']}")
    print(f"  • 標籤分佈 - 僅男廁: {stats['male_only_count']}")
    print(f"  • 標籤分佈 - 僅女廁: {stats['female_only_count']}")
    print(f"  • 標籤分佈 - 無性別標記: {stats['no_gender_count']}")

    if dry_run:
        print("\n✨ [Dry-Run 模式] 檢驗完畢，未對 MongoDB 進行實際寫入或刪除。")
        client.close()
        return stats

    print(f"\n🚀 正在更新 primary documents ({len(updates_to_perform)} 筆)...")
    bulk_updates = [
        UpdateOne({"_id": pid}, {"$set": u_fields})
        for pid, u_fields in updates_to_perform
    ]

    modified_count = 0
    for i in range(0, len(bulk_updates), batch_size):
        chunk = bulk_updates[i:i + batch_size]
        res = col.bulk_write(chunk, ordered=False)
        modified_count += res.modified_count

    print(f"✅ 更新完成：成功更新 {modified_count} 筆 Document")

    if deletes_to_perform:
        print(f"🗑️  正在刪除重複之 Document ({len(deletes_to_perform)} 筆)...")
        del_res = col.delete_many({"_id": {"$in": deletes_to_perform}})
        print(f"✅ 刪除完成：成功刪除 {del_res.deleted_count} 筆重複 Document")

    # 若有評論關聯，更新 review 參照
    if review_updates and reviews_col.count_documents({}) > 0:
        print(f"🔗 正在更新相關評論關聯 ({len(review_updates)} 筆)...")
        for old_id, new_id in review_updates:
            reviews_col.update_many({"toiletId": str(old_id)}, {"$set": {"toiletId": str(new_id)}})
        print("✅ 評論關聯更新完成")

    # 確保 2dsphere 索引
    try:
        col.create_index([("location", "2dsphere")])
        print("🗺️  已確認 2dsphere 空間索引存在")
    except Exception as e:
        print(f"⚠️  2dsphere 索引檢驗: {e}")

    client.close()
    return stats


def load_input_data(input_path: Path) -> List[Dict[str, Any]]:
    """從單一檔案或目錄中載入所有 JSON 資料"""
    records: List[Dict[str, Any]] = []

    if input_path.is_file():
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    records.extend(data)
                elif isinstance(data, dict):
                    records.append(data)
        except Exception as e:
            print(f"❌ 讀取檔案失敗 ({input_path}): {e}")
    elif input_path.is_dir():
        json_files = list(input_path.glob("*.json"))
        for jf in json_files:
            if "cleaned" in jf.name:
                continue
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        records.extend(data)
                    elif isinstance(data, dict):
                        records.append(data)
            except Exception as e:
                print(f"⚠️  讀取檔案跳過 ({jf.name}): {e}")
    else:
        print(f"❌ 輸入路徑不存在：{input_path}")

    return records


def sync_to_mongodb(
    docs: List[Dict[str, Any]],
    uri: str,
    db_name: str,
    collection_name: str = "toilets",
    drop: bool = False,
    batch_size: int = 500
) -> Tuple[int, int]:
    """將清洗後的公廁資料同步寫入/更新至 MongoDB"""
    if MongoClient is None:
        print("❌ 未安裝 pymongo，無法執行 MongoDB 寫入。請執行 pip install pymongo")
        return 0, 0

    print(f"\n🔌 正在連線至 MongoDB: {db_name}.{collection_name} ...")
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[db_name]
        col = db[collection_name]
    except Exception as e:
        print(f"❌ MongoDB 連線失敗: {e}")
        return 0, 0

    if drop:
        print(f"🗑️  清空 Collection: {collection_name}")
        col.drop()

    # 確保 2dsphere 索引
    try:
        col.create_index([("location", "2dsphere")])
    except Exception as e:
        print(f"⚠️  建立 2dsphere 空間索引警告: {e}")

    total_inserted = 0
    total_updated = 0

    operations = []
    for doc in docs:
        query = {"name": doc["name"], "address": doc["address"]}
        update_payload = {"$set": doc}
        operations.append(UpdateOne(query, update_payload, upsert=True))

        if len(operations) >= batch_size:
            result = col.bulk_write(operations, ordered=False)
            total_inserted += result.upserted_count
            total_updated += result.modified_count
            operations = []

    if operations:
        result = col.bulk_write(operations, ordered=False)
        total_inserted += result.upserted_count
        total_updated += result.modified_count

    client.close()
    return total_inserted, total_updated


def main():
    parser = argparse.ArgumentParser(
        description="Bathroom Genius - 公廁資料清洗與男女廁合併工具 (Clean Data & Normalize Gender)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--clean-db",
        action="store_true",
        help="直接讀取並清洗 MongoDB 資料庫中已存在的資料 (進行同地點合併、去性別綴詞與標籤修正)",
    )
    parser.add_argument(
        "--input",
        "-i",
        default="data",
        help="輸入 JSON 檔案或資料夾路徑 (預設: data/)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="data/cleaned_toilets.json",
        help="輸出清洗後 JSON 檔案路徑 (預設: data/cleaned_toilets.json)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["standard", "raw"],
        default="standard",
        help="輸出格式：\n- standard: 標準 Bathroom Genius MongoDB Document 結構 (預設)\n- raw: 保留原始欄位並套用合併與清洗",
    )
    parser.add_argument(
        "--to-mongo",
        action="store_true",
        help="清洗 JSON 完成後直接寫入/更新至 MongoDB",
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
        help="目標 MongoDB Collection 名稱 (預設: toilets)",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="寫入 MongoDB 前清空目標 Collection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="僅執行清洗、分組與合併校驗，不寫入檔案或資料庫",
    )

    args = parser.parse_args()

    print("=" * 72)
    print("🚽 Bathroom Genius - 公廁資料清洗與性別合併工具 (clean_data.py)")
    print("=" * 72)

    # 載入環境變數
    env_path = Path(args.env_file).resolve() if args.env_file else Path(__file__).resolve().parent / ".env"
    load_environment_variables(env_path)
    mongo_uri = args.uri or os.getenv("MONGODB_URI")
    mongo_db_name = args.db or os.getenv("MONGODB_DB_NAME", "bathroom_online")

    # 模式 A: 直接處理資料庫中已存在的資料
    if args.clean_db:
        if not mongo_uri:
            print("❌ 未設定 MONGODB_URI，無法執行資料庫清洗。請確認 .env 或使用 --uri 參數。")
            sys.exit(1)

        print("⚡ 模式：直接清洗資料庫中現存資料")
        clean_existing_database(
            uri=mongo_uri,
            db_name=mongo_db_name,
            collection_name=args.collection,
            dry_run=args.dry_run
        )
        print("\n🎉 資料庫清洗作業順利完成！")
        return

    # 模式 B: 處理檔案
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = Path(__file__).resolve().parent / input_path

    print(f"📂 正在載入資料：{input_path}")
    raw_records = load_input_data(input_path)
    if not raw_records:
        print("❌ 未讀取到任何資料，結束執行。")
        sys.exit(1)

    print(f"📊 原始資料載入完成，共 {len(raw_records)} 筆紀錄")

    # 清洗與合併
    print("⚙️  正在執行同地點合併、名稱性別詞綴移除與標籤指派...")
    cleaned_records, stats = clean_and_merge_records(raw_records, output_format=args.format)

    print("\n📈 清洗統計報告：")
    print(f"  • 原始資料總筆數: {stats['total_input']}")
    print(f"  • 合併產出總地點: {stats['total_output']}")
    print(f"  • 同地點多筆公廁合併組數: {stats['merged_multi_items_groups']}")
    print(f"  • 單筆公廁地點組數: {stats['single_item_groups']}")
    print(f"  • 標籤分佈 - 男女廁: {stats['both_gender_count']}")
    print(f"  • 標籤分佈 - 僅男廁: {stats['male_only_count']}")
    print(f"  • 標籤分佈 - 僅女廁: {stats['female_only_count']}")
    print(f"  • 標籤分佈 - 無性別標記 (如純無障礙等): {stats['no_gender_count']}")
    if stats['skipped_no_coords'] > 0:
        print(f"  • 略過無效座標筆數: {stats['skipped_no_coords']}")

    # 範例展示
    print("\n🔍 代表性合併成果範例展示：")
    samples = []
    both_sample = next((r for r in cleaned_records if "男女廁" in r.get("tags", [])), None)
    male_sample = next((r for r in cleaned_records if "僅男廁" in r.get("tags", [])), None)
    female_sample = next((r for r in cleaned_records if "僅女廁" in r.get("tags", [])), None)

    for s in [both_sample, male_sample, female_sample]:
        if s:
            samples.append(s)

    if not samples and cleaned_records:
        samples = cleaned_records[:3]

    for i, s in enumerate(samples, 1):
        s_display = {**s}
        if "createdAt" in s_display and isinstance(s_display["createdAt"], datetime):
            s_display["createdAt"] = s_display["createdAt"].isoformat()
        if "updatedAt" in s_display and isinstance(s_display["updatedAt"], datetime):
            s_display["updatedAt"] = s_display["updatedAt"].isoformat()
        print(f"\n--- 範例 #{i}: {s_display.get('name')} ---")
        print(json.dumps(s_display, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("\n✨ [Dry-Run 模式] 檢驗完畢，未更動檔案或資料庫。")
        return

    # 儲存輸出 JSON
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parent / output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n💾 正在儲存清洗後資料至 JSON: {output_path} ...")
    
    serializable_docs = []
    for doc in cleaned_records:
        d_copy = {**doc}
        for k, v in d_copy.items():
            if isinstance(v, datetime):
                d_copy[k] = v.isoformat()
        serializable_docs.append(d_copy)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serializable_docs, f, ensure_ascii=False, indent=2)
    print(f"✅ 成功儲存 {len(serializable_docs)} 筆資料至 {output_path}")

    # 同步至 MongoDB (若有指定)
    if args.to_mongo:
        if not mongo_uri:
            print("⚠️  未設定 MONGODB_URI，略過 MongoDB 同步。")
        else:
            upserted, modified = sync_to_mongodb(
                docs=cleaned_records,
                uri=mongo_uri,
                db_name=mongo_db_name,
                collection_name=args.collection,
                drop=args.drop
            )
            print(f"🎉 MongoDB 同步完成：新增 {upserted} 筆 / 更新 {modified} 筆")

    print("\n🎯 全部處理完成！")


if __name__ == "__main__":
    main()
