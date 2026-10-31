@echo off
cd /d E:\Person\LynkCoHelper\LynkCoHelper
set PYTHONIOENCODING=utf-8
echo ======== %date% %time% ======== >> E:\Person\LynkCoHelper\runs.log
python lynkco_daily_tasks.py >> E:\Person\LynkCoHelper\runs.log 2>&1
