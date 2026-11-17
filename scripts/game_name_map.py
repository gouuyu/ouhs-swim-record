import unicodedata, re

# Manual overrides for meets whose SwimRecord raw label uses a non-literal abbreviation
# as its trailing "short name" (so the generic auto-cleaner's suffix-strip can't find it).
# Keyed by a distinctive substring; canonical chosen to match what's already in index.html's
# roster-data for the same meet where applicable.
#
# IMPORTANT: when a new meet-name pattern causes the "auto_cleaned" review list (printed by
# run_merge.py) to show something truncated, dangling (ending in a stray "・" "兼" ":" etc.),
# or otherwise wrong, add a new entry here. Put more specific patterns (e.g. a sub-event like
# a "チャレンジレース") BEFORE a more generic pattern they are a substring of, or the generic
# one will match first and the distinction will be silently lost (this happened once this
# project -- see the 津田チャンピオンズカップ / 津田チャンピオンズカップチャレンジレース pair
# below for the exact pattern to follow).
OVERRIDES = [
    ("第100回 関西学生選手権", "第100回  関西学生選手権水泳競技大会 競泳競技"),  # double space matches existing roster entries
    ("中部支部対抗", "第2回 関西・中部支部対抗水泳競技大会"),
    ("全国JOC", "第49回 全国JOCジュニアオリンピックカップ夏季水泳競技大会 滋賀県予選会"),
    # The "チャレンジレース" (challenge race) sub-event at the same meet is a DISTINCT race
    # from the main 津田チャンピオンズカップ championship, so it must be checked (and matched)
    # BEFORE the generic 津田チャンピオンズカップ override below, or it would be collapsed into
    # the same canonical name as the main event and lose that distinction. Existing roster
    # entries for this sub-event use "第26回津田チャンピオンズカップチャレンジレース" (no space).
    ("津田ﾁｬﾝﾋﾟｵﾝｽﾞｶｯﾌﾟﾁｬﾚﾝｼﾞﾚｰｽ", "第26回津田チャンピオンズカップチャレンジレース"),
    ("津田チャンピオンズカップチャレンジレース", "第26回津田チャンピオンズカップチャレンジレース"),
    ("津田ﾁｬﾝﾋﾟｵﾝｽﾞｶｯﾌﾟ", "第26回 津田チャンピオンズカップ"),
    ("津田チャンピオンズカップ", "第26回 津田チャンピオンズカップ"),
    ("富山県水泳選手権", "第56回 富山県水泳選手権大会"),
    ("日本選手権(25m)", "第68回 日本選手権（25ｍ）水泳競技大会"),
    ("日本選手権（25ｍ）", "第68回 日本選手権（25ｍ）水泳競技大会"),
    # auto_clean's suffix-strip only finds the short "県選手権" fragment inside this raw
    # label and leaves a dangling "...県ジュニア・" -- the genuine trailing short-form
    # ("県ジュニア・県選手権") isn't a literal substring of the main title, so it needs
    # a manual override like the other abbreviated-suffix cases above.
    ("静岡県ｼﾞｭﾆｱ選手権水泳競技大会兼静岡県選手権", "静岡県ジュニア選手権水泳競技大会兼静岡県選手権水泳競技大会"),
    ("静岡県ジュニア選手権水泳競技大会兼静岡県選手権", "静岡県ジュニア選手権水泳競技大会兼静岡県選手権水泳競技大会"),
    # Same dangling-suffix issue: the trailing short-form "県選手権大会" isn't a literal
    # substring of "...県選手権水泳競技大会" earlier in the raw string (interrupted by
    # "水泳競技"), so auto_clean's generic matcher only strips "大会" and leaves a
    # dangling "...県選手権" at the end.
    ("国民ｽﾎﾟｰﾂ大会岩手県代表選手選考会兼令和8年度岩手県選手権水泳競技大会県選手権",
     "国民スポーツ大会岩手県代表選手選考会兼令和8年度岩手県選手権水泳競技大会"),
    ("国民スポーツ大会岩手県代表選手選考会兼令和8年度岩手県選手権水泳競技大会県選手権",
     "国民スポーツ大会岩手県代表選手選考会兼令和8年度岩手県選手権水泳競技大会"),
    # Same dangling-suffix issue: the trailing short-form "国スポ県予選兼県選手権" is not a
    # literal substring of the earlier text (interrupted by "水泳競技大会"), so auto_clean's
    # generic matcher only strips "県選手権" and leaves a dangling "...国スポ県予選兼" at the end.
    ("2026年度国民ｽﾎﾟｰﾂ大会水泳競技県予選会兼2026年度広島県選手権水泳競技大会国ｽﾎﾟ県予選兼県選手権",
     "2026年度国民スポーツ大会水泳競技県予選会兼2026年度広島県選手権水泳競技大会"),
    ("2026年度国民スポーツ大会水泳競技県予選会兼2026年度広島県選手権水泳競技大会国スポ県予選兼県選手権",
     "2026年度国民スポーツ大会水泳競技県予選会兼2026年度広島県選手権水泳競技大会"),
    # This meet's raw label has the team-affiliation prefix "滋賀:" baked in twice (once at
    # the very start, once again between the main title and its repeated short-form copy),
    # so after the generic prefix-stripper removes the first "滋賀:" the generic suffix-stripper
    # can only match the trailing copy's text (not its leading colon), leaving a dangling
    # "...大会滋賀:" at the end.
    ("滋賀県ｽﾌﾟﾘﾝﾄ水泳競技大会滋賀:滋賀県ｽﾌﾟﾘﾝﾄ水泳競技大会", "滋賀県スプリント水泳競技大会"),
    ("滋賀県スプリント水泳競技大会滋賀:滋賀県スプリント水泳競技大会", "滋賀県スプリント水泳競技大会"),
    # The trailing short-form tag here is "浜名湾短水路C" (ending in a bare "C" for
    # "Challenge"), which is not a literal substring of the main title earlier in the
    # string (the main title has no trailing "C"), so auto_clean's suffix-matcher never
    # finds any L that matches and leaves the whole duplicated raw string unstripped.
    ("2026浜名湾短水路ﾁｬﾚﾝｼﾞﾚｰｽ浜名湾短水路C", "2026浜名湾短水路チャレンジレース"),
    ("2026浜名湾短水路チャレンジレース浜名湾短水路C", "2026浜名湾短水路チャレンジレース"),
]

