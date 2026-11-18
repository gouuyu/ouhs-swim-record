#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日本パラ水泳連盟の選手・記録データベース(paraswim3.jp)から、登録選手のパラ水泳
記録を取得し、ロスター(index.html)内の該当選手の「para」データ（history・byEvent）
に安全にマージする、動作確認用の単独スクリプト。

対象サイトはログイン不要・JSレンダリング無しの静的ページで、スイムレコード
モバイルのような認証切れの心配が無い。選手ごとに固定のP-No（選手番号）で
直接アクセスできる:
  https://paraswim3.jp/jpsf_player_db/wp_name_result_S.php?pno=<P-No>

このスクリプトは、まだ週次の自動チェック(check_and_update.py)には組み込んで
いない。まずこれを単独で実行して問題ないことを確認してから、週次チェックに
正式に組み込む予定（2段階で進める）。

設計上の注意点（リスクを下げるための工夫）:
  - history の重複判定は、本体の自動化（merge_swimrecord.py）と同じ考え方で
    「年度・日付(MM/DD)・距離・種目・クラス・タイム」の組み合わせで行い、大会名
    （表記がサイト側で年によって揺れている。例:「JP2018/横浜国際プール」
    「2022ｼﾞｬﾊﾟﾝﾊﾟﾗ／横浜」「ジャパンパラ / 横浜」）は判定に使わない。
  - タイムが「棄権」など数値化できない記録は、既存のpara.historyの設計
    （time:元の文字列, seconds: None, dnf: True）に合わせてそのまま記録として
    残す。ベストタイム(byEvent)の計算からはNoneを安全に除外する。
  - ラップタイム（50mごとの通過タイム）は、大会によって区切り方がバラバラ
    （スペース区切り/ハイフン区切り/分:秒区切り）で誤読のリスクがあるため、
    初版では取得しない（タイム・種目・クラス・大会名・日付・水路のみ反映）。
    必要になったら後で追加する。
  - 日本記録(nationalRecords)は公式な認定が必要な情報なので自動更新の対象外。
    これまで通り手動で管理する。
  - 選手のクラス分け(classifications)もここでは更新しない（クラス再判定は
    公式な手続きが必要なため）。

使い方（PowerShellから）:
  python scripts\\para_check.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from merge_lib import academic_year, load_roster, save_roster, time_to_seconds

# check_and_update.py からもこのモジュールの関数をimportして週次チェックに
# 組み込んでいるため、check_and_update.py側をここでimportすると循環import
# になってしまう。そのためROSTER_PATH・logはここで独立して定義する
# （check_and_update.py内の同名のものと同じ中身）。
ROSTER_PATH = os.environ.get("ROSTER_PATH", "index.html")


def log(msg):
    print(msg, flush=True)

# 選手名 -> 日本パラ水泳連盟データベースのP-No（選手番号）の対応表。
# 新しくパラ選手が加入したら、ここに名前とP-Noを追加する
# （P-Noは https://info.paraswim.jp/db2/ で選手名検索すれば調べられる）。
PARA_PNO = {
    "宇津木 美都": "3722",
}

STYLE_MAP = {
    "自由形": "自由形",
    "平泳ぎ": "平泳ぎ",
    "背泳ぎ": "背泳ぎ",
    "ﾊﾞﾀﾌﾗｲ": "バタフライ",
    "個人ﾒﾄﾞﾚｰ": "個人メドレー",
}

EVENT_RE = re.compile(r"^(\d+m)(.+)$")

# paraswim3.jp側のデータに既知の入力ミスがある場合の補正表。
# キー: (選手名, 日付YYYY-MM-DD, スタイル, クラス) -> 正しい距離。
# 2026-11の動作確認で発覚: 宇津木選手の2025-06-29 背泳ぎ(S9)棄権記録が、サイト上
# では「101m」になっていた（既存の手動入力データでは「100m」で登録済み。実際は
# 100m背泳ぎの棄権だと思われる）。距離違いとして重複除外されず二重登録される
# ところだったので、ここで補正する。
DISTANCE_FIXES = {
    ("宇津木 美都", "2025-06-29", "背泳ぎ", "S9"): "100m",
}


