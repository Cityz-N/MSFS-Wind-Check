@echo off
rem Builds dist\WindsAloft.exe. Needs: python -m pip install pyinstaller
cd /d "%~dp0"
python -m PyInstaller --onefile --noconsole --clean --noconfirm --name WindsAloft --add-data "%~dp0index.html;." --add-data "%~dp0LICENSE;." --add-data "%~dp0THIRD_PARTY_NOTICES.md;." --distpath dist --workpath build --specpath build server.py
