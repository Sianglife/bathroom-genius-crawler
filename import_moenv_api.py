#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOENV Public Toilet API Import Script for Bathroom Genius
從環境部開放資料 API (MOENV API v2) 抓取公廁資料並匯入至 MongoDB
支援：
- 多 API Endpoint 清單管理
- 自動翻頁機制 (offset 從 0 遞增迭代直至無資料或資料不再變動)
- 內建重試與 SSL 憑證異常相容
- 同地點/多樓層公廁紀錄自動合併
- 樓層與性別標籤萃取 (如 1F男女廁、2F僅男廁、無障礙、親子等)
- 匯入前比對資料庫，重複公廁自動略過 (可切換 upsert 模式)
- 支援 Dry-Run 與本機 JSON 備份保存
"""

import os
import sys
import json
import re
import ssl
import time
import hashlib
import argparse
import urllib.request
import urllib.parse
import urllib.error
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
    from pymongo import MongoClient, InsertOne, UpdateOne
    from pymongo.errors import PyMongoError
except ImportError:
    MongoClient = None
    InsertOne = None
    UpdateOne = None
    PyMongoError = Exception


# ==============================================================================
# API Endpoints 清單 (包含 fac_p_07 以及 fac_p_10 至 fac_p_31 各縣市列管公廁資料)
# ==============================================================================
MOENV_API_KEY = "40eaf06a-f396-4f27-8703-016cebce83c8"

# 展開之代碼清單：07、10-31 (共 23 個公廁資料端點，全台灣)
# ENDPOINT_CODES: List[str] = ["07"] + [f"{i:02d}" for i in range(10, 32)]

# 北北基桃代碼清單：18 (基隆)、21 (新北)、28 (臺北)、16 (桃園)
ENDPOINT_CODES: List[str] = ["18", "21", "28", "16"]

MOENV_API_ENDPOINTS: List[str] = [
    f"https://data.moenv.gov.tw/api/v2/fac_p_{code}?offset=0&limit=1000&api_key={MOENV_API_KEY}"
    for code in ENDPOINT_CODES
]


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


# ==============================================================================
# HTTP API 抓取與翻頁模組
# ==============================================================================
def create_ssl_context() -> ssl.SSLContext:
    """建立相容性 SSL Context，處理政府網站憑證相容問題"""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    except Exception:
        return ssl._create_unverified_context()


def fetch_api_page(
    target_url: str,
    ssl_context: ssl.SSLContext,
    timeout: int = 20,
    max_retries: int = 3,
    delay_between_retries: float = 2.0
) -> List[Dict[str, Any]]:
    """
    發送 HTTP GET 請求取得 API 單頁資料，具備自動重試機制
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(target_url, headers=headers)

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                raw_bytes = response.read()
                
                # 解碼文字編碼
                try:
                    text_content = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    text_content = raw_bytes.decode("utf-8-sig", errors="replace")

                data = json.loads(text_content)

                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    # 某些 API 可能包裝在 'records' 或 'data' 欄位
                    if "records" in data and isinstance(data["records"], list):
                        return data["records"]
                    if "data" in data and isinstance(data["data"], list):
                        return data["data"]
                    return [data]
                else:
                    return []

        except urllib.error.HTTPError as e:
            print(f"    ⚠️  HTTP 錯誤 (狀態碼 {e.code})，嘗試重試 ({attempt}/{max_retries})...")
        except urllib.error.URLError as e:
            print(f"    ⚠️  連線錯誤 ({e.reason})，嘗試重試 ({attempt}/{max_retries})...")
        except json.JSONDecodeError as e:
            print(f"    ⚠️  JSON 解析失敗 ({e})，嘗試重試 ({attempt}/{max_retries})...")
        except Exception as e:
            print(f"    ⚠️  未知錯誤 ({e})，嘗試重試 ({attempt}/{max_retries})...")

        if attempt < max_retries:
            time.sleep(delay_between_retries * attempt)

    raise RuntimeError(f"無法取得 API 資料 (已重試 {max_retries} 次): {target_url}")


