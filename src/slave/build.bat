@echo off
cd /d %~dp0
python -m PyInstaller --onefile --noconsole --name game_share --distpath dist --workpath build --specpath build --paths . game_share.py
