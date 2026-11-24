# 按路径恢复备份子目录 — FastAPI 服务

误删某个子目录、而恢复盘放不下整份备份时，值班员可以只从只读挂载的
`tar.gz` 备份里恢复**一个子目录**。

备份成员路径唯一，支持目录、普通文件和硬链接。硬链接按归档根解析，
允许串联，目标可以位于所选目录之外，也可以比链接本身更晚出现。
恢复结果只包含所选目录；指向同一原件的文件在磁盘上共用一个 inode。

## 快速开始

```bash
docker compose up --build
```

启动后：

* Swagger UI：<http://localhost:8000/docs>
* 示例备份：容器内 `/backups/sample-backup.tar.gz`（只读挂载）
* 恢复结果：宿主 `./restored/<restore-id>/2024/...`
* 演练副本：宿主 `./replica/<目录名>`（整修 API 的操作对象）

在 Swagger 里执行 `POST /api/restores`：

```json
{ "archive": "sample-backup.tar.gz", "path": "data/2024" }
```

或命令行：

```bash
curl -s -X POST http://localhost:8000/api/restores \
  -H 'Content-Type: application/json' \
  -d '{"archive":"sample-backup.tar.gz","path":"data/2024"}' | jq
```

响应示例（截断）：

```json
{
  "requested_path": "data/2024",
  "restored_path": "/restored/3f0c.../2024",
  "directories": 2,
  "files": 5,
  "hardlinks": 7,
  "bytes_written": 14188,
  "bytes_extracted": 1002104,
  "archive_payload_bytes": 987916
}
```

> `docker compose` 通过 `RESTORE_UID` / `RESTORE_GID`（默认 `1000`）以宿主用户身份
> 运行，保证写进 `./restored` 的文件属主正确。可按需覆盖：
> `RESTORE_UID=$(id -u) RESTORE_GID=$(id -g) docker compose up --build`。

## 示例备份里的跨目录硬链接

`tools/make_sample_archive.py` 生成 `backups/sample-backup.tar.gz`：

| 成员 | 类型 | 目标 |
|------|------|------|
| `shared/blob.bin` | 普通文件 | — （在链接**之前**出现，位于所选目录**之外**） |
| `data/2024/shared_link` | 硬链接 | `shared/blob.bin` |
| `data/2024/chained_mid` | 硬链接 | `shared/blob.bin` |
| `data/2024/chained_link` | 硬链接 | `data/2024/chained_mid`（串联） |
| `data/2024/three_way_a` | 硬链接 | `shared/blob.bin` |
| `data/2024/three_way_b` | 硬链接 | `data/2024/three_way_a`（链向链接） |
| `data/2024/self_reference` | 硬链接 | `data/2024/inside.txt`（目录内原件） |
| `data/2024/forward_link` | 硬链接 | `shared/forward.bin`（目标**晚于**链接出现） |
| `data/2024/dup_a.txt`、`data/2024/nested/dup_b.txt` | 普通文件 | 内容相同但**互不相链**（演示误合并拆链） |
| `data/2025/...`、`unrelated.bin` | 普通文件 | 不在所选目录，必须跳过 |

