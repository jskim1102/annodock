export interface HsvColor {
  hue: number;
  saturation: number;
  value: number;
}

export interface HueSaturation {
  hue: number;
  saturation: number;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function hexChannels(hex: string): [number, number, number] {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  const normalized = match?.[1] ?? "ef4444";
  return [
    Number.parseInt(normalized.slice(0, 2), 16),
    Number.parseInt(normalized.slice(2, 4), 16),
    Number.parseInt(normalized.slice(4, 6), 16),
  ];
}

export function hexToHsv(hex: string): HsvColor {
  const [redByte, greenByte, blueByte] = hexChannels(hex);
  const red = redByte / 255;
  const green = greenByte / 255;
  const blue = blueByte / 255;
  const maximum = Math.max(red, green, blue);
  const minimum = Math.min(red, green, blue);
  const delta = maximum - minimum;
  let hue = 0;

  if (delta !== 0) {
    if (maximum === red) {
      hue = 60 * (((green - blue) / delta) % 6);
    } else if (maximum === green) {
      hue = 60 * ((blue - red) / delta + 2);
    } else {
      hue = 60 * ((red - green) / delta + 4);
    }
  }

  return {
    hue: hue < 0 ? hue + 360 : hue,
    saturation: maximum === 0 ? 0 : delta / maximum,
    value: maximum,
  };
}

export function hsvToHex({ hue, saturation, value }: HsvColor): string {
  const normalizedHue = ((hue % 360) + 360) % 360;
  const normalizedSaturation = clamp(saturation, 0, 1);
  const normalizedValue = clamp(value, 0, 1);
  const chroma = normalizedValue * normalizedSaturation;
  const hueSegment = normalizedHue / 60;
  const secondary = chroma * (1 - Math.abs((hueSegment % 2) - 1));
  let red = 0;
  let green = 0;
  let blue = 0;

  if (hueSegment < 1) {
    [red, green] = [chroma, secondary];
  } else if (hueSegment < 2) {
    [red, green] = [secondary, chroma];
  } else if (hueSegment < 3) {
    [green, blue] = [chroma, secondary];
  } else if (hueSegment < 4) {
    [green, blue] = [secondary, chroma];
  } else if (hueSegment < 5) {
    [red, blue] = [secondary, chroma];
  } else {
    [red, blue] = [chroma, secondary];
  }

  const offset = normalizedValue - chroma;
  return `#${[red, green, blue]
    .map((channel) => Math.round((channel + offset) * 255).toString(16).padStart(2, "0"))
    .join("")}`;
}

export function pointToHueSaturation(
  x: number,
  y: number,
  width: number,
  height: number,
): HueSaturation {
  const radius = Math.min(width, height) / 2;
  if (radius <= 0) return { hue: 0, saturation: 0 };

  const deltaX = x - width / 2;
  const deltaY = y - height / 2;
  const distance = Math.hypot(deltaX, deltaY);
  const hue = distance === 0
    ? 0
    : (Math.atan2(deltaY, deltaX) * 180 / Math.PI + 360) % 360;

  return {
    hue,
    saturation: clamp(distance / radius, 0, 1),
  };
}
