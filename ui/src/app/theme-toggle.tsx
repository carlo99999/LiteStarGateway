import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/app/use-theme";
import { Button } from "@/components/ui/button";

/** Toggles dark/light. Wired to ThemeProvider — actually switches the theme. */
export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggleTheme}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      title={isDark ? "[ light ]" : "[ dark ]"}
    >
      {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </Button>
  );
}
