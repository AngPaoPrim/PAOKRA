!macro customInstall
  ; Recreate shortcuts from the actual NSIS installation directory.
  ; Bypass the generated shortcut path handling for the assisted installer.
  SetShellVarContext current
  Delete "$SMPROGRAMS\PAOKRA Studio.lnk"
  Delete "$SMPROGRAMS\PAOKRA Studio\PAOKRA Studio.lnk"
  Delete "$DESKTOP\PAOKRA Studio.lnk"
  CreateDirectory "$SMPROGRAMS\PAOKRA Studio"
  CreateShortCut "$SMPROGRAMS\PAOKRA Studio\PAOKRA Studio.lnk" "$INSTDIR\PAOKRA Studio.exe" "" "$INSTDIR\PAOKRA Studio.exe" 0 SW_SHOWNORMAL
  CreateShortCut "$DESKTOP\PAOKRA Studio.lnk" "$INSTDIR\PAOKRA Studio.exe" "" "$INSTDIR\PAOKRA Studio.exe" 0 SW_SHOWNORMAL
!macroend
