@echo off
REM v16 bake (2026-08-04, Zeke's go ~19:50) — the SUPERVISED FALLING round.
REM See scripts/little_brain_corpus_v16.py docstring + memory notes
REM supervised_falling_v16_direction_2026-08-03 / grounding_probe_v12_findings.
REM Dataset rebuilt 2026-08-04 (4080 samples: +v16 falls 25x4 real
REM error->recovery trajectories + 5x8 clean bait-twins). Warmstart from
REM adapter_v12_bak (stable production base, same as v15); v13/v14/v15
REM dialogues stay in so their gains re-learn. Same tight-VRAM config as
REM v13/v14/v15 (12GB card; run RUNTIME-DOWN via scripts/v16_guardian.py —
REM never with perception loaded).
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
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v16.log" 2>&1
