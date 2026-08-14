import { useEffect, useRef, useState, type WheelEvent } from "react";

import { imageResourceUrl, type ImageRow } from "../api/client";
import { AuthenticatedImage } from "./AuthenticatedImage";
import { Icon } from "./Icon";

interface ThumbStripProps {
  total: number;
  currentIndex: number;
  items: Map<number, ImageRow>;
  onSelect: (index: number) => void;
  onNavigate: (offset: -1 | 1) => void;
  onRangeRequest: (from: number, to: number) => void;
}

// 썸네일 한 칸의 기준 폭 (기본 86px + gap 6px) — 실제 폭은 목록을 채우도록 늘어난다
const THUMB_SLOT_WIDTH = 86 + 6;
const MAX_VISIBLE_THUMBS = 17;
const FALLBACK_VISIBLE_THUMBS = 9;

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

export function ThumbStrip({
  total,
  currentIndex,
  items,
  onSelect,
  onNavigate,
  onRangeRequest,
}: ThumbStripProps) {
  const wheelDeltaRef = useRef(0);
  const lastWheelAtRef = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const [visibleCount, setVisibleCount] = useState(FALLBACK_VISIBLE_THUMBS);

  // 스트립 폭에 맞춰 표시 개수를 계산한다 — 창 크기가 바뀌면 다시 잰다.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const measure = () => {
      const count = clamp(Math.floor((list.clientWidth + 6) / THUMB_SLOT_WIDTH), 1, MAX_VISIBLE_THUMBS);
      setVisibleCount(count);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(list);
    return () => observer.disconnect();
  }, []);

  // 현재 이미지가 창 안에 있으면 창을 움직이지 않는다 — 보이는 썸네일을
  // 클릭했을 때 목록이 밀리지 않도록. 창 밖으로 나가면 페이지 단위로 넘긴다:
  // 오른쪽 끝을 넘으면 다음 이미지가 첫 칸에, 왼쪽 끝을 넘으면 마지막 칸에.
  const [start, setStart] = useState(0);
  const [scrubValue, setScrubValue] = useState<number | null>(null);
  const maxStart = Math.max(0, total - visibleCount);
  useEffect(() => {
    setStart((current) => {
      const clamped = clamp(current, 0, maxStart);
      if (currentIndex < clamped) {
        return clamp(currentIndex - visibleCount + 1, 0, maxStart);
      }
      if (currentIndex >= clamped + visibleCount) {
        return clamp(currentIndex, 0, maxStart);
      }
      return clamped;
    });
  }, [currentIndex, visibleCount, maxStart]);
  const visible = Array.from({ length: Math.min(visibleCount, total) }, (_, offset) => start + offset);

  useEffect(() => {
    onRangeRequest(start, Math.min(total, start + visibleCount));
  }, [onRangeRequest, start, total, visibleCount]);

  const onWheel = (event: WheelEvent<HTMLElement>) => {
    if (event.ctrlKey || event.metaKey || Math.abs(event.deltaY) < Math.abs(event.deltaX)) return;
    wheelDeltaRef.current += event.deltaY;
    const now = performance.now();
    if (Math.abs(wheelDeltaRef.current) < 40 || now - lastWheelAtRef.current < 180) return;
    onNavigate(wheelDeltaRef.current > 0 ? 1 : -1);
    wheelDeltaRef.current = 0;
    lastWheelAtRef.current = now;
  };

  return (
    <footer className="thumb-strip" aria-label="이미지 썸네일" onWheel={onWheel}>
      <div className="thumb-scrub-column">
        <input
          className="thumb-scrub"
          type="range"
          min={0}
          max={Math.max(0, total - 1)}
          value={scrubValue ?? currentIndex}
          aria-label="이미지 위치 이동"
          disabled={total === 0}
          onChange={(event) => setScrubValue(Number(event.target.value))}
          onPointerUp={() => {
            if (scrubValue !== null) onSelect(scrubValue);
            setScrubValue(null);
          }}
          onKeyUp={() => {
            if (scrubValue !== null) onSelect(scrubValue);
            setScrubValue(null);
          }}
        />
      <div className="thumb-list" ref={listRef}>
        {visible.map((index) => {
          const image = items.get(index);
          return (
          <button
            className={`viewer-thumb${currentIndex === index ? " is-current" : ""}`}
            type="button"
            key={index}
            aria-label={`${index + 1}번 이미지`}
            aria-current={currentIndex === index ? "true" : undefined}
            onClick={() => onSelect(index)}
          >
            {image ? <AuthenticatedImage resourcePath={imageResourceUrl(image.id, "thumb")} alt="" draggable="false" /> : null}
            {image?.is_modified ? <span className="thumb-warning">●</span> : null}
          </button>
          );
        })}
      </div>
      </div>
      <div className="thumb-pager">
        <button
          type="button"
          aria-label="이전 이미지"
          disabled={currentIndex === 0}
          onClick={() => onNavigate(-1)}
        >
          <Icon name="chevron-left" size={14} />
        </button>
        <span className="mono">{total === 0 ? 0 : currentIndex + 1} / {total}</span>
        <button
          type="button"
          aria-label="다음 이미지"
          disabled={total === 0 || currentIndex === total - 1}
          onClick={() => onNavigate(1)}
        >
          <Icon name="chevron-right" size={14} />
        </button>
      </div>
    </footer>
  );
}
