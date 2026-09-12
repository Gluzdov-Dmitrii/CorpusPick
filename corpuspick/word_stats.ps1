# Own implementation inspired by the supplied page-count workflow.
# Input is environment-only; output contains counts only. Never print exceptions.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$prior = @(Get-Process WINWORD -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$job = Start-Job -ArgumentList $env:CORPUSPICK_DOCUMENT, $prior -ScriptBlock {
    param($documentPath, $priorIds)
    $ErrorActionPreference = 'Stop'
    $word = $null; $doc = $null; $probe = $null; $owned = $false; $oldUpdateLinks = $null; $stage = 'startup'
    try {
        Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class WordWindow { [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p); }'
        $word = New-Object -ComObject Word.Application
        $stage = 'isolation'
        # Word exposes the native handle on Window, not Application.
        $probe = $word.Documents.Add()
        [uint32]$wordPid = 0
        [void][WordWindow]::GetWindowThreadProcessId([IntPtr]$probe.ActiveWindow.Hwnd, [ref]$wordPid)
        if ($wordPid -eq 0 -or $priorIds -contains $wordPid) { throw 'Not isolated' }
        $owned = $true
        $probe.Close(0)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($probe)
        $probe = $null
        $process = Get-Process -Id $wordPid
        [pscustomobject]@{ ownedPid = $wordPid; started = $process.StartTime.ToUniversalTime().Ticks }
        $stage = 'settings'
        $word.Visible = $false
        $word.DisplayAlerts = 0
        $word.AutomationSecurity = 3
        $oldUpdateLinks = $word.Options.UpdateLinksAtOpen
        $word.Options.UpdateLinksAtOpen = $false
        $missing = [Type]::Missing
        # Password arguments prevent interactive password dialogs. Read-only, no MRU.
        $stage = 'open'
        $openArguments = [object[]]@($documentPath, $false, $true, $false, 'CorpusPick-No-Password',
                                    $missing, $false, 'CorpusPick-No-Password', $missing, $missing,
                                    $missing, $false, $false, $missing, $true, $missing)
        $documents = $word.Documents
        $doc = $documents.GetType().InvokeMember('Open', [Reflection.BindingFlags]::InvokeMethod,
                                                  $null, $documents, $openArguments)
        $stage = 'count'
        $doc.Repaginate()
        $appendices = 0
        foreach ($paragraph in $doc.Paragraphs) {
            if ($paragraph.Range.Text.Trim() -match '^(?i:приложение|appendix)\s+(?:[А-ЯЁA-Z]|\d+)(?:\s*[:.\-–—]\s*.*)?$') { $appendices++ }
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($paragraph)
        }
        [pscustomobject]@{ pages = $doc.ComputeStatistics(2); figures = $doc.InlineShapes.Count + $doc.Shapes.Count;
                          tables = $doc.Tables.Count; appendices = $appendices }
    } catch {
        [pscustomobject]@{ info = 'Word'; stage = $stage; code = $_.Exception.HResult }
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
    $finished = Wait-Job $job -Timeout 120
    $items = @(Receive-Job $job -ErrorAction SilentlyContinue)
    if (-not $finished) {
        # Kill only the process positively identified as created by this job.
        $owner = $items | Where-Object { $_.PSObject.Properties.Name -contains 'ownedPid' } | Select-Object -First 1
        if ($owner) {
            $process = Get-Process -Id $owner.ownedPid -ErrorAction SilentlyContinue
            if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq $owner.started) {
                Stop-Process -InputObject $process -Force -ErrorAction SilentlyContinue
            }
        }
        Stop-Job $job
        @{ info = 'Word: превышено время 120 секунд на документ' } | ConvertTo-Json -Compress
    } else {
        $result = $items | Where-Object { $_.PSObject.Properties.Name -notcontains 'ownedPid' } | Select-Object -Last 1
        if ($result.PSObject.Properties.Name -contains 'pages') {
            @{ pages = $result.pages; figures = $result.figures; tables = $result.tables; appendices = $result.appendices } | ConvertTo-Json -Compress
        } else {
            @{ info = ('Word: этап {0}, код {1}. Проверьте доступность Word и защиту файла.' -f $result.stage, $result.code) } | ConvertTo-Json -Compress
        }
    }
} finally {
    Remove-Job $job -Force -ErrorAction SilentlyContinue
}
