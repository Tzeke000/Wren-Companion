@echo off
REM v14 bake (2026-07-25, Zeke greenlit LIVE-SENSOR-ROUTING round). Warm from v12
REM (adapter_v12_bak) — NOT v13, deliberately: v13 regressed in the tool loop
REM (looped on memory_search for live reads, one repeat-token degeneration), so we
REM warmstart from stable v12 and relearn v13's gains (still in v13_dlg x12) PLUS
REM the v14 fixes. Adds (little_brain_corpus_v14.py, v14_dlg x16): live-number ->
REM senses_now (never memory_search); the facts/rules->memory vs live->senses
REM CONTRAST; reach->fail->honest-refuse; use-the-result / anti-loop. Rebuild the
REM dataset first (little_brain_dataset.py now includes v14) — already done at prep.
REM Same tight-VRAM config as v13 (this 12GB card; run RUNTIME-DOWN via guardian).
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v12_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_VRAM_FLOOR=6.0
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v14.log" 2>&1
