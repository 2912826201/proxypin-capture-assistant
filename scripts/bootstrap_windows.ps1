#requires -version 5.1
<#
Agent-facing bootstrap. With no -Install it only inspects local files and returns a plan.
No administrator rights, Python, global PATH edits, GUI launch, or phone configuration.
Choose components explicitly. TargetApk must be a local, user-chosen official APK.
PythonArchiveUrl must be a current official python.org embeddable ZIP selected by the agent.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$AndroidTools,
    [switch]$ProxyPinDesktop,
    [switch]$ProxyPinAndroid,
    [string]$TargetApk,
    [string]$TargetPackage,
    [string]$DeviceSerial,
    [string]$PythonArchiveUrl,
    [string]$PythonArchiveSha256,
    [string]$PythonPath,
    [string]$ToolsRoot
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:Report = [ordered]@{
    schema_version = 1
    mode = $(if ($Install) { 'install' } else { 'detect_plan' })
    status = 'planned'
    tools_root = $null
    architecture = $null
    requested = [ordered]@{
        android_tools = [bool]$AndroidTools; proxypin_desktop = [bool]$ProxyPinDesktop
        proxypin_android = [bool]$ProxyPinAndroid; target_apk = [bool]$TargetApk
        target_package_verification = [bool]$TargetPackage
        python_runtime = [bool]($PythonArchiveUrl -or $PythonPath)
    }
    probed = New-Object 'System.Collections.Generic.List[object]'
    planned = New-Object 'System.Collections.Generic.List[object]'
    installed = New-Object 'System.Collections.Generic.List[object]'
    pending = New-Object 'System.Collections.Generic.List[object]'
    downloads = New-Object 'System.Collections.Generic.List[object]'
    devices = New-Object 'System.Collections.Generic.List[object]'
    notes = New-Object 'System.Collections.Generic.List[object]'
}
$script:Release = $null

function Add-Report {
    param([string]$Bucket, [string]$Component, [string]$State, [hashtable]$Details = @{})
    $item = [ordered]@{ component = $Component; state = $State }
    foreach ($key in $Details.Keys) { $item[$key] = $Details[$key] }
    $script:Report[$Bucket].Add([pscustomobject]$item)
}

function Stop-Bootstrap {
    param([string]$Message)
    $exception = New-Object System.InvalidOperationException -ArgumentList $Message
    $exception.Data['BootstrapSafeMessage'] = $true
    throw $exception
}

function Get-FailureDetail {
    param([object]$Failure)
    $exception = $Failure.Exception
    while ($exception) {
        if ($exception.Data.Contains('BootstrapSafeMessage')) { return $exception.Message }
        $exception = $exception.InnerException
    }
    # Do not serialize native output, signed redirect URLs, tokens, or arbitrary exception text.
    return ('Local or official-source operation failed (' + $Failure.Exception.GetType().Name + ').')
}

function Get-HostArchitecture {
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    switch ($arch.ToUpperInvariant()) {
        'AMD64' { 'x64' }; 'ARM64' { 'arm64' }; 'X86' { 'x86' }; default { 'unknown' }
    }
}

function Get-PeArchitecture {
    param([string]$Path)
    $stream = $null; $reader = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        $reader = New-Object IO.BinaryReader($stream)
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) { return 'unknown' }
        $stream.Position = 0x3C
        $offset = $reader.ReadInt32()
        if ($offset -lt 64 -or ($offset + 6) -gt $stream.Length) { return 'unknown' }
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x00004550) { return 'unknown' }
        switch ($reader.ReadUInt16()) {
            0x8664 { 'x64' }; 0xAA64 { 'arm64' }; 0x014C { 'x86' }; default { 'unknown' }
        }
    } catch { 'unknown' }
    finally { if ($reader) { $reader.Dispose() } elseif ($stream) { $stream.Dispose() } }
}

function Find-LocalExecutable {
    param([string]$Name, [string[]]$Directories, [string]$ExplicitPath)
    if ($ExplicitPath) {
        if (Test-Path -LiteralPath $ExplicitPath -PathType Leaf) {
            return (Get-Item -LiteralPath $ExplicitPath).FullName
        }
        return $null
    }
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command -and $command.Source -notmatch '\\WindowsApps\\') { return $command.Source }
    foreach ($directory in $Directories) {
        if ($directory -and (Test-Path -LiteralPath $directory -PathType Container)) {
            $file = Get-ChildItem -LiteralPath $directory -Filter $Name -File -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($file) { return $file.FullName }
        }
    }
    return $null
}

