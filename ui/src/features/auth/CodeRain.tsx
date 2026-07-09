import { useMemo } from "react";

const GLYPHS = "01<>/{}$#=+*abcdef0123456789";

function randomColumn(length: number): string {
  let out = "";
  for (let i = 0; i < length; i += 1) {
    out += GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
  }
  return out;
}

/**
 * Very-low-opacity animated "code-rain" backdrop. Login screen ONLY — data
 * screens stay still (this is an operational console).
 */
export function CodeRain({ columns = 24 }: { columns?: number }) {
  const streams = useMemo(
    () =>
      Array.from({ length: columns }).map((_, i) => ({
        id: i,
        left: `${(i / columns) * 100}%`,
        delay: `${-(Math.random() * 12).toFixed(2)}s`,
        duration: `${(8 + Math.random() * 8).toFixed(2)}s`,
        text: randomColumn(28),
      })),
    [columns],
  );

  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 overflow-hidden opacity-[0.06]"
    >
      {streams.map((s) => (
        <div
          key={s.id}
          className="absolute top-0 animate-code-rain whitespace-pre font-mono text-xs leading-4 text-primary"
          style={{ left: s.left, animationDelay: s.delay, animationDuration: s.duration }}
        >
          {s.text.split("").map((ch, idx) => (
            <div key={idx}>{ch}</div>
          ))}
        </div>
      ))}
    </div>
  );
}
