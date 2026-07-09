import { useContext } from "react";
import { ThemeContext } from "@/app/theme-provider";

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return ctx;
}
