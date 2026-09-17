# Render the first PowerPoint slide to PNG through an isolated PowerPoint COM process.
# Inputs are environment-only; output is a tiny JSON status and never document content.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$prior = @(Get-Process POWERPNT -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$timeout = 10
if ($env:CORPUSPICK_POWERPOINT_TIMEOUT) {
    [int]::TryParse($env:CORPUSPICK_POWERPOINT_TIMEOUT, [ref]$timeout) | Out-Null
    if ($timeout -lt 1) { $timeout = 10 }
}
$job = Start-Job -ArgumentList $env:CORPUSPICK_DOCUMENT, $env:CORPUSPICK_OUTPUT, $prior, $env:CORPUSPICK_OWNER -ScriptBlock {
    param($documentPath, $outputPath, $priorIds, $ownerPath)
    $ErrorActionPreference = 'Stop'
    $powerpoint = $null; $presentation = $null; $owned = $false; $stage = 'startup'
    try {
        $powerpoint = New-Object -ComObject PowerPoint.Application
        $stage = 'isolation'
        $processes = @(Get-Process POWERPNT -ErrorAction SilentlyContinue |
            Where-Object { $priorIds -notcontains $_.Id } |
            Sort-Object StartTime -Descending)
        $ownedProcess = $processes | Select-Object -First 1
        if ($ownedProcess) {
            $owned = $true
            $owner = [pscustomobject]@{ ownedPid = $ownedProcess.Id; started = $ownedProcess.StartTime.ToUniversalTime().Ticks }
            if ($ownerPath) { $owner | ConvertTo-Json -Compress | Set-Content -LiteralPath $ownerPath -Encoding UTF8 }
            $owner
        }
        $stage = 'settings'
        $powerpoint.DisplayAlerts = 1
        try { $powerpoint.AutomationSecurity = 3 } catch {}
        # Presentations.Open(FileName, ReadOnly, Untitled, WithWindow)
        $stage = 'open'
        $presentation = $powerpoint.Presentations.Open($documentPath, -1, 0, 0)
        if ($presentation.Slides.Count -lt 1) { throw 'No slides' }
        $stage = 'export'
        $slide = $presentation.Slides.Item(1)
        $slide.Export($outputPath, 'PNG', 1280, 720)
        [pscustomobject]@{ ok = $true }
    } catch {
        [pscustomobject]@{ ok = $false; stage = $stage; code = $_.Exception.HResult }
    } finally {
        if ($presentation) { try { $presentation.Close() } catch {}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($presentation) }
        if ($powerpoint) {
            if ($owned) { try { $powerpoint.Quit() } catch {} }
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($powerpoint)
        }
    }
}
try {
    $finished = Wait-Job $job -Timeout $timeout
    $items = @(Receive-Job $job -ErrorAction SilentlyContinue)
    if (-not $finished) {
        $owner = $items | Where-Object { $_.PSObject.Properties.Name -contains 'ownedPid' } | Select-Object -First 1
        if ($owner) {
            $process = Get-Process -Id $owner.ownedPid -ErrorAction SilentlyContinue
            if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq $owner.started) {
                Stop-Process -InputObject $process -Force -ErrorAction SilentlyContinue
            }
        }
        Stop-Job $job
        @{ ok = $false; stage = 'timeout' } | ConvertTo-Json -Compress
    } else {
        $result = $items | Where-Object { $_.PSObject.Properties.Name -notcontains 'ownedPid' } | Select-Object -Last 1
        if ($result -and $result.ok) {
            @{ ok = $true } | ConvertTo-Json -Compress
        } else {
            @{ ok = $false; stage = $result.stage; code = $result.code } | ConvertTo-Json -Compress
        }
    }
} finally {
    Remove-Job $job -Force -ErrorAction SilentlyContinue
}