def fetch_para_rows(page, pno):
    """paraswim3.jpの選手ページから、記録一覧テーブルの各行を
    [年度, 日付, 水路, 種目, クラス, タイム, 場所, 参加区分, ラップ, 新記録対象区分]
    のリストとして返す。ページ内に複数のtableがあるため、見出しの文言
    （"新記録対象区分"かつ"年度"を含む）で記録一覧テーブルを特定する。"""
    url = f"https://paraswim3.jp/jpsf_player_db/wp_name_result_S.php?pno={pno}"
    page.goto(url, wait_until="domcontentloaded")

    tables = page.locator("table")
    n = tables.count()
    target = None
    for i in range(n):
        try:
            txt = tables.nth(i).inner_text(timeout=3000)
        except Exception:
            continue
        if "新記録対象区分" in txt and "年度" in txt:
            target = tables.nth(i)
            break
    if target is None:
        raise RuntimeError("記録一覧テーブルが見つかりませんでした（サイトの構造が変わった可能性があります）。")

    rows = target.locator("tr")
    n_rows = rows.count()
    out = []
    for i in range(1, n_rows):  # 1行目はヘッダーなのでスキップ
        cells = rows.nth(i).locator("td")
        nc = cells.count()
        if nc < 10:
            continue
        vals = [cells.nth(j).inner_text().strip() for j in range(nc)]
        out.append(vals)
    return out


def parse_para_row(name, vals):
    """1行分のセル値を、roster中のpara.history形式のentry dictに変換する。
    種目欄が想定外の形式、または日付が解釈できない行はNoneを返す（呼び出し側で
    スキップする）。「棄権」等、タイムが数値化できない行はスキップせず、既存の
    para.historyの設計（2026-11時点で実際に8件確認）に合わせて、
    time: 元の文字列（例:"棄権"）, seconds: None, dnf: True として残す
    （ベストタイム計算からは除外されるが、参加記録としては保持する）。"""
    date_raw, waterway, event_raw, cls, time_raw, game = vals[1], vals[2], vals[3], vals[4], vals[5], vals[6]

    m = EVENT_RE.match(event_raw)
    if not m:
        return None
    distance, style_raw = m.group(1), m.group(2)
    style = STYLE_MAP.get(style_raw, style_raw)

    date_part = date_raw.split(" ")[0]  # "2026-10-23 00:00:00" -> "2026-10-23"
    try:
        y, mo, da = date_part.split("-")
        cal_year, mo_i, da_i = int(y), int(mo), int(da)
    except ValueError:
        return None

    fix = DISTANCE_FIXES.get((name, date_part, style, cls))
    if fix:
        distance = fix

    time_raw = time_raw.strip()
    time_norm = time_raw
    if ":" in time_raw:
        # "01:22.46" -> "1:22.46" （分の先頭0を落として、既存データの表記に合わせる）
        mm, ss = time_raw.split(":", 1)
        time_norm = f"{int(mm)}:{ss}"
    dnf = False
    try:
        secs = time_to_seconds(time_norm)
    except (ValueError, ZeroDivisionError):
        dnf = True
        secs = None
        time_norm = time_raw  # 「棄権」等はそのままの表記で保持する

    year = academic_year(cal_year, mo_i)
    md = f"{mo_i:02d}/{da_i:02d}"

    entry = {
        "year": year, "waterway": waterway, "style": style, "distance": distance,
        "cls": cls, "time": time_norm, "date": md, "game": game, "seconds": secs,
    }
    if dnf:
        entry["dnf"] = True
    return entry


