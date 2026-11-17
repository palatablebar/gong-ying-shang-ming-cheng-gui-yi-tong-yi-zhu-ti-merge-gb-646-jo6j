# 运行手册 供应商主数据台账

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18205/api/health
```

首次打开任意页面会按内置样例自动采信一次，随后逐级计算归一名、分块、候选对与合并簇。

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

`-v` 会删除命名卷 `merge-data`，批次表与复核决定一并清空。

## 重跑与幂等

- `POST /api/suppliers/import` 带 `force: true` 时按同一批文件重算。
- 批次号由输入摘要（按文件名排序的 sha256 拼接）与规则版本共同决定，同一批文件得到同一个 `run_id`；
  命中已有批次时直接复用结果，不新增记录。
- 复核决定写入后，簇与金额会立即重算，不需要手工触发。

## 付款主体与集团归集

```bash
curl -s http://127.0.0.1:18205/api/hierarchy                 # 付款主体 + 集团 + 对账
curl -s -X POST http://127.0.0.1:18205/api/hierarchy/publish # 重新发布层级（内容未变则不新增版本）
curl -s http://127.0.0.1:18205/api/hierarchy/versions        # 版本链与各版本变化层
curl -s http://127.0.0.1:18205/api/hierarchy/log             # 发布成功/失败记录
curl -s http://127.0.0.1:18205/api/export/payable-subjects.csv -o payable.csv
curl -s http://127.0.0.1:18205/api/export/groups.csv -o groups.csv
```

确认一个付款主体（指纹取自 `/api/hierarchy/subjects`）：

```bash
curl -s -X POST http://127.0.0.1:18205/api/hierarchy/confirm \
  -H 'Content-Type: application/json' \
  -d '{"scope":"subject","target_key":"T|91110108MA01C7X21","decision":"confirm",
       "expected_fingerprint":"<身份指纹>"}'
```

口径提醒：

- 付款只认**付款主体**（一个税号一个主体，账户唯一）。集团文件标"否（仅归集）"，不能拿去付款。
- 上游只改名称时确认继续有效；改税号或账户只让受影响主体与所在集团的确认失效。
- 发布失败（`H_PUBLISH_FAILED`）时旧版本仍是当前版本，页面与导出读数不变；查发布日志后再重试。

## 常用请求

```bash
curl -s -X POST http://127.0.0.1:18205/api/suppliers/import -H 'Content-Type: application/json' -d '{}'
curl -s http://127.0.0.1:18205/api/reconcile
curl -s http://127.0.0.1:18205/api/export/master.csv -o master.csv
curl -s http://127.0.0.1:18205/api/export/manifest.json -o manifest.json
```

判定一张复核单：

```bash
curl -s -X POST http://127.0.0.1:18205/api/reviews/P-00105/decision \
  -H 'Content-Type: application/json' \
  -d '{"decision":"merge","expected_version":0}'
```

## 失败码处理

| 原因码 | 处理 |
|---|---|
| `E_FILE_MISSING` | 检查 `sample_data/` 下的文件名与大小写 |
| `E_MISSING_COLUMN` | 表头缺必填列，按列契约补列后重试 |
| `E_DECODE_FAILED` | 文件既非 UTF-8 也非 GB18030，需转码 |
| `Q_ROW_SHORT` / `Q_ROW_LONG` | 该行进隔离区，修正分隔符后重跑 |
| `V_STALE_DECISION` | 页面停留过久，刷新后按新版本再提交 |
| `V_BAD_DECISION` | 税号冲突的候选对不允许合并 |
| `V_UNKNOWN_PAIR` | 候选对编号不属于当前批次，刷新页面 |
| `H_CHECK_FAILED` | 层级校验未通过（记录/金额/编号不守恒），整版不发布，查 `GET /api/hierarchy/log` |
| `H_PUBLISH_FAILED` | 发布事务失败，旧版本保留；修正存储后重试 `POST /api/hierarchy/publish` |
| `H_BAD_CONFIRM` | 确认指纹已变化或动作非法，刷新页面按新指纹重新确认 |
| `V_UNKNOWN_VERSION` | 层级版本号不存在，重新发布或刷新页面 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
```
