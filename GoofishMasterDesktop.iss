; GoofishMasterDesktop Inno Setup Installer Script
; 生成安装包: ISCC.exe GoofishMasterDesktop.iss

#define MyAppName "GoofishMasterDesktop"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "GoofishMaster"
#define MyAppExeName "GoofishMasterDesktop.exe"
#define MyAppDescription "闲鱼圣手桌面独立运行端"

[Setup]
AppId={{B7F3E2A1-4D5C-6E8F-9A0B-1C2D3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoDescription={#MyAppDescription}
DefaultDirName=D:\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=GoofishMasterDesktop-Setup-{#MyAppVersion}
SetupIconFile=app.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DirExistsWarning=no
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"; Flags: checkedonce

[Files]
; 主程序
Source: "release\GoofishMasterDesktop\GoofishMasterDesktop.exe"; DestDir: "{app}"; Flags: ignoreversion
; _internal 全部依赖（数据文件方式打包，可单文件替换）
Source: "release\GoofishMasterDesktop\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; config.example.json 供参考
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; 卸载前强制停止进程
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F /T"; Flags: runhidden; RunOnceId: "StopApp"

[Code]
var
  PortPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  { 端口配置页面 }
  PortPage := CreateInputQueryPage(wpSelectDir,
    '端口配置',
    '设置各服务监听端口（均绑定 127.0.0.1，仅本机可访问）',
    '如果不确定，请保持默认值。安装后可在 config.json 中修改。');
  PortPage.Add('飞书智能体端口:', False);
  PortPage.Add('AI 路由端口:', False);
  PortPage.Add('分析编排端口:', False);
  PortPage.Add('采集服务端口:', False);
  PortPage.Values[0] := '8911';
  PortPage.Values[1] := '8912';
  PortPage.Values[2] := '8913';
  PortPage.Values[3] := '8914';
end;

function ValidatePort(Value: String; DefaultVal: Integer): Integer;
var
  N: Integer;
begin
  N := StrToIntDef(Trim(Value), -1);
  if (N < 1024) or (N > 65535) then
    Result := DefaultVal
  else
    Result := N;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigPath: String;
  ConfigDir: String;
  Content: String;
  P1, P2, P3, P4: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    ConfigDir := ExpandConstant('{app}\config');
    ConfigPath := ConfigDir + '\config.json';

    { 确保 config 目录存在 }
    ForceDirectories(ConfigDir);

    { 解析端口（非法值回退默认） }
    P1 := ValidatePort(PortPage.Values[0], 8911);
    P2 := ValidatePort(PortPage.Values[1], 8912);
    P3 := ValidatePort(PortPage.Values[2], 8913);
    P4 := ValidatePort(PortPage.Values[3], 8914);

    { 生成 config.json（secret_key 留空，首次启动时程序自动生成随机值） }
    Content :=
      '{' + #13#10 +
      '  "secret_key": "",' + #13#10 +
      '  "ports": {' + #13#10 +
      '    "feishu_agent": ' + IntToStr(P1) + ',' + #13#10 +
      '    "ai_router": ' + IntToStr(P2) + ',' + #13#10 +
      '    "agent_pipeline": ' + IntToStr(P3) + ',' + #13#10 +
      '    "spider": ' + IntToStr(P4) + #13#10 +
      '  },' + #13#10 +
      '  "backends": {' + #13#10 +
      '    "postgres": { "enabled": true, "port": 5439, "user": "goofish", "password": "goofish_v2_secret", "db": "goofish_ai" },' + #13#10 +
      '    "redis": { "enabled": true, "port": 6399 },' + #13#10 +
      '    "qdrant": { "enabled": true, "port": 6339 }' + #13#10 +
      '  },' + #13#10 +
      '  "feishu": {' + #13#10 +
      '    "app_id": "",' + #13#10 +
      '    "app_secret": ""' + #13#10 +
      '  },' + #13#10 +
      '  "ai": {' + #13#10 +
      '    "deepseek_api_key": "",' + #13#10 +
      '    "gemini_api_key": "",' + #13#10 +
      '    "qwen_api_key": "",' + #13#10 +
      '    "proxy_url": ""' + #13#10 +
      '  },' + #13#10 +
      '  "data_dir": "data"' + #13#10 +
      '}';

    SaveStringToFile(ConfigPath, Content, False);
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { 不跳过端口配置页 }
  Result := False;
end;
