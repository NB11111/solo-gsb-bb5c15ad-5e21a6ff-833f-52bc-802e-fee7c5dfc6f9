"""电池循环分析引擎。

核心流程:
1. 状态机切分充/放电循环边界 (segment_cycles)
2. 库仑积分估算每循环容量 / 能量
3. 静置段电压回落(回升)估算直流内阻
4. 规则引擎生成可解释判定片段 (assess_battery)

电流符号约定: 内部统一为 放电为正、充电为负。
CSV 导入时若活跃电流整体为负则自动翻转(见 parse_flight_csv)。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

# ---------------------------------------------------------------- 阈值常量
REST_CURRENT_A = 0.2        # |I| 低于此值视为静置
MIN_REST_S = 30.0           # 静置持续这么久才算"循环闭合"
MIN_CYCLE_S = 20.0          # 短于此的活跃段视为抖动, 不计为循环
RELAX_WINDOW_S = 60.0       # 循环结束后用于观测电压回落的静置窗口
MIN_IR_STEP_A = 0.5         # 估算内阻所需的最小电流阶跃

CAPACITY_DROP_PCT = 0.10    # 容量较上一同类型循环下降超过 10% → 容量突降
SOH_WARN_PCT = 80.0         # SoH 低于 80% → 容量老化告警
TEMP_WARN_C = 45.0          # 循环内最高温度超过 45°C → 异常发热
TEMP_SEVERE_C = 55.0        # 超过 55°C → 严重发热(直接影响健康等级)
TEMP_RISE_WARN_C = 15.0     # 单循环温升超过 15°C → 温升异常
IR_GROWTH_WARN = 1.5        # 内阻较上一循环增长超过 1.5 倍 → 内阻异常增长


# ---------------------------------------------------------------- CSV 解析
_TIME_KEYS = {"time", "timestamp", "t", "datetime", "date_time", "time_s", "时间", "时间戳"}
_VOLT_KEYS = {"voltage", "v", "voltage_v", "volt", "volts", "电压"}
_CURR_KEYS = {"current", "i", "current_a", "amp", "amps", "a", "电流"}
_TEMP_KEYS = {"temperature", "temp", "temp_c", "temperature_c", "temperaturec", "温度"}


class CsvFormatError(ValueError):
    """CSV 格式无法识别时抛出。"""


def _match_column(headers: list[str], candidates: set[str]) -> int | None:
    for idx, h in enumerate(headers):
        if h.strip().lower() in candidates:
            return idx
    return None


def _parse_time(raw: str):
    """返回 (epoch_seconds 或 None, 相对秒 或 None)。

    数字 → 相对秒; ISO 日期时间 → epoch 秒。
    """
    raw = raw.strip()
    try:
        return None, float(raw)
    except ValueError:
        pass
    text = raw.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.fromisoformat(text) if fmt is None else datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp(), None
    raise CsvFormatError(f"无法解析时间值: {raw!r}")


def parse_flight_csv(text: str) -> tuple[list[dict], dict]:
    """解析飞行日志 CSV → (samples, meta)。

    samples: [{"t": 相对秒, "voltage": V, "current": A, "temperature": °C}]
    meta:    {"row_count", "bad_rows", "duration_s", "flipped_current"}
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if len(rows) < 2:
        raise CsvFormatError("CSV 内容为空或缺少数据行")

    headers = rows[0]
    ci_t = _match_column(headers, _TIME_KEYS)
    ci_v = _match_column(headers, _VOLT_KEYS)
    ci_i = _match_column(headers, _CURR_KEYS)
    ci_tp = _match_column(headers, _TEMP_KEYS)
    if None in (ci_t, ci_v, ci_i, ci_tp):
        raise CsvFormatError(
            "缺少必需列(时间/电压/电流/温度)。"
            f"识别到的表头: {[h.strip() for h in headers]}"
        )

    samples, bad = [], 0
    epoch0 = None
    for row in rows[1:]:
        try:
            epoch, rel = _parse_time(row[ci_t])
            v = float(row[ci_v])
            i = float(row[ci_i])
            tp = float(row[ci_tp])
        except (ValueError, IndexError, CsvFormatError):
            bad += 1
            continue
        if epoch is not None:
            if epoch0 is None:
                epoch0 = epoch
            rel = epoch - epoch0
        samples.append({"t": rel, "voltage": v, "current": i, "temperature": tp})

    if len(samples) < 5:
        raise CsvFormatError(f"有效数据行过少({len(samples)} 行), 无法分析")

    samples.sort(key=lambda s: s["t"])
    t0 = samples[0]["t"]
    for s in samples:
        s["t"] = round(s["t"] - t0, 6)

    # 符号约定自适应: 活跃段电流总和为负 → 认为原始数据放电为负, 统一翻转
    active_sum = sum(s["current"] for s in samples if abs(s["current"]) > REST_CURRENT_A)
    flipped = active_sum < 0
    if flipped:
        for s in samples:
            s["current"] = -s["current"]

    meta = {
        "row_count": len(samples),
        "bad_rows": bad,
        "duration_s": round(samples[-1]["t"], 3),
        "flipped_current": flipped,
    }
    return samples, meta


