; AI Quant Agent 安装包
;
; 编译：iscc.exe installer\aqa.iss
;   或  tools\build_installer.bat  （打包 + 编译一条龙）
; 前置：dist\AIQuant\ 已由 aqa.spec 生成
;
; 三个不能改的设计：
;   1. PrivilegesRequired=lowest + 装到 {localappdata}
;      程序要往自己旁边写 data/ 和 config/。装到 Program Files 会在启动时
;      报「目录不可写」——那等于把一个必然的启动失败装进用户机器。
;   2. Excludes 掉 data\ 和 config\
;      打包机的 data\live 里躺着开发者的真实账本，漏掉这句就会把
;      他的仓位历史发给每一个用户。
;   3. 卸载保留 data\ 和 config\
;      账本是唯一副本（MT5 历史有保留期、换服务器就丢），删了不可恢复。

#define MyAppName "AI Quant Agent"
; 版本号不再写死在这里：build_installer.bat 从仓库根 app_version.py 读出
; APP_VERSION 后用 /DMyAppVersion= 传入。0.0.0 是"忘了传参"的醒目信号，
; 故意不用任何真实版本——宁可看着离谱，也别静默发出版本号重复的包。
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppExe "AIQuant.exe"
#define MyAppPublisher "AI Quant Agent"
; 固定 GUID：升级和卸载靠它识别同一个产品，**永远不要改**
#define MyAppId "{{7C3F1A62-9D24-4F0B-9A61-2E5B7C1D8A44}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\AIQuant
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=AIQuant-Setup-{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
; 与 aqa_launcher.py 里 CreateMutexW 的名字一致：程序在跑时安装/卸载能提示先关
AppMutex=Global\AIQuantDashboard

[Languages]
; 中文界面需要社区翻译文件 ChineseSimplified.isl（Inno 官方发行版不带）。
; 把它放进 installer\ 后取消下面这行的注释即可。
; Name: "chinesesimplified"; MessagesFile: "chinesesimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\AIQuant\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "data\*,config\*,*.log"

[Dirs]
; uninsneveruninstall：卸载时这两个目录即使空了也留着
Name: "{app}\data";   Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\config"; Permissions: users-modify; Flags: uninsneveruninstall

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
; 不带 skipifsilent：应用内一键升级走 /SILENT，装完必须由 Setup 拉起新版，
; 否则用户面对"程序自己关了却没开"。首次手动安装时 postinstall 只是
; 不显示勾选框、直接启动，行为无差。
Filename: "{app}\{#MyAppExe}"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall

[UninstallDelete]
; 只清 WebView2 的缓存（可重建）。data\live 和 config\ 一个字都不碰。
Type: filesandordirs; Name: "{app}\data\webview"

[Code]
const
  WEBVIEW2_KEY = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function WebView2Installed(): Boolean;
var
  pv: String;
begin
  Result :=
       RegQueryStringValue(HKCU,   WEBVIEW2_KEY, 'pv', pv)
    or RegQueryStringValue(HKLM,   WEBVIEW2_KEY, 'pv', pv)
    or RegQueryStringValue(HKLM32, WEBVIEW2_KEY, 'pv', pv)
    or RegQueryStringValue(HKLM64, WEBVIEW2_KEY, 'pv', pv);
  { 装了但运行时丢了（pv=0.0.0.0），等同未安装 }
  if Result and (pv = '0.0.0.0') then
    Result := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrCode: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  if WebView2Installed() then Exit;
  { 不内置运行时（体积 +100MB，还要管理员权限），只提示 + 给官方下载页 }
  if MsgBox('Microsoft Edge WebView2 Runtime is missing.' + #13#10 + #13#10 +
            'The app window needs it. It is a free Microsoft component,' + #13#10 +
            '(already present on most Windows 10/11 machines).' + #13#10 + #13#10 +
            'Open the official download page now?' + #13#10 +
            '(You can also run AIQuant.exe --web to use a browser instead.)',
            mbConfirmation, MB_YESNO) = IDYES then
    ShellExec('open', WEBVIEW2_URL, '', '', SW_SHOWNORMAL, ewNoWait, ErrCode);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and
     (Pos('program files', Lowercase(WizardDirValue)) > 0) then
  begin
    if MsgBox('This folder is usually read-only, and the program must write' + #13#10 +
              'its ledger next to itself. It will fail to start there.' + #13#10 + #13#10 +
              'Recommended: keep the default (user AppData\Programs\AIQuant).' + #13#10 +
              'Continue anyway?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if DirExists(ExpandConstant('{app}\data\live')) then
    if MsgBox('Uninstall keeps data\ and config\ (ledger, trades, lessons,' + #13#10 +
              'model settings). They are the only copy - MT5 history expires.' + #13#10 +
              'Delete the install folder by hand if you really want them gone.' + #13#10 + #13#10 +
              'Continue?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
end;
