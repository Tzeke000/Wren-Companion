@echo off
REM v9 bake (2026-07-21, Zeke: root fix — "when it doesn't know something, not
REM make something up, in ALL cases not just the body" + build on v8). Warm-
REM starts from the v8 adapter (adapter_v8_bak — has the grounding wins) on the
REM regenerated dataset that now includes the v9 general don't-fabricate
REM principle + canonical family roster (fixes v8's baked confab) + cerebellum
REM prediction pairs + earned lessons. Same proven config (paged_adamw_8bit
REM avoids the sysmem-fallback wedge; seq 256; 2 epochs; save every 25).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v8_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v9.log" 2>&1