# ---------------------------------------------------------------- 状态机切分
def _state(current: float) -> str:
    if current > REST_CURRENT_A:
        return "discharge"
    if current < -REST_CURRENT_A:
        return "charge"
    return "rest"


def segment_cycles(samples: list[dict]) -> list[dict]:
    """状态机: rest ⇄ discharge / charge。

    - 活跃段之间若静置间隔 < MIN_REST_S, 视为同一活跃期内的短暂回落, 合并;
    - 活跃期内电流方向翻转 → 切分为两个循环段;
    - 过短活跃段 (< MIN_CYCLE_S) 丢弃;
    - 日志结束时仍处于活跃状态 → 该循环 closed=False (未闭合)。
    """
    n = len(samples)
    states = [_state(s["current"]) for s in samples]

    # 1) 找出所有活跃区间 [i0, i1]
    intervals = []
    i = 0
    while i < n:
        if states[i] != "rest":
            j = i
            while j + 1 < n and states[j + 1] != "rest":
                j += 1
            intervals.append([i, j])
            i = j + 1
        else:
            i += 1
    if not intervals:
        return []

    # 2) 合并间隔过短的活跃区间
    merged = [intervals[0]]
    for iv in intervals[1:]:
        gap = samples[iv[0]]["t"] - samples[merged[-1][1]]["t"]
        if gap < MIN_REST_S:
            merged[-1][1] = iv[1]
        else:
            merged.append(iv)

    # 3) 按电流方向切分 + 计算指标
    cycles = []
    for i0, i1 in merged:
        k = i0
        while k <= i1:
            direction = states[k]
            j = k
            while j + 1 <= i1 and states[j + 1] == direction:
                j += 1
            seg = _build_cycle(samples, k, j, direction)
            if seg is not None:
                cycles.append(seg)
            k = j + 1

    # 4) 闭合判定: 段结束后存在 >= MIN_REST_S 的静置或紧随下一段 → 闭合
    last_idx = n - 1
    for c in cycles:
        end_idx = c.pop("_end_idx")
        if end_idx >= last_idx:
            c["closed"] = False
        else:
            rest_len = samples[last_idx]["t"] - c["end_t"] if end_idx == last_idx else None
            # 找到段后第一个非静置点或日志结尾
            m = end_idx + 1
            while m < n and states[m] == "rest":
                m += 1
            rest_end_t = samples[m]["t"] if m < n else samples[last_idx]["t"]
            c["closed"] = (rest_end_t - c["end_t"]) >= MIN_REST_S or m < n
            if rest_len is not None:
                c["closed"] = rest_len >= MIN_REST_S
        c.pop("_start_idx", None)

    # 5) 内阻估算需要段索引信息, 重算一遍端点索引
    for c in cycles:
        c["ir_ohm"] = _estimate_ir(samples, c)

    for seq, c in enumerate(cycles, 1):
        c["seq"] = seq
    return cycles


