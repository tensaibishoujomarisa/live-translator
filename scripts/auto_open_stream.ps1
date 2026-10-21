# ============================================================
#  火羽ひのめ 直播自动打开（预约时间感知版）
# ------------------------------------------------------------
#  由计划任务在周三/六/日 07:55 调用。
#
#  逻辑：
#    1. 确保字幕程序在跑
#    2. 每分钟看一次她的 RSS 最新几条视频
#    3. 若是「直播」(isLiveContent=true) 且预约开播时间已到
#       → 等 20 秒让画面起来 → 用 Chrome 打开直播页
#    4. 都不是就继续等，直到超过 $MaxMinutes
#
#  为什么不能只看「有没有新视频」：
#    她会提前一天建好「预约直播」，那条记录在 RSS 里就已经存在了。
#    实测 10/21 那场是 10/20 就建好的预约，startTimestamp=08:00。
#    若只看"新视频"就会在 07:55 误判为已开播，打开一个倒计时页然后退出。
#
#  日志：logs\auto_open.log
# ============================================================

$ErrorActionPreference = 'Continue'
$Root = 'G:\projects\jp-live-translator'
$LogDir = Join-Path $Root 'logs'
$Log = Join-Path $LogDir 'auto_open.log'
$ChannelId = 'UCypbWFLH4AVFdao9XvpU_Xw'
$RssUrl = "https://www.youtube.com/feeds/videos.xml?channel_id=$ChannelId"
$LiveUrl = "https://www.youtube.com/channel/$ChannelId/live"
$WatchUrlTpl = 'https://www.youtube.com/watch?v=VIDEOID'

# 参数
$MaxMinutes = 120        # 最多守 2 小时（08:00 开播，留足迟到余量）
$IntervalSec = 60
$ProxyCandidates = @('http://127.0.0.1:7890', 'http://127.0.0.1:10809')
$DryRun = $false         # -DryRun 只判断不打开，用于测试
$StartApp = $true        # -NoApp 只打开直播页，不启动字幕程序

foreach ($a in $args) {
    if ($a -eq '-DryRun') { $DryRun = $true }
    if ($a -eq '-NoApp')  { $StartApp = $false }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log([string]$Msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Msg
    Add-Content -Path $Log -Value $line -Encoding utf8
}

function Invoke-Web([string]$Url, [int]$Timeout = 25) {
    $base = @{
        Uri             = $Url
        UseBasicParsing = $true
        TimeoutSec      = $Timeout
        UserAgent       = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'
    }
    try { return Invoke-WebRequest @base } catch { }
    foreach ($p in $ProxyCandidates) {
        try {
            $params = $base.Clone()
            $params['Proxy'] = $p
            return Invoke-WebRequest @params
        } catch { }
    }
    return $null
}

function Get-VideoIds([string]$Xml, [int]$Take = 3) {
    $out = @()
    foreach ($m in [regex]::Matches($Xml, '<yt:videoId>([^<]+)</yt:videoId>')) {
        $out += $m.Groups[1].Value
        if ($out.Count -ge $Take) { break }
    }
    return $out
}

# 返回 $null 或 @{ live_content; ended; is_live; start; title }
#   ended   : 有 endTimestamp → 已经播完了
#   is_live : 页面里 isLive:true → 正在播或即将播
#   实测区分方式（很重要）：
#     已结束的（10/18 那场）  endTimestamp 有、isLive 无
#     预约中的（10/21 这场）  endTimestamp 无、isLive 有
#   只看 isLiveContent 会把"已经播完的存档"当成"正在直播"。
function Get-VideoInfo([string]$VideoId) {
    $url = $WatchUrlTpl -replace 'VIDEOID', $VideoId
    $r = Invoke-Web $url
    if (-not $r) { return $null }
    $html = $r.Content
    $liveContent = [bool]($html -match '"isLiveContent"\s*:\s*true')
    $ended       = [bool]($html -match '"endTimestamp"\s*:\s*"')
    $isLive      = [bool]($html -match '"isLive"\s*:\s*true')

    $start = $null
    $m = [regex]::Match($html, '"startTimestamp"\s*:\s*"([^"]+)"')
    if ($m.Success) {
        try { $start = ([datetime]::Parse($m.Groups[1].Value)).ToUniversalTime() } catch { }
    }
    $t = [regex]::Match($html, '"title"\s*:\s*"([^"]{4,160})"')
    return @{
        live_content = $liveContent
        ended        = $ended
        is_live      = $isLive
        start        = $start
        title        = $(if ($t.Success) { $t.Groups[1].Value } else { '' })
    }
}

Write-Log "==== 自动打开开始（预约时间感知，DryRun=$DryRun，StartApp=$StartApp）===="

# ---------- 1. 字幕程序（可用 -NoApp 关掉） ----------
try {
    if (-not $StartApp) {
        Write-Log '按 -NoApp 设置：只打开直播，不启动字幕程序'
    }
    else {
        $running = @(Get-Process electron -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($Root, 'OrdinalIgnoreCase') })
        if ($running.Count -gt 0) {
            Write-Log "字幕程序已在运行（$($running.Count) 个进程）"
        }
        elseif ($DryRun) {
            Write-Log '[DryRun] 跳过启动字幕程序'
        }
        else {
            $bat = Join-Path $Root '03_启动字幕.bat'
            if (Test-Path $bat) {
                Write-Log '启动字幕程序…'
                Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$bat`"" `
                    -WorkingDirectory $Root -WindowStyle Minimized
                Start-Sleep -Seconds 15
                Write-Log '字幕程序启动完成'
            }
            else { Write-Log "[警告] 找不到 $bat" }
        }
    }
}
catch { Write-Log "启动字幕程序失败：$($_.Exception.Message)" }