def crawl_endpoint_with_pagination(
    endpoint_url: str,
    ssl_context: ssl.SSLContext,
    start_offset: int = 0,
    page_limit: int = 1000,
    max_pages: Optional[int] = None,
    delay_between_pages: float = 0.5,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    從指定的 API 端點進行自動翻頁抓取
    - offset 從 start_offset 開始迭代
    - 終止條件：
        1. 回傳資料為空陣列
        2. 回傳資料與前一頁內容雜湊值完全相同 (資料未變)
        3. 回傳筆數小於 limit
        4. 達到 max_pages 上限 (若有設定)
    """
    parsed_url = urllib.parse.urlparse(endpoint_url)
    query_params = urllib.parse.parse_qs(parsed_url.query)

    # 提取或覆寫 query 中的 limit 與 api_key
    api_key_list = query_params.get("api_key", [])
    api_key = api_key_list[0] if api_key_list else ""

    # 若 URL 原本帶有 limit 則優先採用
    if "limit" in query_params and query_params["limit"]:
        try:
            page_limit = int(query_params["limit"][0])
        except ValueError:
            pass

    # 基礎 URL (移除 query params 後重新組裝)
    base_endpoint = urllib.parse.urlunparse((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path,
        "",
        "",
        ""
    ))

    print(f"\n📡 開始抓取端點：{base_endpoint}")
    print(f"   初始 offset: {start_offset} | 每頁筆數: {page_limit}")

    all_records: List[Dict[str, Any]] = []
    current_offset = start_offset
    page_count = 0
    prev_page_fingerprint: Optional[str] = None
    seen_identifiers: Set[str] = set()

    stats = {
        "endpoint": base_endpoint,
        "total_fetched": 0,
        "pages_fetched": 0,
        "stopped_reason": "",
    }

    while True:
        page_count += 1

        # 組裝此頁 URL
        current_params = {
            "offset": str(current_offset),
            "limit": str(page_limit),
        }
        if api_key:
            current_params["api_key"] = api_key

        request_url = f"{base_endpoint}?{urllib.parse.urlencode(current_params)}"
        print(f"   📄 [第 {page_count:2d} 頁] offset={current_offset:<6d} 請求中...", end="", flush=True)

        try:
            page_data = fetch_api_page(request_url, ssl_context)
        except Exception as e:
            stats["stopped_reason"] = f"請求失敗: {e}"
            print(f" ❌\n   🛑 {stats['stopped_reason']}")
            break

        page_len = len(page_data)
        print(f" ✅ 取得 {page_len} 筆資料")

        # 1. 終止條件：回傳空資料
        if not page_data or page_len == 0:
            stats["stopped_reason"] = "回傳資料為空 (已無更多資料)"
            print(f"   🏁 {stats['stopped_reason']}")
            break

        # 計算本頁資料特徵雜湊 (用於檢測資料是否不再變動)
        page_dump = json.dumps(page_data, sort_keys=True)
        current_page_fingerprint = hashlib.sha256(page_dump.encode("utf-8")).hexdigest()

        # 2. 終止條件：回傳資料與上一頁完全相同
        if current_page_fingerprint == prev_page_fingerprint:
            stats["stopped_reason"] = "回傳資料與前一頁完全相同 (資料不再變更)"
            print(f"   🏁 {stats['stopped_reason']}")
            break

        # 檢測重複紀錄並加入總集
        new_in_page = 0
        for item in page_data:
            # 依 number 或 (county, areacode, name, address) 建立特徵
            item_id = str(item.get("number") or f"{item.get('name')}_{item.get('address')}_{item.get('latitude')}")
            if item_id not in seen_identifiers:
                seen_identifiers.add(item_id)
                all_records.append(item)
                new_in_page += 1
            else:
                # 重複項目仍累積至該頁（但標記）
                all_records.append(item)

        prev_page_fingerprint = current_page_fingerprint
        current_offset += page_limit

        # 3. 終止條件：回傳筆數小於 limit，代表已至最後一頁
        if page_len < page_limit:
            stats["stopped_reason"] = f"回傳筆數 ({page_len}) 小於每頁上限 ({page_limit})，已至尾頁"
            print(f"   🏁 {stats['stopped_reason']}")
            break

        if max_pages and page_count >= max_pages:
            stats["stopped_reason"] = f"已達設定之最大頁數上限 ({max_pages} 頁)"
            print(f"   🛑 {stats['stopped_reason']}")
            break

        if delay_between_pages > 0:
            time.sleep(delay_between_pages)

    stats["total_fetched"] = len(all_records)
    stats["pages_fetched"] = page_count
    print(f"   ✨ 端點抓取完成：共抓取 {page_count} 頁，累積 {len(all_records)} 筆原始紀錄")
    return all_records, stats


def crawl_all_endpoints(
    endpoint_urls: List[str],
    ssl_context: ssl.SSLContext,
    start_offset: int = 0,
    page_limit: int = 1000,
    max_pages: Optional[int] = None,
    delay_between_pages: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    依序抓取所有 API Endpoint 清單並進行全域資料彙整與初步去重
    """
    total_raw_records: List[Dict[str, Any]] = []

    print("=" * 68)
    print(f"🌐 準備抓取 {len(endpoint_urls)} 個 API 端點...")
    print("=" * 68)

    for idx, endpoint in enumerate(endpoint_urls, start=1):
        print(f"\n[{idx}/{len(endpoint_urls)}] 處理端點: {endpoint[:80]}...")
        records, stats = crawl_endpoint_with_pagination(
            endpoint_url=endpoint,
            ssl_context=ssl_context,
            start_offset=start_offset,
            page_limit=page_limit,
            max_pages=max_pages,
            delay_between_pages=delay_between_pages,
        )
        total_raw_records.extend(records)

    return total_raw_records


# ==============================================================================
# 主程式 CLI
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="從環境部 API (MOENV API v2) 抓取公廁開放資料並匯入至 MongoDB\n"
                    "具備自動翻頁、重複資料比對與略過、同地點合併與樓層性別標籤功能",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--url",
        "-u",
        default=None,
        help="自訂 API Endpoint 網址 (若未指定則使用 MOENV_API_ENDPOINTS 清單)",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="翻頁起始 offset (預設: 0)",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=1000,
        help="每頁取得筆數 limit (預設: 1000)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="限制最多抓取頁數 (測試用，預設不限制直至結束)",
    )
    parser.add_argument(
        "--env-file",
        "-e",
        default=None,
        help=".env 檔案路徑 (預設自動偵測專案根目錄 .env)",
    )
    parser.add_argument(
        "--uri",
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
        "--skip-duplicates",
        action="store_true",
        default=True,
        help="匯入前比對資料庫，若已存在相同 (name, address) 則略過 (預設啟用)",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="遇到重複時執行 Upsert 更新資料而非略過",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="匯入前清空目標 Collection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="僅執行 API 抓取、翻頁、解析與校驗，不實際連線寫入 MongoDB",
    )
    parser.add_argument(
        "--save-json",
        default=None,
        help="將抓取並合併後的資料儲存為本機 JSON 檔案 (例如: data/moenv_api_crawled.json)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="批次寫入筆數 (預設: 500)",
    )