def _build_cycle(samples: list[dict], i0: int, i1: int, direction: str) -> dict | None:
    t0, t1 = samples[i0]["t"], samples[i1]["t"]
    if t1 - t0 < MIN_CYCLE_S:
        return None

    cap_as = 0.0   # 安秒
    energy_ws = 0.0
    for k in range(i0, i1):
        dt = samples[k + 1]["t"] - samples[k]["t"]
        a0, a1 = abs(samples[k]["current"]), abs(samples[k + 1]["current"])
        v0, v1 = samples[k]["voltage"], samples[k + 1]["voltage"]
        cap_as += 0.5 * (a0 + a1) * dt                      # 梯形库仑积分
        energy_ws += 0.5 * (a0 * v0 + a1 * v1) * dt

    seg = samples[i0:i1 + 1]
    currents = [abs(s["current"]) for s in seg]
    voltages = [s["voltage"] for s in seg]
    temps = [s["temperature"] for s in seg]
    v_min_k = min(range(len(seg)), key=lambda k: seg[k]["voltage"])
    t_max_k = max(range(len(seg)), key=lambda k: seg[k]["temperature"])

    return {
        "kind": direction,
        "start_t": round(t0, 3),
        "end_t": round(t1, 3),
        "duration_s": round(t1 - t0, 3),
        "capacity_ah": round(cap_as / 3600.0, 5),
        "energy_wh": round(energy_ws / 3600.0, 4),
        "v_start": round(voltages[0], 4),
        "v_end": round(voltages[-1], 4),
        "v_min": round(voltages and min(voltages) or 0.0, 4),
        "v_min_t": round(seg[v_min_k]["t"], 3),
        "v_max": round(max(voltages), 4),
        "i_avg": round(sum(currents) / len(currents), 4),
        "i_max": round(max(currents), 4),
        "temp_start": round(temps[0], 3),
        "temp_max": round(max(temps), 3),
        "temp_max_t": round(seg[t_max_k]["t"], 3),
        "closed": True,  # 稍后由 segment_cycles 覆写
        "_start_idx": i0,
        "_end_idx": i1,
    }


def _estimate_ir(samples: list[dict], cycle: dict) -> float | None:
    """利用循环结束后的静置段电压回落估算直流内阻: R ≈ ΔV / ΔI。

    放电结束后电压回升 → ΔV = V_relaxed - V_end;
    充电结束后电压回落 → ΔV = V_end - V_relaxed。
    """
    end_t = cycle["end_t"]
    after = [s for s in samples if end_t < s["t"] <= end_t + RELAX_WINDOW_S
             and abs(s["current"]) <= REST_CURRENT_A]
    before = [s for s in samples if end_t - 5.0 <= s["t"] <= end_t
              and abs(s["current"]) > REST_CURRENT_A]
    if len(after) < 2 or not before:
        return None

    i_end = sum(abs(s["current"]) for s in before[-3:]) / min(3, len(before))
    if i_end < MIN_IR_STEP_A:
        return None
    v_end = sum(s["voltage"] for s in before[-3:]) / min(3, len(before))
    tail = after[-3:] if len(after) >= 3 else after
    v_relaxed = sum(s["voltage"] for s in tail) / len(tail)

    delta = (v_relaxed - v_end) if cycle["kind"] == "discharge" else (v_end - v_relaxed)
    if delta <= 0:
        return None
    return round(delta / i_end, 5)


