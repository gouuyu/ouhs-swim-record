#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「詳細ページ(record_00.php)」へのリンクのクリックが、なぜ
"element is not visible" でタイムアウトし続けるのかを調査するための、
一回限りの診断用スクリプト。

index.htmlへの書き込みや、記録の追加・LINE通知は一切行わない（純粋に
調査のみ）。ログイン自体は check_and_update.py と全く同じ関数を使う
（＝このスクリプト自体が井田さんのログイン情報を外部に送ったり、見せたり
することはない。環境変数から読み込んでそのままサイトへのログインにだけ
使う）。

やること:
  1. いつも通りログイン
  2. 1人の選手（既定: 阿部 凪紗）のページを開く
  3. 「詳細ページ」へのリンク(record_00.php)のうち、1件目について
     - 要素のHTML（outerHTML）
     - 画面上の位置・サイズ（bounding box）
     - Playwrightから見て「見える」かどうか(is_visible)
     - CSSの計算済みスタイル（display / visibility / opacity /
       pointer-events）
     - 親要素のHTML（何か別の要素が覆いかぶさっていないか確認するため）
     を調べる
  4. 調査結果を scripts/diagnose_output.txt に、ページ全体のスクリーン
     ショットを scripts/diagnose_screenshot.png に保存する
     （どちらもこのリポジトリのフォルダ内なので、あとで確認・共有できる）

使い方（PowerShellから、check_and_update.pyと同じ環境変数のまま）:
  python scripts\\diagnose_detail_link.py
  （対象選手を変えたい場合: python scripts\\diagnose_detail_link.py "選手 名前"）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from check_and_update import (
    LOGIN_ID,
    LOGIN_PW,
    do_login,
    log,
    search_and_open_swimmer,
    select_year_if_possible,
)

TARGET_NAME = sys.argv[1] if len(sys.argv) > 1 else "阿部 凪紗"
OUT_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnose_output.txt")
OUT_PNG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnose_screenshot.png")


def main():
    if not LOGIN_ID or not LOGIN_PW:
        log("エラー: SWIMRECORD_ID / SWIMRECORD_PASSWORD が設定されていません。")
        sys.exit(1)

    lines = []

    def record(msg):
        print(msg, flush=True)
        lines.append(msg)

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
            record(f"ログイン処理でエラー: {e}")
            browser.close()
            sys.exit(1)

        record(f"=== 対象選手: {TARGET_NAME} ===")
        found = search_and_open_swimmer(page, TARGET_NAME)
        if not found:
            record("選手が見つかりませんでした。")
            browser.close()
            sys.exit(1)

        select_year_if_possible(page, "2026")

        links = page.locator('a[href*="record_00.php"]')
        count = links.count()
        record(f"詳細リンクの件数: {count}")

        if count > 0:
            el = links.first
            try:
                record(f"is_visible(): {el.is_visible()}")
            except Exception as e:
                record(f"is_visible()でエラー: {e}")
            try:
                box = el.bounding_box()
                record(f"bounding_box(): {box}")
            except Exception as e:
                record(f"bounding_box()でエラー: {e}")
            try:
                outer = el.evaluate("e => e.outerHTML")
                record(f"outerHTML:\n{outer}")
            except Exception as e:
                record(f"outerHTML取得でエラー: {e}")
            try:
                parent_outer = el.evaluate("e => e.parentElement ? e.parentElement.outerHTML : null")
                record(f"親要素のouterHTML:\n{parent_outer}")
            except Exception as e:
                record(f"親要素のouterHTML取得でエラー: {e}")
            try:
                style = el.evaluate(
                    "e => { const s = getComputedStyle(e); "
                    "return {display: s.display, visibility: s.visibility, "
                    "opacity: s.opacity, pointerEvents: s.pointerEvents, "
                    "position: s.position, zIndex: s.zIndex}; }"
                )
                record(f"計算済みスタイル: {style}")
            except Exception as e:
                record(f"計算済みスタイル取得でエラー: {e}")
            try:
                elem_at_point = None
                if box:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    elem_at_point = page.evaluate(
                        "([x, y]) => { const el = document.elementFromPoint(x, y); "
                        "return el ? el.outerHTML.slice(0, 500) : null; }",
                        [cx, cy],
                    )
                record(f"そのリンクの中心座標に実際にある要素（クリックを奪っている可能性のある要素）:\n{elem_at_point}")
            except Exception as e:
                record(f"elementFromPoint取得でエラー: {e}")
        else:
            record("詳細リンクが1件も見つかりませんでした。")

        try:
            page.screenshot(path=OUT_PNG, full_page=True)
            record(f"スクリーンショットを保存しました: {OUT_PNG}")
        except Exception as e:
            record(f"スクリーンショット保存でエラー: {e}")

        browser.close()

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    record(f"調査結果を保存しました: {OUT_TXT}")
    record("EXIT: diagnose_done")


if __name__ == "__main__":
    main()
