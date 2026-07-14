# pg-slide-harvester

`pg-slide-harvester` is a lightweight command-line tool for discovering,
downloading, and organizing public PostgreSQL conference slide decks.

It is built for PostgreSQL users who want a practical local archive of talks,
PDFs, PPT/PPTX files, and other public presentation materials without manually
opening every event website.

## 中文说明

### 项目简介

`pg-slide-harvester` 用于自动发现、下载和整理 PostgreSQL 生态会议中的公开
PPT/PDF 资料。它会从 PostgreSQL 官方活动列表和会议官网中发现资料链接，将
下载结果按主题分类归档，并为后续检索和补抓提供本地状态记录。

这个项目的目标不是做一个复杂平台，而是解决一个真实的小痛点：

- PostgreSQL 会议、PGDay、meetup、技术沙龙很多，资料分散在不同网站。
- 会议 PPT 往往不会第一时间发布，可能几天或几周后才补上。
- 手动登录、查找、逐个下载和重命名很浪费时间。
- 下载后的文件名常常是缩写或 URL slug，不利于后续阅读和检索。

### 核心能力

- 从 `postgresql.org` 官方活动页发现 PostgreSQL 相关会议。
- 按会议名称自动触发下载，例如：
  `python3 pgppt.py download-event "CERN PGDay 2026"`。
- 支持公开 PDF、PPT、PPTX、ODP 文件下载。
- 下载后优先使用演讲标题命名文件，例如：
  `Semi Joins in Postgres.pdf`。
- 自动清理文件名中的非法字符，处理重名、超长标题和特殊符号。
- 基于 session/页面简介优先判断主题分类，例如优化器、执行器、流复制、备份恢复。
- 记录暂未发布资料的 session，并在后续 `tick` 任务中继续检查。
- 按主题目录保存资料，会议信息保留在 SQLite 和报告中用于溯源。
- 生成本地 HTML/CSV 报告。
- 使用 SQLite 保存抓取状态，重复运行时会跳过已存在文件。
- 如果本地 `archive/` 被手动删除，会在再次抓取时重新下载缺失文件。

### 当前支持的来源

- `pgevents.ca`，例如 PGConf.dev。
- Indico 会议系统，例如 CERN PGDay。
- WordPress 会议官网。
- 通用会议网站扫描器，用于尝试识别独立站点中的资料链接。

不同 PostgreSQL 会议使用的网站系统差异很大，本项目采用“逐步补 adapter”的
方式：遇到新的会议平台，就为它补一个小而稳定的抓取逻辑。

### 快速开始

```bash
python3 pgppt.py init
python3 pgppt.py scan-official
python3 pgppt.py list events
python3 pgppt.py download-event "CERN PGDay 2026"
python3 pgppt.py report
```

常用目录：

- `archive/by_topic/optimizer/`：优化器相关资料。
- `archive/by_topic/executor/`：执行器相关资料。
- `archive/by_topic/streaming-replication/`：流复制相关资料。
- `archive/by_topic/backup-recovery/`：备份恢复相关资料。
- `archive/by_topic/uncategorized/`：暂未命中分类的资料。
- `data/pgppt.sqlite`：本地 SQLite 状态库。
- `reports/index.html`：本地 HTML 报告。
- `reports/index.csv`：CSV 报告。

这些运行产物默认不会提交到 git。

### 常用命令

```bash
# 初始化本地目录和数据库
python3 pgppt.py init

# 从 PostgreSQL 官方活动页发现会议
python3 pgppt.py scan-official

# 查看已发现会议
python3 pgppt.py list events

# 通过会议名称下载资料
python3 pgppt.py download-event "PGConf.dev 2026"

# 下载单个 PDF/PPT/PPTX/ODP
python3 pgppt.py ingest <asset-url>

# 扫描单个页面中的资料链接
python3 pgppt.py ingest <page-url> --event "Event Name" --title "Session Title"

# 检查到期的 session，适合定期运行
python3 pgppt.py tick

# 重新生成报告
python3 pgppt.py report

# 将旧版本下载到其他目录的资料迁移到主题目录
python3 pgppt.py organize-archive

# 查看本地资料和 session 状态
python3 pgppt.py list assets
python3 pgppt.py list sessions
```

