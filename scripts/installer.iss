; VoxSub installer script (Inno Setup 6+)
; 语幕 VoxSub - 大众实时翻译安装包
; Build: 用 InnoSetup (iscc) 编译本脚本 -> VoxSub-Setup-0.9.0-beta.exe
;   iscc scripts\installer.iss
; 说明: 大型模型不打包进安装包, 首次运行经模型广场下载(断点续传+SHA256)。
;       2.3MB 的基础 VAD 随程序分发，首次启动会自动修复到
;       用户模型默认保存到安装目录的 Models\vad，确保新设备下载 ASR 后能直接运行。
;       自签版发布物附 SHA256 + 解除 SmartScreen 指引。正式 OV 证书后签名由
;       build.ps1 的 osslsigncode 统一处理(SignedSetup)。

#define MyAppName "VoxSub"
#define MyAppVersion "0.9.0-beta"
#define MyAppPublisher "VoxSub"
#define MyAppExeName "VoxSub.exe"
#define MyAppId "{{7B5F6A3C-2E8D-4B1A-9C7E-VOXSUB0000001}"
#define MyAppRunningMutex "Local\VoxSub.Application.7B5F6A3C-2E8D-4B1A-9C7E-VOXSUB0000001"
#define MyAppShutdownEvent "Local\VoxSub.InstallerShutdown.7B5F6A3C-2E8D-4B1A-9C7E-VOXSUB0000001"

