import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useRef,
  useState,
} from "react";

import { CLASS_COLOR_PRESETS } from "../utils/classColors";
import {
  hexToHsv,
  hsvToHex,
  pointToHueSaturation,
  type HsvColor,
} from "../utils/colorMath";

interface ClassColorPickerProps {
  className: string;
  color: string;
  top: number;
  placement?: "start" | "end";
  onChange: (color: string) => void;
  onClose: () => void;
}

export function ClassColorPicker({
  className,
  color,
  top,
  placement = "end",
  onChange,
  onClose,
}: ClassColorPickerProps) {
  const pickerRef = useRef<HTMLDivElement>(null);
  const initialHsv = hexToHsv(color);
  const [hsv, setHsv] = useState<HsvColor>(initialHsv);
  const lastCommittedColor = useRef(hsvToHex(initialHsv));

  useEffect(() => {
    const externalColor = hsvToHex(hexToHsv(color));
    if (externalColor === lastCommittedColor.current) return;
    lastCommittedColor.current = externalColor;
    setHsv(hexToHsv(externalColor));
  }, [color]);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !pickerRef.current?.contains(event.target)) {
        onClose();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  const commit = (next: HsvColor) => {
    const nextColor = hsvToHex(next);
    lastCommittedColor.current = nextColor;
    setHsv(next);
    onChange(nextColor);
  };

  const updateFromPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const hueSaturation = pointToHueSaturation(
      event.clientX - bounds.left,
      event.clientY - bounds.top,
      bounds.width,
      bounds.height,
    );
    commit({ ...hsv, ...hueSaturation });
  };

  const handleWheelKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    let next: HsvColor | null = null;
    if (event.key === "ArrowLeft") {
      next = { ...hsv, hue: (hsv.hue + 355) % 360 };
    } else if (event.key === "ArrowRight") {
      next = { ...hsv, hue: (hsv.hue + 5) % 360 };
    } else if (event.key === "ArrowUp") {
      next = { ...hsv, saturation: Math.min(1, hsv.saturation + 0.05) };
    } else if (event.key === "ArrowDown") {
      next = { ...hsv, saturation: Math.max(0, hsv.saturation - 0.05) };
    }
    if (!next) return;
    event.preventDefault();
    commit(next);
  };

  const markerAngle = hsv.hue * Math.PI / 180;
  const markerLeft = 50 + Math.cos(markerAngle) * hsv.saturation * 50;
  const markerTop = 50 + Math.sin(markerAngle) * hsv.saturation * 50;
  const currentColor = hsvToHex(hsv);
  const fullBrightnessColor = hsvToHex({ ...hsv, value: 1 });

  return (
    <div
      ref={pickerRef}
      className={`class-color-picker class-color-picker-${placement}`}
      role="dialog"
      aria-label={`${className} 클래스 색상 선택`}
      style={{ top }}
      onClick={(event) => event.stopPropagation()}
    >
      <div className="class-color-picker-header">
        <strong>{className} 색상</strong>
        <output className="class-color-current mono">
          <span aria-hidden="true" style={{ background: currentColor }} />
          {currentColor.toUpperCase()}
        </output>
      </div>

      <div
        className="class-color-wheel"
        role="slider"
        tabIndex={0}
        aria-label={`${className} 색조와 채도`}
        aria-valuemin={0}
        aria-valuemax={359}
        aria-valuenow={Math.round(hsv.hue)}
        aria-valuetext={`색조 ${Math.round(hsv.hue)}도, 채도 ${Math.round(hsv.saturation * 100)}%`}
        onKeyDown={handleWheelKeyDown}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          updateFromPointer(event);
        }}
        onPointerMove={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            updateFromPointer(event);
          }
        }}
        onPointerUp={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
        }}
        onPointerCancel={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
        }}
      >
        <span
          className="class-color-wheel-shade"
          aria-hidden="true"
          style={{ opacity: 1 - hsv.value }}
        />
        <span
          className="class-color-wheel-marker"
          aria-hidden="true"
          style={{ left: `${markerLeft}%`, top: `${markerTop}%` }}
        />
      </div>

      <label className="class-color-brightness">
        <span>밝기</span>
        <input
          type="range"
          min={0}
          max={100}
          value={Math.round(hsv.value * 100)}
          aria-label={`${className} 색상 밝기`}
          style={{ background: `linear-gradient(90deg, #000000, ${fullBrightnessColor})` }}
          onChange={(event) => commit({ ...hsv, value: Number(event.target.value) / 100 })}
        />
      </label>

      <span className="class-color-preset-label">기본 팔레트</span>
      <div className="class-color-grid">
        {CLASS_COLOR_PRESETS.map((preset) => (
          <button
            type="button"
            key={preset.token}
            className="class-color-option"
            title={`${preset.token} · ${preset.value.toUpperCase()}`}
            aria-label={`${preset.token} ${preset.value.toUpperCase()} 색상`}
            aria-pressed={currentColor === preset.value}
            style={{ background: preset.value }}
            onClick={() => commit(hexToHsv(preset.value))}
          />
        ))}
      </div>
      <button className="class-color-close" type="button" onClick={onClose}>완료</button>
    </div>
  );
}
