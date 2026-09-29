#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bathroom Genius - Main Data Pipeline Orchestrator (main.py)
全自動公廁資料爬取、匯入與資料庫清洗整合管線

【核心流程 (Pipeline)】
1. Step 1: 🌐 Import MOENV API (環境部開放資料 API 爬取與匯入)
2. Step 2: 🏙️  Import Data Taipei (臺北市政府 Data.Taipei 開放資料匯入)
3. Step 3: 🧹 Clean Data (MongoDB 資料庫同地點/鄰里去重合併、男女廁與無障礙設施正規化、衛生紙優選、同地址異名分析)

【架構設計原則】
- import_*.py 專職負責外部資料源的擷取與匯入至 MongoDB。
- clean_data.py 專職負責 MongoDB 資料庫內部資料的清洗、同地點合併、標籤正規化與統計分析。
- main.py 作為統籌管線，依序調度執行各步驟，並產出綜合執行報告。
"""

import os
import sys
import time
import argparse
from pathlib import Path
from typing import Optional, Dict, Any

# 確保 Windows 主控台輸出繁體中文與表情符號不會拋出 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 匯入各模組功能
import import_moenv_api
import import_datataipei_json
import clean_data


def run_pipeline(
    env_file: Optional[str] = None,
    uri: Optional[str] = None,
    db: Optional[str] = None,
    collection: str = "toilets",
    skip_moenv: bool = False,
    skip_taipei: bool = False,
    skip_clean: bool = False,
    dry_run: bool = False,
    taipei_file: str = "data/taipei_datataipei.json",
    moenv_url: Optional[str] = None,
    moenv_max_pages: Optional[int] = None,
    batch_size: int = 500,
    save_moenv_json: Optional[str] = None,
) -> Dict[str, Any]:
    """執行全流程公廁資料管線"""
    pipeline_start_time = time.time()

    # 1. 載入環境變數
    env_path = Path(env_file).resolve() if env_file else Path(__file__).resolve().parent / ".env"
    clean_data.load_environment_variables(env_path)

    mongo_uri = uri or os.getenv("MONGODB_URI")
    mongo_db_name = db or os.getenv("MONGODB_DB_NAME", "bathroom_online")

    print("\n" + "=" * 80)
    print("🚽  BATHROOM GENIUS - 全自動公廁資料爬取、匯入與清洗管線 (Main Pipeline)")
    print("=" * 80)
    print(f"📌 目標資料庫 : {mongo_db_name}")
    print(f"📌 目標集合   : {collection}")
    print(f"📌 模式       : {'🔍 [Dry-Run 模擬模式]' if dry_run else '🚀 [實際執行模式]'}")
    print("=" * 80 + "\n")

    summary_stats = {
        "moenv": None,
        "taipei": None,
        "clean": None,
        "duration_seconds": 0.0,
    }

    # --------------------------------------------------------------------------
    # Step 1: Import MOENV API
    # --------------------------------------------------------------------------
    if skip_moenv:
        print("⏭️  [Step 1/3] 跳過環境部 MOENV API 抓取與匯入 (--skip-moenv)\n")
    else:
        print("🔹" * 40)
        print("🌐 [Step 1/3] 執行環境部 (MOENV API v2) 開放資料抓取與匯入...")
        print("🔹" * 40)
        try:
            moenv_stats = import_moenv_api.run_import(
                env_file=env_file,
                uri=mongo_uri,
                db=mongo_db_name,
                collection=collection,
                url=moenv_url,
                max_pages=moenv_max_pages,
                skip_duplicates=True,
                update_existing=False,
                dry_run=dry_run,
                save_json=save_moenv_json,
                batch_size=batch_size
            )
            summary_stats["moenv"] = moenv_stats
            print("✅ [Step 1/3] 環境部 MOENV API 處理完成！\n")
        except Exception as e:
            print(f"❌ [Step 1/3] 環境部 MOENV API 匯入失敗：{e}\n")

    # --------------------------------------------------------------------------
    # Step 2: Import Data.Taipei JSON
    # --------------------------------------------------------------------------
    if skip_taipei:
        print("⏭️  [Step 2/3] 跳過臺北市 Data.Taipei 資料匯入 (--skip-taipei)\n")
    else:
        print("🔹" * 40)
        print("🏙️  [Step 2/3] 執行臺北市政府 (Data.Taipei) 公廁開放資料匯入...")
        print("🔹" * 40)
        try:
            taipei_stats = import_datataipei_json.run_import(
                file_path=taipei_file,
                env_file=env_file,
                uri=mongo_uri,
                db=mongo_db_name,
                collection=collection,
                drop=False,
                dry_run=dry_run,
                batch_size=batch_size
            )
            summary_stats["taipei"] = taipei_stats
            print("✅ [Step 2/3] 臺北市 Data.Taipei 資料處理完成！\n")
        except Exception as e:
            print(f"❌ [Step 2/3] 臺北市 Data.Taipei 匯入失敗：{e}\n")

    # --------------------------------------------------------------------------
    # Step 3: Clean Data in MongoDB
    # --------------------------------------------------------------------------
    if skip_clean:
        print("⏭️  [Step 3/3] 跳過資料庫清洗與去重合併 (--skip-clean)\n")
    else:
        print("🔹" * 40)
        print("🧹 [Step 3/3] 執行 MongoDB 資料庫資料清洗、去重合併與分析...")
        print("   - 處理男女廁與無障礙後綴詞綴")
        print("   - 名稱相同且地址相似 (去鄰里/村里/郵遞區號) 自動合併")
        print("   - 友善設施與衛生紙衝突時優先保留「有」的一側 (Positive Priority)")
        print("   - 列出地址相同但名稱不同之公廁群組報告")
        print("🔹" * 40)
        try:
            if not mongo_uri:
                raise ValueError("未設定 MONGODB_URI，無法執行資料庫清洗。")

            clean_stats = clean_data.clean_database(
                uri=mongo_uri,
                db_name=mongo_db_name,
                collection_name=collection,
                dry_run=dry_run,
                batch_size=batch_size,
                show_diff_names_report=True
            )
            summary_stats["clean"] = clean_stats
            print("✅ [Step 3/3] 資料庫清洗與合併完成！\n")
        except Exception as e:
            print(f"❌ [Step 3/3] 資料庫清洗失敗：{e}\n")

    # --------------------------------------------------------------------------
    # 總結報告 (Pipeline Summary Dashboard)
    # --------------------------------------------------------------------------
    duration = time.time() - pipeline_start_time
    summary_stats["duration_seconds"] = duration

    print("=" * 80)
    print("📊 BATHROOM GENIUS - 資料處理管線全流程執行報告 (Pipeline Summary)")
    print("=" * 80)
    print(f"⏱️  總執行耗時: {duration:.2f} 秒")

    if summary_stats["moenv"]:
        m = summary_stats["moenv"]
        print(f"🌐 [MOENV API]   原始抓取: {m.get('raw_records', 0):>6} 筆 | 產出: {m.get('valid_docs', 0):>6} 筆 | 新增入庫: {m.get('inserted', 0):>6} 筆 | 略過重複: {m.get('skipped', 0):>6} 筆")

    if summary_stats["taipei"]:
        t = summary_stats["taipei"]
        print(f"🏙️  [Data.Taipei] 原始紀錄: {t.get('raw_records', 0):>6} 筆 | 產出: {t.get('valid_docs', 0):>6} 筆 | Upsert新增: {t.get('upserted', 0):>6} 筆 | 更新: {t.get('modified', 0):>6} 筆")

    if summary_stats["clean"]:
        c = summary_stats["clean"]
        print(f"🧹 [DB Cleaning] 清洗前總數: {c.get('total_before', 0):>6} 筆 ➡️ 清洗後獨立公廁: {c.get('total_after', 0):>6} 筆")
        print(f"                 - 識別重複並合併組數 : {c.get('merged_groups', 0):>5} 組")
        print(f"                 - 移除多餘重複筆數   : {c.get('deleted_count', 0):>5} 筆")
        print(f"                 - 標籤：男女廁 {c.get('both_gender_count', 0)} / 僅男廁 {c.get('male_only_count', 0)} / 僅女廁 {c.get('female_only_count', 0)}")
        print(f"                 - 友善設施：無障礙 {c.get('accessible_count', 0)} 處 / 親子友善 {c.get('family_count', 0)} 處 / 提供衛生紙 {c.get('toilet_paper_count', 0)} 處")
        print(f"                 - 同地址異名公廁群組 : {c.get('diff_name_clusters_count', 0)} 處")

    print("=" * 80)
    print("🎉 Bathroom Genius 資料管線執行圓滿完成！\n")

    return summary_stats


def main():
    parser = argparse.ArgumentParser(
        description="Bathroom Genius - 公廁資料抓取、匯入與清洗全自動管線 (main.py)",
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
        help="全流程模擬執行，不實際寫入或刪除資料庫中的 Document",
    )
    parser.add_argument(
        "--skip-moenv",
        action="store_true",
        help="跳過 Step 1: 環境部 MOENV API 抓取與匯入",
    )
    parser.add_argument(
        "--skip-taipei",
        action="store_true",
        help="跳過 Step 2: 臺北市 Data.Taipei 資料匯入",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="跳過 Step 3: 資料庫清洗與同地點去重合併",
    )
    parser.add_argument(
        "--taipei-file",
        default="data/taipei_datataipei.json",
        help="Data.Taipei JSON 檔案路徑 (預設: data/taipei_datataipei.json)",
    )
    parser.add_argument(
        "--moenv-url",
        default=None,
        help="自訂環境部單一 API Endpoint URL (預設抓取全台 23 個縣市端點)",
    )
    parser.add_argument(
        "--moenv-max-pages",
        type=int,
        default=None,
        help="限制環境部 API 每個端點翻頁抓取上限頁數 (測試用)",
    )
    parser.add_argument(
        "--save-moenv-json",
        default=None,
        help="將 MOENV 抓取合併後的資料備份至本機 JSON 檔案",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="批次寫入每批處理數量 (預設: 500)",
    )

    args = parser.parse_args()

    run_pipeline(
        env_file=args.env_file,
        uri=args.uri,
        db=args.db,
        collection=args.collection,
        skip_moenv=args.skip_moenv,
        skip_taipei=args.skip_taipei,
        skip_clean=args.skip_clean,
        dry_run=args.dry_run,
        taipei_file=args.taipei_file,
        moenv_url=args.moenv_url,
        moenv_max_pages=args.moenv_max_pages,
        batch_size=args.batch_size,
        save_moenv_json=args.save_moenv_json,
    )


if __name__ == "__main__":
    main()