function Add-LocalProbe {
    param([string]$Component, [string]$Path)
    if ($Path) {
        $version = (Get-Item -LiteralPath $Path).VersionInfo.FileVersion
        Add-Report 'probed' $Component 'found_local_file' @{ path = $Path; file_version = $version; architecture = (Get-PeArchitecture $Path); runtime_verified = $false }
    } else { Add-Report 'probed' $Component 'not_found' }
}

function Assert-OfficialUrl {
    param([uri]$Uri, [string[]]$Hosts)
    if ($Uri.Scheme -ne 'https' -or $Uri.UserInfo -or -not $Uri.IsDefaultPort -or
        $Uri.DnsSafeHost.ToLowerInvariant() -notin $Hosts -or $Uri.Fragment) {
        Stop-Bootstrap 'URL is outside the allowed official HTTPS sources.'
    }
}

function Get-OfficialResponse {
    param([uri]$Uri, [string[]]$Hosts)
    if (-not $Install) { Stop-Bootstrap 'Network operations require -Install.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($redirect = 0; $redirect -le 6; $redirect++) {
        Assert-OfficialUrl $Uri $Hosts
        $request = [Net.HttpWebRequest]::Create($Uri)
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'ProxyPinAgentBootstrap/1'
        $request.Accept = 'application/vnd.github+json, application/xml, */*'
        $request.Timeout = 120000
        $request.ReadWriteTimeout = 120000
        $response = $request.GetResponse()
        $status = [int]$response.StatusCode
        if ($status -ge 300 -and $status -lt 400) {
            $location = $response.Headers['Location']
            $response.Close()
            if (-not $location) { Stop-Bootstrap 'Official source returned a redirect without a location.' }
            $Uri = New-Object Uri($Uri, $location)
            continue
        }
        if ($status -ne 200) { $response.Close(); Stop-Bootstrap 'Official source did not return HTTP 200.' }
        return $response
    }
    Stop-Bootstrap 'Official source exceeded the redirect limit.'
}

function Read-OfficialText {
    param([string]$Url, [string[]]$Hosts)
    $response = $null; $reader = $null
    try {
        $response = Get-OfficialResponse ([uri]$Url) $Hosts
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        return $reader.ReadToEnd()
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Close() }
    }
}

