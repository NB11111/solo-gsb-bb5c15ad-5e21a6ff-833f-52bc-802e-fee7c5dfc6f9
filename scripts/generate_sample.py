#!/usr/bin/env python3
"""生成演示用飞行日志 CSV(6S 电池, 25.2V 满电 / 19.2V 安全下限)。

场景: 4 次放电循环, 容量逐次衰减, 第 3 次容量突降并伴随异常发热,
最后一次循环日志中途截断(未闭合)。循环之间含充电段与静置段。
"""

import csv
import math
import os
import random
from datetime import datetime, timedelta

random.seed(42)


def discharge_cycle(t, cap_ah, avg_a, temp_peak):
    """放电段 + 90s 静置(电压回升, 用于内阻估算)。返回值为结束时刻。"""
    duration = cap_ah / avg_a * 3600
    steps = int(duration)
    for k in range(steps):
        soc = 1.0 - (k / steps)
        current = avg_a + 2.5 * math.sin(k / 25.0) + random.uniform(-0.4, 0.4)
        voltage = (19.6 + 5.4 * soc) - 0.012 * current + random.uniform(-0.03, 0.03)
        temp = 24.0 + (temp_peak - 24.0) * (k / steps) + random.uniform(-0.2, 0.2)
        yield t + k, voltage, current, temp
    v_end = 19.6 - 0.012 * avg_a
    for k in range(90):  # 静置: 电压指数回升
        relax = 1 - math.exp(-k / 18.0)
        yield (t + steps + k,
               v_end + 0.012 * avg_a * relax + random.uniform(-0.01, 0.01),
               random.uniform(-0.03, 0.03), 24.5 + random.uniform(-0.2, 0.2))
    return t + steps + 90


def charge_cycle(t, cap_ah):
    """充电段(负电流) + 60s 静置。"""
    avg_a = 3.0
    steps = int(cap_ah / avg_a * 3600)
    for k in range(steps):
        soc = k / steps
        yield (t + k, 21.5 + 3.6 * soc + random.uniform(-0.02, 0.02),
               -(avg_a + random.uniform(-0.1, 0.1)), 26.0 + random.uniform(-0.3, 0.3))
    for k in range(60):
        yield (t + steps + k, 25.1 - 0.15 * (1 - math.exp(-k / 15.0)),
               random.uniform(-0.02, 0.02), 25.0)
    return t + steps + 60


def run(gen):
    """排干生成器, 返回 (rows, 结束时刻)。"""
    rows = []
    try:
        while True:
            rows.append(next(gen))
    except StopIteration as stop:
        return rows, stop.value


def main():
    rows = []
    t = 0.0
    # (容量Ah, 平均电流A, 峰值温度°C) — 第3次: 容量突降 + 异常发热
    plan = [(4.6, 8.0, 38.0), (4.55, 8.5, 39.0), (3.6, 9.0, 52.0), (4.4, 8.0, 40.0)]
    for idx, (cap, cur, tp) in enumerate(plan):
        seg, t = run(discharge_cycle(t, cap, cur, tp))
        if idx == len(plan) - 1:
            rows.extend(seg[:-90])   # 截掉静置段 → 日志在放电中结束(未闭合)
            break
        rows.extend(seg)
        chg, t = run(charge_cycle(t, cap * 0.98))
        rows.extend(chg)

    start = datetime(2026, 9, 20, 10, 0, 0)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "sample_data", "flight_log_demo.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "voltage_v", "current_a", "temperature_c"])
        for tt, v, i, tp in rows:
            w.writerow([(start + timedelta(seconds=tt)).isoformat(),
                        f"{v:.3f}", f"{i:.3f}", f"{tp:.2f}"])
    print(f"已生成 {out}: {len(rows)} 行")


if __name__ == "__main__":
    main()
