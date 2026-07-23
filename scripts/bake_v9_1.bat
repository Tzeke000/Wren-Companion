@echo off
REM v9.1 bake (2026-07-22, Zeke greenlit). Warm-starts from the v9 adapter
REM (adapter_v9_bak — has the general don't-fabricate + grounding) on the
REM regenerated dataset that now adds: WREN_ROLE anchors (fixes v9's residual
REM "Wren is my mother"/pet-cat drift) + the v10 SELF banks (lane/escalation,
REM small-brain humility, relationship-with-Zeke). Same proven config
REM (paged_adamw_8bit avoids the sysmem wedge; seq 256; 2 epochs; save 25).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v9_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v9_1.log" 2>&1
