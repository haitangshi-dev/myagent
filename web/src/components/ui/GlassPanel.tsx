import type { ReactNode } from 'react'

export function GlassPanel({
  className = '',
  children,
  strong = false,
}: {
  className?: string
  children: ReactNode
  strong?: boolean
}) {
  return (
    <div className={`${strong ? 'glass-strong' : 'glass'} rounded-xl2 ${className}`}>
      {children}
    </div>
  )
}
