Unicode true
Name "PAOKRA Studio"
Caption "PAOKRA Studio Setup"
OutFile "G:\paokra\dist\PAOKRA-Studio-Setup-1.1.2.exe"
InstallDir "$LOCALAPPDATA\Programs\PAOKRA Studio"
InstallDirRegKey HKCU "Software\PAOKRA Studio" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
CRCCheck on

!include "MUI2.nsh"
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

VIProductVersion "1.1.2.0"
VIAddVersionKey "ProductName" "PAOKRA Studio"
VIAddVersionKey "CompanyName" "PAOKRA"
VIAddVersionKey "FileDescription" "PAOKRA Studio installer"
VIAddVersionKey "FileVersion" "1.1.2"
VIAddVersionKey "ProductVersion" "1.1.2"

!define MUI_FINISHPAGE_RUN "$INSTDIR\PAOKRA Studio.exe"
!define MUI_FINISHPAGE_RUN_TEXT "เปิด PAOKRA Studio"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

Section "PAOKRA Studio" SEC_MAIN
  SetOutPath "$INSTDIR"
  File /r "G:\paokra\dist\win-unpacked\*.*"
  CreateDirectory "$LOCALAPPDATA\PAOKRA Studio"
  CreateDirectory "$LOCALAPPDATA\PAOKRA Studio\data"
  CreateShortCut "$DESKTOP\PAOKRA Studio.lnk" "$INSTDIR\PAOKRA Studio.exe"
  CreateDirectory "$SMPROGRAMS\PAOKRA Studio"
  CreateShortCut "$SMPROGRAMS\PAOKRA Studio\PAOKRA Studio.lnk" "$INSTDIR\PAOKRA Studio.exe"
  WriteRegStr HKCU "Software\PAOKRA Studio" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PAOKRA Studio" "DisplayName" "PAOKRA Studio"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PAOKRA Studio" "DisplayVersion" "1.1.2"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PAOKRA Studio" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PAOKRA Studio" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\PAOKRA Studio.lnk"
  Delete "$SMPROGRAMS\PAOKRA Studio\PAOKRA Studio.lnk"
  RMDir "$SMPROGRAMS\PAOKRA Studio"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\PAOKRA Studio"
  DeleteRegKey HKCU "Software\PAOKRA Studio"
  RMDir /r "$INSTDIR"
  ; User data and Media under %LOCALAPPDATA%\PAOKRA Studio are intentionally preserved.
SectionEnd

