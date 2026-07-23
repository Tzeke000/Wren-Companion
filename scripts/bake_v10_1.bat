@echo off
REM v10.1 POLISH bake (2026-07-22, Zeke: polish + anything-uncertain). Warm from
REM v10 (adapter_v10_bak — tool-fluent) on the dataset that adds: pet-refusal
REM ("I don't have pets"), a hard motor-cal answer, humility/defer ("long
REM reasoning goes to big-Iris"), emit-call-ALONE, and the limits-loop CLOSING
REM (memory_recall a limit -> defer). memory_search also upgraded (curated
REM facts-file first) — runtime, no bake. SEQ=320 fits the multi-turn dialogues.
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v10_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=320
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v10_1.log" 2>&1