恢复 `data/2024` 后，`shared_link`、`chained_link`、`chained_mid`、
`three_way_a`、`three_way_b` 五个名字的 inode 完全相同；
`self_reference` 与 `inside.txt` 共用 inode。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/archives` | 列出可恢复的 `*.tar.gz` / `*.tgz` |
| `POST` | `/api/restores` | 按 `archive` + `path` 恢复一个子目录 |
| `GET` | `/api/restores` | 列出已发布的恢复编号及其目录 |
| `POST` | `/api/repairs` | 以恢复结果为准，整修副本目录的硬链接 |

状态码：`201` 恢复成功；`200` 整修成功；`400` 归档名/恢复编号/副本目录名非法；
`404` 归档、子目录、恢复编号或副本目录不存在；`409` 所选路径不是目录、
副本与恢复结果冲突、已有整修在进行；`422` 路径非法、链接缺失/成环、
归档成员冲突；`429` 并发恢复数超限；`507` 磁盘不足或其它 I/O 错误。

## 硬链接整修（演练副本）

演练目录经普通复制（`cp -r`、不带 `-H` 的 rsync 等）会**丢掉硬链接**；
按内容去重又可能把**无关文件连在一起**。`POST /api/repairs` 以已发布的
恢复结果为准，重建副本挂载卷上某个目录的硬链接结构：

* 值班员提交 `restore_id`（恢复编号）和 `replica`（副本挂载卷上的目录名）；
* 恢复结果决定路径集合、类型（仅目录和普通文件）以及哪些路径共用一个
  inode；副本的**文件内容一律保留**，整修只重写链接结构；
* 应合并的副本文件字节不同、路径/类型对不上、出现符号链接等不支持的
  类型 → 返回 `409` 并列出全部冲突，**原目录分毫不动**；
* 误合并的文件即使内容相同也拆开；只有拆链需要新 inode 时才复制对应
  文件（`bytes_copied` 计数），其余一律 `os.link` 零拷贝；
* 整棵树在副本挂载卷上暂存、校验后**一次换入**副本原路径；换入前任何
  失败都保留原目录，进程在两次 rename 之间崩溃时，下次启动自动把原目录
  换回去；
* 整修期间两棵树停写：整修全局串行（并发整修返回 `409`），已发布的恢复
  树本身不可变，副本树只有整修会写。

### 在 Swagger 里演示整修

```bash
docker compose up --build
```

1. `POST /api/restores` 恢复 `data/2024`，记下响应里的恢复编号
   （`restored_path` 倒数第二段），或用 `GET /api/restores` 查看；
2. 制造一个"有损"演练副本（丢掉硬链接 + 模拟按内容去重）：

   ```bash
   python tools/make_drill_copy.py restored/<restore-id>/2024 replica/2024-drill --dedup
   ```

3. `POST /api/repairs`：

   ```json
   { "restore_id": "<restore-id>", "replica": "2024-drill" }
   ```

   响应示例（`dup_a.txt` 与 `nested/dup_b.txt` 内容相同但本不相链，
   被去重误合并后由整修拆开）：

   ```json
   {
     "restore_id": "<restore-id>",
     "replica": "2024-drill",
     "repaired_path": "/replica/2024-drill",
     "directories": 2,
     "files": 12,
     "merged": [],
     "split": [["dup_a.txt", "nested/dup_b.txt"]],
     "bytes_copied": 34
   }
   ```

4. 不加 `--dedup` 的纯复制演示**合链**：`merged` 会列出重新合链的两个
   路径组（五链接一组、`inside.txt`/`self_reference` 一组），
   `bytes_copied` 为 0；
5. 演示冲突拒绝：`python tools/make_drill_copy.py restored/<id>/2024 \
   replica/2024-bad --corrupt shared_link` 后再整修 → `409`，
   `detail.conflicts` 列出字节不同的合并组，原目录保持可用。

可用 `find replica/2024-drill -type f -printf '%i %P\n'` 对比整修前后的
inode 分组。

## 实现要点

* **流式读取**：`tarfile` 的 `r|gz` 模式只顺序读一遍，不 seek，适配只读挂载。
* **只写所选目录需要的数据**：无关成员的负载按块读出后丢弃，绝不落盘。
  实测示例备份：归档负载 987,916 B，实际解出 1,002,104 B（≈1.01×）；
  极端情况下（所选目录只含一个指向归档**末尾**外部文件的链接）为 2.0×，
  仍满足“累计解出不超过完整解压一次的两倍”。
* **硬链接**：全部元数据收齐后按归档根解析，支持串联、反向引用、跨目录。
  指向同一普通文件的成员统一 `os.link` 到同一个已落盘副本，因此共享 inode。
  若原件在所选目录内则直接以它为源；否则在**选择组内**选一个名字落盘数据、
  其余链接到它——恢复结果里不会多出所选目录之外的路径。
* **缺少目标 / 成环**：只对“所选目录依赖的链接”报错（`422`），
  所选目录之外的坏链接不影响恢复。
* **原子发布**：所有数据写进 `<output>/.staging/<token>/<basename>`，
  数据与硬链接全部就绪、`fsync` 后才 `rename` 到
  `<output>/<restore-id>/<basename>`。任何失败都会删除本次的 staging 与
  发布目录，`/restored` 下不会残留半成品。
* **安全**：拒绝绝对路径、`..` 穿越、重复/类型冲突成员；符号链接、设备、
  FIFO 一律不落盘（`skipped_unsupported` 计数），避免经由链接逃逸。
* **整修（repair）**：先只读扫描两棵树（`os.scandir` 不跟随符号链接），
  路径集合、类型、合并组字节三者任一不符即收集全部冲突并拒绝；随后按
  恢复树的 inode 分组规划——每个恢复组认领一个未被占用的副本 inode
  直接 `os.link`，被误合并抢占的组才复制字节换新 inode；暂存树与恢复
  树做结构自检（路径集 + inode 分组完全一致）后，两次 rename 换入副本
  原路径，失败即回滚，启动时按 `TARGET` 标记恢复崩溃现场。

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest -q
```

测试覆盖（`tests/test_restore.py`、`tests/test_api.py`、`tests/test_repair.py`）：

* 只恢复所选子目录、跳过兄弟目录与无关大文件；
* 跨目录、串联、链向链接、前向引用、目录内原件的 inode 共享；
* 硬链接缺目标 / 成环 → 报错且清理残留；
* 写到一半失败（模拟磁盘满）→ 清理 staging 与发布目录；
* 累计解出字节 ≤ 2×归档负载；
* 路径穿越、绝对路径、符号链接逃逸、重复目录项、隐式父目录；
* 整修：断链副本的合链、误合并（含内容相同者）的拆链、拆链字节保留、
  整修幂等；路径缺失/多余、类型不符、合并组字节不同、符号链接 → 冲突
  且原目录不动；构建失败与换入失败 → 回滚且无暂存残留；
* API 各状态码与 Swagger 可调用的完整 HTTP 流程。

## 已知限制 / 后续工作

* 仅支持 `tar.gz`/`.tgz`（`tar` 的 `r:*` 形式可扩展，当前明确限定 gzip）。
* 符号链接、设备、FIFO 不恢复；如值班流程需要，应显式加入并做目标校验。
* 硬链接目标若在归档中重复出现且内容不同，会按“路径唯一”前提视为异常报错。
* 整修只处理目录与普通文件；副本树内出现其它类型即冲突拒绝。
* 整修以树内结构为准：副本文件与副本目录**之外**的 inode 共享
  （`st_nlink` 大于树内成员数）目前不检测也不拆开。
* 拆链复制保留副本的模式位与 mtime，不复制 xattr/ACL；新 inode 的属主
  为服务运行用户（`docker compose` 默认即宿主用户，通常无影响）。
* 换入用两次 rename（原树暂存 → 新树就位），之间存在极小的崩溃窗口；
  服务重启时会按暂存里的 `TARGET` 标记把原目录换回去，未换为
  `RENAME_EXCHANGE` 单次系统调用。
* 当前无鉴权、无恢复任务持久化；生产部署应置于内网或加认证，并按需
  增加过期清理策略（启动时已清理 24 小时前的 staging 残留）。
