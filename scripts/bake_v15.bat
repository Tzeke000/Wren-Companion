@echo off
REM v15 bake (2026-08-01, Zeke: "do what you can with the v15 brain"). The
REM ANTI-INVENTION round — see scripts/little_brain_corpus_v15.py docstring and
REM memory note v15_plan_2026-07-28. Warmstart from adapter_v12_bak (stable
REM production base), NOT v14 — its refusal habit is baked in. Dataset rebuilt
REM 2026-08-01 (3938 samples, incl. v15_dlg 39x12 in the REAL senses_now
REM dialect). Same tight-VRAM config as v13/v14 (12GB card; run RUNTIME-DOWN
REM via scripts/v15_guardian.py — never with perception loaded).
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
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v15.log" 2>&1
