# 裂缝切片标注交接

同一张原图上的同一条路面裂缝可能落在多张重叠的切片上。本工具把一份
「原图裂缝框 + 切片框」清单转换成**每张切片内的标注清单**，保证每条裂缝
在模型评测中只被计一次正例：

- 每条裂缝只在**一张能完整容纳其整框**的切片上标为 `positive`；
- 其他与该裂缝有面积重叠的切片，把**重叠部分**标为 `ignore`；
- 含正例的切片数量最少（最小集合覆盖），便于集中复核；
- 复核员逐片查看并确认；确认时校验页面所见版本仍是现行版；
- 替换裂缝框表时，新版本生效前已确认的切片标注**原样保留**；无解则列出冲突并保留旧清单。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据保存在命名卷 `annotator-data` 中，重建容器不丢确认记录。

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
ANNOTATOR_DB=./data/annotator.sqlite3 python -m flask --app wsgi:app run --port 8000
```

## 导入格式

`POST /api/import`（网页上的文本框或文件选择同效），JSON 对象：

```json
{
  "cracks": [{"id": 1, "x": 300, "y": 400, "w": 120, "h": 20}],
  "tiles":  [{"id": 1, "x": 0,   "y": 0,   "w": 900, "h": 700}]
}
```

- 坐标全部是原图上的**整数像素**：`x/y` 为左上角，`w/h` 为宽高，必须为正。
  浮点数（如 `3.0`）、字符串（如 `"3"`）、布尔值一律拒绝，避免像素偏移。
- `id` 为从 1 开始的唯一正整数。
- 切片最多 **20** 个。
- 页面上「下载示例导入」可拿到一份 7 条裂缝、6 张切片的可运行样例。

## 下载标注清单

`GET /api/manifest` 返回现行版本的 JSON 附件：

```json
{
  "format": "slice-annotations/v1",
  "coordinate_system": "slice-local pixels, origin at slice top-left, [x, y, w, h]",
  "revision": 3,
  "fingerprint": "…",
  "tiles": [
    {"id": 1, "rect": [0, 0, 900, 700], "confirmed": true,
     "annotations": [{"crack": 1, "kind": "positive", "rect": [300, 400, 120, 20]}]}
  ]
}
```

标注坐标是**切片内坐标**（减去切片左上角），可直接写进切片文件。

## 版本与确认语义

每次导入生成一个不可变 *revision*，同一时刻只有一个 `active`。

- **确认令牌**：页面为每张切片渲染 `token`（revision id + 该切片标注的
  SHA-256）。`POST /api/confirm {tile, token}` 在 `BEGIN IMMEDIATE` 事务内
  复核令牌；若导入已切换版本，返回 `409 stale`，不写任何记录。
- **冻结快照**：已确认切片的标注会作为快照随新版本原样携带（`pinned`），
  不再按新裂缝表重算。因此替换裂缝框表后，旧确认切片上的标注一字不改。
- **冲突**：当新表使冻结快照无法自洽（裂缝被删/改框、正例不再完整落在切片内、
  切片框移动导致切片内坐标失效、多条裂缝争抢同一冻结正例、或某裂缝在未确认
  切片中放不下正例）时，导入返回 `409`，冲突写入 `revisions(conflicts_json)`，
  旧 `active` 版本与下载清单保持不变。页面顶部显示冲突原因。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 标注交接页面 |
| GET | `/api/state` | 现行版本、切片、确认状态、最近一次冲突 |
| POST | `/api/import` | 导入裂缝/切片框，生成新版本或返回冲突 |
| POST | `/api/confirm` | 用页面令牌确认某张切片 |
| GET | `/api/manifest` | 下载按切片内坐标记录的标注清单 |
| GET | `/api/sample` | 下载内置示例导入 |
| GET | `/healthz` | 健康检查 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：最小覆盖（对拍穷举）、切片内坐标换算、边界相接不产生忽略、整数校验、
确认幂等、版本切换后的陈旧令牌拒绝、冻结快照保留、切片移动/裂缝改框冲突、
旧清单在冲突后继续可用，以及「更正与确认交错」的端到端流程。
