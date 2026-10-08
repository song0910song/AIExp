param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Windows.Forms,System.Drawing
Add-Type @'
using System;
using System.Collections.Generic;
using System.Text;
using System.Runtime.InteropServices;
public static class DialuxDesktop {
    public delegate bool WindowCallback(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")] public static extern bool EnumWindows(WindowCallback callback, IntPtr param);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, WindowCallback callback, IntPtr param);
    [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr window, uint msg, IntPtr param, string value);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageW")] public static extern IntPtr ReadText(IntPtr window, uint msg, IntPtr param, StringBuilder value);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint msg, IntPtr param, IntPtr value);
    [DllImport("user32.dll")] public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr desktop);
    public static IntPtr[] MainWindows(uint targetPid) {
        var windows = new List<IntPtr>();
        EnumWindows((hwnd, unused) => {
            uint pid;
            GetWindowThreadProcessId(hwnd, out pid);
            if (pid != targetPid || !IsWindowVisible(hwnd) || GetWindow(hwnd, 4) != IntPtr.Zero) return true;
            var title = new StringBuilder(2048);
            var className = new StringBuilder(256);
            GetWindowText(hwnd, title, title.Capacity);
            GetClassName(hwnd, className, className.Capacity);
            if (title.ToString().Contains("DIALux evo") && className.ToString().StartsWith("HwndWrapper[DIALux")) windows.Add(hwnd);
            return true;
        }, IntPtr.Zero);
        return windows.ToArray();
    }
    public static IntPtr[] DialogWindows(uint targetPid) {
        var windows = new List<IntPtr>();
        EnumWindows((hwnd, unused) => {
            uint pid;
            GetWindowThreadProcessId(hwnd, out pid);
            if (pid != targetPid || !IsWindowVisible(hwnd)) return true;
            var className = new StringBuilder(256);
            GetClassName(hwnd, className, className.Capacity);
            if (className.ToString() == "#32770") windows.Add(hwnd);
            return true;
        }, IntPtr.Zero);
        return windows.ToArray();
    }
    public static IntPtr[] DialogControls(IntPtr dialog, int id, string wantedClass) {
        var controls = new List<IntPtr>();
        EnumChildWindows(dialog, (hwnd, unused) => {
            var className = new StringBuilder(256);
            GetClassName(hwnd, className, className.Capacity);
            if (GetDlgCtrlID(hwnd) == id && className.ToString() == wantedClass && IsWindowVisible(hwnd)) controls.Add(hwnd);
            return true;
        }, IntPtr.Zero);
        return controls.ToArray();
    }
}
'@
[void][DialuxDesktop]::SetProcessDPIAware()

function Elements($Parent) {
    $all = $null
    for ($readAttempt = 0; $readAttempt -lt 3; $readAttempt++) {
        try {
            $all = $Parent.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
            break
        } catch {
            if ($readAttempt -eq 2) { throw }
            # File dialogs rebuild their UIA tree while becoming ready. Only
            # retry this read, before any target action is invoked.
            Start-Sleep -Milliseconds 250
        }
    }
    # Native import dialogs can be top-level siblings instead of descendants
    # of the WPF main window. Restrict extra roots to this bound process.
    if ($Parent -eq $script:root) {
        $all = @($all)
        foreach ($handle in [DialuxDesktop]::DialogWindows($script:process.Id)) {
            $dialogRoot = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
            $all += $dialogRoot
            $all += @($dialogRoot.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition))
        }
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    @($all | ForEach-Object {
        $key = $_.GetRuntimeId() -join ':'
        if ($seen.Add($key)) { $_ }
    })
}

