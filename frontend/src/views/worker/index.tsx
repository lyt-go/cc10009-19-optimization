import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/worker'
const columns = [
  "节点编号",
  "主机地址",
  "并发槽位数",
  "地域标签",
  "CPU使用率",
  "内存可用MB",
  "上次心跳时间",
  "节点状态标签"
]
const actions = [
  "提交注册",
  "标记在线",
  "标记离线"
]
const capacityColumns = ["已占用槽位", "剩余槽位", "可调度"]
const statuses = [
  "草稿",
  "注册中",
  "在线",
  "离线"
]
const stats = [
  {
    "label": "工作节点总数",
    "value": 0
  },
  {
    "label": "在线节点数",
    "value": 0
  },
  {
    "label": "离线节点数",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

interface SchedulableNode {
  节点标识: string
  并发槽位数: number
  已占用槽位: number
  剩余槽位: number
  心跳秒: number | null
  可调度: boolean
  判定原因: string
}

interface Manifest {
  清单版本: number
  已消费结果版本: number
  已消费释放编号: number
  消费结果条数: number
  消费释放条数: number
  同步时间: string
  可调度节点数: number
  剩余槽位合计: number
}

interface ConsumeReport {
  message: string
  消费条数: number
  跳过条数: number
  items: { 释放编号: number; 作业编号: string; 尝试版本: number; 节点标识: string; 剩余槽位: number }[]
  skipped: { 释放编号: number; 作业编号: string; 原因: string }[]
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export default function WorkerView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [nodes, setNodes] = useState<SchedulableNode[]>([])
  const [manifest, setManifest] = useState<Manifest | null>(null)
  const [consumeReport, setConsumeReport] = useState<ConsumeReport | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reloadDispatch = useCallback(async () => {
    try {
      const [nodesResponse, manifestResponse] = await Promise.all([
        request(`${ENDPOINT}/schedulable`),
        request(`${ENDPOINT}/manifest`),
      ])
      if (nodesResponse.ok) {
        const payload = await nodesResponse.json()
        setNodes(asArray<SchedulableNode>(payload.items))
      }
      if (manifestResponse.ok) {
        setManifest((await manifestResponse.json()) as Manifest)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调度侧容量读取失败')
    }
  }, [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('工作节点列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集节点列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const consumeReleases = useCallback(async () => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/capacity/consume`, { method: 'POST' })
      if (!response.ok) {
        throw new Error('容量释放消费失败')
      }
      setConsumeReport((await response.json()) as ConsumeReport)
      await Promise.all([reload(), reloadDispatch()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '容量释放消费失败')
    }
  }, [reload, reloadDispatch])

  useEffect(() => {
    void reloadDispatch()
  }, [reloadDispatch])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: '动作返回体无法解析' }))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '采集节点动作未生效，请稍后重试')
      }
      await Promise.all([reload(), reloadDispatch()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集节点操作失败')
    }
  }

  return (
    <section className="page" data-module="worker">
      <header className="page-head">
        <div>
          <h2>采集节点管理</h2>
          <p className="page-desc">管理分布式采集集群的工作节点，跟踪资源使用和心跳健康状态。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('工作节点登记入口尚未接入审批流')}>
            登记工作节点
          </button>
          <button className="btn" type="button" onClick={() => void consumeReleases()}>
            消费容量释放
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出采集节点清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <section className="panel">
        <h3 className="panel-title">可调度节点结果</h3>
        <p className="panel-desc">
          {manifest
            ? `调度侧清单版本 ${manifest.清单版本}，已消费结果版本 ${manifest.已消费结果版本}，已消费释放编号 ${manifest.已消费释放编号}，`
              + `可调度节点 ${manifest.可调度节点数} 个、剩余槽位合计 ${manifest.剩余槽位合计}。`
            : '调度侧清单尚未读取。'}
        </p>
        <table className="data-table">
          <thead>
            <tr><th>节点标识</th><th>并发槽位数</th><th>已占用槽位</th><th>剩余槽位</th><th>心跳秒</th><th>可调度</th><th>判定原因</th></tr>
          </thead>
          <tbody>
            {nodes.length === 0 && <tr><td className="empty-state" colSpan={7}>暂无节点容量数据</td></tr>}
            {nodes.map((node) => (
              <tr key={node.节点标识}>
                <td>{node.节点标识}</td>
                <td>{node.并发槽位数}</td>
                <td>{node.已占用槽位}</td>
                <td>{node.剩余槽位}</td>
                <td>{node.心跳秒 ?? '—'}</td>
                <td>{node.可调度 ? '是' : '否'}</td>
                <td>{node.判定原因}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {consumeReport && (
          <>
            <p className="panel-desc">
              {consumeReport.message}，跳过 {consumeReport.跳过条数} 条
              {consumeReport.skipped.length > 0 && `（${consumeReport.skipped.map((item) => `#${item.释放编号} ${item.原因}`).join('；')}）`}
            </p>
            <table className="data-table">
              <thead>
                <tr><th>释放编号</th><th>作业编号</th><th>尝试版本</th><th>节点标识</th><th>归还后剩余槽位</th></tr>
              </thead>
              <tbody>
                {consumeReport.items.length === 0 && <tr><td className="empty-state" colSpan={5}>没有待消费的容量释放</td></tr>}
                {consumeReport.items.map((item) => (
                  <tr key={item.释放编号}>
                    <td>{item.释放编号}</td>
                    <td>{item.作业编号}</td>
                    <td>v{item.尝试版本}</td>
                    <td>{item.节点标识}</td>
                    <td>{item.剩余槽位}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            {capacityColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              {capacityColumns.map((column) => (
                <td key={column}>
                  {column === "可调度" ? (row[column] ? '是' : '否') : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + capacityColumns.length + 1}>暂无采集节点数据，可先登记工作节点</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条采集节点记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
