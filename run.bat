@echo off
where pyw >nul 2>nul && (start "" pyw "%~dp0pydm.py" & exit /b)
start "" pythonw "%~dp0pydm.py"
