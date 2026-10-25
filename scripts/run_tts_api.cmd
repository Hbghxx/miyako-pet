@echo off
rem TTS service runner - launched by the launcher bat (env TTS_PY / TTS_DIR /
rem TTS_LOG are inherited). Console output lands in TTS_LOG so a silent
rem startup crash is diagnosable (minimized windows have no visible console).
"%TTS_PY%" "%TTS_DIR%\api_v2.py" -a 127.0.0.1 -p 9880 -c "%TTS_DIR%\_setup\tts_infer_miyako.yaml" > "%TTS_LOG%" 2>&1
exit /b 0
