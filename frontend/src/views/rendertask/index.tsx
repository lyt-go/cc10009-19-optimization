import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/rendertask'
const columns = [
  "作业编号",
  "目标URL",
  "浏览器版本",
  "视口宽度",
  "视口高度",
  "等待策略",
  "脚本注入",
  "超时阈值秒"
]
const actions = [
  "提交排队",
  "启动渲染",
  "终止渲染",
  "重新排队",
  "提交渲染结果"
]
const statuses = [
  "草稿",
  "排队中",
  "渲染中",
  "已完成",
  "已超时",
  "已失败",
  "已归档"
]
const stats = [
  {
    "label": "渲染作业总数",
    "value": 0
  },
  {
    "label": "渲染中作业",
    "value": 0
  },
  {
    "label": "已完成作业",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type AnyRecord = Record<string, unknown>

interface QueueItem {
  作业编号: string
  status: string
  当前尝试版本: number
  重排次数: number
  已等待秒: number
  生效阈值秒: number
  剩余秒: number
  等待策略: string | null
  是否到期: boolean
}

interface ReapItem {
  作业编号: string
  尝试版本: number
  回收类型: string
  已等待秒: number
  生效阈值秒: number
  是否重排: boolean
  结果版本: number
  结果生效: boolean
}
interface ReapReport {
  回收时间: string
  已回收: number
  已回收条目?: ReapItem[]
  items: ReapItem[]
  队列: { 排队中: number; 渲染中: number; 最长等待秒: number }
  节点容量: { 节点标识: string; 剩余槽位: number; 已占用槽位: number; 可调度: boolean }[]
  当前尝试校验: { ok: boolean; checked: number; issues: AnyRecord[] }
}
interface ResultItem {
  结果版本: number
  尝试版本: number
  结果状态: string
  来源: string
  生效: boolean
  失效原因: string
  耗时秒: number
  输入核对: { ok: boolean; checked: number; mismatched: string[] }
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export default function RendertaskView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [capacity, setCapacity] = useState<ReapReport['节点容量']>([])
  const [reapReport, setReapReport] = useState<ReapReport | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [selectedResults, setSelectedResults] = useState<ResultItem[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('渲染作业列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务列表读取失败')
    }
  }, [filters])

  const reloadQueue = useCallback(async () => {
    try {
      const [queueResponse, capacityResponse] = await Promise.all([
        request(`${ENDPOINT}/queue`),
        request(`${ENDPOINT}/capacity`),
      ])
      if (queueResponse.ok) {
        const payload = await queueResponse.json()
        setQueue(asArray<QueueItem>(payload.items))
      }
      if (capacityResponse.ok) {
        const payload = await capacityResponse.json()
        setCapacity(asArray<ReapReport['节点容量'][number]>(payload.items))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '队列状态读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadQueue()
  }, [reload, reloadQueue])

  async function loadResults(id: number) {
    try {
      const response = await request(`${ENDPOINT}/${id}/results`)
      if (!response.ok) {
        throw new Error('渲染结果读取失败')
      }
      const payload = await response.json()
      setSelectedId(id)
      setSelectedResults(asArray<ResultItem>(payload.items))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染结果读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const values: Record<string, unknown> = { action }
      if (action === "提交渲染结果") {
        values["尝试版本"] = row["当前尝试版本"]
        values["幂等键"] = `${row["作业编号"]}-v${row["当前尝试版本"]}`
        values["结果内容"] = { 页面标题: `${row["作业编号"]} 渲染完成`, 内容长度: 2048 }
      }
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: '动作返回体无法解析' }))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '渲染任务动作未生效，请稍后重试')
      }
      await Promise.all([reload(), reloadQueue()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务操作失败')
    }
  }

  async function reapTimeouts() {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/reap`, { method: 'POST' })
      if (!response.ok) {
        throw new Error('超时回收执行失败')
      }
      const payload = (await response.json()) as ReapReport
      setReapReport(payload)
      await Promise.all([reload(), reloadQueue()])
      if (selectedId !== null) {
        await loadResults(selectedId)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '超时回收失败')
    }
  }

  const liveStats = useMemo(() => {
    const rendering = rows.filter((row) => row.status === '渲染中').length
    const done = rows.filter((row) => row.status === '已完成').length
    return stats.map((item, index) => ({
      ...item,
      value: index === 0 ? total : index === 1 ? rendering : done,
    }))
  }, [rows, total])

  return (
    <section className="page" data-module="rendertask">
      <header className="page-head">
        <div>
          <h2>渲染任务管理</h2>
          <p className="page-desc">管理需要浏览器渲染的页面抓取作业，配置渲染环境和等待策略。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('渲染作业登记入口尚未接入审批流')}>
            登记渲染作业
          </button>
          <button className="btn" type="button" onClick={() => void reapTimeouts()}>
            执行超时回收
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出渲染任务清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {liveStats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
        <article className="stat-card">
          <span className="stat-label">排队 / 渲染中</span>
          <strong className="stat-value">{queue.filter((item) => item.status === '排队中').length} / {queue.filter((item) => item.status === '渲染中').length}</strong>
        </article>
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
        <label className="filter-item">
          <span>状态</span>
          <select value={filters.status ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}>
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      {reapReport && (
        <section className="panel">
          <h3 className="panel-title">超时回收结果</h3>
          <p className="panel-desc">
            回收时间 {reapReport.回收时间}，回收 {reapReport.已回收} 个尝试；
            队列剩余 排队中 {reapReport.队列.排队中} / 渲染中 {reapReport.队列.渲染中}，最长等待 {reapReport.队列.最长等待秒}s；
            当前尝试校验{reapReport.当前尝试校验.ok ? '通过' : `发现 ${reapReport.当前尝试校验.issues.length} 处问题`}。
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>作业编号</th><th>尝试版本</th><th>回收类型</th><th>已等待秒</th>
                <th>生效阈值秒</th><th>是否重排</th><th>结果版本</th><th>结果生效</th>
              </tr>
            </thead>
            <tbody>
              {reapReport.items.length === 0 && <tr><td className="empty-state" colSpan={8}>没有到期的渲染尝试</td></tr>}
              {reapReport.items.map((item) => (
                <tr key={`${item.作业编号}-${item.尝试版本}`}>
                  <td>{item.作业编号}</td>
                  <td>v{item.尝试版本}</td>
                  <td>{item.回收类型}</td>
                  <td>{item.已等待秒}</td>
                  <td>{item.生效阈值秒}</td>
                  <td>{item.是否重排 ? '是' : '否'}</td>
                  <td>{item.结果版本}</td>
                  <td>{item.结果生效 ? '生效' : '归档'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>当前尝试</th>
            <th>重排次数</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>v{row["当前尝试版本"] ?? 0}</td>
              <td>{row["重排次数"] ?? 0}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void loadResults(Number(row.id))}>
                  查看结果
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无渲染任务数据，可先登记渲染作业</td>
            </tr>
          )}
        </tbody>
      </table>

      {selectedId !== null && (
        <section className="panel">
          <h3 className="panel-title">渲染结果（作业 #{selectedId}）</h3>
          <p className="panel-desc">结果按尝试版本与结果版本区分；生效结果与输入快照逐项对照。</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>结果版本</th><th>尝试版本</th><th>结果状态</th><th>来源</th>
                <th>耗时秒</th><th>是否生效</th><th>失效原因</th><th>输入核对</th>
              </tr>
            </thead>
            <tbody>
              {selectedResults.length === 0 && <tr><td className="empty-state" colSpan={8}>该作业还没有渲染结果</td></tr>}
              {selectedResults.map((item) => (
                <tr key={item.结果版本}>
                  <td>{item.结果版本}</td>
                  <td>v{item.尝试版本}</td>
                  <td>{item.结果状态}</td>
                  <td>{item.来源}</td>
                  <td>{item.耗时秒}</td>
                  <td>{item.生效 ? '生效' : '—'}</td>
                  <td>{item.失效原因 || '—'}</td>
                  <td>
                    {item.输入核对.ok
                      ? `一致（${item.输入核对.checked} 项）`
                      : `不一致：${item.输入核对.mismatched.join('、')}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="panel">
        <h3 className="panel-title">队列等待与节点容量</h3>
        <p className="panel-desc">{capacity.length} 个节点，{capacity.filter((item) => item.可调度).length} 个可调度。</p>
        <table className="data-table">
          <thead>
            <tr><th>作业编号</th><th>状态</th><th>尝试版本</th><th>重排次数</th><th>已等待秒</th><th>生效阈值秒</th><th>剩余秒</th><th>等待策略</th></tr>
          </thead>
          <tbody>
            {queue.length === 0 && <tr><td className="empty-state" colSpan={8}>队列为空</td></tr>}
            {queue.map((item) => (
              <tr key={item.作业编号}>
                <td>{item.作业编号}</td>
                <td>{item.status}</td>
                <td>v{item.当前尝试版本}</td>
                <td>{item.重排次数}</td>
                <td>{item.已等待秒}</td>
                <td>{item.生效阈值秒}</td>
                <td>{item.剩余秒}</td>
                <td>{item.等待策略 ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table className="data-table">
          <thead>
            <tr><th>节点标识</th><th>已占用槽位</th><th>剩余槽位</th><th>可调度</th></tr>
          </thead>
          <tbody>
            {capacity.map((node) => (
              <tr key={node.节点标识}>
                <td>{node.节点标识}</td>
                <td>{node.已占用槽位}</td>
                <td>{node.剩余槽位}</td>
                <td>{node.可调度 ? '是' : '否'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条渲染任务记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
