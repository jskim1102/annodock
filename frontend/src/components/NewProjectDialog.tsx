import { useEffect, useRef, useState } from "react";

import type { ProjectRow } from "../api/client";
import { CLASS_COLOR_PRESETS } from "../utils/classColors";
import { ClassColorPicker } from "./ClassColorPicker";
import { Icon } from "./Icon";
import { SelectMenu } from "./SelectMenu";

interface ProjectClass {
  id: number;
  name: string;
  color: string;
}

export interface NewProjectInput {
  name: string;
  classes: Array<{ name: string; color: string }>;
}

interface NewProjectDialogProps {
  projects: ProjectRow[];
  onClose: () => void;
  onCreate: (project: NewProjectInput) => void;
}

type ClassInputMode = "direct" | "existing";

export function NewProjectDialog({ projects, onClose, onCreate }: NewProjectDialogProps) {
  const [name, setName] = useState("");
  const [classInputMode, setClassInputMode] = useState<ClassInputMode>("direct");
  const [sourceDatasetId, setSourceDatasetId] = useState<number | null>(null);
  const [classes, setClasses] = useState<ProjectClass[]>([
    { id: 1, name: "", color: CLASS_COLOR_PRESETS[0].value },
  ]);
  const [colorClassId, setColorClassId] = useState<number | null>(null);
  const nextId = useRef(2);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const sourceDatasets = projects.flatMap((project) =>
    project.datasets.map((dataset) => ({ sourceProject: project, dataset })),
  );
  const selectedSource = sourceDatasets.find(({ dataset }) => dataset.id === sourceDatasetId);
  const canCreate = Boolean(name.trim());

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <section
        className="dialog new-project-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-project-title"
      >
        <button className="btn btn-ghost btn-sm dialog-close" type="button" aria-label="닫기" onClick={onClose}>
          <Icon name="x" size={16} />
        </button>
        <h2 className="dialog-title" id="new-project-title">새 프로젝트</h2>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!canCreate) return;
            const normalizedClasses = classInputMode === "existing"
              ? selectedSource
                ? selectedSource.sourceProject.classes.map((item) => ({
                    name: item.name,
                    color: item.color,
                  }))
                : []
              : classes
                  .map((item) => ({ name: item.name.trim(), color: item.color }))
                  .filter((item) => item.name.length > 0);
            onCreate({ name: name.trim(), classes: normalizedClasses });
          }}
        >
          <div className="field">
            <label htmlFor="project-name">프로젝트명</label>
            <input
              ref={nameRef}
              className="input"
              id="project-name"
              value={name}
              placeholder="프로젝트 이름 입력"
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="field class-field">
            <label>클래스 — 이름과 표시 색 (선택)</label>
            <div className="class-input-options" role="radiogroup" aria-label="클래스 입력 방식">
              <label className="class-input-option">
                <input
                  type="radio"
                  name="class-input-mode"
                  value="direct"
                  checked={classInputMode === "direct"}
                  onChange={() => {
                    setClassInputMode("direct");
                    setColorClassId(null);
                  }}
                />
                <span>신규 데이터셋 · 클래스 직접 입력</span>
              </label>
              <label className="class-input-option">
                <input
                  type="radio"
                  name="class-input-mode"
                  value="existing"
                  checked={classInputMode === "existing"}
                  onChange={() => {
                    setClassInputMode("existing");
                    setColorClassId(null);
                  }}
                />
                <span>기존 데이터셋 · 기존 클래스 활용</span>
              </label>
            </div>
            {classInputMode === "direct" ? (
              <>
                <div className="class-editor-list">
                  {classes.map((item, index) => (
                    <div className="class-editor-row" key={item.id}>
                      <div className="class-color-cell">
                        <button
                          className="class-swatch"
                          style={{ background: item.color }}
                          type="button"
                          aria-label={`${item.name || `${index + 1}번`} 클래스 색 변경`}
                          aria-haspopup="dialog"
                          aria-expanded={colorClassId === item.id}
                          onClick={() => setColorClassId((current) => current === item.id ? null : item.id)}
                        />
                        {colorClassId === item.id ? (
                          <ClassColorPicker
                            className={item.name || `${index + 1}번 클래스`}
                            color={item.color}
                            top={34}
                            placement="start"
                            onChange={(color) =>
                              setClasses((current) =>
                                current.map((candidate) =>
                                  candidate.id === item.id ? { ...candidate, color } : candidate,
                                ),
                              )
                            }
                            onClose={() => setColorClassId(null)}
                          />
                        ) : null}
                      </div>
                      <input
                        className="input"
                        value={item.name}
                        aria-label={`${index + 1}번 클래스 이름`}
                        onChange={(event) =>
                          setClasses((current) =>
                            current.map((candidate) =>
                              candidate.id === item.id
                                ? { ...candidate, name: event.target.value }
                                : candidate,
                            ),
                          )
                        }
                      />
                      <button
                        className="btn btn-ghost btn-sm class-remove"
                        type="button"
                        aria-label={`${item.name || `${index + 1}번 클래스`} 삭제`}
                        disabled={classes.length === 1}
                        onClick={() => setClasses((current) => current.filter((candidate) => candidate.id !== item.id))}
                      >
                        <Icon name="x" size={14} />
                      </button>
                    </div>
                  ))}
                </div>
                <button
                  className="btn btn-ghost btn-sm add-class-button"
                  type="button"
                  onClick={() => {
                    const id = nextId.current++;
                    setClasses((current) => [
                      ...current,
                      {
                        id,
                        name: "",
                        color: CLASS_COLOR_PRESETS[current.length % CLASS_COLOR_PRESETS.length].value,
                      },
                    ]);
                  }}
                >
                  <Icon name="plus" size={14} /> 클래스 추가
                </button>
                <div className="hint">
                  스와치를 눌러 색상을 선택하세요. 비워두면 첫 데이터셋 업로드 시 클래스가 설정됩니다.
                </div>
              </>
            ) : sourceDatasets.length === 0 ? (
              <div className="class-source-empty" role="status">
                기존 데이터셋이 없습니다. 프로젝트는 빈 클래스 상태로 생성되며 첫 데이터셋 업로드 시 클래스가 설정됩니다.
              </div>
            ) : (
              <div className="class-source-panel">
                <label className="field-label" htmlFor="class-source-dataset">가져올 데이터셋</label>
                <SelectMenu
                  id="class-source-dataset"
                  value={String(sourceDatasetId ?? "")}
                  options={[
                    { value: "", label: "데이터셋 선택" },
                    ...sourceDatasets.map(({ sourceProject, dataset }) => ({
                      value: String(dataset.id),
                      label: `${sourceProject.name} — ${dataset.name}`,
                    })),
                  ]}
                  onChange={(nextValue) => setSourceDatasetId(
                    nextValue ? Number(nextValue) : null,
                  )}
                />
                <div className="hint class-source-guidance">
                  선택한 데이터셋이 속한 프로젝트의 클래스 정보만 가져오며 데이터셋은 이동하거나 복제하지 않습니다.
                </div>
                {selectedSource ? (
                  selectedSource.sourceProject.classes.length > 0 ? (
                    <div
                      className="class-source-preview"
                      aria-label={`${selectedSource.sourceProject.name} 클래스 ${selectedSource.sourceProject.classes.length}개`}
                    >
                      {selectedSource.sourceProject.classes.map((item) => (
                        <span className="class-source-chip" key={item.class_id}>
                          <i style={{ background: item.color }} />
                          {item.name}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <div className="class-source-empty" role="status">
                      선택한 데이터셋의 프로젝트에 등록된 클래스가 없습니다. 빈 클래스 상태로 생성한 뒤 첫 데이터셋을 업로드할 수 있습니다.
                    </div>
                  )
                ) : (
                  <div className="hint">
                    선택하지 않으면 빈 클래스 상태로 생성되며 첫 데이터셋 업로드 시 클래스가 설정됩니다.
                  </div>
                )}
              </div>
            )}
          </div>
          <div className="dialog-actions">
            <button className="btn btn-secondary" type="button" onClick={onClose}>취소</button>
            <button className="btn btn-primary" type="submit" disabled={!canCreate}>만들기</button>
          </div>
        </form>
      </section>
    </div>
  );
}
