# Frontend development launcher:
# 1. Reuse a healthy backend when one is already running.
# 2. Wait for an existing local backend that is still starting.
# 3. Otherwise start the backend, wait for /api/health, then start Next.js.
# 4. Stop only the backend process tree started by this script when Next.js exits.
[CmdletBinding()]
param(
    [string]$BackendUrl = $env:LIGHTING_API_URL,
    [ValidateRange(1, 900)]
    [int]$ReadyTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$backendProc = $null
$killJob = $null

if ([string]::IsNullOrWhiteSpace($BackendUrl)) {
    $BackendUrl = "http://127.0.0.1:8000"
}

try {
    $backendUri = [Uri]$BackendUrl
    if ($backendUri.Scheme -notin @("http", "https") -or [string]::IsNullOrWhiteSpace($backendUri.Host)) {
        throw "BackendUrl must be an HTTP(S) URL."
    }
} catch {
    Write-Host "[dev] Error: LIGHTING_API_URL must be a valid HTTP(S) URL. Current value: '$BackendUrl'." -ForegroundColor Red
    exit 1
}

$healthUrl = "$($BackendUrl.TrimEnd('/'))/api/health"
$env:LIGHTING_API_URL = $BackendUrl
$backendHost = $backendUri.Host.ToLowerInvariant()
$isLocalBackend = $backendHost -in @("127.0.0.1", "localhost", "::1")
$backendPort = if ($backendUri.IsDefaultPort) {
    if ($backendUri.Scheme -eq "https") { 443 } else { 80 }
} else {
    $backendUri.Port
}

function Test-BackendHealthy {
    try {
        $resp = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return ($null -ne $resp -and $resp.status -eq "ok")
    } catch {
        return $false
    }
}

function Test-LocalPortListening {
    if (-not $isLocalBackend) {
        return $false
    }

    return $null -ne (Get-NetTCPConnection -LocalPort $backendPort -State Listen -ErrorAction SilentlyContinue)
}

function Stop-StartedBackend {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }

    $Process.Refresh()
    if (-not $Process.HasExited) {
        taskkill /PID $Process.Id /T /F | Out-Null
    }
}

# Windows Job Object with KILL_ON_JOB_CLOSE: when this script's process exits
# for ANY reason (normal exit, Ctrl+C, closing the terminal window, crash),
# the OS closes the job handle and force-kills the whole backend process tree
# (uv -> uvicorn --reload workers). This covers the case where the taskkill in
# the finally block can never run because the shell itself is being torn down.
if (-not ('KillOnCloseJob' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public class KillOnCloseJob
{
    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr hJob, int JobObjectInformationClass,
        ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION lpJobObjectInformation, uint cbJobObjectInformationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;

    private IntPtr _job;

    public KillOnCloseJob()
    {
        _job = CreateJobObject(IntPtr.Zero, null);
        if (_job == IntPtr.Zero)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
        if (!SetInformationJobObject(_job, JobObjectExtendedLimitInformation, ref info,
            (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))))
        {
            CloseHandle(_job);
            _job = IntPtr.Zero;
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
    }

    // Returns false when the process could not be assigned (e.g. it already
    // belongs to another job); the caller then relies on taskkill instead.
    public bool Assign(int pid)
    {
        if (_job == IntPtr.Zero)
        {
            return false;
        }

        using (var process = System.Diagnostics.Process.GetProcessById(pid))
        {
            return AssignProcessToJobObject(_job, process.Handle);
        }
    }

    public void Dispose()
    {
        if (_job != IntPtr.Zero)
        {
            CloseHandle(_job);
            _job = IntPtr.Zero;
        }
    }
}
'@
}

function Wait-ForBackend {
    param([System.Diagnostics.Process]$StartedProcess)

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-BackendHealthy) {
            return $true
        }

        if ($null -ne $StartedProcess) {
            $StartedProcess.Refresh()
            if ($StartedProcess.HasExited) {
                Write-Host "[dev] Error: the backend started by this script exited early (code $($StartedProcess.ExitCode))." -ForegroundColor Red
                return $false
            }
        }

        Start-Sleep -Milliseconds 500
    }

    return $false
}

$nextBin = Join-Path $PSScriptRoot "..\node_modules\.bin\next.ps1"
if (-not (Test-Path -LiteralPath $nextBin)) {
    Write-Host "[dev] Error: $nextBin was not found. Run npm install in the web directory first." -ForegroundColor Red
    exit 1
}

try {
    if (Test-BackendHealthy) {
        Write-Host "[dev] Backend is ready: $healthUrl (reusing the existing process)." -ForegroundColor Green
    } elseif (Test-LocalPortListening) {
        Write-Host "[dev] A backend is starting on port $backendPort. Waiting up to $ReadyTimeoutSeconds seconds for health..." -ForegroundColor Cyan
        if (-not (Wait-ForBackend)) {
            Write-Host "[dev] Error: backend did not pass its health check within $ReadyTimeoutSeconds seconds: $healthUrl" -ForegroundColor Red
            exit 1
        }
        Write-Host "[dev] Backend is ready. Starting the frontend." -ForegroundColor Green
    } elseif ($isLocalBackend -and $backendUri.Scheme -eq "http") {
        Write-Host "[dev] Backend is not running. Starting: uv run uvicorn lighting_agent.web_api:app --reload --host $backendHost --port $backendPort" -ForegroundColor Cyan
        $backendProc = Start-Process -FilePath "uv" `
            -ArgumentList @("run", "uvicorn", "lighting_agent.web_api:app", "--reload", "--host", $backendHost, "--port", $backendPort) `
            -WorkingDirectory $repoRoot -PassThru -WindowStyle Hidden

        try {
            $killJob = New-Object KillOnCloseJob
            if (-not $killJob.Assign($backendProc.Id)) {
                Write-Host "[dev] Warning: could not attach the backend to the kill-on-close job; falling back to shutdown cleanup." -ForegroundColor Yellow
            }
        } catch {
            Write-Host "[dev] Warning: failed to create the kill-on-close job ($($_.Exception.Message)); falling back to shutdown cleanup." -ForegroundColor Yellow
        }

        Write-Host "[dev] Waiting up to $ReadyTimeoutSeconds seconds for the backend to become healthy..." -ForegroundColor Cyan
        if (-not (Wait-ForBackend -StartedProcess $backendProc)) {
            Write-Host "[dev] Error: backend did not pass its health check within $ReadyTimeoutSeconds seconds: $healthUrl" -ForegroundColor Red
            exit 1
        }
        Write-Host "[dev] Backend is ready. Starting the frontend." -ForegroundColor Green
    } else {
        Write-Host "[dev] Error: the remote or HTTPS backend is not ready: $healthUrl" -ForegroundColor Red
        exit 1
    }

    # Directly invoke Next's PowerShell shim so Ctrl+C is forwarded to Node.
    & $nextBin dev
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    if ($null -ne $backendProc) {
        Write-Host "[dev] Frontend exited. Stopping the backend process started by this script." -ForegroundColor Cyan
        Stop-StartedBackend -Process $backendProc
    }
    if ($null -ne $killJob) {
        $killJob.Dispose()
    }
}
