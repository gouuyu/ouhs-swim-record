# OUHS SWIM TEAM ロスター 週次チェック（ローカルPC実行用）
#
# タスクスケジューラからこのファイルを呼び出すことで、GitHub Actionsの代わりに
# このパソコン上で週次チェックを実行する。処理内容（ログイン→記録確認→ロスター
# 更新→LINE通知）自体は scripts/check_and_update.py と全く同じで、違うのは
# 「どこで実行するか」と、「変更があった場合のgit commit/push」をこのスクリプト
# 自身が行う点だけ。
#
# 前提:
#   - このファイルは、index.html や scripts フォルダと同じ階層（リポジトリの
#     一番上）に置くこと
#   - Python・pip install -r scripts/requirements.txt・
#     python -m playwright install chromium が済んでいること
#   - git がコマンドから使えること（git --version で確認できること）
#   - 環境変数 SWIMRECORD_ID / SWIMRECORD_PASSWORD / LINE_CHANNEL_ACCESS_TOKEN が
#     このWindowsアカウントに設定済みであること

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logFile = Join-Path $PSScriptRoot "last_run.log"

# Pythonの出力を必ずUTF-8にする（Windowsだと既定のコンソールのコードページと
# ズレて文字化けすることがあるため、明示的に指定する）
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# PYTHONIOENCODING だけだと、PowerShell 5.1側が外部プロセスの出力を
# 「コンソールの既定コードページ（日本語Windowsだと多くの場合Shift-JIS系）」
# で読み込んでしまい、Pythonが実際にはUTF-8で出力していても文字化けする。
# PowerShell側の出力エンコーディングも明示的にUTF-8にする。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

"=== 実行開始: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Encoding utf8

# 2>&1 でエラー出力も含めて1つのテキストストリームにまとめ、Out-File側で明示的に
# utf8指定して書き出す（文字化け対策）。
python scripts\check_and_update.py 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

"" | Out-File -FilePath $logFile -Append -Encoding utf8

# index.html に実際に変更があった場合だけ commit & push する。
# （git status --porcelain だけで判定すると、last_run.log やキャッシュフォルダ
# など他の無関係なファイルの存在にも反応して「変更あり」と誤判定してしまうため、
# 必ず index.html だけを対象に diff で判定する）
git add index.html 2>&1 | Out-Null
git diff --cached --quiet -- index.html

if ($LASTEXITCODE -ne 0) {
    "index.htmlに変更を検出しました。commit & push します。" | Out-File -FilePath $logFile -Append -Encoding utf8
    $commitMsg = "スイムレコード自動チェック: 記録を更新 ($(Get-Date -Format 'yyyy-MM-dd'))"
    git commit -m $commitMsg 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

    git push 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        "pushしました。" | Out-File -FilePath $logFile -Append -Encoding utf8
    } else {
        "push に失敗しました（終了コード: $LASTEXITCODE）。GitHub Desktopで手動でPushしてみてください。" | Out-File -FilePath $logFile -Append -Encoding utf8
    }
} else {
    "index.htmlに変更はありませんでした。commit & push は行いません。" | Out-File -FilePath $logFile -Append -Encoding utf8
}

"=== 実行終了: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8
