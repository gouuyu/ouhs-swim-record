#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
既存のロスター(index.html)にある記録のうち、まだラップタイム・リアクションタイムを
取得できていないもの（2026-11、詳細ページ取得の不具合でほぼ全選手・全記録が
取得できていなかった）を、全選手・全年度についてまとめて拾い直す一回限りの
スクリプト。

仕組み:
  check_and_update.py の fetch_detail_for_new_entries() は、2026-11の修正で
  「新規の記録」だけでなく「既存の記録でもラップ・リアクションがまだ無いもの」
  も詳細ページ取得の対象にするようになっている。また merge_swimrecord.py の
  merge_swimmer() も、重複（＝既存）と判定した記録に新しくラップ・リアクションが
  付いていれば、既存のhistoryエントリにそれを補完するようになっている。
  このスクリプトは、それらの仕組みをそのまま使って「全選手・全年度」を1回
  舐めるだけでよく、取得ロジック自体はここには実装していない。

  年度ごとの切り替え・ページ送りは audit_all_swimmers.py の仕組み（年度セレクタで
  新しい方から古い方へ順に見ていき、3年度連続で記録が0件だったら打ち切り）を
  そのまま流用している。

このスクリプトは index.html を実際に書き換える（commit/push は行わない。
内容を確認した上で、いつも通りGitHub Desktopでコミット・プッシュすること）。
LINE通知も行わない。

全選手×最大21年度、かつ既存記録の多くについて詳細ページを開き直すため、
通常の週次チェックよりもかなり時間がかかる見込み（1時間以上を想定）。
詳細ページ取得が規定回数（DETAIL_FETCH_FAILURE_LIMIT）失敗すると、その選手の
残りの取得はスキップされる仕組み（check_and_update.py参照）なので、途中で
サイト側に問題が起きても止まらず最後まで実行される。

使い方（PowerShellから、check_and_update.pyと同じ環境変数のまま）:
  python scripts\\backfill_laps_reaction.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from audit_all_swimmers import CONSECUTIVE_EMPTY_STOP, list_year_options, select_year_and_submit
from check_and_update import (
    LOGIN_ID,
    LOGIN_PW,
    ROSTER_PATH,
    collect_entries_with_detail,
    do_login,
    existing_keys_for,
    log,
    search_and_open_swimmer,
)
from merge_lib import load_roster
from merge_swimrecord import merge_swimmer


def backfill_swimmer(page, full_name, existing_keys):
    """1人分、全年度（早期打ち切りあり）を見て回り、新規記録の取得＋既存記録の
    ラップ・リアクション拾い直しの両方を兼ねたentriesリストを返す。
    audit_swimmer()とほぼ同じ構造だが、詳細ページ取得つきのcollect_entries_with_detail
    を使う点が異なる。"""
    found = search_and_open_swimmer(page, full_name)
    if not found:
        return None

    year_options = list_year_options(page)
    if not year_options:
        return None

    all_entries = []
    consecutive_empty = 0
    for yt in year_options:
        try:
            select_year_and_submit(page, yt)
        except Exception as e:
            log(f"    {yt}: 年度切り替えに失敗: {e}")
            continue
        entries = collect_entries_with_detail(page, existing_keys)
        all_entries.extend(entries)
        if len(entries) == 0:
            consecutive_empty += 1
            if consecutive_empty >= CONSECUTIVE_EMPTY_STOP:
                break
        else:
            consecutive_empty = 0

    return all_entries


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
            existing_keys = existing_keys_for(swimmer)
            try:
                all_entries = backfill_swimmer(page, name, existing_keys)
            except Exception as e:
                log(f"  ※ エラーでスキップ: {e}")
                results.append((name, None, None, str(e)))
                continue

            if all_entries is None:
                log("  見つかりませんでした（スキップ）。")
                results.append((name, None, None, "not_found"))
                continue

            report = merge_swimmer(ROSTER_PATH, name, all_entries, dry_run=False)
            if "error" in report:
                log(f"  ※ {report['error']}")
                results.append((name, None, None, report["error"]))
                continue

            log(
                f"  追加: {report['added']}件 / 重複: {report['dup']}件 / "
                f"リレー除外: {report['relay_skip']}件 / "
                f"ラップ・リアクション拾い直し: {report.get('backfilled', 0)}件"
            )
            if report["auto_cleaned"]:
                log("  ※ 大会名が自動クリーニングされました（要確認）:")
                for raw, canon in report["auto_cleaned"]:
                    log(f"      {raw}  →  {canon}")
            results.append((name, report["added"], report.get("backfilled", 0), None))

        browser.close()

    log("")
    log("=== 全体サマリー ===")
    total_added = 0
    total_backfilled = 0
    for name, added, backfilled, err in results:
        if err:
            log(f"  {name}: スキップ ({err})")
        else:
            total_added += added or 0
            total_backfilled += backfilled or 0
            log(f"  {name}: 追加{added}件 / 拾い直し{backfilled}件")
    log(f"\n合計: 新規追加{total_added}件 / ラップ・リアクション拾い直し{total_backfilled}件")
    log("")
    log(
        "index.htmlを更新しました。内容を確認の上、"
        "いつも通りGitHub Desktopでコミット・プッシュしてください。"
    )
    log("")
    log("EXIT: backfill_laps_reaction_done")


if __name__ == "__main__":
    main()
