const BYTES_PER_GIB = 1024 ** 3;

export function quotaBytesFromGiB(value: string): number | null {
  if (value.trim() === "") return null;
  const gibibytes = Number(value);
  if (!Number.isFinite(gibibytes) || gibibytes < 0.1) return null;
  const bytes = Math.round(gibibytes * BYTES_PER_GIB);
  if (!Number.isSafeInteger(bytes) || bytes <= 0) return null;
  return bytes;
}

export function quotaGiBFromBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const gibibytes = bytes / BYTES_PER_GIB;
  if (Number.isInteger(gibibytes)) return String(gibibytes);
  return String(Number(gibibytes.toFixed(3)));
}
