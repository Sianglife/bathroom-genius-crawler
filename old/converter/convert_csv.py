#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gov Toilet CSV Cleaner and Transformer for Bathroom Genius
將政府公廁開放資料轉換為符合系統 Schema 的乾淨 CSV 格式
"""

import csv
import sys
from pathlib import Path
from typing import Dict, List, Any

# 確保 Windows 主控台輸出中文與 emoji 不會拋出 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def clean_and_transform_row(row: Dict[str, str]) -> Dict[str, Any]:
    """將政府原始資料列轉換為乾淨的 Toilet Schema 欄位格式"""
    # 移除 key 前後空白
    row = {k.strip() if isinstance(k, str) else k: v for k, v in row.items()}

    # 1. 取得名稱與地址
    name = row.get("公廁名稱", "").strip() or row.get("name", "").strip()
    address = row.get("公廁地址", "").strip() or row.get("address", "").strip()

    # 2. 經緯度
    lng = row.get("經度", "").strip() or row.get("lng", "").strip()
    lat = row.get("緯度", "").strip() or row.get("lat", "").strip()

    # 3. 行政區與類別
    district = row.get("行政區", "").strip()
    category = row.get("公廁類別", "").strip()
    management = row.get("管理單位", "").strip()

    # 4. 無障礙與親子座數判斷
    def safe_int(v: Any) -> int:
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            return 0

    accessible_seats = safe_int(row.get("無障礙廁所座數") or row.get("無障礙廁座數") or 0)
    family_seats = safe_int(row.get("親子廁所座數") or row.get("親子廁座數") or 0)

    is_accessible = True if accessible_seats > 0 else False

    # 5. 評價等級解析 (直接取最低評分，並捨棄座數)
    special_grade = safe_int(row.get("特優級", 0))
    excellent_grade = safe_int(row.get("優等級", 0))
    normal_grade = safe_int(row.get("普通級", 0))
    improve_grade = safe_int(row.get("加強級") or row.get("改善級") or 0)

    tags: List[str] = []
    if district:
        tags.append(district)
    if category:
        tags.append(category)
    if accessible_seats > 0:
        tags.append("無障礙")
    if family_seats > 0:
        tags.append("親子")

    # 其他設施
    other_facilities = row.get("其他設施", "").strip()
    if other_facilities:
        for f_tag in other_facilities.split(","):
            f_tag = f_tag.strip()
            if f_tag:
                tags.append(f_tag)

    # 評價等級：由低至高判定，僅取最低等級且不帶座數
    if improve_grade > 0:
        tags.append("改善級")
    elif normal_grade > 0:
        tags.append("普通級")
    elif excellent_grade > 0:
        tags.append("優等級")
    elif special_grade > 0:
        tags.append("特優級")

    # 去除重複 tag
    tags = list(dict.fromkeys(tags))

    # 6. 整理備註 (包含備註、一般時段位置、無障礙時段位置、照護床/污物盆位置、管理單位)
    notes_list = []
    raw_note = row.get("備註", "").strip()
    general_loc = row.get("一般時段位置", "").strip()
    accessible_loc = row.get("無障礙時段位置", "").strip()
    bed_loc = row.get("照護床位置", "").strip()
    basin_loc = row.get("污物盆位置", "").strip()

    if raw_note:
        notes_list.append(f"備註: {raw_note}")
    if general_loc:
        notes_list.append(f"一般時段位置: {general_loc}")
    if accessible_loc:
        notes_list.append(f"無障礙時段位置: {accessible_loc}")
    if bed_loc:
        notes_list.append(f"照護床位置: {bed_loc}")
    if basin_loc:
        notes_list.append(f"污物盆位置: {basin_loc}")
    if management and management != name:
        notes_list.append(f"管理單位: {management}")

    note = "；".join(notes_list) if notes_list else ""

    # 7. 地標 (Landmark)
    landmark = ""
    if district and category:
        landmark = f"{district}{category}"
    elif district:
        landmark = district

    return {
        "name": name,
        "address": address,
        "lng": lng,
        "lat": lat,
        "landmark": landmark,
        "isAccessible": "true" if is_accessible else "false",
        "hasToiletPaper": "",  # 留空代表未知 (null)
        "tags": ",".join(tags),
        "note": note,
    }


def convert_file(input_path: str, output_path: str = None):
    """讀取來源 CSV，轉換並輸出為符合 Schema 的標準 CSV"""
    in_file = Path(input_path)
    
    if not in_file.exists():
        print(f"[錯誤] 找不到檔案: {in_file.resolve()}")
        sys.exit(1)

    if not output_path:
        out_file = in_file.parent / f"{in_file.stem}_converted{in_file.suffix}"
    else:
        out_file = Path(output_path)

    encodings = ["utf-8-sig", "utf-8", "big5", "cp950"]
    content = None
    for enc in encodings:
        try:
            with open(in_file, "r", encoding=enc) as f:
                content = f.read()
                break
        except UnicodeDecodeError:
            continue

    if not content:
        print("[錯誤] 無法解析檔案編碼")
        sys.exit(1)

    lines = [line for line in content.splitlines() if line.strip()]
    reader = csv.DictReader(lines)

    fieldnames = [
        "name",
        "address",
        "lng",
        "lat",
        "landmark",
        "isAccessible",
        "hasToiletPaper",
        "tags",
        "note",
    ]

    converted_rows = []
    for row in reader:
        converted = clean_and_transform_row(row)
        if converted["name"] and converted["lng"] and converted["lat"]:
            converted_rows.append(converted)

    with open(out_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(converted_rows)

    print(f"✅ 轉換完成！共轉換 {len(converted_rows)} 筆資料，已輸出至: {out_file.resolve()}")
    return str(out_file)


if __name__ == "__main__":
    in_p = sys.argv[1] if len(sys.argv) > 1 else "raw_toilets.csv"
    out_p = sys.argv[2] if len(sys.argv) > 2 else None
    convert_file(in_p, out_p)