def run_import(
    env_file: Optional[str] = None,
    uri: Optional[str] = None,
    db: Optional[str] = None,
    collection: str = "toilets",
    url: Optional[str] = None,
    start_offset: int = 0,
    page_limit: int = 1000,
    max_pages: Optional[int] = None,
    skip_duplicates: bool = True,
    update_existing: bool = False,
    drop: bool = False,
    dry_run: bool = False,
    save_json: Optional[str] = None,
    batch_size: int = 500
) -> Dict[str, Any]:
    """執行 MOENV API 公廁抓取與匯入程序，並回傳統計字典"""
    # 1. 載入環境變數
    env_path = Path(env_file).resolve() if env_file else Path(__file__).resolve().parent / ".env"
    load_environment_variables(env_path)

    # 2. 決定 MongoDB 連線參數
    mongo_uri = uri or os.getenv("MONGODB_URI")
    mongo_db_name = db or os.getenv("MONGODB_DB_NAME", "bathroom_online")

    print("=" * 68)
    print("🚽 Bathroom Genius - 環境部 API 公廁自動爬蟲與匯入工具")
    print("=" * 68)

    # 3. 決定 API Endpoints 清單
    endpoints = [url] if url else MOENV_API_ENDPOINTS
    print(f"📋 共配置 {len(endpoints)} 個 API 端點")
    for i, ep in enumerate(endpoints, 1):
        print(f"  {i}. {ep}")

    # 4. 抓取 API 資料 (含自動翻頁)
    ssl_context = create_ssl_context()
    raw_data = crawl_all_endpoints(
        endpoint_urls=endpoints,
        ssl_context=ssl_context,
        start_offset=start_offset,
        page_limit=page_limit,
        max_pages=max_pages,
    )

    total_records = len(raw_data)
    if total_records == 0:
        print("⚠️ 未抓取到任何公廁資料，程式結束。")
        return {"raw_records": 0, "valid_docs": 0, "inserted": 0, "upserted": 0, "modified": 0, "skipped": 0, "db_total": 0}

    print(f"\n📊 API 抓取完成，共取得 {total_records} 筆原始公廁紀錄")

    # 5. 依地點與主體名稱分組 (同地點多樓層合併)
    print("🔄 正在進行地點識別與同地點多樓層合併...")
    grouped_data = defaultdict(list)
    for item in raw_data:
        addr = str(item.get("address", "")).strip()
        name = str(item.get("name", "")).strip()
        bname = get_base_name(name)
        grouped_data[(addr, bname)].append(item)

    merged_locations_count = len(grouped_data)
    multi_toilet_locations = sum(1 for v in grouped_data.values() if len(v) > 1)
    print(f"🏢 辨識出 {merged_locations_count} 個獨立公廁地點（其中 {multi_toilet_locations} 處包含多座/多樓層分開紀錄並已合併）")

    # 6. 轉換與校驗 Document
    valid_docs: List[Dict[str, Any]] = []
    skipped_invalid_count = 0

    for (addr, bname), items in grouped_data.items():
        doc, err = merge_toilet_group(addr, bname, items)
        if err:
            skipped_invalid_count += 1
            if skipped_invalid_count <= 5:
                print(f"  ⚠️  {err}")
        else:
            valid_docs.append(doc)

    print(f"✅ 資料合併完成：成功產出 {len(valid_docs)} 筆公廁 Document / 經緯度無效略過 {skipped_invalid_count} 筆")

    # 7. 若有指定 save_json 則輸出本機檔案
    if save_json:
        save_path = Path(save_json)
        if not save_path.is_absolute():
            save_path = Path(__file__).resolve().parent / save_path
        save_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"💾 正在儲存合併後資料至本機：{save_path}")

        serializable_docs = []
        for d in valid_docs:
            d_copy = {**d}
            if isinstance(d_copy.get("createdAt"), datetime):
                d_copy["createdAt"] = d_copy["createdAt"].isoformat()
            if isinstance(d_copy.get("updatedAt"), datetime):
                d_copy["updatedAt"] = d_copy["updatedAt"].isoformat()
            serializable_docs.append(d_copy)

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(serializable_docs, f, ensure_ascii=False, indent=2)
        print("✅ JSON 檔案儲存成功！")

    # 若為 Dry-Run 模式則印出示範並結束
    if dry_run:
        print("\n🔍 [Dry-Run 模式] 不會連線寫入 MongoDB。以下為具代表性的轉換與合併範例：")
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
        return {"raw_records": total_records, "valid_docs": len(valid_docs), "inserted": 0, "upserted": 0, "modified": 0, "skipped": 0, "db_total": 0}

    # 8. 連線至 MongoDB
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

    # 9. 檢查資料庫重複紀錄
    mode_skip_duplicates = skip_duplicates and not update_existing
    existing_db_keys: Set[Tuple[str, str]] = set()

    if mode_skip_duplicates:
        print("🔎 正在比對資料庫中已存在的公廁紀錄 (以 name + address 比對)...")
        try:
            cursor = col_obj.find({}, {"name": 1, "address": 1, "_id": 0})
            for record in cursor:
                r_name = str(record.get("name", "")).strip()
                r_addr = str(record.get("address", "")).strip()
                if r_name or r_addr:
                    existing_db_keys.add((r_name, r_addr))
            print(f"   📊 目前資料庫中已有 {len(existing_db_keys)} 筆公廁資料")
        except Exception as e:
            print(f"   ⚠️ 預先查詢資料庫時發生錯誤：{e}，將改以 upsert 方式處理")
            mode_skip_duplicates = False

    # 10. 匯入或寫入 MongoDB
    print(f"\n🚀 開始執行匯入處理 (共 {len(valid_docs)} 筆待處理資料) ...")
    docs_to_insert: List[Dict[str, Any]] = []
    skipped_duplicate_count = 0

    total_inserted = 0
    total_upserted = 0
    total_modified = 0

    if mode_skip_duplicates:
        print("🛡️  [重複檢查模式] 比對為已存在之公廁將自動略過 (Skip duplicates)...")
        for doc in valid_docs:
            key = (doc["name"], doc["address"])
            if key in existing_db_keys:
                skipped_duplicate_count += 1
            else:
                docs_to_insert.append(doc)
                existing_db_keys.add(key)

        print(f"   • 略過重複筆數 (Skipped): {skipped_duplicate_count} 筆")
        print(f"   • 預備新增筆數 (To Insert): {len(docs_to_insert)} 筆")

        if docs_to_insert:
            for i in range(0, len(docs_to_insert), batch_size):
                batch = docs_to_insert[i:i + batch_size]
                insert_ops = [InsertOne(d) for d in batch]
                res = col_obj.bulk_write(insert_ops, ordered=False)
                total_inserted += res.inserted_count
                print(f"   已寫入 {min(i + batch_size, len(docs_to_insert))}/{len(docs_to_insert)} 筆新增資料...")
    else:
        print("🔄 [更新模式] 對已存在之公廁執行 Upsert 更新...")
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
                batch_operations.clear()
                print(f"   已處理 {i + 1}/{len(valid_docs)} 筆...")

        if batch_operations:
            result = col_obj.bulk_write(batch_operations, ordered=False)
            total_upserted += len(result.upserted_ids)
            total_modified += result.modified_count
            batch_operations.clear()

    current_db_total = col_obj.count_documents({})
    client.close()

    print("\n" + "=" * 68)
    print("🎉 匯入完成！統計報告：")
    print(f"  • API 原始抓取筆數   ：{total_records}")
    print(f"  • 合併後獨立公廁總數 ：{len(valid_docs)}")
    if mode_skip_duplicates:
        print(f"  • 比對重複已略過     ：{skipped_duplicate_count}")
        print(f"  • 實際新增入庫筆數   ：{total_inserted}")
    else:
        print(f"  • 新增筆數 (Upserted)：{total_upserted}")
        print(f"  • 更新筆數 (Modified)：{total_modified}")
    print(f"  • 資料庫目前總筆數   ：{current_db_total}")
    print("=" * 68)

    return {
        "raw_records": total_records,
        "valid_docs": len(valid_docs),
        "inserted": total_inserted,
        "upserted": total_upserted,
        "modified": total_modified,
        "skipped": skipped_duplicate_count,
        "db_total": current_db_total
    }


