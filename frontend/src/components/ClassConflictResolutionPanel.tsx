import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  type ClassResolutionAction,
  type ClassResolutionChoice,
  type ClassResolutionPlan,
} from "../api/client";
import { Icon } from "./Icon";

interface ClassConflictResolutionPanelProps {
  plan: ClassResolutionPlan;
  initialChoices: Record<string, ClassResolutionAction>;
  affectedDatasetCount: number;
  busy: boolean;
  error: string | null;
  onSubmit: (resolutions: ClassResolutionChoice[]) => void;
}

export function ClassConflictResolutionPanel({
  plan,
  initialChoices,
  affectedDatasetCount,
  busy,
  error,
  onSubmit,
}: ClassConflictResolutionPanelProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [choices, setChoices] = useState<Record<string, ClassResolutionAction>>(
    () => initialChoices,
  );

  useEffect(() => {
    setChoices(initialChoices);
    headingRef.current?.focus();
  }, [plan.revision]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit(plan.conflicts.map((conflict) => ({
      key: conflict.key,
      action: choices[conflict.key] ?? "use_project",
    })));
  };

  return (
    <section
      className="class-resolution-panel"
      aria-labelledby="class-resolution-title"
      aria-live="polite"
    >
      <div className="class-resolution-heading">
        <Icon name="warning" size={20} />
        <div>
          <h3 id="class-resolution-title" ref={headingRef} tabIndex={-1}>
            클래스 명칭 확인이 필요합니다
          </h3>
          <p>
            프로젝트와 업로드 데이터셋의 클래스명이 다릅니다. 각 클래스에 사용할
            이름을 선택하면 같은 업로드 작업을 이어서 처리합니다. 이번 업로드의
            같은 클래스명 차이에는 선택이 자동으로 적용됩니다.
          </p>
        </div>
      </div>

      <form onSubmit={submit}>
        <div className="class-conflict-list">
          {plan.conflicts.map((conflict, index) => {
            const groupName = `class-conflict-${index}`;
            const warningId = `${groupName}-project-warning`;
            return (
              <fieldset className="class-conflict-item" disabled={busy} key={conflict.key}>
                <legend>클래스 ID {conflict.class_id} 명칭 선택</legend>
                <p className="class-conflict-source">
                  업로드 파일 <span className="mono">{conflict.source_path}</span>
                </p>
                <div className="class-conflict-comparison" aria-label="클래스명 비교">
                  <div>
                    <span>프로젝트 클래스명</span>
                    <strong>{conflict.project_name}</strong>
                  </div>
                  <Icon name="chevron-right" size={18} />
                  <div>
                    <span>업로드 클래스명</span>
                    <strong>{conflict.uploaded_name}</strong>
                  </div>
                </div>

                <div className="class-resolution-options">
                  <label className="class-resolution-option">
                    <input
                      type="radio"
                      name={groupName}
                      value="use_project"
                      checked={(choices[conflict.key] ?? "use_project") === "use_project"}
                      onChange={() => setChoices((current) => ({
                        ...current,
                        [conflict.key]: "use_project",
                      }))}
                    />
                    <span>
                      <strong>업로드 클래스명 수정</strong>
                      <small>
                        프로젝트 클래스명을 기준으로 이 데이터셋에만 적용
                        <b>{conflict.uploaded_name} → {conflict.project_name}</b>
                      </small>
                    </span>
                  </label>
                  <label className="class-resolution-option">
                    <input
                      type="radio"
                      name={groupName}
                      value="use_upload"
                      checked={choices[conflict.key] === "use_upload"}
                      aria-describedby={warningId}
                      onChange={() => setChoices((current) => ({
                        ...current,
                        [conflict.key]: "use_upload",
                      }))}
                    />
                    <span>
                      <strong>프로젝트 클래스명 수정</strong>
                      <small>
                        업로드 클래스명으로 프로젝트 기준을 변경
                        <b>{conflict.project_name} → {conflict.uploaded_name}</b>
                      </small>
                      <em className="class-resolution-warning" id={warningId}>
                        기존 데이터셋 {affectedDatasetCount.toLocaleString()}개에도 적용됩니다.
                      </em>
                    </span>
                  </label>
                </div>
              </fieldset>
            );
          })}
        </div>

        {error ? <p className="class-resolution-error" role="alert">{error}</p> : null}
        <div className="class-resolution-actions">
          <span>선택을 적용하기 전까지 업로드 처리가 일시 중지됩니다.</span>
          <button
            className="btn btn-primary"
            type="submit"
            disabled={busy || plan.conflicts.length === 0}
          >
            {busy ? "적용 중…" : "선택한 이름으로 업로드 계속"}
          </button>
        </div>
      </form>
    </section>
  );
}
