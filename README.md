# 无人机电池循环健康评估

无需账号的单机 Web 应用:导入无人机飞行日志 CSV(时间/电压/电流/温度),自动识别充放电循环、估算每循环容量与内阻,标记容量突降、异常发热、未闭合循环等异常,给出可解释的判定片段与趋势摘要,并支持按电池导出 JSON 评估结果。

- 后端:FastAPI + SQLite(标准库 `sqlite3`,启动时自动建库建表)
- 前端:原生 HTML/CSS/JS(无构建步骤)
- 分析:状态机循环切分 · 梯形库仑积分 · 静置段电压回落法内阻估算 · 规则引擎判定

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

### 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后访问 **http://localhost:8000**(Web 界面与 API 同端口)。SQLite 数据库文件默认位于 `./data/battery_health.db`(可用环境变量 `DATA_DIR` 修改),首次启动自动完成初始化。

交互式 API 文档:http://localhost:8000/docs

## 使用流程

1. **新建电池**:填写名称、额定容量(Ah)、安全电压上下限(V)。
2. **导入 CSV**:选择电池后上传飞行日志,系统立即完成循环切分与指标计算。
3. **查看评估**:趋势摘要(SoH、内阻、温度、健康等级)+ 循环明细表,点击任意循环查看可解释判定片段。
4. **导出 JSON**:点击「导出评估 JSON」下载该电池完整评估结果。

可先生成演示数据体验:

```bash
python scripts/generate_sample.py   # 生成 sample_data/flight_log_demo.csv
```

该样例包含 4 次放电循环:容量逐次衰减、第 3 次容量突降并伴随异常发热、最后一次循环未闭合(日志在放电中截断)。

## CSV 格式

首行必须为表头,列名支持以下别名(大小写不敏感):

| 含义 | 可识别的列名 |
| --- | --- |
| 时间 | `time` `timestamp` `t` `datetime` `时间` …(相对秒或 ISO 时间戳) |
| 电压 | `voltage` `voltage_v` `v` `volt` `电压` …(V) |
| 电流 | `current` `current_a` `i` `amps` `电流` …(A) |
| 温度 | `temperature` `temp` `temp_c` `温度` …(°C) |

说明:

- 电流符号约定为**放电为正**;若上传数据中活跃电流整体为负,系统自动翻转并在导入结果中提示。
- 无法解析的行会被跳过并计入 `bad_rows`。
- 文件编码支持 UTF-8 / GBK。

## 分析方法

### 循环边界识别(状态机)

状态集合:`rest`(静置,|I| ≤ 0.2A)⇄ `discharge` / `charge`。

- 活跃段之间的静置间隔 < 30s 视为同一活跃期内的短暂回落,予以合并;
- 活跃期内电流方向翻转则切分为两个循环段;
- 活跃持续 < 20s 的段视为抖动丢弃;
- 段结束后出现 ≥ 30s 静置(或紧接下一段)→ 循环**闭合**;日志结束时仍处于活跃状态 → **未闭合循环**并标记。

### 容量估算(库仑积分)

对每个循环段做梯形积分:`容量(Ah) = Σ|I|·Δt / 3600`,`SoH = 容量 / 额定容量 × 100%`。

### 内阻估算(静置电压回落)

循环结束后电流阶跃至 0,电压在静置窗口(60s)内回升(放电)或回落(充电):

`R ≈ |V_relaxed − V_end| / |I_end|`(要求电流阶跃 ≥ 0.5A,否则该循环不给出内阻)。

### 判定规则(均可解释,附证据片段)

| 代码 | 规则 | 级别 |
| --- | --- | --- |
| `capacity_drop` | 容量较上一同类型循环下降 ≥ 10% | critical |
| `low_soh` | SoH < 80% | warning |
| `overtemp` | 循环最高温 ≥ 45°C(≥ 55°C 升级) | warning/critical |
| `temp_rise` | 单循环温升 ≥ 15°C | warning |
| `undervoltage` / `overvoltage` | 电压越出用户设定的安全范围 | critical |
| `ir_growth` | 内阻较上一循环增长 ≥ 1.5 倍 | warning |
| `unclosed_cycle` | 日志结束时循环未闭合 | warning |

趋势摘要包含:SoH 首末值与变化量、容量线性衰减斜率(Ah/循环)、平均/最新内阻、历史最高温、判定计数,以及由规则推导的健康等级(`good` / `attention` / `risk`)与理由列表。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batteries` | 新建电池(名称/额定容量/安全电压范围) |
| GET | `/api/batteries` | 电池列表(含循环数/日志数) |
| GET / PUT / DELETE | `/api/batteries/{id}` | 查询 / 更新设置 / 删除 |
| POST | `/api/batteries/{id}/logs` | 上传飞行日志 CSV(multipart) |
| GET | `/api/batteries/{id}/logs` | 该电池的日志列表 |
| GET | `/api/logs/{id}/cycles` | 某次日志的循环明细 |
| DELETE | `/api/logs/{id}` | 删除日志及其循环 |
| GET | `/api/batteries/{id}/assessment` | 实时评估(循环判定 + 趋势摘要) |
| GET | `/api/batteries/{id}/export` | 导出评估 JSON 文件 |

评估在请求时基于**当前**电池设置实时计算,修改额定容量或安全电压范围后无需重新导入数据。

## 项目结构

```
├── app/
│   ├── main.py          # FastAPI 入口与 API 路由
│   ├── analyzer.py      # 状态机切分 / 库仑积分 / 内阻估算 / 判定规则
│   ├── database.py      # SQLite 初始化与数据访问
│   └── static/          # 原生前端(index.html / app.js / style.css)
├── scripts/generate_sample.py   # 演示数据生成器
├── sample_data/         # 生成的演示 CSV
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
