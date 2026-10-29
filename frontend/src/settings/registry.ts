import type { ComponentType } from "react";
import AppearanceSection from "./AppearanceSection";
import ModelConfigSection from "./ModelConfigSection";
import MemorySection from "./MemorySection";

/**
 * The settings dialog renders one entry per item here. Adding a section — retrieval
 * thresholds, web search keys, anything — means appending to this array and dropping
 * in a component; the gear entry point, the navigation and the pane need no changes.
 */
export interface SettingsSection {
  id: string;
  label: string;
  hint: string;
  icon: string;
  component: ComponentType;
}

export const settingsSections: SettingsSection[] = [
  {
    id: "model",
    label: "模型配置",
    hint: "模型名称 / Base URL / API Key",
    icon: "◈",
    component: ModelConfigSection,
  },
  {
    id: "memory",
    label: "长期记忆",
    hint: "用户画像 / 情景 / 隐私控制",
    icon: "◇",
    component: MemorySection,
  },
  {
    id: "appearance",
    label: "外观",
    hint: "界面主题 · 青色 / 亮色 / 暗色",
    icon: "◐",
    component: AppearanceSection,
  },
];

export const defaultSectionId = settingsSections[0]?.id ?? "";
