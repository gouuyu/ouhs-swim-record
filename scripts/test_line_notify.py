#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE通知が正しく届くかどうかを確認するための、テスト専用スクリプト。
check_and_update.py本番の新記録通知と見分けられるよう、メッセージの先頭に
「【テスト】」と付けて送る。index.htmlへの書き込みや、スイムレコードモバイル・
パラ水泳連盟データベースへのアクセスは一切行わない（純粋にLINE通知の送信
テストのみ。すぐ終わる）。

今回追加したパラ記録通知のセクション（build_notification_textのpara_notify
引数）の見え方も、実際にLINEで届く形で確認できる。

使い方（PowerShellから、check_and_update.pyと同じ環境変数のまま）:
  python scripts\\test_line_notify.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_and_update import build_notification_text
from line_notify import send_line_broadcast

# 通常の新記録通知と、パラ記録の通知セクションを両方含んだサンプル（テストデータ）
sample_results_with_new = [
    {
        "name": "山田 太郎（テストデータ）",
        "added": 2,
        "added_entries": [
            {"date": "11/18", "style": "自由形", "distance": "50m", "time": "0:25.00", "game": "テスト大会"},
            {"date": "11/18", "style": "背泳ぎ", "distance": "100m", "time": "1:05.00", "game": "テスト大会"},
        ],
    },
]
sample_para_notify = [
    (
        "宇津木 美都（テストデータ）",
        [
            {"date": "11/18", "style": "自由形", "distance": "50m", "cls": "S9", "time": "0:32.00", "game": "テストパラ大会"},
        ],
    ),
]

text = build_notification_text(sample_results_with_new, sample_para_notify)
text = text.replace(
    "【OUHS SWIM TEAM】新しい記録が見つかりました",
    "【テスト】LINE通知の確認テストです（実際の新記録ではありません）",
    1,
)

print(text)
print()
ok = send_line_broadcast(text)
print("EXIT:", "line_test_sent" if ok else "line_test_failed_or_skipped")
