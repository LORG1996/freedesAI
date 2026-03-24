@echo off
echo --------------------------------------------------
echo Checking libraries and starting Freedes AI search...
echo --------------------------------------------------

:: Встановлення бібліотек з requirements.txt
pip install -r requirements.txt

:: Запуск програми
python -m streamlit run app_arch.py

pause