### 主题分类策略

下载资料会进入 `archive/by_topic/<category>/`，而不是按会议分散保存。
分类优先使用 session 页面或会议系统中的简介/摘要，其次才使用标题作为辅助信号。
会议名称不会参与关键词打分，避免把整场会议误判成某一个主题。

当前内置主题包括：

- `optimizer`：优化器、planner、cost、statistics、join order 等。
- `executor`：执行器、query execution、parallel query、aggregate、sort 等。
- `streaming-replication`：流复制、physical replication、standby、WAL sender/receiver 等。
- `logical-replication`：逻辑复制、logical decoding、publication/subscription 等。
- `backup-recovery`：备份、恢复、PITR、pgBackRest、Barman 等。
- `high-availability`：高可用、failover、Patroni、repmgr、disaster recovery 等。
- 以及 `performance`、`operations`、`storage`、`internals`、`extensions-ecosystem`、`cloud-native`、`security`。

未命中分类的资料会放入 `archive/by_topic/uncategorized/`，后续可以通过完善
`config/categories.json` 再运行：

```bash
python3 pgppt.py classify
python3 pgppt.py organize-archive
```

### 文件命名策略

下载文件会优先使用 session 或页面标题，而不是 URL 中的缩写文件名。

例如：

```text
Semi Joins in Postgres.pdf
PostgreSQL Backup Patterns - Demo Notes.pdf
```

命名时会自动处理：

- Windows/macOS/Linux 不适合出现在文件名中的字符。
- `/ \ : * ? " < > |` 等特殊字符。
- 控制字符、多余空格、尾部点号。
- 过长标题。
- 同名文件，自动追加 `-2`、`-3`。

### 延迟发布资料的处理

很多会议不会在活动当天立即发布 PPT。本工具会为每个 session 维护状态：

- `missing`：暂未发现资料。
- `found`：已发现资料链接。
- `downloaded`：已下载。
- `failed`：下载失败。
- `login_required`：需要登录或没有权限，暂时跳过。

可以定期运行：

```bash
python3 pgppt.py tick
python3 pgppt.py report
```

这样之前缺失的资料会被继续检查，等会议官网补上 PDF/PPT 后再下载。

### 设计原则

- 只下载公开可访问的会议资料。
- 不绕过登录、权限或付费限制。
- 对会议网站保持温和访问，避免高频请求。
- 本地归档、数据库和报告不进入版本控制。
- 优先使用小而明确的 adapter，而不是脆弱的大而全爬虫。

### 路线图

- 支持更多 PostgreSQL 会议平台。
- 增强 PGConf.EU/PostgreSQL Europe 等站点 adapter。
- 改进主题分类质量。
- 增加可选的定时任务安装说明。
- 增加更完整的测试覆盖。

## English

### Overview

`pg-slide-harvester` helps you build a local archive of public PostgreSQL
conference slides. It discovers PostgreSQL events, follows conference pages,
downloads available presentation assets, and keeps track of sessions whose
materials may be published later.

The project is intentionally small and practical. It is designed to save time,
not to become a heavy content platform.

### Features

- Discover PostgreSQL events from `postgresql.org`.
- Download public PDF, PPT, PPTX, and ODP files.
- Download by event name after discovery.
- Name downloaded files with readable talk titles.
- Sanitize filenames for common cross-platform filesystem constraints.
- Re-check sessions whose slides are not available yet.
- Organize files by topic, while keeping event metadata in SQLite and reports.
- Generate local HTML and CSV reports.
- Store crawl state in SQLite.
- Re-download missing local files if the archive directory was removed.

### Supported Sources

Current adapters include:

- `pgevents.ca`, such as PGConf.dev.
- Indico-based events, such as CERN PGDay.
- WordPress-based conference websites.
- A generic fallback crawler for independent conference websites.

Conference websites vary a lot, so the project grows adapter by adapter as new
platforms are encountered.

