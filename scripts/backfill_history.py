#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
棚卸し調査（audit_all_swimmers.py / audit_remaining_swimmers.py）で見つかった、
手元に無い過去の記録を、実際にロスター(index.html)へ反映する一回限りのスクリプト。

全選手について、スイムレコードモバイル側の全年度の記録を取得し、
merge_swimmer()（週次チェックで使っているのと同じ、安全な重複チェック・
ベストタイム再計算・保存を行う実績のある関数）にそのまま渡す。merge_swimmer
の内部で、既にロスターにある記録は自動的にスキップされるので、同じ選手を
何度処理しても安全（二重に追加されることはない）。

このスクリプトは index.html を実際に書き換える（commit/push は行わない。
内容を確認した上で、いつも通りGitHub Desktopでコミット・プッシュすること）。
LINE通知も行わない。

全選手×全年度分の取得が必要なため、棚卸し調査と同程度の時間（30分〜1時間超）
がかかる見込み。

使い方（PowerShellから）:
  python scripts\\backfill_history.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from audit_all_swimmers import audit_swimmer
from check_and_update import LOGIN_ID, LOGIN_PW, ROSTER_PATH, do_login, log
from merge_lib import load_roster
from merge_swimrecord import merge_swimmer


def main():
    if not LOGIN_ID or not LOGIN_PW:
        log("エラー: SWIMRECORD_ID / SWIMRECORD_PASSWORD が設定されていません。")
        sys.exit(1)

    _, _, roster_data = load_roster(ROSTER_PATH)
    log(f"対象: ロスター全{len(roster_data)}名")

    results = []

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

        for swimmer in roster_data:
            name = swimmer["name"]
            log(f"=== {name} ===")
            try:
                all_entries = audit_swimmer(page, name)
            except Exception as e:
                log(f"  ※ エラーでスキップ: {e}")
                results.append((name, None, str(e)))
                continue

            if all_entries is None:
                log("  見つかりませんでした（スキップ）。")
                results.append((name, None, "not_found"))
                continue

            report = merge_swimmer(ROSTER_PATH, name, all_entries, dry_run=False)
            if "error" in report:
                log(f"  ※ {report['error']}")
                results.append((name, None, report["error"]))
                continue

            log(
                f"  追加: {report['added']}件 / 重複: {report['dup']}件 / "
                f"リレー除外: {report['relay_skip']}件 / "
                f"タイム記載無し除外: {report['invalid_time_skip']}件"
            )
            if report["invalid_time_entries"]:
                log("  ※ タイムが「-」等で記録されていなかったため除外（失格・欠場等と思われます。要確認):")
                for e in report["invalid_time_entries"]:
                    log(f"      {e['date']} {e['style']} {e['distance']} time={e['time']!r} {e['game_raw']}")
            if report["auto_cleaned"]:
                log("  ※ 大会名が自動クリーニングされました（要確認）:")
                for raw, canon in report["auto_cleaned"]:
                    log(f"      {raw}  →  {canon}")
            results.append((name, report["added"], None))

        browser.close()

    log("")
    log("=== 全体サマリー ===")
    total_added = 0
    for name, added, err in results:
        if err:
            log(f"  {name}: スキップ ({err})")
        else:
            total_added += added
            log(f"  {name}: {added}件 追加")
    log(f"\n合計: {total_added}件 追加しました。")
    log("")
    log(
        "index.htmlを更新しました。内容を確認の上、"
        "いつも通りGitHub Desktopでコミット・プッシュしてください。"
    )
    log("")
    log("EXIT: backfill_done")


if __name__ == "__main__":
    main()
