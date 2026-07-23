@echo off
REM v7.1 WARM-START bake (2026-07-21, Zeke: "take the already-made v7 and train
REM it with the new stuff — saves time"). Continue-trains from v7's finished
REM adapter (adapter_v7_bak) on the REBALANCED dataset (grounding x4 + lessons
REM x3, commit 928faf4). Identity is already baked into v7 so it just needs to
REM strengthen grounding + absorb lessons -> fewer epochs.
REM WEDGE FIX: IRIS_LB_OPTIM=paged_adamw_8bit. The default adamw_torch kept full
REM optimizer state in VRAM -> tipped into driver sysmem-fallback thrash (step 0
REM never completed, 0.004 it/s). The paged 8-bit optimizer pages under pressure,
REM which is what the ORIGINAL v7 used and why it stepped fine.
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v7_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v7_1_warm.log" 2>&1
