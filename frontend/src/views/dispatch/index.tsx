import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dispatch'
const columns = [
  "策略编号",
  "目标域名",
  "分配算法",
  "权重因子",
  "节点亲和性",
  "负载阈值",
  "再平衡间隔",
  "溢出处理"
]
const actions = [
  "提交策略验证",
  "启用策略",
  "停用策略"
]
const statuses = [
  "草稿",
  "策略验证中",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "分配策略总数",
    "value": 0
  },
  {
    "label": "已启用策略",
    "value": 0
  },
  {
    "label": "验证中策略",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

interface PlanCandidate {
  作业编号: string
  当前尝试版本: number
  已等待秒: number
  重排次数: number
  尝试次数: number
  派发得分: number
  剩余秒: number
  排队公平性: string
}
interface PlanNode {
  节点标识: string
  剩余槽位: number
  心跳秒: number | null
  可调度: boolean
  判定原因: string
}
interface Plan {
  decision: string
  candidates: PlanCandidate[]
  nodes: PlanNode[]
  可派发: boolean
  建议作业: string | null
  建议节点: string | null
}
interface SyncResult {
  message: string
  manifest: { 清单版本: number; 已消费结果版本: number; 可调度节点数: number; 剩余槽位合计: number }
  record: { 清单版本: number; 消费结果条数: number; 消费释放条数: number; 生效结果版本: number[] } | null
  results: { 结果版本: number; 作业编号: string; 尝试版本: number; 结果状态: string; 生效: boolean }[]
}
interface DispatchRecord {
  id: number
  清单版本: number
  已消费结果版本: number
  消费结果条数: number
  消费释放条数: number
  剩余槽位合计: number
  同步时间: string
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export default function DispatchView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [plan, setPlan] = useState<Plan | null>(null)
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null)
  const [records, setRecords] = useState<DispatchRecord[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reloadPlan = useCallback(async () => {
    try {
      const [planResponse, recordsResponse] = await Promise.all([
        request(`${ENDPOINT}/plan`),
        request(`${ENDPOINT}/records?limit=10`),
      ])
      if (planResponse.ok) {
        setPlan((await planResponse.json()) as Plan)
      }
      if (recordsResponse.ok) {
        const payload = await recordsResponse.json()
        setRecords(asArray<DispatchRecord>(payload.items))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '派发解释读取失败')
    }
  }, [])

  const syncResults = useCallback(async () => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/sync`, { method: 'POST' })
      if (!response.ok) {
        throw new Error('调度侧同步失败')
      }
      setSyncResult((await response.json()) as SyncResult)
      await reloadPlan()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调度侧同步失败')
    }
  }, [reloadPlan])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('分配策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '请求分配列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadPlan()
  }, [reloadPlan])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: '动作返回体无法解析' }))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '请求分配动作未生效，请稍后重试')
      }
      await Promise.all([reload(), reloadPlan()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '请求分配操作失败')
    }
  }

  return (
    <section className="page" data-module="dispatch">
      <header className="page-head">
        <div>
          <h2>请求分配管理</h2>
          <p className="page-desc">管理采集请求的分配策略，按域名粒度和节点负载进行动态调度。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分配策略登记入口尚未接入审批流')}>
            登记分配策略
          </button>
          <button className="btn" type="button" onClick={() => void syncResults()}>
            同步渲染结果与容量释放
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出请求分配清单
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
        <h3 className="panel-title">下一次派发</h3>
        <p className="panel-desc">{plan ? plan.decision : '派发解释尚未读取。'}</p>
        <table className="data-table">
          <thead>
            <tr><th>作业编号</th><th>尝试版本</th><th>已等待秒</th><th>重排次数</th><th>尝试次数</th><th>派发得分</th><th>剩余秒</th><th>排队公平性</th></tr>
          </thead>
          <tbody>
            {(!plan || plan.candidates.length === 0) && <tr><td className="empty-state" colSpan={8}>当前没有排队中的渲染作业</td></tr>}
            {plan?.candidates.map((item) => (
              <tr key={item.作业编号}>
                <td>{item.作业编号}</td>
                <td>v{item.当前尝试版本}</td>
                <td>{item.已等待秒}</td>
                <td>{item.重排次数}</td>
                <td>{item.尝试次数}</td>
                <td>{item.派发得分}</td>
                <td>{item.剩余秒}</td>
                <td>{item.排队公平性}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table className="data-table">
          <thead>
            <tr><th>节点标识</th><th>剩余槽位</th><th>心跳秒</th><th>可调度</th><th>判定原因</th></tr>
          </thead>
          <tbody>
            {plan?.nodes.map((node) => (
              <tr key={node.节点标识}>
                <td>{node.节点标识}</td>
                <td>{node.剩余槽位}</td>
                <td>{node.心跳秒 ?? '—'}</td>
                <td>{node.可调度 ? '是' : '否'}</td>
                <td>{node.判定原因}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3 className="panel-title">调度侧结果消费</h3>
        <p className="panel-desc">
          {syncResult
            ? `${syncResult.message}；清单版本 ${syncResult.manifest.清单版本}，已消费结果版本 ${syncResult.manifest.已消费结果版本}。`
            : '点击“同步渲染结果与容量释放”后，新的渲染结果版本会写进调度清单并落到业务记录。'}
        </p>
        {syncResult?.record && (
          <p className="panel-desc">
            本次消费结果 {syncResult.record.消费结果条数} 条、容量释放 {syncResult.record.消费释放条数} 条，
            生效结果版本 {syncResult.record.生效结果版本.length > 0 ? syncResult.record.生效结果版本.join('、') : '无'}。
          </p>
        )}
        <table className="data-table">
          <thead>
            <tr><th>结果版本</th><th>作业编号</th><th>尝试版本</th><th>结果状态</th><th>是否生效</th></tr>
          </thead>
          <tbody>
            {(!syncResult || syncResult.results.length === 0) && <tr><td className="empty-state" colSpan={5}>本次没有新的渲染结果</td></tr>}
            {syncResult?.results.map((item) => (
              <tr key={item.结果版本}>
                <td>{item.结果版本}</td>
                <td>{item.作业编号}</td>
                <td>v{item.尝试版本}</td>
                <td>{item.结果状态}</td>
                <td>{item.生效 ? '生效' : '归档'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table className="data-table">
          <thead>
            <tr><th>清单版本</th><th>已消费结果版本</th><th>消费结果条数</th><th>消费释放条数</th><th>剩余槽位合计</th><th>同步时间</th></tr>
          </thead>
          <tbody>
            {records.length === 0 && <tr><td className="empty-state" colSpan={6}>还没有调度侧同步记录</td></tr>}
            {records.map((record) => (
              <tr key={record.id}>
                <td>{record.清单版本}</td>
                <td>{record.已消费结果版本}</td>
                <td>{record.消费结果条数}</td>
                <td>{record.消费释放条数}</td>
                <td>{record.剩余槽位合计}</td>
                <td>{record.同步时间}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无请求分配数据，可先登记分配策略</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条请求分配记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
