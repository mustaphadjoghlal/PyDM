@echo off
rem تثبيت PyDM بصمت للمستخدم الحالي (بلا صلاحيات مدير). ضع هذا الملف بجانب PyDM-Setup.exe
"%~dp0PyDM-Setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /LOG="%TEMP%\PyDM-install.log"
