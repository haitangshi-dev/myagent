import { create } from 'zustand'

interface DebugState {
  visible: boolean
  toggle: () => void
  set: (v: boolean) => void
}

export const useDebugStore = create<DebugState>((set) => ({
  visible: true,
  toggle: () => set((s) => ({ visible: !s.visible })),
  set: (v) => set({ visible: v }),
}))
