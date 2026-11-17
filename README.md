# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 渲染超时回收与结果新鲜度

渲染任务不是一次性记录，而是「作业 → 尝试 → 结果」三层账本，状态单向流转：
草稿 → 排队中 → 渲染中 → 已完成 / 已超时 / 已失败 → 已归档。

### 数据口径

| 账本 | 位置 | 作用 |
| --- | --- | --- |
| 渲染尝试 | `render_attempt` | 每次排队/渲染一个尝试版本，记录节点、开始/结束时间、是否已释放容量 |
| 渲染结果 | `render_result` | 每次结算一份结果，带尝试版本与全局递增的结果版本 |
| 容量释放 | `capacity_release` | 每个尝试最多一条，记录要归还的槽位与消费状态 |
| 调度清单 | `dispatch_manifest` | 已消费结果版本、释放水位、可调度节点与剩余槽位 |
| 调度记录 | `dispatch_record` | 每次同步落的业务记录，可按清单版本回溯 |

### 结算规则

- **只有一个赢家**：完成回调、超时回收、节点离线并发时，靠 `store.lock` 与「尝试已有结果」双重判断，
  同一尝试只会写一份结果，后来者拿到既有结果。
- **结果新鲜度**：结算是拿尝试版本与作业的 `当前尝试版本` 比较。旧页面的回调与新一轮渲染同时完成时，
  两份结果指向不同尝试，旧的那份标为不生效并记录失效原因，不会覆盖新结果。
- **已完成不被超时改写**：超时回收先看作业终态，再只看当前进行中且尚未结算的尝试。
- **容量只释放一次**：释放记录按尝试去重；重复终止只累加 `重复终止次数`，不再归还槽位。
- **幂等**：完成回调支持 `幂等键`，同一尝试内重复提交沿用第一次的结果版本。
- **重排有上限**：`重排次数` 与 `最大重排次数`（默认 2）共同约束，超限后直接落到终态。

### 生效超时

`生效阈值秒 = 超时阈值秒 + 等待策略宽限`。宽限按等待策略取值：
`networkidle0` +20s、`networkidle` +15s、`domcontentloaded` +5s、`load` +0s。

### 派发解释

`GET /api/dispatch/plan` 给出下一次派发的完整理由：

`派发得分 = 已等待秒 − 重排次数 × 60 − (尝试次数 − 1) × 20`

得分高的优先；节点侧必须同时满足「在线 + 心跳新鲜（≤120s）+ 剩余槽位 > 0」才算可调度。

### 相关接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/rendertask/reap` | 执行超时回收，返回回收明细、队列快照、节点容量与当前尝试校验 |
| `GET /api/rendertask/queue` | 排队/渲染中尝试的等待时长与到期判断 |
| `GET /api/rendertask/capacity` | 节点槽位与可调度判定 |
| `GET /api/rendertask/verify` | 校验进行中作业的当前尝试是否唯一且最新 |
| `GET /api/rendertask/{id}/results` | 渲染结果清单，逐项对照输入快照 |
| `GET /api/rendertask/{id}` | 作业视角：尝试链、结果链、当前生效结果 |
| `POST /api/rendertask/{id}/actions` | 提交排队 / 启动渲染 / 终止渲染 / 重新排队 / 提交渲染结果 |
| `GET /api/worker/schedulable` | 可调度节点结果（含剩余槽位） |
| `POST /api/worker/capacity/consume` | 消费容量释放并回写剩余槽位 |
| `GET /api/dispatch/plan` | 下一次派发的解释 |
| `POST /api/dispatch/sync` | 消费新结果与容量释放，写调度清单与业务记录 |
| `GET /api/dispatch/records` | 调度侧同步记录 |

## 测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

覆盖：超时回收、已完成不被改写、旧回调归档、并发三选一、重复终止、幂等提交、
结果与输入逐项核对、重排计数、调度侧消费与清单版本。测试需要 `httpx`（FastAPI TestClient 依赖）。

## 约定


- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
