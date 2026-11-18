#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_all_swimmers.py で「見つかりませんでした」になった7名だけを対象に、
search_and_open_swimmer の修正（検索結果のページ送り対応・漢字異体字対応・
上原心選手の所属例外）を反映した上で再調査する追加スクリプト。

結果は audit_report_remaining.txt に書き出す（audit_report.txt とは別ファイル。
51名分を audit_report.txt（44名）+ audit_report_remaining.txt（この7名）の
2つでカバーする）。index.htmlへの書き込み・commit/push・LINE通知は行わない
（読み取り専用）。

使い方（PowerShellから）:
  python scripts\\audit_remaining_swimmers.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from audit_all_swimmers import audit_swimmer, compute_missing
from check_and_update import LOGIN_ID, LOGIN_PW, ROSTER_PATH, do_login, existing_keys_for, log
from merge_lib import load_roster

TARGET_NAMES = [
    "早﨑 愛莉",
    "伊藤 大智",
    "岡﨑 一彗",
    "桒野 元輝",
    "德永 央",
    "藤田 涼",
    "上原 心",
]

REPORT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "audit_report_remaining.txt"
)


def main():
    if not LOGIN_ID or not LOGIN_PW:
        log("エラー: SWIMRECORD_ID / SWIMRECORD_PASSWORD が設定されていません。")
        sys.exit(1)

    _, _, roster_data = load_roster(ROSTER_PATH)
    roster_by_name = {s["name"]: s for s in roster_data}

    report_lines = [
        f"=== 再調査レポート（旧ロジックで見つからなかった7名）({datetime.date.today():%Y-%m-%d}) ===",
        "",
    ]
    summary = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 412, "height": 915},
        )
        page = context.new_page()
        try:
            do_login(page)
        except Exception as e:
            log(f"ログイン処理でエラー: {e}")
            browser.close()
            sys.exit(1)

        for name in TARGET_NAMES:
            swimmer = roster_by_name.get(name)
            if swimmer is None:
                log(f"=== {name} ===\n  ※ ロスターに見つかりません（スキップ）。")
                summary.append((name, None, None, "not_in_roster"))
                continue

            existing_keys = existing_keys_for(swimmer)
            log(f"=== {name} (手元: {len(swimmer.get('history', []))}件) ===")
            try:
                all_entries = audit_swimmer(page, name)
            except Exception as e:
                log(f"  ※ エラーで調査をスキップしました: {e}")
                summary.append((name, None, None, str(e)))
                continue

            if all_entries is None:
                log("  今回も見つかりませんでした（スキップ）。")
                summary.append((name, None, None, "not_found"))
                continue

            missing, found_count, relay_count, dup_count = compute_missing(
                all_entries, existing_keys
            )
            log(f"  サイト側個人種目: {found_count}件 / 手元に無い: {len(missing)}件")
            summary.append((name, found_count, len(missing), None))

            report_lines.append(
                f"■ {name}  (手元: {len(swimmer.get('history', []))}件 / "
                f"サイト側個人種目: {found_count}件 / 手元に無い: {len(missing)}件)"
            )
            for e in missing:
                report_lines.append(
                    f"  {e['date']} {e['style']} {e['distance']} {e['time']} {e['game_raw']}"
                )
            report_lines.append("")

        browser.close()

    log("")
    log("=== サマリー ===")
    total_missing = 0
    for name, found_count, missing_count, err in summary:
        if err:
            log(f"  {name}: スキップ ({err})")
        else:
            total_missing += missing_count
            log(f"  {name}: 手元に無い {missing_count}件 / サイト側 {found_count}件")
    log(f"\n合計: 手元に無い記録 {total_missing}件")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    log(f"\n詳細レポートを書き出しました: {REPORT_PATH}")

    log("")
    log("EXIT: audit_done")


if __name__ == "__main__":
    main()