def auto_clean(raw):
    s = unicodedata.normalize('NFKC', raw)
    while True:
        m = re.match(r'^[^\s:]{1,12}[:：]', s)
        if m:
            s = s[m.end():]
        else:
            break
    n = len(s)
    best = 0
    for L in range(min(30, n - 1), 1, -1):
        suf = s[n - L:]
        if suf.replace(' ', '') in s[:n - L].replace(' ', ''):
            best = L
            break
    if best:
        s = s[:n - best]
    return re.sub(r'\s+', ' ', s).strip()

# The genuine national 国スポ (国民スポーツ大会) meet's raw label is exactly
# "第N回国民ｽﾎﾟｰﾂ大会水泳競技大会国ｽﾎﾟ" -- i.e. ends with nothing but the short-form
# abbreviation "国スポ" right after "水泳競技大会", with no prefecture-qualifier clause
# tacked on. A compound/prefectural meet (e.g. 兵庫県選手権 or 沖縄県予選会) that merely
# *mentions* 国民スポーツ大会 as a selection criterion has more text after that point and
# must NOT match this -- hence the end-anchored regex rather than a substring check.
KOKUSUPO_RE = re.compile(r'^第(\d+)回国民スポーツ大会水泳競技大会国スポ$')

def canonical_game_name(raw):
    """Returns (canonical_name, source) where source is 'override' or 'auto'.
    Caller should review/print 'auto' results since they weren't manually verified."""
    norm = unicodedata.normalize('NFKC', raw)
    m = KOKUSUPO_RE.match(norm)
    if m:
        return f"第{m.group(1)}回 国民スポーツ大会水泳競技大会", 'override'
    for key, canon in OVERRIDES:
        if key in raw:
            return canon, 'override'
    return auto_clean(raw), 'auto'