### Quick Start

```bash
python3 pgppt.py init
python3 pgppt.py scan-official
python3 pgppt.py list events
python3 pgppt.py download-event "CERN PGDay 2026"
python3 pgppt.py report
```

Generated local artifacts:

- `archive/by_topic/optimizer/`: optimizer-related materials.
- `archive/by_topic/executor/`: executor-related materials.
- `archive/by_topic/streaming-replication/`: streaming replication materials.
- `archive/by_topic/backup-recovery/`: backup and recovery materials.
- `archive/by_topic/uncategorized/`: files without a confident category yet.
- `data/pgppt.sqlite`: local SQLite state.
- `reports/index.html`: local HTML report.
- `reports/index.csv`: CSV report.

These artifacts are intentionally ignored by git.

### Commands

```bash
# Initialize local folders and database.
python3 pgppt.py init

# Discover events from the official PostgreSQL event pages.
python3 pgppt.py scan-official

# List discovered events.
python3 pgppt.py list events

# Download slides by event name.
python3 pgppt.py download-event "PGConf.dev 2026"

# Download a single PDF/PPT/PPTX/ODP asset.
python3 pgppt.py ingest <asset-url>

# Scan a single page for slide links.
python3 pgppt.py ingest <page-url> --event "Event Name" --title "Session Title"

# Check sessions whose next check time is due.
python3 pgppt.py tick

# Regenerate reports.
python3 pgppt.py report

# Move older downloaded files into topic directories.
python3 pgppt.py organize-archive

# Inspect local state.
python3 pgppt.py list assets
python3 pgppt.py list sessions
```

### Topic Classification

Downloaded files are stored under `archive/by_topic/<category>/`, not grouped
by event. Classification primarily uses the session/page abstract, with the
title as a secondary signal. Event names are not used for keyword scoring.

Built-in categories include:

- `optimizer`: planner, cost model, statistics, join order, and related topics.
- `executor`: executor, query execution, parallel query, aggregate, sort, and related topics.
- `streaming-replication`: physical replication, standby, WAL sender/receiver, and replication slots.
- `logical-replication`: logical decoding, publication/subscription, and CDC.
- `backup-recovery`: backup, restore, PITR, pgBackRest, Barman, and base backup.
- `high-availability`: failover, Patroni, repmgr, disaster recovery, and availability.
- `performance`, `operations`, `storage`, `internals`, `extensions-ecosystem`, `cloud-native`, and `security`.

Files without a confident match are stored under `archive/by_topic/uncategorized/`.
After improving `config/categories.json`, run:

```bash
python3 pgppt.py classify
python3 pgppt.py organize-archive
```

### Filename Policy

Downloaded files are named from the talk/session title whenever possible,
instead of using short or cryptic URL filenames.

Examples:

```text
Semi Joins in Postgres.pdf
PostgreSQL Backup Patterns - Demo Notes.pdf
```

The filename sanitizer handles:

- Filesystem-sensitive characters.
- Characters such as `/ \ : * ? " < > |`.
- Control characters and repeated whitespace.
- Trailing dots and spaces.
- Very long titles.
- Duplicate filenames, by appending `-2`, `-3`, and so on.

### Delayed Slide Publication

Many conferences publish slides days or weeks after the event. Each session is
tracked with a status:

- `missing`: no public material found yet.
- `found`: material link found.
- `downloaded`: file downloaded.
- `failed`: download failed.
- `login_required`: login or permission required.

For recurring usage, run:

```bash
python3 pgppt.py tick
python3 pgppt.py report
```

### Principles

- Download only publicly available materials.
- Do not bypass authentication, permissions, or paywalls.
- Be polite to conference websites.
- Keep local archives, reports, and SQLite state out of version control.
- Prefer small dedicated adapters over one fragile universal crawler.

### Roadmap

- Add more PostgreSQL conference platform adapters.
- Improve PGConf.EU/PostgreSQL Europe support.
- Improve topic classification.
- Add optional recurring job setup instructions.
- Expand automated tests.

## License

MIT
