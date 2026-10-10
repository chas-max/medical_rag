import type { ComponentType } from "react";
import ModelConfigSection from "./ModelConfigSection";

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
];

export const defaultSectionId = settingsSections[0]?.id ?? "";
