@echo off
REM Resume the v7 7B little-brain bake from its last checkpoint (2026-07-21,
REM post power-blip). Pinned to the EXACT config that made checkpoint-250:
REM Qwen2.5-7B base, attn-only LoRA (q/k/v/o), seq 256, save every 50 steps.
REM IRIS_LB_RESUME=1 -> HF Trainer picks up the latest checkpoint in output_dir.
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_SAVE=steps
set IRIS_LB_SEQ=256
set IRIS_LB_RESUME=1
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v7_resume.log" 2>&1
