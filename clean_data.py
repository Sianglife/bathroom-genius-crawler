#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Toilet Data Cleaner, Normalizer & Analyzer for Bathroom Genius (clean_data.py)
公廁資料庫清洗、去重合併、性別/無障礙/親子設施正規化與同地址異名分析工具

【設計原則與職責劃分】
- clean_data.py 專注於處理 MongoDB 資料庫中已存在的資料清洗、同地點/鄰里合併、性別/無障礙/親子設施標籤正規化與資料分析。
- 外部資料源（環境部 MOENV API、臺北市 Data.Taipei 開放資料等）由 import_ 開頭之專用匯入腳本負責。

【核心功能】
1. 男女廁標記去除與標籤賦予：
   - 去除名稱中的男女標記（如「僅男廁」、「女廁」、「1F男廁」等各類後綴與括號詞）。
   - 依同地點男女覆蓋情況自動指派 '男女廁'、'僅男廁'、'僅女廁' 標籤。
2. 無障礙與親子設施處理：
   - 去除名稱中的無障礙後綴（如「無障礙廁所」、「(含無障礙)」、「女廁及無障礙廁所」等），自動將 isAccessible 設定為 True 並指派 '無障礙' 標籤。
   - 去除名稱中的親子後綴（如「1F西親子」、「文化館親子」、「-親子廁所」、「(含親子)」、「親子無障礙廁所-1」等），自動指派 '親子' 標籤。
3. 友善設施衝突保留原則 (Positive Priority)：
   - 合併同地點多筆紀錄時，若友善設施（無障礙 isAccessible、親子設施、衛生紙 hasToiletPaper）有不同狀態，優先保留「有」（True）的一側。
4. 名稱相同且地址相似（僅差在鄰里/村里有無）之合併：
   - 透過 normalize_address 去除地址中的「XX里」、「XX村」、「\d+鄰」、「\d+組」、郵遞區號，並統一「台」->「臺」。
   - 將「臺北市中正區建國里重慶南路一段122號」與「臺北市中正區重慶南路一段122號」等相同地點自動合併為單一紀錄。
5. 列出地址相同但名稱不同的項目：
   - 分析並列出相同地址（或相同正規化地址）但主體名稱不同之公廁地點（例如同大樓/商場內不同樓層或不同主管單位之公廁），方便審查與校對。
6. 資料庫參照完整性：
   - 自動更新關聯之 reviews collection 中的 toiletId，確保評分與留言不丟失。
   - 自動確認與建立 location 欄位之 2dsphere 空間索引。
