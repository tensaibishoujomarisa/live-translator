# ============================================================
#  火羽ひのめ 直播自动打开（轮询版）
# ------------------------------------------------------------
#  由 Windows 计划任务在每周三/六/日 07:55 调用。
#
#  为什么不能直接在 07:55 打开 /live：
#    她不提前挂「预约直播」，7:55 时页面还是"没有直播"，
#    等她 08:00 开播时，那个页面**不会自己变成直播**。
#    必须等她真正开播之后再打开，YouTube 才会自动播放。
#
#  所以做法是：
#    1. 确保字幕程序在跑（它采集系统声音做字幕）
#    2. 每分钟查一次她的 YouTube RSS，直到出现「新的 videoId」
#    3. 一旦发现，等 30 秒（让直播稳定），用 Chrome 打开 /live
#
#  日志：logs\auto_open.log
#  已见 videoId：logs\last_video_id.txt
# ============================================================

$ErrorActionPreference = 'Continue'
$Root = 'G:\projects\jp-live-translator'
$LogDir = Join-Path $Root 'logs'
$Log = Join-Path $LogDir 'auto_open.log'
$StateFile = Join-Path $LogDir 'last_video_id.txt'
$ChannelId = 'UCypbWFLH4AVFdao9XvpU_Xw'
$RssUrl = "https://www.youtube.com/feeds/videos.xml?channel_id=$ChannelId"
$LiveUrl = "https://www.youtube.com/channel/$ChannelId/live"

# 轮询参数
$MaxMinutes = 90
$IntervalSec = 60
$ProxyCandidates = @('http://127.0.0.1:7890', 'http://127.0.0.1:10809')

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log([string]$Msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Msg
    Add-Content -Path $Log -Value $line -Encoding utf8
}

function Get-Rss([string]$Proxy) {
    $params = @{
        Uri             = $RssUrl
        UseBasicParsing = $true
        TimeoutSec      = 20
        UserAgent       = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36'
    }
    if ($Proxy) { $params['Proxy'] = $Proxy }
    return Invoke-WebRequest @params
}

function Fetch-Rss {
    # 先直连，失败再逐个试代理（用户平时可能开着 Clash/TUN）
    try { return Get-Rss $null } catch { }
    foreach ($p in $ProxyCandidates) {
        try { return Get-Rss $p } catch { }
    }
    return $null
}

function Get-LatestId([string]$Xml) {
    $m = [regex]::Match($Xml, '<yt:videoId>([^<]+)</yt:videoId>')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

Write-Log '==== 自动打开开始（轮询模式）===='

# ---------- 1. 字幕程序 ----------
try {
    $running = @(Get-Process electron -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Root, 'OrdinalIgnoreCase') })
    if ($running.Count -gt 0) {
        Write-Log "字幕程序已在运行（$($running.Count) 个进程）"
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
        else {
            Write-Log "[警告] 找不到 $bat"
        }
    }
}
catch {
    Write-Log "启动字幕程序失败：$($_.Exception.Message)"
}

# ---------- 2. 记住「开播前已经存在的 videoId」 ----------
$baseline = $null
if (Test-Path $StateFile) {
    $baseline = (Get-Content $StateFile -Raw -ErrorAction SilentlyContinue).Trim()
}
$resp = Fetch-Rss
if ($resp) {
    $latest = Get-LatestId $resp.Content
    if ($latest -and -not $baseline) {
        $baseline = $latest
        Set-Content -Path $StateFile -Value $baseline -Encoding utf8
    }
}
Write-Log "基准 videoId：$baseline"

# ---------- 3. 轮询 ----------
$deadline = (Get-Date).AddMinutes($MaxMinutes)
$found = $false
$attempt = 0
while ((Get-Date) -lt $deadline) {
    $attempt++
    Start-Sleep -Seconds $IntervalSec
    $resp = Fetch-Rss
    if (-not $resp) {
        if ($attempt % 5 -eq 0) { Write-Log "第 $attempt 次：网络取不到 RSS，继续重试" }
        continue
    }
    $latest = Get-LatestId $resp.Content
    if (-not $latest) { continue }
    if ($latest -ne $baseline) {
        Write-Log "检测到新视频：$latest（第 $attempt 次检查）→ 等 30 秒"
        $found = $true
        break
    }
    if ($attempt % 10 -eq 0) { Write-Log "第 $attempt 次：还没有新视频（$latest）" }
}

if (-not $found) {
    Write-Log "等了 $MaxMinutes 分钟没检测到开播，放弃（今天可能没播）"
    exit 0
}

Set-Content -Path $StateFile -Value $latest -Encoding utf8
Start-Sleep -Seconds 30

# ---------- 4. 打开直播页 ----------
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
catch {
    Write-Log "打开浏览器失败：$($_.Exception.Message)"
}

Write-Log '==== 自动打开结束 ===='