def recompute_para_by_event(para):
    """history内の最速タイムで、byEvent（種目別ベストタイム）を更新する。本体の
    recompute_by_stroke()と全く同じ理由でここでも必須: history更新後にこれを
    呼ばないと、新しく追加した記録がベストタイムでも画面表示（byEvent）側には
    反映されない、という不具合になる（本体側で一度実際に起きたのと同じ種類の
    不具合なので、ここでも最初から対応しておく）。"""
    changes = []
    hist = para.get("history", [])
    by_event = para.setdefault("byEvent", {})

    best = {}
    for h in hist:
        if h.get("seconds") is None:
            continue  # 「棄権」等(dnf=True)はベストタイム比較の対象外
        k = (h["style"], h["distance"], h.get("cls"), h["waterway"])
        if k not in best or h["seconds"] < best[k]["seconds"]:
            best[k] = h

    for style, entries in by_event.items():
        for i, b in enumerate(entries):
            k = (style, b["distance"], b.get("cls"), b["waterway"])
            hb = best.get(k)
            if hb and b.get("seconds") is not None and hb["seconds"] < b["seconds"] - 1e-6:
                new_entry = dict(hb)
                changes.append((style, b["distance"], b.get("cls"), b["waterway"], b["time"], hb["time"]))
                entries[i] = new_entry
    return changes


def merge_para_history(roster_path, name, entries, dry_run=False):
    content, span, data = load_roster(roster_path)
    by_name = {r["name"].replace(" ", "").replace("　", ""): r for r in data}
    key = name.replace(" ", "").replace("　", "")
    r = by_name.get(key)
    if not r:
        return {"error": f"NOT IN ROSTER: {name}"}
    if "para" not in r or not isinstance(r["para"], dict):
        return {"error": f"paraデータが存在しない選手です: {name}"}

    para = r["para"]
    hist = para.setdefault("history", [])

    def dedup_key(h):
        return (h["year"], h["date"], h["distance"], h["style"], h.get("cls"), h["time"])

    existing_keys = {dedup_key(h) for h in hist}

    added, dup = [], []
    for e in entries:
        k = dedup_key(e)
        if k in existing_keys:
            dup.append(e)
            continue
        existing_keys.add(k)
        if not dry_run:
            hist.append(e)
        added.append(e)

    by_event_changes = []
    if not dry_run and added:
        def key_fn(h):
            mo, da = h["date"].split("/")
            return (h["year"], int(mo), int(da))
        hist.sort(key=key_fn, reverse=True)
        by_event_changes = recompute_para_by_event(para)
        save_roster(roster_path, content, span, data)

    return {
        "name": name, "added": len(added), "dup": len(dup),
        "added_entries": added, "by_event_changes": by_event_changes,
    }


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        for name, pno in PARA_PNO.items():
            log(f"=== {name}（パラ水泳 P-No:{pno}） ===")
            try:
                rows = fetch_para_rows(page, pno)
            except Exception as e:
                log(f"  ※ サイトから取得できませんでした: {e}")
                continue

            parsed_all = [parse_para_row(name, v) for v in rows]
            parsed = [e for e in parsed_all if e is not None]
            skipped = len(parsed_all) - len(parsed)
            dnf_count = sum(1 for e in parsed if e.get("dnf"))
            log(f"  サイト側の記録: {len(rows)}件（種目/日付が読み取れずスキップ: {skipped}件 / うち棄権等: {dnf_count}件）")

            report = merge_para_history(ROSTER_PATH, name, parsed, dry_run=False)
            if "error" in report:
                log(f"  ※ {report['error']}")
                continue
            log(f"  追加: {report['added']}件 / 重複: {report['dup']}件")
            for e in report["added_entries"]:
                log(f"    {e['date']} {e['style']}{e['distance']}({e['cls']}) {e['time']}  {e['game']}")
            if report["by_event_changes"]:
                log("  ※ ベストタイム(byEvent)が更新されました:")
                for c in report["by_event_changes"]:
                    log(f"    {c}")

        browser.close()

    log("")
    log("index.htmlを更新しました（まだ週次自動チェックには組み込んでいません）。")
    log("内容を確認の上、問題なければ教えてください。週次チェックへの組み込みはその後に行います。")
    log("EXIT: para_check_done")


if __name__ == "__main__":
    main()