"""

import os
import sys
import json
import re
import unicodedata
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


def normalize_address(address: str) -> str:
    r"""
    台灣地址正規化工具：
    用於比對相同地點但寫法略有差異（如鄰里有無、台/臺差異、郵遞區號）的地址。
    
    規則：
    1. 全形英數符號轉半形 (NFKC)。
    2. 統一 '台' -> '臺' (台北市 -> 臺北市, 台中市 -> 臺中市, 台灣 -> 臺灣 等)。
    3. 去除開頭郵遞區號 (如 100, 10048, 220 等)。
    4. 去除鄉鎮市區後面的「XX里」、「XX村」。
    5. 去除「\d+鄰」、「\d+組」。
    6. 去除多餘空白、頓號、逗號與橫槓。
    """
    if not address:
        return ""

    addr = str(address).strip()
    # 1. 全形轉半形
    addr = unicodedata.normalize('NFKC', addr)

    # 2. 統一 台 -> 臺
    addr = re.sub(r'^台灣', '臺灣', addr)
    addr = re.sub(r'台([北市中南北東灣])', r'臺\1', addr)

    # 3. 去除開頭郵遞區號 (3或5-6碼數字)
    addr = re.sub(r'^\d{3,6}\s*', '', addr)

    # 4. 去除括號內的補充說明 (例如 "(地下室)", "（捷運站內）")
    addr = re.sub(r'[\(（][^\)）]*[\)）]', '', addr)

    # 5. 去除「XX里」、「XX村」
    # 若在區/鄉/鎮/市後面緊接著里/村 (例如: 中正區建國里重慶南路 -> 中正區重慶南路, 富里鄉新興村中山路 -> 富里鄉中山路)
    addr = re.sub(r'(?<=[區鄉鎮市])([^\d縣市區鄉鎮路街段巷弄號樓Ff]{1,6}[村里])', '', addr)
    # 若無行政區但開頭為村里 (例如: 仁和村慶豐街 -> 慶豐街)
    addr = re.sub(r'^[^\d縣市區鄉鎮路街段巷弄號樓Ff]{1,6}[村里]', '', addr)

    # 6. 去除鄰、組 (例如: 10鄰, 3組, 15鄰)
    addr = re.sub(r'(\d{1,4}[鄰組])', '', addr)

    # 7. 去除多餘空白與分隔符號
    addr = re.sub(r'[\s,，、_]+', '', addr)

    return addr.strip()


def clean_display_address(address: str) -> str:
    """
    產出標準且整潔之顯示地址：
    去除開頭郵遞區號、統一「臺」字、去除村里鄰雜訊，保留乾淨的路名與門牌。
    """
    if not address:
        return ""
    addr = str(address).strip()
    addr = unicodedata.normalize('NFKC', addr)
    addr = re.sub(r'^台灣', '臺灣', addr)
    addr = re.sub(r'台([北市中南北東灣])', r'臺\1', addr)
    addr = re.sub(r'^\d{3,6}\s*', '', addr)
    # 去除行政區後面的里鄰
    addr = re.sub(r'(?<=[區鄉鎮市])([^\d縣市區鄉鎮路街段巷弄號樓Ff]{1,6}[村里])(?:\d{1,4}[鄰組])?', '', addr)
    addr = re.sub(r'(?<=[區鄉鎮市])\d{1,4}[鄰組]', '', addr)
    addr = re.sub(r'^[^\d縣市區鄉鎮路街段巷弄號樓Ff]{1,6}[村里](?:\d{1,4}[鄰組])?', '', addr)
    addr = re.sub(r'\s+', ' ', addr).strip()
    return addr


def normalize_and_extract_gender(name: str, type_str: str = "") -> Tuple[str, Set[str], bool, bool]:
    r"""
    從公廁名稱中萃取性別、無障礙與親子設施資訊，並去除男/女/男廁/女廁/無障礙/親子等標記，產出乾淨的主體名稱 (Base Name)。
    回傳值：(base_name, detected_genders, is_accessible, is_family)

    支援模式：
    - 括號性別、無障礙與親子：(男), (女), (男女), （男廁）, (無障礙), (含無障礙), (含無障礙及親子), (親子), (含親子), (親子廁所) 等
    - 複合括號：(前站男廁) -> (前站), (後站無障礙親子廁所) -> (後站)
    - 結尾括號前綴詞：-女廁(含無障礙) -> '', 無障礙廁所(中油直營) -> (中油直營)
    - 結尾後綴：健康國小地下停車場男 -> 健康國小地下停車場, 泰安瀑布-無障礙廁所 -> 泰安瀑布, 1F西親子 -> 1F西, 文化館親子 -> 文化館, 1F戶外親子無障礙廁所-1 -> 1F戶外
    - 中間嵌入詞：女廁一 -> 一、1F女廁二 -> 1F二、特力家居南崁店女廁B1 -> 特力家居南崁店B1、1樓男長安西 -> 1樓長安西
    - 輔助型態：type 欄位包含 男廁所 / 女廁所 / 混合廁所 / 無障礙廁所 / 親子 等
    """
    if not name:
        return "", set(), False, False

    original = name.strip()
    detected_genders: Set[str] = set()
    is_accessible: bool = False
    is_family: bool = False
    cleaned = original

    # 0. 輔助 type 欄位檢測
    if type_str:
        if "無障礙" in type_str:
            is_accessible = True
        if "親子" in type_str or "尿布" in type_str:
            is_family = True
        if "男女" in type_str or "混合" in type_str:
            detected_genders.add("男女")
        elif "男" in type_str and "男女" not in type_str:
            detected_genders.add("男")
        elif "女" in type_str and "男女" not in type_str:
            detected_genders.add("女")

    if "無障礙" in original:
        is_accessible = True
    if "親子" in original or "尿布" in original:
        is_family = True

    # 1. 括號內的純性別 / 無障礙 / 親子標記
    def sub_paren_gender_acc_fam(m: re.Match) -> str:
        nonlocal is_accessible, is_family
        content = m.group(1).strip()
        if "無障礙" in content:
            is_accessible = True
        if "親子" in content or "尿布" in content:
            is_family = True
        if "男女" in content or ("男" in content and "女" in content):
            detected_genders.add("男女")
        elif "男" in content:
            detected_genders.add("男")
        elif "女" in content:
            detected_genders.add("女")
        return ""

    cleaned = re.sub(
        r'[\(（]\s*((?:(?:男女|男|女)?(?:廁(?:所)?)?(?:及|與|含|、)?(?:無障礙|親子)?(?:及|與|含|、)?(?:無障礙|親子)?(?:專用|設施|廁所|廁|友善|共用)?|無障礙(?:專用|設施|廁所|廁)?|含無障礙(?:及親子)?|親子(?:專用|設施|廁所|廁|友善)?|含親子(?:及無障礙)?))\s*[\)）]',
        sub_paren_gender_acc_fam,
        cleaned
    )

    # 處理含有其他字樣的括號，如 (前站男廁) -> (前站), (後站無障礙親子廁所) -> (後站)
    def paren_inner_sub(m: re.Match) -> str:
        nonlocal is_accessible, is_family
        full = m.group(0)
        inner = m.group(1)
        has_kw = False
        if "無障礙" in inner:
            is_accessible = True
            has_kw = True
        if "親子" in inner or "尿布" in inner:
            is_family = True
            has_kw = True
        if any(w in inner for w in ["男廁", "女廁", "男女廁", "男女", "男", "女"]):
            has_kw = True
            if "男女" in inner or ("男" in inner and "女" in inner):
                detected_genders.add("男女")
            elif "男" in inner:
                detected_genders.add("男")
            elif "女" in inner:
                detected_genders.add("女")
        if has_kw:
            new_inner = re.sub(
                r'[-_]?(?:男女廁(?:所)?|男廁(?:所)?|女廁(?:所)?|男女|[男女]|無障礙(?:專用|設施|廁所|廁)?|(?:及|與|含|、)?無障礙(?:專用|設施|廁所|廁)?|親子(?:專用|設施|廁所|廁|友善)?|(?:及|與|含|、)?親子(?:專用|設施|廁所|廁|友善)?)',
                '',
                inner
            ).strip()
            new_inner = re.sub(r'^[-_]+|[-_]+$', '', new_inner).strip()
            if new_inner:
                return f"({new_inner})"
            return ""
        return full

    cleaned = re.sub(r'[\(（]([^\)）]+)[\)）]', paren_inner_sub, cleaned)

    # 2. 處理結尾前綴或後綴之「無障礙」、「親子」與性別複合詞
    acc_fam_gender_tail_patterns = [
        # 親子 + 無障礙複合詞 (例如: 親子無障礙廁所-1, 1F戶外親子無障礙廁所-1, 無障礙及親子共用廁所 等)
        r'[-_~—\s]*(?:親子及無障礙|無障礙及親子|親子無障礙|無障礙親子)(?:親子共用廁所|專用廁所|設施廁所|專用|設施|廁所|廁|共用廁所|友善)?(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        # 無障礙複合詞
        r'[-_~—\s]*(?:男|女|男女)?(?:廁(?:所)?)?(?:及|與|含|、)無障礙(?:專用|設施|廁所|廁|及親子共用廁所|親子共用廁所)?(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        r'[-_~—\s]*無障礙(?:及|與|含|、)(?:男|女|男女)?(?:廁(?:所)?)?(?:專用|設施|廁所|廁|及親子共用廁所|親子共用廁所)?(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        r'[-_~—\s]*無障礙(?:親子共用廁所|專用廁所|設施廁所|專用|設施|廁所|廁)(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        r'[-_~—\s]*無障礙(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        # 親子複合詞 (例如: 1F西親子, 4F文化館親子, -親子廁所, 親子廁所-1, 親子1號 等)
        r'[-_~—\s]*(?:男|女|男女)?(?:廁(?:所)?)?(?:及|與|含|、)親子(?:專用|設施|廁所|廁|友善)?(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        r'[-_~—\s]*親子(?:及|與|含|、)(?:男|女|男女)?(?:廁(?:所)?)?(?:專用|設施|廁所|廁|友善)?(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        r'[-_~—\s]*親子(?:專用廁所|設施廁所|專用|設施|廁所|廁|友善廁所|友善)(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
        r'[-_~—\s]*親子(?:[-_]?[\d一二三四五六七八九十]+號?)?$',
    ]

    for pat in acc_fam_gender_tail_patterns:
        m = re.search(pat, cleaned)
        if m:
            matched_txt = m.group(0)
            if "無障礙" in matched_txt:
                is_accessible = True
            if "親子" in matched_txt or "尿布" in matched_txt:
                is_family = True
            if "男女" in matched_txt:
                detected_genders.add("男女")
            elif "男" in matched_txt:
                detected_genders.add("男")
            elif "女" in matched_txt:
                detected_genders.add("女")
            cleaned = cleaned[:m.start()]
            break

    # 3. 處理結尾性別後綴（例如 -男廁、_女廁所、(男)、男1、女2、1號男廁、男、女）
    tail_patterns = [
        (r'[-_~—\s]*(?:僅男廁|僅女廁|男女廁所|男女廁|混合廁所|性別友善廁所|性別友善)(?:[-_]?[\d一二三四五六七八九十]+號?)?$', '男女'),
        (r'[-_~—\s]*男廁所(?:[-_]?[\d一二三四五六七八九十]+號?)?$', '男'),
        (r'[-_~—\s]*女廁所(?:[-_]?[\d一二三四五六七八九十]+號?)?$', '女'),
        (r'[-_~—\s]*男廁(?:[-_]?[\d一二三四五六七八九十]+號?)?$', '男'),
        (r'[-_~—\s]*女廁(?:[-_]?[\d一二三四五六七八九十]+號?)?$', '女'),
        (r'[-_~—\s]*男女(?:[-_]?[\d一二三四五六七八九十]+號?)?$', '男女'),
        (r'[-_~—\s]+[男女]$', None),
        (r'[-_~—\s]*[男女](?:[-_]?[\d一二三四五六七八九十]+號?)$', None),
    ]

    for pat, g in tail_patterns:
        m = re.search(pat, cleaned)
        if m:
            matched_txt = m.group(0)
            if g:
                detected_genders.add(g)
            else:
                if "男女" in matched_txt:
                    detected_genders.add("男女")
                elif "男" in matched_txt:
                    detected_genders.add("男")
                elif "女" in matched_txt:
                    detected_genders.add("女")
            cleaned = cleaned[:m.start()]
            break

    # 4. 處理單字結尾「男」或「女」
    if cleaned.endswith("男") and not cleaned.endswith("指南") and not cleaned.endswith("台南") and not cleaned.endswith("臺南") and not cleaned.endswith("雲南") and not cleaned.endswith("江南") and not cleaned.endswith("嶺南") and not cleaned.endswith("勝男"):
        detected_genders.add("男")
        cleaned = cleaned[:-1]
    elif cleaned.endswith("女") and not cleaned.endswith("子女") and not cleaned.endswith("婦女") and not cleaned.endswith("仙女"):
        detected_genders.add("女")
        cleaned = cleaned[:-1]

    # 5. 處理名稱中間嵌入之性別、無障礙或親子標記
    def sub_infix_gender_acc_fam(m: re.Match) -> str:
        nonlocal is_accessible, is_family
        txt = m.group(0)
        if "無障礙" in txt:
            is_accessible = True
        if "親子" in txt or "尿布" in txt:
            is_family = True
        if "男女" in txt:
            detected_genders.add("男女")
        elif "男" in txt:
            detected_genders.add("男")
        elif "女" in txt:
            detected_genders.add("女")
        return ""

    cleaned = re.sub(
        r'[-_]?(?:男女廁(?:所)?|男廁(?:所)?|女廁(?:所)?|無障礙(?:專用|設施|廁所|廁)?|親子(?:專用|設施|廁所|廁)?)(?=[B\d一二三四五六七八九十]+(?:[Ff樓層號棟]|館|室|處|門|側|棟|站|區|$))',
        sub_infix_gender_acc_fam,
        cleaned
    )

    # 6. 清理結尾殘留符號
    cleaned = re.sub(r'[-_~—\s、,，/]+$', '', cleaned).strip()
    cleaned = re.sub(r'^[-_~—\s、,，/]+', '', cleaned).strip()
    cleaned = re.sub(r'[\(（]\s*[\)）]', '', cleaned).strip()

    # 備援防呆
    if not cleaned:
        cleaned = original

    return cleaned, detected_genders, is_accessible, is_family


def parse_coordinates(records: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """從紀錄群組中尋找並校驗有效的經緯度 [lng, lat]"""
    for r in records:
        loc = r.get("location")
        if isinstance(loc, dict) and "coordinates" in loc:
            coords = loc["coordinates"]
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                try:
                    lng = float(coords[0])
                    lat = float(coords[1])
                    if lat > 50.0 and lng < 50.0:
                        lat, lng = lng, lat
                    if -180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0:
                        return lng, lat
                except (ValueError, TypeError):
                    pass

        raw_lng = r.get("longitude") or r.get("lng") or r.get("經度")
        raw_lat = r.get("latitude") or r.get("lat") or r.get("緯度")
        if raw_lng is not None and raw_lat is not None:
            try:
                cur_lng = float(str(raw_lng).strip())
                cur_lat = float(str(raw_lat).strip())
                if cur_lat > 50.0 and cur_lng < 50.0:
                    cur_lat, cur_lng = cur_lng, cur_lat
                if -180.0 <= cur_lng <= 180.0 and -90.0 <= cur_lat <= 90.0:
                    return cur_lng, cur_lat
            except (ValueError, TypeError):
                continue
    return None, None


def analyze_same_address_different_names(
    docs: List[Dict[str, Any]],
    print_report: bool = True
) -> List[Dict[str, Any]]:
    """
    分析並找出「地址相同（或正規化地址相同）但主體名稱 (Base Name) 不同」的公廁群組。
    例如同一市政大樓/商場/轉運站內有不同單位的公廁。
    
    返回詳細分析清單，每筆包含：
    - normalized_address: 正規化地址
    - raw_addresses: 群組內出現的所有原始地址列表
    - venues: 包含的各個不同名稱公廁詳細資訊 (base_name, count, ids, tags, isAccessible, isFamily, hasToiletPaper)
    - total_records: 該地址下的總筆數
    """
    addr_groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for d in docs:
        raw_addr = str(d.get("address", "")).strip()
        raw_name = str(d.get("name", "")).strip()
        tags = d.get("tags") or []
        type_str = " ".join(str(t) for t in tags if "男" in str(t) or "女" in str(t) or "無障礙" in str(t) or "親子" in str(t))
        
        bname, _, _, _ = normalize_and_extract_gender(raw_name, type_str)
        norm_addr = normalize_address(raw_addr)

        if not norm_addr:
            continue

        addr_groups[norm_addr][bname].append(d)

    different_name_clusters: List[Dict[str, Any]] = []

    for norm_addr, name_map in addr_groups.items():
        if len(name_map) > 1:
            all_docs_in_addr = []
            venues_info = []
            all_raw_addrs = set()

            for bname, sub_docs in name_map.items():
                all_docs_in_addr.extend(sub_docs)
                for sd in sub_docs:
                    if sd.get("address"):
                        all_raw_addrs.add(str(sd["address"]).strip())

                has_acc = any(sd.get("isAccessible") is True or "無障礙" in (sd.get("tags") or []) for sd in sub_docs)
                has_fam = any("親子" in (sd.get("tags") or []) or "親子" in str(sd.get("name", "")) for sd in sub_docs)
                has_tp = any(sd.get("hasToiletPaper") is True for sd in sub_docs)
                ids = [str(sd.get("_id", "")) for sd in sub_docs if sd.get("_id")]

                venues_info.append({
                    "name": bname or "(無名稱)",
                    "record_count": len(sub_docs),
                    "document_ids": ids,
                    "isAccessible": has_acc,
                    "isFamily": has_fam,
                    "hasToiletPaper": has_tp,
                })

            cluster = {
                "normalized_address": norm_addr,
                "raw_addresses": list(all_raw_addrs),
                "distinct_name_count": len(name_map),
                "total_records": len(all_docs_in_addr),
                "venues": venues_info,
            }
            different_name_clusters.append(cluster)

    # 依照不同名稱數量降序排列
    different_name_clusters.sort(key=lambda x: x["distinct_name_count"], reverse=True)

    if print_report:
        print("\n" + "=" * 78)
        print("🏢 【地址相同但名稱不同】之公廁群組分析報告")
        print("=" * 78)
        print(f"📊 總計發現 {len(different_name_clusters)} 處地點存在「相同地址但不同主體名稱」的公廁：\n")

        if not different_name_clusters:
            print("  ✨ 未發現任何相同地址但不同名稱的公廁紀錄。")
        else:
            for idx, c in enumerate(different_name_clusters[:25], 1):
                raw_addr_str = " | ".join(c["raw_addresses"][:2])
                print(f"[{idx:02d}] 📍 地址：{c['normalized_address']}")
                if raw_addr_str and raw_addr_str != c['normalized_address']:
                    print(f"     原始地址：{raw_addr_str}")
                print(f"     包含 {c['distinct_name_count']} 個不同主體名稱 (共 {c['total_records']} 筆紀錄)：")
                for v in c["venues"]:
                    acc_badge = "♿ 無障礙" if v["isAccessible"] else ""
                    fam_badge = "👶 親子" if v.get("isFamily") else ""
                    tp_badge = "🧻 衛生紙" if v["hasToiletPaper"] else ""
                    badges = " ".join(b for b in [acc_badge, fam_badge, tp_badge] if b)
                    badge_str = f" [{badges}]" if badges else ""
                    print(f"       • {v['name']} ({v['record_count']} 筆){badge_str}")
                print("-" * 78)

            if len(different_name_clusters) > 25:
                print(f"  ... 另有 {len(different_name_clusters) - 25} 處同地址異名群組未在終端機完整展開。")
        print("=" * 78 + "\n")

    return different_name_clusters


def clean_existing_database(
    uri: str,
    db_name: str,
    collection_name: str = "toilets",
    dry_run: bool = False,
    batch_size: int = 500,
    show_diff_names_report: bool = True
) -> Dict[str, Any]:
    """
    直接讀取並清洗 MongoDB 資料庫中已存在的公廁資料：
    1. 抓取所有現存公廁 Document。
    2. 執行地址正規化 (normalize_address 去除村里鄰、統一臺/台、去除郵遞區號)。
    3. 依 (normalized_address, base_name) 分組，識別相同地點之男廁、女廁、無障礙廁所、親子廁所及鄰里重複紀錄。
    4. 執行主體名稱去除性別、無障礙與親子詞綴，並統一標註 '男女廁' / '僅男廁' / '僅女廁' / '無障礙' / '親子'。
    5. 友善設施與衛生紙衝突時，優先保留「有」（isAccessible=True, 親子標籤, hasToiletPaper=True）的一側。
    6. 保留各組 primary document 進行更新，並將其他重複 document 刪除。
    7. 自動更新關聯之 reviews 的 toiletId，保持參照完整性。
    8. 分析並印出「地址相同但名稱不同」的項目清單。
    9. 自動確認與建立 location 欄位之 2dsphere 空間索引。
    """
    if MongoClient is None:
        raise ImportError("未安裝 pymongo，請執行 pip install pymongo dnspython python-dotenv")

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

    if total_before == 0:
        print("⚠️ 資料庫中目前無公廁資料。")
        client.close()
        return {"total_before": 0, "total_after": 0}

    # 1. 執行「地址相同但名稱不同」的先期分析
    diff_name_clusters = analyze_same_address_different_names(all_docs, print_report=show_diff_names_report)

    # 2. 依 (normalized_address, base_name) 進行分組與同地點多筆合併
    grouped = defaultdict(list)
    for d in all_docs:
        raw_addr = str(d.get("address", "")).strip()
        raw_name = str(d.get("name", "")).strip()
        type_str = ""
        tags = d.get("tags") or []
        for t in tags:
            if "男" in str(t) or "女" in str(t) or "無障礙" in str(t) or "親子" in str(t):
                type_str += " " + str(t)

        bname, genders, is_acc, is_fam = normalize_and_extract_gender(raw_name, type_str)
        norm_addr = normalize_address(raw_addr)
        
        # 鍵值：(正規化地址, 清洗後主體名稱)
        grouped[(norm_addr, bname)].append((d, raw_addr, bname, genders, is_acc, is_fam))

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
        "accessible_count": 0,
        "family_count": 0,
        "toilet_paper_count": 0,
        "deleted_count": 0,
        "diff_name_clusters_count": len(diff_name_clusters),
    }

    now = datetime.now(timezone.utc)

    for (norm_addr, bname), items in grouped.items():
        all_genders = set()
        for d, r_addr, b, g, is_acc, is_fam in items:
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

        # 彙整標準地址：優先選取最乾淨且標準的地址
        clean_addr_candidate = clean_display_address(items[0][1])
        if not clean_addr_candidate:
            clean_addr_candidate = items[0][1]

        # 彙整所有 tags (過濾舊的性別標籤)
        combined_tags = []
        for d, r_addr, b, g, is_acc, is_fam in items:
            for t in (d.get("tags") or []):
                t_str = str(t).strip()
                if re.search(r'^\d*[Ff樓層]?(?:僅男廁|僅女廁|男女廁|混合性別|男廁(?:所)?|女廁(?:所)?|男女|[男女])$', t_str):
                    continue
                if t_str and t_str not in combined_tags:
                    combined_tags.append(t_str)

        if final_gender_tag:
            combined_tags.insert(1 if len(combined_tags) > 0 else 0, final_gender_tag)

        # 彙整無障礙 (Positive Priority)
        is_accessible = any(
            is_acc is True or
            d.get("isAccessible") is True or
            d.get("isAccessible") in ("true", "True", "1", 1) or
            "無障礙" in (d.get("tags") or []) or
            "無障礙" in str(d.get("type") or "") or
            "無障礙" in str(d.get("name") or "")
            for d, r_addr, b, g, is_acc, is_fam in items
        )
        if is_accessible:
            stats["accessible_count"] += 1
            if "無障礙" not in combined_tags:
                combined_tags.append("無障礙")

        # 彙整親子 (Positive Priority)
        has_family = any(
            is_fam is True or
            "親子" in (d.get("tags") or []) or
            "親子" in str(d.get("type") or "") or
            "親子" in str(d.get("name") or "") or
            str(d.get("diaper", "0")).strip() not in ("0", "", "無", "none", "None", "null")
            for d, r_addr, b, g, is_acc, is_fam in items
        )
        if has_family:
            stats["family_count"] += 1
            if "親子" not in combined_tags:
                combined_tags.append("親子")

        # 彙整衛生紙 (Positive Priority: 若任一紀錄標記為 True，則保留 True)
        has_tp: Optional[bool] = None
        for d, r_addr, b, g, is_acc, is_fam in items:
            tp_val = d.get("hasToiletPaper")
            if tp_val is True or tp_val in ("true", "True", "1", 1, "有"):
                has_tp = True
                break
            elif tp_val is False or tp_val in ("false", "False", "0", 0, "無"):
                if has_tp is None:
                    has_tp = False

        if has_tp is True:
            stats["toilet_paper_count"] += 1

        # 彙整 notes
        notes = []
        for d, r_addr, b, g, is_acc, is_fam in items:
            n = d.get("note")
            if n and str(n).strip():
                for part in str(n).split(" / "):
                    p_clean = part.strip()
                    if p_clean and p_clean not in notes:
                        notes.append(p_clean)
        merged_note = " / ".join(notes) if notes else None

        # 經緯度補齊與校驗
        lng, lat = parse_coordinates([d for d, r_addr, b, g, is_acc, is_fam in items])
        loc = primary_doc.get("location")
        if lng is not None and lat is not None:
            loc = {"type": "Point", "coordinates": [lng, lat]}

        # 評論數與最早建立時間
        total_reviews = sum(int(d.get("reviewCount", 0)) for d, r_addr, b, g, is_acc, is_fam in items)
        earliest_created = min((d.get("createdAt") for d, r_addr, b, g, is_acc, is_fam in items if d.get("createdAt")), default=now)

        district = extract_district(clean_addr_candidate)
        place_type = next((t for t in combined_tags if t not in ["男女廁", "僅男廁", "僅女廁", "無障礙", "親子", "特優級", "優等級", "普通級", "改善級"]), "")
        landmark = primary_doc.get("landmark")
        if district and place_type:
            landmark = f"{district}{place_type}"
        elif district:
            landmark = district

        update_fields = {
            "name": bname,
            "address": clean_addr_candidate,
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
            other_ids = [d["_id"] for d, r_addr, b, g, is_acc, is_fam in items[1:]]
            deletes_to_perform.extend(other_ids)
            stats["deleted_count"] += len(other_ids)
            for oid in other_ids:
                review_updates.append((oid, primary_id))
        else:
            stats["single_updates"] += 1

    print("\n📈 資料庫清洗與合併統計報告：")
    print(f"  • 資料庫原始公廁筆數: {stats['total_before']}")
    print(f"  • 清洗合併後獨立公廁: {stats['total_after']}")
    print(f"  • 識別重複並合併組數: {stats['merged_groups']}")
    print(f"  • 單筆公廁紀錄清洗數: {stats['single_updates']}")
    print(f"  • 預計移除多餘重複筆數: {stats['deleted_count']}")
    print(f"  • 標籤分佈 - 男女廁: {stats['both_gender_count']}")
    print(f"  • 標籤分佈 - 僅男廁: {stats['male_only_count']}")
    print(f"  • 標籤分佈 - 僅女廁: {stats['female_only_count']}")
    print(f"  • 標籤分佈 - 無性別標記: {stats['no_gender_count']}")
    print(f"  • 友善設施 - 無障礙設施: {stats['accessible_count']}")
    print(f"  • 友善設施 - 親子友善:   {stats['family_count']}")
    print(f"  • 提供衛生紙地點數:     {stats['toilet_paper_count']}")

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


# 程式化調用別名
clean_database = clean_existing_database


def clean_and_merge_records(
    records: List[Dict[str, Any]],
    output_format: str = "standard"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    純記憶體資料清洗函數（供單元測試或內部調用）：
    - 依 (normalize_address(addr), base_name) 分組
    - 移除性別/無障礙/親子詞綴，並指派對應 tags 與 isAccessible
    - 友善設施衝突保留「有」的一側
    """
    grouped = defaultdict(list)
    for r in records:
        addr = str(r.get("address") or r.get("公廁地址") or "").strip()
        raw_name = str(r.get("name") or r.get("公廁名稱") or "").strip()
        type_str = str(r.get("type") or r.get("公廁類別") or "").strip()

        raw_tags = r.get("tags") or []
        for t in raw_tags:
            if "男" in str(t) or "女" in str(t) or "無障礙" in str(t) or "親子" in str(t):
                type_str += " " + str(t)

        base_name, genders, is_acc, is_fam = normalize_and_extract_gender(raw_name, type_str)
        norm_addr = normalize_address(addr)

        grouped[(norm_addr, base_name)].append({
            "raw": r,
            "raw_address": addr,
            "base_name": base_name,
            "genders": genders,
            "is_accessible": is_acc,
            "is_family": is_fam,
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
        "accessible_count": 0,
        "family_count": 0,
        "toilet_paper_count": 0,
        "skipped_no_coords": 0,
    }

    now = datetime.now(timezone.utc)

    for (norm_addr, base_name), items in grouped.items():
        if not base_name and not norm_addr:
            continue

        all_genders: Set[str] = set()
        for it in items:
            all_genders.update(it["genders"])

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
        clean_addr = clean_display_address(items[0]["raw_address"]) or items[0]["raw_address"]

        lng, lat = parse_coordinates([it["raw"] for it in items])
        if output_format == "standard" and (lng is None or lat is None):
            stats["skipped_no_coords"] += 1
            continue

        tags: List[str] = []
        for it in items:
            raw_t = it["raw"].get("tags")
            if isinstance(raw_t, list):
                for t in raw_t:
                    t_str = str(t).strip()
                    if re.search(r'^\d*[Ff樓層]?(?:僅男廁|僅女廁|男女廁|混合性別|男廁(?:所)?|女廁(?:所)?|男女|[男女])$', t_str):
                        continue
                    if t_str and t_str not in tags:
                        tags.append(t_str)

        if gender_tag:
            tags.insert(1 if len(tags) > 0 else 0, gender_tag)

        place_types: List[str] = []
        has_accessible = False
        has_family = False
        has_tp: Optional[bool] = None
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

            if it.get("is_accessible") or "無障礙" in tp or "無障礙" in nm or raw_doc.get("isAccessible") is True or "無障礙" in (raw_doc.get("tags") or []):
                has_accessible = True
            if it.get("is_family") or diaper not in ("0", "", "無", "none", "None", "null") or "親子" in tp or "親子" in nm or "親子" in (raw_doc.get("tags") or []):
                has_family = True
            if grade:
                grades.append(grade)
            if admin:
                admins.append(admin)
            if exec_unit:
                execs.append(exec_unit)

            tp_val = raw_doc.get("hasToiletPaper")
            if tp_val is True or tp_val in ("true", "True", "1", 1, "有"):
                has_tp = True
            elif tp_val is False or tp_val in ("false", "False", "0", 0, "無"):
                if has_tp is None:
                    has_tp = False

        if has_accessible:
            stats["accessible_count"] += 1
        if has_family:
            stats["family_count"] += 1
        if has_tp is True:
            stats["toilet_paper_count"] += 1

        if place_types:
            tags.extend(place_types)
        if has_accessible and "無障礙" not in tags:
            tags.append("無障礙")
        if has_family and "親子" not in tags:
            tags.append("親子")
        if grades:
            tags.extend(grades)

        tags = list(dict.fromkeys(tags))

        district = extract_district(clean_addr)
        main_place_type = place_types[0] if place_types else ""
        if district and main_place_type:
            landmark = f"{district}{main_place_type}"
        elif district:
            landmark = district
        elif main_place_type:
            landmark = main_place_type
        else:
            landmark = first_raw.get("landmark") or None

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

        total_reviews = sum(int(it["raw"].get("reviewCount", 0)) for it in items)
        earliest_created = min((it["raw"].get("createdAt") for it in items if it["raw"].get("createdAt")), default=now)

        doc: Dict[str, Any] = {
            "name": base_name,
            "location": {
                "type": "Point",
                "coordinates": [lng, lat],
            },
            "address": clean_addr,
            "hasToiletPaper": has_tp,
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


def main():
    parser = argparse.ArgumentParser(
        description="Bathroom Genius - MongoDB 公廁資料庫清洗與去重合併工具 (clean_data.py)",
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
        "--collection",
        "-c",
        default="toilets",
        help="目標 MongoDB Collection 名稱 (預設: toilets)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="僅執行清洗、分組與合併校驗，不寫入或刪除資料庫中的 Document",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="批次寫入每批處理數量 (預設: 500)",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="僅列出地址相同但名稱不同之公廁分析報告，不執行資料庫清洗",
    )

    args = parser.parse_args()

    print("=" * 75)
    print("🚽 Bathroom Genius - MongoDB 公廁資料庫清洗與去重合併工具 (clean_data.py)")
    print("=" * 75)

    env_path = Path(args.env_file).resolve() if args.env_file else Path(__file__).resolve().parent / ".env"
    load_environment_variables(env_path)
    mongo_uri = args.uri or os.getenv("MONGODB_URI")
    mongo_db_name = args.db or os.getenv("MONGODB_DB_NAME", "bathroom_online")

    if not mongo_uri:
        print("❌ 未設定 MONGODB_URI，無法連線至資料庫。請確認 .env 檔案或使用 --uri 參數傳入。")
        sys.exit(1)

    if args.report_only:
        print("🔍 模式：僅產出同地址異名分析報告...")
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
        docs = list(client[mongo_db_name][args.collection].find({}))
        analyze_same_address_different_names(docs, print_report=True)
        client.close()
        return

    clean_existing_database(
        uri=mongo_uri,
        db_name=mongo_db_name,
        collection_name=args.collection,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        show_diff_names_report=True
    )
    print("\n🎉 資料庫清洗作業順利完成！")


if __name__ == "__main__":
    main()
