@echo off
REM Local BACKUP trigger for the afternoon voicemail texts (primary is the Cloudflare dispatcher at 4:30 PM Central).
REM Registered in Windows Task Scheduler as "SiftStack PM Texts" (Mon-Fri 4:00 PM Mountain = 5:00 PM Central, window 17:00-17:45 so it never overlaps the primary).
REM Primary: dispatcher/ Cloudflare Worker -> GitHub workflow_dispatch, window 16:15-17:00.
cd /d C:\Users\kayci\Desktop\SiftStack
echo ==== %DATE% %TIME% ==== >> logs\pm_texts_local.log
"C:\Users\kayci\AppData\Local\Python\pythoncore-3.14-64\python.exe" src\pm_text_sender.py pm-texts --send --slack --only-between 17:00-17:45 >> logs\pm_texts_local.log 2>&1