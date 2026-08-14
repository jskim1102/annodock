import { useTheme } from "../theme";
import { Icon } from "./Icon";

export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const next = theme === "light" ? "다크" : "라이트";

  return (
    <button
      className="btn btn-ghost btn-sm icon-button"
      type="button"
      aria-label={`${next} 테마로 전환`}
      title={`${next} 테마로 전환`}
      onClick={toggleTheme}
    >
      <Icon name={theme === "light" ? "moon" : "sun"} size={15} />
    </button>
  );
}