function Find-One($Selector, [int]$Attempts = 5) {
    if (!$Selector.id -and !$Selector.name -and !$Selector.class_name) { throw 'A specific selector is required' }
    $scope = $script:root
    if ($Selector.parent_id) {
        $parents = @(Elements $scope | Where-Object { $_.Current.AutomationId -eq $Selector.parent_id })
        if ($parents.Count -ne 1) { throw "Parent selector matched $($parents.Count) controls" }
        $scope = $parents[0]
    }
    $found = @()
    for ($selectorAttempt = 0; $selectorAttempt -lt $Attempts; $selectorAttempt++) {
        $found = @(Elements $scope | Where-Object {
            $c = $_.Current
            (!$Selector.id -or $c.AutomationId -eq $Selector.id) -and
            (!$Selector.name -or $c.Name -ceq $Selector.name) -and
            (!$Selector.type -or $c.ControlType.ProgrammaticName -eq ('ControlType.' + $Selector.type)) -and
            (!$Selector.class_name -or $c.ClassName -eq $Selector.class_name) -and
            !$c.IsOffscreen -and $c.IsEnabled
        })
        if ($found.Count -ne 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($found.Count -ne 1) { throw "Selector matched $($found.Count) controls: $($Selector | ConvertTo-Json -Compress)" }
    $found[0]
}

function Focus-Target {
    [uint32]$foregroundPid = 0
    [void][DialuxDesktop]::GetWindowThreadProcessId([DialuxDesktop]::GetForegroundWindow(), [ref]$foregroundPid)
    # Re-activating the main window while one of its popups owns focus closes
    # that popup and can invert a subsequent toggle. Preserve existing focus.
    if ($foregroundPid -eq $script:process.Id) { return }
    $pattern = $null
    if ($script:root.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$pattern)) {
        if ($pattern.Current.WindowVisualState -eq 'Minimized') { $pattern.SetWindowVisualState('Normal') }
    }
    [void][DialuxDesktop]::SetForegroundWindow($script:mainWindowHandle)
    Start-Sleep -Milliseconds 150
    $foregroundPid = 0
    [void][DialuxDesktop]::GetWindowThreadProcessId([DialuxDesktop]::GetForegroundWindow(), [ref]$foregroundPid)
    if ($foregroundPid -ne $script:process.Id) {
        $script:root.SetFocus()
        [void][DialuxDesktop]::GetWindowThreadProcessId([DialuxDesktop]::GetForegroundWindow(), [ref]$foregroundPid)
    }
    if ($foregroundPid -ne $script:process.Id) { throw 'DIALux could not acquire foreground focus' }
}

function Click-Control($Element) {
    $invoke = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
        $invoke.Invoke()
        return
    }
    Focus-Target
    $b = $Element.Current.BoundingRectangle
    if ($b.IsEmpty -or $b.Width -le 0 -or $b.Height -le 0) { throw 'Control has no clickable bounds' }
    [void][DialuxDesktop]::SetCursorPos([int]($b.X + $b.Width/2), [int]($b.Y + $b.Height/2))
    [DialuxDesktop]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
    [DialuxDesktop]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
}

function Snapshot {
    $dialogs = @([DialuxDesktop]::DialogWindows($script:process.Id) | ForEach-Object {
        $title = New-Object System.Text.StringBuilder 2048
        [void][DialuxDesktop]::GetWindowText($_, $title, $title.Capacity)
        @{title=$title.ToString();handle=$_.ToInt64().ToString();
          open_edits=@([DialuxDesktop]::DialogControls($_,1148,'Edit')).Count;
          save_edits=@([DialuxDesktop]::DialogControls($_,1001,'Edit')).Count}
    })
    $controls = @(Elements $script:root | ForEach-Object {
        $c = $_.Current
        if ($c.ControlType.ProgrammaticName -eq 'ControlType.Image') { return }
        $value = $null
        $pattern = $null
        if ($_.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) { $value = $pattern.Current.Value }
        if ($_.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$pattern)) { $value = $pattern.Current.ToggleState.ToString() }
        if ($_.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) { $value = $pattern.Current.IsSelected }
        $b = $c.BoundingRectangle
        $bounds = $null
        if (!$b.IsEmpty) { $bounds = @($b.X,$b.Y,$b.Width,$b.Height) }
        @{name=$c.Name; id=$c.AutomationId; type=$c.ControlType.ProgrammaticName.Replace('ControlType.',''); class_name=$c.ClassName;
          enabled=$c.IsEnabled; offscreen=$c.IsOffscreen; value=$value; bounds=$bounds}
    })
    @{pid=$script:process.Id; started_ticks=$script:process.StartTime.ToUniversalTime().Ticks.ToString();
      main_window_handle=$script:mainWindowHandle.ToInt64().ToString();
      session_id=$script:process.SessionId; executable=$script:process.Path;
      file_version=$script:process.MainModule.FileVersionInfo.FileVersion;
      title=$script:root.Current.Name; controls=$controls;native_dialogs=$dialogs}
}