[Setup]
AppId={#MyAppId}
AppName={cm:MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoDescription=VoxSub Setup
VersionInfoProductName=VoxSub
DefaultDirName={autopf}\VoxSub
DefaultGroupName={cm:MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
; 正式版发布物统一输出到 D:\OneDrive\app_dve\Release (用户约定 2026-08-17)
OutputDir=..\..\Release
OutputBaseFilename=VoxSub-Setup-0.9.0-beta
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; VoxSub closes its main window to the tray. Restart Manager treats that as a
; refusal and blocks for roughly 30 seconds, so use the explicit bounded
; shutdown handshake in [Code] instead.
CloseApplications=no
RestartApplications=no
; 按 Windows UI 语言自动选择；无对应翻译时回退到英文，不显示语言选择弹窗。
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=no
ArchitecturesInstallIn64BitMode=x64compatible
; 图标(若有: SetupIconFile=..\assets\icon.ico)
; 签名(正式 OV 证书后启用):
; SignTool=signtool /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f
; SignedUninstaller=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "chinesetraditional"; MessagesFile: "compiler:Languages\ChineseTraditional.isl"

[CustomMessages]
english.MyAppName=VoxSub
chinesesimplified.MyAppName=语幕 VoxSub
chinesetraditional.MyAppName=語幕 VoxSub
english.CreateDesktopShortcut=Create a desktop shortcut
chinesesimplified.CreateDesktopShortcut=创建桌面快捷方式
chinesetraditional.CreateDesktopShortcut=建立桌面捷徑
english.AdditionalIcons=Additional icons:
chinesesimplified.AdditionalIcons=附加图标：
chinesetraditional.AdditionalIcons=附加圖示：
english.LaunchApp=Launch %1
chinesesimplified.LaunchApp=立即启动 %1
chinesetraditional.LaunchApp=立即啟動 %1
english.ClosingApp=Closing VoxSub before updating...
chinesesimplified.ClosingApp=正在关闭语幕 VoxSub 以完成更新…
chinesetraditional.ClosingApp=正在關閉語幕 VoxSub 以完成更新…
english.AppCloseFailed=Setup could not close VoxSub. End VoxSub.exe in Task Manager, then try again.
chinesesimplified.AppCloseFailed=安装程序无法关闭语幕 VoxSub。请在任务管理器中结束 VoxSub.exe 后重试。
chinesetraditional.AppCloseFailed=安裝程式無法關閉語幕 VoxSub。請在工作管理員中結束 VoxSub.exe 後重試。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopShortcut}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; VoxSub 主程序 + 全部运行时 (PyInstaller onedir 输出)
Source: "..\dist\VoxSub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; PyInstaller's onedir layout changes as dependencies evolve. Overwriting an
; existing installation leaves removed packages behind; a partial legacy
; brotlicffi directory has already been observed shadowing urllib3 and breaking
; RapidOCR. Remove only application-managed runtime directories after VoxSub
; has been closed by PrepareToInstall. Models and Cache are user data and must
; never be included here.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\models_base"

[Dirs]
; 模型是用户数据，不随升级/卸载删除。只让普通用户写 Models，程序文件仍受保护。
Name: "{app}\Models"; Permissions: users-modify
; OCR cache is enabled only when {app} is not on C:. Creating writable
; directories here lets non-system-drive installations cache without elevation.
Name: "{app}\Cache\OCR\originals"; Permissions: users-modify
Name: "{app}\Cache\OCR\translated"; Permissions: users-modify

[Icons]
Name: "{group}\{cm:MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{cm:MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchApp,{cm:MyAppName}}"; Flags: nowait postinstall skipifsilent

[Registry]
; 可选: 注册文件关联(.wav/.mp4 -> 语幕打开) -- 二期再做, 此略

[Code]
(*
  安装后检测模型缺失, 弹提示引导下载 (ModelScope 主源)。
  注意: 此脚本本身不下载模型(避免安装器巨大), 由 app 首次运行诊断页引导。
*)
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

const
  EVENT_MODIFY_STATE = $0002;
  SYNCHRONIZE = $00100000;
  { OCR and translation workers can need a little over two seconds to unwind.
    Keep the wait bounded, but do not race their normal cleanup. }
  GracefulCloseTimeoutMs = 5000;
  ForcedCloseVerifyTimeoutMs = 2000;

function OpenEvent(dwDesiredAccess: LongWord; bInheritHandle: Boolean;
  lpName: string): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(hEvent: THandle): Boolean;
  external 'SetEvent@kernel32.dll stdcall';
function OpenMutex(dwDesiredAccess: LongWord; bInheritHandle: Boolean;
  lpName: string): THandle;
  external 'OpenMutexW@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function GetTickCount(): LongWord;
  external 'GetTickCount@kernel32.dll stdcall';

function SignalRunningVoxSub(): Boolean;
var
  EventHandle: THandle;
begin
  Result := False;
  EventHandle := OpenEvent(EVENT_MODIFY_STATE, False, '{#MyAppShutdownEvent}');
  if EventHandle <> 0 then
  begin
    Result := SetEvent(EventHandle);
    CloseHandle(EventHandle);
  end;
end;

function NewVoxSubIsRunning(): Boolean;
var
  MutexHandle: THandle;
begin
  MutexHandle := OpenMutex(SYNCHRONIZE, False, '{#MyAppRunningMutex}');
  Result := MutexHandle <> 0;
  if Result then
    CloseHandle(MutexHandle);
end;

function WaitForNewVoxSubToExit(TimeoutMs: Cardinal): Boolean;
var
  StartedAt: Cardinal;
begin
  StartedAt := GetTickCount;
  while NewVoxSubIsRunning() and
    (GetTickCount - StartedAt < TimeoutMs) do
    Sleep(100);
  Result := not NewVoxSubIsRunning();
end;

function ForceCloseVoxSub(): Boolean;
var
  ResultCode: Integer;
begin
  { /T also closes a bundled llama-server child. Exit 128 means no match. }
  ResultCode := -1;
  Result := Exec(ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM "{#MyAppExeName}"', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  Log(Format('VoxSub taskkill completed: started=%d exit=%d', [Ord(Result), ResultCode]));
  Result := Result and ((ResultCode = 0) or (ResultCode = 128));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Signalled: Boolean;
begin
  Result := '';
  WizardForm.StatusLabel.Caption := ExpandConstant('{cm:ClosingApp}');
  WizardForm.StatusLabel.Update;

  Signalled := SignalRunningVoxSub();
  if Signalled then
  begin
    Log('VoxSub shutdown event was signalled; waiting for cleanup.');
    if not WaitForNewVoxSubToExit(GracefulCloseTimeoutMs) then
    begin
      { The application may disappear between this check and taskkill.  Do not
        treat taskkill's resulting "not found"/race response as a failure;
        verify the named running mutex after the attempt instead. }
      ForceCloseVoxSub();
      if not WaitForNewVoxSubToExit(ForcedCloseVerifyTimeoutMs) then
        Result := ExpandConstant('{cm:AppCloseFailed}');
    end;
  end;

  { 0.7.1 and older do not expose the event; an unresponsive newer process
    can also outlive the short grace period. Handle both without Restart
    Manager's 30-second wait. }
  if (Result = '') and (not Signalled) then
    if not ForceCloseVoxSub() then
      Result := ExpandConstant('{cm:AppCloseFailed}');
end;
