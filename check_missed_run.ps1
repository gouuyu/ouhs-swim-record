# OUHS SWIM TEAM ロスター 「見張り役」（見逃しチェック）
#
# 週次チェック（run_check.ps1）は、実行予定時刻にこのパソコンの電源が
# 入っていなければ実行されない（スキップして翌週に繰り越す、という
# 意図的な仕様）。そのためスクリプト自身の中からは「今週は実行できません
# でした」と報告できない。
#
# このファイルは、それとは別に「パソコンを起動・ログインしたとき」に
# 動くようタスクスケジューラへ登録しておく、別タスク用のラッパー。
# 最後に週次チェックが実際に動いた時刻を見て、明らかに間隔が空きすぎて
# いたら（既定8日）LINEで知らせる。
#
# 前提: run_check.ps1 と同じフォルダ（リポジトリの一番上）に置くこと。

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logFile = Join-Path $PSScriptRoot "watchdog_last_run.log"

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

"=== 見張り役 実行: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Encoding utf8
python scripts\notify_if_missed.py 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
