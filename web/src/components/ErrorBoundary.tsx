import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}
interface State {
  error: Error | null
}

/**
 * 全局错误边界：任意一个组件在渲染期抛异常，不再让整个 React 树被卸载（白屏），
 * 而是显示可恢复的错误卡片 + 重试按钮。这是对「推理时前端突然白屏」类问题的兜底。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: unknown) {
    // 错误已记录到控制台；如需上报可在此接埋点
    console.error('[ErrorBoundary] 渲染异常已捕获：', error, info)
  }

  handleRetry = () => {
    this.setState({ error: null })
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex h-screen w-screen items-center justify-center bg-surface p-6">
          <div className="w-full max-w-md rounded-2xl border border-danger/30 bg-danger/5 p-6">
            <h1 className="text-[15px] font-semibold text-danger">界面渲染出错</h1>
            <p className="mt-2 text-[13px] leading-relaxed text-dim">
              前端某个组件渲染时抛出了异常（已被错误边界接住，未导致整页白屏）。
              可先点「重试」恢复；若反复出现，请把下方信息反馈给开发者。
            </p>
            <pre className="mt-3 max-h-40 overflow-auto rounded-lg bg-surface-strong/60 p-3 font-mono text-[11.5px] leading-relaxed text-faint">
              {String(this.state.error?.stack || this.state.error?.message || this.state.error)}
            </pre>
            <button
              onClick={this.handleRetry}
              className="btn btn-accent mt-4 w-full"
            >
              重试
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
