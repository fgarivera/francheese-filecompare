@echo off
REM Double-click this file to launch FolderCompare.
REM It only ever READS the folders you compare - it never modifies your files.
cd /d "%~dp0"
python folder_compare.py
if errorlevel 1 pause
