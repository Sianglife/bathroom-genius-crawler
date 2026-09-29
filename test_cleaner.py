#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit and Integration Tests for Bathroom Genius Crawler & Cleaner
"""

import unittest
from clean_data import (
    normalize_address,
    clean_display_address,
    normalize_and_extract_gender,
    clean_and_merge_records,
    analyze_same_address_different_names
)


class TestAddressNormalization(unittest.TestCase):
    def test_village_and_neighborhood_removal(self):
        # 1. 相同地點，僅差在鄰里有無
        addr1 = "臺北市中正區建國里重慶南路一段122號"
        addr2 = "臺北市中正區重慶南路一段122號"
        addr3 = "台北市中正區10鄰重慶南路一段122號"
        addr4 = "10048臺北市中正區建國里10鄰重慶南路一段122號"

        norm1 = normalize_address(addr1)
        norm2 = normalize_address(addr2)
        norm3 = normalize_address(addr3)
        norm4 = normalize_address(addr4)

        self.assertEqual(norm1, norm2)
        self.assertEqual(norm2, norm3)
        self.assertEqual(norm3, norm4)
        self.assertEqual(norm1, "臺北市中正區重慶南路一段122號")

    def test_various_taiwan_addresses(self):
        # 2. 新北市板橋區留侯里10鄰府中路30號 vs 府中路30號
        n1 = normalize_address("新北市板橋區留侯里10鄰府中路30號")
        n2 = normalize_address("新北市板橋區府中路30號")
        self.assertEqual(n1, n2)
        self.assertEqual(n1, "新北市板橋區府中路30號")

        # 3. 宜蘭市神農里15鄰復興路一段1號
        n3 = normalize_address("宜蘭縣宜蘭市神農里15鄰復興路一段1號")
        n4 = normalize_address("宜蘭縣宜蘭市復興路一段1號")
        self.assertEqual(n3, n4)

        # 4. 花蓮縣吉安鄉仁和村1鄰慶豐街50號
        n5 = normalize_address("花蓮縣吉安鄉仁和村1鄰慶豐街50號")
        n6 = normalize_address("花蓮縣吉安鄉慶豐街50號")
        self.assertEqual(n5, n6)

        # 5. 鄉鎮名為「富里鄉」不可被誤切
        n7 = normalize_address("花蓮縣富里鄉新興村中山路10號")
        self.assertEqual(n7, "花蓮縣富里鄉中山路10號")


class TestGenderAccessibilityAndFamilyExtraction(unittest.TestCase):
    def test_gender_removal(self):
        name, genders, is_acc, is_fam = normalize_and_extract_gender("大安森林公園-男廁")
        self.assertEqual(name, "大安森林公園")
        self.assertIn("男", genders)
        self.assertFalse(is_acc)
        self.assertFalse(is_fam)

        name, genders, is_acc, is_fam = normalize_and_extract_gender("松山文創園區(女廁)")
        self.assertEqual(name, "松山文創園區")
        self.assertIn("女", genders)
        self.assertFalse(is_fam)

    def test_accessibility_removal(self):
        name, genders, is_acc, is_fam = normalize_and_extract_gender("新店區公所-無障礙廁所")
        self.assertEqual(name, "新店區公所")
        self.assertTrue(is_acc)
        self.assertFalse(is_fam)

        name, genders, is_acc, is_fam = normalize_and_extract_gender("泰安瀑布(含無障礙)")
        self.assertEqual(name, "泰安瀑布")
        self.assertTrue(is_acc)

        name, genders, is_acc, is_fam = normalize_and_extract_gender("臺北市立圖書館女廁及無障礙廁所")
        self.assertEqual(name, "臺北市立圖書館")
        self.assertTrue(is_acc)
        self.assertIn("女", genders)

    def test_family_removal(self):
        # 1. 結尾親子
        name, genders, is_acc, is_fam = normalize_and_extract_gender("國立臺灣科學教育館1F西親子")
        self.assertEqual(name, "國立臺灣科學教育館1F西")
        self.assertTrue(is_fam)

        name, genders, is_acc, is_fam = normalize_and_extract_gender("臺北流行音樂中心4F文化館親子")
        self.assertEqual(name, "臺北流行音樂中心4F文化館")
        self.assertTrue(is_fam)

        # 2. 親子無障礙複合詞
        name, genders, is_acc, is_fam = normalize_and_extract_gender("臺北流行音樂中心1F戶外親子無障礙廁所-1")
        self.assertEqual(name, "臺北流行音樂中心1F戶外")
        self.assertTrue(is_acc)
        self.assertTrue(is_fam)

        # 3. 括號親子
        name, genders, is_acc, is_fam = normalize_and_extract_gender("板橋車站(含無障礙及親子)")
        self.assertEqual(name, "板橋車站")
        self.assertTrue(is_acc)
        self.assertTrue(is_fam)


class TestMergeRecordsWithSimilarAddressAndPositivePriority(unittest.TestCase):
    def test_merge_similar_address_records(self):
        records = [
            {
                "name": "總統府-男廁",
                "address": "臺北市中正區建國里重慶南路一段122號",
                "longitude": 121.5119,
                "latitude": 25.0400,
                "hasToiletPaper": True,
                "isAccessible": False,
            },
            {
                "name": "總統府-女廁",
                "address": "臺北市中正區重慶南路一段122號",
                "longitude": 121.5119,
                "latitude": 25.0400,
                "hasToiletPaper": None,
                "isAccessible": False,
            },
            {
                "name": "總統府(無障礙廁所)",
                "address": "10048臺北市中正區10鄰重慶南路一段122號",
                "longitude": 121.5119,
                "latitude": 25.0400,
                "hasToiletPaper": False,
                "isAccessible": True,
            },
            {
                "name": "總統府-親子廁所",
                "address": "臺北市中正區重慶南路一段122號",
                "longitude": 121.5119,
                "latitude": 25.0400,
                "hasToiletPaper": None,
                "isAccessible": False,
            }
        ]

        cleaned, stats = clean_and_merge_records(records)
        self.assertEqual(len(cleaned), 1)
        merged = cleaned[0]

        # 驗證名稱去除綴詞
        self.assertEqual(merged["name"], "總統府")
        # 驗證性別標籤涵蓋男女
        self.assertIn("男女廁", merged["tags"])
        # 驗證無障礙標籤與 isAccessible (Positive Priority)
        self.assertTrue(merged["isAccessible"])
        self.assertIn("無障礙", merged["tags"])
        # 驗證親子標籤
        self.assertIn("親子", merged["tags"])
        # 驗證衛生紙保留 True (Positive Priority)
        self.assertTrue(merged["hasToiletPaper"])
        # 驗證地址正規化
        self.assertEqual(merged["address"], "臺北市中正區重慶南路一段122號")


class TestSameAddressDifferentNamesAnalysis(unittest.TestCase):
    def test_different_names_same_address(self):
        docs = [
            {
                "_id": "id1",
                "name": "臺北市政府市政大樓-1F男廁",
                "address": "臺北市信義區市府路1號",
                "tags": ["男女廁"],
                "isAccessible": False,
                "hasToiletPaper": True,
            },
            {
                "_id": "id2",
                "name": "臺北探索館(無障礙及親子)",
                "address": "臺北市信義區市府路1號",
                "tags": ["無障礙", "親子"],
                "isAccessible": True,
                "hasToiletPaper": False,
            },
            {
                "_id": "id3",
                "name": "信義區公所",
                "address": "臺北市信義區福德里市府路1號",
                "tags": [],
                "isAccessible": False,
                "hasToiletPaper": None,
            },
        ]

        clusters = analyze_same_address_different_names(docs, print_report=False)
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual(c["normalized_address"], "臺北市信義區市府路1號")
        self.assertEqual(c["distinct_name_count"], 3)
        venue_names = [v["name"] for v in c["venues"]]
        self.assertIn("臺北市政府市政大樓-1F", venue_names)
        self.assertIn("臺北探索館", venue_names)
        self.assertIn("信義區公所", venue_names)


if __name__ == "__main__":
    unittest.main()
