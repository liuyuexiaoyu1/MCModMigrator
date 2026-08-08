@echo off
REM ============================================
REM  打包为单文件 exe（GUI 无控制台黑窗）
REM  产物: dist\MCModMigrator.exe
REM  命令行模式在 exe 中会自动申请控制台，--cli 仍可用
REM ============================================
cd /d "%~dp0"

echo [1/2] 安装打包依赖...
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller || goto :err

echo [2/2] 打包...
.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --clean --noconfirm --name MCModMigrator mod_migrator.py || goto :err

echo.
echo 打包完成: %~dp0dist\MCModMigrator.exe
pause
exit /b 0

:err
echo.
echo 打包失败，请检查上方错误信息。
pause
exit /b 1
