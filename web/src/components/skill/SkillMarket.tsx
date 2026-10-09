import { useCallback, useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  Loader2,
  Search,
  Sparkles,
} from 'lucide-react'
import {
  getSkillCategories,
  installSkill,
  searchSkillMarket,
  type SkillCategory,
  type SkillMarketItem,
} from '../../lib/api'
import { useUIStore } from '../../store/useUIStore'

const PAGE_SIZE = 12

export function SkillMarket() {
  const [keyword, setKeyword] = useState('')
  const [category, setCategory] = useState('')
  const [categories, setCategories] = useState<SkillCategory[]>([])
  const [items, setItems] = useState<SkillMarketItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [installing, setInstalling] = useState<string | null>(null)

  const installed = useUIStore((s) => s.skillInstalled)
  const loadInstalled = useUIStore((s) => s.loadInstalledSkills)

  const doSearch = useCallback(
    async (kw: string, cat: string, pg: number) => {
      setLoading(true)
      setError(null)
      try {
        const res = await searchSkillMarket(cat ? `${kw}` : kw, pg, PAGE_SIZE)
        setItems(res.skills || [])
        setTotal(res.total || 0)
        setPage(res.page || pg)
      } catch (e: any) {
        setError(e?.message || '检索失败')
        setItems([])
        setTotal(0)
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  // 初次加载：分类 + 已安装清单 + 默认检索
  useEffect(() => {
    getSkillCategories()
      .then(setCategories)
      .catch(() => setCategories([]))
    loadInstalled()
    doSearch('', '', 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const onSearch = (e: React.FormEvent) => {
    e.preventDefault()
    setPage(1)
    doSearch(keyword.trim(), category, 1)
  }

  const onCategory = (cat: string) => {
    setCategory(cat)
    setPage(1)
    doSearch(keyword.trim(), cat, 1)
  }

  const onInstall = async (item: SkillMarketItem) => {
    const slug = item.slug || ''
    if (!slug || installing) return
    setInstalling(slug)
    try {
      await installSkill({
        slug,
        name: item.name,
        description: item.description,
        description_zh: item.description_zh,
        category: item.category,
        source: item.source,
        homepage: item.homepage,
        version: item.version,
        icon_url: item.iconUrl,
      })
      await loadInstalled()
    } catch (e: any) {
      setError(e?.message || '安装失败')
    } finally {
      setInstalling(null)
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* 搜索 + 分类 */}
      <div className="space-y-2 border-b border-line p-3">
        <form onSubmit={onSearch} className="flex gap-2">
          <div className="relative flex-1">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint"
            />
            <input
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="搜索技能：pdf / 翻译 / 爬虫…"
              className="field !py-2 !pl-8"
            />
          </div>
          <button type="submit" className="btn btn-accent !px-3" disabled={loading}>
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            搜索
          </button>
        </form>
        <div className="no-scrollbar flex gap-1.5 overflow-x-auto pb-1">
          <button
            onClick={() => onCategory('')}
            className={`chip shrink-0 !px-2.5 ${
              category === '' ? '!border-skill/40 !text-skill' : ''
            }`}
          >
            全部
          </button>
          {categories.map((c) => (
            <button
              key={c.id}
              onClick={() => onCategory(c.id)}
              className={`chip shrink-0 !px-2.5 ${
                category === c.id ? '!border-skill/40 !text-skill' : ''
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>

      {/* 列表 */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {error && (
          <div className="rounded-xl border border-danger/30 bg-danger/10 px-3 py-2 text-[12.5px] text-danger">
            {error}
          </div>
        )}

        {loading && items.length === 0 ? (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-faint">
            <Loader2 size={16} className="animate-spin" /> 正在检索技能市场…
          </div>
        ) : items.length === 0 ? (
          <div className="py-10 text-center text-sm text-faint">
            没有找到匹配的技能
          </div>
        ) : (
          <>
            <div className="mb-2 text-[11px] text-faint">
              共 {total} 个技能 · 第 {page}/{totalPages} 页
            </div>
            <div className="space-y-2">
              {items.map((item, i) => {
                const slug = item.slug || ''
                const inst = installed[slug]
                const isInstalled = Boolean(inst)
                const realInstalled = inst?.real === true
                const desc = item.description_zh || item.description || ''
                return (
                  <motion.div
                    key={slug + i}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.25, delay: Math.min(i * 0.02, 0.2) }}
                    className="rounded-xl border border-line bg-surface/50 p-3"
                  >
                    <div className="flex items-start gap-2">
                      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-skill/15 text-skill">
                        <Sparkles size={14} />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-[13.5px] font-medium text-ink">
                            {item.name || slug}
                          </span>
                          {item.category && (
                            <span className="chip !px-1.5 !py-0.5 !text-[10px]">
                              {categories.find((c) => c.id === item.category)?.label ||
                                item.category}
                            </span>
                          )}
                        </div>
                        {desc && (
                          <p className="mt-1 line-clamp-2 text-[12.5px] leading-relaxed text-dim">
                            {desc}
                          </p>
                        )}
                        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-faint">
                          {item.installs != null && <span>安装 {item.installs}</span>}
                          {item.source && <span>来源 {item.source}</span>}
                          {item.version && <span>v{item.version}</span>}
                        </div>
                      </div>
                    </div>
                    <div className="mt-2 flex items-center gap-2">
                      <button
                        onClick={() => onInstall(item)}
                        disabled={isInstalled || installing === slug}
                        className={`btn !py-1.5 !text-[12px] ${
                          isInstalled
                            ? 'btn-ghost !cursor-default'
                            : 'btn-accent'
                        }`}
                      >
                        {isInstalled ? (
                          <>
                            <Check size={13} /> 已安装{realInstalled ? '·真实' : '·书签'}
                          </>
                        ) : installing === slug ? (
                          <>
                            <Loader2 size={13} className="animate-spin" /> 安装中
                          </>
                        ) : (
                          <>
                            <Download size={13} /> 安装
                          </>
                        )}
                      </button>
                      {item.homepage && (
                        <a
                          href={item.homepage}
                          target="_blank"
                          rel="noreferrer"
                          className="btn-ghost btn !py-1.5 !text-[12px]"
                        >
                          <ExternalLink size={13} /> 主页
                        </a>
                      )}
                    </div>
                  </motion.div>
                )
              })}
            </div>
          </>
        )}
      </div>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-line px-3 py-2 text-[12px]">
          <button
            onClick={() => {
              const p = Math.max(1, page - 1)
              setPage(p)
              doSearch(keyword.trim(), category, p)
            }}
            disabled={page <= 1 || loading}
            className="btn-ghost btn !py-1.5 !text-[12px]"
          >
            <ChevronLeft size={14} /> 上一页
          </button>
          <span className="text-faint">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => {
              const p = Math.min(totalPages, page + 1)
              setPage(p)
              doSearch(keyword.trim(), category, p)
            }}
            disabled={page >= totalPages || loading}
            className="btn-ghost btn !py-1.5 !text-[12px]"
          >
            下一页 <ChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  )
}