function Save-OfficialDownload {
    param([string]$Component, [string]$Url, [string]$FileName, [string[]]$Hosts,
        [string]$Digest, [long]$ExpectedSize = 0)
    if ($FileName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]+$') { Stop-Bootstrap 'Unsafe upstream asset filename.' }
    $directory = Join-Path $script:Report.tools_root ('downloads\' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $path = Join-Path $directory $FileName
    $response = $null; $output = $null; $inputStream = $null
    try {
        $response = Get-OfficialResponse ([uri]$Url) $Hosts
        $inputStream = $response.GetResponseStream()
        $output = [IO.File]::Create($path)
        $inputStream.CopyTo($output)
    } finally {
        if ($output) { $output.Dispose() }
        if ($inputStream) { $inputStream.Dispose() }
        if ($response) { $response.Close() }
    }
    $size = (Get-Item -LiteralPath $path).Length
    if ($ExpectedSize -gt 0 -and $size -ne $ExpectedSize) { Stop-Bootstrap 'Downloaded size differs from official metadata; file will not be used.' }
    $sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    $integrity = 'computed_sha256_only_no_upstream_digest'
    if ($Digest) {
        if ($Digest -notmatch '^(sha256|sha1):([0-9a-fA-F]+)$') { Stop-Bootstrap 'Unsupported official digest; file will not be used.' }
        $algorithm = $Matches[1].ToUpperInvariant(); $expected = $Matches[2].ToLowerInvariant()
        $actual = (Get-FileHash -LiteralPath $path -Algorithm $algorithm).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { Stop-Bootstrap 'Downloaded checksum differs from official metadata; file will not be used.' }
        $integrity = 'matched_upstream_' + $algorithm.ToLowerInvariant()
    }
    Add-Report 'downloads' $Component 'download_verified' @{
        source_url = $Url; path = $path; bytes = $size; sha256 = $sha256
        upstream_digest = $Digest; integrity = $integrity
    }
    return $path
}

function Assert-SafeZipEntries {
    param([object]$Entries, [string]$Destination)
    $base = [IO.Path]::GetFullPath($destination).TrimEnd('\') + '\'
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    [long]$total = 0
    foreach ($entry in $Entries) {
            $name = $entry.FullName.Replace('/', '\')
            if (-not $name -or [IO.Path]::IsPathRooted($name) -or $name.Contains(':') -or
                (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000 -or
                ($entry.ExternalAttributes -band 0x400) -ne 0) { Stop-Bootstrap 'Archive contains an unsafe path or link.' }
            foreach ($segment in $name.TrimEnd('\').Split('\')) {
                if (-not $segment -or $segment -in @('.', '..') -or $segment.TrimEnd(' ', '.') -ne $segment -or
                    $segment -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { Stop-Bootstrap 'Archive contains an unsafe Windows path.' }
            }
            $full = [IO.Path]::GetFullPath((Join-Path $destination $name))
            if (-not $full.StartsWith($base, [StringComparison]::OrdinalIgnoreCase) -or -not $names.Add($full)) {
                Stop-Bootstrap 'Archive path escapes its destination or duplicates another entry.'
            }
            $total += $entry.Length
            if ($total -gt 2GB -or $Entries.Count -gt 100000) { Stop-Bootstrap 'Archive exceeds the bootstrap extraction limit.' }
    }
}

function Expand-SafeZip {
    param([string]$Path, [string]$Component)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destination = Join-Path $script:Report.tools_root ($Component + '\' + [guid]::NewGuid().ToString('N'))
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try { Assert-SafeZipEntries $archive.Entries $destination
    } finally { $archive.Dispose() }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $destination)) | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($Path, $destination)
    return $destination
}

function Get-ProxyPinRelease {
    if ($script:Release) { return $script:Release }
    $url = 'https://api.github.com/repos/wanghongenpin/proxypin/releases/latest'
    $release = Read-OfficialText $url @('api.github.com') | ConvertFrom-Json
    if ($release.draft -or $release.prerelease -or -not $release.tag_name -or -not $release.assets) {
        Stop-Bootstrap 'The official latest release is not a usable stable release.'
    }
    $script:Release = $release
    Add-Report 'probed' 'proxypin_release' 'official_stable_metadata' @{
        tag = $release.tag_name; published_at = $release.published_at; source_url = $url
    }
    return $release
}

function Select-ProxyPinAsset {
    param([object]$Release, [string]$Kind, [string]$Architecture)
    $assets = @($Release.assets | Where-Object {
        $_.name -notmatch '(?i)(windows7|win7|legacy|setup)' -and
        $_.browser_download_url -match '^https://github\.com/wanghongenpin/proxypin/releases/download/'
    })
    if ($Kind -eq 'desktop') {
        $windows = @($assets | Where-Object { $_.name -match '(?i)^proxypin-windows.*\.zip$' })
        $pattern = switch ($Architecture) { 'x64' { '(?i)(x64|amd64)' }; 'arm64' { '(?i)(arm64|aarch64)' }; 'x86' { '(?i)(x86|ia32)' }; default { '(?!)' } }
        $matches = @($windows | Where-Object { $_.name -match $pattern })
        if ($matches.Count -eq 0) {
            $matches = @($windows | Where-Object { $_.name -notmatch '(?i)(x64|amd64|arm64|aarch64|x86|ia32)' })
        }
    } else {
        $android = @($assets | Where-Object { $_.name -match '(?i)^proxypin-android.*\.apk$' })
        $matches = @()
        if ($Architecture -eq 'arm64') { $matches = @($android | Where-Object { $_.name -match '(?i)arm64' }) }
        if ($matches.Count -eq 0) {
            $matches = @($android | Where-Object { $_.name -notmatch '(?i)(arm64|aarch64|armeabi|x64|x86|amd64|v7a)' })
        }
    }
    if ($matches.Count -ne 1) { Stop-Bootstrap 'Official release has no unique matching asset; agent must review the release metadata.' }
    $asset = $matches[0]
    $uri = [uri]$asset.browser_download_url
    Assert-OfficialUrl $uri @('github.com')
    $expectedPrefix = '/wanghongenpin/proxypin/releases/download/' + [uri]::EscapeDataString([string]$Release.tag_name) + '/'
    if (-not $uri.AbsolutePath.StartsWith($expectedPrefix, [StringComparison]::Ordinal) -or
        [uri]::UnescapeDataString($uri.Segments[-1]) -cne $asset.name) { Stop-Bootstrap 'Asset URL does not match the official release metadata.' }
    return $asset
}

function Save-ProxyPinAsset {
    param([object]$Asset, [string]$Component)
    $digest = $null
    if ($Asset.PSObject.Properties['digest']) { $digest = $Asset.digest }
    Save-OfficialDownload $Component $Asset.browser_download_url $Asset.name @(
        'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github-releases.githubusercontent.com'
    ) $digest ([long]$Asset.size)
}

function Install-AndroidTools {
    $url = 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
    $filename = 'platform-tools-latest-windows.zip'; $digest = $null; [long]$size = 0
    try {
        $xmlText = Read-OfficialText 'https://dl.google.com/android/repository/repository2-3.xml' @('dl.google.com')
        $settings = New-Object Xml.XmlReaderSettings
        $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $stringReader = New-Object IO.StringReader($xmlText)
        $xmlReader = [Xml.XmlReader]::Create($stringReader, $settings)
        try { $xml = New-Object Xml.XmlDocument; $xml.XmlResolver = $null; $xml.Load($xmlReader) }
        finally { $xmlReader.Dispose(); $stringReader.Dispose() }
        $package = $xml.SelectSingleNode("//*[local-name()='remotePackage' and @path='platform-tools']")
        $archive = $package.SelectSingleNode("*[local-name()='archives']/*[local-name()='archive'][*[local-name()='host-os']='windows']/*[local-name()='complete']")
        $filename = $archive.SelectSingleNode("*[local-name()='url']").InnerText
        if ($filename -notmatch '^platform-tools[_-][A-Za-z0-9._-]+\.zip$') { Stop-Bootstrap 'Unrecognized Google archive filename.' }
        $url = 'https://dl.google.com/android/repository/' + $filename
        $checksum = $archive.SelectSingleNode("*[local-name()='checksum']")
        $digest = $checksum.GetAttribute('type') + ':' + $checksum.InnerText.Trim()
        $size = [long]$archive.SelectSingleNode("*[local-name()='size']").InnerText
    } catch {
        $url = 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
        $filename = 'platform-tools-latest-windows.zip'; $digest = $null; $size = 0
        Add-Report 'notes' 'android_tools' 'google_checksum_metadata_unavailable' @{ detail = 'Using the official moving ZIP URL; SHA256 will be reported without an upstream checksum claim.' }
    }
    $zip = Save-OfficialDownload 'android_tools' $url $filename @('dl.google.com') $digest $size
    $directory = Expand-SafeZip $zip 'android-tools'
    $adb = Join-Path $directory 'platform-tools\adb.exe'
    if (-not (Test-Path -LiteralPath $adb -PathType Leaf)) { Stop-Bootstrap 'Official platform-tools archive did not contain platform-tools/adb.exe.' }
    $arch = Get-PeArchitecture $adb
    if ($arch -eq 'unknown' -or ($arch -ne $script:Report.architecture -and -not ($arch -eq 'x86' -and $script:Report.architecture -eq 'x64'))) {
        Add-Report 'pending' 'android_tools' 'architecture_needs_review' @{ path = $adb; executable_architecture = $arch; host_architecture = $script:Report.architecture }
        return $null
    }
    Add-Report 'installed' 'android_tools' 'portable_extracted' @{ path = $adb; architecture = $arch }
    return $adb
}

function Invoke-Adb {
    param([string]$Path, [string[]]$Arguments)
    if (-not $Install) { Stop-Bootstrap 'Device commands require -Install.' }
    $previous = $ErrorActionPreference
    try { $ErrorActionPreference = 'Continue'; $lines = @(& $Path @Arguments 2>&1); $exitCode = $LASTEXITCODE }
    finally { $ErrorActionPreference = $previous }
    [pscustomobject]@{ exit_code = $exitCode; output = ($lines | ForEach-Object { $_.ToString() }) -join "`n" }
}

function Select-AndroidDevice {
    param([string]$Adb)
    $result = Invoke-Adb $Adb @('devices', '-l')
    if ($result.exit_code -ne 0) { Stop-Bootstrap 'ADB could not enumerate devices.' }
    $devices = @()
    foreach ($line in ($result.output -split "`r?`n")) {
        if ($line -match '^(\S+)\s+(device|unauthorized|offline|recovery|sideload|bootloader|no permissions)(?:\s|$)') {
            $device = [pscustomobject]@{ serial = $Matches[1]; state = $Matches[2] }
            $devices += $device; $script:Report.devices.Add($device)
        }
    }
    if ($DeviceSerial) {
        $selected = @($devices | Where-Object { $_.serial -ceq $DeviceSerial })
        if ($selected.Count -eq 0) {
            Add-Report 'pending' 'android_device' 'requested_device_not_connected' @{ manual_step = 'Connect the selected phone with USB debugging enabled, then rerun after it appears in ADB.' }
            return $null
        }
        $device = $selected[0]
        if ($device.state -ne 'device') {
            Add-Report 'pending' 'android_device' $device.state @{ serial = $device.serial; manual_step = 'Unlock the selected phone and approve its USB debugging prompt; offline devices may need reconnecting. Rerun after ADB reports device.' }
            return $null
        }
        return $device.serial
    }
    $authorized = @($devices | Where-Object { $_.state -eq 'device' })
    if ($authorized.Count -eq 1) { return $authorized[0].serial }
    if ($authorized.Count -gt 1) {
        Add-Report 'pending' 'android_device' 'multiple_authorized_devices' @{ manual_step = 'The agent must select the intended device and rerun with DeviceSerial.' }
    } elseif (@($devices | Where-Object { $_.state -eq 'unauthorized' }).Count -gt 0) {
        Add-Report 'pending' 'android_device' 'unauthorized' @{ manual_step = 'Unlock the phone and approve the USB debugging prompt, then rerun after ADB reports device.' }
    } else {
        Add-Report 'pending' 'android_device' 'no_authorized_device' @{ manual_step = 'Connect the intended phone, enable USB debugging, and approve its prompt. Rerun after ADB reports device.' }
    }
    return $null
}

function Get-AndroidPackage {
    param([string]$Adb, [string]$Serial, [string]$Package)
    $path = Invoke-Adb $Adb @('-s', $Serial, 'shell', 'pm', 'path', $Package)
    if ($path.exit_code -ne 0 -or $path.output -notmatch '(?m)^package:') { return $null }
    $details = Invoke-Adb $Adb @('-s', $Serial, 'shell', 'dumpsys', 'package', $Package)
    $versionName = $null; $versionCode = $null
    if ($details.output -match '(?m)^\s*versionName=([^\r\n]+)') { $versionName = $Matches[1].Trim() }
    if ($details.output -match '\bversionCode=(\d+)') { $versionCode = $Matches[1] }
    [pscustomobject]@{ package = $Package; version_name = $versionName; version_code = $versionCode }
}

function Install-AndroidApk {
    param([string]$Adb, [string]$Serial, [string]$Apk, [string]$Component, [string]$Package)
    if ($Package) {
        $existing = Get-AndroidPackage $Adb $Serial $Package
        if ($existing) {
            Add-Report 'installed' $Component 'reused_installed_package' @{ serial = $Serial; package = $Package; version_name = $existing.version_name; version_code = $existing.version_code; target_apk_not_installed = $true; detail = 'Existing package reused. Updating an existing app requires a separate explicitly authorized and identity-verified flow.' }
            return
        }
    }
    # -r keeps existing data. Never use -d, uninstall, clear, or signature-bypass flags.
    $result = Invoke-Adb $Adb @('-s', $Serial, 'install', '-r', $Apk)
    if ($result.exit_code -ne 0 -or $result.output -notmatch '(?m)^Success\s*$') {
        $reason = 'ADB_INSTALL_FAILED'
        if ($result.output -match '\b(INSTALL_FAILED_[A-Z0-9_]+)\b') { $reason = $Matches[1] }
        Add-Report 'pending' $Component 'install_failed_existing_app_preserved' @{ path = $Apk; serial = $Serial; reason = $reason; detail = 'No uninstall, downgrade, data clearing, or signature bypass was attempted.' }
        return
    }
    if ($Package) {
        $verified = Get-AndroidPackage $Adb $Serial $Package
        if (-not $verified) {
            Add-Report 'pending' $Component 'adb_success_package_not_verified' @{ path = $Apk; serial = $Serial; expected_package = $Package }
            return
        }
        Add-Report 'installed' $Component 'adb_success_new_expected_package_present' @{ path = $Apk; serial = $Serial; package = $Package; version_name = $verified.version_name; version_code = $verified.version_code; verification = 'ADB reported Success; expected package was absent before and present after. APK manifest and signature were not independently parsed.' }
    } else {
        Add-Report 'pending' $Component 'installed_but_identity_unverified' @{ path = $Apk; serial = $Serial; installation_succeeded = $true; detail = 'ADB reported Success. TargetPackage was not supplied; the agent must verify the intended app identity and version.' }
    }
}

function Test-PythonRuntime {
    param([string]$Path)
    if (-not $Install) { Stop-Bootstrap 'Runtime execution requires -Install.' }
    # Avoid embedded double quotes: Windows PowerShell 5.1 native argument passing can remove them.
    $code = 'import sys,ssl,json,urllib.request,concurrent.futures; sys.exit(0 if sys.version_info >= (3,10) else 2)'
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& $Path --version 2>&1); $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -or ($lines -join '') -notmatch '^Python (\d+\.\d+\.\d+)(?:\S*)\s*$') { return $null }
        $version = $Matches[1]
        if ([version]$version -lt [version]'3.10') { return $null }
        $null = & $Path -I -c $code 2>&1; $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) { return $null }
    [pscustomobject]@{ version = $version; modules = 'ssl,json,urllib.request,concurrent.futures' }
}

function Install-PythonRuntime {
    $uri = [uri]$PythonArchiveUrl
    Assert-OfficialUrl $uri @('www.python.org', 'python.org')
    if ($uri.Query -or $uri.AbsolutePath -notmatch '^/ftp/python/(\d+\.\d+\.\d+)/python-\1-embed-(amd64|arm64|win32)\.zip$') {
        Stop-Bootstrap 'PythonArchiveUrl must be an exact stable official Windows embeddable ZIP URL.'
    }
    $arch = switch ($Matches[2]) { 'amd64' { 'x64' }; 'arm64' { 'arm64' }; 'win32' { 'x86' } }
    if ($arch -ne $script:Report.architecture) {
        Add-Report 'pending' 'python_runtime' 'archive_architecture_mismatch' @{ archive_architecture = $arch; host_architecture = $script:Report.architecture }
        return $null
    }
    $digest = $null
    if ($PythonArchiveSha256) { $digest = 'sha256:' + $PythonArchiveSha256.ToLowerInvariant() }
    $zip = Save-OfficialDownload 'python_runtime' $PythonArchiveUrl $uri.Segments[-1] @('www.python.org', 'python.org') $digest
    $directory = Expand-SafeZip $zip 'python-runtime'
    $python = Join-Path $directory 'python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or (Get-PeArchitecture $python) -ne $arch) {
        Stop-Bootstrap 'Extracted Python executable does not match the selected native architecture.'
    }
    return $python
}

try {
    if (-not $ToolsRoot) { $ToolsRoot = Join-Path $PSScriptRoot '..\app-tools' }
    $script:Report.tools_root = [IO.Path]::GetFullPath($ToolsRoot)
    $script:Report.architecture = Get-HostArchitecture
    if ($PythonArchiveSha256 -and (-not $PythonArchiveUrl -or $PythonArchiveSha256 -notmatch '^[0-9a-fA-F]{64}$')) { Stop-Bootstrap 'PythonArchiveSha256 requires PythonArchiveUrl and an upstream SHA256 value of 64 hexadecimal characters.' }
    if ($DeviceSerial -and $DeviceSerial -notmatch '^[A-Za-z0-9._:-]+$') { Stop-Bootstrap 'DeviceSerial has an invalid format.' }
    if ($TargetPackage -and $TargetPackage -notmatch '^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$') { Stop-Bootstrap 'TargetPackage has an invalid format.' }
    if ($TargetApk) {
        if (-not (Test-Path -LiteralPath $TargetApk -PathType Leaf) -or [IO.Path]::GetExtension($TargetApk) -ine '.apk') {
            Stop-Bootstrap 'TargetApk must be an existing local APK file chosen by the user.'
        }
        $TargetApk = (Get-Item -LiteralPath $TargetApk).FullName
    }
    $adb = Find-LocalExecutable 'adb.exe' @((Join-Path $script:Report.tools_root 'android-tools'))
    $desktopDirectories = @((Join-Path $script:Report.tools_root 'proxypin-desktop'))
    if ($env:LOCALAPPDATA) { $desktopDirectories += Join-Path $env:LOCALAPPDATA 'Programs\ProxyPin' }
    if ($env:ProgramFiles) { $desktopDirectories += Join-Path $env:ProgramFiles 'ProxyPin' }
    $desktop = Find-LocalExecutable 'ProxyPin.exe' $desktopDirectories
    $python = Find-LocalExecutable 'python.exe' @((Join-Path $script:Report.tools_root 'python-runtime')) $PythonPath
    Add-LocalProbe 'android_tools' $adb
    Add-LocalProbe 'proxypin_desktop' $desktop
    Add-LocalProbe 'python_runtime' $python
    Add-Report 'planned' 'android_tools' $(if ($adb) { 'reuse_local' } else { 'authorized_portable_install_needed' }) @{ install_option = '-Install -AndroidTools'; source = 'Official Google platform-tools' }
    Add-Report 'planned' 'proxypin_desktop' $(if ($desktop) { 'reuse_local_after_architecture_check' } else { 'authorized_portable_install_needed' }) @{ install_option = '-Install -ProxyPinDesktop'; source = 'Latest official stable wanghongenpin/proxypin Windows ZIP' }
    Add-Report 'planned' 'python_runtime' $(if ($python) { 'verify_local_version_and_stdlib' } else { 'agent_selects_official_embeddable_zip_if_needed' }) @{ install_option = '-Install -PythonPath or -PythonArchiveUrl'; minimum_version = '3.10' }
    if ($TargetApk) {
        Add-Report 'probed' 'target_apk' 'local_file' @{ path = $TargetApk; sha256 = (Get-FileHash -LiteralPath $TargetApk -Algorithm SHA256).Hash.ToLowerInvariant(); provenance = 'User-selected local APK; official provenance must be checked by the agent.' }
    }
    if (-not $Install) {
        Add-Report 'notes' 'bootstrap' 'no_changes' @{ detail = 'No network, executable, or device commands ran. Invoke -Install with explicit components only after authorization; PythonArchiveUrl must come from a current official release page.' }
        if ($ProxyPinAndroid -or $TargetApk -or $TargetPackage) {
            Add-Report 'pending' 'android_device' 'not_probed_in_detect_mode' @{ detail = 'Device authorization and installed packages will be checked only during the authorized install run.' }
        }
    } else {
        if (-not ($AndroidTools -or $ProxyPinDesktop -or $ProxyPinAndroid -or $TargetApk -or $TargetPackage -or $PythonArchiveUrl -or $PythonPath)) {
            Add-Report 'pending' 'bootstrap' 'no_components_selected' @{ detail = 'The agent must select the components authorized by the user.' }
        }
        if ($AndroidTools) {
            try {
                if ($adb) { Add-Report 'installed' 'android_tools' 'reused_existing' @{ path = $adb } }
                else { $adb = Install-AndroidTools }
                if ($adb) {
                    $adbVersion = Invoke-Adb $adb @('version')
                    if ($adbVersion.exit_code -eq 0 -and $adbVersion.output -match 'Android Debug Bridge version ([^\r\n]+)') {
                        Add-Report 'probed' 'android_tools' 'runtime_verified' @{ path = $adb; version = $Matches[1].Trim() }
                    } else { Add-Report 'pending' 'android_tools' 'runtime_verification_failed' @{ path = $adb }; $adb = $null }
                }
            } catch { Add-Report 'pending' 'android_tools' 'operation_failed' @{ detail = (Get-FailureDetail $_) } }
        }
        if ($ProxyPinDesktop) {
            try {
                if (-not $desktop) {
                    $release = Get-ProxyPinRelease
                    $asset = Select-ProxyPinAsset $release 'desktop' $script:Report.architecture
                    $zip = Save-ProxyPinAsset $asset 'proxypin_desktop'
                    $directory = Expand-SafeZip $zip 'proxypin-desktop'
                    $executables = @(Get-ChildItem -LiteralPath $directory -Filter 'ProxyPin.exe' -File -Recurse)
                    if ($executables.Count -ne 1) { Stop-Bootstrap 'Official ZIP has no unique ProxyPin.exe.' }
                    $desktop = $executables[0].FullName
                }
                $desktopArch = Get-PeArchitecture $desktop
                if ($desktopArch -ne $script:Report.architecture -or $desktopArch -eq 'unknown') {
                    Add-Report 'pending' 'proxypin_desktop' 'architecture_needs_review' @{ path = $desktop; executable_architecture = $desktopArch; host_architecture = $script:Report.architecture; detail = 'No GUI was launched. An x64 executable on ARM64 requires separate emulation validation.' }
                } else {
                    Add-Report 'installed' 'proxypin_desktop' 'available_local_no_gui_launch' @{ path = $desktop; architecture = $desktopArch; file_version = (Get-Item -LiteralPath $desktop).VersionInfo.FileVersion }
                }
            } catch { Add-Report 'pending' 'proxypin_desktop' 'operation_failed' @{ detail = (Get-FailureDetail $_) } }
        }
        if ($PythonArchiveUrl -or $PythonPath) {
            try {
                $verifiedPython = $null
                if ($python) { $verifiedPython = Test-PythonRuntime $python }
                if (-not $verifiedPython -and $PythonArchiveUrl) {
                    $python = Install-PythonRuntime
                    if ($python) { $verifiedPython = Test-PythonRuntime $python }
                }
                if ($verifiedPython) {
                    Add-Report 'installed' 'python_runtime' 'runtime_and_stdlib_verified' @{ path = $python; version = $verifiedPython.version; modules = $verifiedPython.modules }
                } elseif ($python) { Add-Report 'pending' 'python_runtime' 'runtime_or_stdlib_verification_failed' @{ path = $python; minimum_version = '3.10' } }
                elseif (-not $PythonArchiveUrl) { Add-Report 'pending' 'python_runtime' 'runtime_unavailable' @{ detail = 'The specified PythonPath was not found. Agent must locate a runtime or select an official PythonArchiveUrl.' } }
            } catch { Add-Report 'pending' 'python_runtime' 'operation_failed' @{ detail = (Get-FailureDetail $_) } }
        }
        if ($ProxyPinAndroid -or $TargetApk -or $TargetPackage) {
            if (-not $adb) {
                Add-Report 'pending' 'android_device' 'adb_unavailable' @{ detail = 'Authorize AndroidTools installation and rerun with -Install -AndroidTools plus the intended Android action.' }
            } else {
                try {
                    $serial = Select-AndroidDevice $adb
                    if ($serial) {
                        if ($ProxyPinAndroid) {
                            try {
                                $existing = Get-AndroidPackage $adb $serial 'com.network.proxy'
                                if ($existing) {
                                    Add-Report 'installed' 'proxypin_android' 'reused_installed_package' @{ serial = $serial; package = $existing.package; version_name = $existing.version_name; version_code = $existing.version_code }
                                } else {
                                    $abiResult = Invoke-Adb $adb @('-s', $serial, 'shell', 'getprop', 'ro.product.cpu.abilist')
                                    $androidArch = 'universal'
                                    if ($abiResult.exit_code -eq 0 -and ($abiResult.output.Trim() -split ',') -contains 'arm64-v8a') { $androidArch = 'arm64' }
                                    $release = Get-ProxyPinRelease
                                    $asset = Select-ProxyPinAsset $release 'android' $androidArch
                                    $apk = Save-ProxyPinAsset $asset 'proxypin_android'
                                    Install-AndroidApk $adb $serial $apk 'proxypin_android' 'com.network.proxy'
                                }
                            } catch { Add-Report 'pending' 'proxypin_android' 'operation_failed' @{ detail = (Get-FailureDetail $_) } }
                        }
                        if ($TargetApk) { Install-AndroidApk $adb $serial $TargetApk 'target_apk' $TargetPackage }
                        elseif ($TargetPackage) {
                            $package = Get-AndroidPackage $adb $serial $TargetPackage
                            if ($package) { Add-Report 'probed' 'target_package' 'installed_package_verified' @{ serial = $serial; package = $package.package; version_name = $package.version_name; version_code = $package.version_code } }
                            else { Add-Report 'pending' 'target_package' 'package_not_found' @{ serial = $serial; package = $TargetPackage } }
                        }
                    }
                } catch { Add-Report 'pending' 'android_device' 'operation_failed' @{ detail = (Get-FailureDetail $_) } }
            }
        }
        $script:Report.status = $(if ($script:Report.pending.Count -gt 0) { 'pending' } else { 'complete' })
    }
} catch {
    Add-Report 'pending' 'bootstrap' 'invalid_input_or_local_probe_failed' @{ detail = (Get-FailureDetail $_) }
    $script:Report.status = 'pending'
}
Add-Report 'notes' 'integrity' 'scope' @{ detail = 'Hashes check downloaded bytes against available upstream metadata; they do not independently establish a publisher signature. No certificate, proxy, VPN, root, phone data, or global PATH settings were changed.' }
$script:Report | ConvertTo-Json -Depth 9