try {
    $request = Get-Content -LiteralPath $RequestPath -Encoding UTF8 -Raw | ConvertFrom-Json
    $script:process = Get-Process -Id $request.pid
    if ($process.ProcessName -ne 'DIALux_x64') { throw 'Target process is not DIALux_x64' }
    if ($request.started_ticks -and $request.started_ticks -ne $process.StartTime.ToUniversalTime().Ticks.ToString()) { throw 'Target process has changed' }
    if ($process.SessionId -ne (Get-Process -Id $PID).SessionId) { throw 'DIALux belongs to another desktop session' }
    $mainWindows = @([DialuxDesktop]::MainWindows($process.Id))
    if ($mainWindows.Count -eq 0) { throw 'DIALux has no main window yet' }
    if ($mainWindows.Count -ne 1) { throw 'DIALux main window is ambiguous' }
    $script:mainWindowHandle = $mainWindows[0]
    if ($request.main_window_handle -and $request.main_window_handle -ne $mainWindowHandle.ToInt64().ToString()) { throw 'DIALux main window identity changed' }
    $desktop = [DialuxDesktop]::OpenInputDesktop(0, $false, 1)
    if ($desktop -eq [IntPtr]::Zero) { throw 'Interactive desktop is unavailable' }
    [void][DialuxDesktop]::CloseDesktop($desktop)
    try { $script:root = [System.Windows.Automation.AutomationElement]::FromHandle($mainWindowHandle) }
    catch { throw 'DIALux has no main window yet' }
    if ($request.expected_project -and $request.action -notin @('snapshot','capture')) {
        $expectedTitle = [regex]::Escape($request.expected_project) + '\*? - DIALux'
        if ($root.Current.Name -notmatch ('^' + $expectedTitle)) { throw 'A different project is now open; refusing desktop mutation' }
    }
    $result = $null
    $actionStarted = $true
    switch ($request.action) {
        'snapshot' { $result = Snapshot }
        'file_dialog' {
            $dialogs = @([DialuxDesktop]::DialogWindows($process.Id))
            if ($dialogs.Count -ne 1) { throw 'Expected one native file dialog in the dedicated process' }
            $dialog = $dialogs[0]
            $title = New-Object System.Text.StringBuilder 2048
            [void][DialuxDesktop]::GetWindowText($dialog, $title, $title.Capacity)
            if ($title.ToString() -cne $request.title) { throw "Unexpected file dialog: $title" }
            $edits = @([DialuxDesktop]::DialogControls($dialog, [int]$request.edit_id, 'Edit'))
            $buttons = @([DialuxDesktop]::DialogControls($dialog, 1, 'Button'))
            if ($edits.Count -ne 1 -or $buttons.Count -ne 1) { throw 'Native file controls are ambiguous or unavailable' }
            [void][DialuxDesktop]::SendMessage($edits[0], 12, [IntPtr]::Zero, [string]$request.path)
            $entered = New-Object System.Text.StringBuilder 4096
            [void][DialuxDesktop]::ReadText($edits[0], 13, [IntPtr]$entered.Capacity, $entered)
            if ($entered.ToString() -cne $request.path) { throw 'File path write was not confirmed' }
            if (![DialuxDesktop]::PostMessage($buttons[0], 245, [IntPtr]::Zero, [IntPtr]::Zero)) { throw 'File dialog submit was rejected' }
            $result = @{title=$title.ToString(); path=$entered.ToString(); submitted=$true}
        }
        'close' {
            if ($root.Current.Name -match '\* - DIALux') { throw 'Refusing to close a project with unsaved changes' }
            ([System.Windows.Automation.WindowPattern]$root.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)).Close()
        }
        'capture' {
            Focus-Target
            $b = $root.Current.BoundingRectangle
            $bitmap = New-Object System.Drawing.Bitmap([int]$b.Width, [int]$b.Height)
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.CopyFromScreen([int]$b.X, [int]$b.Y, 0, 0, $bitmap.Size)
                $bitmap.Save($request.path, [System.Drawing.Imaging.ImageFormat]::Png)
            } finally { $graphics.Dispose(); $bitmap.Dispose() }
            $result = @{path=$request.path}
        }
        'menu' {
            Focus-Target
            $menuDepth = 0
            foreach ($menuId in $request.path.Split('/')) {
                # WPF creates nested menu items asynchronously after expansion.
                # Longer read-only polling never replays the parent command.
                $element = Find-One @{id=$menuId; type='MenuItem'} $(if ($menuDepth) { 20 } else { 5 })
                $pattern = $null
                if ($element.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern) -and $pattern.Current.ExpandCollapseState -ne 'LeafNode') { $pattern.Expand() }
                else { ([System.Windows.Automation.InvokePattern]$element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)).Invoke() }
                Start-Sleep -Milliseconds 150
                $menuDepth++
            }
        }
        'click' { Click-Control (Find-One $request.selector) }
        'start_and_observe' {
            $activityCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $request.activity_id)
            $deadline = [DateTime]::UtcNow.AddSeconds(10)
            Click-Control (Find-One $request.selector)
            $observed = $false
            while ([DateTime]::UtcNow -lt $deadline) {
                $active = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $activityCondition) | Where-Object { $_.Current.IsEnabled -and !$_.Current.IsOffscreen })
                if ($active.Count -gt 0) { $observed = $true; break }
                Start-Sleep -Milliseconds 30
            }
            if (!$observed) { throw 'Start command sent once but no running state was observed' }
            $result = @{started=$true; observed_at=[DateTime]::UtcNow.ToString('o'); activity_id=$request.activity_id; enabled=$true}
        }
        'invoke' { ([System.Windows.Automation.InvokePattern](Find-One $request.selector).GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)).Invoke() }
        'select' { ([System.Windows.Automation.SelectionItemPattern](Find-One $request.selector).GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)).Select() }
        'room_context' {
            # Resolve the room by its visible label inside the context menu,
            # never by a cached index that can change with the room order.
            $button = Find-One @{id='ToggleButton_RoomContextSelectionButton'}
            $toggle = [System.Windows.Automation.TogglePattern]$button.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            if ($toggle.Current.ToggleState.ToString() -ne 'On') { Click-Control $button }
            Click-Control (Find-One @{id='_menuItemControl';type='MenuItem'})
            $matches = @(Elements $script:root | Where-Object {
                $_.Current.AutomationId -like 'MenuItem_ToggleButton_RoomContextSelectionButton_*' -and
                @((Elements $_) | Where-Object { $_.Current.Name -ceq $request.room }).Count -gt 0
            })
            if ($matches.Count -ne 1) { throw 'Requested room is absent or ambiguous in the context menu' }
            $roomMenuId = $matches[0].Current.AutomationId
            Click-Control $matches[0]
            $result = @{room=$request.room;menu_id=$roomMenuId}
        }
        'expand' { ([System.Windows.Automation.ExpandCollapsePattern](Find-One $request.selector).GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)).Expand() }
        'combo_select' {
            $combo = Find-One @{id=$request.id;type='ComboBox'}
            $expand = [System.Windows.Automation.ExpandCollapsePattern]$combo.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
            $expand.Expand()
            $item = Find-One @{parent_id=$request.id;id=$request.item_id;type='ListItem'}
            ([System.Windows.Automation.SelectionItemPattern]$item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)).Select()
            $selection = [System.Windows.Automation.SelectionPattern]$combo.GetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern)
            $selected = @($selection.Current.GetSelection())
            if ($selected.Count -ne 1 -or $selected[0].Current.AutomationId -cne $request.item_id) { throw 'Combo selection was not confirmed' }
            $result = @{id=$request.id;selected_id=$selected[0].Current.AutomationId;selected_name=$selected[0].Current.Name}
            $expand.Collapse()
        }
        'combo_value' {
            $combo = Find-One @{id=$request.id;type='ComboBox'}
            $selection = [System.Windows.Automation.SelectionPattern]$combo.GetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern)
            $selected = @($selection.Current.GetSelection())
            if ($selected.Count -ne 1) { throw 'Combo selection is absent or ambiguous' }
            $result = @{id=$request.id;selected_id=$selected[0].Current.AutomationId;selected_name=$selected[0].Current.Name}
        }
        'set' {
            $element = Find-One $request.selector
            ([System.Windows.Automation.ValuePattern]$element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).SetValue([string]$request.value)
        }
        'native_set' {
            $element = Find-One $request.selector
            if ($element.Current.ClassName -ne 'Edit' -or !$element.Current.NativeWindowHandle) { throw 'Native write requires a Win32 Edit control' }
            [void][DialuxDesktop]::SendMessage([IntPtr]$element.Current.NativeWindowHandle, 12, [IntPtr]::Zero, [string]$request.value)
        }
        'native_click' {
            $element = Find-One $request.selector
            if ($element.Current.ClassName -ne 'Button' -or !$element.Current.NativeWindowHandle) { throw 'Native click requires a Win32 Button control' }
            if (![DialuxDesktop]::PostMessage([IntPtr]$element.Current.NativeWindowHandle, 245, [IntPtr]::Zero, [IntPtr]::Zero)) { throw 'Native button message was rejected' }
        }
        'toggle' {
            $element = Find-One $request.selector
            $pattern = [System.Windows.Automation.TogglePattern]$element.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            if ($pattern.Current.ToggleState.ToString() -ne $request.value) {
                if ($request.selector.id -eq 'ShowResultsMonitor') { Click-Control $element }
                else { $pattern.Toggle() }
            }
        }
        'configure_calculation' {
            # Keep the popup and its controls in one UIA connection. Windows
            # may dismiss a WPF popup between separate helper processes.
            $button = Find-One @{id='CalculationButtonShowCalcOptionsDropDown'}
            $toggle = [System.Windows.Automation.TogglePattern]$button.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            if ($toggle.Current.ToggleState.ToString() -ne 'On') { $toggle.Toggle() }
            $switch = Find-One @{id='PopupStandardCalcOptionsSwitchButton'}
            if ($switch.Current.Name -ne $request.mode_label) { throw 'Expected full-scene calculation mode' }
            foreach ($controlId in $request.option_ids) {
                $element = Find-One @{id=$controlId;type='CheckBox'}
                $option = [System.Windows.Automation.TogglePattern]$element.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
                if ($option.Current.ToggleState.ToString() -ne 'Off') { $option.Toggle() }
                if ($option.Current.ToggleState.ToString() -ne 'Off') { throw "Calculation option was not disabled: $controlId" }
            }
            $result = Snapshot
            $toggle.Toggle()
            if ($toggle.Current.ToggleState.ToString() -ne 'Off') { throw 'Calculation popup did not close' }
        }
        default { throw 'Unsupported desktop action' }
    }
    @{ok=$true; result=$result} | ConvertTo-Json -Depth 12 -Compress
} catch {
    $code = 'desktop_failure'
    if ($actionStarted -and $request.action -eq 'snapshot') { $code = 'snapshot_unavailable' }
    @{ok=$false; error=$_.Exception.Message; code=$code; location=$_.ScriptStackTrace} | ConvertTo-Json -Depth 4 -Compress
    exit 1
}
