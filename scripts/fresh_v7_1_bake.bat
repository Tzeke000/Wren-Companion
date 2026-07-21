@echo off
REM Fresh v7.1 7B little-brain bake (2026-07-21, Zeke "go for it"). Same pinned
REM config as v7 (Qwen2.5-7B, attn-only q/k/v/o, seq 256, save every 25) but on
REM the REBALANCED dataset: grounding x4 + learned-lessons layer x3 (commit
REM 928faf4) to fix v7's grounding regression. Trains from scratch on the
REM freshly-rebuilt state/little_brain/train.jsonl (1645 samples).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v7_1.log" 2>&1