def main():
    parser = argparse.ArgumentParser(
        description="Bathroom Genius - 環境部 (MOENV) 公廁 API 自動抓取與匯入工具",
        formatter_class=argparse.RawTextHelpFormatter,
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
        "--url",
        default=None,
        help="自訂單一 API Endpoint URL (預設使用全台 23 個縣市端點清單)",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="起始 offset 偏移量 (預設: 0)",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=1000,
        help="每頁取得筆數 (預設: 1000)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="每個端點最大翻頁數 (預設: 無限制，直到資料取完為止)",
    )
    parser.add_argument(
        "--collection",
        "-c",
        default="toilets",
        help="目標 Collection 名稱 (預設: toilets)",
    )
    parser.add_argument(
        "--skip-duplicates",
        action="store_true",
        default=True,
        help="匯入前比對資料庫，若已存在相同 (name, address) 則略過 (預設啟用)",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="遇到重複時執行 Upsert 更新資料而非略過",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="匯入前清空目標 Collection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="僅執行 API 抓取、翻頁、解析與校驗，不實際連線寫入 MongoDB",
    )
    parser.add_argument(
        "--save-json",
        default=None,
        help="將抓取並合併後的資料儲存為本機 JSON 檔案 (例如: data/moenv_api_crawled.json)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="批次寫入筆數 (預設: 500)",
    )

    args = parser.parse_args()

    run_import(
        env_file=args.env_file,
        uri=args.uri,
        db=args.db,
        collection=args.collection,
        url=args.url,
        start_offset=args.start_offset,
        page_limit=args.page_limit,
        max_pages=args.max_pages,
        skip_duplicates=args.skip_duplicates,
        update_existing=args.update_existing,
        drop=args.drop,
        dry_run=args.dry_run,
        save_json=args.save_json,
        batch_size=args.batch_size
    )


if __name__ == "__main__":
    main()