# ---------------------------------------------------------------- 评估判定
def assess_battery(cycles: list[dict], settings: dict) -> dict:
    """基于循环指标 + 用户设置生成判定片段与趋势摘要。

    settings: {"rated_capacity_ah", "safe_vmin", "safe_vmax"}
    """
    rated = settings["rated_capacity_ah"]
    vmin, vmax = settings["safe_vmin"], settings["safe_vmax"]

    assessed = []
    prev_by_kind: dict[str, dict] = {}
    prev_ir_by_kind: dict[str, float] = {}
    for c in cycles:
        soh = round(c["capacity_ah"] / rated * 100.0, 2) if rated > 0 else None
        flags = []
        span = f"{_fmt_t(c['start_t'])}–{_fmt_t(c['end_t'])}"
        kind_cn = "放电" if c["kind"] == "discharge" else "充电"

        if not c["closed"]:
            flags.append({
                "code": "unclosed_cycle", "severity": "warning",
                "title": "未闭合循环",
                "snippet": f"第 {c['seq']} 循环({kind_cn} {span})在日志结束时仍处于活跃状态, "
                           f"循环边界未闭合, 容量 {c['capacity_ah']:.3f}Ah 为不完整统计。",
                "evidence": {"start_t": c["start_t"], "end_t": c["end_t"],
                             "capacity_ah": c["capacity_ah"]},
            })

        prev = prev_by_kind.get(c["kind"])
        if prev and prev["capacity_ah"] > 0:
            drop = 1.0 - c["capacity_ah"] / prev["capacity_ah"]
            if drop >= CAPACITY_DROP_PCT:
                flags.append({
                    "code": "capacity_drop", "severity": "critical",
                    "title": "容量突降",
                    "snippet": f"第 {c['seq']} 循环容量 {c['capacity_ah']:.3f}Ah, 较上一同类型循环 "
                               f"{prev['capacity_ah']:.3f}Ah 下降 {drop * 100:.1f}%, "
                               f"超过阈值 {CAPACITY_DROP_PCT * 100:.0f}%。",
                    "evidence": {"capacity_ah": c["capacity_ah"],
                                 "prev_capacity_ah": prev["capacity_ah"],
                                 "drop_pct": round(drop * 100, 2),
                                 "threshold_pct": CAPACITY_DROP_PCT * 100},
                })

        if soh is not None and soh < SOH_WARN_PCT:
            flags.append({
                "code": "low_soh", "severity": "warning",
                "title": "健康度偏低",
                "snippet": f"第 {c['seq']} 循环 SoH={soh:.1f}%(容量 {c['capacity_ah']:.3f}Ah / "
                           f"额定 {rated:.2f}Ah), 低于 {SOH_WARN_PCT:.0f}% 警戒线。",
                "evidence": {"soh_pct": soh, "rated_capacity_ah": rated},
            })

        if c["temp_max"] >= TEMP_WARN_C:
            sev = "critical" if c["temp_max"] >= TEMP_SEVERE_C else "warning"
            flags.append({
                "code": "overtemp", "severity": sev,
                "title": "异常发热",
                "snippet": f"第 {c['seq']} 循环在 {_fmt_t(c['temp_max_t'])} 测得最高温度 "
                           f"{c['temp_max']:.1f}°C, 超过 {TEMP_WARN_C:.0f}°C 安全阈值。",
                "evidence": {"temp_max": c["temp_max"], "temp_max_t": c["temp_max_t"],
                             "threshold_c": TEMP_WARN_C},
            })

        rise = c["temp_max"] - c["temp_start"]
        if rise >= TEMP_RISE_WARN_C:
            flags.append({
                "code": "temp_rise", "severity": "warning",
                "title": "温升异常",
                "snippet": f"第 {c['seq']} 循环温度从 {c['temp_start']:.1f}°C 升至 "
                           f"{c['temp_max']:.1f}°C, 单循环温升 {rise:.1f}°C "
                           f"超过 {TEMP_RISE_WARN_C:.0f}°C 阈值。",
                "evidence": {"temp_start": c["temp_start"], "temp_max": c["temp_max"],
                             "rise_c": round(rise, 2)},
            })

        if c["v_min"] < vmin:
            flags.append({
                "code": "undervoltage", "severity": "critical",
                "title": "电压低于安全下限",
                "snippet": f"第 {c['seq']} 循环在 {_fmt_t(c['v_min_t'])} 电压跌至 "
                           f"{c['v_min']:.2f}V, 低于安全下限 {vmin:.2f}V, 存在过放风险。",
                "evidence": {"v_min": c["v_min"], "v_min_t": c["v_min_t"],
                             "safe_vmin": vmin},
            })
        if c["v_max"] > vmax:
            flags.append({
                "code": "overvoltage", "severity": "critical",
                "title": "电压高于安全上限",
                "snippet": f"第 {c['seq']} 循环电压峰值 {c['v_max']:.2f}V, "
                           f"高于安全上限 {vmax:.2f}V, 存在过充风险。",
                "evidence": {"v_max": c["v_max"], "safe_vmax": vmax},
            })

        ir = c.get("ir_ohm")
        prev_ir = prev_ir_by_kind.get(c["kind"])
        if ir is not None and prev_ir is not None and prev_ir > 0 \
                and ir >= prev_ir * IR_GROWTH_WARN:
            flags.append({
                "code": "ir_growth", "severity": "warning",
                "title": "内阻异常增长",
                "snippet": f"第 {c['seq']} 循环内阻 {ir * 1000:.1f}mΩ, 较上一同类型循环 "
                           f"{prev_ir * 1000:.1f}mΩ 增长 {ir / prev_ir:.1f} 倍, "
                           f"超过 {IR_GROWTH_WARN:.1f} 倍阈值, 提示老化或连接异常。",
                "evidence": {"ir_ohm": ir, "prev_ir_ohm": prev_ir},
            })
        if ir is not None:
            prev_ir_by_kind[c["kind"]] = ir

        prev_by_kind[c["kind"]] = c
        assessed.append({**c, "soh_pct": soh, "flags": flags})

    return {"cycles": assessed, "summary": _trend_summary(assessed, settings)}


