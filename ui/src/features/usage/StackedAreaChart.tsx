import { useId, useMemo, useState } from "react";
import { MAX_DIRECT_SERIES, seriesColorVar } from "@/features/usage/chartColors";
import type { Granularity } from "@/features/usage/api";
import type { StackedSeries } from "@/features/usage/timeseriesChart";

const WIDTH = 720;
const HEIGHT = 220;
const PADDING_LEFT = 8;
const PADDING_RIGHT = 8;
const PADDING_TOP = 8;
const PADDING_BOTTOM = 24;
const PLOT_WIDTH = WIDTH - PADDING_LEFT - PADDING_RIGHT;
const PLOT_HEIGHT = HEIGHT - PADDING_TOP - PADDING_BOTTOM;
const GRID_LINES = 4;

function formatBucketLabel(iso: string, granularity: Granularity): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const stamp = date.toISOString();
  return granularity === "hour" ? stamp.slice(5, 13).replace("T", " ") : stamp.slice(0, 10);
}

interface StackedAreaChartProps {
  series: StackedSeries;
  granularity: Granularity;
  formatValue: (value: number) => string;
}

/** A dependency-free inline-SVG stacked area chart. Fixed categorical color
 * order (never re-sorted by value), a legend below naming every series so
 * identity never relies on color alone, and a hover crosshair + tooltip —
 * matching this repo's terminal-tech, no-new-dependency conventions (no
 * charting library is in `ui/package.json`). */
export function StackedAreaChart({ series, granularity, formatValue }: StackedAreaChartProps) {
  const gradientId = useId();
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const { points, seriesNames } = series;

  const maxValue = useMemo(() => {
    let max = 0;
    for (const point of points) {
      const stacked = Object.values(point.values).reduce((sum, v) => sum + v, 0);
      if (stacked > max) max = stacked;
    }
    return max || 1;
  }, [points]);

  const xFor = (index: number): number =>
    points.length <= 1
      ? PADDING_LEFT + PLOT_WIDTH / 2
      : PADDING_LEFT + (index / (points.length - 1)) * PLOT_WIDTH;
  const yFor = (value: number): number =>
    PADDING_TOP + PLOT_HEIGHT - (value / maxValue) * PLOT_HEIGHT;

  const areas = useMemo(() => {
    let cumulative = points.map(() => 0);
    return seriesNames.map((name, seriesIndex) => {
      const topValues = points.map((point, i) => cumulative[i]! + (point.values[name] ?? 0));
      const bottomValues = cumulative;
      const topPath = topValues
        .map((v, i) => `${i === 0 ? "M" : "L"} ${xFor(i)} ${yFor(v)}`)
        .join(" ");
      const bottomPath = bottomValues
        .map((_v, i) => `L ${xFor(points.length - 1 - i)} ${yFor(bottomValues[points.length - 1 - i]!)}`)
        .join(" ");
      cumulative = topValues;
      return {
        name,
        color: seriesColorVar(seriesIndex),
        path: `${topPath} ${bottomPath} Z`,
      };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- xFor/yFor are pure re-derivations of points/maxValue, already deps below
  }, [points, seriesNames, maxValue]);

  if (points.length === 0) return null;

  const tickIndices = (() => {
    const desired = Math.min(6, points.length);
    if (desired <= 1) return [0];
    const step = (points.length - 1) / (desired - 1);
    return Array.from({ length: desired }, (_, i) => Math.round(i * step));
  })();

  const hovered = hoverIndex !== null ? points[hoverIndex] : null;
  const directSeries = seriesNames.slice(0, MAX_DIRECT_SERIES);
  const otherCount = seriesNames.length - directSeries.length;

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full"
        role="img"
        aria-label="Stacked usage over time by model"
        onMouseLeave={() => setHoverIndex(null)}
      >
        {/* Recessive gridlines. */}
        {Array.from({ length: GRID_LINES + 1 }, (_, i) => {
          const y = PADDING_TOP + (PLOT_HEIGHT / GRID_LINES) * i;
          return (
            <line
              key={i}
              x1={PADDING_LEFT}
              x2={WIDTH - PADDING_RIGHT}
              y1={y}
              y2={y}
              stroke="var(--border)"
              strokeWidth={1}
            />
          );
        })}

        {areas.map((area) => (
          <path
            key={area.name}
            d={area.path}
            fill={area.color}
            fillOpacity={0.85}
            stroke="var(--card)"
            strokeWidth={2}
          />
        ))}

        {/* x-axis date ticks. */}
        {tickIndices.map((index) => (
          <text
            key={index}
            x={xFor(index)}
            y={HEIGHT - 6}
            textAnchor="middle"
            fontSize={9}
            fontFamily="JetBrains Mono, ui-monospace, monospace"
            fill="var(--muted-foreground)"
          >
            {formatBucketLabel(points[index]!.bucketStart, granularity)}
          </text>
        ))}

        {hoverIndex !== null ? (
          <line
            x1={xFor(hoverIndex)}
            x2={xFor(hoverIndex)}
            y1={PADDING_TOP}
            y2={PADDING_TOP + PLOT_HEIGHT}
            stroke="var(--foreground)"
            strokeWidth={1}
            strokeDasharray="2,2"
          />
        ) : null}

        {/* Invisible hover targets, one strip per point. */}
        {points.map((_point, index) => (
          <rect
            key={index}
            x={xFor(index) - PLOT_WIDTH / Math.max(1, points.length) / 2}
            y={PADDING_TOP}
            width={PLOT_WIDTH / Math.max(1, points.length)}
            height={PLOT_HEIGHT}
            fill="transparent"
            onMouseEnter={() => setHoverIndex(index)}
          />
        ))}
      </svg>

      {hovered ? (
        <div className="mt-2 rounded-md border border-border bg-background p-2 font-mono text-xs">
          <p className="mb-1 text-muted-foreground">
            {formatBucketLabel(hovered.bucketStart, granularity)}
          </p>
          <ul className="space-y-0.5">
            {seriesNames.map((name, index) => (
              <li key={name} className="flex items-center justify-between gap-4">
                <span className="flex items-center gap-1.5">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: seriesColorVar(index) }}
                  />
                  <span className="text-foreground">{name}</span>
                </span>
                <span className="tabular text-muted-foreground">
                  {formatValue(hovered.values[name] ?? 0)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Legend — always present for ≥ 2 series, so identity never relies on
       * color alone. */}
      {seriesNames.length > 1 ? (
        <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-3">
          {directSeries.map((name, index) => (
            <li key={name} className="flex items-center gap-1.5 font-mono text-xs">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: seriesColorVar(index) }}
              />
              <span className="text-muted-foreground">{name}</span>
            </li>
          ))}
          {otherCount > 0 ? (
            <li className="flex items-center gap-1.5 font-mono text-xs">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: seriesColorVar(MAX_DIRECT_SERIES) }}
              />
              <span className="text-muted-foreground">other ({otherCount})</span>
            </li>
          ) : null}
        </ul>
      ) : null}
      <span className="sr-only" id={gradientId}>
        {seriesNames.length} series over {points.length} buckets
      </span>
    </div>
  );
}
