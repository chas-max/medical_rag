import { afterEach, beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

afterEach(() => {
  vi.resetModules();
});

it("applies the theme saved in localStorage when the module first loads", async () => {
  localStorage.setItem("app-theme", "dark");
  const theme = await import("../settings/theme");
  expect(theme.readTheme()).toBe("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
});

it("falls back to teal when the stored value is unknown", async () => {
  localStorage.setItem("app-theme", "solarized");
  const theme = await import("../settings/theme");
  expect(theme.readTheme()).toBe("teal");
  expect(document.documentElement.dataset.theme).toBe("teal");
});

it("switching a theme writes the attribute, persists it and notifies subscribers", async () => {
  const theme = await import("../settings/theme");
  const seen: string[] = [];
  const stop = theme.subscribeTheme(() => seen.push(theme.readTheme()));
  theme.setTheme("light");
  expect(theme.readTheme()).toBe("light");
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem("app-theme")).toBe("light");
  theme.setTheme("light");
  expect(seen).toEqual(["light"]);
  stop();
  theme.setTheme("dark");
  expect(seen).toEqual(["light"]);
  expect(theme.readTheme()).toBe("dark");
});

it("exposes exactly the three shipped themes", async () => {
  const { themes } = await import("../settings/theme");
  expect(themes.map((item) => item.id)).toEqual(["teal", "light", "dark"]);
});
