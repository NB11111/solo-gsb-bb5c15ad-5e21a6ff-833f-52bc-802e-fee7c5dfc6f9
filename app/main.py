"""无人机电池循环健康评估 — FastAPI 入口。

启动即完成 SQLite 初始化; Web 界面与 API 均通过 8000 端口访问。
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analyzer, database

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="无人机电池循环健康评估",
    version="1.0.0",
    description="导入飞行日志 CSV, 基于状态机识别充放电循环, 评估容量/内阻/温度健康指标。",
)


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


# ------------------------------------------------------------------ schemas
class BatteryIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    rated_capacity_ah: float = Field(..., gt=0, description="额定容量 Ah")
    safe_vmin: float = Field(..., gt=0, description="安全电压下限 V")
    safe_vmax: float = Field(..., gt=0, description="安全电压上限 V")


class BatteryUpdate(BaseModel):
    name: str | None = None
    rated_capacity_ah: float | None = Field(None, gt=0)
    safe_vmin: float | None = Field(None, gt=0)
    safe_vmax: float | None = Field(None, gt=0)


def _get_battery_or_404(battery_id: int) -> dict:
    battery = database.get_battery(battery_id)
    if battery is None:
        raise HTTPException(404, f"电池 {battery_id} 不存在")
    return battery


def _settings_of(battery: dict) -> dict:
    return {
        "rated_capacity_ah": battery["rated_capacity_ah"],
        "safe_vmin": battery["safe_vmin"],
        "safe_vmax": battery["safe_vmax"],
    }


def _assessment_for(battery: dict) -> dict:
    cycles = database.list_cycles(battery["id"])
    result = analyzer.assess_battery(cycles, _settings_of(battery))
    return {
        "battery": battery,
        "cycles": result["cycles"],
        "summary": result["summary"],
    }


# ------------------------------------------------------------------ batteries
@app.post("/api/batteries", status_code=201)
def create_battery(payload: BatteryIn):
    if payload.safe_vmin >= payload.safe_vmax:
        raise HTTPException(400, "安全电压下限必须小于上限")
    return database.create_battery(
        payload.name, payload.rated_capacity_ah,
        payload.safe_vmin, payload.safe_vmax,
    )


@app.get("/api/batteries")
def list_batteries():
    return database.list_batteries()


@app.get("/api/batteries/{battery_id}")
def get_battery(battery_id: int):
    return _get_battery_or_404(battery_id)


@app.put("/api/batteries/{battery_id}")
def update_battery(battery_id: int, payload: BatteryUpdate):
    _get_battery_or_404(battery_id)
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "没有需要更新的字段")
    updated = database.update_battery(battery_id, fields)
    if updated["safe_vmin"] >= updated["safe_vmax"]:
        raise HTTPException(400, "安全电压下限必须小于上限")
    return updated


@app.delete("/api/batteries/{battery_id}")
def delete_battery(battery_id: int):
    if not database.delete_battery(battery_id):
        raise HTTPException(404, f"电池 {battery_id} 不存在")
    return {"deleted": battery_id}


# ------------------------------------------------------------------ logs
@app.post("/api/batteries/{battery_id}/logs", status_code=201)
async def upload_log(battery_id: int, file: UploadFile = File(...)):
    """上传飞行日志 CSV, 立即执行循环切分并落库。"""
    _get_battery_or_404(battery_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(400, "文件编码无法识别(支持 UTF-8 / GBK)")

    try:
        samples, meta = analyzer.parse_flight_csv(text)
    except analyzer.CsvFormatError as exc:
        raise HTTPException(422, str(exc))

    cycles = analyzer.segment_cycles(samples)
    log_id = database.create_log(battery_id, file.filename or "upload.csv", meta)
    database.insert_samples(log_id, samples)
    database.insert_cycles(battery_id, log_id, cycles)
    database.set_log_cycle_count(log_id, len(cycles))

    return {
        "log_id": log_id,
        "filename": file.filename,
        "row_count": meta["row_count"],
        "bad_rows": meta["bad_rows"],
        "duration_s": meta["duration_s"],
        "flipped_current": meta["flipped_current"],
        "cycle_count": len(cycles),
        "unclosed_count": sum(1 for c in cycles if not c["closed"]),
    }


@app.get("/api/batteries/{battery_id}/logs")
def list_logs(battery_id: int):
    _get_battery_or_404(battery_id)
    return database.list_logs(battery_id)


@app.get("/api/logs/{log_id}/cycles")
def log_cycles(log_id: int):
    if database.get_log(log_id) is None:
        raise HTTPException(404, f"日志 {log_id} 不存在")
    return database.list_cycles_by_log(log_id)


@app.delete("/api/logs/{log_id}")
def delete_log(log_id: int):
    if not database.delete_log(log_id):
        raise HTTPException(404, f"日志 {log_id} 不存在")
    return {"deleted": log_id}


# ------------------------------------------------------------------ assessment
@app.get("/api/batteries/{battery_id}/assessment")
def assess(battery_id: int):
    """实时评估: 循环指标 + 判定片段 + 趋势摘要(使用当前电池设置)。"""
    battery = _get_battery_or_404(battery_id)
    return _assessment_for(battery)


@app.get("/api/batteries/{battery_id}/export")
def export_assessment(battery_id: int):
    """按电池导出完整评估结果 JSON 文件。"""
    battery = _get_battery_or_404(battery_id)
    payload = _assessment_for(battery)
    payload["exported_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         __import__("time").gmtime())
    payload["methodology"] = {
        "cycle_detection": "状态机(rest/discharge/charge), 静置>=30s闭合循环",
        "capacity": "梯形库仑积分 Σ|I|·dt/3600",
        "internal_resistance": "循环结束后60s静置窗口电压回落 ΔV/ΔI",
        "thresholds": {
            "capacity_drop_pct": analyzer.CAPACITY_DROP_PCT * 100,
            "temp_warn_c": analyzer.TEMP_WARN_C,
            "temp_rise_warn_c": analyzer.TEMP_RISE_WARN_C,
            "soh_warn_pct": analyzer.SOH_WARN_PCT,
        },
    }
    filename = f"battery_{battery_id}_assessment.json"
    return JSONResponse(
        content=json.loads(json.dumps(payload, ensure_ascii=False)),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ frontend
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
