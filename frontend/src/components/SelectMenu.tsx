import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { Icon } from "./Icon";

export interface SelectMenuOption {
  value: string;
  label: string;
}

interface SelectMenuProps {
  id?: string;
  value?: string;
  defaultValue?: string;
  options: SelectMenuOption[];
  onChange?: (value: string) => void;
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
}

export function SelectMenu({
  id,
  value,
  defaultValue,
  options,
  onChange,
  ariaLabel,
  className,
  disabled = false,
}: SelectMenuProps) {
  const generatedId = useId();
  const triggerId = id ?? `select-menu-${generatedId}`;
  const listboxId = `${triggerId}-listbox`;
  const controlled = value !== undefined;
  const [internalValue, setInternalValue] = useState(
    defaultValue ?? options[0]?.value ?? "",
  );
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedValue = controlled ? value : internalValue;
  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === selectedValue),
  );
  const selectedOption = options[selectedIndex];

  useEffect(() => {
    if (!open) return;

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const focusOption = (index: number) => {
    requestAnimationFrame(() => optionRefs.current[index]?.focus());
  };

  const openAt = (index: number) => {
    if (disabled || options.length === 0) return;
    setOpen(true);
    focusOption(Math.min(Math.max(index, 0), options.length - 1));
  };

  const choose = (nextValue: string) => {
    if (!controlled) setInternalValue(nextValue);
    onChange?.(nextValue);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      openAt(selectedIndex);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      openAt(selectedIndex);
    } else if (event.key === "Home") {
      event.preventDefault();
      openAt(0);
    } else if (event.key === "End") {
      event.preventDefault();
      openAt(options.length - 1);
    }
  };

  const handleOptionKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusOption((index + 1) % options.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusOption((index - 1 + options.length) % options.length);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusOption(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusOption(options.length - 1);
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className={`select-menu${className ? ` ${className}` : ""}`} ref={rootRef}>
      <button
        className="select select-menu-trigger"
        id={triggerId}
        type="button"
        ref={triggerRef}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        onClick={() => {
          if (open) setOpen(false);
          else openAt(selectedIndex);
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        <span>{selectedOption?.label ?? "선택"}</span>
        <Icon name={open ? "chevron-up" : "chevron-down"} size={15} />
      </button>
      {open ? (
        <div className="select-menu-popover" id={listboxId} role="listbox" aria-labelledby={ariaLabel ? undefined : triggerId}>
          {options.map((option, index) => (
            <button
              className={`select-menu-option${option.value === selectedValue ? " is-selected" : ""}`}
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === selectedValue}
              ref={(node) => { optionRefs.current[index] = node; }}
              onClick={() => choose(option.value)}
              onKeyDown={(event) => handleOptionKeyDown(event, index)}
            >
              <span>{option.label}</span>
              {option.value === selectedValue ? <Icon name="check" size={14} /> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