def _trend_summary(cycles: list[dict], settings: dict) -> dict:
    closed = [c for c in cycles if c["closed"]]
    discharge = [c for c in closed if c["kind"] == "discharge"]
    sohs = [c["soh_pct"] for c in discharge if c["soh_pct"] is not None]
    irs = [c["ir_ohm"] for c in closed if c.get("ir_ohm") is not None]

    flag_counts: dict[str, int] = {}
    for c in cycles:
        for f in c["flags"]:
            flag_counts[f["code"]] = flag_counts.get(f["code"], 0) + 1

    # 容量线性趋势 (Ah/循环), 简单最小二乘
    slope = None
    if len(discharge) >= 2:
        xs = list(range(len(discharge)))
        ys = [c["capacity_ah"] for c in discharge]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom > 0:
            slope = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom, 5)

    temp_ever = max((c["temp_max"] for c in cycles), default=None)

    # 健康等级判定(可解释)
    reasons, level = [], "good"
    soh_latest = sohs[-1] if sohs else None
    if soh_latest is not None and soh_latest < 70:
        level = "risk"
        reasons.append(f"最新 SoH {soh_latest:.1f}% 低于 70%")
    elif soh_latest is not None and soh_latest < 85:
        level = "attention"
        reasons.append(f"最新 SoH {soh_latest:.1f}% 处于 70–85% 区间")
    if temp_ever is not None and temp_ever >= TEMP_SEVERE_C:
        level = "risk"
        reasons.append(f"历史最高温度 {temp_ever:.1f}°C 超过 {TEMP_SEVERE_C:.0f}°C")
    if flag_counts.get("capacity_drop"):
        if level != "risk":
            level = "attention"
        reasons.append(f"出现 {flag_counts['capacity_drop']} 次容量突降")
    if flag_counts.get("unclosed_cycle"):
        if level == "good":
            level = "attention"
        reasons.append(f"存在 {flag_counts['unclosed_cycle']} 个未闭合循环")
    if flag_counts.get("undervoltage") or flag_counts.get("overvoltage"):
        level = "risk"
        reasons.append("存在电压越出安全范围的循环")
    if not reasons:
        reasons.append("各循环容量、温度、电压均在设定阈值内")

    return {
        "cycle_count": len(cycles),
        "closed_count": len(closed),
        "unclosed_count": len(cycles) - len(closed),
        "discharge_cycle_count": len(discharge),
        "soh_first_pct": sohs[0] if sohs else None,
        "soh_latest_pct": soh_latest,
        "soh_delta_pct": round(sohs[-1] - sohs[0], 2) if len(sohs) >= 2 else None,
        "capacity_slope_ah_per_cycle": slope,
        "ir_avg_ohm": round(sum(irs) / len(irs), 5) if irs else None,
        "ir_latest_ohm": irs[-1] if irs else None,
        "temp_max_ever_c": temp_ever,
        "flag_counts": flag_counts,
        "health": level,
        "health_reasons": reasons,
        "settings_used": settings,
    }


def _fmt_t(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"
