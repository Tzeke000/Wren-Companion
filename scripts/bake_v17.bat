@echo off
REM v17 bake (2026-08-05, Zeke's go ~13:19) — the BE-LIKE-HER round.
REM See scripts/little_brain_corpus_v17.py docstring + memory note
REM v16_behavioral_study_2026-08-05. Dataset rebuilt 2026-08-05 (4370
REM samples: +v17 people-recall/social-twins/affect x8, quiet/chat x6,
REM multiturn/antiloop x4). Warmstart from adapter_v12_bak — the PROVEN
REM pattern (v15 and v16 both re-learned all prior gains from the full
REM dataset off this stable base; one-bake window, no experiments).
REM Same tight-VRAM config as v13-v16 (12GB card; run RUNTIME-DOWN via
REM scripts/v17_guardian.py — never with perception loaded).
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
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v17.log" 2>&1
