export function latestMetricValue<T extends object>(metrics: T[], key: keyof T) {
  for (let index = metrics.length - 1; index >= 0; index -= 1) {
    const value = metrics[index]?.[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

export function formatMetricValue(value: number) {
  return value.toFixed(4);
}

interface MetricValuePoint {
  key: string;
  y: number;
}

export interface PositionedMetricValue extends MetricValuePoint {
  labelY: number;
}

export function positionMetricValueLabels(
  points: MetricValuePoint[],
  minY: number,
  maxY: number,
  minimumGap: number,
): PositionedMetricValue[] {
  if (points.length === 0) return [];

  const positioned = points
    .map((point, index) => ({
      ...point,
      index,
      labelY: Math.min(maxY, Math.max(minY, point.y)),
    }))
    .sort((left, right) => left.labelY - right.labelY);

  for (let index = 1; index < positioned.length; index += 1) {
    positioned[index].labelY = Math.max(
      positioned[index].labelY,
      positioned[index - 1].labelY + minimumGap,
    );
  }

  const overflow = positioned[positioned.length - 1].labelY - maxY;
  if (overflow > 0) {
    for (const point of positioned) point.labelY -= overflow;
  }

  for (let index = positioned.length - 2; index >= 0; index -= 1) {
    positioned[index].labelY = Math.min(
      positioned[index].labelY,
      positioned[index + 1].labelY - minimumGap,
    );
  }

  const underflow = minY - positioned[0].labelY;
  if (underflow > 0) {
    for (const point of positioned) point.labelY += underflow;
  }

  return positioned
    .sort((left, right) => left.index - right.index)
    .map(({ index: _index, ...point }) => point);
}
