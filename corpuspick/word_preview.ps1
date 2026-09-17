# Render the first Word page to PDF through an isolated Word COM process.
# Inputs are environment-only; output is a tiny JSON status and never document content.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$prior = @(Get-Process WINWORD -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$timeout = 10
if ($env:CORPUSPICK_WORD_TIMEOUT) {
    [int]::TryParse($env:CORPUSPICK_WORD_TIMEOUT, [ref]$timeout) | Out-Null
    if ($timeout -lt 1) { $timeout = 10 }
}
$job = Start-Job -ArgumentList $env:CORPUSPICK_DOCUMENT, $env:CORPUSPICK_OUTPUT, $prior, $env:CORPUSPICK_OWNER -ScriptBlock {
    param($documentPath, $outputPath, $priorIds, $ownerPath)
    $ErrorActionPreference = 'Stop'
    $word = $null; $doc = $null; $probe = $null; $owned = $false; $oldUpdateLinks = $null; $stage = 'startup'
    try {
        Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class WordWindow { [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p); }'
        $word = New-Object -ComObject Word.Application
        $stage = 'isolation'
        $probe = $word.Documents.Add()
        [uint32]$wordPid = 0
        [void][WordWindow]::GetWindowThreadProcessId([IntPtr]$probe.ActiveWindow.Hwnd, [ref]$wordPid)
        if ($wordPid -eq 0 -or $priorIds -contains $wordPid) { throw 'Not isolated' }
        $owned = $true
        $probe.Close(0)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($probe)
        $probe = $null
        $process = Get-Process -Id $wordPid
        $owner = [pscustomobject]@{ ownedPid = $wordPid; started = $process.StartTime.ToUniversalTime().Ticks }
        if ($ownerPath) { $owner | ConvertTo-Json -Compress | Set-Content -LiteralPath $ownerPath -Encoding UTF8 }
        $owner
        $stage = 'settings'
        $word.Visible = $false
        $word.DisplayAlerts = 0
        $word.AutomationSecurity = 3
        $oldUpdateLinks = $word.Options.UpdateLinksAtOpen
        $word.Options.UpdateLinksAtOpen = $false
        $missing = [Type]::Missing
        $stage = 'open'
        $openArguments = [object[]]@($documentPath, $false, $true, $false, 'CorpusPick-No-Password',
                                    $missing, $false, 'CorpusPick-No-Password', $missing, $missing,
                                    $missing, $false, $false, $missing, $true, $missing)
        $documents = $word.Documents
        $doc = $documents.GetType().InvokeMember('Open', [Reflection.BindingFlags]::InvokeMethod,
                                                  $null, $documents, $openArguments)
        $stage = 'repaginate'
        $doc.Repaginate()
        $stage = 'export'
        $wdExportFormatPDF = 17
        $wdExportOptimizeForOnScreen = 1
        $wdExportFromTo = 3
        $wdExportDocumentContent = 0
        $wdExportCreateNoBookmarks = 0
        $doc.ExportAsFixedFormat($outputPath, $wdExportFormatPDF, $false, $wdExportOptimizeForOnScreen,
                                 $wdExportFromTo, 1, 1, $wdExportDocumentContent, $false, $false,
                                 $wdExportCreateNoBookmarks, $false, $true, $false)
        [pscustomobject]@{ ok = $true }
    } catch {
        [pscustomobject]@{ ok = $false; stage = $stage; code = $_.Exception.HResult }
    } finally {
        if ($probe) { try { $probe.Close(0) } catch {}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($probe) }
        if ($doc) { try { $doc.Close(0) } catch {}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc) }
        if ($word) {
            if ($null -ne $oldUpdateLinks) { try { $word.Options.UpdateLinksAtOpen = $oldUpdateLinks } catch {} }
            if ($owned) { try { $word.Quit(0) } catch {} }
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)
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
