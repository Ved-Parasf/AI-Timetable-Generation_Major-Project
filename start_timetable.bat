@echo off
cd /d "%~dp0"
py -m streamlit run timetable_app.py
if errorlevel 1 pause
