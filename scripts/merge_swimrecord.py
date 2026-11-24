from merge_lib import load_roster, save_roster, time_to_seconds, academic_year, recompute_by_stroke
from game_name_map import canonical_game_name


def merge_swimmer(roster_path, name, entries, dry_run=False):
    """entries: list of dicts from parse_swimrecord_page/parse_swimrecord_text
    (date=YYYY/MM/DD, style, distance, waterway, time, game_raw, is_relay).
    Returns a report dict. When dry_run is False, writes the updated roster back to
    roster_path and also recomputes byStroke (career-best cards) so a new personal
    best added here is reflected in the main display, not just in the full history.
    """
    content, span, data = load_roster(roster_path)
    by_name = {r['name'].replace(' ', '').replace('　', ''): r for r in data}
    key = name.replace(' ', '').replace('　', '')
    r = by_name.get(key)
    if not r:
        return {"error": f"NOT IN ROSTER: {name}"}

    added, dup, relay_skip, auto_cleaned, invalid_time_skip, backfilled = [], [], [], [], [], []

    for e in entries:
        if e['is_relay']:
            relay_skip.append(e)
            continue
        # 「-」など、公式タイムが記録されていない行（失格・欠場など）は
        # time_to_seconds() で数値化できない。このようなレースはベストタイム
        # 比較の対象にならないので、安全にスキップする（クラッシュさせない）。
        # 2026-11 の全選手バックフィルで木下妃魚選手のデータに遭遇して発覚。
        try:
            secs = time_to_seconds(e['time'])
        except (ValueError, ZeroDivisionError):
            invalid_time_skip.append(e)
            continue
        y, mo, da = e['date'].split('/')
        cal_year = int(y)
        mo_i = int(mo)
        year = academic_year(cal_year, mo_i)
        md = f"{mo_i:02d}/{int(da):02d}"
        canon, source = canonical_game_name(e['game_raw'])
        if source == 'auto':
            auto_cleaned.append((e['game_raw'], canon))
        existing = next(
            (h for h in r['history'] if h['year'] == year and h['date'] == md
             and h['distance'] == e['distance'] and h['style'] == e['style'] and h['time'] == e['time']),
            None,
        )
        if existing is not None:
            dup.append(e)
            # 2026-11追加: 既存記録でも、今回ラップ・リアクションが取れていて
            # （check_and_update.pyのfetch_detail_for_new_entriesが拾い直し対象として
            # 詳細ページを開いた結果）、かつ既存側にまだ無ければ補完する。
            # 既にある情報（finaPoint/ranking/raceCountなど）は上書きしない。
            if not dry_run:
                patched = False
                if e.get('laps') and not existing.get('laps'):
                    existing['laps'] = e['laps']
                    patched = True
                if e.get('reaction') is not None and existing.get('reaction') is None:
                    existing['reaction'] = e['reaction']
                    patched = True
                if patched:
                    backfilled.append(existing)
            continue
        entry = {
            "year": year, "waterway": e['waterway'], "style": e['style'], "distance": e['distance'],
            "time": e['time'], "finaPoint": None, "date": md, "game": canon,
            "ranking": None, "raceCount": 1, "seconds": secs
        }
        # リアクションタイム・ラップタイムは、スイムレコードモバイルの記録一覧ページ
        # ではなく、各レースの「詳細ページ」(record_00.php) を個別に開かないと
        # 取得できない。check_and_update.py が新規レースと判定したものについてのみ
        # 詳細ページを開いてこれらをentryに追加しているので、ここではその値を
        # そのまま引き継ぐだけでよい（存在する場合のみ）。
        if e.get('laps'):
            entry['laps'] = e['laps']
        if e.get('reaction') is not None:
            entry['reaction'] = e['reaction']
        if not dry_run:
            r['history'].append(entry)
        added.append(entry)

    by_stroke_changes = []
    # backfilledのみ（新規追加は無いがラップ・リアクションを既存記録に補完した）の場合も
    # 保存・byStroke再計算が必要。そうしないと、せっかく拾い直したデータがindex.htmlに
    # 書き戻されない。
    if not dry_run and (added or backfilled):
        def key_fn(h):
            mo, da = h['date'].split('/')
            return (h['year'], int(mo), int(da))
        r['history'].sort(key=key_fn, reverse=True)
        by_stroke_changes = recompute_by_stroke(data)
        save_roster(roster_path, content, span, data)

    return {
        "name": name, "added": len(added), "dup": len(dup),
        "relay_skip": len(relay_skip), "auto_cleaned": list(set(auto_cleaned)),
        "invalid_time_skip": len(invalid_time_skip), "invalid_time_entries": invalid_time_skip,
        "added_entries": added, "by_stroke_changes": by_stroke_changes,
        "backfilled": len(backfilled),
    }
