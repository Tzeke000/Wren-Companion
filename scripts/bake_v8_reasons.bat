@echo off
REM v8 REASONS bake (2026-07-21, Zeke: "train the v7 on what it failed and let
REM it know WHY those things are true — give it the reason for the thing").
REM Warm-starts from the v7 adapter (adapter_v7_bak, identity-solid) on the
REM regenerated dataset that now includes the v8 reasoned failure-cases
REM (little_brain_corpus_v8_reasons.py x5: prox-quality=confidence mechanism,
REM reserve-control authority + safety-veto, stock roam/cliff/calibration WHY).
REM Same proven config as the v7.1 warmstart (paged_adamw_8bit avoids the
REM sysmem-fallback wedge; seq 256; 2 epochs; save every 25).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v7_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v8.log" 2>&1
