import { useSyncExternalStore } from "react";

export type ThemeId = "teal" | "light" | "dark";

export interface ThemeMeta {
  id: ThemeId;
  label: string;
  hint: string;
}

/** 三套主题的元信息；配色本体在 styles.css 的 :root[data-theme=…] 里。 */
export const themes: ThemeMeta[] = [
  { id: "teal", label: "青色", hint: "默认 · 医疗绿" },
  { id: "light", label: "亮色", hint: "纸白 · 蓝点缀" },
  { id: "dark", label: "暗色", hint: "墨灰 · 绿点缀" },
];

const STORAGE_KEY = "app-theme";

function isThemeId(value: unknown): value is ThemeId {
  return value === "teal" || value === "light" || value === "dark";
}

function loadInitial(): ThemeId {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isThemeId(stored)) return stored;
  } catch {
    // 隐私模式等拿不到 localStorage，用默认主题即可
  }
  return "teal";
}

let current: ThemeId = loadInitial();
const listeners = new Set<() => void>();

function apply() {
  document.documentElement.dataset.theme = current;
}

apply();

export function readTheme(): ThemeId {
  return current;
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function setTheme(id: ThemeId): void {
  if (id === current) return;
  current = id;
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // 存不进去也不影响本次会话内的切换
  }
  apply();
  listeners.forEach((listener) => listener());
}

export function useTheme(): ThemeId {
  return useSyncExternalStore(subscribeTheme, readTheme);
}
