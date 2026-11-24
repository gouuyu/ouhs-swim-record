#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全選手について、スイムレコードモバイル側の「全年度」の記録を調べ、手元のロスター
(index.html)にある履歴と比較して、オンライン側にはあるが手元には無い（＝取りこぼしの
可能性がある）記録を選手ごとにリストアップする、一回限りの棚卸し調査用スクリプト。

audit_one_swimmer.py（1人用・動作確認済み）を全員ループに拡張したもの。
index.htmlへの書き込み・commit/push・LINE通知は一切行わない（読み取り専用）。

年度セレクタ・ページ送りの仕組みはブラウザで実際に確認済み:
  - <select name="year"> で年度（＝academic_yearと同じ考え方）を選び、
    "絞込検索" (<a onclick="document.swimmer.submit();return false;">) で送信する。
  - 1年度の記録が11件以上あると複数ページになるが、ページ送りは「次へ」という
    文字列ではなく "< 1 2 3 4 5 6 >" のような番号付きリンクで、次ページへのリンクは
    表示テキストが完全に ">" だけのリンクだった。hrefを直接goto()する。

高速化のため、年度は新しい方から古い方へ順番に見ていき、3年度連続で0件だったら
それより古い年度はもう調べない（競技キャリアはある時点より前には記録が存在しない
はずなので、安全に打ち切れる前提。実際に横山あいさんのテストでも、記録がある年度は
途切れなく連続していた）。

結果は:
  - 標準出力/last_run的なログ: 選手ごとの件数サマリー
  - audit_report.txt（リポジトリのトップ）: 選手ごとの、手元に無い記録の詳細一覧
    （レビュー用。.gitignore 済み、コミットはされない）

使い方（PowerShellから）:
  python scripts\\audit_all_swimmers.py
  （SWIMRECORD_ID / SWIMRECORD_PASSWORD は既存の環境変数を使う）
  全選手×最大21年度を調べるため、かなり時間がかかります（横山あいさん1人で
  387件取得するのに相当な時間がかかったため、全体では数十分程度を想定）。
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from check_and_update import (
    LOGIN_ID,
    LOGIN_PW,
    ROSTER_PATH,
    do_login,
    entry_dedup_key,
    existing_keys_for,
    log,
    search_and_open_swimmer,
)
from merge_lib import load_roster
from swimrecord_parse import parse_swimrecord_text

MAX_PAGES_PER_YEAR = 30  # 安全上限。1年度でこれを超えるページ数は想定していない。
CONSECUTIVE_EMPTY_STOP = 3  # これだけ連続で0件だったら、それより古い年度の調査を打ち切る
REPORT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "audit_report.txt"
)


def get_next_page_href(page):
    candidates = page.locator("a").filter(has_text=re.compile(r"^>$"))
    if candidates.count() > 0:
        return candidates.first.get_attribute("href")
    return None


def collect_entries_for_current_view(page):
    all_entries = []
    for _ in range(MAX_PAGES_PER_YEAR):
        try:
            body_text = page.locator("body").inner_text(timeout=10000)
        except Exception:
            break
        all_entries.extend(parse_swimrecord_text(body_text))
        next_href = get_next_page_href(page)
        if next_href:
            try:
                page.goto(next_href, wait_until="domcontentloaded")
                continue
            except Exception:
                break
        break
    return all_entries


def list_year_options(page):
    try:
        select = page.locator('select[name="year"]')
        if select.count() == 0:
            return []
        options = select.first.locator("option")
        return [options.nth(i).inner_text().strip() for i in range(options.count())]
    except Exception:
        return []


def select_year_and_submit(page, year_text):
    select = page.locator('select[name="year"]').first
    select.select_option(label=year_text)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
        page.evaluate("document.forms['swimmer'] && document.forms['swimmer'].submit()")


def audit_swimmer(page, full_name):
    """1人分の全年度(早期打ち切りあり)の記録を集める。選手が見つからなければNone。"""
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
        entries = collect_entries_for_current_view(page)
        all_entries.extend(entries)
        if len(entries) == 0:
            consecutive_empty += 1
            if consecutive_empty >= CONSECUTIVE_EMPTY_STOP:
                break
        else:
            consecutive_empty = 0

    return all_entries


def compute_missing(all_entries, existing_keys):
    missing = []
    seen_keys = set()
    relay_count = 0
    dup_count = 0
    for e in all_entries:
        if e["is_relay"]:
            relay_count += 1
            continue
        key = entry_dedup_key(e)
        if key in seen_keys:
            dup_count += 1
            continue
        seen_keys.add(key)
        if key not in existing_keys:
            missing.append(e)
    return missing, len(seen_keys), relay_count, dup_count


def main():
    if not LOGIN_ID or not LOGIN_PW:
        log("エラー: SWIMRECORD_ID / SWIMRECORD_PASSWORD が設定されていません。")
        sys.exit(1)

    _, _, roster_data = load_roster(ROSTER_PATH)
    log(f"対象: ロスター全{len(roster_data)}名")

    report_lines = [
        f"=== 全選手 取りこぼし調査レポート ({datetime.date.today():%Y-%m-%d}) ===",
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

        for swimmer in roster_data:
            name = swimmer["name"]
            existing_keys = existing_keys_for(swimmer)
            log(f"=== {name} (手元: {len(swimmer.get('history', []))}件) ===")
            try:
                all_entries = audit_swimmer(page, name)
            except Exception as e:
                log(f"  ※ エラーで調査をスキップしました: {e}")
                summary.append((name, None, None, str(e)))
                continue

            if all_entries is None:
                log("  見つかりませんでした（スキップ）。")
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
    log("=== 全体サマリー ===")
    total_missing = 0
    total_checked = 0
    for name, found_count, missing_count, err in summary:
        if err:
            log(f"  {name}: スキップ ({err})")
        else:
            total_missing += missing_count
            total_checked += 1
            log(f"  {name}: 手元に無い {missing_count}件 / サイト側 {found_count}件")
    log(f"\n合計: {total_checked}名分を調査、手元に無い記録 合計{total_missing}件")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    log(f"\n詳細レポートを書き出しました: {REPORT_PATH}")

    log("")
    log("EXIT: audit_done")


if __name__ == "__main__":
    main()