# ---------- 2. 轮询 ----------
$deadline = (Get-Date).AddMinutes($MaxMinutes)
$attempt = 0
$opened = $false

while ((Get-Date) -lt $deadline) {
    $attempt++
    $rss = Invoke-Web $RssUrl
    if (-not $rss) {
        if ($attempt % 5 -eq 0) { Write-Log "第 $attempt 次：RSS 取不到，继续重试" }
        Start-Sleep -Seconds $IntervalSec
        continue
    }

    $ids = Get-VideoIds $rss.Content 2
    foreach ($vid in $ids) {
        $info = Get-VideoInfo $vid
        if (-not $info) { continue }
        if (-not $info.live_content) { continue }        # 普通投稿，跳过
        if ($info.ended) { continue }                    # 已经播完的存档，跳过
        if (-not $info.is_live) { continue }             # 不是直播状态，跳过
        if (-not $info.start) { continue }               # 没有开播时间，没法判断

        $nowUtc = (Get-Date).ToUniversalTime()
        $waitSec = ($info.start - $nowUtc).TotalSeconds

        # 必须是预约时间**已经到**才打开。
        # 早几十秒打开只会停在倒计时页，YouTube 不一定会自动切换成播放。
        if ($waitSec -le 0) {
            Write-Log ("命中直播：{0}「{1}」预约 {2:yyyy-MM-dd HH:mm}（北京）" -f `
                $vid, $info.title, $info.start.ToLocalTime())
            $opened = $true
            break
        }
        elseif ($attempt -eq 1 -or $attempt % 5 -eq 0) {
            Write-Log ("等待中：{0} 预约 {1:HH:mm}（北京），还有 {2:N0} 分钟" -f `
                $vid, $info.start.ToLocalTime(), ($waitSec / 60))
        }
    }

    if ($opened) { break }
    Start-Sleep -Seconds $IntervalSec
}

if (-not $opened) {
    Write-Log "守了 $MaxMinutes 分钟没等到直播，放弃"
    exit 0
}

Start-Sleep -Seconds 20

# ---------- 3. 打开直播页 ----------
if ($DryRun) {
    Write-Log '[DryRun] 本应打开直播页，已跳过'
    exit 0
}

try {
    $chrome = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($chrome) {
        # --autoplay-policy：不加这个，Chrome 可能因为没有用户手势而不播放，
        # 那样字幕程序就采不到声音 —— 挂机场景最容易踩的坑。
        Start-Process -FilePath $chrome -ArgumentList @(
            '--autoplay-policy=no-user-gesture-required',
            '--new-window',
            $LiveUrl
        )
        Write-Log "已用 Chrome 打开：$LiveUrl"
    }
    else {
        Start-Process $LiveUrl
        Write-Log "没找到 Chrome，用默认浏览器打开：$LiveUrl"
    }
}
catch { Write-Log "打开浏览器失败：$($_.Exception.Message)" }

Write-Log '==== 自动打开结束 ===='
